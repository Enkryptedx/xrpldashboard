"""The mpt_supply_history freshness canary.

Guards the gap that let the MPT holders walk sit dead for 88 days: every
existing signal asked the WALKER how its run went, and the run went fine.
`mpt_holders_refresh` exited 0 on all ~2000 runs after the break while
writing `history_rows=0` each time, so walker_health's ok axis never moved.
This canary asks the only question that cannot be answered falsely by a
walker that does nothing -- did the TABLE grow.

Tests run with no DB and no network: `db.pg_available` / `db.pg_connect`
are monkeypatched, and `check()` takes an injectable `now_ts` so a row can
be placed either side of the 13-hour ceiling without touching the clock.
"""

import datetime as dt

import pytest

import mpt_supply_history_canary as canary


NOW = 1795000000  # fixed reference instant for every case


class _FakeCursor:
    def __init__(self, newest, count, raise_on_query=False):
        self._newest = newest
        self._count = count
        self._raise = raise_on_query
        self._last = None

    def execute(self, sql, params=None):
        if self._raise:
            raise RuntimeError("connection is lost")
        self._last = "max" if "max(snapshot_ts)" in sql else "count"

    def fetchone(self):
        return (self._newest,) if self._last == "max" else (self._count,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, newest, count, raise_on_query=False):
        self._args = (newest, count, raise_on_query)

    def cursor(self):
        return _FakeCursor(*self._args)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, newest, count=500, available=True,
          raise_on_query=False):
    monkeypatch.setattr(canary.db, "pg_available", lambda: available)
    monkeypatch.setattr(
        canary.db, "pg_connect",
        lambda *a, **k: _FakeConn(newest, count, raise_on_query))


class TestTheThreshold:
    def test_the_ceiling_is_thirteen_hours(self):
        """Owner instruction 2026-11-27, pinned as a number so a later edit
        has to be deliberate."""
        assert canary.STALE_CEILING_SECONDS == 13 * 3600 == 46800

    def test_the_ceiling_clears_two_writer_cycles(self):
        """Below 2x the writer's cadence the canary FLAPS: one skipped 6h
        run -- a sleeping Mac, a single Clio timeout -- would page. The
        13h ceiling must stay at least two cycles wide."""
        assert canary.STALE_CEILING_SECONDS >= 2 * canary.WRITER_CADENCE_SECONDS

    def test_the_writer_cadence_matches_the_real_walker(self):
        """If mpt_holders_refresh's cadence moves and this constant does
        not, the 'missed cycles' arithmetic in the page becomes wrong."""
        import mpt_holders_refresh
        assert (canary.WRITER_CADENCE_SECONDS
                == mpt_holders_refresh.WALKER_CADENCE_SECONDS)


class TestFreshSeries:
    def test_a_recent_row_is_clean(self, monkeypatch):
        _wire(monkeypatch, newest=NOW - 3600)
        r = canary.check(now_ts=NOW)
        assert r["findings"] == 0, r
        assert r["age_seconds"] == 3600
        assert r["reason"].startswith("fresh_")

    def test_exactly_at_the_ceiling_is_still_clean(self, monkeypatch):
        """Boundary belongs to the clean side: the rule is "no new rows FOR
        13 hours", so 13h on the dot has not yet exceeded it."""
        _wire(monkeypatch, newest=NOW - canary.STALE_CEILING_SECONDS)
        assert canary.check(now_ts=NOW)["findings"] == 0


class TestStaleSeries:
    def test_one_second_past_the_ceiling_fires(self, monkeypatch):
        _wire(monkeypatch, newest=NOW - canary.STALE_CEILING_SECONDS - 1)
        r = canary.check(now_ts=NOW)
        assert r["findings"] == 1, r
        assert "stale_" in r["reason"]

    def test_the_eighty_eight_day_outage_would_have_fired(self, monkeypatch):
        """THE regression case. Last good holder write was 2026-08-31
        05:36:17Z and the series did not advance again until today. This
        canary would have reported it on the very first run after the
        13-hour mark instead of 88 days later."""
        last_good = int(dt.datetime(2026, 8, 31, 5, 36, 17,
                                    tzinfo=dt.timezone.utc).timestamp())
        one_day_later = last_good + 86400
        _wire(monkeypatch, newest=last_good, count=0)
        r = canary.check(now_ts=one_day_later)
        assert r["findings"] == 1
        assert r["rows_in_window"] == 0
        assert "missed_writer_cycles" in r["reason"], r["reason"]

    def test_the_message_names_the_writer_so_the_page_is_actionable(
            self, monkeypatch):
        _wire(monkeypatch, newest=NOW - 99999, count=0)
        msg = canary._message(canary.check(now_ts=NOW))
        assert "mpt_holders_refresh" in msg
        assert "mpt_supply_history" in msg


class TestUnknownIsNeverTreatedAsFresh:
    """The whole point. A canary that cannot see its table knows nothing,
    and 'unknown == fine' is the failure mode this file exists to end."""

    def test_pg_unavailable_is_a_finding(self, monkeypatch):
        _wire(monkeypatch, newest=NOW, available=False)
        r = canary.check(now_ts=NOW)
        assert r["findings"] == 1, "unreadable PG reported as healthy"
        assert r["reason"] == "pg_unavailable"

    def test_a_query_failure_is_a_finding(self, monkeypatch):
        _wire(monkeypatch, newest=NOW, raise_on_query=True)
        r = canary.check(now_ts=NOW)
        assert r["findings"] == 1
        assert r["reason"].startswith("query_failed_")

    def test_an_empty_table_is_a_finding(self, monkeypatch):
        """Never-written is not fresh. A cold table reads as age-zero on a
        naive max()-is-null path."""
        _wire(monkeypatch, newest=None, count=0)
        r = canary.check(now_ts=NOW)
        assert r["findings"] == 1
        assert r["reason"] == "table_empty_no_rows_ever"

    def test_the_canary_never_raises_on_a_broken_db(self, monkeypatch):
        """A canary that crashes stops being a canary; walker_health would
        show a run failure rather than the finding it was built to report."""
        _wire(monkeypatch, newest=NOW, raise_on_query=True)
        canary.check(now_ts=NOW)  # must not raise


class TestAlertPosture:
    def test_findings_is_the_alert_axis_not_ok(self, monkeypatch):
        """Matches xrpl_env_drift_guard: ok=True whenever the canary itself
        completed, so consecutive_failures keeps meaning 'the check is
        broken' rather than 'the check found something'."""
        calls = {}
        monkeypatch.setattr(canary.db, "write_walker_health_start",
                            lambda *a, **k: None)
        monkeypatch.setattr(
            canary.db, "write_walker_health_end",
            lambda name, ok, message=None, findings_count=None: calls.update(
                name=name, ok=ok, message=message, findings=findings_count))
        _wire(monkeypatch, newest=0, count=0)  # maximally stale
        rc = canary.main()
        assert calls["ok"] is True, "a finding must not look like a crash"
        assert calls["findings"] == 1
        assert calls["name"] == "mpt_supply_history_canary"
        assert rc == 1, "exit code still signals the finding to launchd"

    def test_a_clean_run_exits_zero_with_no_findings(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(canary.db, "write_walker_health_start",
                            lambda *a, **k: None)
        monkeypatch.setattr(
            canary.db, "write_walker_health_end",
            lambda name, ok, message=None, findings_count=None: calls.update(
                ok=ok, findings=findings_count))
        _wire(monkeypatch, newest=NOW - 60)
        # main() reads the real clock, so put the row 60s before it.
        monkeypatch.setattr(
            canary, "check",
            lambda now_ts=None: {"findings": 0, "age_seconds": 60,
                                 "newest_ts": NOW - 60, "rows_in_window": 500,
                                 "reason": "fresh_60s",
                                 "ceiling_seconds":
                                     canary.STALE_CEILING_SECONDS})
        assert canary.main() == 0
        assert calls["ok"] is True
        assert calls["findings"] == 0
