"""/token/<cur>/<iss> route guard (Charlie 2026-10-08). Hermetic.

Incident: a crawler walking the RLUSD-impostor issuer long tail at 40+/min
(every hit a cache miss: ~5 Neon reads + live amm_info with a 20 s timeout
x3 tunnel tries + public fallback = up to 80 s) pinned all 24 gunicorn
threads at 14:49 ET; Render's /healthz probe failed.

Guard: (1) per-worker cap of TOKEN_RENDER_MAX_CONCURRENT in-flight
renders, NON-blocking acquire -> immediate 503 + Retry-After when full,
slot released in finally; (2) amm_info from the web path bounded to 5 s
per call, 1 tunnel attempt.
"""
from __future__ import annotations

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import app as app_mod  # noqa: E402
import token_data  # noqa: E402
import sovereign_tunnel_client as stc  # noqa: E402

ISS = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
CUR = "524C555344000000000000000000000000000000"


def _patch_render(monkeypatch, hold_s=0.0, started=None, release=None):
    """Replace the real render with a stub that optionally blocks until
    `release` is set, so tests can hold N slots deterministically."""
    def fake(currency, issuer):
        if started is not None:
            started.release()
        if release is not None:
            release.wait(timeout=10)
        elif hold_s:
            time.sleep(hold_s)
        return f"ok {currency} {issuer}", 200
    monkeypatch.setattr(app_mod, "_token_detail_render", fake)


def _fresh_semaphore(monkeypatch, n):
    monkeypatch.setattr(app_mod, "TOKEN_RENDER_MAX_CONCURRENT", n)
    monkeypatch.setattr(app_mod, "_token_render_slots", threading.BoundedSemaphore(n))
    monkeypatch.setattr(app_mod, "_token_render_rejects", {"n": 0})


def test_under_cap_serves_200(monkeypatch):
    _fresh_semaphore(monkeypatch, 3)
    _patch_render(monkeypatch)
    c = app_mod.app.test_client()
    r = c.get(f"/token/{CUR}/{ISS}")
    assert r.status_code == 200 and b"ok" in r.data
    # slot released afterwards
    assert app_mod._token_render_slots._value == 3


def test_over_cap_returns_503_immediately_with_retry_after(monkeypatch):
    n = 2
    _fresh_semaphore(monkeypatch, n)
    started = threading.Semaphore(0)
    release = threading.Event()
    _patch_render(monkeypatch, started=started, release=release)
    c = app_mod.app.test_client()
    holders = [threading.Thread(target=lambda: c.get(f"/token/{CUR}/{ISS}")) for _ in range(n)]
    for t in holders:
        t.start()
    for _ in range(n):
        assert started.acquire(timeout=5), "render threads did not start"
    # all slots busy -> the next request must NOT wait
    t0 = time.perf_counter()
    r = app_mod.app.test_client().get(f"/token/{CUR}/{ISS}")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 503
    assert elapsed < 0.5, f"503 path blocked for {elapsed:.2f}s"
    assert r.headers["Retry-After"] == str(app_mod.TOKEN_RENDER_RETRY_AFTER_S)
    assert r.headers["Cache-Control"] == "no-store"
    assert b"Busy" in r.data
    assert app_mod._token_render_rejects["n"] == 1
    release.set()
    for t in holders:
        t.join(timeout=5)
    # slots fully returned after the holders finish
    assert app_mod._token_render_slots._value == n
    # and a request now succeeds again
    assert app_mod.app.test_client().get(f"/token/{CUR}/{ISS}").status_code == 200


def test_slot_released_when_render_raises(monkeypatch):
    _fresh_semaphore(monkeypatch, 1)

    def boom(currency, issuer):
        raise RuntimeError("render failed")
    monkeypatch.setattr(app_mod, "_token_detail_render", boom)
    app_mod.app.config["PROPAGATE_EXCEPTIONS"] = False
    try:
        r = app_mod.app.test_client().get(f"/token/{CUR}/{ISS}")
        assert r.status_code == 500
    finally:
        app_mod.app.config["PROPAGATE_EXCEPTIONS"] = None
    assert app_mod._token_render_slots._value == 1


def test_invalid_issuer_404_does_not_leak_slot(monkeypatch):
    _fresh_semaphore(monkeypatch, 1)
    r = app_mod.app.test_client().get("/token/USD/not-an-address")
    assert r.status_code == 404
    assert app_mod._token_render_slots._value == 1


def test_healthz_unaffected_while_token_slots_full(monkeypatch):
    _fresh_semaphore(monkeypatch, 1)
    started = threading.Semaphore(0)
    release = threading.Event()
    _patch_render(monkeypatch, started=started, release=release)
    c = app_mod.app.test_client()
    t = threading.Thread(target=lambda: c.get(f"/token/{CUR}/{ISS}"))
    t.start()
    assert started.acquire(timeout=5)
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "pg_ping", lambda *a, **k: True, raising=False)
    t0 = time.perf_counter()
    r = app_mod.app.test_client().get("/healthz")
    assert time.perf_counter() - t0 < 2
    assert r.status_code in (200, 503)  # routing probe answers; not queued
    release.set()
    t.join(timeout=5)


# ------------------------------------------------------------- amm_info bound
def test_web_path_amm_info_uses_5s_timeout_and_one_attempt(monkeypatch):
    captured = {}

    class FakeFetcher:
        def __init__(self, public_url, walker_name="unknown", fallback_sink=None,
                     timeout=None, max_attempts=None):
            captured.update(timeout=timeout, max_attempts=max_attempts, walker=walker_name)
        def call(self, method, params):
            captured["method"] = method
            return {"amm": {"amount": "1000000", "amount2": {"currency": CUR, "issuer": ISS, "value": "5"}}}

    monkeypatch.setattr(stc, "SovereignFetcher", FakeFetcher)
    token_data._amm_reserves_cache.clear()
    out = token_data._amm_reserves_cached("rAMMaccount000000000000000000000000")
    assert captured["method"] == "amm_info"
    assert captured["timeout"] == 5.0 == token_data.AMM_INFO_WEB_TIMEOUT_S
    assert captured["max_attempts"] == 1
    assert out and out["xrp"] == 1.0


def test_fetcher_timeout_is_passed_to_httpx(monkeypatch):
    seen = []

    class FakeResp:
        status_code = 200
        def json(self): return {"result": {"ok": True}}

    class FakeClient:
        def post(self, url, **kw):
            seen.append(kw.get("timeout", "default"))
            return FakeResp()

    monkeypatch.setattr(stc, "_client_for", lambda url: FakeClient())
    monkeypatch.setattr(stc, "TUNNEL_CONFIGURED", True)
    monkeypatch.setattr(stc, "SOVEREIGN_NODE", "http://tunnel.test")
    f = stc.SovereignFetcher("http://public.test", "t", timeout=5.0, max_attempts=1)
    f._tunnel_headers = {}
    assert f._try_tunnel({"method": "amm_info"}) == ({"ok": True}, None)
    assert seen == [5.0]
    # defaults untouched for walkers
    g = stc.SovereignFetcher("http://public.test", "walker")
    g._tunnel_headers = {}
    seen.clear(); g._try_tunnel({"method": "x"})
    assert seen == ["default"] and g.max_attempts == stc.TUNNEL_RETRY_ATTEMPTS


def test_fetcher_one_attempt_then_public(monkeypatch):
    calls = []

    class Boom:
        def post(self, url, **kw):
            calls.append(url)
            if "tunnel" in url:
                raise TimeoutError("slow")
            class R:
                status_code = 200
                def json(self): return {"result": {"from": "public"}}
            return R()

    monkeypatch.setattr(stc, "_client_for", lambda url: Boom())
    monkeypatch.setattr(stc, "TUNNEL_CONFIGURED", True)
    monkeypatch.setattr(stc, "SOVEREIGN_NODE", "http://tunnel.test")
    monkeypatch.setattr(stc.time, "sleep", lambda s: None)
    f = stc.SovereignFetcher("http://public.test", "t", timeout=5.0, max_attempts=1,
                             fallback_sink=lambda *a: None)
    f._tunnel_headers = {}
    assert f.call("amm_info", {}) == {"from": "public"}
    assert calls == ["http://tunnel.test", "http://public.test"]  # exactly 1 tunnel try
