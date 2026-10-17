"""Tier labels for the tokens whose cited TOML was audited 2026-10-17.

LABELS ARE CAVEATS, NOT BADGES. This is the thing to understand before
touching any of it, and it is the opposite of the intuitive reading:

  tier             evidence                              renders
  ---------------  ------------------------------------  ----------------------
  toml             TOML names issuer AND code            bare, no qualifier
  domain_fallback  TOML names the issuer address only    "issuer verified via X"
  self_described   nothing the domain confirms           "self-described
                                                          (unverified)"

More evidence means FEWER words, not more. A fully verified token renders
clean; a qualifier is a warning that something is unproven. The precedent is
pinned in test_curation_walker_9.py: TestCert (self_described) asserts the
qualifier IS present, TestXaga (toml) asserts it is absent -- "a toml-tier
token needs no qualifier" -- and that suite's header states XAGa "earns the
full toml tier and renders bare like any other verified token".

So `domain_fallback`'s "issuer verified via X" is not a badge either. It is
a caveat meaning: we confirmed the address is theirs, but NOT that this
ticker is the one they meant.

WHAT THIS CHANGE DOES. Three entries held a tier below their evidence:

  token    cited TOML            ACCOUNTS  CURRENCIES  on-ledger    level
                                 addr hit  code+iss    Domain
  WST      pay.aiic.nz           0         1           matches      currency
  XRT      xrpturbo.com          1         1           matches      currency
  EVE      evecoin.org           1         1           matches      currency
  XAGa     silver.assetiko.com   1         1           matches      currency
  HVI3ESC  hvi3.com              1         0 (no table) matches     issuer

All three move to `toml`, matching the XAGa precedent, and so stop carrying
qualifiers they had not earned.

WST IS THE CAUTIONARY ONE. The first audit scanned [[ACCOUNTS]],
[[ISSUERS]] and [[TOKENS]] -- never [[CURRENCIES]], which is where the
xrp-ledger.toml spec actually pairs a code with an issuer. On that partial
read WST looked like an unreciprocated one-way claim and was briefly
labelled self_described. pay.aiic.nz names it outright:

  {"code": "WST", "issuer": "rBoZPmku6RKZPvZs1wBAWQgUGFALzw9WST",
   "network": "main", "display_decimals": 2}

Its address IS genuinely absent from ACCOUNTS; the proof lives entirely in
CURRENCIES. One table is never enough.

Hermetic: committed token_names.json only. No network, no DB.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import rank_amms  # noqa: E402
import token_label as tl  # noqa: E402

TOML_TIER = "toml"
ONDO_HEX = "4F4E444F00000000000000000000000000000000"
ONDO_KEY = ONDO_HEX + ":"
XAGA_HEX = "5841476100000000000000000000000000000000"
XAGA_KEY = XAGA_HEX + ":rQJ1EbtepKKzx4sBCUtPjvSk8CfiAx2H8e"

UPGRADED = ["WST", "XRT", "EVE"]


@pytest.fixture(scope="module")
def names():
    return json.loads((ROOT / "token_names.json").read_text(encoding="utf-8"))


def entry_for(names, display):
    found = [v for v in names.values()
             if isinstance(v, dict) and v.get("currency_display") == display]
    assert len(found) == 1, f"expected exactly one {display}, got {len(found)}"
    return found[0]


# ── the three upgraded entries ──────────────────────────────────────
class TestCurrencyLevelEarnsTheTomlTier:
    @pytest.mark.parametrize("display", UPGRADED)
    def test_tier_is_toml(self, names, display):
        assert entry_for(names, display)["tier"] == TOML_TIER

    @pytest.mark.parametrize("display", UPGRADED)
    def test_renders_bare_like_xaga(self, names, display):
        """Verified means NO qualifier. Same contract TestXaga pins."""
        should_render, label = tl.render_decision(entry_for(names, display))
        assert should_render is True
        assert label is None, "a toml-tier token needs no qualifier"

    @pytest.mark.parametrize("display", UPGRADED)
    def test_display_is_the_plain_name(self, names, display):
        assert tl.display_labelled(entry_for(names, display)) == display

    @pytest.mark.parametrize("display", UPGRADED)
    def test_no_longer_carries_an_unearned_caveat(self, names, display):
        """Each of these previously rendered either "self-described
        (unverified)" or "issuer verified via ...". Both understated a
        currency-level citation."""
        label = tl.label_for(entry_for(names, display))
        assert label is None


# ── WST: the corrected verdict ──────────────────────────────────────
class TestWstCurrencyLevel:
    """Replaces TestWstUnreciprocated, which pinned the wrong conclusion.

    pay.aiic.nz does NOT list this address in [[ACCOUNTS]] -- that is what
    made it look unreciprocated. Its [[CURRENCIES]] table names code WST
    against this exact issuer, so the domain confirms the pairing outright.
    """

    def test_tier_is_toml_not_self_described(self, names):
        e = entry_for(names, "WST")
        assert e["tier"] == TOML_TIER
        assert e["tier"] != tl.SELF_DESCRIBED

    def test_does_not_render_the_unverified_caveat(self, names):
        label = tl.label_for(entry_for(names, "WST"))
        assert label != tl.LABEL_SELF_DESCRIBED
        assert label is None

    def test_citation_still_points_at_the_reciprocating_domain(self, names):
        assert tl.cited_domain(entry_for(names, "WST")) == "pay.aiic.nz"


# ── XAGa: the precedent this follows ────────────────────────────────
class TestXagaPrecedentUnchanged:
    """Not re-litigated here (test_curation_walker_9.py owns XAGa); asserted
    only so the three upgrades provably match it."""

    def test_xaga_is_toml_and_bare(self, names):
        should_render, label = tl.render_decision(names[XAGA_KEY])
        assert names[XAGA_KEY]["tier"] == TOML_TIER
        assert should_render is True and label is None

    @pytest.mark.parametrize("display", UPGRADED)
    def test_upgraded_entries_render_identically_to_xaga(self, names, display):
        assert tl.label_for(entry_for(names, display)) == \
            tl.label_for(names[XAGA_KEY])


# ── HVI3ESC: issuer-level keeps its caveat ──────────────────────────
class TestIssuerLevelKeepsItsCaveat:
    """hvi3.com has no [[CURRENCIES]] table, so only the address is
    confirmed. The qualifier stays, because the ticker is still unproven."""

    def test_tier_is_domain_fallback(self, names):
        assert entry_for(names, "HVI3ESC")["tier"] == tl.DOMAIN_FALLBACK

    def test_still_renders_the_issuer_level_caveat(self, names):
        assert tl.display_labelled(entry_for(names, "HVI3ESC")) == \
            "HVI3ESC \u00b7 issuer verified via hvi3.com"

    def test_issuer_level_is_noisier_than_currency_level(self, names):
        """The ladder, stated as a test: weaker evidence renders MORE text."""
        issuer_level = tl.label_for(entry_for(names, "HVI3ESC"))
        currency_level = tl.label_for(entry_for(names, "XRT"))
        assert issuer_level is not None
        assert currency_level is None


# ── ONDO: still untouched ───────────────────────────────────────────
class TestOndoSentinelUntouched:
    """A null-canonical brand-protection sentinel from 21878e8, not a token.

    Its empty `issuer` is the MECHANISM: an entry keyed '<hex>:' registers
    the brand with an EMPTY allowlist, so every XRPL issuer of that code
    fails membership and flags as spoofing on /pools. A verification tier is
    meaningless without an issuer to verify.
    """

    def test_issuer_is_deliberately_empty(self, names):
        assert names[ONDO_KEY]["issuer"] == ""

    def test_has_no_tier(self, names):
        assert "tier" not in names[ONDO_KEY]

    def test_citation_still_registers_the_protected_brand(self, names):
        """load_verified_brands() gates on `verified_via` containing
        'xrp-ledger.toml'. Rewriting that citation silently disables ONDO
        brand protection."""
        assert "xrp-ledger.toml" in names[ONDO_KEY]["verified_via"]
        brands = rank_amms.load_verified_brands()
        assert ONDO_HEX in brands
        assert brands[ONDO_HEX] == set(), "allowlist must stay EMPTY"

    def test_every_ondo_issuer_fails_membership(self):
        allow = rank_amms.load_verified_brands().get(ONDO_HEX, set())
        for probe in ("rondoSPOOFvanity1111",
                      "rHuiXXjHLpMP8ZE9sSQU5aADQVWDwv6h5p"):
            assert probe not in allow


# ── blast radius ────────────────────────────────────────────────────
def test_tier_census_is_exact(names):
    """Guards against a bulk retag: 4 currency-level (XAGa + the three
    upgraded), 1 issuer-level (HVI3ESC)."""
    toml_tier = {k for k, v in names.items()
                 if isinstance(v, dict) and v.get("tier") == TOML_TIER}
    df = {k for k, v in names.items()
          if isinstance(v, dict) and v.get("tier") == tl.DOMAIN_FALLBACK}
    assert len(toml_tier) == 4, f"toml tier drifted: {sorted(toml_tier)}"
    assert len(df) == 1, f"domain_fallback drifted: {sorted(df)}"


def test_self_described_entries_all_keep_their_caveat(names):
    """The inverse guard: upgrading three entries must not have loosened the
    rule that an unverified name always discloses."""
    for v in names.values():
        if not isinstance(v, dict) or v.get("tier") != tl.SELF_DESCRIBED:
            continue
        if v.get("suppressed"):
            continue
        assert tl.label_for(v) is not None


def test_no_verification_caveat_renders_without_a_host(names):
    """domain_fallback interpolates cited_domain(); a missing host must fall
    back to the self-described string, never 'issuer verified via None'."""
    for v in names.values():
        if not isinstance(v, dict) or v.get("tier") != tl.DOMAIN_FALLBACK:
            continue
        label = tl.label_for(v)
        assert "None" not in label
        assert tl.cited_domain(v), f"domain_fallback without host: {v}"
