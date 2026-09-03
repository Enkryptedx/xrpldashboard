"""Sovereign tunnel HTTP client with retry + fail-open public fallback.

Shared by lending_data.py and lending_amendment.py — both fetchers used
by the /lending route go through this so the page has consistent tunnel
behavior across every RPC it makes per visit.

Contract:
  - If XRPL_TUNNEL_NODE + CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET
    are all set: tunnel-first with retry-then-cascade to public.
  - Any one unset: skip the tunnel, use public directly, sourcing flag
    reflects that (fail-open — never crash on missing config).
  - Sticky fallback within one fetch: once a call falls back, all
    subsequent calls in the SAME SovereignFetcher stay on public. A new
    fetch (new SovereignFetcher) re-attempts the tunnel from scratch.
  - Retry recoveries against the tunnel are silent (no walker_node_fallback
    row) — a caught flap is not a failure.
  - Real cascades write ONE walker_node_fallback row per fetch (not per
    call) with the caller-provided walker_name.

Sourcing values surfaced on each SovereignFetcher:
  - "sovereign"                   — tunnel served every call
  - "fallback-public-rpc"         — at least one call fell back
  - "public-no-tunnel-configured" — tunnel env vars unset (local dev)

Callers with multiple SovereignFetcher instances per page (e.g. /lending
runs an amendment status fetch + a data fetch) should aggregate the
sourcing with worse_sourcing() — any single fallback taints the whole
page per the disclosure symmetry rule.
"""

import os
import random
import time

import httpx


# ── Env config ────────────────────────────────────────────────────────
TUNNEL_NODE = (os.environ.get("XRPL_TUNNEL_NODE") or "").strip() or None
_CF_CLIENT_ID = (os.environ.get("CF_ACCESS_CLIENT_ID") or "").strip() or None
_CF_CLIENT_SECRET = (os.environ.get("CF_ACCESS_CLIENT_SECRET") or "").strip() or None
TUNNEL_CONFIGURED = bool(TUNNEL_NODE and _CF_CLIENT_ID and _CF_CLIENT_SECRET)

# Retry knobs — same shape as xrpl_client.py's local retry: 3 attempts
# with 100/200ms jittered backoff. Absorbs transient CF edge / tunnel
# origin hiccups without spilling to public infra.
TUNNEL_RETRY_ATTEMPTS = 3
TUNNEL_RETRY_BASE_MS = 100
TUNNEL_RETRY_JITTER_MS = 100

REQUEST_TIMEOUT_SECONDS = 20.0


# ── Sourcing flag values ──────────────────────────────────────────────
SOURCING_SOVEREIGN = "sovereign"
SOURCING_FALLBACK = "fallback-public-rpc"
SOURCING_NO_TUNNEL = "public-no-tunnel-configured"
# Added 2026-09-03 for /cold-storage + /escrow-supply DB-cache pattern:
# the page reads walker-populated DB rows instead of hitting XRPL live.
# STALE_CACHE fires when the newest row's fetched_at is older than the
# page's staleness threshold (typically 3× the walker cadence, e.g. 45min
# for a 15-min walker). It's DISTINCT from FALLBACK — no live RPC is
# happening, just the cache is old. Ranks between SOVEREIGN (fresh cache)
# and NO_TUNNEL (public RPC directly): worse than sovereign, but better
# than a live public-RPC hit because Ripple's public servers aren't
# touched.
SOURCING_STALE_CACHE = "stale-cache"


# ── Connection pool (module-level, thread-safe) ───────────────────────
_HTTP_LIMITS = httpx.Limits(
    max_keepalive_connections=8,
    max_connections=16,
    keepalive_expiry=30.0,
)
_http_clients: dict = {}


def _client_for(url: str) -> httpx.Client:
    c = _http_clients.get(url)
    if c is None:
        c = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, limits=_HTTP_LIMITS)
        _http_clients[url] = c
    return c


def _tunnel_backoff_seconds(attempt: int) -> float:
    """Backoff between attempts — 100 * (attempt-1) + 0-100ms jitter.
    Only called between attempts (attempt=2 or attempt=3)."""
    base_ms = TUNNEL_RETRY_BASE_MS * (attempt - 1)
    jitter_ms = random.uniform(0, TUNNEL_RETRY_JITTER_MS)
    return (base_ms + jitter_ms) / 1000.0


class SovereignFetcher:
    """Per-fetch RPC client. Not thread-safe — instantiate one per logical
    page fetch (each Flask request creates its own).

    public_url: fallback URL when tunnel unavailable/unconfigured
    walker_name: identifier written to walker_node_fallback on real cascade
    """

    def __init__(self, public_url: str, walker_name: str = "unknown"):
        self.public_url = public_url
        self.walker_name = walker_name
        if TUNNEL_CONFIGURED:
            self.sourcing = SOURCING_SOVEREIGN
            self._tunnel_headers = {
                "CF-Access-Client-Id": _CF_CLIENT_ID,
                "CF-Access-Client-Secret": _CF_CLIENT_SECRET,
                "Content-Type": "application/json",
            }
        else:
            self.sourcing = SOURCING_NO_TUNNEL
            self._tunnel_headers = None

    @property
    def effective_node_url(self) -> str:
        """Best-guess of which URL last served this fetcher — for display
        only. The authoritative signal is .sourcing."""
        if self.sourcing == SOURCING_SOVEREIGN:
            return TUNNEL_NODE or self.public_url
        return self.public_url

    def _try_tunnel(self, payload):
        """TUNNEL_RETRY_ATTEMPTS against the tunnel with jittered backoff.
        Returns (result_dict, None) on success, (None, reason) on total
        failure."""
        last_reason = None
        for attempt in range(1, TUNNEL_RETRY_ATTEMPTS + 1):
            try:
                r = _client_for(TUNNEL_NODE).post(
                    TUNNEL_NODE, json=payload, headers=self._tunnel_headers,
                )
                if r.status_code == 200:
                    return (r.json() or {}).get("result") or {}, None
                # 403 = Access headers rejected; 502 = tunnel origin down;
                # any non-200 is a failure worth cascading after retries.
                last_reason = f"tunnel_http_{r.status_code}"
            except Exception as e:
                last_reason = f"tunnel_unreachable:{type(e).__name__}"
            if attempt < TUNNEL_RETRY_ATTEMPTS:
                time.sleep(_tunnel_backoff_seconds(attempt + 1))
        return None, last_reason

    def _try_public(self, payload):
        """Single attempt to public_url. Returns result dict on success,
        None on transport failure."""
        try:
            r = _client_for(self.public_url).post(self.public_url, json=payload)
            return (r.json() or {}).get("result") or {}
        except Exception:
            return None

    def call(self, method, params):
        """One RPC call. Returns result dict (may be empty) or None on total
        failure. Handles retry, cascade, sourcing state, and fallback
        logging. Callers can assume this never raises."""
        payload = {"method": method, "params": [params]}
        if self.sourcing == SOURCING_SOVEREIGN:
            result, tunnel_fail_reason = self._try_tunnel(payload)
            if result is not None:
                return result
            # Sticky downgrade: log ONE fallback row per fetcher (not per call)
            self.sourcing = SOURCING_FALLBACK
            try:
                import db
                db.write_walker_node_fallback(
                    self.walker_name, tunnel_fail_reason or "tunnel_unknown",
                )
            except Exception:
                # DB write failures must not break the page load
                pass
        return self._try_public(payload)


def worse_sourcing(a: str, b: str) -> str:
    """Aggregate two sourcing values for a page that ran multiple fetches
    (e.g. /lending: amendment check + broker/vault/loan fetch). Any single
    fallback taints the whole page. Precedence (worst first):
      fallback-public-rpc > public-no-tunnel-configured > stale-cache > sovereign
    """
    if SOURCING_FALLBACK in (a, b):
        return SOURCING_FALLBACK
    if SOURCING_NO_TUNNEL in (a, b):
        return SOURCING_NO_TUNNEL
    if SOURCING_STALE_CACHE in (a, b):
        return SOURCING_STALE_CACHE
    return SOURCING_SOVEREIGN
