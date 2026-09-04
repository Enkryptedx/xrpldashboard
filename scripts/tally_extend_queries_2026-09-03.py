#!/usr/bin/env python3
"""Read-only queries for the tally-extension turn (2026-09-03 evening)."""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone

HERE = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, HERE)
import db  # noqa: E402

# Deploy epoch for state/region tracking.
STATE_CLOCK_EPOCH = 1788295800  # 2026-09-01 20:50:00 UTC

# US state code → name (partial — sufficient for what we've captured).
US_STATE_NAMES = {
    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California",
    "CO":"Colorado","CT":"Connecticut","DE":"Delaware","DC":"District of Columbia",
    "FL":"Florida","GA":"Georgia","HI":"Hawaii","ID":"Idaho","IL":"Illinois",
    "IN":"Indiana","IA":"Iowa","KS":"Kansas","KY":"Kentucky","LA":"Louisiana",
    "ME":"Maine","MD":"Maryland","MA":"Massachusetts","MI":"Michigan","MN":"Minnesota",
    "MS":"Mississippi","MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada",
    "NH":"New Hampshire","NJ":"New Jersey","NM":"New Mexico","NY":"New York",
    "NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon",
    "PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota",
    "TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia",
    "WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming",
    "PR":"Puerto Rico","VI":"US Virgin Islands","GU":"Guam","AS":"American Samoa",
    "MP":"Northern Mariana Islands",
}


def hr(t=""):
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


def sub(t):
    print(f"\n--- {t} ---")


def run():
    with db.pg_connect() as conn:
        c = conn.cursor()

        hr("PART 1a — US states first-seen since Sep 1 20:50 UTC")
        # HUMAN
        c.execute("""
            SELECT region_code, MIN(ts) AS first_ts
            FROM page_views
            WHERE region_code LIKE 'US-%%' AND is_bot IS NOT TRUE
              AND ts >= %s
            GROUP BY region_code
            ORDER BY first_ts
        """, (STATE_CLOCK_EPOCH,))
        sub("HUMAN — one row per first-seen state")
        for rc, ts in c.fetchall():
            code = rc.split("-", 1)[1]
            name = US_STATE_NAMES.get(code, "?")
            d = datetime.fromtimestamp(ts, timezone.utc)
            print(f"  {d.date()} {d.strftime('%H:%M UTC')}  {code}  {name}")

        # ANY-TRAFFIC
        c.execute("""
            SELECT region_code, MIN(ts) AS first_ts
            FROM page_views
            WHERE region_code LIKE 'US-%%' AND ts >= %s
            GROUP BY region_code
            ORDER BY first_ts
        """, (STATE_CLOCK_EPOCH,))
        sub("ANY-TRAFFIC — one row per first-seen state")
        for rc, ts in c.fetchall():
            code = rc.split("-", 1)[1]
            name = US_STATE_NAMES.get(code, "?")
            d = datetime.fromtimestamp(ts, timezone.utc)
            print(f"  {d.date()} {d.strftime('%H:%M UTC')}  {code}  {name}")

        hr("PART 1b — New countries in last 30 days")
        # Cutoff = 30 days ago
        cutoff = int(datetime(2026, 8, 4, tzinfo=timezone.utc).timestamp())

        c.execute("""
            WITH fs AS (
              SELECT country, MIN(ts) AS first_ts
              FROM page_views
              WHERE country IS NOT NULL AND country <> 'T1'
              GROUP BY country
            )
            SELECT country, first_ts
            FROM fs
            WHERE first_ts >= %s
            ORDER BY first_ts
        """, (cutoff,))
        sub("ANY-TRAFFIC — new countries first-seen since 2026-08-04")
        for co, ts in c.fetchall():
            d = datetime.fromtimestamp(ts, timezone.utc)
            print(f"  {d.date()}  {co}")

        c.execute("""
            WITH fs AS (
              SELECT country, MIN(ts) AS first_ts
              FROM page_views
              WHERE country IS NOT NULL AND country <> 'T1'
                AND is_bot IS NOT TRUE
              GROUP BY country
            )
            SELECT country, first_ts
            FROM fs
            WHERE first_ts >= %s
            ORDER BY first_ts
        """, (cutoff,))
        sub("HUMAN — new countries first-seen since 2026-08-04")
        for co, ts in c.fetchall():
            d = datetime.fromtimestamp(ts, timezone.utc)
            print(f"  {d.date()}  {co}")

        hr("PART 1c — Non-US regions seen since Sep 1 (all + human)")
        c.execute("""
            SELECT region_code, country,
                   MIN(ts) AS first_ts,
                   COUNT(*) FILTER (WHERE is_bot IS NOT TRUE) AS human_hits,
                   COUNT(*) AS total_hits
            FROM page_views
            WHERE region_code IS NOT NULL
              AND region_code NOT LIKE 'US-%%'
              AND ts >= %s
            GROUP BY region_code, country
            ORDER BY country, region_code
        """, (STATE_CLOCK_EPOCH,))
        for rc, co, ts, h, t in c.fetchall():
            d = datetime.fromtimestamp(ts, timezone.utc)
            print(f"  {d.date()}  {rc:>10s}  ({co})  human={h}  all={t}")

        # Summary counts
        c.execute("""
            SELECT
              COUNT(DISTINCT region_code) FILTER (WHERE region_code NOT LIKE 'US-%%' AND region_code IS NOT NULL) AS non_us_regions_all,
              COUNT(DISTINCT region_code) FILTER (WHERE region_code NOT LIKE 'US-%%' AND region_code IS NOT NULL AND is_bot IS NOT TRUE) AS non_us_regions_human,
              COUNT(DISTINCT region_code) FILTER (WHERE region_code IS NOT NULL) AS all_regions_all,
              COUNT(DISTINCT region_code) FILTER (WHERE region_code IS NOT NULL AND is_bot IS NOT TRUE) AS all_regions_human,
              COUNT(DISTINCT country) FILTER (WHERE region_code IS NOT NULL) AS countries_with_regions
            FROM page_views
        """)
        rec = c.fetchone()
        print(f"\n  non_us_regions_all={rec[0]}  non_us_regions_human={rec[1]}")
        print(f"  ALL_regions_all={rec[2]}  ALL_regions_human={rec[3]}")
        print(f"  countries_with_regions={rec[4]}")

        hr("PART 4a — CORRECTED state-resolution rate")
        # Denominator: US-country HUMAN hits SINCE Sep 1 20:50 UTC
        # Numerator: those with region_code set
        c.execute("""
            SELECT
              COUNT(*) FILTER (WHERE country = 'US' AND is_bot IS NOT TRUE AND ts >= %s) AS us_human_since,
              COUNT(*) FILTER (WHERE country = 'US' AND is_bot IS NOT TRUE AND ts >= %s AND region_code IS NOT NULL) AS us_human_resolved
            FROM page_views
        """, (STATE_CLOCK_EPOCH, STATE_CLOCK_EPOCH))
        us_h, us_h_res = c.fetchone()
        pct = 100 * us_h_res / us_h if us_h else 0
        print(f"  US-human hits since Sep 1 20:50 UTC : {us_h:,}")
        print(f"  ...of which have region_code set   : {us_h_res:,}")
        print(f"  Resolution rate (HUMAN, since deploy): {pct:.1f}%")

        # Compare: US-all (bots + humans) since deploy for context
        c.execute("""
            SELECT
              COUNT(*) FILTER (WHERE country = 'US' AND ts >= %s) AS us_all_since,
              COUNT(*) FILTER (WHERE country = 'US' AND ts >= %s AND region_code IS NOT NULL) AS us_all_resolved
            FROM page_views
        """, (STATE_CLOCK_EPOCH, STATE_CLOCK_EPOCH))
        us_all, us_all_res = c.fetchone()
        pct_all = 100 * us_all_res / us_all if us_all else 0
        print(f"\n  For comparison — US-all hits since deploy: {us_all:,}")
        print(f"  ...with region_code set                   : {us_all_res:,}")
        print(f"  All-traffic resolution rate                : {pct_all:.1f}%")

        hr("PART 4b — walker_node_fallback rows with walker_name='unknown'")
        c.execute("""
            SELECT ts, reason, COALESCE(details, '(none)') AS details
            FROM walker_node_fallback
            WHERE walker_name = 'unknown'
            ORDER BY ts DESC
            LIMIT 5
        """)
        for ts, reason, details in c.fetchall():
            print(f"  {ts.isoformat()}  reason={reason[:60]}  details={str(details)[:60]}")


if __name__ == "__main__":
    run()
