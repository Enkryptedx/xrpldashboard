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
    DEFAULT_UA_ALLOWLIST_SUBSTRINGS,
    SessionRateLimiter,
    _normalize_ref,
    _reset_ref_state_for_tests,
    get_ref_for_session,
    mark_session_seen,
    session_key_from_context,
    should_bypass_rate_limit,
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


# ─────────────────────────────────────────────────────────────────────
# Ref-tag capture (per-directory attribution) — added 2026-08-12 for
# the 3→9 MCP directory expansion. Each MCP session is stamped once
# with its ?ref=<slug> so October's revenue read can count arriving
# agents by referring directory. See mcp_session_rate_limit.py and
# project_mcp_directory_expansion_2026-08-13.md for the design.
# ─────────────────────────────────────────────────────────────────────

class _RefQueryParams:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, key, default=None):
        return self._m.get(key, default)


class _RefRequest:
    def __init__(self, ref=None, missing=False):
        if missing:
            self.query_params = None
        else:
            self.query_params = _RefQueryParams({"ref": ref} if ref is not None else {})


class _RefRequestContext:
    def __init__(self, request):
        self.request = request


class _RefCtx:
    def __init__(self, request=None):
        self.request_context = _RefRequestContext(request)


def test_normalize_ref_valid_slugs():
    assert _normalize_ref("anthropic") == "anthropic"
    assert _normalize_ref("Smithery") == "smithery"          # lowercased
    assert _normalize_ref("mcp-so") == "mcp-so"              # hyphen allowed
    assert _normalize_ref("all_mcps") == "all_mcps"          # underscore allowed
    assert _normalize_ref("glama2") == "glama2"              # digits allowed
    assert _normalize_ref("a") == "a"                        # 1-char minimum


def test_normalize_ref_edge_cases():
    assert _normalize_ref(None) == "direct"
    assert _normalize_ref("") == "direct"
    assert _normalize_ref("   ") == "direct"                 # stripped-then-empty
    assert _normalize_ref("has space") == "invalid"
    assert _normalize_ref("has/slash") == "invalid"
    assert _normalize_ref("SELECT *") == "invalid"
    assert _normalize_ref("-leading-hyphen") == "invalid"    # must start alnum
    assert _normalize_ref("a" * 65) == "invalid"             # length cap
    assert _normalize_ref("a" * 64) == ("a" * 64)            # length cap boundary


def test_mark_session_seen_binds_and_stamps_once(monkeypatch):
    """First call captures ref + stamps; second call is idempotent (no
    second stamp). This is the load-bearing behaviour — a chatty agent
    that calls 500 tools in one session must show up as ONE arrival,
    not 500, in the per-directory counter."""
    stamps = []

    def fake_stamp(session_key, ref):
        stamps.append((session_key, ref))

    monkeypatch.setattr(
        "mcp_session_rate_limit.stamp_session_start", fake_stamp
    )

    async def scenario():
        await _reset_ref_state_for_tests()
        ctx = _RefCtx(_RefRequest(ref="anthropic"))
        r1 = await mark_session_seen(ctx, "session:1234")
        r2 = await mark_session_seen(ctx, "session:1234")
        r3 = await mark_session_seen(ctx, "session:1234")
        assert r1 == "anthropic"
        assert r2 == "anthropic"
        assert r3 == "anthropic"
        assert len(stamps) == 1, "session_start must stamp exactly once per session"
        assert stamps[0] == ("session:1234", "anthropic")
        assert get_ref_for_session("session:1234") == "anthropic"

    _run(scenario())


def test_mark_session_seen_missing_ref_is_direct(monkeypatch):
    """Sessions that arrive without ?ref= are stamped as 'direct' —
    the ratio of tagged/direct is itself a signal the counting query
    surfaces (organic vs directory-attributed traffic)."""
    stamps = []
    monkeypatch.setattr(
        "mcp_session_rate_limit.stamp_session_start",
        lambda k, r: stamps.append((k, r)),
    )

    async def scenario():
        await _reset_ref_state_for_tests()
        ctx = _RefCtx(_RefRequest(ref=None))  # no ?ref= at all
        r = await mark_session_seen(ctx, "session:direct-1")
        assert r == "direct"
        assert stamps == [("session:direct-1", "direct")]

    _run(scenario())


def test_mark_session_seen_invalid_ref_is_bucketed(monkeypatch):
    """A wire-poisoned ref value can't break session init — it collapses
    to 'invalid' and the session is still counted. Never raises."""
    stamps = []
    monkeypatch.setattr(
        "mcp_session_rate_limit.stamp_session_start",
        lambda k, r: stamps.append((k, r)),
    )

    async def scenario():
        await _reset_ref_state_for_tests()
        ctx = _RefCtx(_RefRequest(ref="'; DROP TABLE walker_health;--"))
        r = await mark_session_seen(ctx, "session:poison")
        assert r == "invalid"
        assert stamps == [("session:poison", "invalid")]

    _run(scenario())


def test_mark_session_seen_survives_missing_request(monkeypatch):
    """If the transport layer ever fails to stash the Starlette Request
    (e.g., MCP SDK version drift), ref capture falls back to 'direct'
    without raising. Q1 fail-open discipline — same rule as the UA
    allowlist path."""
    stamps = []
    monkeypatch.setattr(
        "mcp_session_rate_limit.stamp_session_start",
        lambda k, r: stamps.append((k, r)),
    )

    async def scenario():
        await _reset_ref_state_for_tests()
        # request is None entirely
        ctx = _RefCtx(request=None)
        r = await mark_session_seen(ctx, "session:no-request")
        assert r == "direct"
        assert stamps == [("session:no-request", "direct")]
        # query_params is None (structural gap)
        await _reset_ref_state_for_tests()
        ctx2 = _RefCtx(_RefRequest(missing=True))
        r2 = await mark_session_seen(ctx2, "session:no-qp")
        assert r2 == "direct"

    _run(scenario())


def test_mark_session_seen_distinct_sessions_bind_independently(monkeypatch):
    """Two sessions arriving via different directories are counted
    independently. Regression fence against the class of bug where a
    module-level ref cache would let session #2's ref overwrite session
    #1's."""
    stamps = []
    monkeypatch.setattr(
        "mcp_session_rate_limit.stamp_session_start",
        lambda k, r: stamps.append((k, r)),
    )

    async def scenario():
        await _reset_ref_state_for_tests()
        ctx_a = _RefCtx(_RefRequest(ref="smithery"))
        ctx_b = _RefCtx(_RefRequest(ref="glama"))
        await mark_session_seen(ctx_a, "session:aaa")
        await mark_session_seen(ctx_b, "session:bbb")
        await mark_session_seen(ctx_a, "session:aaa")  # dup — no re-stamp
        assert get_ref_for_session("session:aaa") == "smithery"
        assert get_ref_for_session("session:bbb") == "glama"
        assert stamps == [
            ("session:aaa", "smithery"),
            ("session:bbb", "glama"),
        ]

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
# should_bypass_rate_limit — UA allowlist for directory scanners
# ─────────────────────────────────────────────────────────────────────

class _FakeHeaders:
    def __init__(self, ua: str | None) -> None:
        self._ua = ua

    def get(self, key: str, default: str = "") -> str:
        if key.lower() == "user-agent":
            return self._ua if self._ua is not None else default
        return default


class _FakeRequest:
    def __init__(self, ua: str | None) -> None:
        self.headers = _FakeHeaders(ua)


class _FakeRequestContext:
    def __init__(self, request):
        self.request = request


class _CtxWithRequest:
    def __init__(self, ua: str | None) -> None:
        self.request_context = _FakeRequestContext(_FakeRequest(ua))


class _CtxRequestContextRaises:
    @property
    def request_context(self):
        raise RuntimeError("no request context")


def test_bypass_matches_smitherybot_default():
    """Default allowlist bypasses SmitheryBot UA — the reason this
    module exists as of 2026-08-05. If this ever regresses, we walk
    into Smithery's SmitheryBot scan with a 429 waiting for it, which
    was the pre-written landmine we shipped this to defuse.
    """
    assert "SmitheryBot" in DEFAULT_UA_ALLOWLIST_SUBSTRINGS
    ctx = _CtxWithRequest("SmitheryBot/1.0 (+https://smithery.ai)")
    bypassed, matched = should_bypass_rate_limit(ctx)
    assert bypassed is True
    assert matched == "smitherybot"


def test_bypass_case_insensitive():
    """UA match is case-insensitive so a version bump to
    ``SMITHERYBOT/2.0`` doesn't break the bypass."""
    ctx = _CtxWithRequest("SMITHERYBOT/2.0")
    bypassed, _ = should_bypass_rate_limit(ctx)
    assert bypassed is True


def test_bypass_does_not_match_ordinary_client():
    """Ordinary MCP clients (Claude Desktop, curl, our own dogfood) are
    NOT bypassed. If this ever regresses, the limiter is silently open."""
    ctx = _CtxWithRequest("Claude-Desktop/1.0")
    assert should_bypass_rate_limit(ctx) == (False, "")

    ctx = _CtxWithRequest("curl/8.4.0")
    assert should_bypass_rate_limit(ctx) == (False, "")

    ctx = _CtxWithRequest("xrpldashboard-mcp-ready-guard/1.0")
    assert should_bypass_rate_limit(ctx) == (False, "")


def test_bypass_absent_ua_not_bypassed():
    """No User-Agent header → not bypassed. Fail-closed."""
    ctx = _CtxWithRequest(None)
    assert should_bypass_rate_limit(ctx) == (False, "")

    ctx = _CtxWithRequest("")
    assert should_bypass_rate_limit(ctx) == (False, "")


def test_bypass_context_missing_request_not_bypassed():
    """No request on the context (stdio transport, tests) → not bypassed."""
    ctx = _CtxWithRequest("SmitheryBot/1.0")
    ctx.request_context.request = None
    assert should_bypass_rate_limit(ctx) == (False, "")


def test_bypass_context_raises_not_bypassed():
    """Any exception reading the context → fail-closed (not bypassed).
    Better to limit a legitimate scanner (they retry with backoff) than
    to leak an unlimited bypass through a wiring bug."""
    assert should_bypass_rate_limit(_CtxRequestContextRaises()) == (False, "")


def test_bypass_env_extends_default(monkeypatch):
    """MCP_SESSION_LIMIT_UA_ALLOWLIST extends the default set."""
    monkeypatch.setenv(
        "MCP_SESSION_LIMIT_UA_ALLOWLIST",
        "MyCustomScanner, ExampleBot",
    )
    ctx = _CtxWithRequest("MyCustomScanner/1.0")
    bypassed, matched = should_bypass_rate_limit(ctx)
    assert bypassed is True
    assert matched == "mycustomscanner"

    # Default still holds under env extension.
    ctx = _CtxWithRequest("SmitheryBot/1.0")
    assert should_bypass_rate_limit(ctx)[0] is True


def test_rate_limited_fastmcp_bypasses_when_should_bypass_true(monkeypatch):
    """Integration: when ``should_bypass_rate_limit`` returns True the
    ``call_tool`` override skips the limiter and delegates straight to
    the FastMCP parent. Ceiling is 2 tool calls; the bypassed path
    makes 5, all pass.

    This is the day-one guarantee for the directory-submission window:
    the Smithery review scan hits our public endpoint with SmitheryBot
    UA, ``should_bypass_rate_limit`` returns True (verified separately
    by ``test_bypass_matches_smitherybot_default``), and our own 600/hr
    limiter cannot be the reason we get auto-rejected.

    The bypass predicate is monkey-patched at the module level BEFORE
    ``build_server`` is called — ``build_server`` re-imports
    ``should_bypass_rate_limit`` on each call, so the closure inside
    the ``_RateLimitedFastMCP`` subclass picks up the patched version.
    """
    import mcp_session_rate_limit

    monkeypatch.setattr(
        mcp_session_rate_limit,
        "should_bypass_rate_limit",
        lambda ctx: (True, "smitherybot"),
    )
    monkeypatch.setattr(
        mcp_session_rate_limit,
        "stamp_rate_limit_bypass",
        lambda *a, **k: None,
    )

    import mcp_server
    from mcp_session_rate_limit import SessionRateLimiter

    tiny = SessionRateLimiter(max_calls=2, window_seconds=60)
    server = mcp_server.build_server(register_tools=False, session_limiter=tiny)

    @server.tool()
    def echo(x: int) -> int:  # noqa: ARG001
        return x

    async def scenario():
        # Five calls under the bypass predicate — all pass despite ceiling=2.
        for i in range(5):
            r = await server.call_tool("echo", {"x": i})
            assert r is not None, f"bypassed call {i+1}/5 unexpectedly blocked"

    _run(scenario())


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
