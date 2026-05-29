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
    ISSUANCE_LIST_PATH,
    _atomic_write,
    _holders_pending,
    _is_test_issuance,
    _walk_holders,
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

    Returns (row, status) where status ∈ {ok, ledger_entry_failed,
    skipped_test, holders_incomplete}. Only "ok" rows make it into
    history."""
    if entry.get("is_test"):
        return None, "skipped_test"

    iid = entry.get("issuance_id")
    fresh = fetch_one_issuance(iid)
    if fresh is None:
        return None, "ledger_entry_failed"

    fresh["holders"] = _walk_holders(iid, fresh.get("issuer"))
    reason = (fresh.get("holders") or {}).get("reason")
    if reason not in ("complete", "no_holders"):
        # Still return the row so the snapshot reflects the pending state,
        # but signal the caller not to write a supply-history row.
        return fresh, "holders_incomplete"
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


def run_once(list_path=None, snapshot_path=None):
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
    counts = {"ok": 0, "ledger_entry_failed": 0,
              "skipped_test": 0, "holders_incomplete": 0}

    for entry in entries:
        row, status = _refresh_one(entry)
        counts[status] = counts.get(status, 0) + 1
        if row is not None:
            refreshed[row.get("issuance_id") or entry.get("issuance_id")] = row

    written_at = int(time.time())

    history_rows = db.write_mpt_supply_history(
        [r for r in refreshed.values()
         if (r.get("holders") or {}).get("reason") in ("complete", "no_holders")],
        ts=written_at,
    )

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
    }


if __name__ == "__main__":
    db.write_walker_health_start("mpt_holders_refresh")
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
                f"holders_incomplete={counts.get('holders_incomplete')}"
            )
        else:
            message = f"reason={r.get('reason')}"
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
