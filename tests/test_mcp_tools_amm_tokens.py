"""Day 4 AMM + token attestation tool tests.

Four things verified beyond the shared envelope-shape guarantee already
exercised by tests/test_mcp_tools_ledger.py and value_flows:

  1. `get_amm_pool` / `get_amm_top_by_tvl` — envelope emitted, filter
     matches by amm_account, top-N sorts by tvl_usd (NULLs last),
     absence of the requested account raises rather than stubbing.

  2. `get_token_attestation` — three-state tier classifier (verified /
     self-described / null); `dispute_contact_url` present in the data
     payload (the third-party-naming discipline).

  3. `get_rwa_families` / `get_rwa_pools` — envelope emitted, dispute
     URL present, absence of PG raises.

  4. `get_mpt_snapshot` — envelope emitted, empty-snapshot raises
     rather than stubbing (absence IS the signal), NO dispute URL
     (not third-party-naming).

Underlying db calls are monkeypatched; no PG touched.
"""
from __future__ import annotations

import decimal

import pytest

import mcp_server
import mcp_tools_amm_tokens


def _install_stamp_noop(monkeypatch):
    monkeypatch.setattr(mcp_server, "stamp_tool_call", lambda name: None)


def _pool_row(amm_account="rAMM1", tvl_usd=decimal.Decimal("1000000"),
              tvl_status="ok", pair="XRP/USD", snapshot_ts=1_800_000_000):
    return {
        "amm_account": amm_account,
        "pair": pair,
        "fee_pct": decimal.Decimal("0.3"),
        "fee_raw": 500,
        "amount_a": decimal.Decimal("100"),
        "amount_b": decimal.Decimal("200"),
        "asset_a": {"currency": "XRP"},
        "asset_b": {"currency": "USD", "issuer": "rISS"},
        "tvl_usd": tvl_usd,
        "tvl_status": tvl_status,
        "kind": "amm",
        "_snapshot_ts": snapshot_ts,
    }


# ─────────────────────────────────────────────────────────────────────
# get_amm_pool
# ─────────────────────────────────────────────────────────────────────

def test_get_amm_pool_envelope_and_shape(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(
        db, "read_amm_ranked_pools",
        lambda: [_pool_row(amm_account="rAMM1"), _pool_row(amm_account="rAMM2")],
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_amm_tokens.tool_get_amm_pool("rAMM1")
    assert set(env.keys()) == {"data", "proof", "server"}
    assert env["proof"]["source"] == "rank_amms_walker+amm_tvl_recorder"
    assert env["proof"]["freshness_contract"] == "≤ 30min"
    assert env["proof"]["methodology_url"].endswith("#amm")
    assert env["proof"]["claims_ref"] == "amm_pool_by_address"
    assert env["data"]["amm_account"] == "rAMM1"
    assert env["data"]["tvl_usd"] == "1000000"


def test_get_amm_pool_raises_when_not_in_snapshot(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "read_amm_ranked_pools", lambda: [_pool_row("rAMM1")])
    _install_stamp_noop(monkeypatch)
    with pytest.raises(RuntimeError):
        mcp_tools_amm_tokens.tool_get_amm_pool("rUNKNOWN")


def test_get_amm_pool_raises_when_pg_unavailable(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: False)
    _install_stamp_noop(monkeypatch)
    with pytest.raises(RuntimeError):
        mcp_tools_amm_tokens.tool_get_amm_pool("rAMM1")


# ─────────────────────────────────────────────────────────────────────
# get_amm_top_by_tvl
# ─────────────────────────────────────────────────────────────────────

def test_get_amm_top_by_tvl_sorts_desc_null_last(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "read_amm_snapshot_ts", lambda: 1_800_000_000)
    monkeypatch.setattr(
        db, "read_amm_ranked_pools",
        lambda: [
            _pool_row("rA", tvl_usd=decimal.Decimal("100")),
            _pool_row("rB", tvl_usd=decimal.Decimal("5000")),
            _pool_row("rC", tvl_usd=None),
            _pool_row("rD", tvl_usd=decimal.Decimal("1000")),
        ],
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_amm_tokens.tool_get_amm_top_by_tvl(limit=10)
    accts = [p["amm_account"] for p in env["data"]["pools"]]
    # 5000 > 1000 > 100 > NULL
    assert accts == ["rB", "rD", "rA", "rC"]
    assert env["data"]["count"] == 4
    assert env["proof"]["claims_ref"] == "amm_top_by_tvl_rank"


def test_get_amm_top_by_tvl_limit_clamped(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "read_amm_snapshot_ts", lambda: 1_800_000_000)
    monkeypatch.setattr(
        db, "read_amm_ranked_pools",
        lambda: [_pool_row(f"r{i}", tvl_usd=decimal.Decimal(str(1000 - i)))
                 for i in range(20)],
    )
    _install_stamp_noop(monkeypatch)
    env = mcp_tools_amm_tokens.tool_get_amm_top_by_tvl(limit=5)
    assert env["data"]["count"] == 5


# ─────────────────────────────────────────────────────────────────────
# get_token_attestation
# ─────────────────────────────────────────────────────────────────────

def test_get_token_attestation_verified_when_source_toml(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(
        db, "read_account_labels",
        lambda addrs: {
            "rISS": {
                "address": "rISS",
                "name": "ACME Corp",
                "category": "mpt_issuer",
                "source": "toml",
                "confidence": 1.0,
                "extra": {"domain": "acme.example"},
            }
        },
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_amm_tokens.tool_get_token_attestation("USD", "rISS")
    assert env["data"]["attestation_tier"] == "verified"
    assert env["data"]["issuer_name"] == "ACME Corp"
    assert env["data"]["issuer_domain"] == "acme.example"
    assert env["data"]["dispute_contact_url"] == mcp_tools_amm_tokens.DISPUTE_CONTACT_URL
    assert env["proof"]["claims_ref"] == "token_attestation_status"


def test_get_token_attestation_self_described_when_non_toml_source(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(
        db, "read_account_labels",
        lambda addrs: {
            "rISS": {
                "address": "rISS",
                "name": "Bridge Gateway",
                "category": "bridge",
                "source": "derived:axelar",
                "confidence": 0.9,
                "extra": {},
            }
        },
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_amm_tokens.tool_get_token_attestation("USDC", "rISS")
    assert env["data"]["attestation_tier"] == "self-described"


def test_get_token_attestation_null_when_no_label(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "read_account_labels", lambda addrs: {})
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_amm_tokens.tool_get_token_attestation("XYZ", "rISS")
    assert env["data"]["attestation_tier"] is None
    assert env["data"]["attestation_tier_reason"] == "no account_labels row for issuer"
    # Dispute URL still present even when tier is null — absence of a label
    # is itself something an issuer might want to correct.
    assert env["data"]["dispute_contact_url"] == mcp_tools_amm_tokens.DISPUTE_CONTACT_URL


# ─────────────────────────────────────────────────────────────────────
# get_rwa_families
# ─────────────────────────────────────────────────────────────────────

def test_get_rwa_families_envelope_and_dispute_url(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(
        db, "read_rwa_families",
        lambda: [
            {
                "family_slug": "acme-treasury",
                "family_name": "ACME Treasury",
                "description": "Tokenized treasury bills",
                "external_url": "https://acme.example",
                "attestation_level": "verified",
                "pool_count": 3,
            },
        ],
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_amm_tokens.tool_get_rwa_families()
    assert env["data"]["count"] == 1
    assert env["data"]["families"][0]["family_slug"] == "acme-treasury"
    assert env["data"]["dispute_contact_url"] == mcp_tools_amm_tokens.DISPUTE_CONTACT_URL
    assert env["proof"]["source"] == "rwa_family+rwa_pool_attribution"
    assert env["proof"]["methodology_url"].endswith("#rwa")


def test_get_rwa_families_raises_when_pg_returns_none(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "read_rwa_families", lambda: None)
    _install_stamp_noop(monkeypatch)
    with pytest.raises(RuntimeError):
        mcp_tools_amm_tokens.tool_get_rwa_families()


# ─────────────────────────────────────────────────────────────────────
# get_rwa_pools
# ─────────────────────────────────────────────────────────────────────

def test_get_rwa_pools_envelope_and_tvl_join(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(
        db, "read_rwa_pools_attributed",
        lambda: [
            {
                "pool_address": "rPOOL1",
                "family_slug": "acme-treasury",
                "confidence": "high",
                "provenance": "TOML declaration + issuer domain",
                "notes": None,
                "tvl_usd": decimal.Decimal("500000"),
                "tvl_status": "ok",
            },
        ],
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_amm_tokens.tool_get_rwa_pools()
    assert env["data"]["count"] == 1
    assert env["data"]["pools"][0]["pool_address"] == "rPOOL1"
    assert env["data"]["pools"][0]["tvl_usd"] == "500000"
    assert env["data"]["dispute_contact_url"] == mcp_tools_amm_tokens.DISPUTE_CONTACT_URL


# ─────────────────────────────────────────────────────────────────────
# get_mpt_snapshot
# ─────────────────────────────────────────────────────────────────────

def test_get_mpt_snapshot_envelope_and_no_dispute_url(monkeypatch):
    """Not third-party-naming; dispute_contact_url MUST NOT be in the
    data payload — the on-page rule (only surfaces that assign per-party
    labels expose dispute channels) applies here too."""
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(
        db, "read_mpt_snapshot",
        lambda: {
            "ok": True,
            "total": 42,
            "by_class": {"treasury": 30, "stablecoin": 12},
            "snapshot_age_seconds": 3600.0,
            "from_postgres": True,
        },
    )
    _install_stamp_noop(monkeypatch)

    env = mcp_tools_amm_tokens.tool_get_mpt_snapshot()
    assert env["data"]["total_active"] == 42
    assert env["data"]["by_class"]["treasury"] == 30
    assert "dispute_contact_url" not in env["data"]
    assert env["proof"]["source"] == "mpt_snapshot+mpt_holders_refresh"
    assert env["proof"]["methodology_url"].endswith("#mpt")
    assert env["proof"]["claims_ref"] == "mpt_active_count"


def test_get_mpt_snapshot_raises_when_snapshot_empty(monkeypatch):
    """Absence of a snapshot IS the signal — no stub envelope."""
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "read_mpt_snapshot", lambda: None)
    _install_stamp_noop(monkeypatch)
    with pytest.raises(RuntimeError):
        mcp_tools_amm_tokens.tool_get_mpt_snapshot()
