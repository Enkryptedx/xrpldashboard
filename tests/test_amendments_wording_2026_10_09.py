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
    assert "2 votes to spare" in box
    assert "Already over the bar" not in box


def test_singular_one_vote_to_spare(monkeypatch):
    box = _subbox(_render(monkeypatch, 30))
    assert "1 vote to spare" in box
    assert "1 votes to spare" not in box


def test_exactly_at_the_bar_is_at_risk_with_no_spare(monkeypatch):
    box = _subbox(_render(monkeypatch, 29))
    assert "at risk: no votes to spare" in box
    assert "votes to spare" in box


def test_below_the_bar_says_how_many_more(monkeypatch):
    box = _subbox(_render(monkeypatch, 27))
    assert "at risk: needs 2 more" in box


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
