"""The 15 Domain-first `toml` tiers, and the brand-enrolment side effect.

HOW THESE 15 WERE DECIDED. The host came from each issuer's ON-LEDGER
`Domain` field (own node), not from the stored `verified_via` -- so the
reverse leg is true by construction and the forward leg was then checked
with the repo's own `two_way_toml_verifier_walker._find_toml_match`. Over
all 575 untiered rendering entries that gave 15 currency-level passes, 90
issuer-level, 375 with no Domain (240 of them AMM pseudo-accounts, which
carry none) and 95 fetch failures -- every failure a fetch error, never a
content mismatch.

WHY ONLY THE 15 LANDED. `rank_amms.load_verified_brands()` decides brand
protection by a SUBSTRING TEST ON `verified_via`:

    if not any(t in ver for t in ("xrp-ledger.toml", "xrpscan@")): continue

It never reads `tier`. So writing a `.../well-known/xrp-ledger.toml`
citation onto an entry that previously had `None` or a bare host silently
ENROLS that currency_hex as a protected brand. Tiering the six
issuer-level passes (Opulence, DEV, XPN, Xspence, HVI Spike Te, THE Y PASS)
required exactly that write to render their label, and measured 73 -> 79
families. The owner ruled that unacceptable as a side effect, so those six
stay untiered pending an explicit enrolment mechanism.

The 15 here are safe because none of them changes the loader's output:
  * the 7 Axelar entries already matched via `xrpscan@axelar-bridge`, so
    rewriting them to the axelar.foundation TOML keeps them enrolled with
    the same allowlist;
  * BITx, LHT, USDV, MAG, PLR, ZRP and USDC carry bare host URLs
    (`https://bitx.fyi`) which match NEITHER substring, and are left alone;
  * SOIL's citation is left as None for the same reason -- see
    test_held_citations_are_deliberate below.
"""

import json
import os

import pytest

import rank_amms
import token_label as TL

NAMES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "token_names.json")

AXELAR = {"USDC.axl", "USDf", "WBTC", "WETH", "mBTC", "mTBILL", "mXRP"}
TIERED_15 = {
    "SOIL": "axelar.foundation", "USDC.axl": "axelar.foundation",
    "USDf": "axelar.foundation", "WBTC": "axelar.foundation",
    "WETH": "axelar.foundation", "mBTC": "axelar.foundation",
    "mTBILL": "axelar.foundation", "mXRP": "axelar.foundation",
    "BITx": "bitx.fyi", "LHT": "lhtoken.io", "USDV": "valtorum.com",
    "MAG": "xmagnetic.org", "PLR": "xrpillars.com", "ZRP": "zerpmon.world",
    "USDC": "circle.com",
}


@pytest.fixture(scope="module")
def names():
    with open(NAMES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _entry(names, display):
    hits = [v for v in names.values()
            if isinstance(v, dict) and v.get("currency_display") == display
            and (v.get("issuer") or "")]
    assert len(hits) == 1, f"{display}: {len(hits)} records with an issuer"
    return hits[0]


# ── presence: every one of the 15 is tiered, with its evidence ──────
@pytest.mark.parametrize("display", sorted(TIERED_15))
def test_each_of_the_15_is_tiered_toml_with_evidence(names, display):
    e = _entry(names, display)
    assert e.get("tier") == "toml", (display, e.get("tier"))
    ev = e.get("tier_evidence") or {}
    assert ev.get("match_level") == "currency", (display, ev)
    assert ev.get("host_source") == "issuer on-ledger Domain", (display, ev)
    assert TIERED_15[display] in ev.get("toml_url", ""), (display, ev)
    assert ev.get("reverse_leg") is True, (display, ev)
    assert e.get("tier_reason"), display


@pytest.mark.parametrize("display", sorted(TIERED_15))
def test_toml_tier_renders_bare(names, display):
    """`toml` is the top tier: evidence means FEWER words, not more. A
    caveat here would be a warning the evidence does not support."""
    assert TL.label_for(_entry(names, display)) is None, display


@pytest.mark.parametrize("display", sorted(AXELAR))
def test_axelar_citations_point_at_the_reciprocating_host(names, display):
    """These cited `xrpscan@axelar-bridge` -- a provenance note, not the
    host that actually reciprocated. Now they cite axelar.foundation,
    whose TOML names each code AND whose Domain the issuer points back at."""
    via = _entry(names, display).get("verified_via") or ""
    assert via == "https://axelar.foundation/.well-known/xrp-ledger.toml", (display, via)


# ── the side effect this PR exists to avoid ─────────────────────────
def test_brand_protection_family_census_is_exact():
    """PINS THE INCIDENTAL-ENROLMENT SURFACE.

    73 families on main, and this PR keeps it at 73. Because enrolment is a
    substring test on `verified_via` and nothing more, ANY future citation
    edit can move brand protection without touching a line of brand code.
    This assertion is the tripwire: if it goes red, a curation pass has
    changed which tickers are brand-protected, and that needs an owner
    decision rather than a number bump.

    Of the enrolling entries only 3 are deliberate: the
    `xrpscan@brand-protected` null-canonical sentinels (MIDAS, MTBILL,
    SOIL). 63 owe their enrolment purely to a `.../xrp-ledger.toml`
    curation citation -- ONDO's sentinel among them, since it cites
    ondo.finance's TOML rather than the brand-protected marker -- and 7 to
    an `xrpscan@<other>` provenance note. So all but 3 were enrolled as a
    side effect of describing provenance, not of deciding to protect a
    brand.
    """
    assert len(rank_amms.load_verified_brands()) == 73


def test_held_citations_were_released_once_enrolment_became_explicit(names):
    """SUPERSEDED by the explicit-brands change (2026-11-22).

    This previously asserted SOIL carried NO citation and USDC still cited
    the bare host `https://circle.com`, because under the old loader a TOML
    citation WAS a brand decision:
      SOIL  534F494C... was already a family with an EMPTY allowlist, so
            citing the real Axelar issuer moved [] -> {rfmS3z...}
      USDC  5553444300... was not a family at all; citing it created one
    Enrolment is now declared, so a citation enrols nothing and both can
    carry the host that actually reciprocated. The assertion is moved, not
    deleted: the hold is over and the reason it existed is recorded here.
    """
    assert _entry(names, "SOIL").get("verified_via") == \
        "https://axelar.foundation/.well-known/xrp-ledger.toml"
    assert _entry(names, "USDC").get("verified_via") == \
        "https://circle.com/.well-known/xrp-ledger.toml"


def test_the_six_issuer_level_passes_are_now_tiered(names):
    """Also superseded. These six passed the morning two-way sweep and were
    held ONLY because their label needed a citation and that citation
    silently enrolled six brand families. Issuer level is the ceiling: the
    TOML names the address, never the ticker."""
    for display, host in (("Opulence", "opulencex.io"), ("DEV", "xrpdev.net"),
                          ("XPN", "xrpdev.net"), ("Xspence", "xrpdev.net"),
                          ("HVI Spike Te", "hvi3.com"), ("THE Y PASS", "hvi3.com")):
        e = _entry(names, display)
        assert e.get("tier") == "domain_fallback", (display, e.get("tier"))
        assert TL.label_for(e) == f"issuer verified via {host}", display
