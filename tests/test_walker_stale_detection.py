"""Corpse-test for read-time STALE detection.

Each case is a walker in a state we have actually observed producing
false-GREEN or misleadingly-labeled STALE. If any of these regress,
we've broken the detector that catches Patient A (ledger_definitions,
macOS 26 LNP block) and Patient B (rank_amms, --reset hardcoded).

See project_walker_liveness_diagnoses_2026-08-06.md and
project_walker_liveness_cross_exam_2026-08-06.md.

Detector spec (verdict answer #6):
    is_stale iff cadence_seconds IS NOT NULL AND
                 (now - last_success_ts) > max(3 * cadence, 1800)
"""

import datetime as _dt

from db import compute_walker_staleness


NOW = _dt.datetime(2026, 8, 6, 22, 0, 0, tzinfo=_dt.timezone.utc)
NOW_TS = int(NOW.timestamp())


def _stale_at(hours=0, minutes=0, seconds=0):
    return NOW_TS - (hours * 3600 + minutes * 60 + seconds)


class TestCorpsePatientA:
    """ledger_definitions_walker observed at cross-exam origin:
    62h stale, cadence 6h (10× overdue). Root cause: macOS 26 Local
    Network Privacy blocks launchd from reaching Lenovo's LAN IP."""

    def test_ledger_definitions_62h_stale_is_stale_and_carries_label(self):
        out = compute_walker_staleness(
            cadence_seconds=21600,
            last_success_ts=_stale_at(hours=62),
            now_ts=NOW_TS,
        )
        assert out["is_stale"] is True, "Patient A must render STALE"
        assert out["stale_seconds"] == 62 * 3600
        assert out["stale_threshold"] == 3 * 21600  # 18h, well above 30m floor


class TestCorpsePatientB:
    """rank_amms observed at cross-exam origin: ~5h stale, cadence 1h
    (5× overdue). Root cause: launchd wrapper hardcodes --reset,
    walker cannot complete before next fire."""

    def test_rank_amms_5h_stale_is_stale(self):
        out = compute_walker_staleness(
            cadence_seconds=3600,
            last_success_ts=_stale_at(hours=5),
            now_ts=NOW_TS,
        )
        assert out["is_stale"] is True
        assert out["stale_seconds"] == 5 * 3600
        assert out["stale_threshold"] == max(3 * 3600, 1800)


class TestThresholdBoundaries:
    def test_at_3x_cadence_exactly_not_stale(self):
        # 3h stale with 1h cadence: threshold = 3*3600 = 10800.
        # stale_seconds = 10800; strict `>` means equal is not stale.
        out = compute_walker_staleness(3600, _stale_at(hours=3), NOW_TS)
        assert out["stale_seconds"] == 10800
        assert out["stale_threshold"] == 10800
        assert out["is_stale"] is False

    def test_at_3x_cadence_plus_1s_is_stale(self):
        out = compute_walker_staleness(3600, _stale_at(hours=3, seconds=1),
                                       NOW_TS)
        assert out["is_stale"] is True


class TestFloor:
    """30-min floor prevents short-cadence walkers from false-staling
    during slow deploys. A 60s-cadence walker at 10min stale is NOT
    stale (10m < 30m floor), even though 10m >> 3*60s = 3m."""

    def test_60s_cadence_10min_stale_not_stale_due_to_floor(self):
        out = compute_walker_staleness(60, _stale_at(minutes=10), NOW_TS)
        assert out["stale_threshold"] == 1800
        assert out["is_stale"] is False

    def test_60s_cadence_31min_stale_is_stale(self):
        out = compute_walker_staleness(60, _stale_at(minutes=31), NOW_TS)
        assert out["is_stale"] is True


class TestOptIn:
    """Walkers without declared cadence are opt-out — no STALE check.
    Event-triggered, manual, or paused walkers shouldn't false-fire.
    The cadence declaration IS the opt-in signal."""

    def test_no_cadence_never_stale_even_if_ancient(self):
        out = compute_walker_staleness(None, _stale_at(hours=24 * 365),
                                       NOW_TS)
        assert out["is_stale"] is False
        assert out["stale_threshold"] is None

    def test_zero_cadence_treated_as_no_cadence(self):
        out = compute_walker_staleness(0, _stale_at(hours=24), NOW_TS)
        assert out["is_stale"] is False


class TestNeverSucceeded:
    """A cadence-declared walker that has never successfully completed
    IS stale — the row exists but no run has ever finished ok."""

    def test_never_succeeded_with_cadence_is_stale(self):
        out = compute_walker_staleness(3600, None, NOW_TS)
        assert out["is_stale"] is True
        assert out["stale_seconds"] is None
        assert out["stale_threshold"] == 10800
