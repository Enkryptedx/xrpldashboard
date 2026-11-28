"""
Background writer for the XLS-66 lending stack.

Pulls the full broker/vault/loan picture via lending_data.fetch_lending_data(),
enriches the top brokers by TVL with depositor counts (mpt_holders RPC on
each vault's ShareMPTID), and writes lending_snapshot.json atomically.

The /lending route prefers this snapshot over the in-process fetcher,
so the page returns in <100ms even though a fresh broker scan can take
minutes when there are many ledger objects.

Designed to run from launchd on a 15-30 min cadence. Dormant pre-
activation: if amendments aren't live, writes an "activated: false"
snapshot and exits — no wasted RPC calls.

Run manually: venv/bin/python lending_snapshot.py
With devnet override:
  XRPL_NODE=https://s.devnet.rippletest.net:51234 \\
  XRPL_LENDING_NODE=https://s.devnet.rippletest.net:51234 \\
  venv/bin/python lending_snapshot.py

── Why the depositor walk does NOT use XRPL_NODE (2026-11-27) ──────────

`mpt_holders` is Clio-only: rippled 3.4.1 answers `unknownCmd`. This module
used to send it to XRPL_NODE (via lending_data), which the env drift guard
pins to our own rippled — so every depositor count was structurally
unobtainable, exactly like the MPT holders walk that sat dead for 88 days.

It was never caught because it could not fire: the LendingProtocol
amendment is not enabled, so `activated` is False, `brokers` is empty, and
the enrichment loop has no rows to walk. A latent fault that arms itself on
the day lending activates, in a walker that runs every 15 minutes.

Found by auditing every RPC method our jobs call against our own node
(2026-11-27). `mpt_holders` was the only method it refuses, and this was
its second caller; PR #196 had routed only mpt_snapshot.py.

There was a second, independent bug in the same function: it read
`result["holders"]`, but Clio returns the page under `mptokens` (measured
against s2-clio 2026-11-27, top-level keys: ledger_index, limit, marker,
mpt_issuance_id, mptokens, status, validated). So even pointed at a working
Clio endpoint it would have counted zero and reported it as a real zero.

This now shares mpt_snapshot's contract exactly: the endpoint list from
tools/anchor_nodes.mpt_holders_nodes(), the same unsupported/transient
split, provenance travelling with each row, and a dominant
`rpc_unsupported` share failing the run loudly instead of reporting green.
"""

import json
import os
import sys
import time

import httpx

from lending_data import (
    SNAPSHOT_PATH,
    fetch_lending_data,
)

DEPOSITOR_ENRICH_TOP_N = int(os.environ.get("LENDING_ENRICH_TOP_N", "30"))
WALKER_CADENCE_SECONDS = 900  # StartInterval in com.xrpldashboard.lending_snapshot.plist

HERE = os.path.dirname(os.path.abspath(__file__))

# `mpt_holders` is Clio-only (rippled 3.4.1 -> unknownCmd), so the depositor
# walk reads public Clio endpoints. One shared list, one env override, and the
# measurement that established the finding: see tools/anchor_nodes.py. The
# walk must NOT be pointed at our own node — that is the bug being fixed.
sys.path.insert(0, os.path.join(HERE, "tools"))
from anchor_nodes import mpt_holders_nodes  # noqa: E402

UNSUPPORTED_ERRORS = {"unknownCmd", "unknownCommand", "notImpl"}

# Endpoints that answered `unknownCmd` this run. A rippled server cannot be
# talked into implementing the method, so it is asked once and then skipped
# for the rest of the cycle.
_HOLDERS_DEAD: set[str] = set()

HOLDER_PAGE_CAP = int(os.environ.get("LENDING_HOLDER_PAGE_CAP", "50"))
HOLDER_PAGE_LIMIT = int(os.environ.get("LENDING_HOLDER_PAGE_LIMIT", "400"))

# When this share of ATTEMPTED broker walks comes back `rpc_unsupported`, the
# run is a CONFIGURATION failure rather than a data gap: every endpoint in the
# list rejected the method. Same default and same reasoning as
# mpt_holders_refresh.RPC_UNSUPPORTED_ALERT_SHARE.
RPC_UNSUPPORTED_ALERT_SHARE = float(
    os.environ.get("LENDING_HOLDERS_UNSUPPORTED_ALERT_SHARE", "0.5")
)


def _holders_rpc(params):
    """Call `mpt_holders` on each Clio endpoint until one answers.

    Same contract as mpt_snapshot._holders_rpc. Returns
    (result, endpoint, failure) where exactly one of result/failure is set:

      "rpc_unsupported" - every endpoint said unknownCmd. PERMANENT: the
          endpoint list is pointing at rippled, not Clio. A configuration
          fault, so the run alerts rather than recording a data gap.
      "rpc_error"       - transport failure, timeout or any other error.
          Transient; the next cycle retries.
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


def _count_mpt_holders(mpt_issuance_id):
    """Count depositors for one vault's ShareMPTID.

    Returns a provenance dict, never a bare number, so a count can never be
    rendered without the facts about where it came from:

      count     - int, or None whenever the figure is not trustworthy
      source    - the Clio endpoint that answered, or None if none did
      sovereign - ALWAYS False: our own node cannot serve this method at
                  any endpoint, so a depositor count is never sovereign
      reason    - "complete" | "no_holders" | "incomplete" |
                  "rpc_unsupported" | "rpc_error" | "pending"

    `count` is None for every non-success reason. A failed walk previously
    returned None with no reason attached, which is how an RPC failure and
    a genuine zero became indistinguishable.
    """
    if not mpt_issuance_id:
        return {"count": None, "source": None, "sovereign": False,
                "reason": "pending"}

    total = 0
    marker = None
    source = None
    pages = 0

    while pages < HOLDER_PAGE_CAP:
        params = {"mpt_issuance_id": mpt_issuance_id, "limit": HOLDER_PAGE_LIMIT}
        if marker is not None:
            params["marker"] = marker
        result, endpoint, failure = _holders_rpc(params)
        if failure is not None:
            return {"count": None, "source": None, "sovereign": False,
                    "reason": failure}
        source = endpoint
        # Clio returns the page under `mptokens`. Reading "holders" here
        # counted zero against a perfectly healthy endpoint.
        total += len(result.get("mptokens") or [])
        pages += 1
        marker = result.get("marker")
        if not marker:
            break

    if marker is not None:
        # Page cap fired mid-marker-chain: a partial count is not a count.
        return {"count": None, "source": source, "sovereign": False,
                "reason": "incomplete"}

    return {"count": total, "source": source, "sovereign": False,
            "reason": "complete" if total > 0 else "no_holders"}


def unsupported_share(counts):
    """Share of ATTEMPTED broker walks that came back `rpc_unsupported`.

    Denominator is attempts only. Brokers past the top-N cut and brokers
    with no ShareMPTID never reached `mpt_holders`, so counting them would
    dilute the signal and let a fully broken endpoint list hide behind a
    pile of skips.
    """
    attempted = sum(counts.get(k, 0) for k in
                    ("complete", "no_holders", "incomplete",
                     "rpc_unsupported", "rpc_error"))
    if attempted <= 0:
        return 0.0
    return counts.get("rpc_unsupported", 0) / attempted


def _enrich_depositors(data, top_n):
    """Mutates data['brokers']: depositor count + provenance for the top N.

    `depositors` stays an int-or-None so templates/lending.html renders
    unchanged; the provenance keys travel alongside it.
    Returns the per-reason counts so the caller can judge the whole run.
    """
    brokers = data.get("brokers") or []
    counts = {"complete": 0, "no_holders": 0, "incomplete": 0,
              "rpc_unsupported": 0, "rpc_error": 0, "pending": 0,
              "not_attempted": 0}

    for b in brokers[:top_n]:
        h = _count_mpt_holders(b.get("share_mpt_id"))
        b["depositors"] = h["count"]
        b["depositors_source"] = h["source"]
        b["depositors_sovereign"] = h["sovereign"]
        b["depositors_reason"] = h["reason"]
        counts[h["reason"]] = counts.get(h["reason"], 0) + 1

    for b in brokers[top_n:]:
        b.setdefault("depositors", None)
        b.setdefault("depositors_source", None)
        b.setdefault("depositors_sovereign", False)
        b.setdefault("depositors_reason", "not_attempted")
        counts["not_attempted"] += 1

    return counts


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def run_once(path=None, enrich_top_n=None):
    path = path or SNAPSHOT_PATH
    top_n = enrich_top_n if enrich_top_n is not None else DEPOSITOR_ENRICH_TOP_N

    t0 = time.time()
    data = fetch_lending_data()
    fetch_secs = time.time() - t0

    enrich_secs = 0.0
    if data.get("ok") and data.get("activated"):
        t1 = time.time()
        counts = _enrich_depositors(data, top_n)
        enrich_secs = time.time() - t1

        share = unsupported_share(counts)
        data["depositor_enrich"] = {
            "counts": counts,
            "unsupported_share": round(share, 4),
            # Standing fact, not a per-run outcome: our own node cannot serve
            # `mpt_holders` at any endpoint, so no depositor count is ever
            # sovereign. Recorded on the snapshot so the page can disclose it
            # without re-deriving it.
            "sovereign": False,
            "endpoints": list(mpt_holders_nodes()),
        }

        # A dominant rpc_unsupported bucket is a CONFIGURATION failure: every
        # endpoint rejected the method, so the enrichment produced nothing and
        # must not report green. This is the signal whose absence let the MPT
        # holders walk sit dead for 88 days while its job exited 0.
        if share >= RPC_UNSUPPORTED_ALERT_SHARE:
            data["ok"] = False
            data["depositor_enrich"]["alert"] = (
                f"mpt_holders rejected as unknownCmd by every configured "
                f"endpoint for {counts.get('rpc_unsupported', 0)} of "
                f"{sum(counts.get(k, 0) for k in ('complete', 'no_holders', 'incomplete', 'rpc_unsupported', 'rpc_error'))} "
                f"attempted brokers ({share:.0%} of attempts). "
                f"`mpt_holders` is Clio-only; check MPT_HOLDERS_NODES points "
                f"at Clio, not rippled."
            )

    data["written_at"] = int(time.time())
    data["fetch_seconds"] = round(fetch_secs, 1)
    data["enrich_seconds"] = round(enrich_secs, 1)

    _atomic_write(path, data)
    return data


if __name__ == "__main__":
    import db as _db
    _db.write_walker_health_start("lending_snapshot", cadence_seconds=WALKER_CADENCE_SECONDS)
    _ok = False
    _msg = "init"
    try:
        d = run_once()
        activated = d.get("activated", False)
        _ok = bool(d.get("ok")) if activated else True
        if not activated:
            _msg = f"pre-activation (activated=False) fetch={d.get('fetch_seconds')}s"
        else:
            _enr = d.get("depositor_enrich") or {}
            _alert = _enr.get("alert")
            _msg = (f"activated=True brokers={d.get('broker_count')} "
                    f"vaults={d.get('vault_count')} loans={d.get('loan_count')} "
                    f"fetch={d.get('fetch_seconds')}s enrich={d.get('enrich_seconds')}s")
            if _enr.get("counts"):
                _msg += f" depositors={_enr['counts']}"
            # The configuration failure goes in the walker_health message, not
            # only in the snapshot, so it is visible without reading the JSON.
            if _alert:
                _msg = f"{_alert} | {_msg}"
        print(f"wrote {SNAPSHOT_PATH}")
        print(f"  node={d.get('node')}")
        print(f"  activated={d.get('activated')}  ok={d.get('ok')}")
        print(f"  brokers={d.get('broker_count')}  vaults={d.get('vault_count')}  loans={d.get('loan_count')}")
        print(f"  fetch={d.get('fetch_seconds')}s  enrich={d.get('enrich_seconds')}s")
    except Exception as e:
        _msg = f"{type(e).__name__}: {e}"
        raise
    finally:
        _db.write_walker_health_end("lending_snapshot", ok=_ok, message=_msg)
    sys.exit(0 if _ok else 1)
