"""XLS-26 [[ISSUERS]]/[[TOKENS]] pairing is currency-level evidence.

WHY THIS FILE EXISTS. RLUSD (`524C555344...` @ rMxCKbED...) is Ripple's own
stablecoin and ripple.com publishes its own issuer, but ripple.com serves
**zero `[[CURRENCIES]]` blocks**. Its token evidence lives in the XLS-26
tables instead:

    [[ISSUERS]]
    address = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
    name = "RLUSD"

    [[TOKENS]]
    issuer = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
    currency = "RLUSD"
    name = "Ripple USD"

XLS-26 ("IOU Token Metadata via xrp-ledger.toml", xls.xrpl.org, status
Draft) defines both tables; the core xrp-ledger.toml spec on xrpl.org does
not mention either. A reader that accepts only the core spec's
`[[CURRENCIES]]` therefore scores Ripple's first-party naming of its own
issuer as *issuer-level at best*, which is how RLUSD came within one step of
publishing with a "source cited · not yet checked" caveat on 2026-11-22.

The repo verifier already reads all four tables (`_SCANNED_ARRAYS`, added by
7e3337b). These tests pin that behaviour against the real captured file so a
later tidy-up cannot narrow it back to the core spec.
"""

import os
import tomllib

import pytest

import two_way_toml_verifier_walker as W

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "toml")

RLUSD_HEX = "524C555344000000000000000000000000000000"
RLUSD_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_URL = "https://ripple.com/.well-known/xrp-ledger.toml"

# weildx.com is the control: it publishes all four tables, so it must reach
# currency level through the CORE spec table and prove the XLS-26 path is an
# addition rather than a replacement.
WAAPL_HEX = "774141504C000000000000000000000000000000"  # 40 chars
WEILDX_URL = "https://weildx.com/.well-known/xrp-ledger.toml"


def _load(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return tomllib.load(fh)


@pytest.fixture(scope="module")
def ripple_toml():
    return _load("ripple_com.toml")


@pytest.fixture(scope="module")
def weildx_toml():
    return _load("weildx_com.toml")


def test_ripple_com_serves_no_currencies_table(ripple_toml):
    """The premise. If ripple.com ever adds [[CURRENCIES]], the XLS-26 path
    below stops being the only route and this test should be revisited."""
    assert "CURRENCIES" not in ripple_toml
    assert ripple_toml.get("ISSUERS"), "fixture lost its [[ISSUERS]] table"
    assert ripple_toml.get("TOKENS"), "fixture lost its [[TOKENS]] table"


def test_rlusd_is_currency_level_through_the_repo_verifier(ripple_toml):
    """The real code path, not a re-implementation of it."""
    block, level = W._find_toml_match(ripple_toml, RLUSD_HEX, RLUSD_ISSUER)
    assert level == W.MATCH_CURRENCY, (
        f"RLUSD scored {level!r}; XLS-26 [[TOKENS]] pairs code+issuer so this "
        f"must be currency level"
    )
    assert block is not None
    assert block.get("issuer") == RLUSD_ISSUER
    # The pairing is what earns currency level: the block names the code too.
    assert "RLUSD" in W._block_codes(block)


def test_rlusd_pairing_comes_from_an_xls26_table(ripple_toml):
    """Locate the matched block so a silent fallback to a weaker table is
    visible. [[ACCOUNTS]] on ripple.com lists 20 escrow/ops wallets and
    never the RLUSD issuer, so an [[ACCOUNTS]] match here would be a bug."""
    block, _level = W._find_toml_match(ripple_toml, RLUSD_HEX, RLUSD_ISSUER)
    in_tables = [name for name in W._SCANNED_ARRAYS
                 for b in (ripple_toml.get(name) or []) if b is block]
    assert in_tables, "matched block belongs to no scanned table"
    assert in_tables[0] in ("ISSUERS", "TOKENS"), in_tables

    accounts = [b.get("address") for b in (ripple_toml.get("ACCOUNTS") or [])]
    assert RLUSD_ISSUER not in accounts, (
        "fixture changed: the RLUSD issuer is now in [[ACCOUNTS]], so this "
        "no longer isolates the XLS-26 path"
    )


def test_core_spec_table_still_reaches_currency_level(weildx_toml):
    """Control: XLS-26 support must not have displaced [[CURRENCIES]]."""
    issuer = "rsscn6GdUxqkCbtmcA7uTbYyXWfxxY6Dqo"
    block, level = W._find_toml_match(weildx_toml, WAAPL_HEX, issuer)
    assert level == W.MATCH_CURRENCY
    assert block.get("issuer") == issuer


def test_reverse_leg_accepts_a_url_shaped_domain():
    """RLUSD's on-ledger Domain is 'https://ripple.com/' -- scheme and
    trailing slash, not a bare host. Both are valid identity claims."""
    assert W._domain_matches_toml_url("https://ripple.com/", RLUSD_URL)
    assert W._domain_matches_toml_url("ripple.com", RLUSD_URL)
    # And it must still reject a different host.
    assert not W._domain_matches_toml_url("https://example.com/", RLUSD_URL)


def test_wrong_issuer_does_not_match_rlusd(ripple_toml):
    """Fail-closed: the code alone must never be enough."""
    _block, level = W._find_toml_match(
        ripple_toml, RLUSD_HEX, "rMhkqz3DeU7GUUJKGZofusbrTwZe6bDyb1")
    assert level != W.MATCH_CURRENCY
