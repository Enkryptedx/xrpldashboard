"""Session-scoped rate limiter tests (Option A2, 2026-08-04).

Two layers of coverage:

  1. **Unit tests for :class:`SessionRateLimiter`.** Boundary correctness
     (N pass, N+1 blocks), retry_after monotonicity, distinct-key isolation,
     window prune. These are the Q2 ground-truth check for the limiter
     itself — if any of these fail, the limiter is falsely-permissive or
     falsely-restrictive and the `/agents.json` copy is already lying.

  2. **Integration test for the FastMCP subclass.** Every ``call_tool``
     goes through the override; on rate-limit exhaustion the client
     receives a :class:`ToolError` (never a stale success). This is the
     load-bearing behaviour: launching the public endpoint requires
     enforcement matches advertised copy from minute one.

No live network. No live DB. `stamp_rate_limit_hit`'s DB write is
monkey-patched to a no-op in the integration case.
"""
from __future__ import annotations

import asyncio

import pytest

from mcp_session_rate_limit import (
    _FALLBACK_KEY,
    SessionRateLimiter,
    session_key_from_context,
)


# ─────────────────────────────────────────────────────────────────────
# SessionRateLimiter unit tests
# ─────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_constructor_rejects_nonpositive():
    with pytest.raises(ValueError):
        SessionRateLimiter(max_calls=0)
    with pytest.raises(ValueError):
        SessionRateLimiter(max_calls=-1)
    with pytest.raises(ValueError):
        SessionRateLimiter(window_seconds=0)
    with pytest.raises(ValueError):
        SessionRateLimiter(window_seconds=-1)


def test_boundary_exact():
    """N calls pass, N+1 blocks with retry_after >= 1.

    Q2 ground-truth check for the limiter. If this test can pass with
    a limiter that permits N+1, the `/agents.json` line is falsely
    reassuring on day one.
    """
    limiter = SessionRateLimiter(max_calls=5, window_seconds=60)

    async def scenario():
        for i in range(5):
            allowed, retry_after = await limiter.check_and_record("s1")
            assert allowed is True, f"call {i+1}/5 unexpectedly blocked"
            assert retry_after == 0
        allowed, retry_after = await limiter.check_and_record("s1")
        assert allowed is False, "6th call should be blocked at max_calls=5"
        assert retry_after >= 1, "retry_after must be at least 1 (never 0)"

    _run(scenario())


def test_distinct_keys_isolated():
    """Different session keys have independent buckets.

    Regression fence against the class of bug where a spoofable key
    (e.g., ``ctx.client_id``) would let a caller escape the bucket by
    rotating identifiers mid-session. This test only verifies the
    limiter honours distinct keys; the session_key_from_context
    contract (id-of-session, not client_id) is what makes that spoof
    impossible in the caller path.
    """
    limiter = SessionRateLimiter(max_calls=2, window_seconds=60)

    async def scenario():
        assert (await limiter.check_and_record("s1")) == (True, 0)
        assert (await limiter.check_and_record("s1")) == (True, 0)
        blocked_s1, _ = await limiter.check_and_record("s1")
        assert blocked_s1 is False
        # s2 is unrelated — should still have its full quota
        assert (await limiter.check_and_record("s2")) == (True, 0)
        assert (await limiter.check_and_record("s2")) == (True, 0)
        blocked_s2, _ = await limiter.check_and_record("s2")
        assert blocked_s2 is False

    _run(scenario())


def test_window_prune_allows_new_calls():
    """Once entries age out of the window, new calls succeed again."""
    limiter = SessionRateLimiter(max_calls=3, window_seconds=1)

    async def scenario():
        for _ in range(3):
            assert (await limiter.check_and_record("s"))[0] is True
        assert (await limiter.check_and_record("s"))[0] is False
        # Wait past the window
        await asyncio.sleep(1.2)
        allowed, retry_after = await limiter.check_and_record("s")
        assert allowed is True, "call should succeed after window prune"
        assert retry_after == 0

    _run(scenario())


def test_reset_clears_bucket():
    limiter = SessionRateLimiter(max_calls=2, window_seconds=60)

    async def scenario():
        assert (await limiter.check_and_record("s"))[0] is True
        assert (await limiter.check_and_record("s"))[0] is True
        assert (await limiter.check_and_record("s"))[0] is False
        await limiter.reset("s")
        assert (await limiter.check_and_record("s"))[0] is True

    _run(scenario())


def test_snapshot_reports_counts():
    limiter = SessionRateLimiter(max_calls=5, window_seconds=60)

    async def scenario():
        for _ in range(3):
            await limiter.check_and_record("s1")
        for _ in range(2):
            await limiter.check_and_record("s2")

    _run(scenario())
    snap = limiter.snapshot()
    assert snap.get("s1") == 3
    assert snap.get("s2") == 2


# ─────────────────────────────────────────────────────────────────────
# session_key_from_context
# ─────────────────────────────────────────────────────────────────────

class _FakeSession:
    """Stand-in for a FastMCP ServerSession — only its identity matters."""


class _CtxWithSession:
    def __init__(self, session):
        self.session = session


class _CtxRaisesSession:
    @property
    def session(self):
        raise RuntimeError("no request context")


class _CtxNoneSession:
    session = None


def test_session_key_stable_for_same_session():
    s = _FakeSession()
    ctx1 = _CtxWithSession(s)
    ctx2 = _CtxWithSession(s)
    assert session_key_from_context(ctx1) == session_key_from_context(ctx2)


def test_session_key_differs_for_different_sessions():
    ctx1 = _CtxWithSession(_FakeSession())
    ctx2 = _CtxWithSession(_FakeSession())
    assert session_key_from_context(ctx1) != session_key_from_context(ctx2)


def test_session_key_fallback_when_session_raises():
    assert session_key_from_context(_CtxRaisesSession()) == _FALLBACK_KEY


def test_session_key_fallback_when_session_none():
    assert session_key_from_context(_CtxNoneSession()) == _FALLBACK_KEY


# ─────────────────────────────────────────────────────────────────────
# FastMCP subclass integration
# ─────────────────────────────────────────────────────────────────────

def test_rate_limited_fastmcp_blocks_at_ceiling(monkeypatch):
    """Every ``call_tool`` goes through the override; on ceiling
    exhaustion a :class:`ToolError` is raised (never a stale success).

    This is the day-one-launch guarantee: enforcement matches the
    advertised copy from minute one. If this test passes with the
    limiter disabled or bypassed, the site is walking into Red Team #2
    with the finding pre-written.
    """
    import mcp_server
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_session_rate_limit import SessionRateLimiter

    # Test-scale limiter (2 calls) so the boundary hits fast.
    tiny = SessionRateLimiter(max_calls=2, window_seconds=60)

    # Silence walker_health writes — the block path exercises them and we
    # don't want the test touching the real DB.
    monkeypatch.setattr("mcp_session_rate_limit.stamp_rate_limit_hit", lambda *a, **k: None)

    mcp = mcp_server.build_server(register_tools=False, session_limiter=tiny)

    @mcp.tool()
    def echo(x: int) -> int:  # noqa: ARG001 — arity for FastMCP schema gen
        return x

    async def scenario():
        # Two calls succeed
        r1 = await mcp.call_tool("echo", {"x": 1})
        r2 = await mcp.call_tool("echo", {"x": 2})
        assert r1 is not None
        assert r2 is not None
        # Third call must raise ToolError with the advertised copy hints
        with pytest.raises(ToolError) as excinfo:
            await mcp.call_tool("echo", {"x": 3})
        msg = str(excinfo.value)
        assert "Session rate limit exceeded" in msg
        assert "Retry after" in msg

    _run(scenario())


def test_rate_limited_fastmcp_isolates_sessions(monkeypatch):
    """Two independent FastMCP instances (proxy for two live sessions)
    should have independent buckets. Regression fence against the class
    of bug where a global-but-not-session-keyed counter would let one
    caller starve another out of their quota.

    We can't easily fake two concurrent live sessions inside FastMCP's
    context machinery in a unit test, so this test verifies the limiter
    isolation via its public API (already covered by
    test_distinct_keys_isolated) and defers the two-session-two-clients
    integration to the live smoke test that ships alongside the tunnel.
    """
    # This test's assertion IS the assertion in test_distinct_keys_isolated;
    # kept as a named test for the ship-checklist grep — a future reader
    # searching "session isolation" finds both the unit and the ledger of
    # what the live smoke test needs to cover post-tunnel.
    from mcp_session_rate_limit import SessionRateLimiter

    limiter = SessionRateLimiter(max_calls=1, window_seconds=60)

    async def scenario():
        assert (await limiter.check_and_record("session_A"))[0] is True
        assert (await limiter.check_and_record("session_A"))[0] is False
        # Session B is untouched by A's exhaustion.
        assert (await limiter.check_and_record("session_B"))[0] is True

    _run(scenario())
