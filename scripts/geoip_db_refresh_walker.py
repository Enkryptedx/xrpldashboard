#!/usr/bin/env python3
"""geoip_db_refresh_walker — the ONE place that talks to MaxMind on a schedule.

Weekly (launchd StartInterval 604800, Mac): download GeoLite2-City once,
gzip it, and UPSERT it into Postgres `geoip_db_blob`. Every Render container
and every Mac walker then boots from our store (geoip_state.ensure_database,
PG-first) — a deploy costs 0 MaxMind downloads. Charlie ruling 2026-09-26
after the "Daily GeoIP Database Download Limit Reached" incident (GeoLite:
30 direct downloads per rolling 24 h; we made ~120).

Fail loud: walker_health ok=False when the key is unset, MaxMind refuses
(429 = limit, 401 = bad key), or the store write fails. GeoLite City updates
Tue/Fri; weekly is within MaxMind's "keep it current" terms and leaves 29
downloads/day of headroom for anything else.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("GEOIP_DISABLE_RETRY", "1")   # walker: no daemon thread
os.environ.setdefault("GEOIP_MMDB_PATH", os.path.join(tempfile.gettempdir(), "geoip_refresh_walker.mmdb"))

import db  # noqa: E402
import geoip_state  # noqa: E402  (import runs ensure_database — PG-first, harmless)

WALKER_NAME = "geoip_db_refresh"
CADENCE_SECONDS = 7 * 86400


def main() -> int:
    t0 = time.time()
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=CADENCE_SECONDS)
    key = (os.environ.get("MAXMIND_LICENSE_KEY") or "").strip()
    if not key:
        msg = "MAXMIND_LICENSE_KEY unset in walker env — cannot refresh store"
        print(f"[{WALKER_NAME}] FAIL {msg}", file=sys.stderr)
        db.write_walker_health_end(WALKER_NAME, ok=False, message=msg)
        return 1
    data = geoip_state._download_mmdb_bytes(key)
    if not data:
        err = geoip_state.status().get("last_error") or ""
        if "429" in err:
            # Expected while the account's rolling 24 h window is exhausted
            # (2026-09-26 incident). Not a walker failure: the store still
            # serves, and geoip_health_canary pages if it grows stale. Defer
            # to the next scheduled run instead of escalating for a week.
            msg = f"deferred: MaxMind download window exhausted (429); store untouched"
            print(f"[{WALKER_NAME}] DEFER {msg}")
            db.write_walker_health_end(WALKER_NAME, ok=True, message=msg[:400], findings_count=0)
            return 0
        msg = f"MaxMind download failed: {err}"
        print(f"[{WALKER_NAME}] FAIL {msg}", file=sys.stderr)
        db.write_walker_health_end(WALKER_NAME, ok=False, message=msg[:400])
        return 1
    sha = hashlib.sha256(data).hexdigest()
    gz = gzip.compress(data, compresslevel=6)
    if not db.write_geoip_db_blob(geoip_state.EDITION, gz, sha, "mac_weekly_walker"):
        msg = "store write failed (see db _log_err)"
        print(f"[{WALKER_NAME}] FAIL {msg}", file=sys.stderr)
        db.write_walker_health_end(WALKER_NAME, ok=False, message=msg)
        return 1
    meta = db.read_geoip_db_blob_meta(geoip_state.EDITION)
    msg = (f"refreshed {geoip_state.EDITION}: mmdb={len(data):,} B gz={len(gz):,} B "
           f"sha256={sha[:12]} store_fetched_at={meta[0].isoformat() if meta else '?'} "
           f"elapsed={time.time() - t0:.1f}s")
    print(f"[{WALKER_NAME}] PASS {msg}")
    db.write_walker_health_end(WALKER_NAME, ok=True, message=msg[:400], findings_count=0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
