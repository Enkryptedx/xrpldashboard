"""Session-scoped rate limiter for the MCP server (Option A2, 2026-08-04).

Enforces the ``mcp_session: 600 tool-calls/hour/session`` line advertised
in `/agents.json`. Built BEFORE the Cloudflare Tunnel goes public per the
tense-audit standard the site is run to: launching a public endpoint with
a declared-but-unenforced limit would be a member of the whale-filter-
that-didn't-filter class of catch that Red Team #1 was named to prevent.
The rule that stops that class is *enforcement matches advertised copy
from minute one* — "we said so, we did it" beats a footnote-caveat.

Design decisions and their reasons
──────────────────────────────────
Session key = ``id(ctx.session)`` — the transport-level ServerSession
object identity. Stable for the life of a client connection; a new
connection = a new bucket, which is precisely what "session-scoped"
means for a streaming transport. Not `ctx.client_id` — that reads from
the MCP request `_meta` params which are client-controlled and can be
spoofed to a different value on every call to escape the bucket.

Sliding window via deque-of-timestamps, prune-on-check. O(N) where
N ≤ ``max_calls``. At 600/hr = 1 call per 6 seconds average, the prune
cost is trivial; the memory ceiling per session is 600 floats (~5 KB).

Fail-open on session-key extraction — refusing to serve because we
can't identify the session would be a Q1 sibling to the walker_health
class of self-inflicted-outage the fleet is guarded against. Cloudflare
Tunnel + Cloudflare's per-IP DDoS defense provides the coarser floor
underneath us for that path.

Three-question monitor rubric (feedback_three_question_monitor_rubric)
──────────────────────────────────────────────────────────────────────
Q1  What could return allowed=True when the session actually is over cap?
    • Session-key extraction fails → all un-identified callers funnel
      into ``_FALLBACK_KEY``. Coarser than intended (may under-limit at
      the per-session level, may over-limit at the aggregate level) —
      logged-once on first fallback so the miswiring surfaces.
    • Process restart wipes buckets → intentional; a session that
      survives a restart is not the same session by definition.
Q2  Ground-truth check?
    • ``test_boundary_exact`` fires ``max_calls`` calls (all pass), then
      one more (must block with retry_after >= 1). The check catches any
      off-by-one in the prune or record path.
Q3  Who watches this?
    • Every block calls :func:`stamp_rate_limit_hit`, which writes a
      ``walker_health`` row for walker_name ``mcp_session_rate_limit``.
      /walker_health surfaces "session limited" as a real, named
      condition; blocks never leave the fleet as silent 429s.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_MAX_CALLS = 600
DEFAULT_WINDOW_SECONDS = 3600
_FALLBACK_KEY = "__session_key_unavailable__"


class SessionRateLimiter:
    """Sliding-window per-session tool-call limiter.

    Not a general-purpose rate limiter — designed for the MCP server's
    async single-process model. If the MCP server ever runs multi-process,
    the per-process bucket state becomes a Q1 gap (each worker enforces
    its own share of the ceiling); resolve then by moving the counter to
    a shared store (Postgres row per session) rather than growing this
    module.
    """

    def __init__(
        self,
        max_calls: int = DEFAULT_MAX_CALLS,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
    ) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check_and_record(self, session_key: str) -> tuple[bool, int]:
        """Check the session's window and, if allowed, record this call.

        Returns ``(allowed, retry_after_seconds)``. When ``allowed`` is
        True, ``retry_after_seconds`` is 0 and the call has been recorded
        in the session's window. When False, ``retry_after_seconds`` is
        the whole seconds a client should wait before the oldest recorded
        call falls out of the window (always >= 1 so clients never see
        Retry-After: 0).
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            bucket = self._buckets[session_key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_calls:
                oldest = bucket[0]
                retry_after = int(oldest + self.window_seconds - now) + 1
                if retry_after < 1:
                    retry_after = 1
                return False, retry_after
            bucket.append(now)
            return True, 0

    async def reset(self, session_key: str | None = None) -> None:
        """Test hook: clear one bucket (by key) or all buckets."""
        async with self._lock:
            if session_key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(session_key, None)

    def snapshot(self) -> dict[str, int]:
        """Approximate per-key call count. Non-locking — for
        observability only; a concurrent check may change values mid-read.
        """
        return {k: len(v) for k, v in self._buckets.items()}


def session_key_from_context(ctx: Any) -> str:
    """Extract a stable session key from a FastMCP request Context.

    Prefers ``id(ctx.session)`` — the identity of the transport-level
    ServerSession object. Stable for the life of a client connection.
    Returns :data:`_FALLBACK_KEY` (logged-once) if the session can't be
    resolved so the limiter still enforces at aggregate level for that
    class of caller.
    """
    try:
        session = ctx.session
    except Exception as e:  # noqa: BLE001 — property may raise if unavailable
        _log_fallback_once(f"ctx.session raised: {e!r}")
        return _FALLBACK_KEY
    if session is None:
        _log_fallback_once("ctx.session is None")
        return _FALLBACK_KEY
    return f"session:{id(session)}"


_FALLBACK_LOGGED = False


def _log_fallback_once(reason: str) -> None:
    global _FALLBACK_LOGGED
    if not _FALLBACK_LOGGED:
        log.warning(
            "mcp_session_rate_limit: session-key fallback in use — %s. "
            "All un-identified sessions will share one bucket until "
            "the fallback condition clears.",
            reason,
        )
        _FALLBACK_LOGGED = True


_STAMP_DB_MISSING_LOGGED = False


def stamp_rate_limit_hit(session_key: str, retry_after: int) -> None:
    """Write a walker_health row so /walker_health surfaces the block.

    The block itself is not a walker failure (``ok=True``) — the limiter
    performing its job as advertised is success. What /walker_health
    surfaces is *how often* the ceiling is being hit, which is the signal
    operators care about (either the ceiling is too low, or an abusive
    session is hammering it).

    Silent-skip on db import failure — logged once per process. Refusing
    to signal a block because the walker_health writer is unavailable
    would be a Q1 sibling (health infra failure masquerading as tool
    failure). Same pattern as :func:`mcp_server.stamp_tool_call`.
    """
    global _STAMP_DB_MISSING_LOGGED
    try:
        import db
    except Exception as e:  # noqa: BLE001
        if not _STAMP_DB_MISSING_LOGGED:
            log.warning(
                "stamp_rate_limit_hit: db import failed (%s); "
                "walker_health will not surface session-limit hits",
                e,
            )
            _STAMP_DB_MISSING_LOGGED = True
        return
    try:
        db.write_walker_health_end(
            "mcp_session_rate_limit",
            ok=True,
            message=f"blocked session={session_key} retry_after={retry_after}s",
        )
    except Exception as e:  # noqa: BLE001 — write is best-effort
        log.warning("stamp_rate_limit_hit: walker_health write failed: %s", e)
