"""/tokens data layer (Charlie 2026-10-08, tokens-speed). Hermetic.

Before: 10 serial Neon round-trips per hit (7 in the route, 1 in the
data-age label, 1 in live_tier_counts, 1 in the site-wide icon
context_processor) and no cache -> 7-9 s on Render. Now: one 60 s
stale-while-revalidate entry for the range-independent reads, filled
concurrently; one 60 s entry per range chip; the icon lookup cached.

These tests pin: (1) warm hits make zero DB calls, (2) cold fill runs the
reads concurrently and once, (3) past the TTL the stale value is served
while a background refresh runs, (4) a PG outage still falls through to
the original fallbacks (fail-open), (5) the rendered numbers come from
the cached data.
"""
from __future__ import annotations

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("TOKENS_PREWARM", "0")

import app as app_mod  # noqa: E402
import db  # noqa: E402
import shared_tier_verifier  # noqa: E402


def _reset():
    app_mod._tokens_shared_state.update({"value": None, "at": 0.0, "refreshing": False})
    app_mod._tokens_rows_for_range.__wrapped__  # exists
    # ttl_cache has no public clear; re-decorate for a clean store
    app_mod._tokens_rows_for_range = app_mod.ttl_cache(seconds=app_mod._TOKENS_CACHE_TTL)(
        app_mod._tokens_rows_for_range.__wrapped__)


class Counter:
    def __init__(self):
        self.calls = {}
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0

    def wrap(self, name, value, delay=0.0):
        def fn(*a, **k):
            with self.lock:
                self.calls[name] = self.calls.get(name, 0) + 1
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
            time.sleep(delay)
            with self.lock:
                self.inflight -= 1
            return value
        return fn


def _fake_db(monkeypatch, counter, delay=0.05):
    now_hour = int(time.time() // 3600)
    rows = [("USD", "rIssuerA", 1234, 20), ("EUR", "rIssuerB", 99, 5)]
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "read_token_volume_aggregates",
                        counter.wrap("aggregates", rows, delay))
    monkeypatch.setattr(db, "read_token_warning_aggregates",
                        counter.wrap("warn_aggregates", rows[:1], delay))
    monkeypatch.setattr(db, "read_token_volume_bucket_stats",
                        counter.wrap("bucket_stats", (now_hour - 100, now_hour, 101), delay))
    monkeypatch.setattr(db, "read_token_prices_map",
                        counter.wrap("prices", {("USD", "rIssuerA"): 0.5}, delay))
    monkeypatch.setattr(db, "read_token_category_inferred_map",
                        counter.wrap("inferred", {}, delay))
    monkeypatch.setattr(db, "read_token_warnings_recent",
                        counter.wrap("warnings", [], delay))
    monkeypatch.setattr(db, "read_max_token_bucket",
                        counter.wrap("max_bucket", now_hour, delay))
    monkeypatch.setattr(shared_tier_verifier, "resolve_all_map",
                        counter.wrap("tier", ({}, "test"), delay))
    monkeypatch.setattr(shared_tier_verifier, "live_tier_counts",
                        counter.wrap("tier_counts", ({"verified": 1}, "test"), delay))
    monkeypatch.setattr(db, "pg_connect", counter.wrap("pg_connect", None))
    return rows


def test_warm_hits_make_no_db_calls(monkeypatch):
    _reset()
    c = Counter()
    _fake_db(monkeypatch, c)
    client = app_mod.app.test_client()
    r = client.get("/tokens")
    assert r.status_code == 200
    first = dict(c.calls)
    assert first["aggregates"] == 2          # 24h list + 30d hero, once each
    assert first["bucket_stats"] == first["prices"] == first["inferred"] == 1
    assert first["max_bucket"] == first["tier"] == first["tier_counts"] == 1
    for _ in range(3):
        assert client.get("/tokens").status_code == 200
    assert c.calls == first, "warm hits must not touch the DB"


def test_cold_fill_runs_reads_concurrently(monkeypatch):
    _reset()
    c = Counter()
    _fake_db(monkeypatch, c, delay=0.15)
    t = time.perf_counter()
    app_mod._tokens_shared_data()
    elapsed = time.perf_counter() - t
    assert c.max_inflight >= 4, c.max_inflight
    assert elapsed < 0.15 * 4, elapsed      # 8 reads x 0.15 s serial would be 1.2 s


def test_stale_while_revalidate(monkeypatch):
    _reset()
    c = Counter()
    _fake_db(monkeypatch, c, delay=0.2)
    v1 = app_mod._tokens_shared_data()
    app_mod._tokens_shared_state["at"] = time.time() - app_mod._TOKENS_CACHE_TTL - 1
    before = dict(c.calls)
    t = time.perf_counter()
    v2 = app_mod._tokens_shared_data()     # stale: must return immediately
    assert time.perf_counter() - t < 0.1
    assert v2 is v1
    time.sleep(0.6)                          # background refresh completes
    assert c.calls["bucket_stats"] == before["bucket_stats"] + 1
    assert app_mod._tokens_shared_state["refreshing"] is False
    assert time.time() - app_mod._tokens_shared_state["at"] < 2


def test_pg_down_falls_through(monkeypatch):
    _reset()
    monkeypatch.setattr(db, "pg_available", lambda: False)
    monkeypatch.setattr(shared_tier_verifier, "resolve_all_map", lambda: ({}, "snapshot"))
    monkeypatch.setattr(shared_tier_verifier, "live_tier_counts", lambda: ({}, "snapshot"))
    r = app_mod.app.test_client().get("/tokens")
    assert r.status_code == 200
    assert app_mod._tokens_shared_data()["bucket_stats"] is None


def test_rendered_numbers_come_from_cache(monkeypatch):
    _reset()
    c = Counter()
    _fake_db(monkeypatch, c)
    html = app_mod.app.test_client().get("/tokens").data.decode()
    assert "1,234" in html and "rIssue" in html


def test_icon_lookup_cached(monkeypatch):
    calls = {"n": 0}

    class Cur:
        def execute(self, *a): calls["n"] += 1
        def fetchall(self): return [("USD", "rX", "icons/usd.png")]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class Conn:
        def cursor(self): return Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "pg_connect", lambda: Conn())
    app_mod._token_icon_lookup_cached = app_mod.ttl_cache(seconds=60)(
        app_mod._token_icon_lookup_cached.__wrapped__)
    a = app_mod._token_icon_lookup_cached()
    b = app_mod._token_icon_lookup_cached()
    assert a == {"USD::rX": "/static/icons/usd.png"} and b is a
    assert calls["n"] == 1
