#!/usr/bin/env python3
"""Analytics runner — evening 2026-09-03. One-shot: Part 0/1/4/5/6.

Charlie's ask (msg #16421). Read-only. Post-v5 is_bot classification.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, HERE)
import db  # noqa: E402


def epoch(d: str) -> int:
    """Turn 'YYYY-MM-DD' → UTC start-of-day epoch."""
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def hr(t=""):
    print(f"\n{'=' * 80}\n{t}\n{'=' * 80}")


def sub(t):
    print(f"\n--- {t} ---")


def fmt_num(n):
    return f"{n:,}" if isinstance(n, (int, float)) else str(n)


def run():
    with db.pg_connect() as conn:
        c = conn.cursor()

        # ========================================================
        # PART 0 — LAST MONTH (Aug 3 → Sep 3)
        # ========================================================
        hr("PART 0 — LAST MONTH (Aug 3 → Sep 3 UTC), post-v5 classification")

        # Daily human + bot + total
        sub("Daily hits (human · bot · total · bot%)")
        c.execute("""
            SELECT
              DATE(to_timestamp(ts) AT TIME ZONE 'UTC') AS day,
              COUNT(*) FILTER (WHERE is_bot IS NOT TRUE) AS human,
              COUNT(*) FILTER (WHERE is_bot IS TRUE) AS bot,
              COUNT(*) AS total
            FROM page_views
            WHERE ts >= %s AND ts < %s
            GROUP BY day
            ORDER BY day
        """, (epoch("2026-08-03"), epoch("2026-09-04")))
        rows = c.fetchall()
        print(f"{'day':12s} {'human':>10s} {'bot':>10s} {'total':>10s} {'bot%':>7s}")
        for d, h, b, t in rows:
            pct = f"{100*b/t:.1f}%" if t else "—"
            print(f"{str(d):12s} {h:>10,} {b:>10,} {t:>10,} {pct:>7s}")

        # Weekly buckets
        sub("Weekly buckets (Mon–Sun UTC bins)")
        c.execute("""
            SELECT
              DATE(to_timestamp(ts) AT TIME ZONE 'UTC') -
                (EXTRACT(dow FROM to_timestamp(ts) AT TIME ZONE 'UTC')::int - 1 + 7) %% 7 AS week_start,
              COUNT(*) FILTER (WHERE is_bot IS NOT TRUE) AS human,
              COUNT(*) FILTER (WHERE is_bot IS TRUE) AS bot,
              COUNT(*) AS total,
              COUNT(DISTINCT country) FILTER (WHERE country IS NOT NULL AND country <> 'T1' AND is_bot IS NOT TRUE) AS c_hum,
              COUNT(DISTINCT country) FILTER (WHERE country IS NOT NULL AND country <> 'T1') AS c_all,
              COUNT(DISTINCT region_code) FILTER (WHERE region_code LIKE 'US-%%' AND is_bot IS NOT TRUE) AS us_st_hum
            FROM page_views
            WHERE ts >= %s AND ts < %s
            GROUP BY week_start
            ORDER BY week_start
        """, (epoch("2026-08-03"), epoch("2026-09-04")))
        rows_wk = c.fetchall()
        print(f"{'week_start':12s} {'human':>10s} {'bot':>10s} {'total':>10s} {'c_hum':>6s} {'c_all':>6s} {'us_st':>6s}")
        for ws, h, b, t, ch, ca, us in rows_wk:
            print(f"{str(ws):12s} {h:>10,} {b:>10,} {t:>10,} {ch:>6} {ca:>6} {us:>6}")

        # Compute human trend verdict from weekly totals
        wk_humans = [r[1] for r in rows_wk]
        if len(wk_humans) >= 2:
            first, last = wk_humans[0], wk_humans[-1]
            # Growing if >=+10% end-vs-start, declining if <=-10%, else flat
            if first > 0:
                pct_change = 100 * (last - first) / first
            else:
                pct_change = 0
            if pct_change >= 10:
                verdict = "GROWING"
            elif pct_change <= -10:
                verdict = "DECLINING"
            else:
                verdict = "FLAT"
            print(f"\nVERDICT (start-week {first:,} → end-week {last:,} = {pct_change:+.1f}%): {verdict}")

        # Aug 29 bot spike + top bot-country/UA that day
        sub("Aug 29 bot spike breakdown (to isolate from human trend)")
        c.execute("""
            SELECT
              COUNT(*) FILTER (WHERE is_bot IS TRUE) AS bot_29,
              COUNT(*) FILTER (WHERE is_bot IS NOT TRUE) AS hum_29,
              COUNT(*) AS tot_29
            FROM page_views
            WHERE ts >= %s AND ts < %s
        """, (epoch("2026-08-29"), epoch("2026-08-30")))
        b29, h29, t29 = c.fetchone()
        print(f"Aug 29 UTC: total={t29:,} human={h29:,} bot={b29:,} bot%={100*b29/t29:.1f}%")

        c.execute("""
            SELECT country, COUNT(*) AS n FROM page_views
            WHERE ts >= %s AND ts < %s AND is_bot IS TRUE
            GROUP BY country ORDER BY n DESC LIMIT 5
        """, (epoch("2026-08-29"), epoch("2026-08-30")))
        for co, n in c.fetchall():
            print(f"  bot country: {co} → {n:,}")

        c.execute("""
            SELECT COALESCE(substr(user_agent, 1, 60), '(null)') AS ua, COUNT(*) AS n
            FROM page_views WHERE ts >= %s AND ts < %s AND is_bot IS TRUE
            GROUP BY ua ORDER BY n DESC LIMIT 5
        """, (epoch("2026-08-29"), epoch("2026-08-30")))
        for ua, n in c.fetchall():
            print(f"  bot UA: {n:>6,}  {ua}")

        # Weekly distinct US states (humans only, since state clock started Sep 1)
        sub("US states per week (humans only, since 2026-09-01 20:51 UTC)")
        c.execute("""
            SELECT
              DATE(to_timestamp(ts) AT TIME ZONE 'UTC') AS day,
              COUNT(DISTINCT region_code) FILTER (WHERE region_code LIKE 'US-%%' AND is_bot IS NOT TRUE) AS us_st_hum,
              COUNT(DISTINCT region_code) FILTER (WHERE region_code LIKE 'US-%%') AS us_st_all
            FROM page_views
            WHERE ts >= %s AND ts < %s
            GROUP BY day ORDER BY day
        """, (epoch("2026-09-01"), epoch("2026-09-04")))
        for d, hum, alln in c.fetchall():
            print(f"  {d}: human={hum} · any={alln}")

        # ========================================================
        # PART 1 — Two-week growth of all-time totals (Aug 20 → Sep 3)
        # ========================================================
        hr("PART 1 — TWO-WEEK GROWTH OF ALL-TIME TOTALS (Aug 20 → Sep 3)")

        for label, cutoff_epoch in [("as-of Aug 20 (end of day)", epoch("2026-08-21")),
                                    ("as-of Sep 3 (end of day)", epoch("2026-09-04"))]:
            c.execute("""
                SELECT
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE is_bot IS NOT TRUE) AS human,
                  COUNT(DISTINCT country) FILTER (WHERE country IS NOT NULL AND country <> 'T1') AS c_all_no_t1,
                  COUNT(DISTINCT country) FILTER (WHERE country IS NOT NULL AND country <> 'T1' AND is_bot IS NOT TRUE) AS c_hum_no_t1
                FROM page_views
                WHERE ts < %s
            """, (cutoff_epoch,))
            t, h, ca, ch = c.fetchone()
            print(f"{label}:")
            print(f"  total_hits={t:,} human_hits={h:,} countries_any_excl_T1={ca} countries_human_excl_T1={ch}")

        # First-seen day per new country in window
        sub("New countries first-seen in Aug 21 → Sep 3 window (any traffic, excl T1)")
        c.execute("""
            WITH fs AS (
              SELECT country, MIN(ts) AS first_ts
              FROM page_views
              WHERE country IS NOT NULL AND country <> 'T1'
              GROUP BY country
            )
            SELECT DATE(to_timestamp(first_ts) AT TIME ZONE 'UTC') AS day,
                   array_agg(country ORDER BY country) AS countries
            FROM fs
            WHERE first_ts >= %s AND first_ts < %s
            GROUP BY day ORDER BY day
        """, (epoch("2026-08-21"), epoch("2026-09-04")))
        for d, countries in c.fetchall():
            print(f"  {d}: {len(countries)} new · {', '.join(countries)}")

        sub("New countries first-seen from HUMAN traffic (post-v5 kind=human)")
        c.execute("""
            WITH fs AS (
              SELECT country, MIN(ts) AS first_ts
              FROM page_views
              WHERE country IS NOT NULL AND country <> 'T1' AND is_bot IS NOT TRUE
              GROUP BY country
            )
            SELECT DATE(to_timestamp(first_ts) AT TIME ZONE 'UTC') AS day,
                   array_agg(country ORDER BY country) AS countries
            FROM fs
            WHERE first_ts >= %s AND first_ts < %s
            GROUP BY day ORDER BY day
        """, (epoch("2026-08-21"), epoch("2026-09-04")))
        for d, countries in c.fetchall():
            print(f"  {d}: {len(countries)} new · {', '.join(countries)}")

        # Growth headline
        c.execute("""
            SELECT
              (SELECT COUNT(*) FROM page_views WHERE ts >= %s AND ts < %s) AS total_new,
              (SELECT COUNT(*) FROM page_views WHERE ts >= %s AND ts < %s AND is_bot IS NOT TRUE) AS hum_new
        """, (epoch("2026-08-21"), epoch("2026-09-04"),
              epoch("2026-08-21"), epoch("2026-09-04")))
        tn, hn = c.fetchone()
        print(f"\nGrowth headline (Aug 21 → Sep 3 = 14 days): +{tn:,} total hits · +{hn:,} human hits")

        # ========================================================
        # PART 4 — STANDARD EVENING CHECK
        # ========================================================
        hr("PART 4 — STANDARD EVENING CHECK")

        # Since 16:28 UTC today (2026-09-03) — hour bucket
        ep_1628 = int(datetime(2026, 9, 3, 16, 28, tzinfo=timezone.utc).timestamp())
        sub(f"Since 16:28 UTC 2026-09-03 (epoch {ep_1628})")
        c.execute("""
            SELECT COUNT(*) FILTER (WHERE is_bot IS NOT TRUE) AS hum,
                   COUNT(*) FILTER (WHERE is_bot IS TRUE) AS bot,
                   COUNT(*) AS tot
            FROM page_views WHERE ts >= %s
        """, (ep_1628,))
        h, b, t = c.fetchone()
        print(f"  human={h:,} bot={b:,} total={t:,} bot%={100*b/max(t,1):.1f}%")

        sub("Sep 3 day-to-date vs Sep 2 (full day)")
        for lo, hi, label in [
            (epoch("2026-09-03"), epoch("2026-09-04"), "Sep 3 to-date"),
            (epoch("2026-09-02"), epoch("2026-09-03"), "Sep 2 full"),
        ]:
            c.execute("""
                SELECT COUNT(*) FILTER (WHERE is_bot IS NOT TRUE) AS hum,
                       COUNT(*) FILTER (WHERE is_bot IS TRUE) AS bot,
                       COUNT(*) AS tot
                FROM page_views WHERE ts >= %s AND ts < %s
            """, (lo, hi))
            h, b, t = c.fetchone()
            print(f"  {label}: human={h:,} bot={b:,} total={t:,} bot%={100*b/max(t,1):.1f}%")

        sub("Top 10 countries — Sep 3 to-date (human)")
        c.execute("""
            SELECT country, COUNT(*) AS n FROM page_views
            WHERE ts >= %s AND ts < %s AND is_bot IS NOT TRUE
            GROUP BY country ORDER BY n DESC NULLS LAST LIMIT 10
        """, (epoch("2026-09-03"), epoch("2026-09-04")))
        for co, n in c.fetchall():
            print(f"  {co}: {n:,}")

        sub("Top 10 countries — Sep 3 to-date (all)")
        c.execute("""
            SELECT country, COUNT(*) AS n FROM page_views
            WHERE ts >= %s AND ts < %s
            GROUP BY country ORDER BY n DESC NULLS LAST LIMIT 10
        """, (epoch("2026-09-03"), epoch("2026-09-04")))
        for co, n in c.fetchall():
            print(f"  {co}: {n:,}")

        sub("Top 10 pages — Sep 3 to-date (human)")
        c.execute("""
            SELECT path, COUNT(*) AS n FROM page_views
            WHERE ts >= %s AND ts < %s AND is_bot IS NOT TRUE
            GROUP BY path ORDER BY n DESC LIMIT 10
        """, (epoch("2026-09-03"), epoch("2026-09-04")))
        for p, n in c.fetchall():
            print(f"  {n:>6,}  {p[:80]}")

        sub("US states — humans (since deploy 2026-09-01 20:51 UTC)")
        c.execute("""
            SELECT
              COUNT(DISTINCT region_code) FILTER (WHERE region_code LIKE 'US-%%' AND is_bot IS NOT TRUE) AS us_hum,
              COUNT(DISTINCT region_code) FILTER (WHERE region_code LIKE 'US-%%') AS us_all,
              COUNT(*) FILTER (WHERE country = 'US') AS us_rows,
              COUNT(*) FILTER (WHERE country = 'US' AND region_code IS NOT NULL) AS us_resolved
            FROM page_views
        """)
        us_hum, us_all, us_rows, us_resolved = c.fetchone()
        resolve_pct = (100 * us_resolved / us_rows) if us_rows else 0
        print(f"  us_states_human={us_hum} · us_states_all={us_all}")
        print(f"  resolution rate: {us_resolved:,}/{us_rows:,} = {resolve_pct:.1f}%")

        sub("Top 10 human US states (all-time)")
        c.execute("""
            SELECT region_code, COUNT(*) AS n
            FROM page_views WHERE region_code LIKE 'US-%%' AND is_bot IS NOT TRUE
            GROUP BY region_code ORDER BY n DESC LIMIT 10
        """)
        for r, n in c.fetchall():
            print(f"  {r}: {n:,}")

        # ========================================================
        # PART 5 — SOVEREIGNTY HEALTH
        # ========================================================
        hr("PART 5 — SOVEREIGNTY HEALTH")
        ep_6h = int((datetime.now(timezone.utc) - timedelta(hours=6)).timestamp())

        sub(f"walker_node_fallback per walker, last 6h (since {datetime.fromtimestamp(ep_6h, timezone.utc).isoformat()})")
        c.execute("""
            SELECT walker_name, COUNT(*) AS n, MAX(ts) AS last_ts
            FROM walker_node_fallback
            WHERE ts >= to_timestamp(%s) GROUP BY walker_name ORDER BY n DESC
        """, (ep_6h,))
        rows = c.fetchall()
        if rows:
            for w, n, lts in rows:
                # lts is now a datetime (timestamptz); handle either type.
                if hasattr(lts, 'isoformat'):
                    lts_s = lts.isoformat()
                else:
                    lts_s = datetime.fromtimestamp(lts, timezone.utc).isoformat()
                print(f"  {w:30s} → {n:>6,} events, last {lts_s}")
        else:
            print("  (no walker_node_fallback events in the last 6h)")

        sub("Sanity check: cold_storage + escrow_supply should be zero (deployed last night)")
        c.execute("""
            SELECT walker_name, COUNT(*) AS n
            FROM walker_node_fallback
            WHERE ts >= to_timestamp(%s) AND walker_name IN ('cold_storage', 'escrow_supply', 'cold_storage_walker', 'escrow_supply_walker')
            GROUP BY walker_name
        """, (ep_6h,))
        rows = c.fetchall()
        if not rows:
            print("  cold_storage + escrow_supply: 0 events last 6h ✅")
        else:
            for w, n in rows:
                print(f"  ⚠️  {w}: {n} events (expected 0)")

        sub("check_page + token_page + lending_page baseline for tomorrow's build")
        c.execute("""
            SELECT walker_name, COUNT(*) AS n
            FROM walker_node_fallback
            WHERE ts >= to_timestamp(%s) AND walker_name IN ('check_page', 'token_page', 'lending_page')
            GROUP BY walker_name ORDER BY n DESC
        """, (ep_6h,))
        for w, n in c.fetchall():
            print(f"  {w}: {n} events / 6h = ~{n*4}/day")

        # ========================================================
        # PART 6 — HEALTH GLANCE
        # ========================================================
        hr("PART 6 — HEALTH GLANCE")
        sub(f"walker_health findings > 0 since 16:28 UTC (excluding xrpl_stream_restart_rate)")
        c.execute("""
            SELECT walker_name, findings_count, last_run_ok, last_success_at, last_run_message
            FROM walker_health
            WHERE COALESCE(findings_count, 0) > 0
              AND walker_name <> 'xrpl_stream_restart_rate'
            ORDER BY findings_count DESC, walker_name
        """)
        rows = c.fetchall()
        if rows:
            for w, f, ok, sa, msg in rows:
                print(f"  {w:30s} findings={f} ok={ok} · {str(msg)[:60]}")
        else:
            print("  (no walker_health findings > 0 since 16:28 UTC)")

        sub("xrpl_stream_restart_rate latest reading")
        c.execute("""
            SELECT last_run_ok, findings_count, last_run_message, last_run_completed
            FROM walker_health WHERE walker_name = 'xrpl_stream_restart_rate'
        """)
        row = c.fetchone()
        if row:
            print(f"  ok={row[0]} findings_count={row[1]} completed={row[3]}")
            print(f"  msg: {row[2]}")

        sub("site_totals_history — verify Part 3 landed")
        c.execute("""
            SELECT COUNT(*) FROM site_totals_history
        """)
        n = c.fetchone()[0]
        print(f"  rows: {n}")
        if n > 0:
            c.execute("SELECT computed_at, total_hits, human_hits, countries_all_excluding_t1, us_states_all FROM site_totals_history ORDER BY computed_at DESC LIMIT 3")
            for ca, th, hh, cae, us in c.fetchall():
                print(f"  {datetime.fromtimestamp(ca, timezone.utc).isoformat()}: total={th:,} human={hh:,} countries_no_t1={cae} us_states={us}")


if __name__ == "__main__":
    run()
