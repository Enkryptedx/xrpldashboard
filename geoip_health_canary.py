"""geoip_health_canary — page when state-level geo lookup goes dark.

2026-09-26: MaxMind's daily download limit left /healthz.geoip disabled and
page_views.region_code NULL for every visit from 13:00Z, with every walker
green — nothing measured "is region live". This canary does, on a 15-min
cadence from the Mac:

  1. GET https://xrpldashboard.com/healthz → `geoip.available` must be true
     (finding: geoip_unavailable, with last_error — 429 = MaxMind limit,
     "unset" = missing key, "store blob too old" = weekly walker dead).
  2. Postgres: over the last 60 min, if >= MIN_ROWS page_views rows exist
     and the NULL-region share >= NULL_RATIO_MAX, finding: region_null_ratio.

Any finding → walker_health findings_count > 0 → L1 pager
check_walker_findings. Run itself succeeds (ok=True) — findings are the
signal, not a crash. Self-probe UA starts with "xrpldashboard-" so the
analytics filter drops this canary's own hits.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request

import certifi

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db  # noqa: E402

WALKER_NAME = "geoip_health_canary"
CADENCE_SECONDS = 900
HEALTHZ_URL = os.environ.get("GEOIP_CANARY_HEALTHZ_URL", "https://xrpldashboard.com/healthz")
MIN_ROWS = int(os.environ.get("GEOIP_CANARY_MIN_ROWS", "20"))
NULL_RATIO_MAX = float(os.environ.get("GEOIP_CANARY_NULL_RATIO_MAX", "0.9"))
WINDOW_S = 3600


def evaluate(health: dict | None, rows: int | None, null_rows: int | None) -> list[dict]:
    """Pure: findings from the two signals."""
    findings: list[dict] = []
    geo = (health or {}).get("geoip") if isinstance(health, dict) else None
    if not isinstance(geo, dict):
        findings.append({"severity": "high", "reason": "healthz_no_geoip_field"})
    elif not geo.get("available"):
        findings.append({"severity": "high", "reason": "geoip_unavailable",
                         "last_error": (geo.get("last_error") or "")[:160],
                         "source": geo.get("source")})
    if rows is not None and null_rows is not None and rows >= MIN_ROWS:
        ratio = null_rows / rows
        if ratio >= NULL_RATIO_MAX:
            findings.append({"severity": "high", "reason": "region_null_ratio",
                             "rows": rows, "null_rows": null_rows, "ratio": round(ratio, 3)})
    return findings


def _fetch_healthz() -> dict | None:
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(HEALTHZ_URL, headers={"User-Agent": "xrpldashboard-geoip-health-canary/1.0"})
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _null_ratio_rows():
    with db.pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE region_code IS NULL) FROM page_views "
            "WHERE ts >= EXTRACT(EPOCH FROM now()) - %s",
            (WINDOW_S,),
        )
        n, k = cur.fetchone()
        return int(n or 0), int(k or 0)


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=CADENCE_SECONDS)
    health = None
    rows = null_rows = None
    errs = []
    try:
        health = _fetch_healthz()
    except Exception as e:  # noqa: BLE001
        errs.append(f"healthz fetch failed: {type(e).__name__}")
    try:
        rows, null_rows = _null_ratio_rows()
    except Exception as e:  # noqa: BLE001
        errs.append(f"pg read failed: {type(e).__name__}")
    findings = evaluate(health, rows, null_rows)
    geo = (health or {}).get("geoip", {}) if isinstance(health, dict) else {}
    msg = (f"geoip.available={geo.get('available')} source={geo.get('source')} "
           f"rows_1h={rows} null_region_1h={null_rows} findings={len(findings)}"
           + (f" errors={errs}" if errs else ""))
    ok = not errs
    print(f"[{WALKER_NAME}] {msg}" + (f" FINDINGS={json.dumps(findings)}" if findings else ""))
    db.write_walker_health_end(WALKER_NAME, ok=ok, message=msg[:400], findings_count=len(findings))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
