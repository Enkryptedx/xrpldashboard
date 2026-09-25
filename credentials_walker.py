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
