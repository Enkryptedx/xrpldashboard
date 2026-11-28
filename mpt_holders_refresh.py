"""
Hourly MPT holders + supply refresh.

The daily mpt_snapshot.py job walks ledger_data (1-5h) to enumerate every
MPTokenIssuance on mainnet. This job — scheduled hourly — does NOT
re-walk ledger_data. Instead it reads mpt_issuance_list.json (written at
the end of each daily run) and, per issuance:

  1. Fetches current OutstandingAmount via `ledger_entry` (cheap, ~0.3s).
     If this fails for a single issuance, the row is SKIPPED for this
     cycle — never reuse yesterday's stale outstanding_amount, or every
     hourly mpt_supply_history row silently lies. Next hour re-tries.
  2. Walks current holders via `mpt_holders` (the same _walk_holders
     used by the daily job).
  3. Writes the fresh row to mpt_supply_history (PG) so /mpt/<id>
     sparklines update hourly.
  4. Updates mpt_snapshot.json in-place: replaces the holders block +
     outstanding_amount for each refreshed issuance. Atomic write of the
     full JSON.

Skipped (no row written, gap in sparkline — honest):
  - is_test issuances (per the existing skipped_test policy)
  - ledger_entry failure (RPC blip; retried next hour)
  - holders walk reason ∉ {complete, no_holders}

Cadence: hourly via launchd (StartInterval=3600). Run manually:
  venv/bin/python mpt_holders_refresh.py
"""

import json
import os
import sys
import time

import db
from mpt_data import (
    SNAPSHOT_PATH,
    fetch_one_issuance,
)
from mpt_snapshot import (
    HOLDER_ISSUANCE_PAUSE,
    ISSUANCE_LIST_PATH,
    _atomic_write,
    _holders_pending,
    _is_test_issuance,
    _walk_holders,
)

# Mirrors launchd/com.charliebruce.xrpldashboard.mpt_holders_refresh.plist
# StartInterval. Read by /walker_health for per-row staleness thresholds.
#
# 3600 -> 21600 (6h) on 2026-11-27. The holders walk now reads public Clio
# endpoints rather than our own node, because `mpt_holders` is Clio-only.
# Hourly polling of infrastructure we do not own, for data that moves slowly,
# is impolite; four cycles a day is plenty.
WALKER_CADENCE_SECONDS = 21600

# When this share of WALKED issuances comes back `rpc_unsupported`, the run is
# a CONFIGURATION failure rather than a data gap: every endpoint in the list
# rejected the method. The job then exits non-zero and says so, instead of
# reporting green while producing nothing -- which is exactly how 527 of 533
# issuances sat unwalked for weeks.
RPC_UNSUPPORTED_ALERT_SHARE = float(
    os.environ.get("MPT_HOLDERS_UNSUPPORTED_ALERT_SHARE", "0.5")
)


def _load_issuance_list(path):
    """Read the list produced by mpt_snapshot._write_issuance_list. Returns
    a list of dicts or None when missing/malformed. The hourly job MUST
    no-op cleanly when the daily job hasn't run yet (first install)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("issuances")
    if not isinstance(rows, list):
        return None
    return rows


def _load_snapshot(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _refresh_one(entry):
    """Resolve one issuance to a fresh row dict suitable for
    db.write_mpt_supply_history + snapshot replacement.

    Returns (row, status). `status` is "ok", "ledger_entry_failed",
    "skipped_test", or THE STORED REASON WORD from the holders block, one of
    rpc_unsupported / rpc_error / incomplete / pending. Only "ok" rows make
    it into history.

    This used to collapse every non-clean walk into "holders_incomplete", a
    PAGINATION word. The data said `pending` (RPC error) while the counter
    said incomplete (page cap), so the job's own reporting disagreed with the
    rows it had just written and hid a total RPC failure behind a plausible
    label. Reusing the stored word means a counter can no longer drift from
    the sentinel it counts."""
    if entry.get("is_test"):
        return None, "skipped_test"

    iid = entry.get("issuance_id")
    fresh = fetch_one_issuance(iid)
    if fresh is None:
        return None, "ledger_entry_failed"

    fresh["holders"] = _walk_holders(iid, fresh.get("issuer"))
    reason = (fresh.get("holders") or {}).get("reason")
    if reason not in ("complete", "no_holders"):
        # Still return the row so the snapshot reflects the state, but signal
        # the caller not to write a supply-history row. The status IS the
        # stored reason, so no translation can go stale.
        return fresh, reason or "pending"
    return fresh, "ok"


def _merge_into_snapshot(snapshot, refreshed_by_id):
    """In-place: replace each issuance row in `snapshot['issuances']` whose
    id appears in `refreshed_by_id` with the fresh shaped row. Issuances
    not in the map are left untouched (preserves yesterday's daily-walk
    holders block until the next hourly cycle picks them up)."""
    rows = snapshot.get("issuances") or []
    for i, r in enumerate(rows):
        iid = r.get("issuance_id")
        fresh = refreshed_by_id.get(iid)
        if fresh is not None:
            rows[i] = fresh
    snapshot["issuances"] = rows


def unsupported_share(counts):
    """Share of WALKED issuances that came back `rpc_unsupported`.

    Denominator is walks only: skipped_test and ledger_entry_failed never
    reached `mpt_holders`, so counting them would dilute the signal and let a
    fully broken endpoint list hide behind a pile of skips.
    """
    walked = sum(counts.get(k, 0) for k in
                 ("ok", "incomplete", "rpc_unsupported", "rpc_error",
                  "pending"))
    if walked <= 0:
        return 0.0
    return counts.get("rpc_unsupported", 0) / walked


def run_once(list_path=None, snapshot_path=None, dry_run=False):
    """One refresh cycle. `dry_run` performs every READ and the full holders
    walk but writes nothing -- no Postgres rows, no snapshot rewrite -- so a
    cycle can be measured before it is scheduled."""
    list_path = list_path or ISSUANCE_LIST_PATH
    snapshot_path = snapshot_path or SNAPSHOT_PATH

    t_start = time.time()
    entries = _load_issuance_list(list_path)
    if entries is None:
        return {
            "ok": False,
            "reason": "missing_issuance_list",
            "list_path": list_path,
        }

    snapshot = _load_snapshot(snapshot_path)
    if snapshot is None:
        return {
            "ok": False,
            "reason": "missing_snapshot",
            "snapshot_path": snapshot_path,
        }

    refreshed = {}
    # Keys are the STORED reason words plus the two non-walk outcomes, so a
    # counter name always matches the sentinel on the rows it counted.
    counts = {"ok": 0, "ledger_entry_failed": 0, "skipped_test": 0,
              "incomplete": 0, "rpc_unsupported": 0, "rpc_error": 0,
              "pending": 0}

    for entry in entries:
        row, status = _refresh_one(entry)
        counts[status] = counts.get(status, 0) + 1
        if row is not None:
            refreshed[row.get("issuance_id") or entry.get("issuance_id")] = row
        # Courtesy gap between issuances: this is someone else's Clio.
        if not entry.get("is_test"):
            time.sleep(HOLDER_ISSUANCE_PAUSE)

    written_at = int(time.time())
    eligible = [r for r in refreshed.values()
                if (r.get("holders") or {}).get("reason")
                in ("complete", "no_holders")]

    share = unsupported_share(counts)
    # A dominant rpc_unsupported bucket is a CONFIGURATION failure: every
    # endpoint rejected the method, so the run produced nothing and must not
    # report green. Returning ok=False here rather than in __main__ keeps it
    # testable without touching the database.
    if share >= RPC_UNSUPPORTED_ALERT_SHARE:
        return {
            "ok": False,
            "reason": "rpc_unsupported_dominant",
            "alert": (
                f"mpt_holders rejected as unknownCmd by every configured "
                f"endpoint for {counts.get('rpc_unsupported', 0)} of "
                f"{len(entries)} issuances "
                f"({share:.0%} of walks). `mpt_holders` is Clio-only; check "
                f"MPT_HOLDERS_NODES points at Clio, not rippled."
            ),
            "counts": counts,
            "unsupported_share": round(share, 4),
            "elapsed_seconds": round(time.time() - t_start, 1),
            "total_in_list": len(entries),
            "history_rows": 0,
            "dry_run": dry_run,
        }

    if dry_run:
        history_rows = 0
    else:
        history_rows = db.write_mpt_supply_history(eligible, ts=written_at)
        _merge_into_snapshot(snapshot, refreshed)
        snapshot["written_at"] = written_at
        snapshot["last_holders_refresh_at"] = written_at
        snapshot["last_holders_refresh_seconds"] = round(time.time() - t_start, 1)
        snapshot["last_holders_refresh_counts"] = counts
        _atomic_write(snapshot_path, snapshot)
        # Mirror the refreshed snapshot to PG so Render sees the new holders +
        # outstanding amounts. (PG mpt_snapshot row is a single UPSERT; cheap.)
        db.write_mpt_snapshot(snapshot)

    return {
        "ok": True,
        "elapsed_seconds": round(time.time() - t_start, 1),
        "counts": counts,
        "history_rows": history_rows,
        "total_in_list": len(entries),
        "unsupported_share": round(share, 4),
        "eligible_rows": len(eligible),
        "refreshed": refreshed if dry_run else None,
        "dry_run": dry_run,
    }


if __name__ == "__main__":
    db.write_walker_health_start("mpt_holders_refresh", cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = None
    try:
        r = run_once()
        ok = bool(r.get("ok"))
        if ok:
            counts = r.get("counts") or {}
            message = (
                f"elapsed={r.get('elapsed_seconds')}s "
                f"total_in_list={r.get('total_in_list')} "
                f"history_rows={r.get('history_rows')} "
                f"ok_count={counts.get('ok')} "
                f"ledger_failed={counts.get('ledger_entry_failed')} "
                f"skipped_test={counts.get('skipped_test')} "
                f"incomplete={counts.get('incomplete')} "
                f"rpc_unsupported={counts.get('rpc_unsupported')} "
                f"rpc_error={counts.get('rpc_error')} "
                f"pending={counts.get('pending')}"
            )
        else:
            message = f"reason={r.get('reason')}"
            if r.get("alert"):
                message += f" alert={r['alert']}"
                print(f"ALERT: {r['alert']}", file=sys.stderr)
        print(f"mpt_holders_refresh: ok={ok}")
        if not ok:
            print(f"  reason={r.get('reason')}")
        else:
            print(f"  elapsed={r.get('elapsed_seconds')}s  total_in_list={r.get('total_in_list')}")
            print(f"  counts={r.get('counts')}")
            print(f"  history_rows={r.get('history_rows')}")
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end("mpt_holders_refresh", ok=ok, message=message)
    sys.exit(0 if ok else 1)
