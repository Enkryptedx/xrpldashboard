#!/usr/bin/env python3
"""Backfill page_views.region_code for rows logged NULL while GeoIP was
unavailable (2026-09-26 MaxMind limit incident), using the day-salted
ip_day_hash: rows from the SAME IP on the SAME UTC day that DID get a region
(before the outage, or after recovery) donate their region_code.

Honesty rules:
  * only hashes with exactly ONE distinct known region_code that day donate
    (ambiguous hashes are skipped and counted);
  * raw IPs are never stored, so rows whose hash never appears with a region
    that day are unrecoverable and stay NULL;
  * dry-run by default; --apply performs the UPDATE and prints the count.

Run with the owner env (writes). Charlie ruling 2026-09-26 14:47 ET.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import db  # noqa: E402

SQL_STATS = """
WITH d AS (
  SELECT id, ip_day_hash, region_code, ts FROM page_views
   WHERE ts >= EXTRACT(EPOCH FROM %(day_start)s::timestamptz)
     AND ts <  EXTRACT(EPOCH FROM %(day_end)s::timestamptz)
), known AS (
  SELECT ip_day_hash, min(region_code) AS region_code, count(DISTINCT region_code) AS nreg
    FROM d WHERE region_code IS NOT NULL AND ip_day_hash IS NOT NULL GROUP BY ip_day_hash
), nulls AS (
  SELECT id, ip_day_hash FROM d WHERE region_code IS NULL AND ip_day_hash IS NOT NULL
)
SELECT (SELECT count(*) FROM nulls) AS null_rows,
       (SELECT count(*) FROM nulls n JOIN known k ON k.ip_day_hash = n.ip_day_hash WHERE k.nreg = 1) AS recoverable,
       (SELECT count(*) FROM nulls n JOIN known k ON k.ip_day_hash = n.ip_day_hash WHERE k.nreg > 1) AS ambiguous
"""

SQL_APPLY = """
WITH d AS (
  SELECT ip_day_hash, region_code FROM page_views
   WHERE ts >= EXTRACT(EPOCH FROM %(day_start)s::timestamptz)
     AND ts <  EXTRACT(EPOCH FROM %(day_end)s::timestamptz)
), known AS (
  SELECT ip_day_hash, min(region_code) AS region_code
    FROM d WHERE region_code IS NOT NULL AND ip_day_hash IS NOT NULL
   GROUP BY ip_day_hash HAVING count(DISTINCT region_code) = 1
)
UPDATE page_views p SET region_code = k.region_code
  FROM known k
 WHERE p.region_code IS NULL AND p.ip_day_hash = k.ip_day_hash
   AND p.ts >= EXTRACT(EPOCH FROM %(day_start)s::timestamptz)
   AND p.ts <  EXTRACT(EPOCH FROM %(day_end)s::timestamptz)
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-09-26", help="UTC day YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="perform the UPDATE (default: dry-run)")
    a = ap.parse_args()
    params = {"day_start": f"{a.day} 00:00:00+00", "day_end": f"{a.day} 23:59:59.999+00"}
    with db.pg_connect() as conn, conn.cursor() as cur:
        cur.execute(SQL_STATS, params)
        null_rows, recoverable, ambiguous = cur.fetchone()
        print(f"[backfill_region] day={a.day} null_rows={null_rows} recoverable={recoverable} ambiguous={ambiguous}")
        if not a.apply:
            print("[backfill_region] dry-run; pass --apply to update")
            return 0
        cur.execute(SQL_APPLY, params)
        n = cur.rowcount
        conn.commit()
        print(f"[backfill_region] APPLIED updated_rows={n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
