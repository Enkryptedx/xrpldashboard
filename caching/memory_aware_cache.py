"""Per-process TTL cache with a hard memory budget, LRU eviction,
single-flight guard, and stale-while-revalidate.

Introduced in Phase 2 (2026-08-21) as the memory-safe replacement for the
naive homepage SWR cache that OOM'd on 2026-08-15 — see
docs/PHASE2_MEMORY_AWARE_CACHE_DESIGN.md for the full rationale.

The load-bearing invariant is the single-flight guard (Section 4.2 of the
design pack): a per-key reentrant lock ensures that a cold-miss deploy
cutover cannot spawn N parallel computes for the same key on the same
worker. Cache correctness (TTL, SWR, LRU) is secondary to that invariant.

Public API is small and stable:
    cache = MemoryAwareTTLCache(max_bytes=200 * 1024 * 1024,
                                default_ttl_seconds=60,
                                name="home_html")
    value = cache.get_or_compute(key, computer, ttl_seconds=60,
                                 stale_while_revalidate_seconds=240,
                                 size_hint_bytes=len(html.encode()))

The class is thread-safe under the standard sync gunicorn worker model.
It has NOT been validated under gevent greenlets — that gate is added
when the live-mode/SSE pack lands (Section 9.1 of the design pack).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

log = logging.getLogger(__name__)

# Per-key lock acquisition timeout. Waiters raise TimeoutError past this.
# 30s matches the gunicorn --timeout in render.yaml so a hung compute
# can't outlive the request that spawned it.
_PER_KEY_LOCK_TIMEOUT_SECONDS = 30.0

# Anything larger than this stored WITHOUT an explicit size_hint_bytes
# gets a warning log — recursive sizing is approximate above this bar.
_SIZE_HINT_WARN_THRESHOLD_BYTES = 10 * 1024

# Recursive walker recursion depth for size estimation. Deeper than this
# and we accept some undercount rather than pay the walk cost.
_SIZE_WALKER_MAX_DEPTH = 3

# Full-recount cadence in stats() calls. Corrects drift from
# under-counted deep containers without paying the walk cost per set().
_STATS_RECOUNT_EVERY_N_CALLS = 1000


def _estimate_size_bytes(value: Any, depth: int = 0,
                         seen: set[int] | None = None) -> int:
    """Best-effort size estimate. Returns bytes.

    Uses sys.getsizeof for primitives, walks containers up to
    _SIZE_WALKER_MAX_DEPTH, falls back to len(repr(...)) for opaque
    objects. Not exact. Callers who know better should pass
    size_hint_bytes to get_or_compute.
    """
    if seen is None:
        seen = set()

    obj_id = id(value)
    if obj_id in seen:
        return 0
    seen.add(obj_id)

    try:
        base = sys.getsizeof(value)
    except (TypeError, ValueError):
        try:
            return len(repr(value))
        except Exception:
            return 64

    if depth >= _SIZE_WALKER_MAX_DEPTH:
        return base

    if isinstance(value, (str, bytes, bytearray)):
        return base

    if isinstance(value, dict):
        for k, v in value.items():
            base += _estimate_size_bytes(k, depth + 1, seen)
            base += _estimate_size_bytes(v, depth + 1, seen)
        return base

    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            base += _estimate_size_bytes(item, depth + 1, seen)
        return base

    return base


class _Entry:
    """Cache entry. Kept as a plain class (not dataclass) to make the
    memory footprint per entry predictable — dataclass adds slots
    overhead we don't want to reason about in the accounting."""

    __slots__ = ("value", "expires_at", "swr_expires_at", "size_bytes",
                 "refresh_fail_streak")

    def __init__(self, value: Any, expires_at: float,
                 swr_expires_at: float, size_bytes: int):
        self.value = value
        self.expires_at = expires_at
        self.swr_expires_at = swr_expires_at
        self.size_bytes = size_bytes
        self.refresh_fail_streak = 0


class MemoryAwareTTLCache:
    """Per-process TTL cache with hard memory budget + single-flight + SWR.

    See module docstring and docs/PHASE2_MEMORY_AWARE_CACHE_DESIGN.md
    for the full contract. Thread-safe under sync workers.
    """

    def __init__(self, max_bytes: int, default_ttl_seconds: float,
                 name: str = "unnamed"):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be > 0")

        self.name = name
        self.max_bytes = max_bytes
        self.default_ttl_seconds = default_ttl_seconds

        # Insertion-ordered dict IS the LRU: move_to_end on read/write,
        # popitem(last=False) evicts the LRU entry.
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

        # Guards the OrderedDict + accounting + per-key lock registry.
        # Held only during O(1) map ops — never held across a compute.
        self._map_lock = threading.Lock()

        # Per-key locks for single-flight. RLock so a recursive compute
        # on the same key from the same thread doesn't deadlock.
        self._key_locks: dict[str, threading.RLock] = {}

        # Running total. Corrected by periodic full-recount in stats().
        self._current_bytes = 0

        # Cheap counters. Not lock-protected — approximate is fine for stats.
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._sf_collisions = 0
        self._sf_timeouts = 0
        self._refuse_oversized = 0
        self._refresh_ok = 0
        self._refresh_fail = 0
        self._max_bytes_touched = 0
        self._stats_call_count = 0

    def get_or_compute(self,
                       key: str,
                       computer: Callable[[], Any],
                       ttl_seconds: float | None = None,
                       stale_while_revalidate_seconds: float = 0.0,
                       size_hint_bytes: int | None = None) -> Any:
        """Cache lookup + compute-on-miss + single-flight + SWR.

        - Present + fresh: return cached value.
        - Present + stale but within SWR window: return stale, spawn
          background refresh.
        - Absent OR beyond SWR: acquire per-key lock (30s timeout),
          first thread computes, others block then read fresh entry.

        size_hint_bytes: caller-supplied byte count. REQUIRED for
        values >10KB where the recursive walker is unreliable (HTML
        strings should use len(value.encode('utf-8'))).
        """
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds

        # Fast path: check for fresh hit without touching the per-key lock.
        now = time.monotonic()
        with self._map_lock:
            entry = self._entries.get(key)
            if entry is not None:
                if now < entry.expires_at:
                    self._entries.move_to_end(key)
                    self._hits += 1
                    self._emit_stat("hit", key,
                                    size_b=entry.size_bytes,
                                    ms=0.0)
                    return entry.value
                if now < entry.swr_expires_at:
                    # Stale-but-SWR hit: return stale, spawn refresh.
                    self._entries.move_to_end(key)
                    self._hits += 1
                    self._emit_stat("hit", key,
                                    size_b=entry.size_bytes,
                                    ms=0.0,
                                    swr_stale=True)
                    self._spawn_swr_refresh(key, computer, ttl,
                                            stale_while_revalidate_seconds,
                                            size_hint_bytes)
                    return entry.value

        # Cold miss OR beyond SWR: single-flight compute.
        lock = self._get_or_create_key_lock(key)
        wait_start = time.monotonic()
        acquired = lock.acquire(timeout=_PER_KEY_LOCK_TIMEOUT_SECONDS)
        wait_ms = (time.monotonic() - wait_start) * 1000.0

        if not acquired:
            self._sf_timeouts += 1
            self._emit_stat("sf_timeout", key, ms=wait_ms)
            raise TimeoutError(
                f"MemoryAwareTTLCache[{self.name}] key={key[:40]!r} "
                f"single-flight lock timed out after "
                f"{_PER_KEY_LOCK_TIMEOUT_SECONDS}s"
            )

        try:
            # Re-check under lock: another thread may have populated it
            # while we were waiting.
            if wait_ms > 1.0:
                # We waited — someone else was computing. Check for fresh.
                now = time.monotonic()
                with self._map_lock:
                    entry = self._entries.get(key)
                    if entry is not None and now < entry.expires_at:
                        self._sf_collisions += 1
                        self._emit_stat("sf_wait", key,
                                        size_b=entry.size_bytes,
                                        ms=wait_ms,
                                        waited_for_key=True)
                        self._entries.move_to_end(key)
                        return entry.value

            # We're the compute thread. Run it (outside map_lock).
            compute_start = time.monotonic()
            value = computer()
            compute_ms = (time.monotonic() - compute_start) * 1000.0

            self._misses += 1
            self._store(key, value, ttl,
                        stale_while_revalidate_seconds,
                        size_hint_bytes, compute_ms)
            return value
        finally:
            lock.release()

    def invalidate(self, key: str) -> bool:
        """Force-drop an entry. Returns True if it was present."""
        with self._map_lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return False
            self._current_bytes -= entry.size_bytes
            return True

    def clear(self) -> None:
        """Drop all entries. For test teardown; not expected in prod."""
        with self._map_lock:
            self._entries.clear()
            self._current_bytes = 0

    def stats(self) -> dict:
        """Snapshot of cache state. Cheap to call. Triggers a full
        byte-recount every _STATS_RECOUNT_EVERY_N_CALLS calls to correct
        drift from the approximate per-entry accounting."""
        self._stats_call_count += 1
        if self._stats_call_count % _STATS_RECOUNT_EVERY_N_CALLS == 0:
            self._recount_bytes()

        with self._map_lock:
            return {
                "name": self.name,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "sf_collisions": self._sf_collisions,
                "sf_timeouts": self._sf_timeouts,
                "refuse_oversized": self._refuse_oversized,
                "refresh_ok": self._refresh_ok,
                "refresh_fail": self._refresh_fail,
                "current_bytes": self._current_bytes,
                "max_bytes": self.max_bytes,
                "max_bytes_touched": self._max_bytes_touched,
                "entries": len(self._entries),
            }

    # ---- internal ----------------------------------------------------

    def _get_or_create_key_lock(self, key: str) -> threading.RLock:
        """Double-checked locking pattern for per-key lock registry."""
        lock = self._key_locks.get(key)
        if lock is not None:
            return lock
        with self._map_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._key_locks[key] = lock
            return lock

    def _store(self, key: str, value: Any, ttl_seconds: float,
               swr_seconds: float, size_hint_bytes: int | None,
               compute_ms: float) -> None:
        """Insert value into cache with size accounting + LRU eviction.

        If the value alone exceeds max_bytes, it is NOT cached — a
        refuse_oversized event is emitted and the value is returned to
        the caller by get_or_compute regardless.
        """
        if size_hint_bytes is not None:
            size_b = size_hint_bytes
        else:
            size_b = _estimate_size_bytes(value)
            if size_b > _SIZE_HINT_WARN_THRESHOLD_BYTES:
                log.warning(
                    "MemoryAwareTTLCache[%s]: no size_hint_bytes for "
                    "key=%r sized=%d bytes (>%d threshold). Recursive "
                    "sizing is approximate — pass size_hint_bytes.",
                    self.name, key[:40], size_b,
                    _SIZE_HINT_WARN_THRESHOLD_BYTES,
                )

        if size_b > self.max_bytes:
            self._refuse_oversized += 1
            self._emit_stat("refuse_oversized", key,
                            attempted_size_b=size_b)
            return

        now = time.monotonic()
        entry = _Entry(
            value=value,
            expires_at=now + ttl_seconds,
            swr_expires_at=now + ttl_seconds + swr_seconds,
            size_bytes=size_b,
        )

        with self._map_lock:
            # Replacing an existing key: refund old size first.
            old = self._entries.pop(key, None)
            if old is not None:
                self._current_bytes -= old.size_bytes

            # Evict LRU until this entry fits.
            while (self._current_bytes + size_b > self.max_bytes
                   and self._entries):
                evicted_key, evicted = self._entries.popitem(last=False)
                self._current_bytes -= evicted.size_bytes
                self._evictions += 1
                self._emit_stat("evict", evicted_key,
                                size_b=evicted.size_bytes)

            self._entries[key] = entry
            self._current_bytes += size_b
            if self._current_bytes > self._max_bytes_touched:
                self._max_bytes_touched = self._current_bytes

        self._emit_stat("miss", key, size_b=size_b, ms=compute_ms)

    def _spawn_swr_refresh(self, key: str, computer: Callable[[], Any],
                           ttl: float, swr: float,
                           size_hint_bytes: int | None) -> None:
        """Fire-and-forget background refresh. Serializes with the
        foreground single-flight lock — only one refresh per key at a
        time regardless of how many stale hits arrive."""
        def _refresh():
            lock = self._get_or_create_key_lock(key)
            # Don't queue up refreshes — if the lock is held, another
            # refresh (or foreground compute) is already running.
            if not lock.acquire(blocking=False):
                return
            try:
                start = time.monotonic()
                try:
                    value = computer()
                except Exception as e:
                    with self._map_lock:
                        entry = self._entries.get(key)
                        if entry is not None:
                            entry.refresh_fail_streak += 1
                            streak = entry.refresh_fail_streak
                        else:
                            streak = 1
                    self._refresh_fail += 1
                    self._emit_stat("refresh_fail", key,
                                    err=type(e).__name__,
                                    streak=streak)
                    return
                compute_ms = (time.monotonic() - start) * 1000.0
                self._store(key, value, ttl, swr,
                            size_hint_bytes, compute_ms)
                with self._map_lock:
                    entry = self._entries.get(key)
                    if entry is not None:
                        entry.refresh_fail_streak = 0
                self._refresh_ok += 1
                self._emit_stat("refresh_ok", key, ms=compute_ms)
            finally:
                lock.release()

        threading.Thread(target=_refresh, daemon=True,
                         name=f"swr-{self.name}").start()

    def _recount_bytes(self) -> None:
        """Full recount. Corrects drift from approximate per-entry sizing.
        Called on a cadence from stats(), not on every set()."""
        with self._map_lock:
            self._current_bytes = sum(
                e.size_bytes for e in self._entries.values()
            )

    def _emit_stat(self, event: str, key: str, **fields) -> None:
        """Structured log line for cache events. Prefix CACHE_STAT for
        grep. See Section 7.1 of the design pack for the event vocabulary."""
        payload = {
            "cache": self.name,
            "ev": event,
            "key_prefix": key[:40],
            "cur_b": self._current_bytes,
            "max_b": self.max_bytes,
            **fields,
        }
        try:
            log.info("CACHE_STAT %s", json.dumps(payload, default=str))
        except Exception:
            # Logging must never break the cache.
            pass
