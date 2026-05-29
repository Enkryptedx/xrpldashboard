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

import os
import sys
import time

import db

TOP_N = int(os.environ.get("AMM_TVL_TOP_N", "50"))


def record_once():
    """Snapshot the top-N pools by TVL into amm_tvl_history. Returns
    (snapshotted, written) — `snapshotted` is what we read from PG,
    `written` is what survived the validity filter inside db.write_."""
    if not db.pg_available():
        print("[amm_tvl_recorder] DATABASE_URL unset; nothing to do.",
              flush=True)
        return (0, 0)

    pools = db.read_amm_ranked_pools()
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
    db.write_walker_health_start("amm_tvl_recorder")
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
