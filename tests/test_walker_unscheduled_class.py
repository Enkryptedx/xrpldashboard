"""compute_walker_staleness: the UNSCHEDULED class (owner ruling 2026-11-02).

A walker with `cadence_seconds IS NULL` returned `is_stale=False` and so
rendered green forever. That is correct for event-triggered and manual jobs
-- "stale" needs a schedule to be late against -- but it also meant the five
walkers retired on 2026-11-02 sat with last runs in Sept/Oct, nothing loaded
in launchd, and were never once flagged by this function.

The fix NAMES the condition instead of inventing a threshold for work that
has no schedule: `is_stale` stays False, and `no_cadence` / `unscheduled`
carry the real state so /coverage can render UNSCHEDULED as its own class.

Paused walkers are exempt by owner requirement -- paused_walkers.py is the
authoritative pause declaration, and escrow_supply_walker must not start
alarming off this change.

Hermetic: pure arithmetic + one monkeypatched predicate. No DB, no network.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db  # noqa: E402
import paused_walkers  # noqa: E402

NOW = 1_800_000_000


class TestNoCadenceIsUnscheduledNotGreen:
    def test_no_cadence_is_flagged_unscheduled(self):
        out = db.compute_walker_staleness(None, NOW - 999_999, NOW,
                                          walker_name="some_dead_walker")
        assert out["no_cadence"] is True
        assert out["unscheduled"] is True, (
            "a walker with no schedule must render UNSCHEDULED, not green")

    def test_no_cadence_still_not_claimed_stale(self):
        """is_stale stays False on purpose: without a cadence there is no
        interval to be late against, and inventing one would be a
        fabricated claim. UNSCHEDULED is the honest state."""
        out = db.compute_walker_staleness(None, None, NOW,
                                          walker_name="some_dead_walker")
        assert out["is_stale"] is False
        assert out["unscheduled"] is True

    def test_scheduled_walker_is_not_unscheduled(self):
        out = db.compute_walker_staleness(3600, NOW - 60, NOW,
                                          walker_name="healthy_walker")
        assert out["no_cadence"] is False
        assert out["unscheduled"] is False
        assert out["is_stale"] is False


class TestPausedIsExempt:
    def test_paused_no_cadence_is_not_unscheduled(self, monkeypatch):
        """escrow_supply_walker's exact shape: paused, cadence NULLed. It
        must stay quiet -- paused_walkers.py is authoritative."""
        monkeypatch.setattr(paused_walkers, "is_paused",
                            lambda n: n == "escrow_supply_walker")
        out = db.compute_walker_staleness(None, NOW - 9_999_999, NOW,
                                          walker_name="escrow_supply_walker")
        assert out["paused"] is True
        assert out["unscheduled"] is False, (
            "a paused walker must not alarm as UNSCHEDULED")
        assert out["is_stale"] is False

    def test_paused_overdue_with_cadence_is_not_stale(self, monkeypatch):
        monkeypatch.setattr(paused_walkers, "is_paused", lambda n: True)
        out = db.compute_walker_staleness(3600, NOW - 9_999_999, NOW,
                                          walker_name="anything")
        assert out["is_stale"] is False
        assert out["paused"] is True

    def test_unpaused_overdue_is_still_stale(self, monkeypatch):
        """The exemption must not swallow genuine staleness."""
        monkeypatch.setattr(paused_walkers, "is_paused", lambda n: False)
        out = db.compute_walker_staleness(3600, NOW - 9_999_999, NOW,
                                          walker_name="anything")
        assert out["is_stale"] is True
        assert out["paused"] is False


class TestBackwardCompatibility:
    def test_walker_name_is_optional(self):
        """Existing callers pass three positional args; they must keep
        working and must not crash on the paused lookup."""
        out = db.compute_walker_staleness(3600, NOW - 9_999_999, NOW)
        assert out["is_stale"] is True
        assert out["paused"] is False

    def test_original_keys_all_survive(self):
        out = db.compute_walker_staleness(3600, NOW - 100, NOW)
        for k in ("is_stale", "stale_seconds", "stale_threshold"):
            assert k in out, f"{k} is a pre-existing contract key"
        assert out["stale_threshold"] == max(3 * 3600, 1800)

    def test_paused_lookup_failure_cannot_break_the_read(self, monkeypatch):
        """This is a read-time helper on every /coverage render; an import
        or predicate blowing up must never take the page down."""
        def _boom(_n):
            raise RuntimeError("paused_walkers exploded")
        monkeypatch.setattr(paused_walkers, "is_paused", _boom)
        out = db.compute_walker_staleness(3600, NOW - 9_999_999, NOW,
                                          walker_name="anything")
        assert out["is_stale"] is True
        assert out["paused"] is False
