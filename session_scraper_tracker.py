"""Session-scoped scraper block (Charlie ruling 2026-09-21, Mon PM item 5).

Behavioral signature: one visitor_hash, ≥100 hits, ≤2 distinct paths,
within a 2-hour window → 429 with Retry-After for that hash for 24h.

False-positive guard: any visitor_hash that has fetched a static asset
(CSS/JS/font/image) in the same window is exempt — real browsers fetch
those; scrapers typically don't. If a scraper starts fetching /static/*
to evade the block, they've paid the roundtrip cost for it, which is
already a win.

Implementation: in-memory per-process. No Redis, no PG round-trip on
the hot path. Rolls per-hash counters through a fixed 2h sliding
window and stamps a 24h ban when the counters trip.

Not perfect: multi-worker Flask means each worker has its own view.
The tracker is per-worker; a scraper fanning out across workers may
accumulate hits without any single worker seeing enough to trip the
threshold. In practice Render runs a small number of workers so a
scraper polling one URL at ~1 req/second still trips one worker's
threshold within ~2 minutes. Long-term evolution: mirror to Redis
so all workers share state.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from typing import Optional


# Tunables — env-overridable for tests + operator adjustment.
WINDOW_SECONDS = int(os.environ.get("SCRAPER_TRACKER_WINDOW_S", "7200"))  # 2h
HIT_THRESHOLD = int(os.environ.get("SCRAPER_TRACKER_HIT_THRESHOLD", "100"))
PATH_THRESHOLD = int(os.environ.get("SCRAPER_TRACKER_PATH_THRESHOLD", "2"))
BAN_SECONDS = int(os.environ.get("SCRAPER_TRACKER_BAN_S", "86400"))  # 24h

# Log-only mode — Charlie ruling 2026-09-21: every request-path filter
# ships in log-only for 24h first. Report what it would have blocked;
# do not actually block. Flip this env var to arm the filter after the
# shadow period confirms the shape.
#
# For the session-scraper tracker: this filter has ALREADY landed
# enforcing (shipped 6c4b7fe → live-verified → 15:18 ET Render alert
# from the /healthz false-positive → exemption fixed in 255ee84 →
# still enforcing). Charlie's new rule applies to the NEXT filter
# (disguised-Chrome fleet block, deferred). We keep the env var here
# so any future filter can inherit the same pattern by importing
# `LOG_ONLY_MODE` and short-circuiting the block emission behind it
# — but for THIS filter the default stays enforcing (log_only=False).
LOG_ONLY_MODE = os.environ.get("SCRAPER_TRACKER_LOG_ONLY", "0") in ("1", "true", "yes")

_STATIC_PREFIXES = (
    "/static/", "/assets/", "/favicon", "/apple-touch-icon",
    "/robots.txt", "/sitemap", "/lang/",
)
_STATIC_SUFFIXES = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif",
                    ".svg", ".ico", ".woff", ".woff2", ".ttf", ".map")

# Monitor / healthcheck paths — never trip the tracker regardless of
# hit count. These are polled continuously by Render's HTTP health
# check + BetterStack + our own canaries; they hit at a metronome
# cadence on ONE path from a stable source-hash, which is exactly
# the shape the tracker was designed to catch. Charlie triage
# 2026-09-21 (Mon PM, session-tracker false-positive on Render's
# healthcheck: shipped 6c4b7fe → Render alert @ 15:18 ET → this fix).
_MONITOR_EXEMPT_PATHS = (
    "/healthz",
    "/api/health",
    "/health",
    "/.well-known/security.txt",
    "/robots.txt",  # also static-classified above; belt-and-suspenders
    "/api/xrp-usd",  # our own chip context poller
    "/analytics/live",  # 15s poll from /analytics JS interval
    "/status",
)

# UA fragments that identify known monitor/probe clients. Same
# rationale as the path exemption: they poll rapidly on stable paths
# and would trip the tracker. All entries are lowercased at compare
# time.
_MONITOR_EXEMPT_UA_FRAGMENTS = (
    "go-http-client",       # Render's HTTP health check UA
    "renderhealth",
    "render-",
    "betterstack",          # BetterStack Uptime monitor
    "uptimerobot",
    "pingdom",
    "kube-probe",
    "elb-healthchecker",
    "aws-elb-",
    "google-cloud-scheduler",
    "cloudfront-healthcheck",
    "xrpldashboard-",       # every internal canary / walker HTTP client
    "public-route-canary",
    "openclaw-",
)


def _is_monitor_probe(path: str, user_agent: str) -> bool:
    if not path:
        return False
    for p in _MONITOR_EXEMPT_PATHS:
        if path == p or path.startswith(p + "?") or path.startswith(p + "/"):
            return True
    ua = (user_agent or "").lower()
    for fragment in _MONITOR_EXEMPT_UA_FRAGMENTS:
        if fragment in ua:
            return True
    return False


def _is_static_path(path: str) -> bool:
    if not path:
        return False
    for p in _STATIC_PREFIXES:
        if path.startswith(p):
            return True
    for s in _STATIC_SUFFIXES:
        if path.endswith(s):
            return True
    return False


class _HashState:
    """Sliding-window state for one visitor_hash."""
    __slots__ = ("hits", "paths", "fetched_static", "first_hit_ts", "banned_until")

    def __init__(self):
        # hits is a deque of (ts, path) — trimmed to WINDOW_SECONDS on read
        self.hits = deque()
        self.paths: set[str] = set()
        self.fetched_static: bool = False
        self.first_hit_ts: float = 0.0
        self.banned_until: float = 0.0


class SessionScraperTracker:
    def __init__(self):
        self._states: dict[str, _HashState] = defaultdict(_HashState)
        self._lock = threading.Lock()

    def observe(self, visitor_hash: str, path: str, now: Optional[float] = None,
                user_agent: str = "") -> bool:
        """Record one request. Returns True if the hash is banned (caller
        should return 429 + Retry-After); False otherwise. Static-asset
        fetches never trigger the ban and mark the hash as
        real-browser-shaped. Monitor / healthcheck probes are
        SHORT-CIRCUITED (no state written) so a stable-hash probe
        cadence on one path doesn't trip the tracker."""
        if not visitor_hash:
            return False

        # Monitor / healthcheck probes exit before any state write.
        # We don't want them to count in the sliding window, mark the
        # session real-browser-shaped, or ever trigger a ban.
        if _is_monitor_probe(path, user_agent):
            return False

        if now is None:
            now = time.time()
        with self._lock:
            st = self._states[visitor_hash]

            # Static asset → real browser marker, no accounting, no ban check
            if _is_static_path(path):
                st.fetched_static = True
                # Static hits still count in the window (trims stale entries)
                st.hits.append((now, path))
                self._trim(st, now)
                return False

            # Still banned from a prior trip
            if st.banned_until > now:
                return True

            # Real browsers exempt — the guard Charlie asked for
            if st.fetched_static:
                return False

            # Record and trim
            st.hits.append((now, path))
            st.paths.add(path)
            self._trim(st, now)

            # Trip condition
            if (len(st.hits) >= HIT_THRESHOLD
                    and len(st.paths) <= PATH_THRESHOLD):
                # Log-only shadow mode: report but do not enforce. The
                # caller sees a normal `False` return and serves the
                # request; the log line records what WOULD have been
                # blocked so we can eyeball 24h of shadow output before
                # arming. Also: no banned_until stamp in shadow mode so
                # we don't accidentally persist a phantom ban that a
                # later flip-to-enforce would surface as a real 429.
                if LOG_ONLY_MODE:
                    import logging as _logging
                    _logging.getLogger("session_scraper_tracker").info(
                        "SHADOW_TRIP visitor_hash=%s hits=%d paths=%d "
                        "top_path=%s (log-only, not enforcing)",
                        visitor_hash[:12] if visitor_hash else "",
                        len(st.hits), len(st.paths),
                        next(iter(st.paths), ""),
                    )
                    return False
                st.banned_until = now + BAN_SECONDS
                return True
            return False

    def _trim(self, st: _HashState, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while st.hits and st.hits[0][0] < cutoff:
            st.hits.popleft()
        # Recompute distinct paths from the trimmed window — the set
        # can only ever shrink under a rolling window since we drop
        # ts+path tuples. Cheap: window is bounded to WINDOW_SECONDS.
        st.paths = {p for _, p in st.hits}
        if st.hits:
            st.first_hit_ts = st.hits[0][0]

    # Introspection helpers used by tests + a future /admin panel
    def _snapshot(self, visitor_hash: str) -> dict:
        with self._lock:
            st = self._states.get(visitor_hash)
            if not st:
                return {}
            return {
                "hits": len(st.hits),
                "paths": len(st.paths),
                "fetched_static": st.fetched_static,
                "banned_until": st.banned_until,
            }

    def _reset(self) -> None:
        with self._lock:
            self._states.clear()


# Process-wide singleton — imported by app.py's before_request hook.
tracker = SessionScraperTracker()
