"""amm_ranked_pools reads: snapshot_ts-gated caching
(branch amm-reads-snapshot-cache-2026-10-27).

2026-10-27 egress audit (Charlie, read-only via the owner connection
already configured in ~/.config/xrpldashboard/env, pg_stat_activity +
pg_stat_statements): found two full-table-read call sites with no
snapshot gate at all --

  - db.read_amm_account_set() -- called by xrpl_stream.py (Mac mini
    daemon) on every tick with no cache: 16,901 calls over 10.6 days
    (one every ~54s) despite amm_ranked_pools only changing on an
    hourly rank_amms swap.
  - app.py's _ranked_amm_snapshot() -- Render web, was a plain
    @ttl_cache(seconds=60): every gunicorn worker re-pulled the full
    12-column/~30k-row table every 60s regardless of whether the
    underlying snapshot had changed at all.

This file pins the fix for both: a cheap MAX(snapshot_ts) probe gates
the expensive full read, same pattern already used by
read_amm_index_entries_cached() and live_stream_relay.py's own
_pools_cache.

Hermetic: no DB, no network. Monkeypatches db.pg_connect /
db.read_amm_snapshot_ts / db.read_amm_ranked_pools directly.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ── db.read_amm_account_set() ──

class _FakeCursor:
    def __init__(self, snap_value, accounts):
        self.snap_value = snap_value
        self.accounts = accounts
        self.last_sql = None

    def execute(self, sql, params=None):
        self.last_sql = " ".join(sql.split())

    def fetchone(self):
        if "MAX(snapshot_ts)" in self.last_sql:
            return (self.snap_value,)
        return None

    def fetchall(self):
        return [(a,) for a in self.accounts]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, snap_value, accounts, calls):
        self.snap_value = snap_value
        self.accounts = accounts
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        self.calls.append(1)
        return _FakeCursor(self.snap_value, self.accounts)


@pytest.fixture(autouse=True)
def _reset_account_set_cache():
    import db
    db._amm_account_set_cache = {"snap": None, "accounts": None}
    yield
    db._amm_account_set_cache = {"snap": None, "accounts": None}


def test_read_amm_account_set_skips_full_read_when_snapshot_unchanged(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    full_read_count = {"n": 0}

    class _CountingCursor(_FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "WHERE amm_account IS NOT NULL" in self.last_sql:
                full_read_count["n"] += 1

    class _CountingConn(_FakeConn):
        def cursor(self):
            return _CountingCursor(1000, ["rA", "rB"])

    monkeypatch.setattr(db, "pg_connect", lambda: _CountingConn(1000, ["rA", "rB"], []))

    accounts1, snap1 = db.read_amm_account_set()
    assert accounts1 == {"rA", "rB"} and snap1 == 1000
    assert full_read_count["n"] == 1

    # same snapshot -> no second full read
    accounts2, snap2 = db.read_amm_account_set()
    assert accounts2 == accounts1 and snap2 == 1000
    assert full_read_count["n"] == 1, "unchanged snapshot must not trigger a second full read"


def test_read_amm_account_set_refetches_when_snapshot_changes(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    state = {"snap": 1000, "accounts": ["rA"]}
    full_read_count = {"n": 0}

    class _Cursor(_FakeCursor):
        def execute(self, sql, params=None):
            self.last_sql = " ".join(sql.split())

        def fetchone(self):
            return (state["snap"],)

        def fetchall(self):
            full_read_count["n"] += 1
            return [(a,) for a in state["accounts"]]

    class _Conn(_FakeConn):
        def cursor(self):
            return _Cursor(None, None)

    monkeypatch.setattr(db, "pg_connect", lambda: _Conn(None, None, []))

    accounts1, snap1 = db.read_amm_account_set()
    assert accounts1 == {"rA"} and full_read_count["n"] == 1

    state["snap"] = 1001
    state["accounts"] = ["rA", "rB", "rC"]
    accounts2, snap2 = db.read_amm_account_set()
    assert accounts2 == {"rA", "rB", "rC"} and snap2 == 1001
    assert full_read_count["n"] == 2, "a changed snapshot must trigger exactly one more full read"


def test_read_amm_account_set_still_returns_empty_set_when_pg_unavailable(monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: False)
    assert db.read_amm_account_set() == (set(), None)


# ── app.py's _ranked_amm_snapshot() ──

@pytest.fixture(autouse=True)
def _reset_ranked_amm_snapshot_cache():
    import app
    app._ranked_amm_snapshot_cache = {"snap": None, "result": None, "checked_mono": 0.0}
    yield
    app._ranked_amm_snapshot_cache = {"snap": None, "result": None, "checked_mono": 0.0}


def test_ranked_amm_snapshot_no_longer_a_plain_ttl_cache():
    """Pin by source: the egress bug was specifically the bare
    @ttl_cache(seconds=60) decorator, which refetches on a timer with
    no regard for whether the data changed."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("app.py").read_text()
    i = src.index("def _ranked_amm_snapshot():")
    before = src[max(0, i - 120):i]
    assert "@ttl_cache" not in before, \
        "_ranked_amm_snapshot must not be a plain time-based ttl_cache any more"


def test_ranked_amm_snapshot_skips_full_read_when_snapshot_unchanged(monkeypatch):
    import app
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "read_amm_snapshot_ts", lambda: 2000)
    full_read_count = {"n": 0}

    def fake_read_amm_ranked_pools():
        full_read_count["n"] += 1
        return [{"amm_account": "rA"}]

    monkeypatch.setattr(db, "read_amm_ranked_pools", fake_read_amm_ranked_pools)
    monkeypatch.setattr(db, "read_heartbeat", lambda name: {})
    monkeypatch.setattr(app, "_annotate_unverified_brands", lambda rows: rows)
    # force the time-based probe gate open on every call in this test
    monkeypatch.setattr(app, "_RANKED_AMM_SNAPSHOT_PROBE_INTERVAL_S", 0)

    rows1, meta1 = app._ranked_amm_snapshot()
    assert rows1 == [{"amm_account": "rA"}] and full_read_count["n"] == 1

    rows2, meta2 = app._ranked_amm_snapshot()
    assert rows2 == rows1 and meta2 == meta1
    assert full_read_count["n"] == 1, "unchanged snapshot must not trigger a second full table read"


def test_ranked_amm_snapshot_refetches_when_snapshot_changes(monkeypatch):
    import app
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(app, "_RANKED_AMM_SNAPSHOT_PROBE_INTERVAL_S", 0)
    state = {"snap": 2000, "rows": [{"amm_account": "rA"}]}
    full_read_count = {"n": 0}

    monkeypatch.setattr(db, "read_amm_snapshot_ts", lambda: state["snap"])

    def fake_read_amm_ranked_pools():
        full_read_count["n"] += 1
        return state["rows"]

    monkeypatch.setattr(db, "read_amm_ranked_pools", fake_read_amm_ranked_pools)
    monkeypatch.setattr(db, "read_heartbeat", lambda name: {})
    monkeypatch.setattr(app, "_annotate_unverified_brands", lambda rows: rows)

    rows1, _ = app._ranked_amm_snapshot()
    assert full_read_count["n"] == 1

    state["snap"] = 2001
    state["rows"] = [{"amm_account": "rA"}, {"amm_account": "rB"}]
    rows2, _ = app._ranked_amm_snapshot()
    assert rows2 == state["rows"]
    assert full_read_count["n"] == 2, "a changed snapshot must trigger exactly one more full read"


def test_ranked_amm_snapshot_falls_back_to_file_when_pg_unavailable(monkeypatch, tmp_path):
    import app
    import db
    monkeypatch.setattr(db, "pg_available", lambda: False)
    monkeypatch.setattr(app, "_RANKED_AMM_SNAPSHOT_PROBE_INTERVAL_S", 0)
    monkeypatch.setattr(app, "AMM_RANKED_PATH", str(tmp_path / "amm_ranked.json"))
    monkeypatch.setattr(app, "AMM_INDEX_PATH", str(tmp_path / "amm_index.json"))
    monkeypatch.setattr(app, "AMM_RANK_STATE_PATH", str(tmp_path / "amm_rank_state.json"))
    monkeypatch.setattr(app, "_safe_load_json", lambda path: None)
    monkeypatch.setattr(app, "_annotate_unverified_brands", lambda rows: rows)

    rows, meta = app._ranked_amm_snapshot()
    assert rows == []
    assert meta["source"] == "file"
