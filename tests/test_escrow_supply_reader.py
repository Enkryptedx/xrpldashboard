"""read_escrow_supply_snapshot reads the supply block (escrow_ripple_*)
first and only falls back to the legacy escrow_supply_snapshot table.

Regression for 2026-09-24..26: the legacy table's only writer was retired
as redundant with the supply block, but /cold-storage's "XRP locked"
block still read the legacy table, so it went stale and the page wore the
"walker refresh delayed" banner for ~2 days while every walker was green.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import escrow_supply as E  # noqa: E402


class _Cur:
    def __init__(self, block_row, legacy_row=None, accounts=(21, 21)):
        self.block_row, self.legacy_row, self.accounts = block_row, legacy_row, accounts
        self.last = None
        self.queries = []

    def execute(self, sql, *a):
        self.queries.append(sql)
        if "xrp_supply_block" in sql:
            self.last = self.block_row
        elif "cold_storage_snapshot" in sql:
            self.last = self.accounts
        elif "escrow_supply_snapshot" in sql:
            self.last = self.legacy_row
        else:  # pragma: no cover
            raise AssertionError(sql)

    def fetchone(self):
        return self.last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, cur):
    class _Conn:
        def cursor(self):
            return cur

    @contextmanager
    def _pg():
        yield _Conn()

    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "pg_connect", _pg)


def test_block_first_fresh_row_is_sovereign(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    cur = _Cur(block_row=(31_700_000_000.0, 100, 107252759, now - dt.timedelta(minutes=8)),
               legacy_row=(1.0, 1, 1, 1, 1, now - dt.timedelta(days=2)))
    _wire(monkeypatch, cur)
    s = db.read_escrow_supply_snapshot()
    assert s["total_xrp"] == 31_700_000_000.0 and s["object_count"] == 100
    assert s["accounts_scanned"] == 21 and s["accounts_total"] == 21
    assert s["ledger_index"] == 107252759
    assert 0 <= s["age_seconds"] < 600
    assert not any("escrow_supply_snapshot" in q for q in cur.queries), "legacy table must not be read when the block has data"
    locked = E.fetch_escrow_locked_from_db()
    assert locked["sourcing"] == "sovereign" and locked["is_fallback"] is False


def test_falls_back_to_legacy_only_when_block_has_no_escrow_side(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    cur = _Cur(block_row=(None, None, None, None),
               legacy_row=(5.0, 2, 20, 20, 42, now - dt.timedelta(days=2)))
    _wire(monkeypatch, cur)
    s = db.read_escrow_supply_snapshot()
    assert s["total_xrp"] == 5.0 and s["accounts_scanned"] == 20
    locked = E.fetch_escrow_locked_from_db()
    assert locked["sourcing"] == "stale-cache" and locked["is_fallback"] is True


def test_no_data_anywhere_is_stale_cache_not_a_crash(monkeypatch):
    cur = _Cur(block_row=None, legacy_row=None)
    _wire(monkeypatch, cur)
    s = db.read_escrow_supply_snapshot()
    assert s["fetched_at"] is None
    locked = E.fetch_escrow_locked_from_db()
    assert locked["sourcing"] == "stale-cache" and locked["total_xrp"] == 0.0
