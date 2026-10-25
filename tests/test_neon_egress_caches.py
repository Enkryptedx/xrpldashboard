"""2026-10-24 Neon egress fixes B, C, E -- the data-path contracts.

Background: October egress ran ~108 GB/day. pg_stat_statements traced
~28 GB/day to two full-table reads of amm_ranked_pools (~30,300 rows):
read_amm_index_entries at `import app` (token_data + wallet_data module
level, 1,498 calls/day) and read_amm_ranked_pools on every homepage render
(822/day), plus 1.5 GB/day of the relay re-pulling the pool set every 60 s
and 2.35 GB/day of is_bot_writer pulling ~230k hashes per run.
These tests pin the shapes that keep those reads from coming back.
"""
import importlib
import json
import os
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_token_data_and_wallet_data_do_not_read_amm_index_at_import():
    """The 11.4 MB read must not happen on `import`. Pin by source: no
    module-level assignment calling the loader, and both go through the
    cached accessor."""
    td = (ROOT / "token_data.py").read_text()
    wd = (ROOT / "wallet_data.py").read_text()
    assert "_AMM_INDEX = _load_amm_index()" not in td
    assert "_AMM_INDEX = _load_amm_index()" not in wd
    assert "db.read_amm_index_entries_cached()" in td
    assert "db.read_amm_index_entries_cached()" in wd
    assert "db.read_amm_index_entries()" not in td
    assert "db.read_amm_index_entries()" not in wd


def test_cached_accessor_reads_table_once_per_snapshot(monkeypatch, tmp_path):
    import db
    monkeypatch.setattr(db, "AMM_INDEX_CACHE_PATH", str(tmp_path / "amm.json"))
    monkeypatch.setattr(db, "pg_available", lambda: True)
    calls = {"full": 0, "snap": 0}
    snap = {"v": 1000}
    def fake_snap():
        calls["snap"] += 1; return snap["v"]
    def fake_full():
        calls["full"] += 1; return [{"Account": "rA", "Asset": {"currency": "XRP"}, "Asset2": {}}]
    monkeypatch.setattr(db, "read_amm_snapshot_ts", fake_snap)
    monkeypatch.setattr(db, "read_amm_index_entries", fake_full)
    db._amm_index_mem.update(snap=None, entries=None, checked_mono=0.0)

    e1 = db.read_amm_index_entries_cached(force=True)
    assert calls["full"] == 1 and e1[0]["Account"] == "rA"
    # same snapshot, within check interval -> no DB at all
    e2 = db.read_amm_index_entries_cached()
    assert e2 is e1 and calls["full"] == 1
    # force a re-check with unchanged snapshot -> 1-row probe only
    db._amm_index_mem["checked_mono"] = 0.0
    db.read_amm_index_entries_cached()
    assert calls["full"] == 1 and calls["snap"] >= 2
    # a NEW process (empty memory) with the file present -> probe + file, no full read
    db._amm_index_mem.update(snap=None, entries=None, checked_mono=0.0)
    e3 = db.read_amm_index_entries_cached()
    assert calls["full"] == 1 and e3 == e1
    assert json.load(open(tmp_path / "amm.json"))["snapshot_ts"] == 1000
    # snapshot moves -> exactly one more full read
    snap["v"] = 1001
    db._amm_index_mem["checked_mono"] = 0.0
    db.read_amm_index_entries_cached()
    assert calls["full"] == 2


def test_homepage_uses_sql_aggregate_not_full_table():
    src = (ROOT / "app.py").read_text()
    i = src.index("def index():"); j = src.index("def ", i + 10)
    body = src[i:j]
    assert "db.read_amm_homepage_stats(" in body
    # the full-table path survives only as the PG-unavailable fallback
    assert body.count("_ranked_amm_snapshot()") == 1
    assert "_build_xrp_distribution(ranked_full, amm_xrp_total=_amm_xrp_total)" in body


def test_homepage_stats_sql_has_the_same_definitions():
    """The aggregate must encode exactly what index() used to compute in
    Python: exact/estimated TVL sum, priced = exact/estimated AND tvl>0,
    XRP side from asset_a else asset_b, top-N by tvl desc with tvl>0."""
    import inspect, db
    src = inspect.getsource(db.read_amm_homepage_stats)
    assert "FILTER (WHERE tvl_status IN ('exact','estimated'))" in src
    assert "tvl_status IN ('exact','estimated') AND tvl_usd > 0" in src
    assert "asset_a->>'currency' = 'XRP' THEN amount_a" in src
    assert "asset_b->>'currency' = 'XRP' THEN amount_b" in src
    assert "WHERE tvl_usd > 0 ORDER BY tvl_usd DESC" in src


def test_relay_reloads_pool_set_only_when_snapshot_changes(monkeypatch):
    import live_stream_relay as L
    class Cur:
        def __init__(s, log): s.log = log; s._last = None
        def __enter__(s): return s
        def __exit__(s, *a): pass
        def execute(s, q, p=None):
            s.log.append(q); s._last = q
        def fetchone(s): return (777,)
        def fetchall(s):
            if "amm_account" in s._last: return [("rPOOL1",), ("rPOOL2",)]
            return [("USD", "rISS")]
    class Conn:
        def __init__(s, log): s.log = log
        def __enter__(s): return s
        def __exit__(s, *a): pass
        def cursor(s): return Cur(s.log)
    log = []
    import types
    fake = types.SimpleNamespace(connect=lambda *a, **k: Conn(log))
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    L._pools_cache.update(snap=None, pools=set())
    r1 = L.load_refs_from_pg("postgres://x")
    assert r1["pool_accounts"] == {"rPOOL1", "rPOOL2"}
    assert sum("SELECT amm_account" in q for q in log) == 1
    r2 = L.load_refs_from_pg("postgres://x"); r3 = L.load_refs_from_pg("postgres://x")
    assert r3["pool_accounts"] == r1["pool_accounts"]
    assert sum("SELECT amm_account" in q for q in log) == 1, "pool set re-pulled without a snapshot change"
    assert sum("MAX(snapshot_ts)" in q for q in log) == 3


def test_amm_cache_file_corruption_falls_back_to_neon_and_self_heals(monkeypatch, tmp_path):
    """A truncated, empty, wrong-shaped, binary, or stale-snapshot cache file
    must never raise: the accessor falls back to the table read and rewrites
    a valid file."""
    import db
    path = tmp_path / "amm.json"
    monkeypatch.setattr(db, "AMM_INDEX_CACHE_PATH", str(path))
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "read_amm_snapshot_ts", lambda: 5)
    calls = {"n": 0}
    def full():
        calls["n"] += 1; return [{"Account": "rX"}]
    monkeypatch.setattr(db, "read_amm_index_entries", full)
    for content in ('{"snapshot_ts": 5, "entries": [{"Acc', "", '{"snapshot_ts": 5, "entries": {"a":1}}',
                    "\x00\xff\xfe", '{"snapshot_ts": 4, "entries": [{"Account":"OLD"}]}'):
        path.write_text(content, errors="ignore")
        db._amm_index_mem.update(snap=None, entries=None, checked_mono=0.0)
        before = calls["n"]
        assert db.read_amm_index_entries_cached() == [{"Account": "rX"}]
        assert calls["n"] == before + 1, "did not fall back to the table read"
        assert json.loads(path.read_text())["snapshot_ts"] == 5, "did not self-heal the file"


def test_amm_cache_store_is_atomic_under_concurrent_writers(monkeypatch, tmp_path):
    """Many writers, unique temp names, os.replace: a reader must never
    observe a partial file, and no temp files may be left behind."""
    import db, threading
    path = tmp_path / "amm.json"
    monkeypatch.setattr(db, "AMM_INDEX_CACHE_PATH", str(path))
    bad = []
    stop = threading.Event()
    def reader():
        while not stop.is_set():
            try:
                if path.exists():
                    j = json.loads(path.read_text())
                    assert j["snapshot_ts"] == 9 and isinstance(j["entries"], list) and len(j["entries"]) == 300
            except FileNotFoundError:
                pass
            except Exception as e:  # noqa: BLE001
                bad.append(repr(e))
    def writer(i):
        for _ in range(40):
            db._amm_index_file_store(9, [{"Account": f"r{i}", "pad": "x" * 2000}] * 300)
    rt = threading.Thread(target=reader); rt.start()
    ws = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for w in ws: w.start()
    for w in ws: w.join()
    stop.set(); rt.join()
    assert bad == [], bad[:3]
    assert json.loads(path.read_text())["snapshot_ts"] == 9
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []
