"""Day 3 value-flow tool batch for the xrpldashboard MCP server.

Four tools, in order of what they exercise:

  1. `get_whale_events`     — last N large-XRP-transfer events captured
                               by the live xrpl_stream. Envelope source
                               = `local_rippled_stream_capture`, freshness
                               ≤ 5min (stream cadence + PG lag).

  2. `get_whale_watchlist`  — the tagged-watchlist slice of the same
                               capture feed (type='tagged', floor
                               TAGGED_XRP_FLOOR_DROPS = 100 XRP). Exists
                               as a distinct tool so an agent can ask
                               "what's the watchlist doing right now"
                               without post-filtering the whale list.

  3. `get_rlusd_supply`     — cross-chain RLUSD supply from the same
                               PG mirror /rlusd reads. Envelope carries
                               `honest_partial=True` + scope_note when
                               either chain fetch errored (never a
                               fabricated cross-chain total).

  4. `get_rlusd_flow_24h`   — the last finalized 24h net-change from
                               rlusd_supply_history. This is the first
                               tool whose `freshness_contract` is
                               `finalized_only` — the machine-readable
                               form of the R1/R2 finalized-window rule
                               (`snapshot_date < today_utc`) that
                               answer_plausibility_walker applies on the
                               dashboard side.

                               Also the first tool whose
                               `cross_check_status` is derived from a
                               real pair: stored xrpl_net_change_24h
                               (Option-A gateway_balances diff) vs the
                               derived xrpl_supply[t] − xrpl_supply[t-1]
                               difference. If the two independently-
                               derived values agree within
                               `CROSS_CHECK_EPSILON_RLUSD`, the envelope
                               emits `cross_check_status='agree'`;
                               otherwise `disagree`.

Every tool routes its return through `mcp_server.wrap_envelope(...)` —
no bypass. On success each tool also calls
`mcp_server.stamp_tool_call(tool_name)` (Q1 heartbeat-gap mitigation).
Failure paths intentionally leave the watermark stale — absence of
freshness IS the signal, same rule the Day 2 batch established.
"""
from __future__ import annotations

import datetime
import decimal
import time
from typing import Any, Optional

import mcp_server

# Whale thresholds — kept in sync with app.py:94/99. The MCP tools serve
# the same tiering visitors see on the homepage/whales card, so an agent
# and a human get the same answer to "what's a whale event right now?".
WHALE_XRP_THRESHOLD_DROPS = 100_000 * 1_000_000
TAGGED_XRP_FLOOR_DROPS = 100 * 1_000_000

# Cross-check tolerance for the finalized 24h net-change. The stored
# xrpl_net_change_24h is Option-A (gateway_balances snapshot diff), and
# the derived value is a supply diff between the last two finalized
# rows. They should agree exactly in the absence of intra-day
# gateway_balances rounding; a Decimal("1") tolerance absorbs any
# per-issuer rounding without hiding a real divergence.
CROSS_CHECK_EPSILON_RLUSD = decimal.Decimal("1")


def _iso_utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ts_to_iso(ts_unix: Optional[int]) -> Optional[str]:
    if ts_unix is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts_unix)))


# ─────────────────────────────────────────────────────────────────────
# 1. get_whale_events
# ─────────────────────────────────────────────────────────────────────

def _serialize_event(row: tuple) -> dict:
    """Row layout mirrors db.read_recent_events:
        (tx_hash, ledger_index, ts, type, from_addr, to_addr,
         amount_drops, currency, issuer, raw_json)
    raw_json is dropped from the tool response — agents get the
    summarized fields; the tx_hash lets any client fetch the raw
    transaction from a full-history explorer if needed."""
    tx_hash, ledger_index, ts, type_, from_addr, to_addr, amount_drops, currency, issuer, _raw = row
    return {
        "tx_hash": tx_hash,
        "ledger_index": ledger_index,
        "ts_unix": int(ts) if ts is not None else None,
        "ts_iso": _ts_to_iso(ts),
        "type": type_,
        "from_addr": from_addr,
        "to_addr": to_addr,
        "amount_drops": int(amount_drops) if amount_drops is not None else None,
        "currency": currency,
        "issuer": issuer,
    }


def tool_get_whale_events(limit: int = 25) -> dict:
    """Return the last N XRP-denominated whale events + tagged-watchlist
    events (trustset excluded — signal, not movement). Same feed the
    homepage globe pulse reads; envelope makes the source explicit."""
    import db
    if not db.pg_available():
        raise RuntimeError("get_whale_events: DATABASE_URL not configured")
    limit = max(1, min(int(limit), 100))
    rows = db.read_recent_events(limit=limit, tagged_floor_drops=TAGGED_XRP_FLOOR_DROPS)
    data = {
        "events": [_serialize_event(r) for r in rows],
        "count": len(rows),
        "whale_threshold_drops": WHALE_XRP_THRESHOLD_DROPS,
        "tagged_floor_drops": TAGGED_XRP_FLOOR_DROPS,
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="local_rippled_stream_capture",
        as_of=_iso_utc_now(),
        freshness_contract="≤ 5min",
        methodology_url="https://xrpldashboard.com/methodology#whales",
        claims_ref="whale_events_recent",
    )
    mcp_server.stamp_tool_call("get_whale_events")
    return envelope


# ─────────────────────────────────────────────────────────────────────
# 2. get_whale_watchlist
# ─────────────────────────────────────────────────────────────────────

def tool_get_whale_watchlist(limit: int = 25) -> dict:
    """Return the last N tagged-watchlist events (type='tagged'), floored
    at TAGGED_XRP_FLOOR_DROPS for XRP-denominated rows and unfiltered for
    token-denominated rows (amount_drops IS NULL). Same predicate the
    homepage watchlist card uses.

    Fetches a wider window from the underlying feed (3× the requested
    limit) then Python-filters to type='tagged', so agents asking for
    'last 25 watchlist events' don't get short-served when the recent
    whale feed is dominated by large_xfer rows."""
    import db
    if not db.pg_available():
        raise RuntimeError("get_whale_watchlist: DATABASE_URL not configured")
    limit = max(1, min(int(limit), 100))
    fetch_n = min(limit * 3, 300)
    rows = db.read_recent_events(limit=fetch_n, tagged_floor_drops=TAGGED_XRP_FLOOR_DROPS)
    tagged_rows = [r for r in rows if r[3] == "tagged"][:limit]
    data = {
        "events": [_serialize_event(r) for r in tagged_rows],
        "count": len(tagged_rows),
        "tagged_floor_drops": TAGGED_XRP_FLOOR_DROPS,
        "note": (
            "Token-denominated tagged rows (amount_drops IS NULL) bypass "
            "the XRP floor; agents needing USD-priced sizing must join "
            "against the token-price oracle themselves."
        ),
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="local_rippled_stream_capture",
        as_of=_iso_utc_now(),
        freshness_contract="≤ 5min",
        methodology_url="https://xrpldashboard.com/methodology#whales",
        claims_ref="whale_watchlist_recent",
    )
    mcp_server.stamp_tool_call("get_whale_watchlist")
    return envelope


# ─────────────────────────────────────────────────────────────────────
# 3. get_rlusd_supply
# ─────────────────────────────────────────────────────────────────────

def _decimal_or_none(v: Any) -> Optional[str]:
    """Serialize NUMERIC-ish values as strings so the JSON round-trip
    doesn't lose precision (Decimal → float is lossy for large treasury
    balances). None passes through."""
    if v is None:
        return None
    if isinstance(v, (int, float, decimal.Decimal)):
        return str(v)
    return str(v)


def tool_get_rlusd_supply() -> dict:
    """Return the current cross-chain RLUSD supply (Ethereum + XRPL)
    from the same PG mirror /rlusd reads. Honest-partial routing when
    either chain fetch errored — cross-chain total is only computed
    when both sides are present.

    cross_check_status='not_applicable' — the two chains measure
    independent issuance pools, not the same fact from two sources, so
    they cannot agree/disagree. The finalized-window flow tool
    (get_rlusd_flow_24h) is where the envelope's cross-check field
    exercises real paired data."""
    import rlusd_live
    try:
        state = rlusd_live.fetch_state()
    except Exception as e:
        raise RuntimeError(f"rlusd_live.fetch_state failed: {e}") from e
    if not state:
        raise RuntimeError("rlusd_live.fetch_state returned empty payload")

    eth = state.get("eth") or {}
    xrpl = state.get("xrpl") or {}
    eth_err = eth.get("error")
    xrpl_err = xrpl.get("error")
    eth_supply = eth.get("supply")
    xrpl_supply = xrpl.get("supply")

    total = None
    if eth_supply is not None and xrpl_supply is not None:
        try:
            total = _decimal_or_none(
                decimal.Decimal(str(eth_supply)) + decimal.Decimal(str(xrpl_supply))
            )
        except (decimal.InvalidOperation, TypeError):
            total = None

    data = {
        "eth": {
            "supply": _decimal_or_none(eth_supply),
            "error": eth_err,
        },
        "xrpl": {
            "supply": _decimal_or_none(xrpl_supply),
            "error": xrpl_err,
        },
        "total_supply": total,
        "fetched_at_iso": _ts_to_iso(state.get("fetched_at")),
    }

    partial_reasons = []
    if eth_err or eth_supply is None:
        partial_reasons.append(f"eth: {eth_err or 'no supply value'}")
    if xrpl_err or xrpl_supply is None:
        partial_reasons.append(f"xrpl: {xrpl_err or 'no supply value'}")
    honest_partial = bool(partial_reasons)
    scope_note = None
    if honest_partial:
        scope_note = (
            "cross-chain total unavailable — "
            + "; ".join(partial_reasons)
        )

    envelope = mcp_server.wrap_envelope(
        data,
        source="ethereum_public_rpc+xrpl_public_rpc",
        as_of=_iso_utc_now(),
        freshness_contract="≤ 30min",
        methodology_url="https://xrpldashboard.com/methodology#rlusd",
        claims_ref="rlusd_supply_cross_chain",
        honest_partial=honest_partial,
        scope_note=scope_note,
    )
    mcp_server.stamp_tool_call("get_rlusd_supply")
    return envelope


# ─────────────────────────────────────────────────────────────────────
# 4. get_rlusd_flow_24h
# ─────────────────────────────────────────────────────────────────────

def tool_get_rlusd_flow_24h() -> dict:
    """Return the last finalized 24h XRPL net-change from rlusd_supply_history.

    Finalized-window rule (machine-readable via freshness_contract=
    'finalized_only'): only rows with snapshot_date strictly earlier
    than today-UTC are eligible. Today's partial-day row is invisible
    to this tool — same rule answer_plausibility_walker._evaluate_rlusd
    applies to prevent the 2026-07-22 RLUSD partial-day false-flat class
    of bug from resurfacing through a different reader.

    cross_check_status derivation:
      * `agree`      — stored xrpl_net_change_24h and derived
                       (xrpl_supply[t] − xrpl_supply[t-1]) match within
                       CROSS_CHECK_EPSILON_RLUSD.
      * `disagree`   — the two independently-computed values differ by
                       more than epsilon. This is the shape of a real
                       reconciliation-surface drift (see
                       feedback_reconciliation_surfaces_advance_triggers
                       2026-07-28); an agent reading `disagree` should
                       downweight the number, not just relay it.
      * `not_applicable` — fewer than two finalized rows available (new
                       schema install, PG restore recently completed),
                       so no derived-diff exists to compare against.
                       Scope note names the reason.

    Fetch is bounded to 3 rows: one might be today's in-flight partial,
    so we need the last two FINALIZED rows to compute a diff, plus one
    for the strict-less-than filter to potentially drop.
    """
    import db
    if not db.pg_available():
        raise RuntimeError("get_rlusd_flow_24h: DATABASE_URL not configured")
    raw_history = db.read_rlusd_supply_history(days=3)
    if not raw_history:
        raise RuntimeError(
            "rlusd_supply_history is empty — walker has not yet produced "
            "any rows; no finalized 24h flow can be reported"
        )
    today_utc = datetime.datetime.now(datetime.timezone.utc).date()
    finalized = [r for r in raw_history if r["snapshot_date"] < today_utc]
    if not finalized:
        raise RuntimeError(
            "no finalized rlusd_supply_history rows yet — only today's "
            "in-flight partial-day row is present"
        )

    latest = finalized[0]
    stored_net_change = latest.get("xrpl_net_change_24h")

    cross_check = "not_applicable"
    scope_note = None
    if len(finalized) < 2:
        scope_note = (
            "only one finalized rlusd_supply_history row available — "
            "cross-check requires two finalized rows to derive a diff"
        )
    else:
        prior = finalized[1]
        latest_supply = latest.get("xrpl_supply")
        prior_supply = prior.get("xrpl_supply")
        if latest_supply is None or prior_supply is None or stored_net_change is None:
            scope_note = (
                "one of xrpl_supply[t], xrpl_supply[t-1], or stored "
                "xrpl_net_change_24h is NULL — cannot cross-check"
            )
        else:
            try:
                derived = decimal.Decimal(str(latest_supply)) - decimal.Decimal(str(prior_supply))
                stored = decimal.Decimal(str(stored_net_change))
                if abs(derived - stored) <= CROSS_CHECK_EPSILON_RLUSD:
                    cross_check = "agree"
                else:
                    cross_check = "disagree"
            except (decimal.InvalidOperation, TypeError):
                scope_note = "xrpl_net_change_24h or xrpl_supply not decimal-coercible"

    data = {
        "finalized_window": {
            "snapshot_date": latest["snapshot_date"].isoformat(),
            "rule": "snapshot_date < today_utc",
            "authority_ref": "answer_plausibility_walker._evaluate_rlusd",
        },
        "xrpl_net_change_24h": _decimal_or_none(stored_net_change),
        "xrpl_supply": _decimal_or_none(latest.get("xrpl_supply")),
        "eth_supply": _decimal_or_none(latest.get("eth_supply")),
        "finalized_rows_available": len(finalized),
    }

    # honest_partial only when we couldn't cross-check — the finalized
    # row itself is present and complete. This distinguishes "envelope's
    # cross-check field is idle by data-shape" from "the underlying fact
    # is missing" (which is the raise-path above).
    honest_partial = cross_check == "not_applicable" and scope_note is not None

    envelope = mcp_server.wrap_envelope(
        data,
        source="rlusd_supply_history+xrpl_gateway_balances",
        as_of=_iso_utc_now(),
        freshness_contract="finalized_only",
        methodology_url="https://xrpldashboard.com/methodology#rlusd",
        claims_ref="rlusd_xrpl_net_change_24h",
        cross_check_status=cross_check,
        honest_partial=honest_partial,
        scope_note=scope_note,
    )
    mcp_server.stamp_tool_call("get_rlusd_flow_24h")
    return envelope
