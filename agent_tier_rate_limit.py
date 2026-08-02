"""Day 6 agent-tier rate limiting + fleet-block extension + AI-crawler
audit-URL header. Per docs/AGENT_TIER_DESIGN.md §Rate limiting + abuse
posture.

Shape:
- Anonymous / unidentified requests to agent-tier routes: 60/minute/IP
- Identified AI-crawler UAs (fetch-to-cite class per feedback_three_
  audience_rule): 300/minute/IP — warm citations, cold to scrapers
- 429 responses carry Retry-After; never silent throttling
- AI-crawler responses on agent-tier routes get X-XRPL-Dashboard-Audit-URL
  pointing at /coverage — the doc's "warm citations" touch
- Fleet signature detection extended from the proven /whales inline
  block (IL + Chrome/142 residential proxy 2026-07). Same signature
  applied to agent-tier routes so the fleet can't rotate to citing our
  spec instead of scraping /whales

The 3-audience rule is normative (feedback_three_audience_rule):
1. Humans: never friction — no agent-tier route is on the human path,
   so this module never touches them.
2. AI retrieval crawlers: never blocked, always identified — the
   `AI_CRAWLER_UA_SUBSTRINGS` list is a positive allowlist for
   fetch-to-cite classifiers. Additions require explicit review because
   the list is a public claim (surfaced in agents.json rate_limits copy).
3. Scraper fleets: cost-defended — fleet signature check applies before
   the rate-limit bucket, so a rotating residential fleet on Chrome/142
   is soft-blocked even at request 1 of its per-IP burst.

Route scope: `AGENT_TIER_ROUTE_PATHS` names the exact paths this module
guards. Kept explicit rather than pattern-matched — a new discovery
surface is a review-worthy decision, not an automatic broadening.

Test strategy: unit-test the classification functions in isolation
(no HTTP), integration-test the 429 shape via monkeypatched env-var
rates. Rate-limit bucket persists across tests via flask-limiter's
memory storage — tests that exercise the boundary use unique
User-Agent strings to isolate buckets.
"""
from __future__ import annotations

import os
from typing import Optional

from flask import request


# ── AI-crawler classification (positive allowlist) ──────────────────
#
# The three-audience rule: AI retrieval crawlers (fetch-to-cite) are
# ALWAYS identified and get the higher rate. AI training-only crawlers
# with no citation path are handled by the pre-existing _BLOCKED_UA
# _FRAGMENTS list in app.py (fast-path 403). This module is only about
# the citation class.
#
# Substring match is case-insensitive. Each entry MUST be:
# - Documented publicly on the crawler's operator page (link in comment)
# - Unambiguously a citation crawler, not a generic scraper masquerading
# - Present in Cloudflare's verified-bot inventory OR self-documented
#
# Adding an entry here is a claim (declared in agents.json rate_limits
# copy). Removing an entry is also a claim — do not silently trim.
AI_CRAWLER_UA_SUBSTRINGS = (
    # OpenAI (retrieval/citation, distinct from GPTBot-training-only)
    "gptbot",
    "oai-searchbot",
    "chatgpt-user",
    # Anthropic
    "claudebot",
    "claude-web",
    "claude-searchbot",
    # Perplexity
    "perplexitybot",
    "perplexity-user",
    # Google (retrieval; the Extended token distinguishes AI-training opt-in)
    "google-extended",
    "googleother",
    # Apple Intelligence (retrieval; Applebot proper is search-index legacy)
    "applebot-extended",
    # Meta AI retrieval (distinct from meta-externalagent which is
    # training-only and blocked in app.py's _BLOCKED_UA_FRAGMENTS)
    "meta-searchbot",
    # ByteDance / TikTok search
    "bytespider",
    # Common Crawl (research corpus; documented, non-fleet)
    "ccbot",
    # You.com
    "youbot",
)


AUDIT_URL_HEADER_NAME = "X-XRPL-Dashboard-Audit-URL"
AUDIT_URL_PATH = "/coverage"


# ── Route scope (explicit, review-required) ─────────────────────────
#
# Only these paths are guarded by the agent-tier limit + audit-header +
# fleet-block. Human-facing pages have their own limiter decorators or
# no limit at all (three-audience rule call #1).
AGENT_TIER_ROUTE_PATHS = frozenset({
    "/llms.txt",
    "/.well-known/agents.json",
    "/.well-known/security.txt",
    "/robots.txt",
    "/sitemap.xml",
    "/.well-known/snapshots/chain.json",
    "/.well-known/snapshots/pubkey.pem",
    "/openapi.json",
    "/docs",
})

# Prefix-matched routes with dynamic segments (per-date snapshots).
AGENT_TIER_ROUTE_PREFIXES = (
    "/.well-known/snapshots/",  # covers /.well-known/snapshots/{date}.json
)


def is_agent_tier_route(path: str) -> bool:
    """True if `path` is one of the machine-facing agent-tier surfaces
    guarded by this module. Anything else falls through to app.py's
    existing per-route limits and skips both the audit-URL header and
    the fleet-block."""
    if not path:
        return False
    if path in AGENT_TIER_ROUTE_PATHS:
        return True
    for prefix in AGENT_TIER_ROUTE_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def is_ai_crawler(user_agent: Optional[str]) -> bool:
    """True if the User-Agent matches a known citation crawler.
    Case-insensitive substring match; empty/None UA returns False."""
    if not user_agent:
        return False
    ua_lower = user_agent.lower()
    for fragment in AI_CRAWLER_UA_SUBSTRINGS:
        if fragment in ua_lower:
            return True
    return False


# ── Fleet signature (extension of /whales inline block) ─────────────
#
# Founding case: 2026-07-22, 2,036 unique IPs from IL geo hitting /whales
# with a single Chrome/142 UA string, 3am-7am IDT cron. Fleet-block first-
# night result 2026-07-23: BLOCKED with no adaptation; IL /whales collapsed
# 210 → 30 requests. Same signature applied here so the fleet cannot
# rotate from /whales to citing our OpenAPI spec (a competitor-intel
# scraping path we don't want to subsidize).
#
# Returning a label (not just bool) so instrumentation can distinguish
# multiple future fleets and so the whales_cache_daily.blocked counter
# gains a signature dimension in the follow-up.
FLEET_BLOCK_RETRY_AFTER_SECONDS = 86400  # 24h — well-behaved compliance signal


def fleet_signature(req=None) -> Optional[str]:
    """Return the fleet-signature label if the current request matches
    a known scraper-fleet fingerprint, else None.

    Signatures (add here on new-fleet observation, one per line):
    - IL_Chrome142_residential_2026_07: IL geo + Chrome/142 UA, first
      seen 2026-07-22 hitting /whales via rotating residential proxy.
    """
    r = req if req is not None else request
    country = r.headers.get("CF-IPCountry", "")
    ua = r.headers.get("User-Agent", "")
    if country == "IL" and "Chrome/142" in ua:
        return "IL_Chrome142_residential_2026_07"
    return None


# ── Dynamic rate for flask-limiter ──────────────────────────────────
#
# flask-limiter accepts a callable in @limiter.limit(...); the callable
# is invoked in request context so `flask.request` works. Env-var
# overrides let tests exercise the boundary with a tight rate.

def agent_tier_limit_rate() -> str:
    """Return the rate-limit string for the current request. AI crawlers
    get AGENT_TIER_AI_RATE (default 300/min); everything else (including
    unidentified requests and fleet-signature matches, which are handled
    upstream by the fleet-block hook) gets AGENT_TIER_ANON_RATE (60/min).

    Tests override via env vars to exercise the boundary."""
    ua = request.headers.get("User-Agent", "")
    if is_ai_crawler(ua):
        return os.environ.get("AGENT_TIER_AI_RATE", "300 per minute")
    return os.environ.get("AGENT_TIER_ANON_RATE", "60 per minute")
