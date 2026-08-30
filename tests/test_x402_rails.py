"""Tests for x402 rails-dark middleware (x402_rails.py).

Coverage matrix:
    - enforcement=off (default) → wrapper is a pass-through.
    - enforcement=dry_run + missing X-PAYMENT → 402 with correct shape.
    - enforcement=dry_run + valid X-PAYMENT (mocked facilitator) → 200
      with X-PAYMENT-RECEIPT header.
    - enforcement=on + sovereignty=own_node → mainnet-effective mode.
    - enforcement=on + sovereignty=public_infra_dependent → Fence #8
      downgrades to dry_run (never reaches mainnet).
    - enforcement=on + sovereignty=third_party_derived → Fence #8
      downgrades to dry_run (never reaches mainnet).
    - Decoration with unknown sovereignty_class → ValueError at
      decoration time (not at first request).
    - Enforcement active but X402_PAY_TO unset → 500 misconfigured,
      never silent payment routing to zero address.
    - Unknown X402_ENFORCEMENT value → coerced to "off" (fail safe,
      not fail paid).

No real facilitator calls. The `_verify_payment_header` function is
monkey-patched in the "valid payment" case to simulate a successful
receipt without needing the x402-xrpl package installed or a live
testnet round-trip.
"""
from __future__ import annotations

import json

import pytest
from flask import Flask, jsonify

import x402_rails


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every x402_* env var so tests start from a known state.
    Any test that needs a specific value sets it explicitly."""
    for var in (
        "X402_ENFORCEMENT",
        "X402_FACILITATOR_URL",
        "X402_PAY_TO",
        "X402_NETWORK",
        "X402_ASSET",
    ):
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


@pytest.fixture
def app_with_wrapped_route():
    """Build a fresh minimal Flask app with one x402-wrapped route.

    The route's sovereignty class is parameterized via app.config so
    each test can pick own_node / public_infra_dependent /
    third_party_derived without rebuilding the whole app."""
    def _factory(sovereignty_class: str, price_drops=0):
        flask_app = Flask(__name__)
        flask_app.config["TESTING"] = True

        @flask_app.route("/api/x402_test_route")
        @x402_rails.x402_maybe_require_payment(
            sovereignty_class=sovereignty_class,
            price_drops=price_drops,
            scope_note_url="https://xrpldashboard.com/methodology#for-machine-payments",
        )
        def _route():
            return jsonify({"ok": True, "payload": "sovereign result"})

        return flask_app

    return _factory


# ── Pass-through in off mode ────────────────────────────────────────

def test_enforcement_off_is_passthrough(clean_env, app_with_wrapped_route):
    """Default (off) mode: the route behaves as if the wrapper wasn't
    there. Same 200. Same body. No 402. No receipt header."""
    flask_app = app_with_wrapped_route(x402_rails.SOVEREIGNTY_OWN_NODE)
    with flask_app.test_client() as c:
        r = c.get("/api/x402_test_route")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert "X-PAYMENT-RECEIPT" not in r.headers


def test_enforcement_off_ignores_pay_to_missing(clean_env, app_with_wrapped_route):
    """X402_PAY_TO unset in off mode is fine — the misconfig only
    matters when enforcement would actually route payments."""
    flask_app = app_with_wrapped_route(x402_rails.SOVEREIGNTY_OWN_NODE)
    with flask_app.test_client() as c:
        r = c.get("/api/x402_test_route")
    assert r.status_code == 200


def test_unknown_enforcement_value_coerces_to_off(clean_env, app_with_wrapped_route):
    """A typo in production (X402_ENFORCEMENT=onn) fails safe."""
    clean_env.setenv("X402_ENFORCEMENT", "onn")
    flask_app = app_with_wrapped_route(x402_rails.SOVEREIGNTY_OWN_NODE)
    with flask_app.test_client() as c:
        r = c.get("/api/x402_test_route")
    assert r.status_code == 200


# ── dry_run without payment: 402 shape ──────────────────────────────

def test_dry_run_without_payment_returns_402(clean_env, app_with_wrapped_route):
    clean_env.setenv("X402_ENFORCEMENT", "dry_run")
    clean_env.setenv("X402_PAY_TO", "rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd")
    flask_app = app_with_wrapped_route(
        x402_rails.SOVEREIGNTY_OWN_NODE,
        price_drops=1,
    )
    with flask_app.test_client() as c:
        r = c.get("/api/x402_test_route")
    assert r.status_code == 402
    assert r.headers.get("Cache-Control") == "no-store"
    body = r.get_json()
    assert body["x402Version"] == 1
    assert body["error"] == "payment_required"
    assert body["accepts"][0]["network"] == "xrpl:1"  # testnet in dry_run
    assert body["accepts"][0]["asset"] == "RLUSD"
    assert body["accepts"][0]["payTo"] == "rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd"
    assert body["accepts"][0]["maxAmountRequired"] == "1"
    assert body["accepts"][0]["resource"] == "/api/x402_test_route"
    assert body["scopeNote"] == "https://xrpldashboard.com/methodology#for-machine-payments"


def test_dry_run_pay_to_missing_returns_500(clean_env, app_with_wrapped_route):
    """Enforcement active but no destination configured = misconfig.
    Fail loud (500), never silent payment routing."""
    clean_env.setenv("X402_ENFORCEMENT", "dry_run")
    # Deliberately NOT setting X402_PAY_TO.
    flask_app = app_with_wrapped_route(x402_rails.SOVEREIGNTY_OWN_NODE)
    with flask_app.test_client() as c:
        r = c.get("/api/x402_test_route")
    assert r.status_code == 500
    assert r.get_json()["error"] == "x402_misconfigured"


# ── dry_run with valid mocked payment: 200 + receipt ─────────────────

def test_dry_run_with_valid_payment_returns_200_with_receipt(
    clean_env, app_with_wrapped_route, monkeypatch
):
    clean_env.setenv("X402_ENFORCEMENT", "dry_run")
    clean_env.setenv("X402_PAY_TO", "rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd")

    def _fake_verify(payment_header, requirements):
        return {"tx_hash": "TESTNETTXHASHDEADBEEF", "network": requirements["network"]}

    monkeypatch.setattr(x402_rails, "_verify_payment_header", _fake_verify)
    flask_app = app_with_wrapped_route(x402_rails.SOVEREIGNTY_OWN_NODE)
    with flask_app.test_client() as c:
        r = c.get(
            "/api/x402_test_route",
            headers={"X-PAYMENT": "presigned-tx-blob-testnet"},
        )
    assert r.status_code == 200
    assert r.get_json()["payload"] == "sovereign result"
    assert r.headers.get("X-PAYMENT-RECEIPT") == "TESTNETTXHASHDEADBEEF"


def test_dry_run_with_invalid_payment_returns_402(
    clean_env, app_with_wrapped_route, monkeypatch
):
    clean_env.setenv("X402_ENFORCEMENT", "dry_run")
    clean_env.setenv("X402_PAY_TO", "rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd")

    monkeypatch.setattr(x402_rails, "_verify_payment_header", lambda h, r: None)
    flask_app = app_with_wrapped_route(x402_rails.SOVEREIGNTY_OWN_NODE)
    with flask_app.test_client() as c:
        r = c.get(
            "/api/x402_test_route",
            headers={"X-PAYMENT": "invalid-blob"},
        )
    assert r.status_code == 402


# ── Fence #8: sovereignty gates ─────────────────────────────────────

def test_fence8_public_infra_cannot_reach_on(clean_env):
    """A public_infra_dependent endpoint stays capped at dry_run even
    when X402_ENFORCEMENT=on."""
    clean_env.setenv("X402_ENFORCEMENT", "on")
    effective = x402_rails.effective_enforcement_mode(
        x402_rails.SOVEREIGNTY_PUBLIC_INFRA_DEPENDENT
    )
    assert effective == x402_rails.ENFORCEMENT_DRY_RUN


def test_fence8_third_party_cannot_reach_on(clean_env):
    """A third_party_derived endpoint stays capped at dry_run even
    when X402_ENFORCEMENT=on. It CAN dry_run (rails wired), it CANNOT
    flip to mainnet enforcement."""
    clean_env.setenv("X402_ENFORCEMENT", "on")
    effective = x402_rails.effective_enforcement_mode(
        x402_rails.SOVEREIGNTY_THIRD_PARTY_DERIVED
    )
    assert effective == x402_rails.ENFORCEMENT_DRY_RUN


def test_fence8_own_node_reaches_on(clean_env):
    """An own_node endpoint can reach ON (subject to attorney gate,
    which is not this module's concern)."""
    clean_env.setenv("X402_ENFORCEMENT", "on")
    effective = x402_rails.effective_enforcement_mode(
        x402_rails.SOVEREIGNTY_OWN_NODE
    )
    assert effective == x402_rails.ENFORCEMENT_ON


def test_fence8_off_stays_off_regardless_of_class(clean_env):
    """Off is off — Fence #8 doesn't promote anything upward."""
    for cls in (
        x402_rails.SOVEREIGNTY_OWN_NODE,
        x402_rails.SOVEREIGNTY_PUBLIC_INFRA_DEPENDENT,
        x402_rails.SOVEREIGNTY_THIRD_PARTY_DERIVED,
    ):
        assert x402_rails.effective_enforcement_mode(cls) == x402_rails.ENFORCEMENT_OFF


def test_fence8_dry_run_passthrough_for_all_classes(clean_env):
    """dry_run is valid for every class — it's testnet-only."""
    clean_env.setenv("X402_ENFORCEMENT", "dry_run")
    for cls in (
        x402_rails.SOVEREIGNTY_OWN_NODE,
        x402_rails.SOVEREIGNTY_PUBLIC_INFRA_DEPENDENT,
        x402_rails.SOVEREIGNTY_THIRD_PARTY_DERIVED,
    ):
        assert x402_rails.effective_enforcement_mode(cls) == x402_rails.ENFORCEMENT_DRY_RUN


def test_unknown_sovereignty_class_raises_at_decoration_time():
    """A typo at the callsite is caught at import, not at first
    request. This is the "hard fail" case per Fence #8."""
    with pytest.raises(ValueError, match="unknown sovereignty_class"):
        x402_rails.x402_maybe_require_payment(sovereignty_class="own-node")


def test_unknown_sovereignty_class_raises_from_effective_mode():
    with pytest.raises(ValueError, match="unknown sovereignty_class"):
        x402_rails.effective_enforcement_mode("bogus")


# ── Config helpers ──────────────────────────────────────────────────

def test_dry_run_defaults_to_testnet_facilitator(clean_env):
    clean_env.setenv("X402_ENFORCEMENT", "dry_run")
    assert "testnet" in x402_rails.current_facilitator_url()


def test_on_mode_defaults_to_mainnet_facilitator(clean_env):
    clean_env.setenv("X402_ENFORCEMENT", "on")
    assert "mainnet" in x402_rails.current_facilitator_url()


def test_explicit_facilitator_url_wins(clean_env):
    clean_env.setenv("X402_ENFORCEMENT", "dry_run")
    clean_env.setenv("X402_FACILITATOR_URL", "https://facilitator.example.com")
    assert x402_rails.current_facilitator_url() == "https://facilitator.example.com"


def test_dry_run_defaults_to_testnet_network(clean_env):
    clean_env.setenv("X402_ENFORCEMENT", "dry_run")
    assert x402_rails.current_network() == "xrpl:1"


def test_asset_defaults_to_rlusd(clean_env):
    assert x402_rails.current_asset() == "RLUSD"


# ── Decorator introspection stamps ──────────────────────────────────

def test_wrapper_stamps_sovereignty_class_on_view(clean_env):
    """Introspection stamps let a future /api/x402/registry surface
    enumerate every wired endpoint without re-parsing decorators."""
    @x402_rails.x402_maybe_require_payment(
        sovereignty_class=x402_rails.SOVEREIGNTY_OWN_NODE,
        price_drops=42,
    )
    def _view():
        return "ok"

    assert _view._x402_sovereignty_class == x402_rails.SOVEREIGNTY_OWN_NODE
    assert _view._x402_price_drops == 42
