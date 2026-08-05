"""xrpldashboard MCP server — scaffold (Day 1-2 of Agent Tier build).

Founding framing (docs/AGENT_TIER_DESIGN.md):
    The first MCP server whose data proves itself. Every response carries
    its receipts — source tier, freshness stamp, CLAIMS reference, snapshot
    signature. Competitors serve heuristic scores; we serve numbers with
    proof attached.

This module holds two build-first pieces that MUST land before any tool
exists (per §Enforcement of the design doc):

  1. `wrap_envelope(...)` — the single response-wrap function every tool
     must call. No tool bypasses it. Proof envelope is not decoration; it
     is the response schema. Structural fields enforced at call site
     (missing `as_of` or empty `source` → ValueError before the response
     ever leaves the server).

  2. `start_heartbeat()` — a daemon background thread that writes a
     `walker_health` row for walker_name='mcp_server_heartbeat' every
     `HEARTBEAT_CADENCE_SECONDS` (60s). If the MCP server dies, the
     row goes stale → /walker_health flips yellow within 60s → red within
     ~5min → external monitor pages. Same alarm surface every other walker
     uses; no new alert path.

Tools are added in the Day 2-4 build steps (§Tool inventory). At that
point every tool decorator body ends with `return wrap_envelope(...)`.

──────────────────────────────────────────────────────────────────────
Three-question monitor rubric (docs/MONITOR_AUDIT_2026-07.md).

Q1  What could return green when the thing being monitored is broken?
    • MCP server process alive but Postgres unreachable → wrap_envelope
      would still stamp `as_of` = now, but the underlying tool query
      would raise before wrap ever runs (Day 2+ tools own that guard).
    • Heartbeat thread alive but MCP endpoint unreachable → possible
      partial (thread survives an HTTP-listener crash). Mitigated Day
      2 by having each tool call also stamp `mcp_last_tool_call_at` so
      /walker_health can distinguish "heartbeat alive" from "tools
      alive." Until Day 2 ships, heartbeat freshness IS the only signal
      and this Q1 gap is declared, not silent.
    • wrap_envelope invariants violated by a caller (empty source, null
      as_of) → raises ValueError; no partial-envelope escape hatch.

Q2  What is the ground-truth check that catches a false-green?
    • For the heartbeat: /walker_health page + external BetterStack
      probe on the same /api/heartbeat-age endpoint the stream worker
      uses. If PG is dead the endpoint returns 503, which the external
      monitor sees.
    • For envelope integrity: `claims_check.sh` will be extended (Day
      2 checklist) to walk every tool's declared `claims_ref` and
      `methodology_url` — bad refs fail the pre-push gate.

Q3  Who watches this monitor?
    • answer_plausibility_walker's UNDECLARED_WALKER rule fires if the
      `walker_scope_declarations` row for `mcp_server_heartbeat` ever
      goes missing (seed row lives in seed_walker_scope_declarations.py).
    • answer_plausibility_walker itself is watched by its own UNDECLARED
      rule and by /walker_health surfacing consecutive_failures.
──────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
PUBKEY_FP_PATH = os.path.join(HERE, "snapshot_pubkey_fingerprint.txt")

SERVER_NAME = "xrpldashboard-mcp"
SERVER_VERSION = "1.0.0"
SERVER_DOCS_URL = "https://xrpldashboard.com/methodology#for-ai-agents"

WALKER_NAME = "mcp_server_heartbeat"
HEARTBEAT_CADENCE_SECONDS = 60

# Q1 heartbeat-gap mitigation (see header §Q1). A second walker_health
# row watermarks the last successful TOOL response — distinct from the
# heartbeat row which only watermarks the daemon thread. If the HTTP
# listener crashes but the heartbeat thread survives, the heartbeat row
# stays fresh while this row goes stale; /walker_health reads the delta
# and pages on tools-dead-server-alive within one cadence.
TOOL_CALL_WATERMARK_WALKER = "mcp_server_last_tool_call"

VALID_FRESHNESS_CONTRACTS = {
    "≤ 5min", "≤ 30min", "daily", "finalized_only",
}
VALID_CROSS_CHECK_STATUSES = {"agree", "disagree", "not_applicable"}


def _read_pubkey_fingerprint() -> str:
    try:
        with open(PUBKEY_FP_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "unknown"


_PUBKEY_FP_CACHED: Optional[str] = None


def _server_block() -> dict:
    global _PUBKEY_FP_CACHED
    if _PUBKEY_FP_CACHED is None:
        _PUBKEY_FP_CACHED = _read_pubkey_fingerprint()
    return {
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
        "public_key_fingerprint": _PUBKEY_FP_CACHED,
        "docs": SERVER_DOCS_URL,
    }


def _top_level_null_data_keys(data: Any) -> list[str]:
    """Names of top-level dict keys in the response payload whose value is None.

    Envelope contract: a top-level null field IS a missing sub-source. If any
    top-level `data` value is None, the caller must declare honest_partial=True
    and name which sub-source is missing in scope_note. Nested nulls (e.g.,
    an unrecognized amendment with name=None) are legitimate sentinels and
    are intentionally NOT flagged — top-level-only keeps the enforcement
    aligned with observed emit bugs (local_rippled admin-scoped triple)
    without cascading across every tool's inner shape.
    """
    if not isinstance(data, dict):
        return []
    return [k for k, v in data.items() if v is None]


def wrap_envelope(
    data: Any,
    *,
    source: str,
    as_of: str,
    freshness_contract: str,
    methodology_url: str,
    claims_ref: Optional[str] = None,
    cross_check_status: str = "not_applicable",
    honest_partial: bool = False,
    scope_note: Optional[str] = None,
) -> dict:
    """Wrap a tool response payload in the proof-annotation envelope.

    Every MCP tool MUST route its final return through this function. No
    tool bypasses it. See docs/AGENT_TIER_DESIGN.md §Proof-annotation
    envelope for field contracts.

    Raises ValueError on any invariant violation — the server refuses
    to emit a malformed envelope rather than degrade the guarantee.
    """
    if not isinstance(source, str) or not source:
        raise ValueError("envelope.source must be a non-empty string")
    if not isinstance(as_of, str) or not as_of:
        raise ValueError("envelope.as_of must be a non-empty ISO-8601 UTC string")
    if freshness_contract not in VALID_FRESHNESS_CONTRACTS:
        raise ValueError(
            f"envelope.freshness_contract must be one of "
            f"{sorted(VALID_FRESHNESS_CONTRACTS)}, got {freshness_contract!r}"
        )
    if not isinstance(methodology_url, str) or not methodology_url.startswith("https://"):
        raise ValueError("envelope.methodology_url must be an absolute https URL")
    if cross_check_status not in VALID_CROSS_CHECK_STATUSES:
        raise ValueError(
            f"envelope.cross_check_status must be one of "
            f"{sorted(VALID_CROSS_CHECK_STATUSES)}, got {cross_check_status!r}"
        )
    if honest_partial and not scope_note:
        raise ValueError("envelope.scope_note required when honest_partial=True")

    null_data_keys = _top_level_null_data_keys(data)
    if null_data_keys and not honest_partial:
        raise ValueError(
            "envelope contract: data has top-level null field(s) "
            f"{null_data_keys} but honest_partial=False. Callers emitting "
            "null fields MUST set honest_partial=True and populate "
            "scope_note naming which sub-source is missing or "
            "admin-scoped. Externally flagged 2026-08-03 by Grok + ChatGPT "
            "evaluations; the null-triple in tool_get_ledger_stats was the "
            "founding demo case."
        )

    return {
        "data": data,
        "proof": {
            "source": source,
            "as_of": as_of,
            "freshness_contract": freshness_contract,
            "claims_ref": claims_ref,
            "methodology_url": methodology_url,
            "cross_check_status": cross_check_status,
            "honest_partial": honest_partial,
            "scope_note": scope_note,
        },
        "server": _server_block(),
    }


_HEARTBEAT_THREAD: Optional[threading.Thread] = None
_HEARTBEAT_STOP = threading.Event()


_BETTERSTACK_MISSING_LOGGED = False


def _ping_betterstack() -> None:
    """Success-path ping to the external heartbeat monitor. Mirrors the
    pg_backup_canary pattern from `e60ac46`: URL missing = visible skip
    (log once, never fail the heartbeat cycle just because the external
    monitor isn't configured); network failure = warning-log-only. Every
    non-successful walker_health cycle skips this call, so a dead
    server = no ping = BetterStack incident within the grace window
    (5m period + 1m grace = ~6min detection floor).

    Visible-skip on absence — logged once per process to avoid flooding a
    60s heartbeat loop. Silent-skip on the sibling is_bot_canary concealed
    a sourced-but-not-exported env var for three days (2026-07-29→31)."""
    global _BETTERSTACK_MISSING_LOGGED
    url = os.environ.get("BETTERSTACK_MCP_SERVER_HEARTBEAT_URL")
    if not url:
        if not _BETTERSTACK_MISSING_LOGGED:
            log.warning("mcp_server_heartbeat: betterstack skip: URL not in "
                        "environment (BETTERSTACK_MCP_SERVER_HEARTBEAT_URL) — "
                        "external monitor will not receive pings until fixed")
            _BETTERSTACK_MISSING_LOGGED = True
        return
    try:
        import httpx
        httpx.get(url, timeout=10.0).raise_for_status()
    except Exception as e:  # noqa: BLE001 — ping is best-effort
        log.warning("mcp_server_heartbeat: betterstack ping failed: %s", e)


def _heartbeat_loop() -> None:
    try:
        import db
    except Exception as e:
        log.error("mcp_server_heartbeat: db import failed (%s); thread exiting", e)
        return

    try:
        db.write_walker_health_start(
            WALKER_NAME, cadence_seconds=HEARTBEAT_CADENCE_SECONDS,
        )
    except Exception as e:
        log.warning("mcp_server_heartbeat: initial start write failed: %s", e)

    while not _HEARTBEAT_STOP.wait(HEARTBEAT_CADENCE_SECONDS):
        try:
            db.write_walker_health_end(
                WALKER_NAME, ok=True, message="heartbeat",
            )
            db.write_walker_health_start(
                WALKER_NAME, cadence_seconds=HEARTBEAT_CADENCE_SECONDS,
            )
            # BetterStack ping ONLY after both walker_health writes
            # succeed — so a Neon outage that silently no-ops the
            # health writes ALSO withholds the external ping, which
            # is the correct behaviour (Q2 ground-truth alignment).
            _ping_betterstack()
        except Exception as e:
            log.warning("mcp_server_heartbeat: write cycle failed: %s", e)


def start_heartbeat() -> None:
    """Spawn the heartbeat daemon thread. Safe to call multiple times;
    only the first call starts a thread."""
    global _HEARTBEAT_THREAD
    if _HEARTBEAT_THREAD is not None and _HEARTBEAT_THREAD.is_alive():
        return
    _HEARTBEAT_STOP.clear()
    _HEARTBEAT_THREAD = threading.Thread(
        target=_heartbeat_loop,
        name="mcp_server_heartbeat",
        daemon=True,
    )
    _HEARTBEAT_THREAD.start()


def stop_heartbeat() -> None:
    """Signal the heartbeat thread to exit (used by tests)."""
    _HEARTBEAT_STOP.set()


def stamp_tool_call(tool_name: str) -> None:
    """Write the `mcp_server_last_tool_call` walker_health watermark.

    Called by every tool AFTER a successful envelope emit — the failure
    path (fetch raised or wrap_envelope raised) intentionally leaves the
    watermark stale so absence of freshness IS the signal.

    Silent failure of the write itself (e.g. Postgres transiently down)
    logs a warning but does not raise: refusing to serve tool responses
    because the health-watermark can't be written would be a Q1 sibling
    (health infra failure masquerading as tool failure)."""
    try:
        import db
        db.write_walker_health_end(
            TOOL_CALL_WATERMARK_WALKER,
            ok=True,
            message=f"tool={tool_name}",
        )
    except Exception as e:  # noqa: BLE001 — watermark write is best-effort
        log.warning("stamp_tool_call[%s] failed: %s", tool_name, e)


def _register_tools(mcp) -> int:
    """Register the Day 2 ledger-primitives tool batch. Returns the
    count of tools registered. Kept as a separate function so tests can
    build a bare `FastMCP` instance without touching tools, and so the
    Day 3+ tool batches drop in beside this one without churn."""
    import mcp_tools_ledger
    # XRPL_LOCAL_NODE is the fleet-wide canonical name for the local rippled
    # admin URL (see .env.example + walker plists). Prior wiring read
    # XRPL_NODE — the `.env.example` public-endpoint var — and defaulted
    # to public s1.ripple.com:51234 when unset, which is what the
    # 2026-08-02 P1 demo actually hit while its envelope claimed
    # source=`local_rippled` (see RUN_LOG.md § "Final correction"). Reading
    # the correct var + admin-port default closes that mislabel at the
    # config layer; the URL-derived source label in mcp_tools_ledger closes
    # it at the response layer.
    xrpl_node = os.environ.get("XRPL_LOCAL_NODE", "http://127.0.0.1:5005")

    @mcp.tool()
    def get_ledger_stats() -> dict:
        """Return the last-validated ledger stats (index, close time,
        server state, load factor, complete range, build). Wrapped in
        the xrpldashboard proof envelope; envelope source is derived from
        the actual URL host (`local_rippled` for loopback / private LAN,
        `remote_rippled:<host>` otherwise) — never hardcoded."""
        return mcp_tools_ledger.tool_get_ledger_stats(xrpl_node)

    @mcp.tool()
    def get_amendment_status() -> dict:
        """Return the current amendments state (enabled / in-flight /
        superseded / unrecognized-enabled) with `cross_check_status`
        derived from the responding node's feature-RPC vs Amendments
        ledger-object concurrence. Wrapped in the proof envelope."""
        return mcp_tools_ledger.tool_get_amendment_status()

    @mcp.tool()
    def get_unl_status() -> dict:
        """Return the current UNL composition (both published lists,
        overlap, expiry-days). Honest-partial when a list fetch fails.
        Wrapped in the proof envelope."""
        return mcp_tools_ledger.tool_get_unl_status()

    # Day 3 value-flow batch. Same pattern as the ledger batch — every
    # tool routes its return through wrap_envelope; every success stamps
    # mcp_server_last_tool_call for the Q1 heartbeat-gap watermark.
    import mcp_tools_value_flows

    @mcp.tool()
    def get_whale_events(limit: int = 25) -> dict:
        """Return the last N XRP-denominated whale events + tagged-watchlist
        events (trustset excluded). Same feed the homepage globe pulse
        reads. Wrapped in the proof envelope."""
        return mcp_tools_value_flows.tool_get_whale_events(limit)

    @mcp.tool()
    def get_whale_watchlist(limit: int = 25) -> dict:
        """Return the last N tagged-watchlist events (type='tagged'),
        floored at TAGGED_XRP_FLOOR_DROPS for XRP-denominated rows.
        Wrapped in the proof envelope."""
        return mcp_tools_value_flows.tool_get_whale_watchlist(limit)

    @mcp.tool()
    def get_rlusd_supply() -> dict:
        """Return the current cross-chain RLUSD supply (Ethereum + XRPL)
        from the same PG mirror /rlusd reads. Honest-partial when either
        chain errored. Wrapped in the proof envelope."""
        return mcp_tools_value_flows.tool_get_rlusd_supply()

    @mcp.tool()
    def get_rlusd_flow_24h() -> dict:
        """Return the last finalized 24h XRPL net-change from
        rlusd_supply_history. freshness_contract='finalized_only' is
        the machine-readable form of the R1/R2 finalized-window rule.
        cross_check_status derived from stored vs supply-diff pair.
        Wrapped in the proof envelope."""
        return mcp_tools_value_flows.tool_get_rlusd_flow_24h()

    # Day 4 AMM + token-attestation batch. Tools 3-5 are the first that
    # name a THIRD PARTY in the data payload (issuer attestation, RWA
    # family attribution) — every third-party-naming tool carries
    # `dispute_contact_url` so the named party has a first-party channel
    # to correct a label they disagree with. Same rule the /tokens and
    # /rwa footers apply to human readers.
    import mcp_tools_amm_tokens

    @mcp.tool()
    def get_amm_pool(amm_account: str) -> dict:
        """Return the single ranked-pool row for one AMM account from the
        amm_ranked_pools snapshot. Raises when the account is absent
        (unranked, below thresholds, or newer than the last snapshot).
        Wrapped in the proof envelope."""
        return mcp_tools_amm_tokens.tool_get_amm_pool(amm_account)

    @mcp.tool()
    def get_amm_top_by_tvl(limit: int = 10) -> dict:
        """Return the top-N ranked AMM pools by tvl_usd from the same
        snapshot /pools reads. NULL-tvl rows sort last. Wrapped in the
        proof envelope."""
        return mcp_tools_amm_tokens.tool_get_amm_top_by_tvl(limit)

    @mcp.tool()
    def get_token_attestation(currency: str, issuer: str) -> dict:
        """Return the attestation tier (verified / self-described / null)
        for one (currency, issuer) pair. Data payload includes
        `dispute_contact_url` — first tool to name a third party. Wrapped
        in the proof envelope."""
        return mcp_tools_amm_tokens.tool_get_token_attestation(currency, issuer)

    @mcp.tool()
    def get_rwa_families() -> dict:
        """Return every rwa_family row with attributed pool count. Data
        payload includes `dispute_contact_url`. Wrapped in the proof
        envelope."""
        return mcp_tools_amm_tokens.tool_get_rwa_families()

    @mcp.tool()
    def get_rwa_pools() -> dict:
        """Return every attributed RWA pool with provenance + TVL. Data
        payload includes `dispute_contact_url`. Wrapped in the proof
        envelope."""
        return mcp_tools_amm_tokens.tool_get_rwa_pools()

    @mcp.tool()
    def get_mpt_snapshot() -> dict:
        """Return the MPT snapshot dict (issuances / by_class / total).
        Not third-party-naming — no dispute_contact_url. Wrapped in the
        proof envelope."""
        return mcp_tools_amm_tokens.tool_get_mpt_snapshot()

    # Day 7 signed-snapshot batch. The moat expressing itself as an MCP
    # surface: a caller who screenshots a metric today can hand the
    # envelope back into verify_snapshot_signature six months later and
    # prove the number wasn't silently changed. Two tools, one round-trip
    # receipt.
    import mcp_tools_signed_snapshot

    @mcp.tool()
    def get_signed_snapshot(date_str: str) -> dict:
        """Return the Ed25519-signed daily snapshot for `date_str`
        (ISO YYYY-MM-DD) wrapped in the proof envelope. Absence raises —
        the walker has not produced this date yet, or the date predates
        the chain start."""
        return mcp_tools_signed_snapshot.tool_get_signed_snapshot(date_str)

    @mcp.tool()
    def verify_snapshot_signature(envelope: dict) -> dict:
        """Stateless verification: re-derive the leaf hash, check the
        Ed25519 signature against the pinned pubkey, check the audit path
        against the claimed chain root. Accepts full MCP envelope or bare
        signed payload. Wrapped in the proof envelope."""
        return mcp_tools_signed_snapshot.tool_verify_snapshot_signature(envelope)

    return 15


_SESSION_LIMITER = None


def _get_or_init_session_limiter():
    """Lazy singleton for the session rate limiter.

    Env-tunable so ops can tighten the ceiling during the public-beta
    soak without a redeploy. Copy in `/agents.json` reads the same
    constants — any change here must ship with the copy update, or the
    tense-audit reflex flags itself immediately.
    """
    global _SESSION_LIMITER
    if _SESSION_LIMITER is None:
        from mcp_session_rate_limit import (
            DEFAULT_MAX_CALLS,
            DEFAULT_WINDOW_SECONDS,
            SessionRateLimiter,
        )
        max_calls = int(os.environ.get("MCP_SESSION_MAX_CALLS", str(DEFAULT_MAX_CALLS)))
        window_seconds = int(
            os.environ.get("MCP_SESSION_WINDOW_SECONDS", str(DEFAULT_WINDOW_SECONDS))
        )
        _SESSION_LIMITER = SessionRateLimiter(
            max_calls=max_calls,
            window_seconds=window_seconds,
        )
    return _SESSION_LIMITER


def build_server(register_tools: bool = True, session_limiter=None):
    """Return a FastMCP server instance with per-session rate limiting.

    The returned server is a :class:`_RateLimitedFastMCP` — a FastMCP
    subclass that overrides ``call_tool`` to enforce the 600 tool-calls/
    hour/session ceiling advertised in `/agents.json`. Every tool call
    goes through the override; there is no bypass path from a network
    caller. See mcp_session_rate_limit.py for the design.

    When `register_tools=True` (default), the Day 2-7 tool batches are
    registered. Tests that only exercise the envelope pass
    `register_tools=False` to keep the surface minimal.

    ``session_limiter`` lets tests inject a small-window limiter to
    exercise boundary behaviour fast; production leaves it None and the
    module-level lazy singleton is used.
    """
    from mcp.server.fastmcp import FastMCP  # lazy import — package is
    # not required for the envelope-only unit tests.
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_session_rate_limit import (
        session_key_from_context,
        stamp_rate_limit_hit,
    )

    limiter = session_limiter if session_limiter is not None else _get_or_init_session_limiter()

    class _RateLimitedFastMCP(FastMCP):
        """FastMCP with per-session tool-call rate limiting.

        Overrides :meth:`call_tool` — the choke point the low-level MCP
        server routes ``tools/call`` protocol messages to (see
        ``FastMCP._setup_handlers``). Wrapping this one method enforces
        the limit for every registered tool with no per-tool churn.
        """

        async def call_tool(self, name, arguments):
            ctx = self.get_context()
            session_key = session_key_from_context(ctx)
            allowed, retry_after = await limiter.check_and_record(session_key)
            if not allowed:
                stamp_rate_limit_hit(session_key, retry_after)
                raise ToolError(
                    f"Session rate limit exceeded "
                    f"({limiter.max_calls} tool calls per "
                    f"{limiter.window_seconds}s window). "
                    f"Retry after ~{retry_after}s. See "
                    f"{SERVER_DOCS_URL} for the advertised rate limits."
                )
            return await super().call_tool(name, arguments)

    mcp = _RateLimitedFastMCP(SERVER_NAME)
    if register_tools:
        n = _register_tools(mcp)
        log.info("registered %d tool(s) on %s", n, SERVER_NAME)
    return mcp


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("starting mcp_server_heartbeat (cadence=%ds)", HEARTBEAT_CADENCE_SECONDS)
    start_heartbeat()

    mcp = build_server()

    # HTTP listener bind config — FastMCP's `streamable-http` transport
    # honours the FASTMCP_HOST / FASTMCP_PORT env vars its settings
    # object reads at construction time. We surface them under the same
    # MCP_* prefix as MCP_TRANSPORT so operators have one place to look.
    host = os.environ.get("MCP_HTTP_HOST") or os.environ.get("FASTMCP_HOST") or "127.0.0.1"
    port = os.environ.get("MCP_HTTP_PORT") or os.environ.get("FASTMCP_PORT") or "8765"
    os.environ["FASTMCP_HOST"] = host
    os.environ["FASTMCP_PORT"] = str(port)

    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    log.info("mcp starting: transport=%s host=%s port=%s", transport, host, port)
    mcp.run(transport=transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
