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


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start("credentials_walker")
    ok = False
    message = None
    try:
        credentials_state.run_once()
        ok = True
        message = "walked"
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end("credentials_walker", ok=ok, message=message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
