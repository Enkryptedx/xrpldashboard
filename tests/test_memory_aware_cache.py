"""Tests for caching.memory_aware_cache — the 7 gates specified in
Section 6.2 of docs/PHASE2_MEMORY_AWARE_CACHE_DESIGN.md.

All tests must run in < 5s combined. These prove the primitive works
in isolation before it's wired into any request path.
"""

from __future__ import annotations

import threading
import time

import pytest

from caching.memory_aware_cache import MemoryAwareTTLCache


# -- Gate 1: single-flight collision test ---------------------------------

def test_single_flight_collision_computes_exactly_once():
    """20 threads race on the same cold key. Compute must fire ONCE.
    Wall time must be near one compute duration, not 20x."""
    cache = MemoryAwareTTLCache(max_bytes=1_000_000,
                                default_ttl_seconds=60,
                                name="test_sf")
    compute_count = 0
    compute_lock = threading.Lock()

    def slow_computer():
        nonlocal compute_count
        time.sleep(0.5)
        with compute_lock:
            compute_count += 1
        return "the-value"

    results = [None] * 20
    threads = []

    def worker(idx):
        results[idx] = cache.get_or_compute("k", slow_computer)

    start = time.monotonic()
    for i in range(20):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start

    assert compute_count == 1, f"computed {compute_count} times, want 1"
    assert all(r == "the-value" for r in results)
    # 20 x 0.5s sequential = 10s. Single-flight target: well under 2s.
    assert elapsed < 2.0, f"took {elapsed:.2f}s, want <2s (single-flight failed)"

    stats = cache.stats()
    assert stats["misses"] == 1
    # Waiters that saw the fresh entry after unlock get counted as sf_collisions.
    assert stats["sf_collisions"] >= 1


# -- Gate 2: LRU eviction test --------------------------------------------

def test_lru_eviction_when_exceeding_max_bytes():
    """max_bytes=1000, 10 entries at size_hint 200 each. Should evict 5."""
    cache = MemoryAwareTTLCache(max_bytes=1000, default_ttl_seconds=60,
                                name="test_lru")

    for i in range(10):
        cache.get_or_compute(
            f"key_{i}",
            lambda i=i: f"value_{i}",
            size_hint_bytes=200,
        )

    stats = cache.stats()
    assert stats["entries"] == 5, f"got {stats['entries']} entries, want 5"
    assert stats["evictions"] == 5, f"got {stats['evictions']} evictions, want 5"
    assert stats["current_bytes"] == 1000

    # The 5 most-recently-inserted should survive (keys 5..9).
    for i in range(5):
        # key_0..key_4 evicted — asking for them = fresh compute (miss).
        called = []
        cache.get_or_compute(f"key_{i}",
                             lambda i=i: called.append(1) or f"new_{i}",
                             size_hint_bytes=200)
        assert called == [1], f"key_{i} should have been evicted"


# -- Gate 3: oversized-entry test -----------------------------------------

def test_oversized_entry_returned_but_not_cached():
    """Value larger than max_bytes is returned to caller, not cached."""
    cache = MemoryAwareTTLCache(max_bytes=1000, default_ttl_seconds=60,
                                name="test_oversized")

    value = cache.get_or_compute("big",
                                 lambda: "x" * 5000,
                                 size_hint_bytes=5000)

    assert value == "x" * 5000
    stats = cache.stats()
    assert stats["entries"] == 0
    assert stats["refuse_oversized"] == 1
    assert stats["current_bytes"] == 0


# -- Gate 4: SWR refresh test ---------------------------------------------

def test_swr_returns_stale_and_refreshes_in_background():
    """ttl=1s, swr=5s. After ttl, first call returns stale, spawns
    background refresh. Next call after refresh returns fresh."""
    cache = MemoryAwareTTLCache(max_bytes=1_000_000, default_ttl_seconds=60,
                                name="test_swr")

    counter = [0]

    def computer():
        counter[0] += 1
        return f"v{counter[0]}"

    # Populate.
    v0 = cache.get_or_compute("k", computer, ttl_seconds=1.0,
                              stale_while_revalidate_seconds=5.0,
                              size_hint_bytes=32)
    assert v0 == "v1"

    # Wait past ttl, still inside swr.
    time.sleep(1.2)

    v1 = cache.get_or_compute("k", computer, ttl_seconds=1.0,
                              stale_while_revalidate_seconds=5.0,
                              size_hint_bytes=32)
    # SWR served stale.
    assert v1 == "v1", f"SWR should have served stale, got {v1}"

    # Wait for background refresh to fully complete — poll on the
    # stats() event, not the counter. The counter increments BEFORE
    # _store runs, so polling the counter races the store and can
    # observe v1 still in the cache.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and cache.stats()["refresh_ok"] < 1:
        time.sleep(0.05)
    assert counter[0] == 2, "background refresh did not fire"
    assert cache.stats()["refresh_ok"] == 1, "refresh did not complete"

    # Next call should see the fresh value.
    v2 = cache.get_or_compute("k", computer, ttl_seconds=1.0,
                              stale_while_revalidate_seconds=5.0,
                              size_hint_bytes=32)
    assert v2 == "v2"

    stats = cache.stats()
    assert stats["refresh_ok"] == 1
    assert stats["refresh_fail"] == 0


# -- Gate 5: compute-exception test ---------------------------------------

def test_exception_propagates_and_does_not_cache():
    """Failed compute must NOT poison the cache. Next call retries."""
    cache = MemoryAwareTTLCache(max_bytes=1_000_000, default_ttl_seconds=60,
                                name="test_exc")

    calls = [0]

    def bad_computer():
        calls[0] += 1
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError, match="upstream down"):
        cache.get_or_compute("k", bad_computer)

    assert cache.stats()["entries"] == 0
    assert calls[0] == 1

    # Retry: should re-invoke compute (no cached exception).
    with pytest.raises(RuntimeError):
        cache.get_or_compute("k", bad_computer)
    assert calls[0] == 2

    # After a successful compute, entry IS cached.
    def good_computer():
        calls[0] += 1
        return "recovered"

    v = cache.get_or_compute("k", good_computer, size_hint_bytes=32)
    assert v == "recovered"
    assert calls[0] == 3

    # Next call = hit (no more compute).
    v2 = cache.get_or_compute("k", good_computer, size_hint_bytes=32)
    assert v2 == "recovered"
    assert calls[0] == 3


# -- Gate 6: memory-accounting drift test ---------------------------------

def test_memory_accounting_within_10_percent_of_hint_sum():
    """100 entries with known size_hint_bytes. current_bytes must be
    within ±10% of the sum."""
    cache = MemoryAwareTTLCache(max_bytes=10_000_000, default_ttl_seconds=60,
                                name="test_accounting")

    hint = 5000
    for i in range(100):
        cache.get_or_compute(f"k_{i}", lambda i=i: {"i": i, "pad": "x" * 500},
                             size_hint_bytes=hint)

    expected = 100 * hint
    stats = cache.stats()
    actual = stats["current_bytes"]
    drift_pct = abs(actual - expected) / expected * 100

    assert drift_pct <= 10, (
        f"accounting drift {drift_pct:.1f}% "
        f"(actual={actual}, expected={expected})"
    )
    assert stats["entries"] == 100


# -- Gate 7: guard-only smoke test ----------------------------------------

def test_single_flight_serializes_even_with_write_disabled(monkeypatch):
    """Simulate CACHE_ENABLED=false path: cache-write mocked to no-op.
    Single-flight guard must STILL serialize computes on the same key.
    This proves the guard is armed independent of the cache write path
    (Section 6.3 rollout requirement)."""
    cache = MemoryAwareTTLCache(max_bytes=1_000_000, default_ttl_seconds=60,
                                name="test_guard_only")

    # Neutralize the write path — every compute misses cache thereafter.
    def noop_store(key, value, ttl, swr, hint, compute_ms):
        return
    monkeypatch.setattr(cache, "_store", noop_store)

    compute_count = 0
    compute_lock = threading.Lock()

    def slow_computer():
        nonlocal compute_count
        time.sleep(0.3)
        with compute_lock:
            compute_count += 1
        return "v"

    threads = []
    for _ in range(10):
        t = threading.Thread(
            target=lambda: cache.get_or_compute("k", slow_computer)
        )
        threads.append(t)
        t.start()

    start = time.monotonic()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start

    # With write disabled and no cache read of a prior compute, every
    # thread must fall through and re-compute — BUT the per-key lock
    # serializes them. 10 x 0.3s serial ≈ 3s (would be 0.3s if the
    # guard weren't engaging OR ~30s if there were 10 parallel computes
    # without any coordination).
    assert compute_count == 10, (
        f"guard should have serialized 10 computes, got {compute_count}"
    )
    # Serial = ~3s. Parallel-would-have-been = 0.3s. Give margin.
    assert 2.5 < elapsed < 5.0, (
        f"elapsed={elapsed:.2f}s — expected ~3s (serialized). "
        f"<2.5s = guard not engaging, >5s = something else broken"
    )
