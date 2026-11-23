"""The wallet render is capped so /healthz always answers.

2026-11-23 incident: one IP (UA "Android 10; K" Chrome/131) pulled ~8
/wallet/<address> pages in 35s. Each live render fans out over five
cascading branches (main/lp/escrow/offer/mpt), measured 1.15-5.68s against
Render's 5s health-check probe. Three workers went into that tail at once
and /healthz missed its probe twice (Sun 20:50, Mon 08:13).

Two defences, and only the second one actually held:
  * 60/min -> 4/min per IP. But the limiter is storage_uri="memory://", so
    the budget is PER WORKER: with 3 workers the real ceiling is ~12/min,
    and the scraper ran ~14/min. The rate limit alone would NOT have
    stopped it.
  * BoundedSemaphore(2) per worker around the LIVE render only, acquired
    non-blocking. Queueing is what starved /healthz, so a full slot set
    fails fast with 503 + Retry-After.

The pre-rendered PG fast path stays UNCAPPED: sub-ms, zero RPC, so capping
it would 503 cheap hits for no benefit.
"""

import threading
import time

import pytest

import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


ADDR = "rM7ffj9GZV41K8fWUhtpfZSZvYoZB2yA4t"


def test_semaphore_is_bounded_at_two():
    sem = app_module._WALLET_RENDER_SEM
    assert isinstance(sem, type(threading.BoundedSemaphore(1)))
    got = [sem.acquire(blocking=False) for _ in range(3)]
    for g in got:
        if g:
            sem.release()
    assert got[:2] == [True, True], got
    assert got[2] is False, "a third concurrent live render must be rejected"


def test_excess_live_render_gets_503_with_retry_after(client, monkeypatch):
    """Saturate both slots, then prove the next request sheds immediately."""
    monkeypatch.setattr(app_module, "_wallet_live_render",
                        lambda a: "should not be reached")
    # miss the PG fast path so we reach the semaphore
    monkeypatch.setattr(app_module.db, "read_wallet_summary",
                        lambda a: (None, None, None))
    sem = app_module._WALLET_RENDER_SEM
    assert sem.acquire(blocking=False)
    assert sem.acquire(blocking=False)
    try:
        t0 = time.perf_counter()
        resp = client.get(f"/wallet/{ADDR}")
        elapsed = time.perf_counter() - t0
    finally:
        sem.release()
        sem.release()
    assert resp.status_code == 503, resp.status_code
    assert resp.headers.get("Retry-After") == "5", dict(resp.headers)
    assert resp.headers.get("Cache-Control") == "no-store"
    assert elapsed < 1.0, f"reject path must fail FAST, took {elapsed:.3f}s"


def test_healthz_still_answers_while_wallet_renders_are_saturated(client):
    """The whole point. Both slots held, /healthz must still be fast."""
    sem = app_module._WALLET_RENDER_SEM
    assert sem.acquire(blocking=False)
    assert sem.acquire(blocking=False)
    try:
        t0 = time.perf_counter()
        resp = client.get("/healthz")
        elapsed = time.perf_counter() - t0
    finally:
        sem.release()
        sem.release()
    assert resp.status_code in (200, 503), resp.status_code
    assert elapsed < 5.0, f"/healthz took {elapsed:.3f}s with wallets saturated"


def test_the_pg_fast_path_is_not_capped(client, monkeypatch):
    """A fresh pre-rendered body must serve even with both slots held."""
    monkeypatch.setattr(app_module.db, "read_wallet_summary",
                        lambda a: ("<p>cached</p>", 60, 12))
    sem = app_module._WALLET_RENDER_SEM
    assert sem.acquire(blocking=False)
    assert sem.acquire(blocking=False)
    try:
        resp = client.get(f"/wallet/{ADDR}")
    finally:
        sem.release()
        sem.release()
    assert resp.status_code == 200, resp.status_code
    assert "cached" in resp.get_data(as_text=True)
    assert "X-Wallet-Cache" in resp.headers


def test_route_declares_four_per_minute():
    """Pinned: the decorator is the only thing standing between one IP and
    a worker's whole thread pool, so a silent loosening must go red."""
    import inspect
    src = inspect.getsource(app_module)
    assert '@limiter.limit("4 per minute")\ndef wallet(address):' in src
