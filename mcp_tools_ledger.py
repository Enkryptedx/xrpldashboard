"""Day 2 ledger-primitives tool batch for the xrpldashboard MCP server.

Three tools, in order of what they exercise:

  1. `get_ledger_stats`      — live server_info RPC → validated ledger,
                                close time, server state, load factor.
                                Exercises the envelope on a fast-cycling
                                source (≤ 5s freshness contract).

  2. `get_amendment_status`  — cached amendments_state fetch (300s TTL,
                                already the source for /amendments).
                                Exercises `cross_check_status` = "agree"
                                when the responding node's Amendments-
                                ledger-object enabled set matches its
                                feature-RPC enabled set; "disagree" when
                                there is at least one unrecognized-enabled
                                hash (which /amendments surfaces today).

  3. `get_unl_status`        — cached network_state fetch (both published
                                UNLs decoded, overlap + expiry-days
                                computed). Exercises `honest_partial=True`
                                +`scope_note` when one list fetch failed.

Every tool routes its return through `mcp_server.wrap_envelope(...)` —
no bypass. On success each tool also calls
`mcp_server.stamp_tool_call(tool_name)` which writes walker_health for
walker_name='mcp_server_last_tool_call'. That is the Day 2 Q1
mitigation from `mcp_server.py`'s header comment: the heartbeat thread
can outlive an HTTP-listener crash, so the heartbeat freshness alone
can lie; a separate "last tool actually served a response" watermark
lets /walker_health distinguish `server alive` from `tools alive`.

Two failure paths are load-bearing and MUST be preserved:

  * Underlying fetch fails → tool raises. wrap_envelope never runs,
    stamp_tool_call never runs, MCP server surfaces the error to the
    client. Absence of `mcp_server_last_tool_call` freshness IS the
    signal — do NOT silently return a stub envelope on fetch failure.

  * Envelope invariant violated → wrap_envelope raises ValueError
    (empty source, null as_of, unknown freshness_contract, …). Same
    routing: the caller sees an error, no partial envelope escapes,
    stamp_tool_call is never reached.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

import mcp_server


def _iso_utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ─────────────────────────────────────────────────────────────────────
# 1. get_ledger_stats
# ─────────────────────────────────────────────────────────────────────

# 30-Jan-2000 00:00:00Z per XRPL time-representation spec.
XRPL_EPOCH_UNIX_OFFSET = 946684800


def _xrpl_close_to_iso(close_time_xrpl: int) -> str:
    """XRPL close-time is seconds since the ripple epoch (2000-01-01Z).
    Convert to ISO-8601 UTC. Callers pass raw integer close_time from
    server_info; None/missing → return the ingest-side stamp instead
    (never fabricate a close time)."""
    if not isinstance(close_time_xrpl, int):
        return _iso_utc_now()
    unix = XRPL_EPOCH_UNIX_OFFSET + close_time_xrpl
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(unix))


def _fetch_server_info(xrpl_node_url: str) -> dict:
    """Post `server_info` to the configured XRPL node. Returns the
    `result.info` dict. Raises RuntimeError on non-success — caller lets
    it propagate so the MCP client sees the error and
    mcp_server_last_tool_call stays stale."""
    resp = httpx.post(
        xrpl_node_url,
        json={"method": "server_info", "params": [{}]},
        timeout=15.0,
    )
    payload = resp.json() or {}
    result = payload.get("result") or {}
    if result.get("status") != "success":
        raise RuntimeError(f"server_info non-success: {result.get('error') or result}")
    info = result.get("info") or {}
    if not info:
        raise RuntimeError("server_info returned empty info block")
    return info


def tool_get_ledger_stats(xrpl_node_url: str) -> dict:
    """Return the last-validated ledger stats wrapped in the proof envelope.

    Fields surfaced: validated_ledger_index, close_time_iso,
    server_state, load_factor, complete_ledgers (contiguous range),
    build_version. All read straight from `server_info.result.info`
    — no derived values, no rewriting."""
    info = _fetch_server_info(xrpl_node_url)
    validated = info.get("validated_ledger") or {}
    data = {
        "validated_ledger_index": validated.get("seq"),
        "close_time_iso": _xrpl_close_to_iso(validated.get("close_time")),
        "server_state": info.get("server_state"),
        "load_factor": info.get("load_factor"),
        "complete_ledgers": info.get("complete_ledgers"),
        "build_version": info.get("build_version"),
        "hostid": info.get("hostid"),
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="local_rippled",
        as_of=_iso_utc_now(),
        freshness_contract="≤ 5min",
        methodology_url="https://xrpldashboard.com/methodology#ledger",
        claims_ref="ledger_stats_live",
    )
    mcp_server.stamp_tool_call("get_ledger_stats")
    return envelope


# ─────────────────────────────────────────────────────────────────────
# 2. get_amendment_status
# ─────────────────────────────────────────────────────────────────────

def tool_get_amendment_status() -> dict:
    """Return the current amendments state (enabled / in-flight /
    superseded / unrecognized-enabled) wrapped in the proof envelope.

    cross_check_status is derived on the fly from the fetched state:
      * `agree`      — every hash in the Amendments-ledger-object's
                        enabled set is also carried in the responding
                        node's feature-RPC output (both views concur).
      * `disagree`   — at least one enabled hash is unrecognized by
                        the responding node (documented on /amendments
                        as the "unrecognized enabled" list — validators
                        on newer builds voting on definitions the
                        responding node's binary doesn't carry).

    Uses the 300s-cached `amendments_state.fetch_amendments_state_cached()`
    so the underlying RPC pressure is unchanged from /amendments."""
    import amendments_state
    state = amendments_state.fetch_amendments_state_cached()
    if not state or not state.get("ok"):
        raise RuntimeError("amendments_state fetch failed")

    enabled = state.get("enabled") or []
    unrecognized_enabled = state.get("unrecognized_enabled") or []
    in_flight = state.get("in_flight") or []
    superseded = state.get("superseded") or []

    cross_check = "agree" if not unrecognized_enabled else "disagree"

    data = {
        "enabled_count": len(enabled) + len(unrecognized_enabled),
        "recognized_enabled_count": len(enabled),
        "unrecognized_enabled_count": len(unrecognized_enabled),
        "in_flight_count": len(in_flight),
        "superseded_count": len(superseded),
        "enabled": enabled,
        "unrecognized_enabled": unrecognized_enabled,
        "in_flight": in_flight,
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="local_rippled+cross_check_pair_4",
        as_of=_iso_utc_now(),
        freshness_contract="≤ 30min",
        methodology_url="https://xrpldashboard.com/methodology#amendments",
        claims_ref="amendments_active_count",
        cross_check_status=cross_check,
    )
    mcp_server.stamp_tool_call("get_amendment_status")
    return envelope


# ─────────────────────────────────────────────────────────────────────
# 3. get_unl_status
# ─────────────────────────────────────────────────────────────────────

def tool_get_unl_status() -> dict:
    """Return the current UNL composition wrapped in the proof envelope.

    Fields surfaced: per-list expiry-days, validator counts, overlap
    (both / ripple_only / xrplf_only / union / jaccard). Honest-partial
    routing: if one list fetch failed, envelope carries
    `honest_partial=True` and a `scope_note` naming which list is
    missing — never a fabricated union or overlap.

    Underlying 10-min cache is the same one /network uses; no
    additional egress introduced."""
    import network_state
    state = network_state.fetch_network_state_cached()
    if not state or not state.get("ok"):
        raise RuntimeError("network_state fetch failed")

    lists = state.get("lists") or []
    failed = [l for l in lists if not l.get("ok")]
    data = {
        "lists": [
            {
                "key": l.get("key"),
                "url": l.get("url"),
                "ok": l.get("ok"),
                "sequence": l.get("sequence"),
                "validator_count": l.get("validator_count"),
                "expiration_iso": l.get("expiration_iso"),
                "days_remaining": l.get("days_remaining"),
                "is_expired": l.get("is_expired"),
            }
            for l in lists
        ],
        "overlap": state.get("overlap"),
        "fetched_at_iso": state.get("fetched_at_iso"),
    }

    honest_partial = bool(failed)
    scope_note = None
    if honest_partial:
        scope_note = (
            "one or more UNL list fetches failed: "
            + ", ".join(l.get("key") or "unknown" for l in failed)
            + " — overlap/union may be unavailable"
        )

    envelope = mcp_server.wrap_envelope(
        data,
        source="vl.ripple.com+vl.xrplf.org+cross_check_pair_5",
        as_of=_iso_utc_now(),
        freshness_contract="≤ 30min",
        methodology_url="https://xrpldashboard.com/methodology#unl",
        claims_ref="unl_composition",
        honest_partial=honest_partial,
        scope_note=scope_note,
    )
    mcp_server.stamp_tool_call("get_unl_status")
    return envelope
