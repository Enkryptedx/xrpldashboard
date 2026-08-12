"""Tests for /contact bot-filter (soft-drop with fake-200).

Coverage:
  - _is_bot_contact_submission: each of the three signatures fires correctly.
  - _is_bot_contact_submission: clean UA + clean message returns (False, "").
  - Contact POST with bot UA → HTTP 200 + submitted=True in template
    (fake receipt) but NO row in contact_inquiries.
  - Contact POST with clean UA + message → HTTP 200 + row persisted
    (real receipt, real insert).
  - Signature matching is case-insensitive.
  - Multiple campaigns: SEO spam, AV-bundle, unclosed-paren, each caught.
"""
from __future__ import annotations

import pytest
import app as app_module
from app import _is_bot_contact_submission


# ── Unit tests for the detection function ─────────────────────────────────────

class TestIsBotContactSubmission:

    def test_unclosed_paren_ua_detected(self):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko; Chrome/131"
        is_bot, sig = _is_bot_contact_submission(ua, "Hello I have a question")
        assert is_bot is True
        assert sig == "unclosed_paren_ua"

    def test_unclosed_paren_case_insensitive(self):
        ua = "MOZILLA/5.0 (KHTML, LIKE GECKO; chrome/130)"
        is_bot, sig = _is_bot_contact_submission(ua, "normal message")
        assert is_bot is True
        assert sig == "unclosed_paren_ua"

    def test_seo_spam_owner_detected(self):
        msg = "Hello Xrpldashboard Com Owner, My name is Tristan and I'm betting you'd like your website"
        is_bot, sig = _is_bot_contact_submission("Mozilla/5.0 (Macintosh) Chrome/131", msg)
        assert is_bot is True
        assert sig == "seo_spam_owner"

    def test_seo_spam_dotcom_variant_detected(self):
        msg = "Hello Xrpldashboard.com Owner, my name is Eric"
        is_bot, sig = _is_bot_contact_submission("Mozilla/5.0 Chrome/130", msg)
        assert is_bot is True
        assert sig == "seo_spam_owner"

    def test_seo_spam_case_insensitive(self):
        msg = "hello XRPLDASHBOARD com owner"
        is_bot, sig = _is_bot_contact_submission("", msg)
        assert is_bot is True
        assert sig == "seo_spam_owner"

    def test_ccleaner_ua_detected(self):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 CCleaner/130.0.0.0"
        is_bot, sig = _is_bot_contact_submission(ua, "Hi, I wanted to know your price.")
        assert is_bot is True
        assert sig == "av_bundle_ua"

    def test_avast_ua_detected(self):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Avast/131.0.0.0"
        is_bot, sig = _is_bot_contact_submission(ua, "Ciao, volevo sapere il tuo prezzo.")
        assert is_bot is True
        assert sig == "av_bundle_ua"

    def test_av_bundle_case_insensitive(self):
        ua = "Chrome/130 SAFARI/537 CCLEANER/130"
        is_bot, sig = _is_bot_contact_submission(ua, "legit-looking message here")
        assert is_bot is True
        assert sig == "av_bundle_ua"

    def test_clean_submission_passes(self):
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        msg = "Hi, I'm a researcher at a university studying XRPL liquidity. Could we talk?"
        is_bot, sig = _is_bot_contact_submission(ua, msg)
        assert is_bot is False
        assert sig == ""

    def test_empty_ua_with_clean_message_passes(self):
        is_bot, sig = _is_bot_contact_submission("", "Genuine message with some length")
        assert is_bot is False

    def test_none_ua_handled_safely(self):
        is_bot, sig = _is_bot_contact_submission(None, "Hello world message")
        assert is_bot is False

    def test_none_message_handled_safely(self):
        is_bot, sig = _is_bot_contact_submission("Mozilla/5.0", None)
        assert is_bot is False

    def test_closed_paren_ua_not_flagged(self):
        # Correct UA — paren closes before the space: (KHTML, like Gecko)
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        is_bot, sig = _is_bot_contact_submission(ua, "Normal question about XRPL data")
        assert is_bot is False


# ── Integration tests via Flask test client ───────────────────────────────────

BOT_UA_CCLEANER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 CCleaner/130.0.0.0"
)
BOT_UA_UNCLOSED = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko; Chrome/131.0.0.0 Safari/537.36)"
)
CLEAN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

BOT_MSG_SEO = "Hello Xrpldashboard Com Owner, My name is Tristan and I'm betting you'd like your website Xrpldashboard.com"
BOT_MSG_PRICE = "Hej, jeg ønskede at kende din pris."
CLEAN_MSG = "I'm an institutional investor looking at XRPL liquidity data. Could we schedule a call to discuss access?"


@pytest.fixture
def client(monkeypatch):
    app_module.app.config["TESTING"] = True
    # Stub out the DB + email functions so tests are hermetic.
    inserted_rows = []
    logged_drops = []

    monkeypatch.setattr("db.insert_contact_inquiry",
                        lambda **kw: inserted_rows.append(kw) or 999)
    monkeypatch.setattr("db.mark_contact_inquiry_alerted", lambda row_id: None)
    monkeypatch.setattr("db.log_contact_bot_drop",
                        lambda ua, sig: logged_drops.append((ua, sig)))
    monkeypatch.setattr("app._send_contact_alert", lambda inquiry: False)

    with app_module.app.test_client() as c:
        c._inserted_rows = inserted_rows
        c._logged_drops = logged_drops
        yield c


_ip_counter = 0

def _post_contact(client, ua=CLEAN_UA, message=CLEAN_MSG, email="test@example.com"):
    # Use a unique remote-addr per call so the per-IP rate limiter bucket
    # (6/hour) never accumulates across tests.
    global _ip_counter
    _ip_counter += 1
    ip = f"10.0.{(_ip_counter >> 8) & 0xFF}.{_ip_counter & 0xFF}"
    return client.post(
        "/contact",
        data={
            "purpose": "general",
            "name": "Test User",
            "email": email,
            "message": message,
            "website": "",  # honeypot empty
        },
        headers={"User-Agent": ua, "X-Forwarded-For": ip},
        environ_base={"REMOTE_ADDR": ip},
    )


def test_bot_ccleaner_gets_fake_200(client):
    r = _post_contact(client, ua=BOT_UA_CCLEANER, message=BOT_MSG_PRICE)
    assert r.status_code == 200
    assert b"submitted" in r.data or b"received" in r.data or b"Thank" in r.data


def test_bot_ccleaner_not_inserted_in_db(client):
    _post_contact(client, ua=BOT_UA_CCLEANER, message=BOT_MSG_PRICE)
    assert len(client._inserted_rows) == 0


def test_bot_ccleaner_drop_logged(client):
    _post_contact(client, ua=BOT_UA_CCLEANER, message=BOT_MSG_PRICE)
    assert len(client._logged_drops) == 1
    ua_logged, sig_logged = client._logged_drops[0]
    assert "ccleaner" in ua_logged.lower()
    assert sig_logged == "av_bundle_ua"


def test_bot_seo_spam_gets_fake_200(client):
    r = _post_contact(client, ua=CLEAN_UA, message=BOT_MSG_SEO)
    assert r.status_code == 200


def test_bot_seo_spam_not_inserted(client):
    _post_contact(client, ua=CLEAN_UA, message=BOT_MSG_SEO)
    assert len(client._inserted_rows) == 0


def test_bot_seo_spam_drop_logged(client):
    _post_contact(client, ua=CLEAN_UA, message=BOT_MSG_SEO)
    assert len(client._logged_drops) == 1
    _, sig = client._logged_drops[0]
    assert sig == "seo_spam_owner"


def test_bot_unclosed_paren_gets_fake_200(client):
    r = _post_contact(client, ua=BOT_UA_UNCLOSED, message=CLEAN_MSG)
    assert r.status_code == 200


def test_bot_unclosed_paren_not_inserted(client):
    _post_contact(client, ua=BOT_UA_UNCLOSED, message=CLEAN_MSG)
    assert len(client._inserted_rows) == 0


def test_bot_unclosed_paren_drop_logged(client):
    _post_contact(client, ua=BOT_UA_UNCLOSED, message=CLEAN_MSG)
    _, sig = client._logged_drops[0]
    assert sig == "unclosed_paren_ua"


def test_clean_submission_inserts_row(client):
    r = _post_contact(client, ua=CLEAN_UA, message=CLEAN_MSG)
    assert r.status_code == 200
    assert len(client._inserted_rows) == 1


def test_clean_submission_no_drop_logged(client):
    _post_contact(client, ua=CLEAN_UA, message=CLEAN_MSG)
    assert len(client._logged_drops) == 0


def test_clean_submission_passes_through(client):
    """Verify the real submission path: insert called with correct fields."""
    _post_contact(client, ua=CLEAN_UA, message=CLEAN_MSG, email="real@example.com")
    assert len(client._inserted_rows) == 1
    kw = client._inserted_rows[0]
    assert kw["email"] == "real@example.com"
    assert kw["message"] == CLEAN_MSG


def test_honeypot_still_silently_drops(client):
    """Existing honeypot (website field) still fires before the bot filter."""
    r = client.post(
        "/contact",
        data={"purpose": "general", "name": "X", "email": "x@x.com",
              "message": CLEAN_MSG, "website": "http://spam.com"},
        headers={"User-Agent": CLEAN_UA},
    )
    # Honeypot returns the template without saving — should be 200 with
    # submitted=True (or same success template) and no DB insert.
    assert r.status_code == 200
    assert len(client._inserted_rows) == 0
