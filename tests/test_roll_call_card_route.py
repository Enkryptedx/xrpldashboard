"""/amendments renders the roll-call card only when roll_call_card.load_for_page
returns one; absent card → no markup; DB never touched (no DATABASE_URL)."""
from __future__ import annotations
import datetime as dt
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import roll_call_card as C  # noqa: E402

FAKE_CARD = {
    "voting_ledger": 107249407, "flag_ledger": 107249408,
    "observed_utc": "2026-09-26 13:40:13 UTC", "observed_et": "9:40:13 AM ET",
    "age_min": 5, "stale": False, "seen": 34, "unl_size": 35, "trusted_available": 34,
    "threshold": 27, "needed": 28, "needed_heard": 28, "unl_sequence": 85,
    "unl_full": 35, "threshold_full": 28, "needed_full": 29, "not_heard": 1,
    "next_voting_ledger": 107249663, "next_eta_min": 12, "next_overdue": False,
    "seconds_per_ledger": 3.91,
    "rows": [
        {"hash": "0F48", "name": "PermissionDelegationV1_1", "yes_round": 29, "yes_carried": 29, "needed": 29, "spare_votes": 0,
         "passes": True, "prev_passes": True, "status": "passing", "short_by": 0,
         "count_state": "passing", "not_heard": 1, "needed_full": 29, "best_possible": 30,
         "ledger_holding": True},
        {"hash": "9F28", "name": "Other", "yes_round": 16, "yes_carried": 16, "needed": 29, "spare_votes": -13,
         "passes": False, "prev_passes": True, "status": "reset", "short_by": 12,
         "count_state": "short", "not_heard": 1, "needed_full": 29, "best_possible": 17,
         "ledger_holding": False},
        {"hash": "5A5A", "name": "Third", "yes_round": 6, "yes_carried": 6, "needed": 29, "spare_votes": -23,
         "passes": False, "prev_passes": False, "status": "short", "short_by": 22,
         "count_state": "too_close", "not_heard": 1, "needed_full": 29, "best_possible": 29,
         "ledger_holding": True},
    ],
    "any_reset": True, "rounds_recorded": 8, "first_round_iso": "2026-09-26T11:44:53Z",
    "source": C.SOURCE_LABEL,
}


#: The real load_for_page signature, captured at import time - before any
#: monkeypatch can replace it.
_REAL_LOAD_SIG = inspect.signature(C.load_for_page)


def _fake_load_for_page(card):
    """A load_for_page stand-in CHECKED against the real signature.

    Why this is not a plain lambda any more. The old fakes were
    `lambda state, now=None, majority_active=None: ...`. When
    `current_validated_ledger` was added to the real function (ITEM A
    flag-counter work, 2026-10-02) the route started passing it, the fake
    raised TypeError, and the route's `except Exception: roll_call = None`
    swallowed it - so the card SILENTLY VANISHED from the page and these
    tests read "the card did not render" instead of "your fake is stale".
    That cost a full diagnosis pass on 2026-10-10.

    This version takes *args/**kwargs so a newly added parameter can never
    break it again, but binds the REAL signature first - so if the route
    ever passes something the real function does not accept, the test fails
    LOUDLY at the call instead of the card disappearing.
    """
    def fake(*args, **kwargs):
        _REAL_LOAD_SIG.bind(*args, **kwargs)
        return dict(card)
    return fake


def test_route_call_shape_still_matches_the_real_signature():
    """Pins the exact kwargs app.py:6630 passes. If the route's call and the
    real signature ever diverge, this names it instead of a vanished card."""
    _REAL_LOAD_SIG.bind({}, majority_active={}, current_validated_ledger=1)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_card_hidden_by_default(client, monkeypatch):
    monkeypatch.delenv("ROLL_CALL_CARD_ENABLED", raising=False)
    r = client.get("/amendments")
    assert r.status_code == 200
    assert b"data-roll-call" not in r.data
    assert C.is_enabled(dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc), {}) is False


def test_card_renders_counts_only_and_reset_state(client, monkeypatch):
    monkeypatch.setattr(C, "load_for_page", _fake_load_for_page(FAKE_CARD))
    r = client.get("/amendments")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    assert 'data-roll-call data-voting-ledger="107249407"' in html
    assert "107,249,407" in html and "107,249,408" in html
    assert "9:40:13 AM ET" in html and "2026-09-26 13:40:13 UTC" in html
    assert "34" in html and "of</strong>" not in html  # denominators rendered via the strong block
    # PR #45 replaced "a majority needs 28" with "needs 28 of 34".
    assert "needs" in html and "<strong>28 of 34</strong>" in html
    assert "in about" in html and "12" in html
    assert 'data-roll-call-row="reset"' in html and "RESET" in html
    assert 'data-roll-call-row="passing"' in html
    assert "short by" in html
    assert "Counts only." in html and "own-node validations stream" in html
    # standing rule: no validator identities on the public surface
    assert "nHU" not in html and "validation_public_key" not in html


def test_card_stale_state_renders_red_notice(client, monkeypatch):
    card = dict(FAKE_CARD, stale=True, age_min=80, any_reset=False, rows=[])
    monkeypatch.setattr(C, "load_for_page", _fake_load_for_page(card))
    r = client.get("/amendments")
    html = r.data.decode("utf-8")
    assert "Recorder stale:" in html and "80" in html
    assert "Next roll call" not in html
