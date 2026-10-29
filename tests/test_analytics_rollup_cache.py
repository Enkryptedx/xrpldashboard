"""Guards for the hour-keyed analytics rollup cache (2026-10-29).

Context: owner-side pg_stat_statements (window opening 2026-10-27 03:12 ET)
showed the page_views analytics aggregates were the largest non-backup
consumer of compute on the Neon instance -- read_country_count at
2,423 + 710 calls, the COUNT(*)/COUNT(DISTINCT visitor_hash) pair at
2,933, and the two scan_burst_cohorts statements at 1,576 + 1,617 against
a loop that should have fired ~3 times in the window.

These tests are DB-free (DATABASE_URL="") and assert the three properties
the fix rests on:

  1. a value is computed at most ONCE per clock hour per key;
  2. the cache is fail-open -- every fault degrades to the old uncached
     behaviour and never raises into a route;
  3. the guard state lives in Postgres, so it survives the gunicorn
     worker recycle that was driving the burst-cohort scan.

Nothing here talks to a real database or the live site.
"""

import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402


# ── Fake Postgres standing in for analytics_rollup_cache ────────────────
# Only the three statements cached_hourly issues are understood; anything
# else raises, so a future change to the SQL cannot pass this file by
# being silently ignored.

class _FakeCursor:
    def __init__(self, store, log):
        self._store = store
        self._log = log
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self._log.append(flat)
        if flat.startswith("CREATE TABLE"):
            self._result = None
            return
        if flat.startswith("SELECT payload, computed_at"):
            rec = self._store.get(params[0])
            self._result = (
                (rec["payload"], rec["computed_at"]) if rec else None
            )
            return
        if flat.startswith("SELECT payload FROM analytics_rollup_cache"):
            name, bucket = params[0], params[1]
            rec = self._store.get(name)
            self._result = (
                (rec["payload"],)
                if rec and rec["hour_bucket"] == bucket
                else None
            )
            return
        if flat.startswith("INSERT INTO analytics_rollup_cache"):
            name, bucket, payload, computed_at = params[0:4]
            prev = self._store.get(name)
            # Mirrors the WHERE hour_bucket <= EXCLUDED.hour_bucket guard.
            if prev is None or prev["hour_bucket"] <= bucket:
                self._store[name] = {
                    "hour_bucket": bucket,
                    "payload": json.loads(payload),
                    "computed_at": computed_at,
                }
            self._result = None
            return
        raise AssertionError(f"unexpected SQL reached the cache: {flat[:90]}")

    def fetchone(self):
        return self._result


class _FakeConn:
    def __init__(self, store, log):
        self._store = store
        self._log = log
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._store, self._log)

    def commit(self):
        self.commits += 1


@pytest.fixture
def fake_pg(monkeypatch):
    store, log = {}, []
    monkeypatch.setattr(db, "pg_available", lambda: True, raising=True)
    monkeypatch.setattr(
        db, "pg_connect", lambda: _FakeConn(store, log), raising=True
    )
    # Table creation is exercised separately; skip it here so the log
    # holds only the read/write statements under test.
    monkeypatch.setattr(db, "_ROLLUP_CACHE_TABLE_READY", True, raising=True)
    return store, log


# ── 1. the actual requirement: once per clock hour ─────────────────────

def test_cached_hourly_computes_once_per_clock_hour(fake_pg):
    """Two reads in the same clock hour compute once; the next hour
    recomputes. This is the property the whole change exists for."""
    store, _log = fake_pg
    calls = []

    def compute():
        calls.append(1)
        return {"views": len(calls)}

    base = 480_000 * 3600  # an exact hour boundary

    first = db.cached_hourly("probe", compute, now=base)
    mid = db.cached_hourly("probe", compute, now=base + 1800)
    late = db.cached_hourly("probe", compute, now=base + 3599)

    assert first == {"views": 1}
    assert mid == {"views": 1}, "second read in the hour recomputed"
    assert late == {"views": 1}, "read at :59:59 recomputed"
    assert len(calls) == 1, f"expected 1 compute in the hour, got {len(calls)}"

    nxt = db.cached_hourly("probe", compute, now=base + 3600)
    assert nxt == {"views": 2}, "new clock hour did not recompute"
    assert len(calls) == 2
    assert store["probe"]["hour_bucket"] == 480_001


def test_distinct_keys_do_not_share_a_slot(fake_pg):
    store, _log = fake_pg
    base = 480_000 * 3600
    db.cached_hourly("left", lambda: 1, now=base)
    db.cached_hourly("right", lambda: 2, now=base)
    assert store["left"]["payload"] == 1
    assert store["right"]["payload"] == 2


def test_hour_bucket_matches_api_request_counters_convention():
    """hour_bucket is unix_ts // 3600 -- the same convention the existing
    api_request_counters table documents."""
    assert db._hour_bucket(0) == 0
    assert db._hour_bucket(3599) == 0
    assert db._hour_bucket(3600) == 1
    assert db._hour_bucket(1_761_000_000) == 1_761_000_000 // 3600


# ── 2. fail-open ────────────────────────────────────────────────────────

def test_cached_hourly_falls_back_when_pg_unavailable(monkeypatch):
    monkeypatch.setattr(db, "pg_available", lambda: False, raising=True)
    calls = []

    def compute():
        calls.append(1)
        return "value"

    assert db.cached_hourly("probe", compute) == "value"
    assert len(calls) == 1


def test_cached_hourly_falls_back_when_cache_read_raises(monkeypatch):
    """A broken cache must cost the old uncached behaviour, never a 500."""
    def boom():
        raise RuntimeError("neon unreachable")

    monkeypatch.setattr(db, "pg_available", lambda: True, raising=True)
    monkeypatch.setattr(db, "pg_connect", boom, raising=True)
    calls = []

    def compute():
        calls.append(1)
        return {"ok": True}

    assert db.cached_hourly("probe", compute) == {"ok": True}
    assert len(calls) == 1


def test_analytics_readers_fail_open_with_no_database():
    """With DATABASE_URL="" the wrapped readers still return their zeroed
    shapes rather than raising -- the no-DB resilience CI protects."""
    stats = db.read_page_view_stats(kind="human")
    assert set(stats) >= {"now", "hour", "today", "week", "all_time"}
    assert stats["all_time"] == {"views": 0, "uniques": 0}

    cta = db.read_cta_click_stats(cta_id="institutional-contact")
    assert cta["all_time"] == {"clicks": 0, "uniques": 0}

    assert db.read_country_count(24 * 60 * 60, kind="human") == 0
    assert db.read_country_count(None, kind="human") == 0


# ── 3. durability / regression guards ───────────────────────────────────

def test_upsert_refuses_to_overwrite_a_newer_hour(fake_pg):
    """Two workers racing across an hour boundary must not let the older
    hour's value land on top of the newer one."""
    store, _log = fake_pg
    base = 480_000 * 3600
    db.cached_hourly("probe", lambda: "new", now=base + 3600)   # hour N+1
    db.cached_hourly("probe", lambda: "old", now=base)          # hour N
    assert store["probe"]["payload"] == "new"
    assert store["probe"]["hour_bucket"] == 480_001


def test_upsert_sql_carries_the_hour_bucket_guard(fake_pg):
    _store, log = fake_pg
    db.cached_hourly("probe", lambda: 1, now=480_000 * 3600)
    inserts = [s for s in log if s.startswith("INSERT INTO")]
    assert inserts, "no upsert was issued"
    assert "<= EXCLUDED.hour_bucket" in inserts[0]


def test_schema_ddl_declares_the_rollup_cache_table():
    ddl = db.SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS analytics_rollup_cache" in ddl
    assert "cache_key   TEXT    PRIMARY KEY" in ddl
    assert "analytics_rollup_cache_hour_idx" in ddl


def test_scan_burst_cohorts_guard_defaults_to_one_hour():
    """The burst-cohort scan was being re-run by worker churn, not by its
    24h loop. Its guard interval must default to an hour."""
    sig = inspect.signature(db.scan_burst_cohorts)
    assert sig.parameters["min_interval_seconds"].default == 3600


def test_country_count_aggregate_sql_is_unchanged():
    """The cache wraps read_country_count; it must not have altered the
    aggregate or its allow-list layering. Assert the SQL and both
    allow-list branches still live in the uncached implementation."""
    src = inspect.getsource(db._read_country_count_uncached)
    assert "SELECT COUNT(DISTINCT country) " in src
    assert "sql_valid_country_clause" in src
    assert "sql_not_self_probe_ua_clause" in src
    assert "sql_not_bot_ua_clause" in src
