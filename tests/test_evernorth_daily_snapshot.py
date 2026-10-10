"""Evernorth daily treasury snapshot: Postgres writer + reader (2026-10-10).

One row per calendar day, written after the 21:00 ET signed snapshot, read
back as the history line on the /institutional card.

What this pins:

1. The write is an UPSERT on snapshot_date. A retry or a second run on the
   same day must overwrite, not append — otherwise the card's history shows
   two points for one day.
2. balances is serialised with json.dumps + ::jsonb, this module's house
   pattern (psycopg's Json adapter is not imported in db.py, so using it
   would NameError at runtime — something ast.parse cannot catch).
3. An unreadable wallet is stored as null, never 0. Counting an unreadable
   balance as zero would understate the total and render as an outflow
   that never happened.
4. Both sides degrade instead of raising: no PG configured means the write
   is a silent no-op and the read returns [] (not None), so a missing
   history omits the line rather than 500ing the page.
5. A write failure never propagates — a missed day must not break the
   walker that writes it.

Hermetic: fake connection objects, no DB, no network.
"""
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import db  # noqa: E402


class FakeCursor:
    def __init__(self, rows=None, raises=None):
        self.rows = rows or []
        self.raises = raises
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.raises:
            raise self.raises
        self.sql = " ".join(sql.split())
        self.params = list(params or [])

    def fetchall(self):
        return list(self.rows)


class FakeConn:
    def __init__(self, rows=None, raises=None):
        self.cur = FakeCursor(rows, raises)

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── writer ───────────────────────────────────────────────────────────────
def test_write_upserts_on_snapshot_date(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "_get_writer_conn", lambda: conn)
    db.write_evernorth_daily_snapshot(
        "2026-10-10", 1791600000, 473_276_565.78, 13, 13,
        {"rAAA": 1.0, "rBBB": None})
    assert "INSERT INTO evernorth_daily_snapshot" in conn.cur.sql
    assert "ON CONFLICT (snapshot_date) DO UPDATE SET" in conn.cur.sql
    assert conn.cur.params[0] == "2026-10-10"


def test_write_serialises_balances_as_json_text(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "_get_writer_conn", lambda: conn)
    db.write_evernorth_daily_snapshot(
        "2026-10-10", 1, 2.0, 1, 1, {"rAAA": 1.5})
    assert "%s::jsonb" in conn.cur.sql
    payload = conn.cur.params[-1]
    assert isinstance(payload, str), "must be json text, not a Json adapter"
    assert json.loads(payload) == {"rAAA": 1.5}


def test_write_keeps_unreadable_balance_as_null_not_zero(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "_get_writer_conn", lambda: conn)
    db.write_evernorth_daily_snapshot(
        "2026-10-10", 1, 2.0, 1, 2, {"rAAA": 1.5, "rUnreadable": None})
    stored = json.loads(conn.cur.params[-1])
    assert stored["rUnreadable"] is None
    assert stored["rUnreadable"] != 0


def test_write_is_silent_noop_without_pg(monkeypatch):
    monkeypatch.setattr(db, "_get_writer_conn", lambda: None)
    assert db.write_evernorth_daily_snapshot("2026-10-10", 1, 2.0, 1, 1, {}) is None


def test_write_failure_never_propagates(monkeypatch):
    """A missed day must not break the walker that writes it."""
    monkeypatch.setattr(db, "_get_writer_conn",
                        lambda: FakeConn(raises=RuntimeError("pg down")))
    monkeypatch.setattr(db, "_log_err", lambda *a, **k: None)
    assert db.write_evernorth_daily_snapshot("2026-10-10", 1, 2.0, 1, 1, {}) is None


# ── table guarantee ──────────────────────────────────────────────────────
def test_ensure_table_issues_create_if_not_exists(monkeypatch):
    """The job owns its own table.

    Declaring it in SCHEMA_DDL is NOT enough: init_schema() is a manual
    one-off that nothing runs at boot. Verified 2026-10-10 against
    production - the table did not exist, so every insert would have been
    swallowed by the writer's best-effort except: a silently missing row
    instead of a visible failure.
    """
    conn = FakeConn()
    monkeypatch.setattr(db, "_get_writer_conn", lambda: conn)
    assert db.ensure_evernorth_daily_snapshot_table() is True
    assert "CREATE TABLE IF NOT EXISTS evernorth_daily_snapshot" in conn.cur.sql
    # PRIMARY KEY on the date is what makes a retry idempotent.
    assert "snapshot_date DATE PRIMARY KEY" in conn.cur.sql


def test_ensure_table_false_without_pg(monkeypatch):
    monkeypatch.setattr(db, "_get_writer_conn", lambda: None)
    assert db.ensure_evernorth_daily_snapshot_table() is False


def test_ensure_table_never_raises(monkeypatch):
    """A DDL failure must not kill the job before it tries to write."""
    monkeypatch.setattr(db, "_get_writer_conn",
                        lambda: FakeConn(raises=RuntimeError("no perms")))
    monkeypatch.setattr(db, "_log_err", lambda *a, **k: None)
    assert db.ensure_evernorth_daily_snapshot_table() is False


# ── reader ───────────────────────────────────────────────────────────────
import datetime as _dt  # noqa: E402


def test_read_returns_empty_list_without_pg(monkeypatch):
    monkeypatch.setattr(db, "pg_available", lambda: False)
    assert db.read_evernorth_daily_totals() == []


def test_read_maps_rows_newest_first(monkeypatch):
    rows = [
        (_dt.date(2026, 10, 10), 473_276_565.78, 13, 13, 1791600000),
        (_dt.date(2026, 10, 9), 473_276_430.00, 12, 13, 1791513600),
    ]
    conn = FakeConn(rows=rows)
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "pg_connect", lambda: conn)
    out = db.read_evernorth_daily_totals(limit=30)
    assert [r["date"] for r in out] == ["2026-10-10", "2026-10-09"]
    assert out[0]["total_xrp"] == 473_276_565.78
    assert out[1]["readable_count"] == 12
    # The DESC ordering is the query's job, not the caller's: assert on the
    # SQL actually issued rather than on already-sorted fake rows, which
    # would pass even if the ORDER BY were dropped.
    assert "ORDER BY snapshot_date DESC" in conn.cur.sql
    assert conn.cur.params == [30]


def test_read_returns_empty_list_on_error(monkeypatch):
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "pg_connect",
                        lambda: FakeConn(raises=RuntimeError("boom")))
    assert db.read_evernorth_daily_totals() == []
