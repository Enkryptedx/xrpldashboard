"""Source labels for UNTIERED names: approval record + invariants.

WHY. An untiered name renders BARE -- exactly how the top `toml` tier
renders, since evidence means fewer words. So a bare name was ambiguous:
reviewed, or merely unexamined? These labels say where a name CAME FROM
without claiming a verdict.

WHY SHORT IN LISTS. All nine consumers of `display_labelled` are list or
row contexts -- /tokens rows, the 30d hero, trades, whale and trustline
event rows, the inferred-label lookup, labels.json and the wallet issuance
list -- and three emit it as a JSON "display"/"label" field. Measured
2026-11-22, the full sentence put wallet and event rows at 40 chars
against a ~38-char phone row AND changed a machine-readable field. Lists
take the tag; the sentence is for a token's own page and for a key.

This file pins the approved WORDING and the INVARIANTS, deliberately not
per-label counts -- counts move every curation pass, and a count is not an
approval.
"""

import json
import os

import pytest

import token_label as TL

NAMES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "token_names.json")

# Owner-approved 2026-11-22. EQUALITY on purpose: this is the record of
# what may be published. New wording must be added here deliberately.
APPROVED = {
    "mpt_metadata":     ("name set by issuer \u00b7 not verified", "unverified"),
    "lp_derived":       ("name from pool data",                    "pool data"),
    "other-url":        ("informal source \u00b7 not verified",    "informal source"),
    "curator_inferred": ("name inferred from activity",            "inferred"),
    "toml-cited":       ("source cited \u00b7 could not confirm",  "unconfirmed"),
}
APPROVED_STRINGS = {s for pair in APPROVED.values() for s in pair}


@pytest.fixture(scope="module")
def names():
    with open(NAMES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _rendering(names):
    return [v for v in names.values() if isinstance(v, dict)
            and v.get("verified_via") != "TODO_curation_pass"
            and not TL.is_suppressed(v)]


# ── approval record: the ten strings, exactly ───────────────────────
def test_the_ten_approved_strings_are_exact():
    assert TL.SOURCE_LABELS == APPROVED, TL.SOURCE_LABELS
    assert len(APPROVED_STRINGS) == 10


def test_each_bucket_offers_both_a_sentence_and_a_tag():
    for bucket, (full, tag) in TL.SOURCE_LABELS.items():
        assert full and tag, bucket
        assert full != tag, bucket
        assert len(tag) <= len(full), bucket


def test_dead_buckets_are_absent():
    """`xrpscan` and `none` emptied on 2026-11-22. Wording for a bucket
    with no members would ship as dead copy in a key."""
    assert "xrpscan" not in TL.SOURCE_LABELS
    assert "none" not in TL.SOURCE_LABELS


# ── invariant 1: every untiered rendering entry maps to one bucket ──
def test_every_untiered_rendering_entry_maps_to_one_bucket(names):
    """Exactly one, or None for a DOCUMENTED exclusion. Anything else is an
    entry the label system silently ignores."""
    unmapped = []
    for v in _rendering(names):
        if v.get("tier"):
            continue
        bucket = TL.source_bucket(v)
        if bucket is not None:
            assert bucket in TL.SOURCE_LABELS, bucket
            continue
        # permitted exclusions, each with its own reason
        if not (v.get("issuer") or ""):
            continue                      # sentinel: cannot form a row
        if v.get(TL.REVIEW_MARKER):
            continue                      # parked for review: stays bare
        if not (v.get("verified_via") or "") and v.get("source") != "mpt_metadata":
            continue                      # genuinely no source recorded
        unmapped.append((v.get("currency_display"), v.get("source"),
                         v.get("verified_via")))
    assert unmapped == [], unmapped[:5]


# ── invariant 2: tiered entries never carry a source label ─────────
def test_tiered_entries_carry_no_source_label(names):
    """The double-label guard, asserted over the real file rather than a
    fixture. Precedence lives in display_labelled, so this also proves no
    template can stack the two."""
    for v in _rendering(names):
        if not v.get("tier"):
            continue
        assert TL.source_bucket(v) is None, v.get("currency_display")
        assert TL.source_label(v) is None, v.get("currency_display")
        assert TL.source_label(v, short=True) is None, v.get("currency_display")


# ── invariant 3: nothing outside the approved set can render ────────
def test_no_unapproved_source_string_can_render(names):
    seen = set()
    for v in _rendering(names):
        for short in (False, True):
            s = TL.source_label(v, short=short)
            if s is not None:
                seen.add(s)
    assert seen <= APPROVED_STRINGS, seen - APPROVED_STRINGS


def test_rendered_qualifier_is_tier_wording_or_an_approved_tag(names):
    """display_labelled appends exactly one qualifier. In a list context it
    is either the tier's own wording or an approved SHORT tag -- never a
    sentence, which is what overflowed the row."""
    sentences = {full for full, _ in TL.SOURCE_LABELS.values()}
    for v in _rendering(names):
        rendered = TL.display_labelled(v, short=True)
        name = v.get("currency_display") or ""
        if rendered == name:
            continue
        qualifier = rendered[len(name) + 3:]
        assert qualifier not in sentences, (name, qualifier)


# ── positive assertions on surfaces proven to render ───────────────
def test_mpt_issuer_rows_get_a_short_tag(names):
    """/mpt/issuer renders 9 of 10 sampled mpt_metadata names live
    (2026-11-22); the tenth is an empty listing for that issuer, not a
    labelling gap. label_for_issuance is the one chokepoint /mpts,
    /mpt/<id> and /mpt/issuer/<addr> all pass through."""
    sample = [v for v in _rendering(names)
              if TL.source_bucket(v) == "mpt_metadata" and TL.is_mpt(v)]
    assert sample, "no mpt_metadata entries to assert on"
    v = sample[0]
    iid, iss = v["currency_hex"], v["issuer"]
    label = TL.label_for_issuance(iid, iss, {(iid, iss): v})
    assert label == "unverified", label


def test_mpt_issuer_row_keeps_tier_wording_when_tiered(names):
    tiered = [v for v in _rendering(names)
              if v.get("tier") == "domain_fallback" and TL.is_mpt(v)]
    assert tiered, "no tiered MPT to assert on"
    v = tiered[0]
    iid, iss = v["currency_hex"], v["issuer"]
    label = TL.label_for_issuance(iid, iss, {(iid, iss): v})
    assert label == TL.label_for(v)
    assert label not in APPROVED_STRINGS, label


@pytest.mark.parametrize("bucket,expected", [
    ("mpt_metadata", "unverified"),
    ("lp_derived", "pool data"),
    ("other-url", "informal source"),
    ("curator_inferred", "inferred"),
    ("toml-cited", "unconfirmed"),
])
def test_list_row_string_for_each_bucket(names, bucket, expected):
    """One real row per bucket, as a list context renders it."""
    hits = [v for v in _rendering(names) if TL.source_bucket(v) == bucket]
    assert hits, f"no live entry in bucket {bucket}"
    v = hits[0]
    rendered = TL.display_labelled(v, short=True)
    assert rendered.endswith(" \u00b7 " + expected), rendered
    assert len(rendered) <= 38, (len(rendered), rendered)
