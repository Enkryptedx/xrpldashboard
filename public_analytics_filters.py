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
- SELF_PROBE_PATHS / SELF_PROBE_PATH_PREFIXES — /health and every
  monitor/canary path (Charlie ruling 2026-09-25) — excluded from every
  public number regardless of UA, stamped is_bot=TRUE at ingest via
  is_self_probe(), and folded into db.BOT_PATH_PATTERNS for the writer.
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


# ── Self-probe PATHS (Charlie ruling 2026-09-25 18:39 ET) ────────────
# /health and every monitor/canary path are part of the ONE self-probe
# definition, alongside the UA fragments above. Rationale: uptime monitors
# and health checkers that present a browser UA (yesterday: 12 hits from
# 11 distinct visitor hashes, identical Mac-Chrome UA, no referrer, all on
# /health) slip past the UA list and inflate the human count. A hit on a
# monitor path is infrastructure traffic regardless of UA.
#   SELF_PROBE_PATHS          — exact matches (request.path, no query string)
#   SELF_PROBE_PATH_PREFIXES  — prefix matches
# /healthz is already skipped at ingest (app._PAGEVIEW_SKIP_PREFIXES); it is
# listed here so the SQL side agrees with the ingest side if that ever
# changes. Consumers: sql_not_bot_ua_clause + sql_not_self_probe_ua_clause
# (weekly_analytics + every db.py public-analytics reader), is_self_probe()
# (ingest-time stamp in app.py), and db.BOT_PATH_PATTERNS (is_bot_writer).
SELF_PROBE_PATHS = (
    "/health",
    "/healthz",
)
SELF_PROBE_PATH_PREFIXES = (
    "/health/",
    "/healthz/",
    "/api/health",
    "/_status",
    "/ping",
)


def is_self_probe_path(path) -> bool:
    """True when `path` (request.path, no query string) is a monitor/canary
    path per SELF_PROBE_PATHS / SELF_PROBE_PATH_PREFIXES."""
    p = (path or "").split("?", 1)[0]
    if p in SELF_PROBE_PATHS:
        return True
    return any(p.startswith(pre) for pre in SELF_PROBE_PATH_PREFIXES)


def is_self_probe_ua(user_agent) -> bool:
    """True when the UA carries any SELF_PROBE_UA_FRAGMENTS entry
    (case-insensitive substring, mirroring the SQL ILIKE)."""
    ua = (user_agent or "").lower()
    return any(f.lower() in ua for f in SELF_PROBE_UA_FRAGMENTS)


def is_self_probe(user_agent, path) -> bool:
    """THE python-side self-probe predicate: UA fragment OR monitor path.
    Used at ingest (app.py) to stamp is_bot=TRUE on our own traffic the
    moment it lands, so the row never counts as human even before the
    is_bot_writer pass."""
    return is_self_probe_ua(user_agent) or is_self_probe_path(path)


def sql_not_self_probe_path_clause(alias: str = "p") -> str:
    """AND-joined SQL fragment excluding rows on monitor/canary paths.
    No wildcards in the exact list, so no psycopg escaping needed for
    those; prefixes use LIKE with a trailing % — callers that splice into
    cursor.execute(sql, params) pass psycopg_escape via the wrappers."""
    exact = ", ".join(f"'{p}'" for p in SELF_PROBE_PATHS)
    parts = [f"{alias}.path NOT IN ({exact})"]
    parts += [f"{alias}.path NOT LIKE '{pre}%'" for pre in SELF_PROBE_PATH_PREFIXES]
    return " AND ".join(parts)


def _path_clause(alias: str, psycopg_escape: bool) -> str:
    c = sql_not_self_probe_path_clause(alias)
    return c.replace("%", "%%") if psycopg_escape else c


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
    ua = " AND ".join(
        f"COALESCE({alias}.user_agent, '') NOT ILIKE '{wc}{f}{wc}'"
        for f in fragments
    )
    # 2026-09-25: monitor/canary PATHS are part of the same definition.
    return ua + " AND " + _path_clause(alias, psycopg_escape)


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
    ua = " AND ".join(
        f"COALESCE({alias}.user_agent, '') NOT ILIKE '{wc}{f}{wc}'"
        for f in SELF_PROBE_UA_FRAGMENTS
    )
    # 2026-09-25: monitor/canary PATHS are part of the same definition.
    return ua + " AND " + _path_clause(alias, psycopg_escape)


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
