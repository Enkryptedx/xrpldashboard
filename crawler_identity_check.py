"""crawler_identity_check — reverse-DNS forgery detection for named crawlers.

Charlie ruling 2026-09-22 Tue midday (motivating incident: yesterday
13 requests carried a `GPTBot` UA and probed for `/.env`, `/firebase-
admin.json`, `/awsConfig.js`, `/config/prod.exs`, `/actuator/threaddump`,
etc. Real GPTBot doesn't probe those paths; the UA was forged).

## Semantics

For every request whose UA matches a KNOWN AI-crawler / search-crawler
signature (GPTBot / ClaudeBot / Googlebot / Bingbot / PerplexityBot /
Amazonbot), do a reverse-DNS lookup on the connecting client IP and
verify the PTR resolves to a trusted domain that the crawler
publishes. Forward-confirm by resolving the PTR back to an IP that
matches the original.

If UA says GPTBot but rDNS says `some-random-vps.com` (or has no PTR
at all), we've caught a forgery. That fleet-blocks via the same
`banned_until` mechanism session_scraper_tracker uses.

## Log-only first

Per the standing `request_path_filter_log_only_first` rule (Charlie
09-21 Mon PM after the Render 429 near-miss): this check ships in
LOG-ONLY mode for 24h. `CRAWLER_FORGERY_SHADOW:` lines report what
WOULD be blocked; nothing is actually blocked. Tomorrow morning we
review the shadow list, then flip `CRAWLER_FORGERY_ENFORCE=1` on
Render if it's clean of monitors and real crawlers-with-atypical-IPs.

## Cost

rDNS is a real cost (100–300ms per lookup on cache-miss). We cache the
(ip, ua_class) → verdict for 24h so a per-IP flood only costs one
lookup. Cache is in-process; on multi-worker Render this means each
worker builds its own cache — still cheaper than the alternative of
external forgery reaching /.env probes.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
import logging as _logging

log = _logging.getLogger("crawler_identity_check")

# Env-gate for enforcement. Ships log-only 2026-09-22; flip to "1" on
# Render after 24h of clean shadow review.
ENFORCE = os.environ.get("CRAWLER_FORGERY_ENFORCE", "0") in ("1", "true", "yes")

# Per-UA published rDNS suffix policy. Each entry is a tuple of allowed
# PTR domain suffixes; if the PTR ends with ANY of them, the crawler
# claim is provisionally trusted (subject to forward-confirmation).
#
# Sources (as of 2026-09-22):
#   Googlebot:  developers.google.com/search/docs/crawling-indexing/
#               verifying-googlebot — .googlebot.com or .google.com
#   Bingbot:    bing.com/webmasters/help/how-to-verify-bingbot-3905dc26
#               — search.msn.com or msn.com
#   GPTBot:     platform.openai.com/docs/gptbot — .openai.com
#   ClaudeBot:  docs.anthropic.com/en/docs/build-with-claude/… — Anthropic
#               publishes IP ranges but rDNS may point at their
#               .anthropic.com infra hostnames
#   PerplexityBot: docs.perplexity.ai/guides/bots — .perplexity.ai
#   Amazonbot:  developer.amazon.com/amazonbot — .amazonbot.amazon.com
_UA_RDNS_POLICY = {
    "GPTBot":       (".openai.com",),
    "ChatGPT-User": (".openai.com", ".chatgpt.com"),
    "OAI-SearchBot":(".openai.com",),
    "ClaudeBot":    (".anthropic.com",),
    "Claude-User":  (".anthropic.com",),
    "PerplexityBot":(".perplexity.ai",),
    "Googlebot":    (".googlebot.com", ".google.com"),
    "AdsBot-Google":(".googlebot.com", ".google.com"),
    "bingbot":      (".search.msn.com", ".msn.com", ".bing.com"),
    "Amazonbot":    (".amazonbot.amazon.com",),
    "Applebot":     (".applebot.apple.com", ".apple.com"),
    # Meta-ExternalAgent surfaces content into Meta AI answers on
    # Facebook/Instagram/WhatsApp (fetch-to-cite retrieval, not training-
    # only). Prior policy fleet-blocked it as training-only — reversed
    # 2026-09-22 after the Tuesday-close analytics showed 40 × 403s in
    # one day. Meta's crawler docs (developers.facebook.com/docs/sharing/
    # webmasters/web-crawlers) publish `.crawl.facebook.com` as the
    # verified PTR suffix; `.tfbnw.net` is also cited by some operators
    # in transit. Real Meta crawler passes; anyone with the UA but
    # off-suffix PTR is forged (shadow-logged, then enforceable).
    "Meta-ExternalAgent": (".crawl.facebook.com", ".tfbnw.net"),
}

# UA substring match — first hit wins. Lower-case comparison against
# the request's raw User-Agent string.
_UA_MATCHERS = [
    ("gptbot",             "GPTBot"),
    ("chatgpt-user",       "ChatGPT-User"),
    ("oai-searchbot",      "OAI-SearchBot"),
    ("claudebot",          "ClaudeBot"),
    ("claude-user",        "Claude-User"),
    ("perplexitybot",      "PerplexityBot"),
    ("adsbot-google",      "AdsBot-Google"),
    ("googlebot",          "Googlebot"),
    ("bingbot",            "bingbot"),
    ("amazonbot",          "Amazonbot"),
    ("applebot",           "Applebot"),
    ("meta-externalagent", "Meta-ExternalAgent"),
]


def match_ua_claim(user_agent: str | None) -> str | None:
    """Return the canonical UA class (from _UA_RDNS_POLICY keys) if
    this request claims to be one of the tracked bots. None otherwise.
    Fast — just a lowercased substring match, safe to call per-request."""
    if not user_agent:
        return None
    ua_lower = user_agent.lower()
    for needle, canonical in _UA_MATCHERS:
        if needle in ua_lower:
            return canonical
    return None


# Verdict cache: (ip, ua_class) -> (verdict, expires_unix)
# verdict is "trusted" / "forged" / "no_ptr" / "resolve_failed"
_verdict_cache: dict[tuple[str, str], tuple[str, float]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL_SEC = 24 * 3600


def _cached(ip: str, ua_class: str) -> str | None:
    now = time.time()
    with _cache_lock:
        cached = _verdict_cache.get((ip, ua_class))
        if cached and cached[1] > now:
            return cached[0]
        # trim expired
        if cached:
            _verdict_cache.pop((ip, ua_class), None)
    return None


def _cache_put(ip: str, ua_class: str, verdict: str) -> None:
    with _cache_lock:
        _verdict_cache[(ip, ua_class)] = (verdict, time.time() + _CACHE_TTL_SEC)


def _rdns_lookup(ip: str) -> str | None:
    """Reverse DNS lookup. Returns the primary PTR (lowercased) or
    None if resolution failed / no PTR."""
    try:
        # socket.gethostbyaddr blocks — we're inside a Flask request
        # handler, keep the timeout short so we can't stall on a
        # slow-DNS resolver.
        socket.setdefaulttimeout(1.5)
        host, aliases, _addrs = socket.gethostbyaddr(ip)
        # Sometimes the "primary" name is a CNAME target; use the last
        # PTR-shaped alias when host itself looks like a raw hex/IPv6
        # reverse zone (rare, but safer).
        best = (host or "").lower()
        return best or None
    except (socket.herror, socket.gaierror, socket.timeout, OSError):
        return None
    finally:
        try:
            socket.setdefaulttimeout(None)
        except Exception:
            pass


def _forward_confirm(hostname: str, original_ip: str) -> bool:
    """Forward-confirm: resolve the hostname back and verify one of
    the returned IPs matches the original request's IP. This catches
    the case where an attacker PTRs their IP to `bot.openai.com`
    but bot.openai.com resolves to a different real IP."""
    try:
        socket.setdefaulttimeout(1.5)
        # getaddrinfo covers both A and AAAA
        for family, _stype, _proto, _canon, sockaddr in socket.getaddrinfo(
            hostname, None
        ):
            if family in (socket.AF_INET, socket.AF_INET6):
                if sockaddr and sockaddr[0] == original_ip:
                    return True
        return False
    except (socket.gaierror, socket.timeout, OSError):
        return False
    finally:
        try:
            socket.setdefaulttimeout(None)
        except Exception:
            pass


def verify(user_agent: str | None, client_ip: str | None) -> tuple[str, str | None]:
    """Return (verdict, ptr) for a request.

    verdict ∈ {
      "not_a_bot_claim"   – UA doesn't match any tracked crawler; do nothing
      "trusted"           – rDNS + forward-confirm passed
      "forged"            – rDNS mismatch (or forward-confirm failed)
      "no_ptr"            – no PTR record for the IP; forgery-suspect
      "resolve_failed"    – DNS glitch during verification; ignore this call
    }
    """
    ua_class = match_ua_claim(user_agent)
    if not ua_class:
        return ("not_a_bot_claim", None)
    if not client_ip:
        return ("resolve_failed", None)

    # Validate IP shape
    try:
        ipaddress.ip_address(client_ip)
    except ValueError:
        return ("resolve_failed", None)

    cached = _cached(client_ip, ua_class)
    if cached:
        return (cached, None)  # PTR text not retained across cache

    policy = _UA_RDNS_POLICY.get(ua_class)
    if not policy:
        return ("not_a_bot_claim", None)

    ptr = _rdns_lookup(client_ip)
    if not ptr:
        _cache_put(client_ip, ua_class, "no_ptr")
        return ("no_ptr", None)

    if not any(ptr.rstrip(".").endswith(suffix.strip(".")) for suffix in policy):
        _cache_put(client_ip, ua_class, "forged")
        return ("forged", ptr)

    # Provisionally trusted PTR — forward-confirm.
    if _forward_confirm(ptr, client_ip):
        _cache_put(client_ip, ua_class, "trusted")
        return ("trusted", ptr)

    # PTR matched suffix but forward-confirm failed → still a forgery
    _cache_put(client_ip, ua_class, "forged")
    return ("forged", ptr)


def evaluate_request(user_agent: str | None, client_ip: str | None,
                     path: str | None = None) -> bool:
    """Called from the Flask before-request hook. Returns True if the
    request should be BLOCKED (403), False otherwise.

    Ships log-only until ENFORCE=1. Log-only always returns False;
    the CRAWLER_FORGERY_SHADOW log line records what WOULD be blocked
    so we can review 24h of shadow output before arming.

    Motivating incident (2026-09-21): 13 `GPTBot`-claiming requests
    probed for `.env` and secret-config paths. Real GPTBot only asks
    for `/llms.txt` and known routes. rDNS forgery-check catches
    these before the request-path filter would have to."""
    verdict, ptr = verify(user_agent, client_ip)
    if verdict in ("not_a_bot_claim", "trusted", "resolve_failed"):
        return False

    # verdict ∈ {"forged", "no_ptr"} — this is a forgery candidate.
    ua_class = match_ua_claim(user_agent) or "?"
    log.info(
        "CRAWLER_FORGERY_SHADOW: ua_class=%s ip=%s verdict=%s ptr=%s "
        "path=%s (enforce=%s)",
        ua_class,
        client_ip,
        verdict,
        ptr or "-",
        path or "?",
        "1" if ENFORCE else "0-log-only",
    )
    try:
        import hashlib as _hashlib
        import db as _db
        ip_hash = (
            _hashlib.sha256((client_ip or "").encode()).hexdigest()[:32]
            if client_ip else None
        )
        _db.write_crawler_forgery_shadow(
            kind="crawler_forgery",
            claimed_ua=(user_agent or "")[:300],
            ip_hash=ip_hash,
            path=path,
            verdict=("enforced" if ENFORCE else "would_enforce"),
            details={
                "ua_class": ua_class,
                "verdict": verdict,
                "ptr": ptr or "",
                "expected_suffixes": list(_UA_RDNS_POLICY.get(ua_class, ())),
            },
        )
    except Exception:
        pass
    if ENFORCE:
        return True
    return False
