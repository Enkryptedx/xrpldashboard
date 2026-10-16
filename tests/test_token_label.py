"""token_label: self-described tier, suppression, lookalike rule.

THE WOUND (2026-10-16). `_load_token_names_dict()` suppressed exactly one
thing: `verified_via == "TODO_curation_pass"`. An entry with NO
`verified_via` field fell straight through and rendered — the opposite of
the policy its own docstring quoted. 14 freshly-walked MPT entries had no
`verified_via` at all, so committing them as a data-only change would have
published 5 Ripple-RLUSD lookalikes, 2 XRP lookalikes and 5 self-declared
test tokens under curated-looking names. Verified before the fix: "of the 14
new entries, 14 would be LIVE on the site".

ROOT CAUSE: `source` was doubling as a trust tier. `source: mpt_metadata`
means "the name came from XLS-89 on-ledger metadata" — protocol-
deterministic, i.e. we read the ledger correctly. It does NOT mean the
issuer's claim is true; anyone can mint an MPT and self-declare any ticker.
Trust now lives in its own `tier` field.

SCOPE GUARD, pinned below: the lookalike rule is gated on
`tier == self_described`. Ungated, substring matching would fire on 92
legitimate `lp_derived` LP tokens (`LP: XRP/SOLO`, `LP: RLUSD/mXRP`, …) and
on 3 unreviewed pre-existing entries (`XRP NFT`, `XRP+`, `XRP3D`).

Hermetic: pure functions, no DB, no network, no app import.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
import token_label as tl  # noqa: E402

RLUSD_CANON = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
TOML = "https://hvi3.com/.well-known/xrp-ledger.toml"


def sd(display, issuer="rSomeIssuerAddressXXXXXXXXXXXXX", **kw):
    """A self-described entry."""
    e = {"currency_display": display, "issuer": issuer,
         "source": "mpt_metadata", "tier": tl.SELF_DESCRIBED}
    e.update(kw)
    return e


# ── 1. suppression ──────────────────────────────────────────────────
class TestSuppression:
    def test_explicit_suppressed_reason_blocks_render(self):
        e = sd("prRLUSD", suppressed="self_declared_test")
        assert tl.is_suppressed(e) is True
        assert tl.render_decision(e) == (False, None)

    def test_legacy_todo_sentinel_still_blocks(self):
        """Pre-existing behaviour must not regress."""
        e = {"currency_display": "X", "verified_via": "TODO_curation_pass"}
        assert tl.is_suppressed(e) is True

    def test_untagged_entry_is_not_suppressed(self):
        """The ~286 entries with no tier keep rendering exactly as today."""
        e = {"currency_display": "SOLO", "issuer": "rSolo"}
        assert tl.is_suppressed(e) is False
        assert tl.render_decision(e) == (True, None)

    def test_non_dict_is_suppressed_not_crashed(self):
        assert tl.is_suppressed(None) is True
        assert tl.is_suppressed("junk") is True


# ── 2. the lookalike rule ───────────────────────────────────────────
class TestLookalikeRule:
    @pytest.mark.parametrize("display", ["nsRLUSD", "nrRLUSD", "niRLUSD",
                                         "ntRLUSD", "prRLUSD"])
    def test_rlusd_lookalikes_detected(self, display):
        assert tl.lookalike_asset(sd(display)) == "RLUSD"

    @pytest.mark.parametrize("display", ["pubXRP", "NXRP"])
    def test_xrp_lookalikes_detected(self, display):
        """XRP is the native asset — it has NO canonical issuer, so any
        ISSUED token calling itself XRP is by definition not the real one."""
        assert tl.lookalike_asset(sd(display)) == "XRP"

    def test_canonical_rlusd_issuer_is_never_a_lookalike(self):
        assert tl.lookalike_asset(sd("RLUSD", issuer=RLUSD_CANON)) is None

    def test_canonical_issuer_variant_ticker_is_not_a_lookalike(self):
        """Ripple issuing e.g. RLUSD2 is still Ripple."""
        assert tl.lookalike_asset(sd("RLUSD2", issuer=RLUSD_CANON)) is None

    def test_unrelated_ticker_is_not_a_lookalike(self):
        assert tl.lookalike_asset(sd("HASH")) is None
        assert tl.lookalike_asset(sd("DDG")) is None

    def test_rule_is_case_insensitive_on_display(self):
        assert tl.lookalike_asset(sd("myrlusd")) == "RLUSD"


# ── 3. THE SCOPE GUARD ──────────────────────────────────────────────
class TestScopeGuard:
    def test_lp_derived_can_never_be_labelled(self):
        """92 real LP tokens contain XRP/RLUSD. Ungated substring matching
        would mislabel every one of them."""
        for disp in ("LP: XRP/SOLO", "LP: RLUSD/mXRP", "LP: XRP/BTC.Bitstamp",
                     "LP: SOLO/mXRP"):
            e = {"currency_display": disp, "issuer": "rLP",
                 "source": "lp_derived",
                 "verified_via": "https://example.test/x"}
            assert tl.lookalike_asset(e) is None
            assert tl.label_for(e) is None

    def test_untagged_xrp_entries_untouched(self):
        """XRP NFT / XRP+ / XRP3D are pre-existing and unreviewed. This PR
        is scoped to the 14, so they must get no label."""
        for disp in ("XRP NFT", "XRP+", "XRP3D"):
            e = {"currency_display": disp, "issuer": "rPre",
                 "source": "mpt_metadata"}      # note: no tier
            assert tl.lookalike_asset(e) is None
            assert tl.label_for(e) is None

    def test_tier_is_what_makes_an_entry_eligible(self):
        e = {"currency_display": "nsRLUSD", "issuer": "rX",
             "source": "mpt_metadata"}
        assert tl.lookalike_asset(e) is None          # untagged
        e["tier"] = tl.SELF_DESCRIBED
        assert tl.lookalike_asset(e) == "RLUSD"       # tagged


# ── 4. exact rendered strings (owner-approved wording) ──────────────
class TestExactLabels:
    def test_rlusd_lookalike_string(self):
        assert tl.label_for(sd("nsRLUSD")) == \
            "\u26a0 not Ripple's RLUSD \u00b7 self-described"

    def test_xrp_lookalike_string(self):
        assert tl.label_for(sd("NXRP")) == \
            "\u26a0 not XRP \u00b7 self-described"

    def test_plain_self_described_string(self):
        for d in ("HASH", "DDG", "X40AM"):
            assert tl.label_for(sd(d)) == "self-described (unverified)"

    def test_domain_fallback_string_is_issuer_level_only(self):
        """A TOML [[CURRENCIES]] block cannot describe an MPT, so this
        proves WHO issued it, never that the ticker is right."""
        e = sd("HVI3ESC", tier=tl.DOMAIN_FALLBACK, verified_via=TOML)
        assert tl.label_for(e) == "issuer verified via hvi3.com"

    def test_domain_fallback_without_a_url_degrades_safely(self):
        e = sd("X", tier=tl.DOMAIN_FALLBACK)
        assert tl.label_for(e) == "self-described (unverified)"


# ── 5. the reciprocation rule ───────────────────────────────────────
class TestReciprocation:
    def test_reciprocated_domain_passes(self):
        toml = '[[ACCOUNTS]]\naddress = "r9avT7NURuqC7jbVxUjcrMkHAr5aqmHkSN"\n'
        assert tl.domain_is_reciprocated(
            "r9avT7NURuqC7jbVxUjcrMkHAr5aqmHkSN", toml) is True

    def test_unreciprocated_domain_fails(self):
        """DDG's AccountRoot claims bithomp.com; bithomp's TOML lists two
        unrelated accounts. A one-way claim is not evidence."""
        bithomp = ('[[ACCOUNTS]]\naddress = "rPPHhfSQbHt1t2XPHWAK1HTcjqDg56TzZy"\n'
                   '[[ACCOUNTS]]\naddress = "rKontEGtDju5MCEJCwtrvTWQQqVAw5juXe"\n')
        assert tl.domain_is_reciprocated(
            "rKRQCkc9XWuCgYSY2nF5Hxhz6kTknZFz72", bithomp) is False

    def test_missing_toml_fails_closed(self):
        assert tl.domain_is_reciprocated("rAny", None) is False
        assert tl.domain_is_reciprocated("rAny", "") is False

    def test_no_address_fails_closed(self):
        assert tl.domain_is_reciprocated("", "address = \"rAny\"") is False


# ── 6. bithomp must never reach a rendered string ───────────────────
def test_bithomp_never_appears_in_any_label():
    """Owner instruction: never display DDG's bithomp.com Domain claim."""
    ddg = sd("DDG", issuer="rKRQCkc9XWuCgYSY2nF5Hxhz6kTknZFz72")
    label = tl.label_for(ddg) or ""
    assert "bithomp" not in label.lower()
    # and it must not sneak in via a cited domain either
    ddg_with_claim = sd("DDG", verified_via="https://bithomp.com")
    assert "bithomp" not in (tl.label_for(ddg_with_claim) or "").lower() or \
        tl.label_for(ddg_with_claim) == "self-described (unverified)"


def test_cited_domain_ignores_the_todo_sentinel():
    assert tl.cited_domain({"verified_via": "TODO_curation_pass"}) is None
    assert tl.cited_domain({}) is None
