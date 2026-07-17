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

import db
import rlusd_live

# Mirrors launchd/com.charliebruce.xrpldashboard.rlusd_refresher.plist
# StartInterval. Read by /walker_health for per-row staleness thresholds.
WALKER_CADENCE_SECONDS = 300


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start("rlusd_refresher", cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = None
    try:
        ok = bool(rlusd_live._refresh_cache_once())
        if ok:
            # Surface partial failures (e.g. aggregate fetch failed while
            # supply succeeded) so walker_health isn't clean-green while
            # the page shows '—'. The payload's error field records which
            # sub-fetch failed; read it back from the cache we just wrote.
            try:
                payload, _ = db.read_rlusd_state_cache()
                err = payload.get("error") if payload else None
            except Exception:
                err = None
            message = f"refreshed (partial: {err})" if err else "refreshed"
        else:
            # Soft fail: _refresh_cache_once returned False (upstream RPC
            # error). Previous cache row stays in place; next 5-min cycle
            # retries. Log + surface via walker_health.last_run_message so
            # consecutive_failures climbs and the staleness banner trips
            # if upstream stays down across multiple cycles.
            message = "refresh_failed_upstream_rpc"
            logging.getLogger("rlusd_refresher_walker").error(
                "refresh failed (upstream RPC error); previous cache row left in place"
            )
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end("rlusd_refresher", ok=ok, message=message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
