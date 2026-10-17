"""display_labelled: the chokepoint every surface must render.

WHY (2026-10-16). PR 1 shipped a labelling system that labelled nothing. It
computed `_trust_label` in `_load_token_names_dict()` and no template or view
ever read it, so 9 self-described MPT names — including 5 Ripple-RLUSD
lookalikes — would have rendered bare the moment one surfaced. Verified live
at the time: zero label hits on every surface.

The invariant now: **a self-described name never renders without its label.**
That cannot survive eight hand-edited call sites, so it is enforced at one
chokepoint plus a drift guard that fails if a ninth bare read appears.

`currency_display` stays pristine: `_resolve_display_label_to_hex` matches on
it to resolve shareable `/token/<ticker>/<issuer>` URLs. Mutating it to carry
the label would break those.

THE AUTOMATIC RULE is scoped by key shape, not trust tier. An MPT's
`currency_hex` is the 48-char MPTokenIssuanceID; `lp_derived` LP tokens are
40-char. So `LP: XRP/SOLO` and `LP: RLUSD/mXRP` are excluded STRUCTURALLY
(240 of them), which is stronger than the tier gate it replaces.

Hermetic: pure functions and source assertions. No DB, no network.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
import token_label as tl  # noqa: E402

APP_PY = os.path.join(REPO_ROOT, "app.py")
WALLET_HTML = os.path.join(REPO_ROOT, "templates", "wallet.html")
NAMES_JSON = os.path.join(REPO_ROOT, "token_names.json")

RLUSD_CANON = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
MPT_HEX = "06683DD2AFEF6B43E98F535E" + "0" * 24          # 48 chars
CLASSIC_HEX = "A" * 40                                    # 40 chars


def mpt(display, issuer="rSomeIssuer", **kw):
    e = {"currency_display": display, "issuer": issuer,
         "currency_hex": MPT_HEX, "source": "mpt_metadata"}
    e.update(kw)
    return e


# ── 1. the automatic rule is MPT-scoped, not tier-gated ─────────────
class TestAutomaticRule:
    def test_untagged_mpt_lookalike_is_caught(self):
        """No hand-tagging: a walker-discovered MPT is flagged on sight."""
        assert tl.lookalike_asset(mpt("nsRLUSD")) == "RLUSD"
        assert tl.lookalike_asset(mpt("NXRP")) == "XRP"

    def test_classic_40char_token_is_structurally_excluded(self):
        """240 lp_derived LP tokens contain XRP/RLUSD and must never trip."""
        e = {"currency_display": "LP: XRP/SOLO", "issuer": "rLP",
             "currency_hex": CLASSIC_HEX, "source": "lp_derived"}
        assert tl.is_mpt(e) is False
        assert tl.lookalike_asset(e) is None
        assert tl.label_for(e) is None

    def test_canonical_issuer_never_flagged(self):
        assert tl.lookalike_asset(mpt("RLUSD", issuer=RLUSD_CANON)) is None

    def test_non_mpt_still_honours_explicit_tier(self):
        """PR-1 behaviour preserved for classic tokens tagged by hand."""
        e = {"currency_display": "nsRLUSD", "issuer": "rX",
             "currency_hex": CLASSIC_HEX, "tier": tl.SELF_DESCRIBED}
        assert tl.lookalike_asset(e) == "RLUSD"

    def test_unrelated_mpt_gets_no_warning(self):
        assert tl.lookalike_asset(mpt("HASH")) is None


# ── 2. the review-marker exemption ──────────────────────────────────
class TestReviewMarker:
    def test_marked_entry_is_skipped(self):
        e = mpt("XRP NFT", **{tl.REVIEW_MARKER: "pending_owner_decision_2026-10-16"})
        assert tl.lookalike_asset(e) is None
        assert tl.label_for(e) is None

    def test_marker_count_is_pinned_at_three(self):
        """A fourth exemption must not appear quietly. Delete the three
        markers to let the automatic rule cover them."""
        raw = json.load(open(NAMES_JSON, encoding="utf-8"))
        marked = sorted(v.get("currency_display") for v in raw.values()
                        if isinstance(v, dict) and v.get(tl.REVIEW_MARKER))
        assert marked == ["XRP NFT", "XRP+", "XRP3D"], marked

    def test_marked_entries_render_bare(self):
        raw = json.load(open(NAMES_JSON, encoding="utf-8"))
        for v in raw.values():
            if isinstance(v, dict) and v.get(tl.REVIEW_MARKER):
                assert tl.display_labelled(v) == v["currency_display"]


# ── 3. precedence: ONE "not X" phrase, never two ────────────────────
class TestPrecedence:
    def test_guard_only(self):
        assert tl.merge_qualifiers(None, "not Tether") == "not Tether"

    def test_trust_only(self):
        lab = "\u26a0 not XRP \u00b7 self-described"
        assert tl.merge_qualifiers(lab, None) == lab

    def test_both_keeps_one_not_phrase(self):
        """The collision guard owns the 'not X' slot - it has audited
        canonical-issuer data and the three-state Circle wording."""
        out = tl.merge_qualifiers("\u26a0 not Tether \u00b7 self-described",
                                   "USDT (rX\u2026 \u2014 not Tether)")
        assert out == "USDT (rX\u2026 \u2014 not Tether) \u00b7 self-described"
        assert out.count("not Tether") == 1

    def test_non_warning_trust_label_appends(self):
        out = tl.merge_qualifiers("self-described (unverified)",
                                   "USDT (rX\u2026 \u2014 not Tether)")
        assert out == "USDT (rX\u2026 \u2014 not Tether) \u00b7 self-described (unverified)"


# ── 4. the chokepoint string ────────────────────────────────────────
class TestDisplayLabelled:
    def test_label_is_appended(self):
        assert tl.display_labelled(mpt("nsRLUSD")) == \
            "nsRLUSD \u00b7 \u26a0 not Ripple's RLUSD \u00b7 self-described"

    def test_unlabelled_entry_renders_bare(self):
        e = {"currency_display": "SOLO", "issuer": "rSolo",
             "currency_hex": CLASSIC_HEX}
        assert tl.display_labelled(e) == "SOLO"

    def test_currency_display_is_never_mutated(self):
        """The matcher resolves /token/<ticker>/<issuer> from this field."""
        e = mpt("nsRLUSD")
        tl.display_labelled(e)
        assert e["currency_display"] == "nsRLUSD"


# ── 5. DRIFT GUARD: every consumer reads the chokepoint ─────────────
class TestDriftGuard:
    """The invariant cannot survive eight hand-edited call sites. These fail
    if a bare render read reappears."""

    @pytest.fixture(scope="class")
    def app_src(self):
        return open(APP_PY, encoding="utf-8").read()

    @pytest.mark.parametrize("bare", [
        'display = meta.get("currency_display") or cur',
        'cur_disp = (token or {}).get("currency_display") or (\n',
        '"display": inf.get("currency_display") or cur,',
    ])
    def test_no_bare_render_read_remains(self, app_src, bare):
        assert bare not in app_src, (
            "a render site reads currency_display without display_labelled "
            "first - a self-described name would render unlabelled")

    def test_chokepoint_attaches_the_field(self, app_src):
        assert 'entry["display_labelled"] = token_label.display_labelled(entry)' \
            in app_src

    def test_all_nine_sites_read_display_labelled(self, app_src):
        assert app_src.count('display_labelled') >= 9, app_src.count('display_labelled')

    def test_matcher_still_reads_pristine_field(self, app_src):
        assert 'entry.get("currency_display") or ""' in app_src

    def test_wallet_template_uses_the_chokepoint(self):
        html = open(WALLET_HTML, encoding="utf-8").read()
        assert "tok.display_labelled or tok.currency_display" in html
        assert "{{ tok.currency_display }}" not in html


# ── 6. suppression still holds end to end ───────────────────────────
class TestSuppression:
    def test_suppressed_entries_never_render(self):
        """The invariant is per-entry, not a census. The exact count lives in
        tests/test_curation_walker_9.py, which owns the current roster -- a
        count pinned in two files has to be bumped in two places on every
        curation pass, and the second one gets forgotten."""
        raw = json.load(open(NAMES_JSON, encoding="utf-8"))
        sup = [v for v in raw.values()
               if isinstance(v, dict) and v.get("suppressed")]
        assert sup, "expected at least the self-declared test tokens"
        for v in sup:
            assert tl.render_decision(v) == (False, None)
            assert isinstance(v["suppressed"], str) and v["suppressed"]

    def test_the_nine_visible_labels_are_exact(self):
        """Owner-approved wording, verbatim."""
        raw = json.load(open(NAMES_JSON, encoding="utf-8"))
        got = {}
        for v in raw.values():
            if not isinstance(v, dict) or not tl.is_mpt(v):
                continue
            if v.get(tl.REVIEW_MARKER) or tl.is_suppressed(v):
                continue
            lab = tl.label_for(v)
            if lab:
                got[v["currency_display"]] = lab
        # EQUALITY on purpose: this set is the record of what the owner has
        # approved for publication. A new entry appearing here without an
        # approval should fail the build, so each curation pass updates this
        # dict deliberately. CERT added 2026-10-17.
        assert got == {
            "nsRLUSD": "\u26a0 not Ripple's RLUSD \u00b7 self-described",
            "nrRLUSD": "\u26a0 not Ripple's RLUSD \u00b7 self-described",
            "niRLUSD": "\u26a0 not Ripple's RLUSD \u00b7 self-described",
            "ntRLUSD": "\u26a0 not Ripple's RLUSD \u00b7 self-described",
            "NXRP": "\u26a0 not XRP \u00b7 self-described",
            "HASH": "self-described (unverified)",
            "DDG": "self-described (unverified)",
            "X40AM": "self-described (unverified)",
            "HVI3ESC": "issuer verified via hvi3.com",
            "CERT": "self-described (unverified)",
        }, got
