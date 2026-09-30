"""read_amm_ranked_pools(columns=...) + read_amm_account_set + the stream's
snapshot_ts-gated reload. No DB: pg_connect is monkeypatched with a fake cursor.
Neon egress finding 2026-09-30: the stream reloaded all 12 columns × 30k rows
every 60 s (~5.8 GB/day)."""
import types
import pytest

import db


class _Cur:
    def __init__(self, rows, maxts):
        self.rows, self.maxts, self.sql = rows, maxts, []
    def execute(self, sql, *a):
        self.sql.append(sql)
        self._last = sql
    def fetchall(self):
        sql = self._last
        if "SELECT amm_account FROM amm_ranked_pools" in sql:
            return [(r["amm_account"],) for r in self.rows]
        cols = sql.split("SELECT ", 1)[1].split(" FROM")[0].replace(" ", "").split(",")
        return [tuple(r.get(c) for c in cols) for r in self.rows]
    def fetchone(self):
        return (self.maxts,)
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Conn:
    def __init__(self, cur): self.cur = cur
    def cursor(self): return self.cur
    def __enter__(self): return self
    def __exit__(self, *a): return False


ROWS = [
    {"amm_account": "rA", "pair": "XRP/USD", "fee_pct": 0.1, "fee_raw": 100, "amount_a": "1", "amount_b": "2",
     "asset_a": "XRP", "asset_b": "USD", "tvl_usd": 5.0, "tvl_status": "ok", "kind": "amm", "snapshot_ts": 111},
    {"amm_account": "rB", "pair": "XRP/EUR", "fee_pct": 0.2, "fee_raw": 200, "amount_a": "3", "amount_b": "4",
     "asset_a": "XRP", "asset_b": "EUR", "tvl_usd": 6.0, "tvl_status": "ok", "kind": "amm", "snapshot_ts": 111},
]


@pytest.fixture
def fake_pg(monkeypatch):
    cur = _Cur(ROWS, 111)
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "pg_connect", lambda: _Conn(cur))
    return cur


def test_default_shape_unchanged(fake_pg):
    out = db.read_amm_ranked_pools()
    assert len(out) == 2
    assert set(out[0]) == set(db._AMM_RANKED_POOL_COLUMNS) | {"_snapshot_ts"}
    assert out[0]["_snapshot_ts"] == 111 and out[1]["tvl_usd"] == 6.0


def test_projection_selects_only_requested(fake_pg):
    out = db.read_amm_ranked_pools(columns=("amm_account", "pair"))
    assert set(out[0]) == {"amm_account", "pair", "_snapshot_ts"}
    assert "SELECT amm_account, pair, snapshot_ts FROM amm_ranked_pools" in fake_pg.sql[-1]


def test_projection_rejects_unknown(fake_pg):
    with pytest.raises(ValueError):
        db.read_amm_ranked_pools(columns=("amm_account", "password"))


def test_account_set(fake_pg):
    accts, snap = db.read_amm_account_set()
    assert accts == {"rA", "rB"} and snap == 111
    assert any("SELECT amm_account FROM amm_ranked_pools" in s for s in fake_pg.sql)


def test_stream_reload_skips_when_snapshot_unchanged(monkeypatch):
    import xrpl_stream as xs
    calls = {"set": 0, "ts": 0}
    def _ts():
        calls["ts"] += 1; return 111
    def _set():
        calls["set"] += 1; return {"rA", "rB"}, 111
    monkeypatch.setattr(xs.pgbridge, "pg_available", lambda: True)
    monkeypatch.setattr(xs.pgbridge, "read_amm_snapshot_ts", _ts)
    monkeypatch.setattr(xs.pgbridge, "read_amm_account_set", _set)
    monkeypatch.setattr(xs, "_AMM_ACCOUNT_SET", None)
    monkeypatch.setattr(xs, "_AMM_ACCOUNT_SET_SNAPSHOT_TS", None)
    monkeypatch.setattr(xs, "_AMM_ACCOUNT_SET_LAST_RELOAD", 0.0)
    xs._load_amm_account_set()            # first load: full read
    assert xs._AMM_ACCOUNT_SET == {"rA", "rB"} and calls["set"] == 1
    xs._load_amm_account_set()            # same snapshot_ts: probe only
    xs._load_amm_account_set()
    assert calls["ts"] == 3 and calls["set"] == 1
    monkeypatch.setattr(xs.pgbridge, "read_amm_snapshot_ts", lambda: 222)
    monkeypatch.setattr(xs.pgbridge, "read_amm_account_set", lambda: ({"rA", "rB", "rC"}, 222))
    xs._load_amm_account_set()            # new snapshot: full read again
    assert xs._AMM_ACCOUNT_SET == {"rA", "rB", "rC"} and xs._AMM_ACCOUNT_SET_SNAPSHOT_TS == 222
