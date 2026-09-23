"""AI Web Observatory — daily roll-up walker (Charlie ruling
2026-09-21 Mon PM, item 3a).

Aggregates `ai_crawler_hits` by (ua_class, page_family, day),
excluding self-probes, into `ai_crawler_daily_rollup`. Powers the
public /observatory page which serves ONE-WEEK-DELAYED weekly views.

Page family classifier — a small closed vocabulary so the public
page can group without leaking exact endpoints:
- amendments, tokens, check, changes, registry, snapshots, whales,
  pools, lending, rlusd, methodology, home, thisweek, other.

`token_pages_top5` (top 5 token pages fetched by AI crawlers, names
only) is computed separately by the /observatory route from
page_views + token_names; this walker only handles the family
roll-up.

Cadence: hourly (so partial days show up during the day; the final
row settles at day boundary).
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db  # noqa: E402


WALKER_NAME = "ai_crawler_daily_rollup"
WALKER_CADENCE_SECONDS = 3600


def _classify_path(path: str) -> str:
    """Map a URL path to a page family. Closed vocabulary; anything
    unrecognized lands in 'other'. Case-insensitive."""
    if not path:
        return "other"
    p = path.lower()
    if p == "/" or p == "":
        return "home"
    if p.startswith("/amendments"):
        return "amendments"
    if p.startswith("/token") or p.startswith("/tokens"):
        return "tokens"
    if p.startswith("/check"):
        return "check"
    if p.startswith("/changes"):
        return "changes"
    if p.startswith("/registry"):
        return "registry"
    if p.startswith("/.well-known/snapshots") or p.startswith("/snapshots"):
        return "snapshots"
    if p.startswith("/.well-known/registry"):
        return "registry"
    if p.startswith("/whales"):
        return "whales"
    if p.startswith("/pools"):
        return "pools"
    if p.startswith("/lending"):
        return "lending"
    if p.startswith("/rlusd"):
        return "rlusd"
    if p.startswith("/nfts") or p.startswith("/nft"):
        return "nfts"
    if p.startswith("/mpts") or p.startswith("/mpt"):
        return "mpts"
    if p.startswith("/methodology"):
        return "methodology"
    if p.startswith("/thisweek"):
        return "thisweek"
    return "other"


def rollup_day(day_iso: str) -> int:
    """Read ai_crawler_hits for UTC day `day_iso` and upsert one row per
    (ua_class, page_family). Returns number of rows written."""
    if not db.pg_available():
        return 0
    day = dt.date.fromisoformat(day_iso)
    start_ts = int(dt.datetime.combine(day, dt.time.min).replace(
        tzinfo=dt.timezone.utc).timestamp())
    end_ts = int(dt.datetime.combine(
        day + dt.timedelta(days=1), dt.time.min
    ).replace(tzinfo=dt.timezone.utc).timestamp())

    # Charlie ruling 2026-09-23 Wed 07:06 ET: exclude self-probe UAs from
    # the rollup. ai_crawler_hits has no UA column, so LEFT JOIN with
    # page_views on (ts, path) and skip rows whose UA matches any
    # SELF_PROBE_UA_FRAGMENTS entry (rate-test, smoke-test,
    # integration-test, xrpldashboard-, canary probes, etc.). The join
    # can miss (multiple UAs on same (ts, path)); when the page_views UA
    # is NULL the row still counts — the walker keeps the historical
    # behavior for un-joinable rows.
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.abspath(
        _os.path.join(_os.path.dirname(__file__), "..")
    ))
    from public_analytics_filters import SELF_PROBE_UA_FRAGMENTS
    self_probe_patterns = [f"%{f}%" for f in SELF_PROBE_UA_FRAGMENTS]

    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ach.ua_class, ach.path, COUNT(*) "
                "FROM ai_crawler_hits ach "
                "LEFT JOIN LATERAL ( "
                "  SELECT user_agent FROM page_views "
                "   WHERE ts = ach.ts AND path = ach.path LIMIT 1 "
                ") pv ON true "
                "WHERE ach.ts >= %s AND ach.ts < %s "
                "  AND ach.ua_class IS NOT NULL AND ach.ua_class <> '' "
                "  AND NOT (COALESCE(pv.user_agent, '') ILIKE ANY(%s)) "
                "GROUP BY ach.ua_class, ach.path",
                (start_ts, end_ts, self_probe_patterns),
            )
            rows = cur.fetchall()

    # Reduce (ua_class, path) → (ua_class, family)
    per_family: dict[tuple[str, str], int] = {}
    for ua_class, path, cnt in rows:
        family = _classify_path(path or "")
        key = (ua_class, family)
        per_family[key] = per_family.get(key, 0) + int(cnt)

    def _do(conn):
        with conn.cursor() as cur:
            for (ua_class, family), hits in per_family.items():
                cur.execute(
                    "INSERT INTO ai_crawler_daily_rollup "
                    "  (as_of_date, ua_class, page_family, hits, computed_at) "
                    "VALUES (%s::date, %s, %s, %s, now()) "
                    "ON CONFLICT (as_of_date, ua_class, page_family) "
                    "DO UPDATE SET hits = EXCLUDED.hits, computed_at = now()",
                    (day_iso, ua_class, family, hits),
                )
    db._writer_execute_with_retry(f"ai_crawler_rollup[{day_iso}]", _do)
    return len(per_family)


def sitewide_rollup_day(day_iso: str) -> int:
    """Site-wide rollup — classifies UAs across ALL page_views paths
    (not just the 9 agent-tier files ai_crawler_hits covers). Powers
    the primary /observatory view; the agent-tier rollup remains as
    the "what they fetch to verify" sub-table.

    Charlie ruling 2026-09-23 Wed 07:27 ET: ai_crawler_hits' 9-path
    scope was showing 0.6% of the real AI-crawler activity on the
    site; observatory needed the full picture. `classify_ai_crawler`
    is called on the raw UA string, so we get real AI-answer /
    unlisted / etc. bucketing on every page_views row without touching
    the request path.

    Exclusions:
    - SELF_PROBE_UA_FRAGMENTS (canaries, walkers, JJ shell,
      rate-test/smoke-test/integration-test markers)
    - ua_class = 'seo-crawler' (they crawl for SEO products, not for
      AI answers; documented separately in the agent-tier sub-view)
    - ua_class IS NULL (normal browser / empty UA — not a crawler)

    Returns number of (ua_class, family) rows written."""
    if not db.pg_available():
        return 0
    day = dt.date.fromisoformat(day_iso)
    start_ts = int(dt.datetime.combine(day, dt.time.min).replace(
        tzinfo=dt.timezone.utc).timestamp())
    end_ts = int(dt.datetime.combine(
        day + dt.timedelta(days=1), dt.time.min
    ).replace(tzinfo=dt.timezone.utc).timestamp())

    HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from public_analytics_filters import SELF_PROBE_UA_FRAGMENTS
    from agent_tier_rate_limit import classify_ai_crawler
    self_probe_patterns = [f"%{f}%" for f in SELF_PROBE_UA_FRAGMENTS]

    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_agent, path "
                "FROM page_views "
                "WHERE ts >= %s AND ts < %s "
                "  AND user_agent IS NOT NULL AND user_agent <> '' "
                "  AND NOT (user_agent ILIKE ANY(%s))",
                (start_ts, end_ts, self_probe_patterns),
            )
            rows = cur.fetchall()

    # Charlie ruling 2026-09-23 Wed 13:32 ET (build #3): behavioral
    # fingerprint for the ~600/day UNLISTED. Relabel UNLISTED rows to
    # `scraper-unclassified` when their visitor_hash matches the
    # scraper session profile:
    #   - zero static-asset fetches (never hit /static/* or /favicon*)
    #   - >= 20 non-asset hits in the day (high rate)
    #   - <= 3 distinct non-asset paths (narrow path set)
    # UNLISTED rows whose visitor_hash doesn't match the profile stay
    # as UNLISTED. Classification only, no blocks — nothing about
    # rate-limits or content changes.
    scraper_hashes: set[str] = set()
    if db.pg_available():
        try:
            with db.pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT visitor_hash "
                        "  FROM page_views "
                        " WHERE ts >= %s AND ts < %s "
                        "   AND visitor_hash IS NOT NULL AND visitor_hash <> '' "
                        " GROUP BY visitor_hash "
                        "HAVING COUNT(*) FILTER (WHERE path NOT LIKE '/static/%%' "
                        "                             AND path NOT LIKE '/favicon%%') >= 20 "
                        "   AND COUNT(DISTINCT path) FILTER (WHERE path NOT LIKE '/static/%%' "
                        "                                       AND path NOT LIKE '/favicon%%') <= 3 "
                        "   AND COUNT(*) FILTER (WHERE path LIKE '/static/%%' "
                        "                             OR  path LIKE '/favicon%%') = 0",
                        (start_ts, end_ts),
                    )
                    scraper_hashes = {r[0] for r in cur.fetchall() if r[0]}
        except Exception:
            scraper_hashes = set()

    # Re-fetch with visitor_hash to apply the reclassification.
    if scraper_hashes:
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_agent, path, visitor_hash "
                    "FROM page_views "
                    "WHERE ts >= %s AND ts < %s "
                    "  AND user_agent IS NOT NULL AND user_agent <> '' "
                    "  AND NOT (user_agent ILIKE ANY(%s))",
                    (start_ts, end_ts, self_probe_patterns),
                )
                rows = cur.fetchall()

    per_family: dict[tuple[str, str], int] = {}
    for row in rows:
        if len(row) == 3:
            user_agent, path, visitor_hash = row
        else:
            user_agent, path = row
            visitor_hash = None
        ua_class = classify_ai_crawler(user_agent)
        if ua_class is None or ua_class == "seo-crawler":
            continue
        if ua_class == "UNLISTED" and visitor_hash and visitor_hash in scraper_hashes:
            ua_class = "scraper-unclassified"
        family = _classify_path(path or "")
        key = (ua_class, family)
        per_family[key] = per_family.get(key, 0) + 1

    def _do(conn):
        with conn.cursor() as cur:
            # Delete the day's rows first so classes that dropped to 0
            # (e.g. after a class collapse) don't leave stale entries.
            cur.execute(
                "DELETE FROM ai_crawler_sitewide_daily_rollup "
                "WHERE as_of_date = %s::date",
                (day_iso,),
            )
            for (ua_class, family), hits in per_family.items():
                cur.execute(
                    "INSERT INTO ai_crawler_sitewide_daily_rollup "
                    "  (as_of_date, ua_class, page_family, hits, computed_at) "
                    "VALUES (%s::date, %s, %s, %s, now()) "
                    "ON CONFLICT (as_of_date, ua_class, page_family) "
                    "DO UPDATE SET hits = EXCLUDED.hits, computed_at = now()",
                    (day_iso, ua_class, family, hits),
                )
    db._writer_execute_with_retry(f"ai_crawler_sitewide_rollup[{day_iso}]", _do)
    return len(per_family)


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = "not_yet_stamped"
    try:
        today = dt.date.today().isoformat()
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        # Roll up yesterday (final) + today (partial). Yesterday is a
        # settled day; today keeps updating throughout the day and
        # settles at day boundary.
        n_today = rollup_day(today)
        n_yesterday = rollup_day(yesterday)
        # Site-wide rollup (page_views, all paths, self-probe + SEO excluded).
        # Charlie ruling 2026-09-23 Wed 07:27 ET — primary observatory source.
        s_today = sitewide_rollup_day(today)
        s_yesterday = sitewide_rollup_day(yesterday)
        message = (
            f"agent-tier today={today}:{n_today} yesterday={yesterday}:{n_yesterday} · "
            f"sitewide today={today}:{s_today} yesterday={yesterday}:{s_yesterday}"
        )
        print(f"[ai_crawler_daily_rollup] {message}")
        ok = True
        return 0
    except Exception as e:
        message = f"exception: {type(e).__name__}: {e}"
        raise
    finally:
        db.write_walker_health_end(
            WALKER_NAME, ok=ok,
            message=message or ("clean_no_message" if ok else "unlabeled_failure"),
        )


if __name__ == "__main__":
    sys.exit(main())
