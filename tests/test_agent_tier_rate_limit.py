"""Day 6 agent-tier rate-limit + fleet-block + AI-crawler audit-header tests.

Guards the design contract from docs/AGENT_TIER_DESIGN.md §Rate limiting +
abuse posture:

  - Anonymous requests to agent-tier surfaces: bucketed limit, 429 +
    Retry-After on breach (never silent throttling)
  - AI-crawler UAs get a higher bucket AND the X-XRPL-Dashboard-Audit-URL
    header on every response (warm-citations touch)
  - Fleet-signature match returns 429 immediately, bypassing bucket
    (same signature that runs on /whales inline block)
  - Non-agent-tier paths (/health, /) are untouched by all three arms

Rate boundaries are tested via env-var overrides that pinch the anon rate
down to a testable count. flask-limiter memory storage persists across
tests within a run, so bucket tests use unique User-Agent strings to
avoid cross-test bucket pollution.
"""
from __future__ import annotations

import os

import pytest


# ── unit: is_ai_crawler ─────────────────────────────────────────────

def test_is_ai_crawler_gptbot():
    from agent_tier_rate_limit import is_ai_crawler
    assert is_ai_crawler("Mozilla/5.0 (compatible; GPTBot/1.2; +https://openai.com/gptbot)")


def test_is_ai_crawler_claudebot_case_insensitive():
    from agent_tier_rate_limit import is_ai_crawler
    assert is_ai_crawler("CLAUDEBOT/1.0")
    assert is_ai_crawler("claudebot/1.0")
    assert is_ai_crawler("Mozilla/5.0 (compatible; ClaudeBot/1.0)")


def test_is_ai_crawler_perplexity():
    from agent_tier_rate_limit import is_ai_crawler
    assert is_ai_crawler("Mozilla/5.0 (compatible; PerplexityBot/1.0)")


def test_is_ai_crawler_google_extended():
    from agent_tier_rate_limit import is_ai_crawler
    assert is_ai_crawler("Mozilla/5.0 Google-Extended")


def test_is_ai_crawler_regular_chrome_false():
    from agent_tier_rate_limit import is_ai_crawler
    assert not is_ai_crawler(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    )


def test_is_ai_crawler_empty_or_none():
    from agent_tier_rate_limit import is_ai_crawler
    assert not is_ai_crawler("")
    assert not is_ai_crawler(None)


# ── unit: is_agent_tier_route ───────────────────────────────────────

def test_is_agent_tier_route_exact_matches():
    from agent_tier_rate_limit import is_agent_tier_route
    for path in [
        "/llms.txt",
        "/.well-known/agents.json",
        "/.well-known/security.txt",
        "/robots.txt",
        "/sitemap.xml",
        "/.well-known/snapshots/chain.json",
        "/.well-known/snapshots/pubkey.pem",
        "/openapi.json",
        "/docs",
    ]:
        assert is_agent_tier_route(path), path


def test_is_agent_tier_route_snapshot_date_prefix():
    from agent_tier_rate_limit import is_agent_tier_route
    assert is_agent_tier_route("/.well-known/snapshots/2026-07-31.json")


def test_is_agent_tier_route_human_pages_false():
    from agent_tier_rate_limit import is_agent_tier_route
    for path in ["/", "/whales", "/rlusd", "/methodology", "/coverage", "/analytics"]:
        assert not is_agent_tier_route(path), path


def test_is_agent_tier_route_empty_path_false():
    from agent_tier_rate_limit import is_agent_tier_route
    assert not is_agent_tier_route("")


# ── unit: fleet_signature ───────────────────────────────────────────

class _FakeReq:
    def __init__(self, headers):
        self.headers = headers


def test_fleet_signature_il_chrome_142_matches():
    from agent_tier_rate_limit import fleet_signature
    req = _FakeReq({
        "CF-IPCountry": "IL",
        "User-Agent": "Mozilla/5.0 Chrome/142.0.0.0 Safari/537.36",
    })
    assert fleet_signature(req) == "IL_Chrome142_residential_2026_07"


def test_fleet_signature_us_chrome_142_no_match():
    from agent_tier_rate_limit import fleet_signature
    req = _FakeReq({
        "CF-IPCountry": "US",
        "User-Agent": "Mozilla/5.0 Chrome/142.0.0.0 Safari/537.36",
    })
    assert fleet_signature(req) is None


def test_fleet_signature_il_chrome_141_no_match():
    from agent_tier_rate_limit import fleet_signature
    req = _FakeReq({
        "CF-IPCountry": "IL",
        "User-Agent": "Mozilla/5.0 Chrome/141.0.0.0 Safari/537.36",
    })
    assert fleet_signature(req) is None


def test_fleet_signature_empty_headers_no_match():
    from agent_tier_rate_limit import fleet_signature
    req = _FakeReq({})
    assert fleet_signature(req) is None


# ── integration: audit-URL header ───────────────────────────────────

def test_ai_crawler_response_has_audit_url_header(client):
    """AI-crawler UAs get X-XRPL-Dashboard-Audit-URL on agent-tier
    routes — the warm-citations touch."""
    r = client.get(
        "/llms.txt",
        headers={"User-Agent": "GPTBot/1.2 (+https://openai.com/gptbot)"},
    )
    assert r.status_code == 200
    from agent_tier_rate_limit import AUDIT_URL_HEADER_NAME
    assert AUDIT_URL_HEADER_NAME in r.headers
    assert r.headers[AUDIT_URL_HEADER_NAME].endswith("/coverage")


def test_anonymous_response_no_audit_url_header(client):
    """Anonymous requests don't get the audit-URL header — no
    citation-graph value to expose, per design doc."""
    r = client.get(
        "/llms.txt",
        headers={"User-Agent": "curl/8.4.0"},
    )
    assert r.status_code == 200
    from agent_tier_rate_limit import AUDIT_URL_HEADER_NAME
    assert AUDIT_URL_HEADER_NAME not in r.headers


def test_human_page_no_audit_url_header_even_for_ai_crawler(client):
    """Non-agent-tier routes never get the audit-URL header, even for
    an AI-crawler UA. The header is a machine-consumption breadcrumb;
    HTML pages have inline audit surfaces."""
    r = client.get(
        "/",
        headers={"User-Agent": "ClaudeBot/1.0"},
    )
    from agent_tier_rate_limit import AUDIT_URL_HEADER_NAME
    assert AUDIT_URL_HEADER_NAME not in r.headers


# ── integration: fleet-block ────────────────────────────────────────

def test_fleet_signature_returns_429_on_agent_tier_route(client):
    """IL + Chrome/142 fingerprint on /llms.txt → 429 + Retry-After
    + X-Fleet-Signature label. Founding /whales fleet signature
    extended to the agent-tier surface."""
    r = client.get(
        "/llms.txt",
        headers={
            "CF-IPCountry": "IL",
            "User-Agent": "Mozilla/5.0 Chrome/142.0.0.0 Safari/537.36",
        },
    )
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "86400"
    assert r.headers.get("X-Fleet-Signature") == "IL_Chrome142_residential_2026_07"


def test_fleet_signature_not_applied_to_human_pages(client):
    """Fleet block only guards agent-tier routes. The /whales inline
    block still exists (unchanged), but the homepage is untouched by
    the agent-tier hook — verifying isolation."""
    r = client.get(
        "/",
        headers={
            "CF-IPCountry": "IL",
            "User-Agent": "Mozilla/5.0 Chrome/142.0.0.0 Safari/537.36",
        },
    )
    # Homepage may return 200 or a redirect; it must NOT be 429 from
    # the agent-tier fleet block. (The /whales route has its own
    # inline block; we're not testing that path here.)
    assert r.status_code != 429


# ── integration: rate-limit boundary ────────────────────────────────

def test_agent_tier_429_includes_retry_after(client, monkeypatch):
    """Boundary behavior: pinch the anon rate down and blow past it.
    Response must be 429 with Retry-After (per design doc — "never
    silent throttling")."""
    from app import limiter
    limiter.reset()
    monkeypatch.setenv("AGENT_TIER_ANON_RATE", "2 per minute")

    ua = "curl/rate-test-anon-boundary/8.4.0"
    # First 2 should pass under the 2/min bucket.
    for i in range(2):
        r = client.get("/llms.txt", headers={"User-Agent": ua})
        assert r.status_code == 200, f"request {i} unexpectedly {r.status_code}"

    # The 3rd should breach.
    r = client.get("/llms.txt", headers={"User-Agent": ua})
    assert r.status_code == 429
    assert "Retry-After" in r.headers, "flask-limiter must set Retry-After on 429"

    limiter.reset()


def test_ai_crawler_gets_higher_bucket(client, monkeypatch):
    """Same bucket count for the AI crawler UA should NOT breach
    when the anon rate is pinched — verifies the two-tier bucket
    routing works. Anon rate = 1/min; AI rate = 100/min; fire 3
    requests as the AI crawler → all pass."""
    from app import limiter
    limiter.reset()
    monkeypatch.setenv("AGENT_TIER_ANON_RATE", "1 per minute")
    monkeypatch.setenv("AGENT_TIER_AI_RATE", "100 per minute")

    ua = "GPTBot/rate-test-ai-bucket/1.2"
    for i in range(3):
        r = client.get("/llms.txt", headers={"User-Agent": ua})
        assert r.status_code == 200, f"AI-crawler request {i} got {r.status_code}"

    limiter.reset()


def test_fleet_block_bypasses_rate_limit_bucket(client, monkeypatch):
    """Fleet signature match is a soft-block; it returns 429 without
    consuming the rate-limit bucket. Ordering matters — fleet check
    fires before limiter check."""
    from app import limiter
    limiter.reset()
    monkeypatch.setenv("AGENT_TIER_ANON_RATE", "100 per minute")

    r = client.get(
        "/llms.txt",
        headers={
            "CF-IPCountry": "IL",
            "User-Agent": "Mozilla/5.0 Chrome/142.0.0.0 Safari/537.36",
        },
    )
    assert r.status_code == 429
    # The signature header proves the block came from the fleet check,
    # not the rate limiter.
    assert r.headers.get("X-Fleet-Signature") == "IL_Chrome142_residential_2026_07"

    limiter.reset()


# ── contract: agents.json rate-limit copy mentions the enforcement ──

def test_agents_json_rate_limit_copy_matches_module(client):
    """agents.json declares '60 requests/minute/IP' and '300
    requests/minute'. If we ever change the defaults in
    agent_tier_rate_limit.agent_tier_limit_rate, we must also update
    the agents.json copy — this test guards the two staying in sync."""
    aj = client.get("/.well-known/agents.json").get_json()
    anon_copy = aj["rate_limits"]["anonymous"]
    ai_copy = aj["rate_limits"]["identified_ai_crawler"]
    # Defaults come from the module's default-rate strings.
    import agent_tier_rate_limit as atrl
    # Read via the callable's default-branch by clearing overrides.
    saved_anon = os.environ.pop("AGENT_TIER_ANON_RATE", None)
    saved_ai = os.environ.pop("AGENT_TIER_AI_RATE", None)
    try:
        # Compute the defaults through the module (source of truth).
        with client.application.test_request_context(
            "/", headers={"User-Agent": "curl/8.4.0"}
        ):
            default_anon = atrl.agent_tier_limit_rate()
        with client.application.test_request_context(
            "/", headers={"User-Agent": "GPTBot/1.2"}
        ):
            default_ai = atrl.agent_tier_limit_rate()
    finally:
        if saved_anon is not None:
            os.environ["AGENT_TIER_ANON_RATE"] = saved_anon
        if saved_ai is not None:
            os.environ["AGENT_TIER_AI_RATE"] = saved_ai
    assert "60" in anon_copy and "60" in default_anon, (
        f"anon copy {anon_copy!r} vs module default {default_anon!r}"
    )
    assert "300" in ai_copy and "300" in default_ai, (
        f"AI copy {ai_copy!r} vs module default {default_ai!r}"
    )
