"""roll_call_card — pure builder + gate tests (no DB)."""
from __future__ import annotations
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import roll_call_card as C  # noqa: E402

UTC = dt.timezone.utc
PD = "0F48FF56D6D6F2A6C0A5F1B3D0B1C2D3E4F5A6B7C8D9E0F1A2B3C4D5E6F7A8B9"
OTHER = "9F287AED0000000000000000000000000000000000000000000000000000AAAA"


def _round(vl, iso, tallies, seen=34, unl=35, trusted=34, thr=27, needed=28):
    return {"voting_ledger": vl, "flag_ledger": vl + 1, "observed_iso": iso, "unl_size": unl,
            "seen": seen, "trusted_available": trusted, "threshold": thr, "needed": needed,
            "unl_sequence": 85, "tallies": tallies, "rounds_recorded": 8,
            "first_round_iso": "2026-09-26T11:44:53Z"}


IN_FLIGHT = [{"hash": PD, "name": "PermissionDelegationV1_1"}, {"hash": OTHER, "name": "Other"}]


def test_gate_requires_flag_only():
    # Not-before gate removed 2026-09-27 (one full day of recording elapsed).
    # The env flag alone now gates the card; time no longer matters.
    after = dt.datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    before = dt.datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
    assert C.is_enabled(after, {}) is False                                 # flag off
    assert C.is_enabled(before, {"ROLL_CALL_CARD_ENABLED": "1"}) is True    # no time gate now
    assert C.is_enabled(after, {"ROLL_CALL_CARD_ENABLED": "1"}) is True
    assert not hasattr(C, "NOT_BEFORE_UTC")


def test_card_counts_both_denominators_and_times():
    now = dt.datetime(2026, 9, 26, 13, 45, 0, tzinfo=UTC)
    rounds = [
        _round(107249407, "2026-09-26T13:40:13Z", {PD: (29, 29, True), OTHER: (16, 16, False)}),
        _round(107249151, "2026-09-26T13:23:31Z", {PD: (29, 29, True), OTHER: (16, 16, False)}),
    ]
    card = C.build_card(rounds, IN_FLIGHT, now)
    assert card["voting_ledger"] == 107249407 and card["flag_ledger"] == 107249408
    assert card["seen"] == 34 and card["unl_size"] == 35 and card["trusted_available"] == 34
    assert card["threshold"] == 27 and card["needed"] == 28
    assert card["observed_utc"] == "2026-09-26 13:40:13 UTC"
    assert card["observed_et"] == "9:40:13 AM ET"
    assert card["stale"] is False and card["age_min"] == 5
    assert card["next_voting_ledger"] == 107249407 + 256
    # measured interval: 1002 s / 256 ledgers ≈ 3.91 s → next ≈ 13:56:55 → ~12 min from 13:45
    assert 3.8 <= card["seconds_per_ledger"] <= 4.0
    assert card["next_eta_min"] == 12
    assert card["source"].startswith("own-node")
    pd = next(r for r in card["rows"] if r["hash"] == PD)
    assert pd["status"] == "passing" and pd["yes_carried"] == 29
    # Full-UNL bar: 35 -> threshold 28, needs 29. PD at 29 heard = passing for sure.
    assert card["unl_full"] == 35 and card["needed_full"] == 29 and card["not_heard"] == 1
    assert pd["count_state"] == "passing" and pd["short_by"] == 0
    other = next(r for r in card["rows"] if r["hash"] == OTHER)
    # OTHER at 16 heard, not_heard=1 -> best_possible 17 < 29 -> short for sure.
    assert other["status"] == "short" and other["count_state"] == "short"
    assert card["any_reset"] is False


def test_too_close_when_unheard_votes_could_decide():
    # PD at exactly 28 heard, 1 validator not heard, full-UNL needs 29.
    # 28 < 29 (not passing) but 28+1 = 29 >= 29 -> the unheard vote could
    # decide it -> amber "too close to call", never green or red.
    now = dt.datetime(2026, 9, 26, 13, 45, 0, tzinfo=UTC)
    rounds = [
        _round(107249407, "2026-09-26T13:40:13Z", {PD: (28, 28, True)}),
        _round(107249151, "2026-09-26T13:23:31Z", {PD: (28, 28, True)}),
    ]
    card = C.build_card(rounds, [IN_FLIGHT[0]], now)
    pd = card["rows"][0]
    assert card["needed_full"] == 29 and card["not_heard"] == 1
    assert pd["count_state"] == "too_close" and pd["best_possible"] == 29


def test_headline_is_ledger_truth_not_vote_count():
    # THE BUG (Charlie 2026-09-27): the ledger's Amendments Majorities object
    # says PD's majority is HOLDING (active, not removed) and the box counts
    # to Oct 8 — but our node has only heard 28 of the needed 29 yes votes.
    # The headline MUST read "Holding" from ledger truth; the shortfall only
    # drives the amber count line. It must never flip the headline to
    # "Countdown restarted" off a vote-count/epoch-count heuristic.
    now = dt.datetime(2026, 9, 27, 13, 45, 0, tzinfo=UTC)
    rounds = [
        _round(107249407, "2026-09-27T13:40:13Z", {PD: (28, 28, True)}),
        _round(107249151, "2026-09-27T13:23:31Z", {PD: (28, 28, True)}),
    ]
    # Ledger truth: PD majority is active/holding right now.
    card = C.build_card(rounds, [IN_FLIGHT[0]], now, majority_active={PD: True})
    pd = card["rows"][0]
    # Headline input: ledger says holding.
    assert pd["ledger_holding"] is True
    # Count line still honestly amber (28 heard, 1 unheard, needs 29).
    assert pd["count_state"] == "too_close" and pd["best_possible"] == 29
    # And when the ledger says the run was removed, the SAME vote count must
    # instead read as a restarted headline — proving the headline follows the
    # ledger flag, not the count.
    card2 = C.build_card(rounds, [IN_FLIGHT[0]], now, majority_active={PD: False})
    assert card2["rows"][0]["ledger_holding"] is False
    assert card2["rows"][0]["count_state"] == "too_close"  # count line unchanged
    # Unknown hash (walker has no row) -> None -> template omits the headline.
    card3 = C.build_card(rounds, [IN_FLIGHT[0]], now, majority_active={})
    assert card3["rows"][0]["ledger_holding"] is None


def test_tallied_hash_absent_from_in_flight_still_gets_its_count():
    # THE BUG (Charlie 2026-09-27): fixBatchV1_2 (14A2B45E) is tallied by the
    # recorder (32/32/passes, our node runs 3.4.1 and supports it) but is NOT
    # in state['in_flight'] (no in-flight name). The old builder only walked
    # in_flight, so its box showed "not in our roll call" instead of its real
    # count. Rows must be built from the TALLIES, keyed by HASH; a tally row
    # with a null in-flight name must still surface its count (name falls back
    # to the short hash, and the template can name it from the majority box).
    FB = "14A2B45E48A4A124D1BBA657AC7B0DC3D5EA8C256C89E8F0D8142D32960A7944"
    now = dt.datetime(2026, 9, 27, 13, 45, 0, tzinfo=UTC)
    rounds = [
        _round(107249407, "2026-09-27T13:40:13Z", {FB: (32, 32, True)}),
        _round(107249151, "2026-09-27T13:23:31Z", {FB: (32, 32, True)}),
    ]
    # in_flight is EMPTY -> the old code produced no row for FB at all.
    card = C.build_card(rounds, [], now, majority_active={FB: True})
    fb = next((r for r in card["rows"] if r["hash"] == FB), None)
    assert fb is not None, "tallied hash must get a row even when absent from in_flight"
    assert fb["yes_carried"] == 32 and fb["count_state"] == "passing"
    assert fb["ledger_holding"] is True
    # Null in-flight name -> name falls back to the short hash, count still shown.
    assert fb["name"] == FB[:8]


def test_row_matches_by_hash_even_with_named_inflight_entry():
    # Same hash present in in_flight WITH a name, and tallied -> one row, named,
    # count shown. Proves the union is keyed by hash (no duplicate, name wins).
    FB = "14A2B45E48A4A124D1BBA657AC7B0DC3D5EA8C256C89E8F0D8142D32960A7944"
    now = dt.datetime(2026, 9, 27, 13, 45, 0, tzinfo=UTC)
    rounds = [
        _round(107249407, "2026-09-27T13:40:13Z", {FB: (32, 32, True)}),
        _round(107249151, "2026-09-27T13:23:31Z", {FB: (32, 32, True)}),
    ]
    card = C.build_card(rounds, [{"hash": FB, "name": "fixBatchV1_2"}], now,
                        majority_active={FB: True})
    fbs = [r for r in card["rows"] if r["hash"] == FB]
    assert len(fbs) == 1 and fbs[0]["name"] == "fixBatchV1_2"
    assert fbs[0]["yes_carried"] == 32 and fbs[0]["count_state"] == "passing"


def test_reset_state_when_previous_round_passed():
    now = dt.datetime(2026, 9, 23, 12, 50, tzinfo=UTC)
    rounds = [
        _round(107181567, "2026-09-23T12:47:20Z", {PD: (28, 28, False)}, needed=29, thr=28, trusted=35, seen=35),
        _round(107181311, "2026-09-23T12:30:40Z", {PD: (29, 29, True)}, needed=29, thr=28, trusted=35, seen=35),
    ]
    card = C.build_card(rounds, [IN_FLIGHT[0]], now)
    pd = card["rows"][0]
    assert pd["status"] == "reset" and pd["prev_passes"] is True
    assert card["any_reset"] is True


def test_stale_recorder_flagged():
    now = dt.datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
    rounds = [_round(107249407, "2026-09-26T13:40:13Z", {PD: (29, 29, True)})]
    card = C.build_card(rounds, IN_FLIGHT, now)
    assert card["stale"] is True and card["age_min"] == 80
    assert card["next_overdue"] is True
    assert card["seconds_per_ledger"] == C.FALLBACK_SECONDS_PER_LEDGER  # single round → fallback


def test_no_rounds_means_no_card():
    assert C.build_card([], IN_FLIGHT, dt.datetime.now(UTC)) is None


def test_amendment_missing_from_tallies_reads_zero_short():
    now = dt.datetime(2026, 9, 26, 13, 45, tzinfo=UTC)
    rounds = [_round(107249407, "2026-09-26T13:40:13Z", {})]
    card = C.build_card(rounds, IN_FLIGHT, now)
    # 0 heard yes, not_heard=1, needs 29 -> best_possible 1 < 29 -> short for sure.
    assert all(r["status"] == "short" and r["yes_carried"] == 0 and r["count_state"] == "short" for r in card["rows"])


# ---------------------------------------------------------------------------
# 2026-10-08: enabled amendments keep getting yes votes. Name them from the
# enabled list (feature RPC knows every name) and show state "enabled"
# instead of a bare hash + "majority".
# ---------------------------------------------------------------------------
def test_enabled_amendment_keeps_name_and_shows_enabled_state():
    now = dt.datetime(2026, 10, 8, 21, 40, 0, tzinfo=UTC)
    rounds = [
        _round(107524991, "2026-10-08T21:38:00Z", {PD: (30, 30, True), OTHER: (35, 35, True)}),
        _round(107524735, "2026-10-08T21:21:00Z", {PD: (30, 30, True), OTHER: (35, 35, True)}),
    ]
    # PD just activated: it is no longer in in_flight, only in recognized_enabled.
    card = C.build_card(rounds, [IN_FLIGHT[1]], now,
                        enabled=[{"hash": PD, "name": "PermissionDelegationV1_1"}])
    by_hash = {r["hash"]: r for r in card["rows"]}
    pd = by_hash[PD]
    assert pd["name"] == "PermissionDelegationV1_1"          # not "0F48FF56"
    assert pd["enabled"] is True and pd["status"] == "enabled"
    assert by_hash[OTHER]["enabled"] is False and by_hash[OTHER]["status"] == "passing"
    assert card["any_reset"] is False
    # Every row has a real name — no bare-hash fallback anywhere.
    assert all(len(r["name"]) != 8 or not all(c in "0123456789ABCDEF" for c in r["name"])
               for r in card["rows"])


def test_enabled_hash_without_name_still_falls_back_safely():
    now = dt.datetime(2026, 10, 8, 21, 40, 0, tzinfo=UTC)
    rounds = [_round(107524991, "2026-10-08T21:38:00Z", {PD: (30, 30, True)})]
    card = C.build_card(rounds, [], now, enabled=[{"hash": PD, "name": None}])
    assert card["rows"][0]["status"] == "enabled" and card["rows"][0]["name"] == PD[:8]


def test_load_for_page_passes_recognized_enabled(monkeypatch):
    seen = {}
    def fake_build(rounds, in_flight, now, **kw):
        seen.update(kw); return {"rows": []}
    monkeypatch.setattr(C, "build_card", fake_build)
    monkeypatch.setattr(C, "is_enabled", lambda *a, **k: True)
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class _Conn(_Cur):
        def cursor(self): return _Cur()
    monkeypatch.setattr(db, "pg_connect", lambda: _Conn())
    monkeypatch.setattr(C, "read_rounds", lambda cur: [])
    state = {"in_flight": [IN_FLIGHT[1]], "recognized_enabled": [{"hash": PD, "name": "PermissionDelegationV1_1"}]}
    C.load_for_page(state, majority_active={})
    assert seen["enabled"] == state["recognized_enabled"]


def test_template_renders_enabled_state_green():
    """Real template: an enabled row shows 'enabled', never 'majority'."""
    import app as app_mod
    rounds = [_round(107524991, "2026-10-08T21:38:00Z", {PD: (30, 30, True), OTHER: (35, 35, True)})]
    now = dt.datetime(2026, 10, 8, 21, 40, 0, tzinfo=UTC)
    card = C.build_card(rounds, [IN_FLIGHT[1]], now,
                        enabled=[{"hash": PD, "name": "PermissionDelegationV1_1"}])
    with app_mod.app.test_request_context("/amendments"):
        from flask import render_template_string
        html = render_template_string(
            "{% for r in roll_call.rows %}<tr data-roll-call-row=\"{{ r.status }}\"><td class=\"name\">{{ r.name }}</td>"
            "<td class=\"state\">{% if r.enabled %}<span style=\"color: var(--green, #22c55e); font-weight: 600;\">{{ _('enabled') }}</span>"
            "{% elif r.status == 'passing' %}<span>{{ _('majority') }}</span>{% endif %}</td></tr>{% endfor %}",
            roll_call=card)
    assert "PermissionDelegationV1_1" in html and "0F48FF56</td>" not in html
    import re
    pd_row = re.search(r'<tr data-roll-call-row="enabled">.*?</tr>', html).group(0)
    assert ">enabled<" in pd_row and "majority" not in pd_row
