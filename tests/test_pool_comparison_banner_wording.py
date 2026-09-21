"""Pool-comparison banner wording (Charlie ruling 2026-09-20 late-late).

On a ticker-collision token detail page the banner reads the CANONICAL
ISSUER'S name in possessive form, not the token's brand name.
- RLUSD is issued by "Ripple" → the banner reads "Ripple's RLUSD",
  NOT "Ripple USD's RLUSD" (Ripple USD is the token brand, not the org).
- USDC is issued by "Circle" → "Circle's USDC", not "Circle USDC's USDC".

Data lives in `ticker_canonical_issuers.json` as `issuer_name`; if
absent the pool_comparison dict falls back to `brand`. Adding a new
canonical ticker without `issuer_name` won't fail this test — this
test asserts the shape for the tickers we have annotated today.
"""
from __future__ import annotations

import pytest

import app as app_module


IMPOSTOR_RLUSD = "/token/524C555344000000000000000000000000000000/rLUSDtykL2NVz3HJe1Jqoc7dsxWFVcsmuK"


@pytest.fixture(scope="module")
def client():
    return app_module.app.test_client()


def test_rlusd_impostor_banner_uses_issuer_possessive(client):
    r = client.get(IMPOSTOR_RLUSD)
    assert r.status_code == 200
    body = r.data.decode()
    assert "not Ripple's RLUSD" in body, (
        "Expected banner to read 'not Ripple's RLUSD' (issuer-possessive). "
        "If it reads 'not Ripple USD's RLUSD', the template is still "
        "using canonical_brand (token brand) instead of "
        "canonical_issuer_name (issuer org)."
    )
    # The pool-holdings line uses the same possessive.
    assert "Ripple's RLUSD/XRP pool holds" in body, (
        "Expected 'Ripple's RLUSD/XRP pool holds …' — the second "
        "possessive form in the banner must also use the issuer name."
    )
    # Regression: the token brand string must NOT appear in the banner.
    # (Elsewhere on the page the token brand 'Ripple USD' may legitimately
    # render, e.g. the display header, so we anchor on the two banner
    # phrasings.)
    for wrong in ("not Ripple USD's RLUSD", "Ripple USD's RLUSD/XRP pool holds"):
        assert wrong not in body, (
            f"Regression: banner is using the token brand instead of the "
            f"issuer name — found {wrong!r} on {IMPOSTOR_RLUSD}."
        )


def test_registry_has_issuer_name_where_canonical_issuer_set():
    """Any ticker with a canonical_issuers list must expose an
    issuer_name so the possessive comparison reads as the org name.
    Fallback to brand still works — this test surfaces tickers we
    should annotate BEFORE they end up in the banner rendering.
    """
    from token_data import _load_canonical_registry
    reg = _load_canonical_registry()
    missing = []
    for ticker, entry in reg.items():
        if entry.get("canonical_issuers"):
            if not entry.get("issuer_name"):
                missing.append(ticker)
    assert not missing, (
        f"Tickers with canonical_issuers but no issuer_name: {missing}. "
        f"Add 'issuer_name' to the registry entry so the pool-comparison "
        f"banner reads as the issuer's org, not the token brand."
    )
