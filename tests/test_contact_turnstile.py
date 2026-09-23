"""Turnstile guard on /contact + /institutional/contact.

Verifies the fail-closed contract Charlie signed off on 2026-09-23:
  - env missing  → GET renders "temporarily unavailable" line + POST 503
  - env present + bad token → soft-drop as bot (fake-200, no persist)
  - env present + good token → normal path (honeypot, XRPL filter, insert)
The secret never appears in any assertion string; only the public
site key is inspected in template output.
"""
from __future__ import annotations

import pytest

import app as app_module
import turnstile_verify


@pytest.fixture
def client(monkeypatch):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _force_enabled(monkeypatch, verify_result):
    """Enable Turnstile module in-process and stub the network verify."""
    monkeypatch.setattr(turnstile_verify, "TURNSTILE_ENABLED", True)
    monkeypatch.setattr(turnstile_verify, "TURNSTILE_SITE_KEY", "0x_test_site_key")
    monkeypatch.setattr(turnstile_verify, "verify_turnstile",
                        lambda token, remote_ip=None: verify_result)


def _force_disabled(monkeypatch):
    monkeypatch.setattr(turnstile_verify, "TURNSTILE_ENABLED", False)
    monkeypatch.setattr(turnstile_verify, "TURNSTILE_SITE_KEY", "")


def test_contact_get_shows_widget_when_enabled(client, monkeypatch):
    _force_enabled(monkeypatch, (True, "ok"))
    resp = client.get("/contact")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "cf-turnstile" in body
    assert "0x_test_site_key" in body
    assert "challenges.cloudflare.com" in body


def test_contact_get_shows_unavailable_when_disabled(client, monkeypatch):
    _force_disabled(monkeypatch)
    resp = client.get("/contact")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "temporarily unavailable" in body.lower()
    assert "disabled" in body.lower() or "disabled" in body


def test_contact_post_503_when_disabled(client, monkeypatch):
    _force_disabled(monkeypatch)
    resp = client.post("/contact", data={
        "purpose": "general",
        "email": "test@example.com",
        "message": "Testing XRPL relevance is fine here",
    })
    assert resp.status_code == 503


def test_contact_post_soft_drops_bad_token(client, monkeypatch):
    _force_enabled(monkeypatch, (False, "invalid-input-response"))
    resp = client.post("/contact", data={
        "purpose": "general",
        "cf-turnstile-response": "bad-token",
        "email": "test@example.com",
        "message": "Testing XRPL relevance is fine here",
    })
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Message received" in body or "Saved to" in body


def test_contact_post_good_token_reaches_validator(client, monkeypatch):
    """A good Turnstile token gets past layer 1; the request then hits
    the existing email/message validators (empty email → 400). We don't
    exercise DB persistence here — that's the write path's own tests."""
    _force_enabled(monkeypatch, (True, "ok"))
    resp = client.post("/contact", data={
        "purpose": "general",
        "cf-turnstile-response": "good-token",
        "email": "not-an-email",  # trip layer-2 email validator
        "message": "Testing XRPL relevance is fine here",
    })
    assert resp.status_code == 400
    assert b"valid email" in resp.data


def test_institutional_get_shows_widget_when_enabled(client, monkeypatch):
    _force_enabled(monkeypatch, (True, "ok"))
    resp = client.get("/institutional/contact")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "cf-turnstile" in body
    assert "0x_test_site_key" in body


def test_institutional_post_503_when_disabled(client, monkeypatch):
    _force_disabled(monkeypatch)
    resp = client.post("/institutional/contact", data={
        "email": "test@example.com",
        "message": "Testing XRPL relevance is fine here",
    })
    assert resp.status_code == 503


def test_turnstile_verify_module_empty_token(monkeypatch):
    """Verify helper returns disabled/missing_token without any network call."""
    monkeypatch.setattr(turnstile_verify, "TURNSTILE_ENABLED", False)
    ok, reason = turnstile_verify.verify_turnstile("some-token")
    assert ok is False
    assert reason == "disabled"

    monkeypatch.setattr(turnstile_verify, "TURNSTILE_ENABLED", True)
    ok, reason = turnstile_verify.verify_turnstile("")
    assert ok is False
    assert reason == "missing_token"
