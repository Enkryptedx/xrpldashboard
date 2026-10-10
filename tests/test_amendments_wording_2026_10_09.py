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

UPDATED 2026-10-10 — spare_votes / short_by moved to the HEARD bar.

The reason is internal consistency, nothing more: the printed bar and the
margin must be the same number, or the card contradicts itself at any
count below full attendance. Before this change, at 34 heard the headline
read "needs 28 of 34" while the wording line read "short by 1" — measured
against 29. Two answers on one card.

The fixtures below run at 34 heard (_round's default), so the bar they
measure against moves 29 -> 28 and the expectations move with it. Where a
test's NAME states a relationship ("exactly at the bar", "singular one
vote"), the INPUT was moved to preserve that relationship rather than the
expected number being bumped to match the code — bumping the number would
have made the test agree with whatever the code now does.

Note the fixCleanup3_4_0 case is NOT evidence for the heard bar: we heard
all 35 that round, so the heard bar was also 29. It is evidence for the
certainty band staying conservative, and it lives with the band in
roll_call_card.build_card.
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


def _card(pd_yes, seen=34):
    """_round defaults to seen=34, unl=35 -> needed_full 29, heard bar 28.

    `seen` is overridable so the fixCleanup3_4_0 test can model what
    actually happened that round: all 35 heard.
    """
    rounds = [
        _round(107249407, "2026-10-09T13:40:13Z",
               {PD: (pd_yes, pd_yes, True), OTHER: (16, 16, False)}, seen=seen),
        _round(107249151, "2026-10-09T13:23:31Z",
               {PD: (pd_yes, pd_yes, True), OTHER: (16, 16, False)}, seen=seen),
    ]
    card = C.build_card(rounds, IN_FLIGHT, NOW)
    return next(r for r in card["rows"] if r["hash"] == PD), card


# ── change 2 data layer: spare_votes ──

def test_needed_full_is_29_on_a_35_validator_unl():
    row, card = _card(31)
    assert card["unl_full"] == 35
    assert card["needed_full"] == 29


def test_spare_votes_positive_above_the_bar():
    # 31 yes against the heard bar of 28 (34 heard) -> 3 to spare.
    row, _ = _card(31)
    assert row["spare_votes"] == 3


def test_spare_votes_zero_exactly_at_the_bar():
    # "At the bar" is now 28 yes (34 heard), not 29. The INPUT moves so the
    # test keeps testing the relationship its name describes.
    row, _ = _card(28)
    assert row["spare_votes"] == 0


def test_spare_votes_negative_below_the_bar():
    # 27 yes against the heard bar of 28 -> one short.
    row, _ = _card(27)
    assert row["spare_votes"] == -1
    # short_by stays the positive mirror, unchanged behaviour.
    assert row["short_by"] == 1


def test_spare_votes_matches_the_fixcleanup_case():
    """The real 2026-10-09 case, now modelled faithfully: all 35 heard.

    Verified against our own recorded roll call (round 107536127, observed
    2026-10-09T09:34:33Z): validations_seen 35, votes_needed 29,
    yes_carried 28, passes_rippled FALSE — while
    amendment_majority_history recorded majority_close 09:34:30Z.

    So 28 yes is one SHORT by our count even though the ledger granted the
    majority. With all 35 heard the heard bar IS 29, so this expectation
    does not move — which is exactly why this case cannot be used as
    evidence for the heard bar. The fixture previously ran at 34 heard and
    only looked like it reproduced the event.
    """
    row, card = _card(28, seen=35)
    assert card["needed_heard"] == 29 and card["needed_full"] == 29
    assert row["spare_votes"] == -1


# ── rendered page: the REAL route + REAL templates, fakes only ──

import app as app_mod  # noqa: E402


def _render(monkeypatch, pd_yes):
    state = {
        "ok": True, "enabled_count": 95, "in_flight_count": 12,
        "ledger_index": 107536999,
        "recognized_enabled": [], "unrecognized_enabled": [],
        "unrecognized_enabled_count": 0,
        "in_flight": list(IN_FLIGHT), "superseded": [], "superseded_count": 0,
        "majorities": [{
            "hash": PD, "name": "PermissionDelegationV1_1", "recognized": True,
            "known_meta": None,
            "majority_reached_iso": "2026-09-25T14:12:51Z",
            "activation_eta_iso": "2026-10-20T14:12:51Z",
        }],
        "network_votes_source": {}, "in_development": [],
        "in_development_count": 0, "sourcing": "sovereign",
        "cached_age_seconds": 0.0, "fetched_at_iso": "2026-10-09T13:45:00Z",
    }
    rounds = [
        _round(107249407, "2026-10-09T13:40:13Z",
               {PD: (pd_yes, pd_yes, True), OTHER: (16, 16, False)}),
        _round(107249151, "2026-10-09T13:23:31Z",
               {PD: (pd_yes, pd_yes, True), OTHER: (16, 16, False)}),
    ]
    monkeypatch.setattr(app_mod, "fetch_amendments_state_cached",
                        lambda *a, **k: dict(state))
    monkeypatch.setattr(app_mod, "_load_amendment_majority_history",
                        lambda *a, **k: [])
    monkeypatch.setattr(C, "is_enabled", lambda *a, **k: True)
    monkeypatch.setattr(C, "read_rounds", lambda cur: rounds)
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn(_Cur):
        def cursor(self):
            return _Cur()

    monkeypatch.setattr(db, "pg_connect", lambda: _Conn())
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        r = c.get("/amendments")
    assert r.status_code == 200
    return r.data.decode()


def _subbox(html):
    """The rendered roll-call sub-box only. Anchor on data-roll-call-box:
    the class name `.countdown-rollcall` also appears in the <style> block
    above the markup, and slicing from there returns stylesheet text."""
    i = html.find("data-roll-call-box")
    assert i != -1, "roll-call sub-box not rendered"
    j = html.find("</details>", i)
    return html[html.rfind("<", 0, i):j if j != -1 else len(html)]


# change 1

def test_needs_line_is_attributed_to_our_node(monkeypatch):
    box = _subbox(_render(monkeypatch, 31))
    assert "to keep its countdown" in box
    assert "by our node&#39;s count" in box or "by our node's count" in box


def test_ledger_decision_is_the_final_word(monkeypatch):
    box = _subbox(_render(monkeypatch, 31))
    assert 'data-ledger-verdict="in-majorities"' in box
    assert "The ledger decides, not our count:" in box
    assert "in Majorities" in box
    assert "the countdown is running." in box


# change 2

def test_votes_to_spare_replaces_over_the_bar_sentence(monkeypatch):
    box = _subbox(_render(monkeypatch, 31))
    assert "3 votes to spare" in box
    assert "Already over the bar" not in box


def test_singular_one_vote_to_spare(monkeypatch):
    # One to spare is now 29 yes against the heard bar of 28.
    box = _subbox(_render(monkeypatch, 29))
    assert "1 vote to spare" in box
    assert "1 votes to spare" not in box


def test_exactly_at_the_bar_is_at_risk_with_no_spare(monkeypatch):
    # "At the bar" is 28 yes against the heard bar of 28.
    box = _subbox(_render(monkeypatch, 28))
    assert "at risk: no votes to spare" in box
    assert "votes to spare" in box


def test_below_the_bar_says_how_many_more(monkeypatch):
    box = _subbox(_render(monkeypatch, 27))
    assert "at risk: needs 1 more" in box


def test_old_count_state_sentences_are_gone(monkeypatch):
    """All three retired sentences, at every margin."""
    for yes in (31, 29, 27):
        box = _subbox(_render(monkeypatch, yes))
        assert "Already over the bar" not in box
        assert "Too close to call from our node" not in box
        assert "it would still fall short" not in box


# change 3

def test_majority_reached_drops_at_fetch(monkeypatch):
    html = _render(monkeypatch, 31)
    assert "majority reached at fetch" not in html
    assert "majority reached" in html
