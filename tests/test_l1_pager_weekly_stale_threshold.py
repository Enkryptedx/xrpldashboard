"""Cadence-tiered STALE threshold for L1 pager.

Filed alongside the enrich_token_names plist drift wound (2026-08-29).
Weekly-and-longer walkers used to inherit the 3× multiplier meant for
daily-and-shorter walkers, allowing 3 missed cycles = 21d before firing.
Tightened to 2× + 1d grace at cadence ≥ weekly. Sub-weekly unchanged.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import l1_pager as p


class TestSubWeeklyUnchanged:
    """Daily and shorter walkers keep the historical 3× / 24h-floor
    formula. Regression guard for daily walkers whose alert semantics
    haven't changed."""

    def test_daily_cadence_still_3d(self):
        assert p._stale_threshold_for(86400) == 3 * 86400

    def test_six_hour_cadence_floors_to_24h(self):
        # 6h*3 = 18h < 24h floor
        assert p._stale_threshold_for(21600) == p.STALE_FLOOR_SECONDS

    def test_hourly_cadence_floors_to_24h(self):
        assert p._stale_threshold_for(3600) == p.STALE_FLOOR_SECONDS


class TestWeeklyTightened:
    """Weekly-and-longer walkers use 2× + 1d grace, which is tighter
    than the old 3× formula for the same cadence."""

    def test_weekly_threshold_is_15d(self):
        # 604800*2 + 86400 = 1_296_000 s = 15 days
        assert p._stale_threshold_for(604800) == 15 * 86400

    def test_biweekly_threshold_is_29d(self):
        # 1209600*2 + 86400 = 2_505_600 s = 29 days
        assert p._stale_threshold_for(2 * 604800) == 29 * 86400

    def test_weekly_is_tighter_than_pre_change_would_have_been(self):
        # Pre-change formula: max(3*604800, 86400) = 21d.
        # New:                              2*604800 + 86400 = 15d.
        assert p._stale_threshold_for(604800) < 3 * 604800
