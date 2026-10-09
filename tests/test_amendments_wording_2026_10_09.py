"""/amendments approved wording changes (branch amendments-wording-2026-10-09).

Three changes, all fakes only — no DB, no network, no production
DATABASE_URL (owner rule 2026-10-08):

  1. "Needs N to keep its countdown" is labelled as OUR NODE'S count, and
     the ledger's own Majorities decision is shown as the final word.
  2. "Already over the bar even without every validator counted." becomes
     "N votes to spare"; at or below the bar it becomes "at risk: ...".
  3. "majority reached at fetch <date>" becomes "majority reached <date>".

Motivating fact (verified 2026-10-09): fixCleanup3_4_0 took its majority
on 28 yes while our node heard 35 validators and our page said it needed
29. Our count and the ledger's decision can disagree, so the page must
attribute the count to us and let the ledger have the last word.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import roll_call_card as C  # noqa: E402
from tests.test_roll_call_card import OTHER, PD, UTC, _round  # noqa: E402

IN_FLIGHT = [{"hash": PD, "name": "PermissionDelegationV1_1"},
             {"hash": OTHER, "name": "Other"}]
NOW = dt.datetime(2026, 10, 9, 13, 45, 0, tzinfo=UTC)


def _card(pd_yes):
    """_round defaults to unl=35 -> needed_full = 35*80//100 + 1 = 29."""
    rounds = [
        _round(107249407, "2026-10-09T13:40:13Z",
               {PD: (pd_yes, pd_yes, True), OTHER: (16, 16, False)}),
        _round(107249151, "2026-10-09T13:23:31Z",
               {PD: (pd_yes, pd_yes, True), OTHER: (16, 16, False)}),
    ]
    card = C.build_card(rounds, IN_FLIGHT, NOW)
    return next(r for r in card["rows"] if r["hash"] == PD), card


# ── change 2 data layer: spare_votes ──

def test_needed_full_is_29_on_a_35_validator_unl():
    row, card = _card(31)
    assert card["unl_full"] == 35
    assert card["needed_full"] == 29


def test_spare_votes_positive_above_the_bar():
    row, _ = _card(31)
    assert row["spare_votes"] == 2


def test_spare_votes_zero_exactly_at_the_bar():
    row, _ = _card(29)
    assert row["spare_votes"] == 0


def test_spare_votes_negative_below_the_bar():
    row, _ = _card(27)
    assert row["spare_votes"] == -2
    # short_by stays the positive mirror, unchanged behaviour.
    assert row["short_by"] == 2


def test_spare_votes_matches_the_fixcleanup_case():
    """The real 2026-10-09 case: 28 yes against our needed_full of 29 is
    one SHORT by our count, even though the ledger granted the majority."""
    row, _ = _card(28)
    assert row["spare_votes"] == -1
