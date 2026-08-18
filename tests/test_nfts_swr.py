"""Tests for /nfts SWR (stale-while-revalidate) cache behavior.

Sibling of the /whales SWR (app.py:2704-2719, 855-886) — same pattern,
single-slot key. Cases: fresh-hit (no rebuild), stale-serve (fast + one
rebuild), single-flight (N concurrent triggers spawn 1 thread), and
cold-miss still populates."""

import threading
import time
from unittest.mock import patch

import pytest

import app as app_module


@pytest.fixture(autouse=True)
def reset_nfts_cache():
    """Clear cache + rebuild state per test — app state is process-global."""
    app_module._NFTS_CACHE["body"] = None
    app_module._NFTS_CACHE["at"] = 0.0
    with app_module._NFTS_REBUILD_LOCK:
        app_module._NFTS_REBUILD_STATE["in_flight"] = False
    yield


def test_fresh_hit_returns_cached_no_rebuild(client):
    app_module._NFTS_CACHE["body"] = b"<cached-body-1>"
    app_module._NFTS_CACHE["at"] = time.monotonic()

    with patch.object(app_module, "_trigger_nfts_rebuild") as spy:
        r = client.get("/nfts")

    assert r.status_code == 200
    assert r.data == b"<cached-body-1>"
    spy.assert_not_called()


def test_stale_hit_serves_stale_fast_and_triggers_rebuild(client):
    """Stale body served immediately (<100ms) AND one background rebuild fires.
    Kills the 22.6s cold-render regression observed 2026-08-17."""
    app_module._NFTS_CACHE["body"] = b"<stale-body>"
    app_module._NFTS_CACHE["at"] = (
        time.monotonic() - app_module._NFTS_CACHE_TTL_S - 10
    )

    with patch.object(app_module, "_trigger_nfts_rebuild") as spy:
        t0 = time.perf_counter()
        r = client.get("/nfts")
        elapsed = time.perf_counter() - t0

    assert r.status_code == 200
    assert r.data == b"<stale-body>"
    spy.assert_called_once()
    assert elapsed < 0.1, f"stale serve took {elapsed:.3f}s (expected <0.1s)"


def test_trigger_is_single_flight_under_concurrent_calls():
    """N concurrent _trigger_nfts_rebuild() calls while a rebuild is in
    progress spawn ZERO additional threads."""
    with app_module._NFTS_REBUILD_LOCK:
        app_module._NFTS_REBUILD_STATE["in_flight"] = True

    spawned = []
    original = app_module.threading.Thread

    def counting_thread(*a, **kw):
        spawned.append(kw.get("name") or "unnamed")
        return original(*a, **kw)

    with patch.object(app_module.threading, "Thread", side_effect=counting_thread):
        for _ in range(10):
            app_module._trigger_nfts_rebuild()

    assert spawned == [], f"in-flight guard breached: {spawned}"


def test_cold_miss_populates_cache(client):
    """Miss path unchanged — synchronous render populates cache. Regression
    guard: SWR add must not break the cold-cache-first-visitor path."""
    assert app_module._NFTS_CACHE["body"] is None
    r = client.get("/nfts")
    assert r.status_code == 200
    assert app_module._NFTS_CACHE["body"] is not None
    assert app_module._NFTS_CACHE["at"] > 0
