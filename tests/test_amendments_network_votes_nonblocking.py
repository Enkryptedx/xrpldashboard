"""amendments_network_votes: fetch outside the lock (Charlie 2026-10-08).

Before: the data.xrpl.org round-trip (10 s timeout) ran INSIDE _lock, so
when the 300 s TTL lapsed every concurrent /amendments request queued
behind it — 14.6 s page at 14:54 ET with no other load. Now the lock only
guards the cache; with last-good data a single background thread
refreshes while callers are answered immediately; a failed fetch never
overwrites the last good data.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import patch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import amendments_network_votes as anv  # noqa: E402
from tests.test_amendments_network_votes import VHS_HAPPY as VHS  # noqa: E402  (real VHS shape)


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
    def json(self):
        return self._p


def setup_function(_):
    anv._reset_cache_for_tests()
    os.environ["NETWORK_VOTES_ENABLED"] = "1"


def _prime():
    with patch("amendments_network_votes.httpx.get", return_value=_Resp(200, VHS)):
        env = anv.fetch_network_vote_tallies_cached()
    assert env["status"] == "ok" and env["data"]
    return env


def test_expired_ttl_serves_last_good_immediately_while_slow_upstream():
    first = _prime()
    gate = threading.Event()

    def slow_get(*a, **k):
        gate.wait(timeout=10)          # simulate a 10 s upstream
        return _Resp(200, VHS)

    later = time.monotonic() + anv.CACHE_TTL + 1
    with patch("amendments_network_votes._now_monotonic", return_value=later), \
         patch("amendments_network_votes.httpx.get", side_effect=slow_get):
        t0 = time.perf_counter()
        envs = [anv.fetch_network_vote_tallies_cached() for _ in range(20)]
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"callers blocked on upstream for {elapsed:.2f}s"
        assert all(e["status"] == "ok" and e["data"] == first["data"] for e in envs)
        assert anv._refresh["in_flight"] is True
        gate.set()
        anv._join_refresh_for_tests()
    assert anv._refresh["in_flight"] is False


def test_only_one_background_fetch_per_expiry():
    _prime()
    calls = {"n": 0}
    gate = threading.Event()

    def slow_get(*a, **k):
        calls["n"] += 1
        gate.wait(timeout=10)
        return _Resp(200, VHS)

    later = time.monotonic() + anv.CACHE_TTL + 1
    with patch("amendments_network_votes._now_monotonic", return_value=later), \
         patch("amendments_network_votes.httpx.get", side_effect=slow_get):
        for _ in range(25):
            anv.fetch_network_vote_tallies_cached()
        gate.set()
        anv._join_refresh_for_tests()
    assert calls["n"] == 1


def test_failure_never_overwrites_last_good():
    first = _prime()
    later = time.monotonic() + anv.CACHE_TTL + 1
    with patch("amendments_network_votes._now_monotonic", return_value=later), \
         patch("amendments_network_votes.httpx.get", side_effect=Exception("500")):
        anv.fetch_network_vote_tallies_cached()
        anv._join_refresh_for_tests()
        env = anv.fetch_network_vote_tallies_cached()
        anv._join_refresh_for_tests()
    assert env["status"] == "stale"
    assert env["data"] == first["data"]
    assert env["as_of_iso"] == first["as_of_iso"]
    assert anv._cache["last_success_data"] == first["data"]
    # a later success replaces it and returns to "ok"
    with patch("amendments_network_votes._now_monotonic", return_value=later + 1), \
         patch("amendments_network_votes.httpx.get", return_value=_Resp(200, VHS)):
        anv.fetch_network_vote_tallies_cached()
        anv._join_refresh_for_tests()
        env2 = anv.fetch_network_vote_tallies_cached()
    assert env2["status"] == "ok"


def test_non_200_is_not_cached():
    first = _prime()
    later = time.monotonic() + anv.CACHE_TTL + 1
    with patch("amendments_network_votes._now_monotonic", return_value=later), \
         patch("amendments_network_votes.httpx.get", return_value=_Resp(503, {})):
        anv.fetch_network_vote_tallies_cached()
        anv._join_refresh_for_tests()
    assert anv._cache["last_success_data"] == first["data"]


def test_cold_cache_fetches_once_and_concurrent_cold_callers_share_it():
    calls = {"n": 0}
    gate = threading.Event()

    def slow_get(*a, **k):
        calls["n"] += 1
        gate.wait(timeout=10)
        return _Resp(200, VHS)

    results = []
    with patch("amendments_network_votes.httpx.get", side_effect=slow_get):
        ths = [threading.Thread(target=lambda: results.append(anv.fetch_network_vote_tallies_cached()))
               for _ in range(5)]
        for t in ths:
            t.start()
        time.sleep(0.2)
        assert anv._lock.acquire(timeout=0.5), "lock must not be held during the fetch"
        anv._lock.release()
        gate.set()
        for t in ths:
            t.join(timeout=5)
    assert calls["n"] == 1
    assert len(results) == 5 and all(r["status"] == "ok" and r["data"] for r in results)


def test_lock_is_never_held_across_http():
    _prime()
    seen = {}

    def probe_get(*a, **k):
        # If the lock were held across this HTTP call (the old design) we
        # could never acquire it here. A brief wait tolerates the caller
        # thread still exiting its own `with _lock:` block.
        got = anv._lock.acquire(timeout=0.5)
        seen["lock_free_during_fetch"] = got
        if got:
            anv._lock.release()
        return _Resp(200, VHS)

    later = time.monotonic() + anv.CACHE_TTL + 1
    with patch("amendments_network_votes._now_monotonic", return_value=later), \
         patch("amendments_network_votes.httpx.get", side_effect=probe_get):
        anv.fetch_network_vote_tallies_cached()
        anv._join_refresh_for_tests()
    assert seen["lock_free_during_fetch"] is True


# ------------------------------------------------- outer amendments_state cache
import amendments_state  # noqa: E402


def _reset_state():
    amendments_state._join_refresh_for_tests()
    with amendments_state._cache_lock:
        amendments_state._cache["fetched_at"] = 0.0
        amendments_state._cache["data"] = None
        amendments_state._refresh["in_flight"] = False
        amendments_state._refresh["thread"] = None
        amendments_state._refresh["done"].set()


def test_state_cache_expired_serves_last_good_while_slow_rebuild(monkeypatch):
    _reset_state()
    gate = threading.Event()
    calls = {"n": 0}

    def fake_state():
        calls["n"] += 1
        if calls["n"] > 1:
            gate.wait(timeout=10)       # the rebuild is slow (node + votes = up to 10 s)
        return {"ok": True, "sourcing": "sovereign", "n": calls["n"]}
    monkeypatch.setattr(amendments_state, "fetch_amendments_state", fake_state)
    first = amendments_state.fetch_amendments_state_cached(ttl=300)
    assert first["n"] == 1
    with amendments_state._cache_lock:
        amendments_state._cache["fetched_at"] -= 400    # past TTL, inside the 900 s cap
    t0 = time.perf_counter()
    outs = [amendments_state.fetch_amendments_state_cached(ttl=300) for _ in range(20)]
    assert time.perf_counter() - t0 < 0.5
    assert all(o["n"] == 1 for o in outs)                # last good, immediately
    assert calls["n"] == 2                                # exactly one background rebuild
    assert amendments_state._cache_lock.acquire(timeout=0.5); amendments_state._cache_lock.release()
    gate.set()
    amendments_state._join_refresh_for_tests()
    assert amendments_state.fetch_amendments_state_cached(ttl=300)["n"] == 2
    _reset_state()


def test_state_cache_failed_rebuild_keeps_last_good(monkeypatch):
    _reset_state()
    import itertools
    seq = itertools.chain([{"ok": True, "sourcing": "sovereign", "v": 1}],
                          itertools.repeat({"ok": False, "sourcing": "fallback"}))
    monkeypatch.setattr(amendments_state, "fetch_amendments_state", lambda: next(seq))
    assert amendments_state.fetch_amendments_state_cached(ttl=300)["v"] == 1
    with amendments_state._cache_lock:
        amendments_state._cache["fetched_at"] -= 400    # past TTL, inside the cap
    amendments_state.fetch_amendments_state_cached(ttl=300)
    amendments_state._join_refresh_for_tests()
    out = amendments_state.fetch_amendments_state_cached(ttl=300)
    assert out["ok"] is True and out["v"] == 1              # failure not cached
    _reset_state()


def test_state_cache_cold_concurrent_callers_share_one_fetch(monkeypatch):
    _reset_state()
    gate = threading.Event(); calls = {"n": 0}

    def slow():
        calls["n"] += 1; gate.wait(timeout=10); return {"ok": True, "sourcing": "sovereign"}
    monkeypatch.setattr(amendments_state, "fetch_amendments_state", slow)
    res = []
    ths = [threading.Thread(target=lambda: res.append(amendments_state.fetch_amendments_state_cached(ttl=300))) for _ in range(5)]
    [t.start() for t in ths]; time.sleep(0.2)
    assert amendments_state._cache_lock.acquire(timeout=0.5); amendments_state._cache_lock.release()
    gate.set(); [t.join(timeout=5) for t in ths]
    assert calls["n"] == 1 and len(res) == 5 and all(r["ok"] for r in res)
    _reset_state()


# ------------------------------------------- 15-minute staleness cap (state only)

def _age_state(seconds):
    with amendments_state._cache_lock:
        amendments_state._cache["fetched_at"] -= seconds


def test_stale_max_default_is_900s_from_env():
    assert amendments_state.STALE_MAX_SECONDS == 900
    assert os.environ.get("AMENDMENTS_STALE_MAX_SECONDS") in (None, "900")


def test_state_under_cap_serves_last_good_and_refreshes_in_background(monkeypatch):
    _reset_state()
    gate = threading.Event(); calls = {"n": 0}

    def fake_state():
        calls["n"] += 1
        if calls["n"] > 1:
            gate.wait(timeout=10)
        return {"ok": True, "sourcing": "sovereign", "n": calls["n"]}
    monkeypatch.setattr(amendments_state, "fetch_amendments_state", fake_state)
    amendments_state.fetch_amendments_state_cached(ttl=300)
    _age_state(899)                                   # one second inside the cap
    t0 = time.perf_counter()
    out = amendments_state.fetch_amendments_state_cached(ttl=300)
    assert time.perf_counter() - t0 < 0.5
    assert out["n"] == 1 and out["cached_age_seconds"] >= 899
    assert amendments_state._refresh["in_flight"] is True
    gate.set(); amendments_state._join_refresh_for_tests()
    assert amendments_state.fetch_amendments_state_cached(ttl=300)["n"] == 2
    _reset_state()


def test_state_past_cap_rebuilds_synchronously_and_never_serves_old_data(monkeypatch):
    _reset_state()
    calls = {"n": 0}

    def fake_state():
        calls["n"] += 1
        time.sleep(0.2)                               # a visibly slow rebuild
        return {"ok": True, "sourcing": "sovereign", "n": calls["n"]}
    monkeypatch.setattr(amendments_state, "fetch_amendments_state", fake_state)
    amendments_state.fetch_amendments_state_cached(ttl=300)
    _age_state(901)                                   # past the cap
    t0 = time.perf_counter()
    out = amendments_state.fetch_amendments_state_cached(ttl=300)
    assert time.perf_counter() - t0 >= 0.2            # waited for the rebuild (cold path)
    assert out["n"] == 2 and out["cached_age_seconds"] == 0.0
    assert calls["n"] == 2
    assert amendments_state._refresh["in_flight"] is False
    assert amendments_state._cache_lock.acquire(timeout=0.5); amendments_state._cache_lock.release()
    _reset_state()


def test_state_past_cap_failed_rebuild_reports_unavailable_not_old_data(monkeypatch):
    _reset_state()
    seq = iter([{"ok": True, "sourcing": "sovereign", "v": 1},
                {"ok": False, "sourcing": "fallback"}])
    monkeypatch.setattr(amendments_state, "fetch_amendments_state", lambda: next(seq))
    assert amendments_state.fetch_amendments_state_cached(ttl=300)["v"] == 1
    _age_state(5000)
    out = amendments_state.fetch_amendments_state_cached(ttl=300)
    assert out["ok"] is False and "v" not in out      # >cap data is never resurrected
    assert amendments_state._cache["data"]["v"] == 1  # ...but the failure did not clobber the dict
    _reset_state()


def test_state_past_cap_concurrent_callers_coalesce_on_one_rebuild(monkeypatch):
    _reset_state()
    gate = threading.Event(); calls = {"n": 0}

    def fake_state():
        calls["n"] += 1
        if calls["n"] > 1:
            gate.wait(timeout=10)
        return {"ok": True, "sourcing": "sovereign", "n": calls["n"]}
    monkeypatch.setattr(amendments_state, "fetch_amendments_state", fake_state)
    amendments_state.fetch_amendments_state_cached(ttl=300)
    _age_state(2000)
    res = []
    ths = [threading.Thread(target=lambda: res.append(amendments_state.fetch_amendments_state_cached(ttl=300)))
           for _ in range(6)]
    [t.start() for t in ths]; time.sleep(0.2)
    assert res == []                                  # nobody was handed the >cap state
    assert amendments_state._cache_lock.acquire(timeout=0.5); amendments_state._cache_lock.release()
    gate.set(); [t.join(timeout=5) for t in ths]
    assert calls["n"] == 2                            # prime + exactly one shared rebuild
    assert len(res) == 6 and all(r["ok"] and r["n"] == 2 for r in res)
    _reset_state()


def test_state_crossing_cap_while_background_refresh_runs_joins_it(monkeypatch):
    """Age < cap started a background refresh; a caller arriving after the
    cap must wait for that same refresh (not be served the old state, not
    start a second fetch)."""
    _reset_state()
    gate = threading.Event(); calls = {"n": 0}

    def fake_state():
        calls["n"] += 1
        if calls["n"] > 1:
            gate.wait(timeout=10)
        return {"ok": True, "sourcing": "sovereign", "n": calls["n"]}
    monkeypatch.setattr(amendments_state, "fetch_amendments_state", fake_state)
    amendments_state.fetch_amendments_state_cached(ttl=300)
    _age_state(400)
    assert amendments_state.fetch_amendments_state_cached(ttl=300)["n"] == 1   # bg refresh started
    assert amendments_state._refresh["in_flight"] is True
    _age_state(600)                                   # now 1000 s old: past the cap
    res = []
    t = threading.Thread(target=lambda: res.append(amendments_state.fetch_amendments_state_cached(ttl=300)))
    t.start(); time.sleep(0.2)
    assert res == []                                  # waiting, not served the old state
    gate.set(); t.join(timeout=5); amendments_state._join_refresh_for_tests()
    assert calls["n"] == 2 and res[0]["n"] == 2
    _reset_state()


def test_votes_cache_untouched_by_state_cap():
    """The 15-min cap is on the STATE cache only. Votes keep their own 6 h
    ceiling and "stale" label (amendments_network_votes)."""
    first = _prime()
    later = time.monotonic() + 3600                   # 1 h old: far past 900 s, inside 6 h
    with patch("amendments_network_votes._now_monotonic", return_value=later), \
         patch("amendments_network_votes.httpx.get", side_effect=Exception("down")):
        anv.fetch_network_vote_tallies_cached()
        anv._join_refresh_for_tests()
        env = anv.fetch_network_vote_tallies_cached()
        anv._join_refresh_for_tests()
    assert env["status"] == "stale" and env["data"] == first["data"]
    assert not hasattr(anv, "STALE_MAX_SECONDS")
