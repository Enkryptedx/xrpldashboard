"""Day 2 ledger-primitives tool tests.

Two things being verified:

  1. The envelope shape emitted by each tool is a valid `wrap_envelope`
     output — no tool can bypass or degrade the envelope contract
     (empty source, null as_of, non-https methodology_url, unknown
     freshness_contract all still raise even when routed through a
     tool wrapper).

  2. On failure of the underlying fetch (RPC timeout, malformed
     response, cache empty), the tool RAISES rather than returning a
     stub envelope. This is load-bearing for the Q1 mitigation: a
     silent stub would let the tool APPEAR healthy while returning
     lies — the whole point of the Day 2 batch is to exercise the
     envelope on real reads and inherit their failure modes.

Underlying HTTP / cache calls are monkeypatched — this test file
does NOT hit the live XRPL node. The existing conftest fixtures let
network-touching tests be network-touching; these are unit tests of
the wrapping+stamping layer, not integration tests of the underlying
fetchers.
"""
from __future__ import annotations

import pytest

import mcp_server
import mcp_tools_ledger


# ─────────────────────────────────────────────────────────────────────
# get_ledger_stats
# ─────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _install_server_info_response(monkeypatch, info_block):
    def _fake_post(url, json=None, timeout=None):  # noqa: ARG001
        return _FakeResp({"result": {"status": "success", "info": info_block}})
    monkeypatch.setattr(mcp_tools_ledger.httpx, "post", _fake_post)


def _install_stamp_noop(monkeypatch):
    """Suppress the walker_health write in unit tests — the write path
    is exercised by db.py's own tests."""
    monkeypatch.setattr(mcp_server, "stamp_tool_call", lambda name: None)


def test_get_ledger_stats_returns_valid_envelope(monkeypatch):
    _install_server_info_response(monkeypatch, {
        "validated_ledger": {"seq": 98765432, "close_time": 800000000},
        "server_state": "full",
        "load_factor": 1.0,
        "complete_ledgers": "32570-98765432",
        "build_version": "2.6.0",
        "hostid": "TESTHOST",
    })
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_ledger.tool_get_ledger_stats("https://example.invalid")

    # Envelope shape
    assert set(env.keys()) == {"data", "proof", "server"}
    assert env["proof"]["source"] == "local_rippled"
    assert env["proof"]["freshness_contract"] == "≤ 5min"
    assert env["proof"]["methodology_url"].startswith("https://")
    assert env["proof"]["claims_ref"] == "ledger_stats_live"

    # Data pass-through
    assert env["data"]["validated_ledger_index"] == 98765432
    assert env["data"]["server_state"] == "full"
    assert env["data"]["build_version"] == "2.6.0"


def test_get_ledger_stats_raises_on_upstream_error(monkeypatch):
    def _fake_post(url, json=None, timeout=None):  # noqa: ARG001
        return _FakeResp({"result": {"status": "error", "error": "srvNotReady"}})
    monkeypatch.setattr(mcp_tools_ledger.httpx, "post", _fake_post)
    _install_stamp_noop(monkeypatch)

    with pytest.raises(RuntimeError):
        mcp_tools_ledger.tool_get_ledger_stats("https://example.invalid")


# ─────────────────────────────────────────────────────────────────────
# get_amendment_status
# ─────────────────────────────────────────────────────────────────────

def test_get_amendment_status_agree(monkeypatch):
    import amendments_state
    monkeypatch.setattr(
        amendments_state, "fetch_amendments_state_cached",
        lambda: {
            "ok": True,
            "enabled": [{"hash": "A", "name": "AmendA"}],
            "unrecognized_enabled": [],
            "in_flight": [{"hash": "B", "name": "AmendB"}],
            "superseded": [],
        },
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_ledger.tool_get_amendment_status()
    assert env["proof"]["cross_check_status"] == "agree"
    assert env["data"]["enabled_count"] == 1
    assert env["data"]["unrecognized_enabled_count"] == 0


def test_get_amendment_status_disagree_when_unrecognized_present(monkeypatch):
    import amendments_state
    monkeypatch.setattr(
        amendments_state, "fetch_amendments_state_cached",
        lambda: {
            "ok": True,
            "enabled": [{"hash": "A", "name": "AmendA"}],
            "unrecognized_enabled": [{"hash": "Z", "name": None}],
            "in_flight": [],
            "superseded": [],
        },
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_ledger.tool_get_amendment_status()
    assert env["proof"]["cross_check_status"] == "disagree"
    assert env["data"]["enabled_count"] == 2  # 1 recognized + 1 unrecognized
    assert env["data"]["unrecognized_enabled_count"] == 1


# ─────────────────────────────────────────────────────────────────────
# get_unl_status
# ─────────────────────────────────────────────────────────────────────

def test_get_unl_status_full_ok_envelope(monkeypatch):
    import network_state
    monkeypatch.setattr(
        network_state, "fetch_network_state_cached",
        lambda: {
            "ok": True,
            "lists": [
                {"key": "ripple", "url": "https://vl.ripple.com", "ok": True,
                 "sequence": 5, "validator_count": 35, "expiration_iso": "2027-01-01T00:00:00Z",
                 "days_remaining": 150, "is_expired": False},
                {"key": "xrplf",  "url": "https://vl.xrplf.org",  "ok": True,
                 "sequence": 3, "validator_count": 34, "expiration_iso": "2026-08-01T00:00:00Z",
                 "days_remaining": 2, "is_expired": False},
            ],
            "overlap": {"both": 33, "ripple_only": 2, "xrplf_only": 1, "union": 36, "jaccard_pct": 91.7},
            "fetched_at_iso": "2026-07-30T10:00:00Z",
        },
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_ledger.tool_get_unl_status()
    assert env["proof"]["honest_partial"] is False
    assert env["proof"]["scope_note"] is None
    assert env["data"]["overlap"]["both"] == 33


def test_get_unl_status_honest_partial_when_one_list_fails(monkeypatch):
    import network_state
    monkeypatch.setattr(
        network_state, "fetch_network_state_cached",
        lambda: {
            "ok": True,
            "lists": [
                {"key": "ripple", "url": "https://vl.ripple.com", "ok": True,
                 "sequence": 5, "validator_count": 35, "expiration_iso": "2027-01-01T00:00:00Z",
                 "days_remaining": 150, "is_expired": False},
                {"key": "xrplf",  "url": "https://vl.xrplf.org",  "ok": False,
                 "error": "connection refused"},
            ],
            "overlap": None,
            "fetched_at_iso": "2026-07-30T10:00:00Z",
        },
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_ledger.tool_get_unl_status()
    assert env["proof"]["honest_partial"] is True
    assert "xrplf" in (env["proof"]["scope_note"] or "")
    assert env["data"]["overlap"] is None
