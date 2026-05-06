"""Shared pytest fixtures.

The Flask test client is the workhorse of the route tests — it dispatches
requests through the WSGI app without needing a live server. Network
calls (fetch_pulse_cached, etc.) hit the real XRPL nodes during tests;
that's intentional for now since cache TTLs are short and we want to
catch real upstream contract drift. If test runs become flaky or slow,
introduce monkeypatch fixtures per call site rather than a blanket mock.
"""

import pytest

import app as app_module


@pytest.fixture
def client():
    """Flask test client. Each test gets a fresh client; app state is
    process-global so don't mutate config inside a test."""
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# A real XRPL address that's well-known and stable: Bitstamp cold wallet.
# Used as a fixture for /wallet/<address> and as the issuer side of
# /token/<currency>/<issuer> tests so we exercise the live data paths
# rather than the validation-failure short-circuits.
@pytest.fixture
def known_wallet():
    return "rEhxGqkqPPSxQ3P25J66ft5TwpzV14k2de"


@pytest.fixture
def known_token():
    # USD issued by Bitstamp — historically the largest stablecoin on XRPL.
    return ("USD", "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B")
