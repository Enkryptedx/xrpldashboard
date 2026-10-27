"""
AMM TVL history recorder.

Reads the current point-in-time snapshot from `amm_ranked_pools` (written
by rank_amms.py) and appends a row per pool to `amm_tvl_history`. Run on
a 15-min cadence by launchd so we accumulate forward TVL history without
hammering the public node — rank_amms.py does the AMMInfo fan-out; we
just slice off the top-N and persist.

Why a separate worker and not folded into rank_amms.py:
- rank_amms is point-in-time and wipes its table on each rerank, so it
  can't be its own historian.
- This worker is cheap (one PG read, one batched insert) so it's safe to
  run on a tight schedule even if a rerank is mid-flight.

Top-N rather than all 9,500+ pools because:
- The retail-facing sparkline only ever shows the leaderboard.
- 30 days × 96 buckets/day × N pools = N×2880 rows. N=50 keeps the table
  under 150k rows even after a month — trivially small for PG, but big
  enough to mean something.
"""

import json
import os
import sys
import time

import db

TOP_N = int(os.environ.get("AMM_TVL_TOP_N", "50"))

# Mirrors launchd/com.charliebruce.xrpldashboard.amm_tvl_recorder.plist
# StartInterval. Read by /walker_health to compute per-row staleness
# thresholds (green/yellow/red) without hardcoding multiples in the page.
WALKER_CADENCE_SECONDS = 900

HERE = os.path.dirname(os.path.abspath(__file__))
# 2026-10-27 egress audit: a fresh launchd invocation every 900s re-pulled
# the full 6-column amm_ranked_pools projection (~30,290 rows) every time,
# even though rank_amms.py only rewrites that table roughly hourly -- so
# ~3 of every 4 reads were an identical re-fetch. Unlike app.py's in-
# process caches (amm-reads-snapshot-cache-2026-10-27,
# amm-reads-snapshot-cache-round2-2026-10-27), this walker is a FRESH
# PROCESS each run, so the watermark has to survive on disk, not in a
# module-level dict. One JSON file, same spirit as every other walker's
# cursor/state file in this repo.
STATE_PATH = os.path.join(HERE, "amm_tvl_recorder_state.json")


def _load_last_snapshot_ts():
    try:
        with open(STATE_PATH) as f:
            return json.load(f).get("last_snapshot_ts")
    except (OSError, ValueError):
        return None


def _save_last_snapshot_ts(snap):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"last_snapshot_ts": snap}, f)
    os.replace(tmp, STATE_PATH)


def record_once():
    """Snapshot the top-N pools by TVL into amm_tvl_history. Returns
    (snapshotted, written) — `snapshotted` is what we read from PG,
    `written` is what survived the validity filter inside db.write_.

    Probes read_amm_snapshot_ts() (one row, cheap) first; when it matches
    the watermark from the last run's successful write, skips the full
    6-column pull entirely (and the write — there's nothing new to
    record). A tick that finds nothing new is reported the same way as
    an empty table: snapshotted=0, written=0, which main()'s existing
    "clean no-op, not a failure" handling already covers correctly."""
    if not db.pg_available():
        print("[amm_tvl_recorder] DATABASE_URL unset; nothing to do.",
              flush=True)
        return (0, 0)

    snap = db.read_amm_snapshot_ts()
    last_snap = _load_last_snapshot_ts()
    if snap is not None and snap == last_snap:
        print(f"[amm_tvl_recorder] snapshot_ts={snap} unchanged since last "
              f"run — skipping the full read.", flush=True)
        return (0, 0)

    pools = db.read_amm_ranked_pools(columns=("amm_account", "pair", "amount_a", "amount_b", "tvl_usd"))
    if not pools:
        print("[amm_tvl_recorder] amm_ranked_pools empty — rank_amms.py "
              "hasn't run yet or PG hiccup. Skipping.", flush=True)
        return (0, 0)

    # Sort defensively: amm_ranked_pools has an index on tvl_usd DESC but
    # nothing forces the read to come back in that order.
    pools.sort(key=lambda r: (r.get("tvl_usd") or 0.0), reverse=True)
    top = pools[:TOP_N]
    ts = int(time.time())
    written = db.write_amm_tvl_snapshot(top, ts=ts)
    if snap is not None:
        _save_last_snapshot_ts(snap)
    return (len(top), written)


def main():
    t0 = time.time()
    snapshotted, written = record_once()
    elapsed = round(time.time() - t0, 2)
    print(
        f"[amm_tvl_recorder] top_n={TOP_N} read={snapshotted} "
        f"written={written} elapsed={elapsed}s",
        flush=True,
    )
    # Empty read (snapshotted==0) is a clean no-op — rank_amms.py hasn't run
    # yet or DATABASE_URL is unset; not a failure. snapshotted>0 with
    # written==0 means the validity filter rejected every row, which IS a
    # failure worth surfacing.
    ok = bool(written or snapshotted == 0)
    message = f"snapshotted={snapshotted} written={written} elapsed={elapsed}s"
    return ok, message


if __name__ == "__main__":
    db.write_walker_health_start("amm_tvl_recorder", cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = None
    try:
        ok, message = main()
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end("amm_tvl_recorder", ok=ok, message=message)
    sys.exit(0 if ok else 1)
