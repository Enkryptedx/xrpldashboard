"""Tests for notary_endpoints.py Flask blueprint.

Covers the dark-build flag behavior — every endpoint returns 503 when
NOTARY_ENABLED != "1". Also covers the anchor POST shape and spec GET
when enabled.
"""
from __future__ import annotations

import pytest
from flask import Flask

import notary_endpoints as ne


@pytest.fixture
def notary_app():
    """Build a minimal Flask app with the notary blueprint registered."""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(ne.notary_bp)
    return app


@pytest.fixture
def client_disabled(notary_app, monkeypatch):
    monkeypatch.setenv("NOTARY_ENABLED", "0")
    with notary_app.test_client() as c:
        yield c


@pytest.fixture
def client_enabled(notary_app, monkeypatch):
    monkeypatch.setenv("NOTARY_ENABLED", "1")
    with notary_app.test_client() as c:
        yield c


# ── Flag-off: every endpoint returns 503 ─────────────────────────────────────

@pytest.mark.parametrize("method,path,json_body", [
    ("POST", "/notary/anchor",        {"publisher_id": "did:web:test.com", "sha256_digest": "a" * 64}),
    ("GET",  "/notary/receipt/abc",   None),
    ("POST", "/notary/verify",        {"receipt": {}}),
    ("GET",  "/notary/chain/did%3Aweb%3Atest.com", None),
])
def test_disabled_returns_503(client_disabled, method, path, json_body):
    if method == "POST":
        r = client_disabled.post(path, json=json_body)
    else:
        r = client_disabled.get(path)
    assert r.status_code == 503
    body = r.get_json()
    assert body["error"] == "notary_disabled"
    assert "disclaimer" in body
    assert r.headers["Retry-After"] == "604800"
    assert r.headers["Cache-Control"] == "no-store"


def test_disabled_disclaimer_matches_canonical(client_disabled):
    from xrpl_notary_verify import DISCLAIMER
    r = client_disabled.post("/notary/anchor", json={
        "publisher_id": "did:web:test.com",
        "sha256_digest": "b" * 64,
    })
    assert r.get_json()["disclaimer"] == DISCLAIMER


# ── spec GET is always available ──────────────────────────────────────────────

def test_spec_serves_when_disabled(client_disabled):
    r = client_disabled.get("/notary/spec")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enabled"] is False
    assert "disclaimer" in body


def test_spec_serves_when_enabled(client_enabled):
    r = client_enabled.get("/notary/spec")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enabled"] is True


# ── anchor POST shape when enabled ───────────────────────────────────────────

def test_anchor_missing_fields_returns_400(client_enabled):
    r = client_enabled.post("/notary/anchor", json={"publisher_id": "did:web:x.com"})
    assert r.status_code == 400
    body = r.get_json()
    assert "sha256_digest" in body["fields"]


def test_anchor_invalid_digest_returns_400(client_enabled):
    r = client_enabled.post("/notary/anchor", json={
        "publisher_id": "did:web:x.com",
        "sha256_digest": "not_a_hex_sha256",
    })
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_sha256_digest"


def test_anchor_valid_request_returns_200(client_enabled):
    r = client_enabled.post("/notary/anchor", json={
        "publisher_id": "did:web:example.com",
        "sha256_digest": "c" * 64,
        "utc_date": "2026-09-01",
    })
    assert r.status_code == 200
    body = r.get_json()
    assert "receipt" in body
    assert "receipt_url" in body
    assert "chain_url" in body
    assert body["receipt"]["protocol"] == "xrpldashboard/notary/v1"
    assert body["receipt"]["publisher_id"] == "did:web:example.com"
    assert body["receipt"]["sha256_digest"] == "c" * 64
    assert body["receipt"]["onledger_anchor_tx"] == "pending"
    assert body["disclaimer"] == ne.DISCLAIMER


def test_anchor_receipt_id_format(client_enabled):
    r = client_enabled.post("/notary/anchor", json={
        "publisher_id": "did:web:example.com",
        "sha256_digest": "d" * 64,
        "utc_date": "2026-09-15",
    })
    receipt_id = r.get_json()["receipt"]["receipt_id"]
    assert receipt_id.startswith("2026-09-15-")
    assert len(receipt_id) == len("2026-09-15-") + 8


def test_anchor_disclaimer_in_receipt_body(client_enabled):
    from xrpl_notary_verify import DISCLAIMER
    r = client_enabled.post("/notary/anchor", json={
        "publisher_id": "self",
        "sha256_digest": "e" * 64,
    })
    body = r.get_json()
    assert body["receipt"]["disclaimer"] == DISCLAIMER


# ── is_notary_enabled helper ─────────────────────────────────────────────────

def test_is_notary_enabled_off_by_default(monkeypatch):
    monkeypatch.delenv("NOTARY_ENABLED", raising=False)
    assert ne.is_notary_enabled() is False


def test_is_notary_enabled_on(monkeypatch):
    monkeypatch.setenv("NOTARY_ENABLED", "1")
    assert ne.is_notary_enabled() is True


def test_is_notary_enabled_typo_is_false(monkeypatch):
    monkeypatch.setenv("NOTARY_ENABLED", "true")
    assert ne.is_notary_enabled() is False
