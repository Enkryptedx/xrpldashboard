"""Shared public-analytics filter constants + SQL fragments.

Charlie ruling 2026-09-20 (evening): /analytics and scripts/
weekly_analytics.py must read from the same functions and allow-lists.
Prior state — /analytics counted googlebot as human (398 hits/7d) while
the morning report excluded it (~14/day); /analytics reported 147
countries while weekly_analytics reported 176. Divergence bred
inconsistent public numbers.

This module is the ONE DEFINITION:
- SELF_PROBE_UA_FRAGMENTS — canaries, walkers, JJ's shell — excluded
  from every public number.
- KNOWN_BOT_UA_FRAGMENTS — bots that self-declare (googlebot,
  bingbot, chatgpt-user, claudebot, …) or that the is_bot_writer may
  not have caught — excluded from every public number.
- _KNOWN_JUNK_COUNTRIES — T1 (Tor pseudo-country) and any other 2-letter
  codes that geoip resolves to but that don't represent a real
  sovereign — excluded from country counts.

Both weekly_analytics.py and db.py's public-analytics reader functions
import from here. Editing any list is a one-line diff that propagates
to /analytics AND the weekly report AND the standing-orders daily
report.
"""
from __future__ import annotations


SELF_PROBE_UA_FRAGMENTS = (
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
    # ── Charlie ruling 2026-09-23 Wed 07:06 ET: tests that hit prod
    # ── endpoints (via `client.get(...)` in the test suite) pollute
    # ── production analytics + shadow-log rows. Tests already tag
    # ── their UAs with these markers (see
    # ── tests/test_agent_tier_rate_limit.py: `curl/rate-test-*`,
    # ── `GPTBot/rate-test-*`). Exclude those from every public count.
    "rate-test",              # rate-limit boundary tests
    "smoke-test",             # smoke-test writes (shadow-log wiring, etc.)
    "integration-test",       # integration-test scaffolding
)


KNOWN_BOT_UA_FRAGMENTS = (
    # Search / crawl / SEO — declared, sometimes not stamped
    "PetalBot",               # Huawei
    "Bytespider",             # ByteDance
    "Amazonbot",              # Amazon Alexa's crawl
    "SemrushBot",
    "Applebot",
    "AhrefsBot",
    "MJ12bot",
    "DotBot",
    "YandexBot",
    "Baiduspider",
    "Googlebot",              # Google (both the classic crawler AND the mobile one)
    "GoogleOther",            # Google's non-search crawlers
    "Google-Extended",        # Google AI training opt-in
    "Bingbot",                # Microsoft Bing
    # AI-citation crawlers
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
    "AionBot",                # declared, paces under limiter
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


_KNOWN_JUNK_COUNTRIES = frozenset({"T1"})


def sql_not_bot_ua_clause(alias: str = "p", psycopg_escape: bool = False) -> str:
    """Return an AND-joined SQL clause that excludes rows whose
    `user_agent` matches any SELF_PROBE_UA_FRAGMENTS or
    KNOWN_BOT_UA_FRAGMENTS entry. Use in WHERE clauses on the
    page_views table.

    Callers must have full control of the alias — DO NOT pass a
    user-supplied string here; the fragments themselves are hard-coded
    and safe.

    `psycopg_escape`: when True, doubles the ILIKE `%` wildcards to
    `%%` so psycopg's parameter-string parser doesn't interpret them
    as placeholders. Set True when this fragment is spliced into a
    query executed with cursor.execute(sql, params). Default False for
    raw-SQL callers (e.g. subprocess psql, or SQL rendered offline)."""
    fragments = SELF_PROBE_UA_FRAGMENTS + KNOWN_BOT_UA_FRAGMENTS
    wc = "%%" if psycopg_escape else "%"
    return " AND ".join(
        f"COALESCE({alias}.user_agent, '') NOT ILIKE '{wc}{f}{wc}'"
        for f in fragments
    )


def sql_not_self_probe_ua_clause(alias: str = "p", psycopg_escape: bool = False) -> str:
    """Return an AND-joined SQL clause that excludes rows whose
    `user_agent` matches any SELF_PROBE_UA_FRAGMENTS entry — but NOT
    the declared-bot list.

    Use for kind='all' analytics where we want humans+declared-bots
    combined but not our own canaries/walkers. Charlie ruling
    2026-09-21: /analytics's "countries (all)" must exclude self-probes
    so we don't inflate the count with our own infra.

    Same `psycopg_escape` semantics as sql_not_bot_ua_clause."""
    wc = "%%" if psycopg_escape else "%"
    return " AND ".join(
        f"COALESCE({alias}.user_agent, '') NOT ILIKE '{wc}{f}{wc}'"
        for f in SELF_PROBE_UA_FRAGMENTS
    )


def sql_valid_country_clause(alias: str = "p") -> str:
    """Return a SQL fragment that keeps only rows whose `country`
    field is a real ISO-3166-1 alpha-2 code (two uppercase letters,
    not T1)."""
    junk = ", ".join(f"'{c}'" for c in _KNOWN_JUNK_COUNTRIES)
    return (
        f"{alias}.country IS NOT NULL "
        f"AND LENGTH({alias}.country) = 2 "
        f"AND {alias}.country ~ '^[A-Z][A-Z]$' "
        f"AND {alias}.country NOT IN ({junk})"
    )
