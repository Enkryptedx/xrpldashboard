#!/usr/bin/env python3
"""Render build step: fetch GeoLite2-City.mmdb ONCE per deploy.

Runs from render.yaml buildCommand after pip install. The file lands in the
repo directory (gitignored) and persists into the running container, so the
3 gunicorn workers reuse it instead of each downloading on import — the
2026-09-26 MaxMind daily-download-limit incident (30 per rolling 24 h on
GeoLite; 3 workers × ~40 deploys/day blew through it and region_code went
NULL for every visit for the rest of the day).

Fail-open: a failed download (limit reached, key unset, MaxMind down) must
NOT fail the build — the app boots with state lookup disabled and retries
hourly in the background (geoip_state._retry_loop). Always exits 0.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("GEOIP_DISABLE_RETRY", "1")  # no daemon thread in a build

import geoip_state  # noqa: E402  (import performs ensure_database())

s = geoip_state.status()
print(f"[fetch_geoip_db] available={s['available']} source={s['source']} "
      f"path={s['path']} file_age_s={s['file_age_s']} last_error={s['last_error']}")
if not s["available"]:
    print("[fetch_geoip_db] WARNING: GeoLite2 not available at build; app will boot "
          "with state lookup disabled and retry hourly (fail-open, build continues)")
sys.exit(0)
