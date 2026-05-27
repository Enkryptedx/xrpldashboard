"""Standalone one-shot walker for the RLUSD cross-chain state cache.

Invoked by com.charliebruce.xrpldashboard.rlusd_refresher.plist on a
5-minute cadence. Each run fetches Ethereum + XRPL RLUSD state via the
shared _refresh_cache_once() helper in rlusd_live, which writes the
combined payload to rlusd_state_cache in Postgres. The /rlusd route
and /api/rlusd/state endpoint read from Postgres only.

Replaces the per-worker lazy refresher pattern that previously lived in
rlusd_live._ensure_refresher / _refresh_loop. That pattern died silently
on every gunicorn worker restart (deploys, scaling, OOM kills), leaving
rlusd_state_cache stale for up to 29 hours across a single deploy
window (caught 2026-05-27). Mirrors credentials_walker / mpt_snapshot.
"""

import logging
import sys

import rlusd_live


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ok = rlusd_live._refresh_cache_once()
    if not ok:
        logging.getLogger("rlusd_refresher_walker").error(
            "refresh failed (upstream RPC error); previous cache row left in place"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
