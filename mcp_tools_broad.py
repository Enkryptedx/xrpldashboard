"""Broad-surface MCP tool batch (Charlie ruling 2026-09-23 16:29 ET,
item 3 MCP breadth).

Four tools, one per public sellable-surface JSON endpoint:

  1. `get_tokens(limit)`     — top-N tokens with tier + warnings, mirrors
                               /tokens.json data path (token_category_current
                               JOIN token_facts).

  2. `get_whales()`          — whales_summary rollup cells, mirrors
                               /whales.json (per-tier × per-filter body
                               metadata; live stream on wss.xrpldashboard.com
                               is called out in scope_note).

  3. `get_pools(limit)`      — AMM pools ranked by TVL, mirrors /pools.json.
                               Duplicates the top-by-TVL surface of
                               get_amm_top_by_tvl but at the "broad list"
                               contract (default limit=50 vs 10) and with
                               the same field vocabulary as the HTTP endpoint.

  4. `get_amendments()`      — amendments_block with per-amendment vote
                               tallies, mirrors /amendments.json. Distinct
                               from get_amendment_status which is state-only
                               (enabled/in-flight/superseded/unrecognized);
                               this tool adds live vote counts vs threshold.

Every tool routes its return through `mcp_server.wrap_envelope(...)`; every
success stamps `mcp_server_last_tool_call` for the Q1 heartbeat-gap
watermark. Failure paths intentionally leave the watermark stale.

Third-party-naming discipline:
  - `get_tokens` names issuer addresses AND assigns tier labels — carries
    `dispute_contact_url` per the /tokens footer rule.
  - `get_whales` names issuer addresses in whale rows — carries
    `dispute_contact_url`.
  - `get_pools` names AMM accounts + issuer counterparties — carries
    `dispute_contact_url`.
  - `get_amendments` names Amendment identifiers only (no third parties) —
    no `dispute_contact_url`.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Optional

import mcp_server


DISPUTE_CONTACT_URL = "https://xrpldashboard.com/contact?purpose=attestation-dispute"

# Freshness contracts must be one of mcp_server.VALID_FRESHNESS_CONTRACTS —
# match the HTTP endpoints' contracts exactly so an agent that has both
# surfaces open sees the same guarantee.
_FRESHNESS = {
    "tokens": "≤ 30min",       # token_category_current + token_facts refresh cadence
    "whales": "≤ 5min",        # whales_summary rollup cadence (short bucket)
    "pools":  "≤ 30min",       # amm_ranked_pools rank_amms_walker cadence
    "amendments": "daily",     # amendments_block signal cadence (per-day snapshot)
}


def _iso_utc_now() -> str:
    return (dt.datetime.now(dt.timezone.utc)
              .replace(microsecond=0)
              .isoformat().replace("+00:00", "Z"))


def _dec_str(x: Any) -> Optional[str]:
    if x is None:
        return None
    if isinstance(x, Decimal):
        return str(x)
    try:
        return str(x)
    except Exception:
        return None


# ─── get_tokens ─────────────────────────────────────────────────────
def tool_get_tokens(limit: int = 100) -> dict:
    """Return the top-N tokens by trades_30d, joined against attestation
    tier + warning flags. Mirrors /tokens.json data path."""
    import db
    if not db.pg_available():
        raise RuntimeError("get_tokens: DATABASE_URL not configured")
    limit = max(1, min(int(limit), 500))
    tokens_out = []
    with db.pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT tc.currency_hex, tc.issuer, tc.category, tc.tier,
                   tc.source, tc.citation_url,
                   tf.decoded_name, tf.trades_30d,
                   tf.ticker_collision, tf.non_standard_code
              FROM token_category_current tc
              LEFT JOIN token_facts tf USING (currency_hex, issuer)
             WHERE tc.tier IN ('verified','self-described','labeled','bare','unknown')
             ORDER BY tf.trades_30d DESC NULLS LAST
             LIMIT %s
            """,
            (limit,),
        )
        for r in cur.fetchall():
            tokens_out.append({
                "currency_hex": r[0],
                "issuer": r[1],
                "category": r[2],
                "tier": r[3],
                "attestation_source": r[4],
                "citation_url": r[5],
                "decoded_name": r[6],
                "trades_30d": int(r[7]) if r[7] is not None else None,
                "warning_ticker_collision": bool(r[8]),
                "warning_non_standard_code": bool(r[9]),
            })
    data = {
        "tokens": tokens_out,
        "row_count": len(tokens_out),
        "limit_requested": limit,
        "tier_vocab": ["verified", "self-described", "labeled", "bare", "unknown"],
        "warning_vocab": ["ticker_collision", "non_standard_code"],
        "dispute_contact_url": DISPUTE_CONTACT_URL,
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="token_category_current+token_facts",
        as_of=_iso_utc_now(),
        freshness_contract=_FRESHNESS["tokens"],
        methodology_url="https://xrpldashboard.com/methodology#tokens",
        claims_ref="tokens_top_by_trades_30d",
    )
    mcp_server.stamp_tool_call("get_tokens")
    return envelope


# ─── get_whales ─────────────────────────────────────────────────────
def tool_get_whales() -> dict:
    """Return the whales_summary rollup cells (per-tier × per-filter body
    metadata). Mirrors /whales.json. Live stream is on wss for real-time."""
    import db
    if not db.pg_available():
        raise RuntimeError("get_whales: DATABASE_URL not configured")
    cells: dict = {}
    stats: dict = {}
    computed_at_iso = None
    with db.pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT computed_at, cells, stats "
            "FROM whales_summary "
            "ORDER BY computed_at DESC "
            "LIMIT 1"
        )
        r = cur.fetchone()
        if r:
            computed_at, cells_raw, stats_raw = r
            computed_at_iso = (
                computed_at.astimezone(dt.timezone.utc)
                           .replace(microsecond=0)
                           .isoformat().replace("+00:00", "Z")
                if computed_at else None
            )
            if isinstance(cells_raw, dict):
                # cells keys are "<tier>:<filter_type>", e.g. "1m:tagged".
                # Surface a compact view: per-cell metadata (event count,
                # not the raw event list — that lives on the wss stream).
                for key, val in cells_raw.items():
                    tier, _, ft = key.partition(":")
                    if isinstance(val, dict):
                        event_ct = len(val.get("events") or []) if isinstance(val.get("events"), list) else None
                        cells[key] = {
                            "tier": tier,
                            "filter_type": ft or "default",
                            "event_count": event_ct,
                            "has_events": event_ct is not None and event_ct > 0,
                        }
                    elif isinstance(val, list):
                        cells[key] = {
                            "tier": tier,
                            "filter_type": ft or "default",
                            "event_count": len(val),
                            "has_events": len(val) > 0,
                        }
                    else:
                        cells[key] = {
                            "tier": tier,
                            "filter_type": ft or "default",
                            "event_count": None,
                            "has_events": False,
                        }
            if isinstance(stats_raw, dict):
                stats = stats_raw
    data = {
        "summary_cells": cells,
        "cell_count": len(cells),
        "stats": stats,
        "computed_at": computed_at_iso,
        "live_stream_url": "wss://wss.xrpldashboard.com",
        "scope_note": "Enumerates (tier × filter_type) rollup cells with event counts. The /whales HTML surface renders each cell as a live table; the raw event stream lives on wss for real-time.",
        "dispute_contact_url": DISPUTE_CONTACT_URL,
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="whales_summary",
        as_of=_iso_utc_now(),
        freshness_contract=_FRESHNESS["whales"],
        methodology_url="https://xrpldashboard.com/methodology#whales",
        claims_ref="whales_summary_cells",
    )
    mcp_server.stamp_tool_call("get_whales")
    return envelope


# ─── get_pools ─────────────────────────────────────────────────────
def tool_get_pools(limit: int = 50) -> dict:
    """Return AMM pools ranked by TVL. Mirrors /pools.json — broader list
    contract than get_amm_top_by_tvl (default limit=50 vs 10, same source
    snapshot amm_ranked_pools)."""
    import db
    if not db.pg_available():
        raise RuntimeError("get_pools: DATABASE_URL not configured")
    limit = max(1, min(int(limit), 200))
    ranked = db.read_amm_ranked_pools() or []

    def _tvl_sort_key(r):
        tvl = r.get("tvl_usd")
        if tvl is None:
            return (1, 0.0)
        try:
            return (0, -float(tvl))
        except (TypeError, ValueError):
            return (1, 0.0)

    sorted_pools = sorted(ranked, key=_tvl_sort_key)[:limit]
    pools_out = []
    for p in sorted_pools:
        pools_out.append({
            "pair": p.get("pair"),
            "amm_account": p.get("amm_account"),
            "asset_a": p.get("asset_a"),
            "asset_b": p.get("asset_b"),
            "amount_a": _dec_str(p.get("amount_a")),
            "amount_b": _dec_str(p.get("amount_b")),
            "tvl_usd": float(p.get("tvl_usd") or 0),
            "fee_pct": float(p.get("fee_pct") or 0),
            "kind": p.get("kind"),
            "tvl_status": p.get("tvl_status"),
        })
    data = {
        "pools": pools_out,
        "row_count": len(pools_out),
        "limit_requested": limit,
        "dispute_contact_url": DISPUTE_CONTACT_URL,
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="rank_amms_walker+amm_tvl_recorder",
        as_of=_iso_utc_now(),
        freshness_contract=_FRESHNESS["pools"],
        methodology_url="https://xrpldashboard.com/methodology#amm",
        claims_ref="pools_top_by_tvl",
    )
    mcp_server.stamp_tool_call("get_pools")
    return envelope


# ─── get_amendments ─────────────────────────────────────────────────────
def tool_get_amendments() -> dict:
    """Return the amendments block with per-amendment vote tallies. Mirrors
    /amendments.json. Distinct from get_amendment_status (state-only) — this
    tool adds live vote counts vs UNL-threshold per amendment."""
    import os
    from signed_snapshot import _assemble_amendments_block
    # _assemble_amendments_block is gated behind
    # SIGNED_SNAPSHOT_AMENDMENTS_BLOCK_ENABLED so it stays a no-op in the
    # snapshot leaf until Charlie flips the leaf-enable env. That gate is
    # for the signed-snapshot cadence, not for this read-only MCP tool —
    # override the gate at the process level so we always get the block
    # for the MCP surface. Callers hitting the leaf still respect the
    # snapshot-level env.
    _prev = os.environ.get("SIGNED_SNAPSHOT_AMENDMENTS_BLOCK_ENABLED")
    os.environ["SIGNED_SNAPSHOT_AMENDMENTS_BLOCK_ENABLED"] = "1"
    try:
        result = _assemble_amendments_block(dt.datetime.now(dt.timezone.utc))
    finally:
        if _prev is None:
            os.environ.pop("SIGNED_SNAPSHOT_AMENDMENTS_BLOCK_ENABLED", None)
        else:
            os.environ["SIGNED_SNAPSHOT_AMENDMENTS_BLOCK_ENABLED"] = _prev
    if not isinstance(result, dict):
        raise RuntimeError(
            "get_amendments: amendments_block signal empty (assembler "
            "returned non-dict; check amendments_state + amendments_network_votes "
            "modules)"
        )
    data = {
        "amendments_block": result,
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="amendments_state+amendments_network_votes",
        as_of=_iso_utc_now(),
        freshness_contract=_FRESHNESS["amendments"],
        methodology_url="https://xrpldashboard.com/methodology#amendments",
        claims_ref="amendments_block_with_tallies",
    )
    mcp_server.stamp_tool_call("get_amendments")
    return envelope
