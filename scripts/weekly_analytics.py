"""weekly_analytics.py — deterministic weekly analytics report.

Called by:
- standing-orders morning report (08:00 ET)
- evening ad-hoc report

Codifies (so this cannot silently drift):

- Self-probe UA exclusion — every "xrpldashboard-*", "xrpld-anchor-canary",
  "public-route-canary", "standing-orders", "betterstack", "mac_tunnel*",
  "dockvault*", "route_5xx*", "openclaw_*", "JJ-*" UA is NEVER counted in
  "machines pulling" numbers (third-time-was-a-charm rule, filed
  2026-09-19 evening: self-probes had polluted anchors.json, /check.json,
  and registry pull counts on three separate reports).

- Known-bot UA list — declared bots that occasionally slip past the
  is_bot writer's live-inference. Includes PetalBot (Huawei), Bytespider,
  Amazonbot, SemrushBot, Applebot, AionBot (declared, but paces itself
  under the anon rate limit — fleet-blocked at request time as of
  commit 47357b9), Lightpanda (headless), and the SG Android-7.0
  PetalBot cluster (951 hits w/o classifier stamps 2026-09-14→19).

- Strict ISO-alpha2 country allowlist + US-XX state validators. States
  are reported as N of 50; DC and territories are reported separately.

- One-place "human" definition: `is_bot IS NULL` AND user_agent NOT in
  KNOWN_BOT_UA_FRAGMENTS AND (during writer-off days) baseline-adjusted.

- Conservative-estimate logic for days when is_bot_writer was OFF: use
  the classified-day baseline (writer-on days this week, KNOWN_BOT_UAS
  excluded) applied to the outage days, and report the weekly total as
  a RANGE with the reason spelled out.

- Baseline anomaly guard (Charlie ruling 2026-09-21, threshold 3×):
  any country with this-week "humans" > 3× its prior-4-week weekly
  average gets flagged AND a three-axis diagnostic (top-hash share,
  page depth, peak-2h burst) prints on the next line. The 5× tier
  still gets a ⚠. No surge reaches a headline unverified — BR Sunday
  2026-09-20 headlined 5.2× and turned out to be one home-polling
  client + a quiet Sunday.

Rule the humans out, then count what's left.
"""
from __future__ import annotations

import os
import subprocess
import datetime as dt
import sys
from typing import NamedTuple


# ── UA exclusion lists ────────────────────────────────────────────────
#
# Both lists are matched case-insensitively via ILIKE-style fragment
# search in SQL (COALESCE(user_agent, '') ILIKE '%fragment%').

SELF_PROBE_UA_FRAGMENTS = (
    # NOTE: these fragments are the shared allow-list — the canonical
    # definition now lives in public_analytics_filters.py so /analytics
    # and this script converge (Charlie ruling 2026-09-20). Kept inline
    # here as a copy for import-safety and offline runs, but the
    # weekly report and /analytics import the same source of truth.
    "xrpldashboard-",         # every internal canary / walker HTTP client
    "xrpld-anchor-canary",    # anchor canary
    "public-route-canary",    # route-200 canary
    "standing-orders",        # standing-orders morning report
    "betterstack",            # BetterStack Uptime monitors
    "mac_tunnel",             # Mac tunnel health canary
    "dockvault",              # DockVault mirror/monitor canaries
    "route_5xx",              # route_5xx_rate walker
    "openclaw_",              # openclaw_cli_backend_drift
    "JJ-",                    # JJ interactive shell (owner-authenticated)
    "PROOF-",                 # Charlie's proof-of-life curl wrapper
)

KNOWN_BOT_UA_FRAGMENTS = (
    # Search / crawl / SEO — declared, sometimes not stamped
    "PetalBot",               # Huawei — SG Android-7.0 cluster is here
    "Bytespider",             # ByteDance
    "Amazonbot",              # Amazon Alexa's crawl
    "SemrushBot",
    "Applebot",
    "AhrefsBot",
    "MJ12bot",
    "DotBot",
    "YandexBot",
    "Baiduspider",
    # AI-citation crawlers — usually classified, listed for completeness
    "GPTBot",
    "ChatGPT-User",
    "ClaudeBot",
    "Claude-Web",
    "Claude-User",
    "anthropic-ai",
    "PerplexityBot",
    # Headless + scraper frameworks
    "HeadlessChrome",
    "Lightpanda",             # /check.json headless
    "AionBot",                # declared, paces under limiter — fleet-blocked 47357b9
    "Playwright",
    "Puppeteer",
    "Selenium",
    "Go-http-client",
    "python-requests",
    "aiohttp",
    "Werkzeug",
    "curl/",
    "Wget",
    "Scrapy",
    "Java/",
    "spider",
    "crawler",
    "/bot",
    "compatible; bot",
)


# ── News-domain referrer resolution ───────────────────────────────────
#
# Chrome's default Referrer-Policy is `strict-origin-when-cross-origin`,
# which strips the referring URL to bare origin on cross-origin
# navigation. So a news-site inbound arrives as `https://<site>/` with no
# article path. Out-of-band author confirmation, direct citation quote,
# or manual page-scan is the only way to resolve which specific article
# sent the traffic. When resolved, record it in
# docs/PRESS_CITATIONS.md and add a NEWS_DOMAIN_REFERRERS entry so future
# weekly reports surface the source page next to the raw-referrer count.
#
# Each entry maps the SEEN referrer origin (byte-exact) to a
# {resolved_url, cited_date, cited_page} record. `resolved_url` may be
# None if we know the origin but haven't confirmed the article yet.
NEWS_DOMAIN_REFERRERS = {
    "https://cryptoslate.com/": {
        "resolved_url": "https://cryptoslate.com/xrpls-new-lending-tool-could-lock-up-your-xrp-from-minutes-to-decades/",
        "cited_date": "2026-09-18",
        "cited_page": "/amendments",
        "citation_ref": "docs/PRESS_CITATIONS.md",
    },
    "https://wordupnews.com/cryptocurrency/revolut-faces-multiple-ransom-demands-with-no-direct-contact": {
        "resolved_url": "https://wordupnews.com/cryptocurrency/revolut-faces-multiple-ransom-demands-with-no-direct-contact",
        "cited_date": "2026-09-19",
        "cited_page": "/amendments",
        "citation_ref": None,
    },
    "https://amznusa.com/xrpls-new-lending-tool-could-lock-up-your-xrp-from-minutes-t": {
        "resolved_url": "https://amznusa.com/xrpls-new-lending-tool-could-lock-up-your-xrp-from-minutes-to-decades",
        "cited_date": "2026-09-18",
        "cited_page": "/amendments",
        "citation_ref": None,
    },
    "https://allaboutxrp.com/news/xrpl-fixcleanup3-3-0-majority-activation-window": {
        "resolved_url": "https://allaboutxrp.com/news/xrpl-fixcleanup3-3-0-majority-activation-window",
        "cited_date": "2026-09-18",
        "cited_page": "/amendments",
        "citation_ref": None,
    },
}

# Bare origins we treat as news for the "News referrers" bucket. Includes
# both resolved (see NEWS_DOMAIN_REFERRERS) and known-but-unresolved
# outlets that have sent traffic before. Kept explicit — the classifier
# elsewhere considers anything not in the search/social/AI buckets as
# "direct", which would swallow news. This list is the news-specific
# override so those show up under NEWS instead of DIRECT.
NEWS_DOMAIN_ORIGINS = frozenset({
    "cryptoslate.com",
    "wordupnews.com",
    "amznusa.com",
    "allaboutxrp.com",
    "coindesk.com",
    "cointelegraph.com",
    "theblock.co",
    "decrypt.co",
    "cryptobriefing.com",
    "cryptopolitan.com",
    "coingape.com",
    "cryptonews.net",
    "cryptonews.com",
    "protos.com",
    "crypto.news",
    "beincrypto.com",
    "u.today",
    "watcher.guru",
})


# ── Country / region validators (strict allowlists) ──────────────────

_US_STATE_CODES = frozenset({
    "US-AL","US-AK","US-AZ","US-AR","US-CA","US-CO","US-CT","US-DE",
    "US-FL","US-GA","US-HI","US-ID","US-IL","US-IN","US-IA","US-KS",
    "US-KY","US-LA","US-ME","US-MD","US-MA","US-MI","US-MN","US-MS",
    "US-MO","US-MT","US-NE","US-NV","US-NH","US-NJ","US-NM","US-NY",
    "US-NC","US-ND","US-OH","US-OK","US-OR","US-PA","US-RI","US-SC",
    "US-SD","US-TN","US-TX","US-UT","US-VT","US-VA","US-WA","US-WV",
    "US-WI","US-WY",
})
_US_DC_CODE = "US-DC"
_US_TERRITORY_CODES = frozenset({"US-AS","US-GU","US-MP","US-PR","US-VI"})

# ISO-3166-1 alpha-2 country codes (subset seen on the site — filter
# blocks the T1 pseudo-country and any 2-letter code that's not a
# real country). We do NOT enforce full ISO alpha-2 list here; the
# validator only requires shape [A-Z][A-Z] and blocks the known
# junk codes.
_KNOWN_JUNK_COUNTRIES = frozenset({"T1"})


def _sql_not_bot_ua_clause(alias: str = "p") -> str:
    """Returns a SQL fragment excluding rows whose user_agent matches
    any SELF_PROBE_UA_FRAGMENTS or KNOWN_BOT_UA_FRAGMENTS entry."""
    fragments = SELF_PROBE_UA_FRAGMENTS + KNOWN_BOT_UA_FRAGMENTS
    clauses = " AND ".join(
        f"COALESCE({alias}.user_agent, '') NOT ILIKE '%{f}%'"
        for f in fragments
    )
    return clauses


def _sql_valid_country_clause(alias: str = "p") -> str:
    junk = ", ".join(f"'{c}'" for c in _KNOWN_JUNK_COUNTRIES)
    return (
        f"{alias}.country IS NOT NULL "
        f"AND LENGTH({alias}.country) = 2 "
        f"AND {alias}.country ~ '^[A-Z][A-Z]$' "
        f"AND {alias}.country NOT IN ({junk})"
    )


# ── Query runner ──────────────────────────────────────────────────────
#
# Uses the jj_ro read-only wrapper. NEVER sources the owner env.

_JJ = os.path.expanduser("~/.openclaw/workspace/scripts/jj_query.sh")


def _q(sql: str) -> str:
    return subprocess.check_output([_JJ, "-A", "-t", "-c", sql], text=True)


def _q_rows(sql: str) -> list[list[str]]:
    raw = _q(sql).strip()
    if not raw:
        return []
    return [line.split("|") for line in raw.splitlines()]


# ── Time helpers ──────────────────────────────────────────────────────

def week_bounds(anchor_date: dt.date) -> tuple[str, str]:
    """Return (start_et, end_et) for the ISO week containing anchor_date,
    Monday-to-following-Monday. Both are ISO 8601 timestamptz-parseable
    with 'America/New_York' zone tag appended by callers as needed."""
    monday = anchor_date - dt.timedelta(days=anchor_date.weekday())
    next_mon = monday + dt.timedelta(days=7)
    return (
        f"{monday.isoformat()} 00:00:00 America/New_York",
        f"{next_mon.isoformat()} 00:00:00 America/New_York",
    )


# ── Human count with conservative-range logic ─────────────────────────
#
# On days when is_bot_writer was RUNNING (Mon 09-14, Sat 09-19 in the
# 2026-09-14→19 week), NULL rows minus known-bot UAs is a reasonable
# human estimate. On days when the writer was OFF (outage days), NULL
# includes every bot the writer never got to stamp; we compute an
# UPPER-BOUND (NULL minus known-bot UAs, same filter) and a
# CONSERVATIVE-LOWER-BOUND (baseline extrapolation from writer-on days).
# The weekly total is a RANGE.

class DayEstimate(NamedTuple):
    day_et: str
    writer_on: bool
    naive_null: int
    upper_bound: int      # NULL − known-bot UAs
    baseline_lower: int   # applied writer-on days' baseline


def week_human_estimate(week_start: str, week_end: str,
                        writer_off_days: set[str]) -> list[DayEstimate]:
    bot_filter = _sql_not_bot_ua_clause("p")
    rows = _q_rows(f"""
        SELECT
          to_char(DATE(to_timestamp(ts) AT TIME ZONE 'America/New_York'),
                  'YYYY-MM-DD') AS day_et,
          COUNT(*) FILTER (WHERE is_bot IS NULL) AS naive_null,
          COUNT(*) FILTER (WHERE is_bot IS NULL AND {bot_filter})
            AS upper_bound
        FROM page_views p
        WHERE to_timestamp(ts) >= '{week_start}'
          AND to_timestamp(ts) <  '{week_end}'
        GROUP BY day_et
        ORDER BY day_et
    """)
    days = []
    writer_on_upper_bounds = []
    for row in rows:
        day_et, naive_null, upper_bound = row
        naive_null = int(naive_null)
        upper_bound = int(upper_bound)
        writer_on = day_et not in writer_off_days
        if writer_on:
            writer_on_upper_bounds.append(upper_bound)
        days.append(DayEstimate(day_et, writer_on, naive_null, upper_bound, 0))
    # Baseline lower bound = mean of writer-on-day upper bounds
    baseline = int(round(
        sum(writer_on_upper_bounds) / max(1, len(writer_on_upper_bounds))
    )) if writer_on_upper_bounds else 0
    # Second pass — set baseline_lower for outage days, keep writer-on
    # upper_bound as its own lower bound.
    return [
        DayEstimate(
            d.day_et,
            d.writer_on,
            d.naive_null,
            d.upper_bound,
            d.upper_bound if d.writer_on else baseline,
        )
        for d in days
    ]


def week_range(days: list[DayEstimate]) -> tuple[int, int, int]:
    low = sum(d.baseline_lower for d in days)
    high = sum(d.upper_bound for d in days)
    mid = (low + high) // 2
    return low, mid, high


# ── Country / region tallies (strict allowlist) ──────────────────────

def all_time_country_tallies() -> dict:
    countries_all = int(_q(
        f"SELECT COUNT(DISTINCT country) FROM page_views p "
        f"WHERE {_sql_valid_country_clause()};"
    ).strip())
    countries_human = int(_q(
        f"SELECT COUNT(DISTINCT country) FROM page_views p "
        f"WHERE {_sql_valid_country_clause()} AND is_bot IS NULL;"
    ).strip())
    return {"countries_all": countries_all, "countries_human": countries_human}


def all_time_us_state_split() -> dict:
    """Returns the states / DC / territories split per Charlie's convention
    (2026-09-19 evening ruling)."""
    def seen(codes: frozenset[str], require_human: bool = False) -> int:
        cond = "AND is_bot IS NULL" if require_human else ""
        cs = ", ".join(f"'{c}'" for c in codes)
        return int(_q(
            f"SELECT COUNT(DISTINCT region_code) FROM page_views "
            f"WHERE country='US' AND region_code IN ({cs}) {cond};"
        ).strip())
    states_seen = seen(_US_STATE_CODES)
    states_human = seen(_US_STATE_CODES, True)
    dc_seen = seen(frozenset({_US_DC_CODE}))
    dc_human = seen(frozenset({_US_DC_CODE}), True)
    territories_seen = seen(_US_TERRITORY_CODES)
    territories_human = seen(_US_TERRITORY_CODES, True)
    missing_states = sorted(_US_STATE_CODES - _seen_state_set())
    return {
        "states_seen_of_50": states_seen,
        "states_human_of_50": states_human,
        "states_missing": missing_states,
        "dc_seen": dc_seen == 1,
        "dc_human": dc_human == 1,
        "territories_seen_of_5": territories_seen,
        "territories_human_of_5": territories_human,
    }


def _seen_state_set() -> frozenset[str]:
    rows = _q_rows(
        f"SELECT DISTINCT region_code FROM page_views "
        f"WHERE country='US' AND region_code IS NOT NULL;"
    )
    return frozenset(r[0] for r in rows) & _US_STATE_CODES


def regions_all() -> int:
    return int(_q(
        "SELECT COUNT(DISTINCT region_code) FROM page_views "
        "WHERE region_code IS NOT NULL "
        "AND region_code ~ '^[A-Z]{2}-[A-Z0-9]{1,3}$';"
    ).strip())


# ── 5x-baseline anomaly detector ──────────────────────────────────────

def country_baseline_anomalies(
    week_start: str, week_end: str, baseline_start: str,
    min_ratio: float = 3.0,
) -> list[tuple[str, int, float, float]]:
    """Report countries where this-week 'humans' > `min_ratio` × prior-4-week
    weekly average, KNOWN_BOT_UA fragments already excluded. Returns:
        [(country, this_wk_humans, weekly_baseline_avg, ratio), ...]
    Sorted by ratio DESC. Countries with < 20 this-week hits filtered out.

    Charlie ruling 2026-09-21: threshold lowered from 5× to 3× so the
    three-axis diagnostic (`country_three_axis_check`) fires on any
    surge before it reaches a headline unverified. The 5× row still
    gets a ⚠ flag in the printed table; the 3× floor just widens what
    the diagnostic prints under."""
    bot_filter = _sql_not_bot_ua_clause("p")
    country_filter = _sql_valid_country_clause("p")
    rows = _q_rows(f"""
        WITH by_country AS (
          SELECT p.country,
            COUNT(*) FILTER (WHERE to_timestamp(p.ts) >= '{week_start}'
                             AND to_timestamp(p.ts) < '{week_end}'
                             AND is_bot IS NULL AND {bot_filter}) AS this_wk,
            COUNT(*) FILTER (WHERE to_timestamp(p.ts) >= '{baseline_start}'
                             AND to_timestamp(p.ts) < '{week_start}'
                             AND is_bot IS NULL AND {bot_filter}) AS prior_4wk
          FROM page_views p WHERE {country_filter}
            AND to_timestamp(p.ts) >= '{baseline_start}'
            AND to_timestamp(p.ts) <  '{week_end}'
          GROUP BY p.country
        )
        SELECT country, this_wk, prior_4wk,
          ROUND(prior_4wk::numeric / 4, 1) AS weekly_avg,
          CASE WHEN prior_4wk > 0
               THEN ROUND(this_wk::numeric * 4 / prior_4wk, 2)
               ELSE NULL END AS ratio
        FROM by_country
        WHERE this_wk > 20 AND prior_4wk > 0
        ORDER BY ratio DESC NULLS LAST LIMIT 15;
    """)
    return [
        (r[0], int(r[1]), float(r[3]), float(r[4]))
        for r in rows if r[4] and float(r[4]) >= min_ratio
    ]


# Kept as backward-compat shim so external callers that import
# `country_5x_anomalies` don't break. Same underlying query, threshold
# defaults to 5× to preserve the prior public contract.
def country_5x_anomalies(week_start: str, week_end: str,
                        baseline_start: str) -> list[tuple[str, int, float, float]]:
    return country_baseline_anomalies(
        week_start, week_end, baseline_start, min_ratio=5.0,
    )


def country_three_axis_check(country: str, week_start: str, week_end: str) -> dict:
    """Three-axis diagnostic for a flagged country (Charlie ruling
    2026-09-21). Answers "is this surge real readers?" without touching
    the UA field — the axes are behavioral:

    1. **hash_dominance** — top visitor_hash's share of country hits.
       A rotating-proxy fleet spreads hits across many hashes; a
       single loud client shows up as one hash taking 50-90%. The
       "one loud client" pattern (BR Sunday: 168/201 = 83%) is
       diagnostic all by itself.

    2. **peak_2h_share** — % of this-week hits that fall inside the
       peak 2-hour UTC window. A metronome-polling client (BR: 82%
       of a Sunday inside a 2h burst) tells you the traffic isn't
       distributed like reader browsing.

    3. **top_hash_paths / top_hash_hits** — the top hash's distinct-
       path count and hit count. 168 hits × 1 path = homepage-only
       polling loop; 3 hits × 3 paths = brief real reader.

    Returns a dict {top_hash, top_hash_hits, this_wk_hits,
    top_hash_pct, top_hash_paths, top_hash_top_path, median_hits,
    peak_2h_hits, peak_2h_share}. Empty dict when country isn't seen
    in the window."""
    bot_filter = _sql_not_bot_ua_clause("p")
    country_filter = _sql_valid_country_clause("p")
    country_sql = country.replace("'", "''")
    row = _q_rows(f"""
        WITH wk AS (
          SELECT p.ts, p.visitor_hash, p.path
          FROM page_views p
          WHERE p.country = '{country_sql}'
            AND to_timestamp(p.ts) >= '{week_start}'
            AND to_timestamp(p.ts) <  '{week_end}'
            AND is_bot IS NULL AND {bot_filter} AND {country_filter}
        ),
        per_hash AS (
          SELECT visitor_hash, COUNT(*) AS hits,
                 COUNT(DISTINCT path) AS paths,
                 (array_agg(path ORDER BY ts))[1] AS first_path
          FROM wk GROUP BY 1
        ),
        top_hash AS (
          SELECT visitor_hash, hits, paths, first_path
          FROM per_hash ORDER BY hits DESC LIMIT 1
        ),
        peak AS (
          SELECT MAX(cnt) AS peak_2h FROM (
            SELECT COUNT(*) AS cnt FROM wk
            GROUP BY (ts / 7200)
          ) x
        )
        SELECT
          (SELECT visitor_hash FROM top_hash),
          (SELECT hits FROM top_hash),
          (SELECT paths FROM top_hash),
          (SELECT first_path FROM top_hash),
          (SELECT COUNT(*) FROM wk),
          (SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY hits) FROM per_hash),
          (SELECT peak_2h FROM peak);
    """)
    if not row or not row[0][0]:
        return {}
    r = row[0]
    top_hash = r[0]
    top_hits = int(r[1] or 0)
    top_paths = int(r[2] or 0)
    top_path = r[3] or ""
    this_wk = int(r[4] or 0)
    median = float(r[5] or 0)
    peak_2h = int(r[6] or 0)
    return {
        "top_hash": top_hash[:12] if top_hash else "",
        "top_hash_hits": top_hits,
        "top_hash_paths": top_paths,
        "top_hash_top_path": top_path,
        "this_wk_hits": this_wk,
        "top_hash_pct": round(100.0 * top_hits / this_wk, 1) if this_wk else 0.0,
        "median_hits": median,
        "peak_2h_hits": peak_2h,
        "peak_2h_share": round(100.0 * peak_2h / this_wk, 1) if this_wk else 0.0,
    }


def _format_three_axis(check: dict) -> str:
    """Compact single-line summary for printing under an anomaly row."""
    if not check:
        return "(no data for this country in window)"
    return (
        f"top hash {check['top_hash']}: "
        f"{check['top_hash_hits']}/{check['this_wk_hits']} hits "
        f"({check['top_hash_pct']}%), "
        f"{check['top_hash_paths']} distinct path"
        f"{'s' if check['top_hash_paths'] != 1 else ''} "
        f"({check['top_hash_top_path']}); "
        f"median hits/visitor = {check['median_hits']:.1f}; "
        f"peak 2h window = {check['peak_2h_share']}% of week"
    )


# ── News-referrer resolver ────────────────────────────────────────────

def news_referrals_for_week(start_et: str, end_et: str) -> list[dict]:
    """Return each news-origin referrer seen this week with count and
    resolving article when known.

    Groups by raw referrer value (byte-exact). For each row that matches
    a `NEWS_DOMAIN_REFERRERS` entry, attaches the resolved article; for
    ones that only match a `NEWS_DOMAIN_ORIGINS` domain, returns the
    entry with `resolved_url=None` so we surface unresolved news traffic
    to prompt a follow-up. Excludes bot UAs so this is human-only news
    inbound."""
    domain_ilike = " OR ".join(
        f"p.referrer ILIKE 'https://{d}%'" for d in NEWS_DOMAIN_ORIGINS
    )
    if not domain_ilike:
        return []
    ua_clause = _sql_not_bot_ua_clause("p")
    sql = f"""
        SELECT p.referrer, COUNT(*) AS n
          FROM page_views p
         WHERE p.ts >= EXTRACT(EPOCH FROM TIMESTAMPTZ '{start_et}')
           AND p.ts <  EXTRACT(EPOCH FROM TIMESTAMPTZ '{end_et}')
           AND (p.is_bot IS NULL OR p.is_bot = FALSE)
           AND ({domain_ilike})
           AND {ua_clause}
      GROUP BY 1
      ORDER BY 2 DESC
    """
    rows = _q_rows(sql)
    out = []
    for row in rows:
        if len(row) < 2:
            continue
        referrer, n = row[0], row[1]
        try:
            n = int(n)
        except ValueError:
            continue
        entry = NEWS_DOMAIN_REFERRERS.get(referrer)
        out.append({
            "referrer": referrer,
            "count": n,
            "resolved_url": entry["resolved_url"] if entry else None,
            "cited_date": entry["cited_date"] if entry else None,
            "cited_page": entry["cited_page"] if entry else None,
            "citation_ref": entry["citation_ref"] if entry else None,
        })
    return out


# ── Report builder ────────────────────────────────────────────────────

def build_report(anchor_date: dt.date, writer_off_days: set[str]) -> str:
    start, end = week_bounds(anchor_date)
    baseline_start = (
        (anchor_date - dt.timedelta(days=anchor_date.weekday()) -
         dt.timedelta(weeks=4)).isoformat() + " 00:00:00 America/New_York"
    )
    days = week_human_estimate(start, end, writer_off_days)
    low, mid, high = week_range(days)
    # Charlie ruling 2026-09-21: threshold lowered from 5× to 3× so the
    # three-axis diagnostic prints under every meaningful surge. The 5×
    # tier still gets a ⚠ flag; the 3× floor exposes the middle range
    # (BR Sunday 09-20 was a 5.2× headline that turned out to be one
    # loud client + a quiet Sunday) so no surge reaches print unverified.
    anomalies = country_baseline_anomalies(
        start, end, baseline_start, min_ratio=3.0,
    )
    country_t = all_time_country_tallies()
    state_t = all_time_us_state_split()
    regions_ct = regions_all()
    news_refs = news_referrals_for_week(start, end)

    lines = []
    lines.append(f"# Weekly analytics — week starting {start[:10]}\n")
    lines.append(f"## Human estimate (KNOWN_BOT_UAS excluded)")
    lines.append(f"Writer-off days: {sorted(writer_off_days)}\n")
    lines.append("| day | writer | upper-bound (NULL − bot UAs) | baseline lower |")
    lines.append("|---|---|---|---|")
    for d in days:
        state = "on" if d.writer_on else "OFF"
        lines.append(
            f"| {d.day_et} | {state} | {d.upper_bound} | {d.baseline_lower} |"
        )
    lines.append(f"\n**Weekly humans range: {low:,} – {high:,}** "
                 f"(best point ~{mid:,})\n")

    lines.append(f"## Baseline anomalies (3× floor, ⚠ at 5×) with three-axis check")
    if anomalies:
        lines.append("| country | this_wk | weekly_avg_prior_4wk | ratio |")
        lines.append("|---|---|---|---|")
        for c, tw, avg, ratio in anomalies:
            flag = " ⚠" if ratio >= 5 else ""
            lines.append(f"| {c} | {tw} | {avg} | {ratio}x{flag} |")
            # Three-axis diagnostic (Charlie ruling 2026-09-21): every
            # anomaly row gets a follow-up line stating the top-hash
            # dominance, page-depth, and burst share so a headline
            # like "BR 201, 5.2×" is never printed without the "168 of
            # 201 on / by one hash" counter-evidence right below it.
            check = country_three_axis_check(c, start, end)
            lines.append(f"| ↳ | | | {_format_three_axis(check)} |")
    else:
        lines.append("(none above 3x threshold)")
    lines.append("")

    lines.append(f"## News referrers (this week, human-only)")
    if news_refs:
        lines.append("| referrer | hits | resolved article | cited page |")
        lines.append("|---|---:|---|---|")
        for r in news_refs:
            resolved = r["resolved_url"] or "(unresolved — needs manual link-check)"
            cited_page = r["cited_page"] or "?"
            cited_note = (
                f"{resolved}"
                + (f" · cited {r['cited_date']}" if r["cited_date"] else "")
                + (f" · see {r['citation_ref']}" if r["citation_ref"] else "")
            )
            lines.append(f"| `{r['referrer']}` | {r['count']} | {cited_note} | {cited_page} |")
    else:
        lines.append("(no news-domain referrers this week)")
    lines.append("")

    lines.append(f"## All-time tally (strict allowlist)")
    lines.append(f"- countries_all: {country_t['countries_all']}")
    lines.append(f"- countries_human: {country_t['countries_human']}")
    lines.append(
        f"- **US states seen (of 50)**: {state_t['states_seen_of_50']} — "
        f"missing: {', '.join(state_t['states_missing']) or 'none'}"
    )
    lines.append(f"- **US states with humans (of 50)**: {state_t['states_human_of_50']}")
    lines.append(
        f"- **DC**: {'seen' if state_t['dc_seen'] else 'missing'}"
        f"{' · with human traffic' if state_t['dc_human'] else ''}"
    )
    lines.append(
        f"- **US territories seen (of 5)**: {state_t['territories_seen_of_5']}"
        f"{' · with human traffic: ' + str(state_t['territories_human_of_5']) if state_t['territories_seen_of_5'] else ''}"
    )
    lines.append(f"- regions_all (strict AA-BB): {regions_ct}")
    return "\n".join(lines)


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--anchor-date", default=dt.date.today().isoformat(),
                   help="Any date in the target week (default: today).")
    p.add_argument(
        "--writer-off-days", default="",
        help="Comma-separated YYYY-MM-DD list of days is_bot_writer was OFF "
             "(the outage window for this week). Baseline extrapolation "
             "applies to these days."
    )
    args = p.parse_args()
    anchor = dt.date.fromisoformat(args.anchor_date)
    off = set(d.strip() for d in args.writer_off_days.split(",") if d.strip())
    print(build_report(anchor, off))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
