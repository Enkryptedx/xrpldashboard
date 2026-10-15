"""Paused-walker alarm hardening (2026-10-15).

Wound: `supply_escrow_walker` was paused 2026-10-14 14:39Z mid-run. The
pause convention is `cadence_seconds = NULL`, which makes walker_stale
skip the row — but nothing closed the in-flight row, so it sat at
`last_run_message='run_in_progress'` / `last_run_completed IS NULL` for
17.5h, indistinguishable from a walker wedged mid-run. Meanwhile its
failure streak was frozen at `consecutive_failures=3`, so the FAILING
check — which had no cadence filter — kept paging about a walker that
was deliberately not running.

Three behaviours locked here:
  1. walker_failing SKIPS cadence_seconds IS NULL (paused ≠ failing).
  2. pause_walker CLOSES the in-flight row as "paused", without
     manufacturing either a success or a failure.
  3. walker_stuck catches a stale run_in_progress and deliberately does
     NOT filter on cadence — otherwise (1) would create a blind spot
     exactly where this wound lived.
"""
from __future__ import annotations

import datetime as dt
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import l1_pager as p  # noqa: E402


# ── fakes ───────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, rows=(), rowcount=1):
        self.rows = list(rows)
        self.statements = []
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(self, rows=(), rowcount=1):
        self.cur = FakeCursor(rows, rowcount)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur


# ── 1. threshold maths ──────────────────────────────────────────────
class TestStuckThreshold:
    def test_cadence_none_gets_bare_floor(self):
        # The paused case — no cadence to scale from.
        assert p._stuck_threshold_for(None) == p.STUCK_FLOOR_SECONDS

    def test_short_cadence_floors(self):
        # 300*3 = 15min, far under the 6h floor.
        assert p._stuck_threshold_for(300) == p.STUCK_FLOOR_SECONDS

    def test_hourly_cadence_floors(self):
        assert p._stuck_threshold_for(3600) == p.STUCK_FLOOR_SECONDS

    def test_long_cadence_scales_above_floor(self):
        # 6h cadence * 3 = 18h, which exceeds the floor and wins.
        assert p._stuck_threshold_for(21600) == 64800

    def test_floor_is_six_hours(self):
        assert p.STUCK_FLOOR_SECONDS == 6 * 3600


# ── 2. the row predicate ────────────────────────────────────────────
class TestIsStuckRow:
    SENTINEL = "run_in_progress"

    def test_supply_escrow_walker_wedge_is_stuck(self):
        """The actual 2026-10-14/15 row: cadence NULL, 17.5h open."""
        assert p._is_stuck_row(None, self.SENTINEL, None, int(17.5 * 3600))

    def test_closed_row_never_stuck(self):
        completed = dt.datetime(2026, 10, 15, 8, 0, tzinfo=dt.timezone.utc)
        assert not p._is_stuck_row(completed, self.SENTINEL, None, 10 ** 6)

    def test_non_sentinel_message_never_stuck(self):
        # A real failure message is walker_failing's business, not ours.
        assert not p._is_stuck_row(None, "boom: RPC timeout", 300, 10 ** 6)

    def test_blank_message_never_stuck(self):
        assert not p._is_stuck_row(None, None, None, 10 ** 6)

    def test_live_in_flight_walkers_not_stuck(self):
        """The five walkers genuinely in flight at 2026-10-15 08:10Z were
        all under 10 min old on 300s/3600s cadences."""
        for cadence in (300, 600, 3600):
            assert not p._is_stuck_row(None, self.SENTINEL, cadence, 600)

    def test_long_legitimate_run_not_stuck(self):
        """The escrow seed walk ran ~2.5h in-flight on cadence NULL and
        must not page."""
        assert not p._is_stuck_row(None, self.SENTINEL, None, int(2.5 * 3600))

    def test_fossil_row_skipped(self):
        assert not p._is_stuck_row(None, self.SENTINEL, None,
                                   31 * 86400)

    def test_age_none_not_stuck(self):
        assert not p._is_stuck_row(None, self.SENTINEL, None, None)


# ── 3. FAILING skips the paused convention ──────────────────────────
class TestFailingSkipsPaused:
    def test_query_filters_null_cadence(self, monkeypatch):
        conn = FakeConn(rows=[])
        monkeypatch.setattr(p, "_pg_connect", lambda: conn)
        p.check_walker_failing()
        sql = conn.cur.statements[0][0]
        assert "cadence_seconds IS NOT NULL" in sql

    def test_paused_walker_absent_from_results(self, monkeypatch):
        """Row shape is what the filtered query would return: the paused
        walker is gone, a real failing walker survives."""
        conn = FakeConn(rows=[
            ("rank_amms", 4, dt.datetime(2026, 10, 15,
                                         tzinfo=dt.timezone.utc), "boom"),
        ])
        monkeypatch.setattr(p, "_pg_connect", lambda: conn)
        alerts = p.check_walker_failing()
        names = [a["walker"] for a in alerts]
        assert names == ["rank_amms"]
        assert "supply_escrow_walker" not in names


# ── 4. STUCK must NOT filter on cadence ─────────────────────────────
class TestStuckIgnoresCadence:
    def test_query_has_no_cadence_filter(self, monkeypatch):
        """This is the whole point: filtering cadence here would restore
        the blind spot that TestFailingSkipsPaused creates."""
        conn = FakeConn(rows=[])
        monkeypatch.setattr(p, "_pg_connect", lambda: conn)
        p.check_walker_stuck(dt.datetime.now(dt.timezone.utc))
        sql = conn.cur.statements[0][0]
        assert "cadence_seconds IS NOT NULL" not in sql
        assert "last_run_completed IS NULL" in sql

    def test_wedged_paused_walker_alerts(self, monkeypatch):
        conn = FakeConn(rows=[
            ("supply_escrow_walker", None, None, "run_in_progress",
             int(17.5 * 3600)),
        ])
        monkeypatch.setattr(p, "_pg_connect", lambda: conn)
        alerts = p.check_walker_stuck(dt.datetime.now(dt.timezone.utc))
        assert len(alerts) == 1
        assert alerts[0]["id"] == "walker_stuck:supply_escrow_walker"
        assert alerts[0]["cadence_s"] is None

    def test_live_walkers_produce_no_alert(self, monkeypatch):
        conn = FakeConn(rows=[
            ("whales_summary_walker", 300, None, "run_in_progress", 300),
            ("rank_amms", 3600, None, "run_in_progress", 600),
        ])
        monkeypatch.setattr(p, "_pg_connect", lambda: conn)
        assert p.check_walker_stuck(dt.datetime.now(dt.timezone.utc)) == []

    def test_registered_in_gather_alerts(self):
        src = inspect.getsource(p.gather_alerts)
        assert "check_walker_stuck" in src


# ── 5. the alert text ───────────────────────────────────────────────
class TestFormatStuckAlert:
    def test_paused_variant_names_null_cadence(self):
        out = p.format_alert({
            "id": "walker_stuck:supply_escrow_walker",
            "walker": "supply_escrow_walker",
            "age_s": int(17.5 * 3600),
            "threshold_s": p.STUCK_FLOOR_SECONDS,
            "cadence_s": None,
        })
        assert "STUCK walker" in out
        assert "supply_escrow_walker" in out
        assert "cadence NULL — paused or never declared" in out
        assert "run_in_progress for 17.5h" in out

    def test_cadenced_variant_shows_cadence(self):
        out = p.format_alert({
            "id": "walker_stuck:rank_amms",
            "walker": "rank_amms",
            "age_s": 11 * 3600,
            "threshold_s": p.STUCK_FLOOR_SECONDS,
            "cadence_s": 3600,
        })
        assert "STUCK walker" in out
        assert "cadence 1.0h" in out
        assert "paused" not in out


# ── 6. pause_walker closes the row honestly ─────────────────────────
class TestPauseWalker:
    def _db(self):
        import db
        return db

    def test_nulls_cadence_and_closes_row(self, monkeypatch):
        db = self._db()
        conn = FakeConn(rowcount=1)
        monkeypatch.setattr(db, "_get_writer_conn", lambda: conn)
        ok = db.pause_walker("supply_escrow_walker", reason="stage 4 redesign",
                             today=dt.date(2026, 10, 15))
        assert ok is True
        sql, params = conn.cur.statements[0]
        assert "cadence_seconds = NULL" in sql
        # COALESCE so an already-closed row is not back-dated.
        assert "COALESCE(last_run_completed, now())" in sql
        assert params[0] == "paused 2026-10-15: stage 4 redesign"
        assert params[1] == "supply_escrow_walker"

    def test_does_not_manufacture_success_or_failure(self, monkeypatch):
        """Paused is a third state. Touching last_run_ok / last_success_at
        would make a paused walker look freshly healthy forever;
        incrementing consecutive_failures would invent a failure out of
        an operator action."""
        db = self._db()
        conn = FakeConn(rowcount=1)
        monkeypatch.setattr(db, "_get_writer_conn", lambda: conn)
        db.pause_walker("supply_escrow_walker", today=dt.date(2026, 10, 15))
        sql = conn.cur.statements[0][0]
        for forbidden in ("last_success_at", "consecutive_failures",
                          "last_failure_at", "last_run_ok"):
            assert forbidden not in sql

    def test_reasonless_pause_still_dated(self, monkeypatch):
        db = self._db()
        conn = FakeConn(rowcount=1)
        monkeypatch.setattr(db, "_get_writer_conn", lambda: conn)
        db.pause_walker("w", today=dt.date(2026, 10, 15))
        assert conn.cur.statements[0][1][0] == "paused 2026-10-15"

    def test_unknown_walker_returns_false(self, monkeypatch):
        db = self._db()
        conn = FakeConn(rowcount=0)
        monkeypatch.setattr(db, "_get_writer_conn", lambda: conn)
        assert db.pause_walker("nope", today=dt.date(2026, 10, 15)) is False

    def test_no_writer_conn_returns_false(self, monkeypatch):
        db = self._db()
        monkeypatch.setattr(db, "_get_writer_conn", lambda: None)
        assert db.pause_walker("w") is False

    def test_never_raises_on_db_error(self, monkeypatch):
        db = self._db()

        class Boom(FakeConn):
            def cursor(self):
                raise RuntimeError("connection reset")

        monkeypatch.setattr(db, "_get_writer_conn", lambda: Boom())
        assert db.pause_walker("w") is False
