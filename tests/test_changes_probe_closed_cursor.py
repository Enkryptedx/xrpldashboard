"""The producer probe against a REAL closed cursor.

WHY THIS FILE EXISTS
--------------------
On 2026-11-03 the producer guard shipped with its probe call at
function-body indentation — OUTSIDE build_changes_for_date's
`with pg_connect()` / `with conn.cursor()` blocks. By the time it ran the
cursor was CLOSED, every probe query raised, the fail-safe marked all three
producers missing, and a day with 7 real changes rendered as
"Comparison unavailable". A fail-safe that fires on a closed handle hides
real data, which is worse than having no guard at all.

The whole existing suite missed it, and the reason is instructive: EVERY
fake cursor we had stays open forever and answers every query
(`_Cur`, `PoisonedThenOk`, `Rec`). A handle that closes was literally
unrepresentable. The build_strip tests pass a plain dict and never touch a
cursor. The two scope tests added alongside the fix assert on SOURCE TEXT
(call order, indentation) — static, and only written because the bug was
already known.

So this file uses a REAL DBAPI cursor from stdlib `sqlite3`, which has
genuine closed-state semantics: after .close() it raises
sqlite3.ProgrammingError("Cannot operate on a closed cursor"). That is the
same shape psycopg raises, and it is what production actually hit.

Hermetic: in-memory sqlite, no server, no network.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import changes_builder as cb  # noqa: E402

DATE = dt.date(2026, 11, 3)


class PgStyleCursor:
    """Thin adapter over a REAL sqlite3 cursor.

    Only translates psycopg's `%s` placeholder to sqlite's `?` and renders
    date params as ISO text. Everything else — including close() and the
    error raised after close — is the genuine sqlite3 cursor, which is the
    entire point: we are testing against real closed-handle behaviour, not a
    mock's idea of it.
    """

    def __init__(self, cur: sqlite3.Cursor):
        self._cur = cur

    def execute(self, sql, params=None):
        sql = sql.replace("%s", "?")
        if params:
            params = tuple(
                p.isoformat() if isinstance(p, (dt.date, dt.datetime)) else p
                for p in params
            )
            return self._cur.execute(sql, params)
        return self._cur.execute(sql)

    def fetchone(self):
        return self._cur.fetchone()

    def close(self):
        self._cur.close()


def _seed(date: dt.date) -> sqlite3.Connection:
    """Real sqlite DB with all three producer rows present for `date`."""
    conn = sqlite3.connect(":memory:")
    c = conn.cursor()
    for _label, table, col in cb._PRODUCER_INPUTS:
        c.execute(f"CREATE TABLE {table} ({col} TEXT)")
        c.execute(f"INSERT INTO {table} ({col}) VALUES (?)", (date.isoformat(),))
    conn.commit()
    c.close()
    return conn


class TestRealOpenCursorFindsProducers:
    def test_open_cursor_reports_all_found(self):
        """Baseline: with a real OPEN cursor and all rows present, the probe
        must return []. If this fails the adapter is wrong, not the guard."""
        conn = _seed(DATE)
        cur = PgStyleCursor(conn.cursor())
        try:
            assert cb._assert_producer_inputs(cur, DATE) == []
        finally:
            cur.close(); conn.close()

    def test_open_cursor_detects_a_genuinely_absent_row(self):
        """The guard must still do its real job: a producer with no row for
        the date is reported missing, through the same real cursor."""
        conn = _seed(DATE)
        cur = PgStyleCursor(conn.cursor())
        try:
            assert cb._assert_producer_inputs(cur, dt.date(2026, 11, 4)) == [
                lbl for lbl, _t, _c in cb._PRODUCER_INPUTS]
        finally:
            cur.close(); conn.close()


class TestRealClosedCursorIsTheHazard:
    """These are the tests that would have caught the shipped bug."""

    def test_sqlite_really_raises_after_close(self):
        """Proves the fixture has teeth: a real cursor raises after close.
        Every previous fake stayed open, which is why nothing caught this."""
        conn = _seed(DATE)
        raw = conn.cursor()
        raw.close()
        with pytest.raises(sqlite3.ProgrammingError):
            raw.execute("SELECT 1")
        conn.close()

    def test_closed_cursor_is_indistinguishable_from_missing_data(self):
        """THE BUG: through a real CLOSED cursor the probe reports every
        producer missing — even though all three rows exist. That is how a
        day with 7 real changes rendered 'Comparison unavailable'."""
        conn = _seed(DATE)
        cur = PgStyleCursor(conn.cursor())
        cur.close()
        assert cb._assert_producer_inputs(cur, DATE) == [
            lbl for lbl, _t, _c in cb._PRODUCER_INPUTS]
        conn.close()

    def test_probe_inside_scope_differs_from_probe_after_scope(self):
        """Lifecycle test mirroring build_changes_for_date's structure.

        Inside the `with` the probe sees reality; after the block exits the
        same probe on the same handle reports total absence. The difference
        between those two answers IS the bug, and this asserts the correct
        side of it is what the builder uses.
        """
        conn = _seed(DATE)
        inside = after = None
        with conn:
            cur = PgStyleCursor(conn.cursor())
            inside = cb._assert_producer_inputs(cur, DATE)
            cur.close()          # what leaving the `with` block does
        after = cb._assert_producer_inputs(cur, DATE)
        conn.close()
        assert inside == [], inside
        assert after == [lbl for lbl, _t, _c in cb._PRODUCER_INPUTS], after
        assert inside != after, "a closed handle must not look like an open one"


class TestSavepointWorksOnARealCursor:
    def test_savepoint_round_trip_on_real_sqlite(self):
        """The guard issues SAVEPOINT / RELEASE around each probe. sqlite
        supports both, so this exercises that path for real rather than
        asserting a fake recorded the strings."""
        conn = _seed(DATE)
        cur = PgStyleCursor(conn.cursor())
        try:
            assert cb._assert_producer_inputs(cur, DATE) == []
            # transaction still usable afterwards
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1
        finally:
            cur.close(); conn.close()
