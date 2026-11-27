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

    # WIDENED 2026-11-22. This guard existed precisely to stop a render site
    # reading currency_display without the chokepoint -- and it read app.py
    # ONLY, so wallet_data.py, token_data.py and amm_scan_pools.py drifted
    # with ZERO display_labelled references between them. wallet and
    # /tokens/labels.json shipped bare names as a result.
    WIDENED_FILES = ("app.py", "wallet_data.py", "token_data.py",
                     "amm_scan_pools.py")

    # Each exemption names WHY, so a later pass cannot quietly add one.
    EXEMPT = {
        # The token's OWN page heading. PR 2 puts the full SENTENCE there as
        # a separate element; a tag in the heading would duplicate it.
        ("token_data.py", 'display = meta.get("currency_display") or currency'),
        # Worker input list, not a rendered surface: _load_known_tokens()
        # feeds pool scanning, and tests/test_amm_ranking_sourcing.py asserts
        # app code may not import this module at all.
        ("amm_scan_pools.py", 'display = entry.get("currency_display")'),
    }

    def test_no_new_bare_display_site_appears(self):
        """Every module that builds a token display consults the chokepoint,
        or carries a named exemption."""
        import re as _re
        offenders = []
        for fn in self.WIDENED_FILES:
            lines = open(os.path.join(REPO_ROOT, fn),
                         encoding="utf-8").read().splitlines()
            for i, line in enumerate(lines):
                if "currency_display" not in line:
                    continue
                if not _re.search(r"(display|label|symbol|ticker)\s*[=:]|return ",
                                  line, _re.I):
                    continue
                if "display_labelled" in "\n".join(lines[max(0, i - 2):i + 3]):
                    continue
                stripped = line.strip()
                if any(fn == f and ex in stripped for f, ex in self.EXEMPT):
                    continue
                # a local PARAMETER named currency_display is not a dict read
                if not any(t in line for t in ("meta", "entry", "inf")):
                    continue
                offenders.append(f"{fn}:{i + 1}: {stripped}")
        assert offenders == [], offenders

    def test_chokepoint_attaches_the_field(self, app_src):
        """ASSERTION MOVED 2026-11-22, not weakened.

        The pinned call gained `short=True`. Every consumer of this field
        is a list or row -- /tokens rows, the 30d hero, trades, whale and
        trustline event rows, the inferred-label lookup, labels.json and
        the wallet issuance list -- and three of them emit it as a JSON
        "display"/"label" field. A provenance SENTENCE there overflowed a
        phone row (wallet and event rows measured 40 chars against ~38)
        and changed a machine-readable field, so list contexts take the
        short tag. The full sentence is for a token's own page and for a
        key, neither of which reads this field.

        Still pinned as one exact substring: if the chokepoint ever stops
        passing short=True, every list row silently grows a sentence.
        """
        assert 'entry["display_labelled"] = token_label.display_labelled(' \
            'entry, short=True)' in app_src

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
        # dict deliberately. CERT added 2026-10-17; BOTC + DATEC added
        # 2026-11-12 at issuer level, then re-worded the same day when the
        # owner caught that "issuer verified via" overstated a ONE-WAY
        # claim: their AccountRoot carries no Domain field, so the ledger
        # never confirms the TOML's assertion. They moved to the new
        # `domain_listed` tier. HVI3ESC keeps "issuer verified via" because
        # its issuer's on-ledger Domain really does read "hvi3.com" -- both
        # legs present, only the currency level missing.
        #
        # LOOM + HVI3 added 2026-11-20 (anchor #16 carve-out), owner
        # approved at ISSUER level: both AccountRoots carry on-ledger
        # Domain "hvi3.com" and both addresses appear in that TOML's
        # [[ACCOUNTS]], so they earn HVI3ESC's exact two-way wording. The
        # TOML serves no [[CURRENCIES]] blocks and one cannot describe an
        # MPT, so neither may say more than this.
        #
        # YELLOW was published in the same pass and is deliberately NOT
        # here: it is a `toml`-tier classic currency, and the top tier
        # renders BARE (label_for -> None). More evidence, fewer words.
        assert got == {
            "nsRLUSD": "\u26a0 not Ripple's RLUSD \u00b7 self-described",
            "nrRLUSD": "\u26a0 not Ripple's RLUSD \u00b7 self-described",
            "niRLUSD": "\u26a0 not Ripple's RLUSD \u00b7 self-described",
            "ntRLUSD": "\u26a0 not Ripple's RLUSD \u00b7 self-described",
            # NXRP removed 2026-11-27 (anchor #17 carve-out): BOTH NXRP
            # records are now `suppressed` with
            # impersonation_risk_pending_review on the PUBXRP precedent, so
            # neither reaches this census at all. The lookalike wording is
            # still unit-tested on a synthetic entry
            # (tl.lookalike_asset(mpt("NXRP")) == "XRP"); what changed is the
            # DATA, not the rule.
            "HASH": "self-described (unverified)",
            "DDG": "self-described (unverified)",
            "X40AM": "self-described (unverified)",
            "HVI3ESC": "issuer verified via hvi3.com",
            "LOOM": "issuer verified via hvi3.com",
            "HVI3": "issuer verified via hvi3.com",
            "CERT": "self-described (unverified)",
            "BOTC": "listed by www.xrptokenizer.app \u00b7 not confirmed on-ledger",
            "DATEC": "listed by www.xrptokenizer.app \u00b7 not confirmed on-ledger",
            # Added 2026-11-22 (brands made explicit), owner approved at
            # ISSUER level. These five MPTs passed the Domain-first sweep:
            # xrpdev.net / hvi3.com name each address in their TOML AND each
            # issuer's on-ledger Domain points back at that host, so they
            # earn HVI3ESC's exact two-way wording. They were held back on
            # the morning of 2026-11-22 only because writing the citation
            # their label needs ALSO enrolled six protected brand families
            # under the old substring loader (73 -> 79). Enrolment is now a
            # declaration, so the label is only a label.
            #
            # Opulence shipped in the same pass and is deliberately NOT
            # here: it is a 40-char classic currency, not an MPT, so it
            # never enters this MPT-only census.
            "Xspence": "issuer verified via xrpdev.net",
            "XPN": "issuer verified via xrpdev.net",
            "DEV": "issuer verified via xrpdev.net",
            "HVI Spike Te": "issuer verified via hvi3.com",
            "THE Y PASS": "issuer verified via hvi3.com",
            # WRENCH added 2026-11-27 (anchor #17 carve-out) at ISSUER
            # level, earning HVI3ESC's exact two-way wording: hvi3.com's
            # TOML names issuer rEPggA3ebaVf82Jj3NFnDXntYZGBaFWYp2 AND that
            # issuer's on-ledger Domain resolves back to hvi3.com, both legs
            # read from our own node (tier=domain_fallback, tier_evidence
            # match_level=issuer, reverse_leg=True). hvi3.com publishes no
            # [[CURRENCIES]] blocks and one could not describe an MPT
            # anyway, so currency level is unreachable and this label may
            # say no more than it does.
            "WRENCH": "issuer verified via hvi3.com",
        }, got
