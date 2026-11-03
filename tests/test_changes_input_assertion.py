"""/changes input assertion — "nothing to compare" must never print "no change".

THE OUTAGE THIS PINS (2026-11-02 and 2026-11-03)
------------------------------------------------
The homepage "What's new" box published:

    AMM: no change today.  MPT: no change today.  RWA: no change today.
    See all 0 changes

for two consecutive days, while the signed-snapshot chain was in fact
advancing (leaf 167 → 168 → 169). The differ was never broken. The cause was
a SCHEDULING race created by the end of DST:

    producer  signed_snapshot   StartCalendarInterval Hour=21  (21:00 ET)
    consumer  changes_walker    StartInterval 86400            (~01:23 UTC)

Under EDT, 21:00 ET was 01:00 UTC, so the leaf landed 23 min BEFORE the
build. When DST ended on 2026-11-01, 21:00 ET became 02:00 UTC — the leaf
now lands 37 min AFTER the build. The builder read a day whose inputs did
not exist yet and reported confident silence.

Rescheduling alone is not a fix: `unl_snapshot` is StartInterval 86400 and
drifts, so no fixed slot can be guaranteed to follow it, and the March
switch would re-break any ET ordering. The durable guard is this assertion —
"we could not compare" becomes a first-class state that is never rendered
as "no change".

These tests fail against the old behaviour and pass against the fix.
Hermetic: a fake cursor, no DB, no network.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import changes_builder as cb  # noqa: E402

DATE = dt.date(2026, 11, 3)


class _Cur:
    """Fake cursor: answers the producer-presence probe per table.

    `present` is the set of table names that have a row for the date.
    """

    def __init__(self, present):
        self.present = set(present)
        self._last = None

    def execute(self, sql, params=None):
        self._last = None
        for _label, table, _col in cb._PRODUCER_INPUTS:
            if f"FROM {table} " in sql:
                self._last = (1,) if table in self.present else None
                return

    def fetchone(self):
        return self._last


ALL_TABLES = [t for _l, t, _c in cb._PRODUCER_INPUTS]


class TestProducerAssertion:
    def test_all_inputs_present_means_comparable(self):
        assert cb._assert_producer_inputs(_Cur(ALL_TABLES), DATE) == []

    def test_missing_signed_snapshot_is_detected(self):
        """The exact 11-02/11-03 shape: the leaf had not been written yet."""
        present = [t for t in ALL_TABLES if t != "signed_snapshots"]
        assert cb._assert_producer_inputs(_Cur(present), DATE) == ["signed_snapshot"]

    def test_all_three_producers_are_checked_not_just_signed_snapshot(self):
        missing = cb._assert_producer_inputs(_Cur([]), DATE)
        assert missing == ["signed_snapshot", "unl_snapshot",
                           "signed_registry_snapshot"], missing

    def test_unreadable_table_counts_as_missing(self):
        """Fails SAFE: an input we cannot read is also one we did not
        compare, so it must not license a 'no change' claim."""
        class Boom(_Cur):
            def execute(self, sql, params=None):
                raise RuntimeError("relation does not exist")
        assert len(cb._assert_producer_inputs(Boom([]), DATE)) == 3

    def test_registry_producer_points_at_a_real_table_and_column(self):
        """changes_walker's plist comment calls this producer
        'registry_state', but that is the dict METRIC inside the envelope —
        there is no such table. Guessing it would have made the assertion
        fail-safe on every build and permanently replaced the strip."""
        labels = {l: (t, c) for l, t, c in cb._PRODUCER_INPUTS}
        assert labels["signed_registry_snapshot"] == (
            "signed_registry_snapshots", "snapshot_date_utc")
        assert all(t != "registry_state" for _l, t, _c in cb._PRODUCER_INPUTS)


def _envelope(*, changes=None, inputs_missing=None):
    return {
        "date": "2026-11-03",
        "generated_at_utc": "2026-11-03T01:23:16Z",
        "changes": changes or [],
        "categories_no_change": [] if inputs_missing else ["amm", "mpt", "rwa"],
        "inputs_missing": inputs_missing or [],
        "comparable": not inputs_missing,
    }


class TestStripNeverClaimsNoChangeWhenUncomparable:
    def test_uncomparable_day_emits_comparison_unavailable_only(self):
        strip = cb.build_strip(_envelope(inputs_missing=["signed_snapshot"]), k=3)
        lines = " ".join(s["line"] for s in strip["slots"])
        assert "no change today" not in lines.lower(), lines
        assert "Comparison unavailable" in lines
        assert [s["kind"] for s in strip["slots"]] == ["comparison_unavailable"]

    def test_uncomparable_day_drops_the_biggest_things_headline(self):
        """Fix 4: the headline must not sit above a box reporting nothing."""
        strip = cb.build_strip(_envelope(inputs_missing=["unl_snapshot"]), k=3)
        assert strip["explainer"] is None
        assert strip["comparable"] is False

    def test_genuinely_quiet_day_says_no_changes_detected(self):
        strip = cb.build_strip(_envelope(), k=3)
        assert strip["explainer"] == "No changes detected in this window."
        assert strip["comparable"] is True

    def test_real_changes_keep_the_original_headline(self):
        env = _envelope(changes=[{
            "line": "Signed-snapshot chain advanced: leaf #168 → #169",
            "category": "chain", "source": "signed_snapshot",
            "metric_name": "signed_snapshot_leaf_index",
        }])
        strip = cb.build_strip(env, k=3)
        assert "biggest things that moved" in strip["explainer"]


class TestEtFirstStamp:
    def test_et_first_utc_in_parens(self):
        """Fix 3. 01:23 UTC is the PREVIOUS EVENING in ET, which is why a
        UTC-only stamp misreads as the middle of the night."""
        assert cb._fmt_as_of_et_first("2026-11-03T01:23:16Z") == \
            "8:23 PM ET (01:23 UTC)"

    def test_derived_from_zone_across_the_dst_switch(self):
        """EDT before 2026-11-01, EST after — same UTC wall clock, one hour
        apart in ET. Derived from the zone, never month arithmetic."""
        assert cb._fmt_as_of_et_first("2026-10-31T01:23:00Z").startswith("9:23 PM ET")
        assert cb._fmt_as_of_et_first("2026-11-03T01:23:00Z").startswith("8:23 PM ET")

    def test_missing_or_malformed_timestamp_is_not_fatal(self):
        assert cb._fmt_as_of_et_first(None) is None
        assert cb._fmt_as_of_et_first("not-a-timestamp") is None
