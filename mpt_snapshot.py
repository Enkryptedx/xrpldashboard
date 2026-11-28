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

Cadence: daily via launchd (full ledger walk takes 1-5h). At the end of
each run we persist mpt_issuance_list.json — the bare list of
(issuance_id, issuer, name, ticker, classification, is_test) — for the
hourly mpt_holders_refresh.py job to consume. That job re-walks holders
(and fetches current OutstandingAmount via ledger_entry) per issuance,
so /mpt/<id> supply sparklines update hourly without re-walking
ledger_data.

Run manually: venv/bin/python mpt_snapshot.py
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

# Mirrors launchd/com.charliebruce.xrpldashboard.mpt_snapshot.plist
# StartInterval. Read by /walker_health for per-row staleness thresholds.
WALKER_CADENCE_SECONDS = 86400

HERE = os.path.dirname(os.path.abspath(__file__))
ISSUANCE_LIST_PATH = os.environ.get(
    "MPT_ISSUANCE_LIST_PATH",
    os.path.join(HERE, "mpt_issuance_list.json"),
)

HOLDER_PAGE_CAP = int(os.environ.get("MPT_HOLDER_PAGE_CAP", "50"))
HOLDER_PAGE_LIMIT = int(os.environ.get("MPT_HOLDER_PAGE_LIMIT", "400"))
HOLDER_TOP_N = int(os.environ.get("MPT_HOLDER_TOP_N", "20"))
# Between PAGES of one issuance. Raised 0.05 -> 0.25 on 2026-11-27: the walk
# now runs against a public Clio endpoint we do not own, so it paces politely
# rather than at local-node speed.
HOLDER_RPC_PAUSE = float(os.environ.get("MPT_HOLDER_RPC_PAUSE", "0.25"))
# Between ISSUANCES. Measured 2026-11-27: 0.34s per issuance against Clio with
# 19 of 20 sampled needing a single page, so 533 issuances cost ~3 min of
# request time; a 0.2s courtesy gap puts a full cycle near 5 min, which is far
# inside the 6-hour schedule and keeps us a well-behaved client of someone
# else's infrastructure.
HOLDER_ISSUANCE_PAUSE = float(os.environ.get("MPT_HOLDER_ISSUANCE_PAUSE", "0.2"))

# `mpt_holders` is Clio-only (rippled 3.4.1 -> unknownCmd), so the walk reads
# public Clio endpoints. One shared list, one env override: see
# tools/anchor_nodes.py.
sys.path.insert(0, os.path.join(HERE, "tools"))
from anchor_nodes import mpt_holders_nodes  # noqa: E402

# Endpoints that answered `unknownCmd` this run. A rippled server cannot be
# talked into implementing the method, so it is asked once and then skipped
# for the rest of the cycle instead of 533 more times.
_HOLDERS_DEAD: set[str] = set()

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


# `mpt_holders` is Clio-only: rippled 3.4.1 answers `unknownCmd`. Our own node
# therefore CANNOT serve this call, so the holders walk is a disclosed
# third-party read against Clio and never a sovereign one. See
# tools/anchor_nodes.mpt_holders_nodes() for the endpoint list, the env
# override and the measurement that established it.
UNSUPPORTED_ERRORS = {"unknownCmd", "unknownCommand", "notImpl"}


def _holders_rpc(params):
    """Call `mpt_holders` on each Clio endpoint until one answers.

    Returns (result, endpoint, failure) where exactly one of result/failure
    is set. `failure` is the stored reason word, so the caller never has to
    translate an error into a sentinel:

      "rpc_unsupported" - every endpoint said unknownCmd. PERMANENT: the
          endpoint list is pointing at rippled, not Clio. Not retried, and
          the hourly job alerts on it, because this is a misconfiguration
          and not a gap in the data.
      "rpc_error"       - transport failure, timeout or any other error.
          Transient; the next cycle retries.

    A node that answers unknownCmd is tried ONCE and then abandoned for the
    rest of the run: asking a rippled server 533 more times cannot change
    its answer.
    """
    endpoints = [u for u in mpt_holders_nodes() if u not in _HOLDERS_DEAD]
    if not endpoints:
        return None, None, "rpc_unsupported"
    transient = False
    for url in endpoints:
        try:
            resp = httpx.post(
                url, json={"method": "mpt_holders", "params": [params]},
                timeout=20.0,
            )
            result = (resp.json() or {}).get("result") or {}
        except Exception:
            transient = True
            continue
        err = result.get("error")
        if err in UNSUPPORTED_ERRORS:
            _HOLDERS_DEAD.add(url)
            continue
        if err:
            transient = True
            continue
        return result, url, None
    return None, None, ("rpc_error" if transient else "rpc_unsupported")


def _holders_pending(reason="pending", source=None):
    return {
        "with_balance": None,
        "authorized": None,
        "top": [],
        "walked_complete": False,
        "walked_at": None,
        "reason": reason,
        # Provenance travels with the row. `source` is None when no endpoint
        # was reached (nothing was read, so nothing can be attributed), and
        # `sovereign` is False unconditionally: our own node cannot answer
        # this call at any endpoint, so a holders figure is never sovereign.
        "source": source,
        "sovereign": False,
    }


def _walk_holders(issuance_id, issuer):
    """Walk mpt_holders pages for one issuance. Excludes issuer self-holdings
    from counts and top-N. Returns the v3 holders dict.

    `reason` enum (exhaustive, non-null):
      "pending"         — never walked (cold-start). NOT an error.
      "rpc_unsupported" — every endpoint rejected the method (unknownCmd).
                          Permanent until the endpoint list is fixed.
      "rpc_error"       — transport/timeout/other error. Transient.
      "incomplete"      — page cap fired before exhausting the marker chain
      "no_holders"      — walked clean, zero non-issuer holders
      "complete"        — walked clean, with at least one non-issuer holder

    Until 2026-11-27 an RPC failure of ANY kind was stored as "pending",
    which reads as "never walked". 527 of 533 issuances sat in that state
    because `mpt_holders` is not implemented by rippled 3.4.1, and the
    hourly job counted them under `holders_incomplete` — a pagination word
    for an RPC failure. Three different names for one cause is why it
    survived unnoticed; the enum now separates them.
    """
    if not issuance_id:
        return _holders_pending("pending")

    rows = []
    marker = None
    pages = 0
    source = None
    walked_at = int(time.time())

    while pages < HOLDER_PAGE_CAP:
        params = {"mpt_issuance_id": issuance_id, "limit": HOLDER_PAGE_LIMIT}
        if marker is not None:
            params["marker"] = marker
        result, endpoint, failure = _holders_rpc(params)
        if failure is not None:
            return _holders_pending(failure)
        source = endpoint
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
            "source": source,
            "sovereign": False,
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
        # Which Clio endpoint produced this figure, and the standing fact
        # that it is not our own node. Every surface rendering the number
        # discloses both.
        "source": source,
        "sovereign": False,
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


def _write_issuance_list(path, rows, written_at):
    """Persist the bare-minimum list of (issuance_id, issuer, name, ticker,
    classification, is_test) that mpt_holders_refresh.py needs. The hourly
    job reads this — never re-walks ledger_data — so the daily enumeration
    fully owns "what MPTs exist on mainnet" and the hourly job owns
    "what each one looks like right now."

    `ticker` is included because _is_test_issuance reads it. `is_test` is
    pre-computed so the hourly job stays in sync with the daily classifier
    without re-importing the heuristic."""
    payload = {
        "written_at": int(written_at),
        "source": "mpt_snapshot.run_once",
        "issuances": [
            {
                "issuance_id": r.get("issuance_id"),
                "issuer": r.get("issuer"),
                "name": r.get("name"),
                "ticker": r.get("ticker"),
                "classification": r.get("classification"),
                "is_test": _is_test_issuance(r),
            }
            for r in rows
            if r.get("issuance_id")
        ],
    }
    _atomic_write(path, payload)
    return len(payload["issuances"])


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
    # Append a per-MPT history row for each eligible issuance — eligible
    # meaning the holders walk completed cleanly. Drives the /mpt/<id>
    # supply sparkline. Writer filters incomplete/skipped/pending rows
    # internally so the time-series stays gap-free where it has data.
    history_rows = db.write_mpt_supply_history(
        data.get("issuances") or [],
        ts=data.get("written_at"),
    )
    data["history_rows_written"] = history_rows

    # Persist the issuance list for mpt_holders_refresh.py (hourly job).
    # Written AFTER the snapshot is on disk so a partial/interrupted daily
    # run can never leave a stale list pointing at issuances the snapshot
    # doesn't yet know about.
    list_rows = _write_issuance_list(
        ISSUANCE_LIST_PATH,
        data.get("issuances") or [],
        data.get("written_at"),
    )
    data["issuance_list_rows"] = list_rows
    return data


if __name__ == "__main__":
    # walker_health: write start row up front (defensive ok=false until the
    # end-write fires) so an uncaught crash leaves a visible failure row
    # instead of just a stale last_success_at. /mpts reads this to render
    # honest staleness instead of silently serving last-good.
    db.write_walker_health_start("mpt_snapshot", cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = None
    d = None
    try:
        d = run_once()
        ok = bool(d.get("ok"))
        parts = [
            f"total={d.get('total')}",
            f"walk_complete={d.get('walk_complete')}",
            f"pages={d.get('walk_pages')}",
        ]
        if d.get("walk_partial"):
            parts.append(f"partial_reason={d.get('walk_abort_reason')}")
        retry_secs = d.get("walk_cumulative_retry_seconds") or 0
        if retry_secs:
            parts.append(f"retry_secs={retry_secs}")
        message = "; ".join(parts)
        print(f"wrote {SNAPSHOT_PATH}")
        print(f"  schema_version={d.get('schema_version')}")
        print(f"  node={d.get('node')}")
        print(f"  ok={d.get('ok')}  total={d.get('total')}  issuers={d.get('unique_issuers')}")
        print(f"  by_class={d.get('by_class')}")
        print(f"  holders_walked={d.get('holders_walked')}  skipped_test={d.get('holders_skipped_test')}")
        print(f"  history_rows={d.get('history_rows_written')}")
        print(f"  issuance_list_rows={d.get('issuance_list_rows')}")
        print(f"  fetch={d.get('fetch_seconds')}s  enrich={d.get('enrich_seconds')}s")
    except Exception as exc:
        ok = False
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end("mpt_snapshot", ok=ok, message=message)
    sys.exit(0 if ok else 1)
