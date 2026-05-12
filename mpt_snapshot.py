"""
Background writer for the MPT registry.

Walks every MPTokenIssuance on mainnet, decodes XLS-89 metadata,
enriches each issuance with a holders-block (mpt_holders walk, balance
distribution, top-N), and writes mpt_snapshot.json atomically.

Schema:
  v3 (current) — `holders` is a dict per row:
      {with_balance, authorized, top, walked_complete, walked_at, reason}
      reason ∈ {"pending", "incomplete", "no_holders", "skipped_test", "complete"}
      — exhaustive, non-null. Consumers dispatch on the string, never on null.
  v2 (legacy)  — `holders` is an int (positive-balance count, buggy: always 0)
                 or None. Readers MUST normalize via a single helper for one
                 cycle, then the v2 fallback can be deleted.

Policy (per Charlie):
  * Positive-balance count is the headline; authorized is stored alongside.
  * Issuer self-holdings are excluded from both counts and top-N.
  * Test-status issuances are skipped on the write path (reason="skipped_test")
    so renderers can still produce "—" with the right machine-readable cause.
  * Page cap is a defensive guard, not a budget — at current scale (top MPT has
    10 holders) no walk pages past 1.

Cadence: hourly via launchd. Run manually: venv/bin/python mpt_snapshot.py
"""

import json
import os
import sys
import time

import httpx

import db
from mpt_data import (
    SNAPSHOT_PATH,
    XRPL_NODE,
    _amount_to_int,
    fetch_mpt_data,
)

SCHEMA_VERSION = 3

HOLDER_PAGE_CAP = int(os.environ.get("MPT_HOLDER_PAGE_CAP", "50"))
HOLDER_PAGE_LIMIT = int(os.environ.get("MPT_HOLDER_PAGE_LIMIT", "400"))
HOLDER_TOP_N = int(os.environ.get("MPT_HOLDER_TOP_N", "20"))
HOLDER_RPC_PAUSE = float(os.environ.get("MPT_HOLDER_RPC_PAUSE", "0.05"))

# Same heuristic as app.py:_classify_mpt_status — duplicated here to keep
# the worker independent of Flask import paths. If a third caller needs it,
# promote to mpt_data.
_MPT_TEST_TICKERS = {"TEST", "TMPT"}
_MPT_TEST_NAME_PREFIXES = ("test ", "test mpt")


def _is_test_issuance(row):
    ticker = (row.get("ticker") or "").upper()
    name = (row.get("name") or "").strip().lower()
    if ticker in _MPT_TEST_TICKERS:
        return True
    return any(name.startswith(p) for p in _MPT_TEST_NAME_PREFIXES)


def _rpc(method, params):
    try:
        resp = httpx.post(
            XRPL_NODE,
            json={"method": method, "params": [params]},
            timeout=15.0,
        )
        return (resp.json() or {}).get("result") or {}
    except Exception:
        return None


def _holders_pending(reason="pending"):
    return {
        "with_balance": None,
        "authorized": None,
        "top": [],
        "walked_complete": False,
        "walked_at": None,
        "reason": reason,
    }


def _walk_holders(issuance_id, issuer):
    """Walk mpt_holders pages for one issuance. Excludes issuer self-holdings
    from counts and top-N. Returns the v3 holders dict.

    `reason` enum (exhaustive, non-null):
      "pending"     — never walked (cold-start) or RPC error
      "incomplete"  — page cap fired before exhausting the marker chain
      "no_holders"  — walked clean, zero non-issuer holders
      "complete"    — walked clean, with at least one non-issuer holder
    """
    if not issuance_id:
        return _holders_pending("pending")

    rows = []
    marker = None
    pages = 0
    walked_at = int(time.time())

    while pages < HOLDER_PAGE_CAP:
        params = {"mpt_issuance_id": issuance_id, "limit": HOLDER_PAGE_LIMIT}
        if marker is not None:
            params["marker"] = marker
        result = _rpc("mpt_holders", params)
        if result is None or "error" in (result or {}):
            return _holders_pending("pending")
        rows.extend(result.get("mptokens") or [])
        pages += 1
        marker = result.get("marker")
        if not marker:
            break
        time.sleep(HOLDER_RPC_PAUSE)

    walked_complete = marker is None
    if not walked_complete:
        return {
            "with_balance": None,
            "authorized": None,
            "top": [],
            "walked_complete": False,
            "walked_at": walked_at,
            "reason": "incomplete",
        }

    non_issuer = [h for h in rows if h.get("account") != issuer]
    authorized = len(non_issuer)
    with_balance = sum(
        1 for h in non_issuer if _amount_to_int(h.get("mpt_amount")) > 0
    )

    # Sort: balance desc, account asc for deterministic tie-break across
    # re-renders. Same ordering every run for the same ledger state.
    sorted_holders = sorted(
        non_issuer,
        key=lambda h: (-_amount_to_int(h.get("mpt_amount")), h.get("account") or ""),
    )
    top = [
        {
            "account": h.get("account"),
            "mpt_amount": h.get("mpt_amount"),
            "is_issuer": False,
        }
        for h in sorted_holders[:HOLDER_TOP_N]
    ]

    return {
        "with_balance": with_balance,
        "authorized": authorized,
        "top": top,
        "walked_complete": True,
        "walked_at": walked_at,
        "reason": "complete" if authorized > 0 else "no_holders",
    }


def _enrich_holders(data):
    """Walk holders for every non-test issuance. Test issuances are stamped
    with reason="skipped_test" so renderers can label them distinctly from
    "we haven't walked yet" or "walk failed"."""
    rows = data.get("issuances") or []
    walked = 0
    skipped_test = 0
    for r in rows:
        if _is_test_issuance(r):
            r["holders"] = _holders_pending("skipped_test")
            skipped_test += 1
            continue
        r["holders"] = _walk_holders(r.get("issuance_id"), r.get("issuer"))
        walked += 1
    data["holders_walked"] = walked
    data["holders_skipped_test"] = skipped_test


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def run_once(path=None):
    path = path or SNAPSHOT_PATH

    t0 = time.time()
    data = fetch_mpt_data()
    fetch_secs = time.time() - t0

    enrich_secs = 0.0
    if data.get("ok") and data.get("total"):
        t1 = time.time()
        _enrich_holders(data)
        enrich_secs = time.time() - t1

    data["schema_version"] = SCHEMA_VERSION
    data["written_at"] = int(time.time())
    data["fetch_seconds"] = round(fetch_secs, 1)
    data["enrich_seconds"] = round(enrich_secs, 1)

    _atomic_write(path, data)
    # Mirror into Postgres so Render (no local file, no worker) can serve
    # /mpts without falling into the multi-minute ledger walk fallback.
    db.write_mpt_snapshot(data)
    return data


if __name__ == "__main__":
    d = run_once()
    print(f"wrote {SNAPSHOT_PATH}")
    print(f"  schema_version={d.get('schema_version')}")
    print(f"  node={d.get('node')}")
    print(f"  ok={d.get('ok')}  total={d.get('total')}  issuers={d.get('unique_issuers')}")
    print(f"  by_class={d.get('by_class')}")
    print(f"  holders_walked={d.get('holders_walked')}  skipped_test={d.get('holders_skipped_test')}")
    print(f"  fetch={d.get('fetch_seconds')}s  enrich={d.get('enrich_seconds')}s")
    sys.exit(0 if d.get("ok") else 1)
