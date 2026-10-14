"""Standalone one-shot walker for /credentials.

Invoked by com.charliebruce.xrpldashboard.credentials_walker.plist on a
30-minute cadence. Performs a single amendment-status + account_objects
seed walk + recent-activity scan, persists the result to Postgres, and
exits. The /credentials route reads from Postgres only — no in-process
daemon required.

Replaces the daemon-in-gunicorn-worker pattern that lived in
credentials_state._refresh_loop. Mirrors the mpt_snapshot launchd pattern.
"""

import logging
import sys

import credentials_state
import db

# Mirrors launchd/com.charliebruce.xrpldashboard.credentials_walker.plist
# StartInterval. Read by /walker_health for per-row staleness thresholds.
WALKER_CADENCE_SECONDS = 1800


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # 2026-10-14 (Charlie): httpx logs EVERY request at INFO. This walker's
    # err.log reached 754 MB with 203,608 httpx lines against 320 real ones in
    # the last 20 MB sampled -- a 636:1 noise ratio, nothing rotating it. The
    # lines carry no scanned/total_tx count, so the walker's own "recent --
    # scanned=N total_tx=N" line is the only signal in there. Set AFTER
    # basicConfig deliberately: basicConfig only attaches root handlers, it
    # does not reset per-logger levels, so order is safe either way, but
    # keeping it adjacent means a future reader sees both together.
    # WARNING keeps real transport failures (timeouts, 5xx, resets) visible.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    db.write_walker_health_start("credentials_walker", cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = None
    try:
        # GAP-3: run_once returns the page-level sourcing flag (own node
        # vs labeled public Clio fallback); stamp it into walker_health
        # and stdout so a fallback run is visible without opening the DB.
        sourcing = credentials_state.run_once()
        ok = True
        message = f"walked sourcing={sourcing}"
        print(f"credentials_walker: {message}")
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end("credentials_walker", ok=ok, message=message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
