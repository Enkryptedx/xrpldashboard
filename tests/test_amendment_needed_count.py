"""The published amendment bar is the ACTIVATION count, not the 80% line.

rippled 3.2.0 records an 80% line and then requires strictly MORE than it:

  include/xrpl/protocol/SystemParameters.h:59
      constexpr std::ratio<80, 100> kAmendmentMajorityCalcThreshold;
  AmendmentSet::passes()
      if (trustedValidations_ == 1) return it->second >= threshold_;
      return it->second > threshold_;

So 35 validators => line (35*80)/100 = 28, and activation needs 29. 28 LOSES
-- confirmed on-ledger by PermissionDelegationV1_1, which sat at 28/35 and
lost its majority at flag ledger 107181569 on 2026-09-23.

/amendments was corrected 2026-09-25, but the signed-leaf builder was not, so
all 54 leaves dated 2026-09-22..2026-11-15 recorded `threshold 28` with
`threshold_display "28/35"` -- the line published as the bar -- and
signed_snapshot.py hardcoded `or "28/35"` as a fallback.

What this pins:
 1. the guarded derivation, including the 1-validator case a bare +1 breaks;
 2. the note is templated from each row's own values, never a fixed 28/35
    (validations vary per amendment: the 2026-09-22 leaf carries a 28 AND a
    29 line);
 3. the leaf builder publishes the bar, keeps the raw line, and has no
    hardcoded fallback anywhere;
 4. the field-level pre-sign gate rejects a missing or wrong votes_needed --
    the expected-key name-set could never catch this, which is why 54 leaves
    shipped green;
 5. the dated pages derive the bar for OLD leaves too and say so, while the
    anchored leaf is untouched.

Hermetic: no DB, no network, no signing.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import amendment_threshold as at  # noqa: E402


# ── 1. the guarded derivation ────────────────────────────────────────────

@pytest.mark.parametrize("validations,line,needed", [
    (35, 28, 29),    # today's UNL
    (36, 28, 29),    # (36*80)//100 = 28 -> still 29
    (40, 32, 33),
    (10, 8, 9),
    (2, 1, 2),
])
def test_line_and_needed_match_rippleds_integer_division(validations, line, needed):
    assert at.eighty_percent_line(validations) == line
    assert at.votes_needed(line, validations) == needed


def test_single_validator_is_the_exception_a_bare_plus_one_breaks():
    """rippled switches to `>=` at one trusted validation, so one vote is
    enough. A naive threshold+1 would demand 2 of 1 -- unreachable."""
    assert at.eighty_percent_line(1) == 1
    assert at.votes_needed(1, 1) == 1
    assert at.votes_needed(1, 2) == 2       # two validators: strictly-greater


def test_unknown_inputs_yield_none_not_a_guess():
    assert at.votes_needed(None, 35) is None
    assert at.needed_display(28, None) is None
    assert at.eighty_percent_line(0) is None
    assert at.eighty_percent_line("x") is None
    assert at.correction_note(None, 35) is None


def test_needed_display_is_the_bar_never_the_line():
    assert at.needed_display(28, 35) == "29/35"
    assert at.needed_display(28, 35) != "28/35"


# ── 2. the note is templated, never hardcoded ────────────────────────────

def test_note_is_built_from_the_rows_own_values():
    note = at.correction_note(28, 35)
    assert note == (
        "The signed snapshot for this date recorded the 80% line (28 of 35); "
        "activation requires more than 80%, so 29 are needed. "
        "The leaf is unchanged and still verifies."
    )
    # A different row on the SAME date must get a different sentence --
    # the 2026-09-22 leaf carries both a 28 and a 29 line.
    other = at.correction_note(29, 37)
    assert "29 of 37" in other and "so 30 are needed" in other


def test_no_code_path_defaults_the_display_to_a_literal_bar():
    """The old `or "28/35"` default asserted a stale validator count AND the
    wrong bar on any block whose votes were unavailable.

    Comment lines are stripped first. A bare scan for the string "28/35"
    fails on the very comments that explain the removal -- the same
    count-your-own-matches trap that made an earlier SET-parameter guard
    flag two correct `UPDATE ... SET` clauses. Assert the defect (a literal
    DEFAULT for the display) is gone, not that the digits are unmentionable.
    """
    for fname in ("signed_snapshot.py", "amendment_threshold.py",
                  "amendments_permalink.py"):
        path = os.path.join(_REPO, fname)
        code = "\n".join(
            ln.split("#")[0] for ln in open(path, encoding="utf-8")
            if not ln.lstrip().startswith("#"))
        assert 'or "28/35"' not in code, f"{fname}: literal default survived"
        assert "or '28/35'" not in code, fname
        assert 'threshold_display": "28/35"' not in code, fname


# ── 3. the leaf builder ──────────────────────────────────────────────────

def test_schema_version_bumped_as_the_marker():
    import signed_snapshot as ss
    assert ss.SCHEMA_VERSION == 6


def test_leaf_keeps_the_raw_line_and_adds_the_derived_bar():
    src = open(os.path.join(_REPO, "signed_snapshot.py"), encoding="utf-8").read()
    assert '"threshold": v.get("threshold")' in src, "raw line must survive"
    assert '"votes_needed": votes_needed(' in src
    assert '"threshold_display": threshold_display,' in src


# ── 4. the field-level pre-sign gate ─────────────────────────────────────

def _block(threshold=28, validations=35, needed=29, display="29/35"):
    nv = {"count": 21, "validations": validations, "threshold": threshold,
          "votes_needed": needed}
    return {"name": "amendments_block", "value": {
        "threshold_display": display,
        "per_amendment": {"LendingProtocol": {"network_votes": nv}}}}


def _gate(block):
    import signed_snapshot as ss
    reasons = ss._pre_sign_gate({"metrics": [block]})
    return [r for r in reasons if r.startswith("amendments_block")]


def test_gate_passes_a_correct_block():
    assert _gate(_block()) == []


def test_gate_blocks_a_missing_votes_needed():
    assert _gate(_block(needed=None)) == [
        "amendments_block_votes_needed_missing:LendingProtocol"]


def test_gate_blocks_the_old_off_by_one():
    """The exact defect: the line published as the bar."""
    out = _gate(_block(needed=28))
    assert out == ["amendments_block_votes_needed_wrong:LendingProtocol:28!=29"]


def test_gate_blocks_missing_vote_fields_and_display():
    assert "amendments_block_vote_fields_missing:LendingProtocol" in _gate(
        _block(threshold=None))
    assert "amendments_block_threshold_display_missing" in _gate(
        _block(display=None))


def test_gate_respects_the_one_validator_exception():
    assert _gate(_block(threshold=1, validations=1, needed=1,
                        display="1/1")) == []
    assert _gate(_block(threshold=1, validations=1, needed=2,
                        display="2/1")) != []


def test_rows_without_a_vote_dict_are_not_flagged():
    """enabled / superseded entries carry no network_votes by design."""
    b = {"name": "amendments_block", "value": {
        "threshold_display": "29/35",
        "per_amendment": {"fixAMMv1_3": {"enabled": True,
                                         "network_votes": None}}}}
    assert _gate(b) == []


# ── 5. the dated pages, including pre-schema-6 leaves ────────────────────

def test_old_leaf_gets_a_derived_bar_and_a_note():
    import amendments_permalink as ap
    block = {"per_amendment": {"LendingProtocol": {
        "hash": "AB", "enabled": False,
        "network_votes": {"count": 21, "validations": 35, "threshold": 28}}}}
    row = ap.signed_rows(block)[0]
    assert row["votes_threshold"] == 28, "signed line must be preserved"
    assert row["votes_needed"] == 29, "bar derived for an old leaf"
    assert row["threshold_note"] and "28 of 35" in row["threshold_note"]
    assert "29 are needed" in row["threshold_note"]


def test_schema6_leaf_uses_its_signed_value_and_needs_no_note():
    import amendments_permalink as ap
    block = {"per_amendment": {"LendingProtocol": {
        "network_votes": {"count": 21, "validations": 35, "threshold": 28,
                          "votes_needed": 29}}}}
    row = ap.signed_rows(block)[0]
    assert row["votes_needed"] == 29
    assert row["threshold_note"] is None


def test_template_heads_the_column_needed_and_fixes_the_prose():
    src = open(os.path.join(_REPO, "templates", "amendments_permalink.html"),
               encoding="utf-8").read()
    assert ">Needed</th>" in src
    assert ">Threshold</th>" not in src
    assert "r.votes_needed" in src
    assert "80% line" in src, "the line stays visible beside the bar"
    # The prose used to call the old 28 column "the number needed".
    assert "validators heard / the number needed (" not in src
    assert "needs 29 of 35)" not in src, "no hardcoded bar in the prose"
