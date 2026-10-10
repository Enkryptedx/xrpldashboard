"""Contract: build_card() supplies every key the verdict partial reads.

Why this exists (verified 2026-10-10): `_amendments_rollcall_verdict.html`
landed in e31d3ee on 2026-10-09 and read three `rc_row` keys. Two older
test files built their `rc_row` dicts by hand and predated it
(test_roll_call_card_route.py 2026-09-26, test_amendments_compact_steps.py
2026-10-04), so they never supplied `spare_votes`.

Jinja RAISES on `Undefined > 0`, so `{% if rc_row.spare_votes > 0 %}` did
not degrade — it killed the whole /amendments render inside the
`{% include %}` and took **27 tests** down with it. Neither file was named
in ci.yml, so main stayed green and nobody saw it for ~35 hours.

This test is deliberately NOT a hardcoded list of three names, because a
hardcoded list drifts exactly the same way the fixtures did. It PARSES the
template for every `rc_row.<key>` / `roll_call.<key>` it reads and asserts
the real `build_card()` output carries each one. Add a new field to the
template and this test tells you immediately, naming the key.

Hermetic: pure dict fixtures through the real build_card(). No DB, no node,
no render.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import roll_call_card as C  # noqa: E402

PARTIAL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "_amendments_rollcall_verdict.html")

PD = "0F48FF5604C8D1E06C0D9A4E6E0E5C1F2A3B4C5D6E7F8091A2B3C4D5E6F70811"

# Jinja constructs that look like attribute access but are not card fields.
_NOT_FIELDS = {"items", "keys", "values", "get", "append"}


def _read_partial() -> str:
    with open(PARTIAL, encoding="utf-8") as fh:
        return fh.read()


def _keys_read(obj_name: str) -> set[str]:
    """Every `<obj_name>.<key>` the template reads, Jinja comments excluded.

    Comments matter: the partial carries a long {# ... #} header that names
    fields in prose ("Needs in scope: rc_row, roll_call"), and counting
    those would invent requirements the template does not actually have.
    """
    src = re.sub(r"\{#.*?#\}", " ", _read_partial(), flags=re.S)
    found = set(re.findall(rf"\b{obj_name}\.([a-zA-Z_][a-zA-Z0-9_]*)", src))
    return {k for k in found if k not in _NOT_FIELDS}


def _real_card(seen: int = 34, unl: int = 35):
    """A genuine build_card() result - not a hand-built dict."""
    now = dt.datetime(2026, 10, 10, 20, 25, tzinfo=dt.timezone.utc)
    thr = max(1, (unl * 80) // 100)
    rnd = {
        "voting_ledger": 107500000, "flag_ledger": 107500001,
        "observed_iso": "2026-10-10T20:25:00Z", "unl_size": unl,
        "seen": seen, "trusted_available": seen,
        "threshold": thr, "needed": thr + 1, "unl_sequence": 85,
        "tallies": {PD: (29, seen, True)}, "rounds_recorded": 8,
        "first_round_iso": "2026-09-26T11:44:53Z",
    }
    return C.build_card([rnd], [{"hash": PD, "name": "PermissionDelegationV1_1"}], now)


def test_partial_reads_at_least_the_known_fields():
    """Guard the guard: if the parse returns nothing, the assertions below
    would pass vacuously and this file would protect nothing."""
    rc_keys = _keys_read("rc_row")
    assert "spare_votes" in rc_keys, "parse missed the field that caused the outage"
    assert {"yes_carried", "needed"} <= rc_keys
    assert _keys_read("roll_call"), "roll_call reads not detected"


def test_build_card_rows_supply_every_rc_row_key_the_partial_reads():
    card = _real_card()
    assert card["rows"], "fixture produced no rows"
    needed = _keys_read("rc_row")
    for row in card["rows"]:
        missing = sorted(k for k in needed if k not in row)
        assert not missing, (
            f"_amendments_rollcall_verdict.html reads rc_row.{missing} but "
            f"build_card() rows do not provide it")


def test_build_card_supplies_every_roll_call_key_the_partial_reads():
    card = _real_card()
    missing = sorted(k for k in _keys_read("roll_call") if k not in card)
    assert not missing, (
        f"_amendments_rollcall_verdict.html reads roll_call.{missing} but "
        f"build_card() does not provide it")


def test_spare_votes_is_comparable_not_undefined():
    """The exact failure mode: Jinja raises on `Undefined > 0`, so this
    field must always be a real number, never absent."""
    for seen in (35, 34, 33):
        for row in _real_card(seen=seen)["rows"]:
            assert isinstance(row["spare_votes"], int)
            assert isinstance(row["needed"], int)
            assert row["spare_votes"] > 0 or row["spare_votes"] <= 0
