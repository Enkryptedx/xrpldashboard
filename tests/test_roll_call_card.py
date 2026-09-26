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


def test_gate_requires_flag_and_not_before():
    after = dt.datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    before = dt.datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
    assert C.is_enabled(after, {}) is False                          # flag off
    assert C.is_enabled(before, {"ROLL_CALL_CARD_ENABLED": "1"}) is False  # too early
    assert C.is_enabled(after, {"ROLL_CALL_CARD_ENABLED": "1"}) is True
    assert C.NOT_BEFORE_UTC == dt.datetime(2026, 9, 27, 11, 44, 53, tzinfo=UTC)


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
    assert pd["status"] == "passing" and pd["yes_carried"] == 29 and pd["short_by"] == 0
    other = next(r for r in card["rows"] if r["hash"] == OTHER)
    assert other["status"] == "short" and other["short_by"] == 12
    assert card["any_reset"] is False


def test_reset_state_when_previous_round_passed():
    now = dt.datetime(2026, 9, 23, 12, 50, tzinfo=UTC)
    rounds = [
        _round(107181567, "2026-09-23T12:47:20Z", {PD: (28, 28, False)}, needed=29, thr=28, trusted=35, seen=35),
        _round(107181311, "2026-09-23T12:30:40Z", {PD: (29, 29, True)}, needed=29, thr=28, trusted=35, seen=35),
    ]
    card = C.build_card(rounds, [IN_FLIGHT[0]], now)
    pd = card["rows"][0]
    assert pd["status"] == "reset" and pd["prev_passes"] is True and pd["short_by"] == 1
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
    assert all(r["status"] == "short" and r["yes_carried"] == 0 and r["short_by"] == 28 for r in card["rows"])
