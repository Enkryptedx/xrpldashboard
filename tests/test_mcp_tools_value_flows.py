"""Day 3 value-flow tool tests.

Three things verified beyond the shared envelope-shape guarantee
(already exercised by tests/test_mcp_tools_ledger.py):

  1. `get_whale_events` / `get_whale_watchlist` — envelope emitted,
     row shape mirrors db.read_recent_events, watchlist tool filters
     type='tagged' correctly, DATABASE_URL-unset raises rather than
     stubbing.

  2. `get_rlusd_supply` — honest_partial routing engages when either
     chain fetch errored; cross-chain total is None (not fabricated)
     when either side is unavailable.

  3. `get_rlusd_flow_24h` — THE Day 3 acceptance test. Verifies:
       * freshness_contract='finalized_only' is emitted (the machine-
         readable form of the R1/R2 finalized-window rule).
       * today-UTC row is filtered out of consideration (finalized-
         only rule active — the 2026-07-22 partial-day bug class
         cannot resurface through this reader).
       * cross_check_status='agree' when stored xrpl_net_change_24h
         matches derived xrpl_supply[t]−xrpl_supply[t-1] within
         epsilon; 'disagree' when it doesn't; 'not_applicable' with
         scope_note when <2 finalized rows or NULLs block the diff.
       * Empty history raises RuntimeError — absence IS the signal,
         no stub envelope escape hatch.

Underlying db + rlusd_live calls are monkeypatched; no PG or RPC
touched.
"""
from __future__ import annotations

import datetime
import decimal

import pytest

import mcp_server
import mcp_tools_value_flows


def _install_stamp_noop(monkeypatch):
    monkeypatch.setattr(mcp_server, "stamp_tool_call", lambda name: None)


# ─────────────────────────────────────────────────────────────────────
# get_whale_events
# ─────────────────────────────────────────────────────────────────────

# read_recent_events row order:
#   (tx_hash, ledger_index, ts, type, from_addr, to_addr,
#    amount_drops, currency, issuer, raw_json)
def _make_event_row(ts=1_800_000_000, type_="large_xfer",
                    amount_drops=150_000_000_000, currency=None, issuer=None,
                    tx_hash="TXHASH", ledger=98_000_000):
    return (
        tx_hash, ledger, ts, type_, "rFROM", "rTO",
        amount_drops, currency, issuer, "{}",
    )


def test_get_whale_events_envelope_and_shape(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(
        db, "read_recent_events",
        lambda limit, tagged_floor_drops=None: [
            _make_event_row(type_="large_xfer", amount_drops=250_000_000_000),
            _make_event_row(type_="tagged", amount_drops=500_000_000,
                            tx_hash="TX2"),
        ],
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_value_flows.tool_get_whale_events(limit=10)
    assert set(env.keys()) == {"data", "proof", "server"}
    assert env["proof"]["source"] == "local_rippled_stream_capture"
    assert env["proof"]["freshness_contract"] == "≤ 5min"
    assert env["proof"]["cross_check_status"] == "not_applicable"
    assert env["data"]["count"] == 2
    assert env["data"]["events"][0]["type"] == "large_xfer"
    assert env["data"]["events"][0]["amount_drops"] == 250_000_000_000
    assert env["data"]["events"][0]["ts_iso"].endswith("Z")


def test_get_whale_events_raises_when_pg_unavailable(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: False)
    _install_stamp_noop(monkeypatch)
    with pytest.raises(RuntimeError):
        mcp_tools_value_flows.tool_get_whale_events()


# ─────────────────────────────────────────────────────────────────────
# get_whale_watchlist
# ─────────────────────────────────────────────────────────────────────

def test_get_whale_watchlist_filters_to_tagged_only(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(
        db, "read_recent_events",
        lambda limit, tagged_floor_drops=None: [
            _make_event_row(type_="large_xfer", tx_hash="T1"),
            _make_event_row(type_="tagged", tx_hash="T2", amount_drops=200_000_000),
            _make_event_row(type_="large_xfer", tx_hash="T3"),
            _make_event_row(type_="tagged", tx_hash="T4", amount_drops=None,
                            currency="USDC", issuer="rISSUER"),
        ],
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_value_flows.tool_get_whale_watchlist(limit=25)
    hashes = [e["tx_hash"] for e in env["data"]["events"]]
    assert hashes == ["T2", "T4"]
    assert env["data"]["count"] == 2
    assert env["proof"]["source"] == "local_rippled_stream_capture"


# ─────────────────────────────────────────────────────────────────────
# get_rlusd_supply
# ─────────────────────────────────────────────────────────────────────

def test_get_rlusd_supply_both_chains_ok(monkeypatch):
    import rlusd_live
    monkeypatch.setattr(
        rlusd_live, "fetch_state",
        lambda: {
            "eth": {"supply": decimal.Decimal("500000000"), "error": None},
            "xrpl": {"supply": decimal.Decimal("300000000"), "error": None},
            "fetched_at": 1_800_000_000,
        },
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_value_flows.tool_get_rlusd_supply()
    assert env["proof"]["honest_partial"] is False
    assert env["proof"]["scope_note"] is None
    assert env["data"]["total_supply"] == "800000000"
    assert env["data"]["eth"]["error"] is None


def test_get_rlusd_supply_honest_partial_when_eth_errored(monkeypatch):
    import rlusd_live
    monkeypatch.setattr(
        rlusd_live, "fetch_state",
        lambda: {
            "eth": {"supply": None, "error": "all endpoints failed"},
            "xrpl": {"supply": decimal.Decimal("300000000"), "error": None},
            "fetched_at": 1_800_000_000,
        },
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_value_flows.tool_get_rlusd_supply()
    assert env["proof"]["honest_partial"] is True
    assert "eth" in env["proof"]["scope_note"]
    assert env["data"]["total_supply"] is None  # never fabricated


# ─────────────────────────────────────────────────────────────────────
# get_rlusd_flow_24h — the Day 3 acceptance test
# ─────────────────────────────────────────────────────────────────────

def _hist_row(snapshot_date, xrpl_supply, eth_supply, xrpl_net_change_24h):
    return {
        "snapshot_date": snapshot_date,
        "xrpl_supply": xrpl_supply,
        "eth_supply": eth_supply,
        "xrpl_net_change_24h": xrpl_net_change_24h,
    }


def test_get_rlusd_flow_24h_agree_when_stored_matches_derived(monkeypatch):
    """Founding acceptance: stored xrpl_net_change_24h = 100 matches the
    derived diff xrpl_supply[t]-xrpl_supply[t-1] = 1100 − 1000 = 100."""
    import db
    today = datetime.datetime.now(datetime.timezone.utc).date()
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(
        db, "read_rlusd_supply_history",
        lambda days: [
            # today's in-flight row — must be filtered out by the tool
            _hist_row(today, decimal.Decimal("1200"), decimal.Decimal("2000"),
                      decimal.Decimal("100")),
            _hist_row(today - datetime.timedelta(days=1),
                      decimal.Decimal("1100"), decimal.Decimal("2000"),
                      decimal.Decimal("100")),
            _hist_row(today - datetime.timedelta(days=2),
                      decimal.Decimal("1000"), decimal.Decimal("2000"),
                      decimal.Decimal("50")),
        ],
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_value_flows.tool_get_rlusd_flow_24h()
    assert env["proof"]["freshness_contract"] == "finalized_only"
    assert env["proof"]["cross_check_status"] == "agree"
    # today's in-flight row must not leak into the finalized_window
    assert env["data"]["finalized_window"]["snapshot_date"] == (
        today - datetime.timedelta(days=1)
    ).isoformat()
    assert env["data"]["xrpl_net_change_24h"] == "100"
    assert env["data"]["finalized_rows_available"] == 2


def test_get_rlusd_flow_24h_disagree_when_derived_diverges(monkeypatch):
    import db
    today = datetime.datetime.now(datetime.timezone.utc).date()
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(
        db, "read_rlusd_supply_history",
        lambda days: [
            # stored says +100, but derived says 1100−800 = +300
            _hist_row(today - datetime.timedelta(days=1),
                      decimal.Decimal("1100"), decimal.Decimal("2000"),
                      decimal.Decimal("100")),
            _hist_row(today - datetime.timedelta(days=2),
                      decimal.Decimal("800"), decimal.Decimal("2000"),
                      decimal.Decimal("50")),
        ],
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_value_flows.tool_get_rlusd_flow_24h()
    assert env["proof"]["cross_check_status"] == "disagree"


def test_get_rlusd_flow_24h_not_applicable_when_only_one_finalized_row(monkeypatch):
    import db
    today = datetime.datetime.now(datetime.timezone.utc).date()
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(
        db, "read_rlusd_supply_history",
        lambda days: [
            _hist_row(today, decimal.Decimal("1200"), decimal.Decimal("2000"),
                      decimal.Decimal("100")),
            _hist_row(today - datetime.timedelta(days=1),
                      decimal.Decimal("1100"), decimal.Decimal("2000"),
                      decimal.Decimal("100")),
        ],
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_value_flows.tool_get_rlusd_flow_24h()
    assert env["proof"]["cross_check_status"] == "not_applicable"
    assert env["proof"]["honest_partial"] is True
    assert "one finalized" in (env["proof"]["scope_note"] or "")
    assert env["data"]["finalized_rows_available"] == 1


def test_get_rlusd_flow_24h_raises_when_no_finalized_row(monkeypatch):
    """Absence of a finalized row IS the signal — no stub envelope."""
    import db
    today = datetime.datetime.now(datetime.timezone.utc).date()
    monkeypatch.setattr(db, "pg_available", lambda: True)
    # Only today's partial-day row exists — finalized filter drops it.
    monkeypatch.setattr(
        db, "read_rlusd_supply_history",
        lambda days: [
            _hist_row(today, decimal.Decimal("1200"), decimal.Decimal("2000"),
                      decimal.Decimal("100")),
        ],
    )
    _install_stamp_noop(monkeypatch)

    with pytest.raises(RuntimeError):
        mcp_tools_value_flows.tool_get_rlusd_flow_24h()


def test_get_rlusd_flow_24h_raises_when_history_empty(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "read_rlusd_supply_history", lambda days: [])
    _install_stamp_noop(monkeypatch)

    with pytest.raises(RuntimeError):
        mcp_tools_value_flows.tool_get_rlusd_flow_24h()
