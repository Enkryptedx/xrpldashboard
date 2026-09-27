"""changes_builder UNL-delta truth gate (Charlie 2026-09-27).

Regression coverage for the phantom "UNL: 1 validator removed" line that
rode the homepage "What's new" every day with no evidence a reader could
check. Two root causes, both pinned here:

  1. An unkeyed snapshot entry injected an empty string "" into the
     validator set, so the daily diff manufactured a phantom ''
     added/removed.
  2. A UNL change item was emitted on ANY set difference, even when the
     signed-list sequence had not changed (a re-sign or a parse artifact).

Rule now: a UNL change item is emitted ONLY when the sequence actually
changed AND we can name the key(s), and it carries the proof (before/after
sequence, validator keys, raw signed-list URL). Otherwise nothing.
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import changes_builder as CB  # noqa: E402


def _snap(seq, keys):
    return {"sequence": seq,
            "validators": [{"validation_public_key": k} for k in keys]}


def test_empty_string_key_never_enters_the_set():
    # An entry with no validation_public_key must NOT become "".
    payload = {"sequence": 85, "validators": [
        {"validation_public_key": "nAAA"},
        {"validation_public_key": ""},      # blank
        {"domain": "example.com"},           # no key at all
    ]}
    s = CB._unl_validator_set(payload)
    assert s == {"NAAA"}
    assert "" not in s


def test_same_sequence_emits_nothing_even_with_a_set_difference():
    # THE BUG: same signed-list sequence, but a spurious set difference
    # (here an empty-string artifact on one side). Must emit nothing.
    before = {"sequence": 85, "validators": [{"validation_public_key": "nAAA"},
                                             {"validation_public_key": ""}]}
    after = _snap(85, ["nAAA"])
    assert CB._unl_delta(before, after, source="ripple") == []


def test_missing_sequence_emits_nothing():
    # No captured before/after sequence -> cannot prove a transition.
    before = {"validators": [{"validation_public_key": "nAAA"}]}
    after = {"validators": [{"validation_public_key": "nBBB"}]}
    assert CB._unl_delta(before, after, source="ripple") == []


def test_identical_snapshots_emit_nothing():
    snap = _snap(85, ["nAAA", "nBBB", "nCCC"])
    assert CB._unl_delta(snap, dict(snap), source="ripple") == []


def test_real_removal_emits_with_full_proof():
    # A genuine transition: sequence bumps AND a key drops. The item must
    # carry before/after sequence, the validator key(s), and the raw list URL.
    before = _snap(84, ["nAAA", "nBBB"])
    after = _snap(85, ["nAAA"])
    items = CB._unl_delta(before, after, source="ripple")
    assert len(items) == 1
    it = items[0]
    assert "removed" in it["line"]
    assert it["before_sequence"] == 84 and it["after_sequence"] == 85
    assert it["validator_keys"] == ["NBBB"]
    assert it["raw_list_url"] == "https://vl.ripple.com/"
    assert it["unl_source"] == "ripple"


def test_real_addition_emits_with_full_proof():
    before = _snap(2026070301, ["nAAA"])
    after = _snap(2026070302, ["nAAA", "nZZZ"])
    items = CB._unl_delta(before, after, source="xrplf")
    assert len(items) == 1
    it = items[0]
    assert "added" in it["line"]
    assert it["before_sequence"] == 2026070301 and it["after_sequence"] == 2026070302
    assert it["validator_keys"] == ["NZZZ"]
    assert it["raw_list_url"] == "https://unl.xrpl.foundation/"
