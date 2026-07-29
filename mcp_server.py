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


def build_server():
    """Return a FastMCP server instance with no tools registered yet.

    Tools land in Day 2-4 build steps; each is a `@mcp.tool()`-decorated
    function whose body ends with `return wrap_envelope(...)`. This
    scaffold intentionally exposes zero tools so the surface is a
    truthful "server up, tool inventory empty" during Day 1.
    """
    from mcp.server.fastmcp import FastMCP  # lazy import — package is
    # not required for the envelope-only unit tests.
    mcp = FastMCP(SERVER_NAME)
    return mcp


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("starting mcp_server_heartbeat (cadence=%ds)", HEARTBEAT_CADENCE_SECONDS)
    start_heartbeat()

    mcp = build_server()
    log.info("mcp scaffold ready; tool inventory=0 (Day 1 scaffold)")

    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    mcp.run(transport=transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
