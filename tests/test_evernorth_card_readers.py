"""Card-side Postgres readers: latest snapshot + recent moves (2026-10-10).

These two exist so the /institutional card renders from Postgres alone. The
nightly job is the only thing allowed to touch the node — a route that
renders fine locally can still hang only on Render (PR #28, /tokens), so 13
account_info calls must never sit on a page request.

What this pins:

1. The latest-snapshot read takes the newest row by snapshot_date, and
   `balances` is usable whether the driver hands back a dict or JSON text.
2. An unreadable wallet survives the round trip as None, never 0 — a zero
   would understate the total and read on the card as an outflow that
   never happened.
3. The moves query is XRP-only (`currency IS NULL`). A token amount in
   amount_drops is not a drops value, and dividing it by 1e6 would put a
   number on the card the ledger never stated.
4. Direction is relative to OUR wallets, and a wallet-to-wallet move
   carries counterparty None — naming our own other wallet as the "other
   side" would invent a transfer between parties that is really internal.
5. Both readers degrade (None / []) instead of raising, so a missing
   history or move list omits that block rather than 500ing the page.

Hermetic: fake connection objects, no DB, no network.
"""
import datetime as _dt
import json
import os
import sys

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

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(self, rows=None, raises=None):
        self.cur = FakeCursor(rows, raises)

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _pg(monkeypatch, conn):
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "pg_connect", lambda: conn)


# ── latest snapshot ──────────────────────────────────────────────────────
def test_latest_snapshot_takes_newest_row(monkeypatch):
    conn = FakeConn(rows=[(
        _dt.date(2026, 10, 10), 1791600000, 473_276_565.78, 13, 13,
        {"rAAA": 1.5, "rBBB": None},
    )])
    _pg(monkeypatch, conn)
    out = db.read_evernorth_latest_snapshot()
    assert out["date"] == "2026-10-10"
    assert out["total_xrp"] == 473_276_565.78
    assert out["readable_count"] == 13
    # Ordering is the query's job, not the caller's: a single fake row would
    # pass even if the ORDER BY were dropped.
    assert "ORDER BY snapshot_date DESC" in conn.cur.sql
    assert "LIMIT 1" in conn.cur.sql


def test_latest_snapshot_parses_balances_from_json_text(monkeypatch):
    """psycopg may hand back jsonb already decoded or as text."""
    conn = FakeConn(rows=[(
        _dt.date(2026, 10, 10), 1, 2.0, 1, 2,
        json.dumps({"rAAA": 1.5, "rBBB": None}),
    )])
    _pg(monkeypatch, conn)
    out = db.read_evernorth_latest_snapshot()
    assert out["balances"] == {"rAAA": 1.5, "rBBB": None}


def test_latest_snapshot_keeps_unreadable_as_none_not_zero(monkeypatch):
    conn = FakeConn(rows=[(
        _dt.date(2026, 10, 10), 1, 2.0, 1, 2, {"rAAA": 1.5, "rBad": None},
    )])
    _pg(monkeypatch, conn)
    bal = db.read_evernorth_latest_snapshot()["balances"]
    assert bal["rBad"] is None
    assert bal["rBad"] != 0


def test_latest_snapshot_none_without_pg(monkeypatch):
    monkeypatch.setattr(db, "pg_available", lambda: False)
    assert db.read_evernorth_latest_snapshot() is None


def test_latest_snapshot_none_when_table_empty(monkeypatch):
    _pg(monkeypatch, FakeConn(rows=[]))
    assert db.read_evernorth_latest_snapshot() is None


def test_latest_snapshot_none_on_error(monkeypatch):
    _pg(monkeypatch, FakeConn(raises=RuntimeError("boom")))
    assert db.read_evernorth_latest_snapshot() is None


# ── recent moves ─────────────────────────────────────────────────────────
OURS = ["rOurA", "rOurB"]


def test_moves_query_is_xrp_only_and_newest_first(monkeypatch):
    conn = FakeConn(rows=[])
    _pg(monkeypatch, conn)
    db.read_evernorth_moves(OURS, limit=10)
    sql = conn.cur.sql
    # A token amount in amount_drops is not drops; pricing it here would
    # print a number the ledger never stated.
    assert "currency IS NULL" in sql
    assert "amount_drops > 0" in sql
    assert "ORDER BY ts DESC" in sql
    assert conn.cur.params == [OURS, OURS, 10]


def test_moves_direction_is_relative_to_our_wallets(monkeypatch):
    rows = [
        ("HASH_OUT", 1791600000, "rOurA", "rStranger", 5_000_000),
        ("HASH_IN", 1791500000, "rStranger", "rOurB", 7_000_000),
    ]
    _pg(monkeypatch, FakeConn(rows=rows))
    out = db.read_evernorth_moves(OURS)
    assert out[0]["direction"] == "out"
    assert out[0]["counterparty"] == "rStranger"
    assert out[0]["amount_xrp"] == 5.0
    assert out[1]["direction"] == "in"
    assert out[1]["counterparty"] == "rStranger"


def test_internal_move_has_no_counterparty(monkeypatch):
    """Our own other wallet is not a counterparty."""
    rows = [("HASH_INT", 1791600000, "rOurA", "rOurB", 1_000_000)]
    _pg(monkeypatch, FakeConn(rows=rows))
    out = db.read_evernorth_moves(OURS)
    assert out[0]["internal"] is True
    assert out[0]["counterparty"] is None


def test_moves_iso_is_utc_and_parseable(monkeypatch):
    rows = [("H", 1791600000, "rOurA", "rStranger", 1_000_000)]
    _pg(monkeypatch, FakeConn(rows=rows))
    iso = db.read_evernorth_moves(OURS)[0]["iso"]
    assert iso.endswith("Z")
    # The template's datetime_to_et_first filter has to be able to read it.
    assert _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").year == 2026


def test_moves_empty_for_no_addresses_without_touching_pg(monkeypatch):
    def boom():
        raise AssertionError("must not open a connection for an empty list")
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "pg_connect", boom)
    assert db.read_evernorth_moves([]) == []
    assert db.read_evernorth_moves(None) == []


def test_moves_empty_without_pg(monkeypatch):
    monkeypatch.setattr(db, "pg_available", lambda: False)
    assert db.read_evernorth_moves(OURS) == []


def test_moves_empty_on_error(monkeypatch):
    _pg(monkeypatch, FakeConn(raises=RuntimeError("boom")))
    assert db.read_evernorth_moves(OURS) == []
