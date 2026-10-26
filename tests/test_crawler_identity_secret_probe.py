"""crawler_identity_check: secret/admin-probe path short-circuit
(branch bot-list-additions-2026-10-26).

Charlie 2026-10-26, from the weekly traffic report: 40 hits on
/credentials carried a UA embedding a full desktop-Chrome string plus
"compatible; OAI-SearchBot/1.4". Real OAI-SearchBot has no reason to
probe a credentials path -- same shape as this module's own motivating
incident (GPTBot + /.env, 2026-09-22), just a second UA class.

These tests pin: the new path check fires BEFORE any rDNS lookup (no
network call needed to catch this), only for requests that also claim a
tracked bot UA, and that `evaluate_request` returns False (not blocking)
while ENFORCE stays off -- same log-only-first discipline as the
existing rDNS path.

Hermetic: no network, no DB (db.write_crawler_forgery_shadow is
monkeypatched away).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import crawler_identity_check as cic  # noqa: E402


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """Never touch Postgres from this test file."""
    import db
    monkeypatch.setattr(db, "write_crawler_forgery_shadow", lambda **kw: None)


def test_is_secret_probe_path_matches_credentials():
    assert cic.is_secret_probe_path("/credentials")
    assert cic.is_secret_probe_path("/settings/credentials")
    assert cic.is_secret_probe_path("/.env")
    assert cic.is_secret_probe_path("/.env.production")
    assert cic.is_secret_probe_path("/dashboard/admin")


def test_is_secret_probe_path_rejects_ordinary_paths():
    for p in ("/", "/amendments", "/pools", "/check", "/contact", None, ""):
        assert not cic.is_secret_probe_path(p)


def test_secret_probe_path_short_circuits_before_rdns(monkeypatch):
    """No rDNS lookup -- and thus no network call -- when the path alone
    already damns the request."""
    called = []
    monkeypatch.setattr(cic, "_rdns_lookup", lambda ip: called.append(ip) or None)
    fake_ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
               "Safari/537.36; compatible; OAI-SearchBot/1.4; "
               "+https://openai.com/searchbot")
    cic.evaluate_request(fake_ua, "203.0.113.5", path="/credentials")
    assert called == [], "the path check must short-circuit before any rDNS lookup"


def test_secret_probe_path_does_not_block_while_enforce_is_off(monkeypatch):
    monkeypatch.setattr(cic, "ENFORCE", False)
    fake_ua = "compatible; OAI-SearchBot/1.4; +https://openai.com/searchbot"
    blocked = cic.evaluate_request(fake_ua, "203.0.113.5", path="/credentials")
    assert blocked is False, "log-only-first discipline: never block before an explicit ENFORCE"


def test_secret_probe_path_would_block_when_enforce_is_on(monkeypatch):
    monkeypatch.setattr(cic, "ENFORCE", True)
    fake_ua = "compatible; OAI-SearchBot/1.4; +https://openai.com/searchbot"
    blocked = cic.evaluate_request(fake_ua, "203.0.113.5", path="/credentials")
    assert blocked is True


def test_secret_probe_path_ignored_for_non_bot_uas(monkeypatch):
    """A real browser hitting /credentials (e.g. a curious human, or our
    own dev poking at a 404) is not a crawler-forgery case -- this module
    only judges requests that CLAIM to be one of the tracked bots."""
    called = []
    monkeypatch.setattr(cic, "_rdns_lookup", lambda ip: called.append(ip) or None)
    ordinary_ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
    blocked = cic.evaluate_request(ordinary_ua, "203.0.113.5", path="/credentials")
    assert blocked is False
    assert called == [], "no bot UA claim -> verify() returns not_a_bot_claim, no rDNS either"


def test_secret_probe_logs_shadow_line(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="crawler_identity_check")
    fake_ua = "compatible; OAI-SearchBot/1.4; +https://openai.com/searchbot"
    cic.evaluate_request(fake_ua, "203.0.113.5", path="/credentials")
    assert any("secret_probe_path" in r.message for r in caplog.records)
