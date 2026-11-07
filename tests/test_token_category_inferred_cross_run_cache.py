"""Cross-run cache for the inferred token-category map.

The in-process watermark gate (Oct 2026 egress audit) only helps a
long-lived process. tokens_summary_walker runs every 300s as a FRESH
interpreter, so its cache was always cold and it re-read the whole table
once per run: 1,963 calls x ~9,243 rows = 18.1M rows/week leaving Neon to
fetch data that only changes when token_issuer_flags_walker upserts it.

These tests pin the cache's contract: a cold process must skip the table
read while MAX(fetched_at) is unchanged, must re-read the moment it moves,
and must fall back to the live read on any cache problem -- saving egress is
never worth failing a render.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import pytest

import app as app_mod
import db as db_mod


WM = 1_762_500_000.0
MAP = {
    ("0158415500000000C1F76FF6ECB0BAC600000000", "rIssuerOne"): {
        "currency_display": "ALPHA",
        "category": "stablecoin",
        "source": "toml",
        "source_url": "https://example.test/.well-known/xrp-ledger.toml",
        "fetched_at": dt.datetime(2026, 11, 7, 12, 0, tzinfo=dt.timezone.utc),
    },
    ("USD", "rIssuerTwo"): {
        "currency_display": "USD",
        "category": "other",
        "source": "toml",
        "source_url": None,
        "fetched_at": None,
    },
}


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    """Point the cache at a temp path and reset the in-process gate, so each
    test starts as a genuinely cold process would."""
    path = tmp_path / "tci.json"
    monkeypatch.setattr(app_mod, "_TOKEN_CATEGORY_INFERRED_CACHE_PATH", str(path))
    monkeypatch.setattr(app_mod, "_token_category_inferred_cache",
                        {"watermark": None, "result": None, "checked_mono": 0.0})
    return path


def _wire(monkeypatch, watermark, counter):
    """Stub the two db reads, counting the expensive one."""
    monkeypatch.setattr(db_mod, "read_token_category_inferred_watermark",
                        lambda: watermark, raising=True)

    def _full():
        counter.append(1)
        return MAP

    monkeypatch.setattr(db_mod, "read_token_category_inferred_map",
                        _full, raising=True)


def test_cold_process_reads_table_once_then_writes_cache(cache, monkeypatch):
    reads: list[int] = []
    _wire(monkeypatch, WM, reads)
    got = app_mod._cached_token_category_inferred_map()
    assert got == MAP
    assert len(reads) == 1
    assert cache.is_file(), "first run must leave a cache for the next process"
    assert json.loads(cache.read_text())["watermark"] == WM


def test_second_cold_process_skips_the_table_entirely(cache, monkeypatch):
    """The whole point: 280 walker runs/day stop re-reading the table."""
    reads: list[int] = []
    _wire(monkeypatch, WM, reads)
    app_mod._cached_token_category_inferred_map()
    assert len(reads) == 1
    # Simulate the next tick: brand-new process, same watermark.
    monkeypatch.setattr(app_mod, "_token_category_inferred_cache",
                        {"watermark": None, "result": None, "checked_mono": 0.0})
    got = app_mod._cached_token_category_inferred_map()
    assert got == MAP
    assert len(reads) == 1, "cache hit must not touch the table"


def test_moved_watermark_forces_a_fresh_read(cache, monkeypatch):
    reads: list[int] = []
    _wire(monkeypatch, WM, reads)
    app_mod._cached_token_category_inferred_map()
    assert len(reads) == 1
    monkeypatch.setattr(app_mod, "_token_category_inferred_cache",
                        {"watermark": None, "result": None, "checked_mono": 0.0})
    _wire(monkeypatch, WM + 1, reads)       # an upsert landed
    app_mod._cached_token_category_inferred_map()
    assert len(reads) == 2, "a changed watermark must invalidate the cache"


def test_shape_survives_the_round_trip_including_datetime(cache, monkeypatch):
    """No consumer reads fetched_at today, but a cache that changes a value's
    TYPE is a trap for the next one that does.

    The read count is asserted FIRST and deliberately: without it this test
    passes whether the cache is used or not, because the live read returns
    the same map. It did pass that way while deserialisation was raising
    NameError into a catch-all and every lookup silently missed.
    """
    reads: list[int] = []
    _wire(monkeypatch, WM, reads)
    app_mod._cached_token_category_inferred_map()
    monkeypatch.setattr(app_mod, "_token_category_inferred_cache",
                        {"watermark": None, "result": None, "checked_mono": 0.0})
    got = app_mod._cached_token_category_inferred_map()
    assert len(reads) == 1, "must be served FROM the cache, else this proves nothing"
    assert set(got) == set(MAP), "composite tuple keys must survive"
    a = got[("0158415500000000C1F76FF6ECB0BAC600000000", "rIssuerOne")]
    assert isinstance(a["fetched_at"], dt.datetime)
    assert a["fetched_at"] == MAP[
        ("0158415500000000C1F76FF6ECB0BAC600000000", "rIssuerOne")]["fetched_at"]
    assert got[("USD", "rIssuerTwo")]["fetched_at"] is None


def test_corrupt_cache_falls_back_to_the_live_read(cache, monkeypatch):
    reads: list[int] = []
    _wire(monkeypatch, WM, reads)
    cache.write_text("{not json")
    assert app_mod._cached_token_category_inferred_map() == MAP
    assert len(reads) == 1, "a broken cache must never fail the caller"


def test_unwritable_cache_path_still_serves_the_map(tmp_path, monkeypatch):
    """The web tier may run on a read-only or ephemeral filesystem."""
    monkeypatch.setattr(app_mod, "_TOKEN_CATEGORY_INFERRED_CACHE_PATH",
                        "/proc/nonexistent/dir/tci.json")
    monkeypatch.setattr(app_mod, "_token_category_inferred_cache",
                        {"watermark": None, "result": None, "checked_mono": 0.0})
    reads: list[int] = []
    _wire(monkeypatch, WM, reads)
    assert app_mod._cached_token_category_inferred_map() == MAP
    assert len(reads) == 1


def test_null_watermark_never_caches(cache, monkeypatch):
    """A None watermark means we cannot prove freshness, so it must read
    live every time rather than trust a file it cannot validate."""
    reads: list[int] = []
    _wire(monkeypatch, None, reads)
    app_mod._cached_token_category_inferred_map()
    monkeypatch.setattr(app_mod, "_token_category_inferred_cache",
                        {"watermark": None, "result": None, "checked_mono": 0.0})
    app_mod._cached_token_category_inferred_map()
    assert len(reads) == 2
    assert not cache.exists(), "nothing provable, nothing cached"


def test_write_is_atomic_leaving_no_partial_file(cache, monkeypatch):
    reads: list[int] = []
    _wire(monkeypatch, WM, reads)
    app_mod._cached_token_category_inferred_map()
    leftovers = [p for p in os.listdir(cache.parent) if p.endswith(".tmp")]
    assert leftovers == [], f"temp files left behind: {leftovers}"
