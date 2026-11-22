"""The 83 SIVAX serials are suppressed, and why.

WHAT THEY ARE. One publisher (www.sivax.io) self-attests 83 MPT serials
across two issuers: rM7ffj9GZV41K8fWUhtpfZSZvYoZB2yA4t (80) and
rNyrL3hjvM3mDYtTDhHLWMiGtbnV2wdBv6 (1) -- SIV1..SIV135 plus SVXV1 and
SXRLV1. They reached ISSUER level in the Domain-first sweep: the site's
TOML names the addresses and each issuer's on-ledger Domain points back at
sivax.io, so the citation is real.

WHY SUPPRESS ANYWAY. Read from our own node 2026-11-22:

  rM7ffj9...   gateway_balances obligations: 0 entries
               account_lines: 1 trustline, and it is RLUSD -- not any SIV
  rNyrL3hj...  obligations: 0 entries, 0 trustlines

Zero holders and zero outstanding supply for all 83. They are MPT keys, so
`toml` is unreachable and issuer level is the ceiling regardless. And SIV1
is DUPLICATED at rM7ffj9 (0601A58E... and 0601A594..., sequential issuance
IDs, same display), which trips the (currency_display, issuer) collision
guard that _resolve_display_label_to_hex depends on.

Suppression is not a verdict that the publisher is dishonest. It keeps 83
holderless serials from one operator out of a shared namespace.
"""

import json
import os

import pytest

import rank_amms

NAMES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "token_names.json")
ISSUERS = {"rM7ffj9GZV41K8fWUhtpfZSZvYoZB2yA4t",
           "rNyrL3hjvM3mDYtTDhHLWMiGtbnV2wdBv6"}


@pytest.fixture(scope="module")
def names():
    with open(NAMES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _sivax(names):
    return {k: v for k, v in names.items()
            if isinstance(v, dict) and v.get("issuer") in ISSUERS}


def test_all_83_are_suppressed(names):
    rows = _sivax(names)
    assert len(rows) == 83, len(rows)
    unsuppressed = [k for k, v in rows.items() if not v.get("suppressed")]
    assert unsuppressed == [], unsuppressed


def test_reason_is_a_real_reason_not_the_curation_sentinel(names):
    """Section 5: a suppressed entry names the evidence class it actually
    satisfies. 'TODO_curation_pass' would say the opposite -- that nobody
    has looked yet."""
    for k, v in _sivax(names).items():
        reason = v.get("suppressed") or ""
        assert reason != "TODO_curation_pass", k
        assert "zero holders" in reason, (k, reason)
        assert "zero outstanding obligations" in reason, (k, reason)
        assert "2026-11-22" in reason, (k, reason)


def test_none_of_them_renders(names):
    """Suppression is the mechanism that keeps them off the surfaces; the
    citation stays in the record so the decision remains auditable."""
    rendering = [k for k, v in _sivax(names).items()
                 if v.get("verified_via") != "TODO_curation_pass"
                 and not v.get("suppressed")]
    assert rendering == [], rendering


def test_the_siv1_display_collision_is_gone(names):
    """Two renderable records cannot share (currency_display, issuer):
    _resolve_display_label_to_hex returns the FIRST hit, so a shareable
    /token/SIV1/<issuer> URL would resolve to an arbitrary one of them."""
    groups = {}
    for v in names.values():
        if not isinstance(v, dict) or v.get("suppressed"):
            continue
        key = (str(v.get("currency_display") or "").lower(), v.get("issuer"))
        if key[0]:
            groups.setdefault(key, set()).add(v.get("currency_hex"))
    assert ("siv1", "rM7ffj9GZV41K8fWUhtpfZSZvYoZB2yA4t") not in groups


def test_brand_protection_is_untouched():
    """None of the 83 carried a verified_via at all, so none of them was
    ever enrolled. Suppressing them must not move the family census."""
    assert len(rank_amms.load_verified_brands()) == 73
