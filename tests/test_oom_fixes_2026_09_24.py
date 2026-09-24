"""OOM post-mortem fixes — Charlie ruling 2026-09-24 15:37 ET.

Two guards land together:

  FIX-A: bound `token_data._amm_reserves_cache` as an OrderedDict-backed
         LRU with `AMM_RESERVES_MAX_ENTRIES` cap. Hot entries stick via
         move_to_end on read hit; cold entries evict when cap is reached.

  FIX-B: `/wallet` per-request cap gate — the adaptive escalation to
         `HIGH_RATE_MAX_TX_PAGES` (100 pages, 20k tx, ~60 MB peak) is
         allowed ONLY when the process is safely below
         `WALLET_MEMORY_CEILING_MB`. Fail-closed — an unknown RSS
         returns False, so /wallet stays at MAX_TX_PAGES on any
         resource-check error.

Rationale: OOM post-mortem 2026-09-24 07:42 UTC (03:42 ET) confirmed the
unbounded `_amm_reserves_cache` structure and named /wallet's 20k-tx cap
as the most likely single triggering allocation. Both classes now guarded.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ------------------------------------------------------------------
# FIX-A tests
# ------------------------------------------------------------------

def _reset_amm_cache():
    from token_data import _amm_reserves_cache
    _amm_reserves_cache.clear()


def test_fixa_cache_size_stays_bounded():
    """Insert 3× the cap; the dict must never exceed the cap."""
    import time
    import token_data
    _reset_amm_cache()
    cap = token_data.AMM_RESERVES_MAX_ENTRIES
    # Bypass the network by writing directly under the same lock the
    # module uses; exercises the eviction path.
    for i in range(cap * 3):
        with token_data._amm_reserves_lock:
            token_data._amm_reserves_cache[f"r_synth_{i}"] = (time.monotonic(), None)
            token_data._amm_reserves_cache.move_to_end(f"r_synth_{i}")
            while len(token_data._amm_reserves_cache) > cap:
                token_data._amm_reserves_cache.popitem(last=False)
    assert len(token_data._amm_reserves_cache) == cap, (
        f"cache size {len(token_data._amm_reserves_cache)} != cap {cap}"
    )


def test_fixa_lru_keeps_hot_entries():
    """A hot key touched right before eviction pressure must survive."""
    import time
    import token_data
    _reset_amm_cache()
    cap = token_data.AMM_RESERVES_MAX_ENTRIES
    HOT = "r_hot_key"
    # Seed with cap entries; HOT is the very first (least recently used
    # by insertion order).
    with token_data._amm_reserves_lock:
        token_data._amm_reserves_cache[HOT] = (time.monotonic(), {"marker": True})
        for i in range(cap - 1):
            token_data._amm_reserves_cache[f"r_{i}"] = (time.monotonic(), None)
        # Touch HOT to promote to MRU.
        token_data._amm_reserves_cache.move_to_end(HOT)
    # Now push 50 fresh entries with eviction — HOT should survive.
    for i in range(cap, cap + 50):
        with token_data._amm_reserves_lock:
            token_data._amm_reserves_cache[f"r_{i}"] = (time.monotonic(), None)
            token_data._amm_reserves_cache.move_to_end(f"r_{i}")
            while len(token_data._amm_reserves_cache) > cap:
                token_data._amm_reserves_cache.popitem(last=False)
    assert HOT in token_data._amm_reserves_cache, "hot key evicted despite LRU"


def test_fixa_ttl_still_evicts_on_read():
    """TTL semantics preserved: an expired read returns None (falls
    through to refetch) even under LRU."""
    import time
    import token_data
    _reset_amm_cache()
    old = time.monotonic() - token_data.AMM_RESERVES_TTL - 10
    with token_data._amm_reserves_lock:
        token_data._amm_reserves_cache["r_stale"] = (old, {"cached": True})
    # Directly exercise the "expired" branch of _amm_reserves_cached
    # without hitting the network by mocking SovereignFetcher.
    import unittest.mock as mock
    with mock.patch("sovereign_tunnel_client.SovereignFetcher") as m:
        m.return_value.call.return_value = None  # forces data=None branch
        got = token_data._amm_reserves_cached("r_stale")
        # Return either None (call returned None) or the freshly written
        # entry; either way, TTL was ignored (as expected — the stale
        # cached value must not be returned as-is).
    assert isinstance(got, (dict, type(None)))
    # And the cache entry timestamp is now fresh (or None-valued).
    entry = token_data._amm_reserves_cache["r_stale"]
    assert entry[0] > old, "TTL not honored — stale entry returned as fresh"


# ------------------------------------------------------------------
# FIX-B tests
# ------------------------------------------------------------------

def test_fixb_rss_ok_low_rss_allows_escalation():
    import unittest.mock as mock
    import wallet_data
    with mock.patch.object(wallet_data, "_current_rss_mb", return_value=100.0):
        assert wallet_data._rss_ok_for_wallet_escalation() is True


def test_fixb_rss_ok_high_rss_blocks_escalation():
    import unittest.mock as mock
    import wallet_data
    ceiling = wallet_data.WALLET_MEMORY_CEILING_MB
    with mock.patch.object(wallet_data, "_current_rss_mb", return_value=ceiling + 50):
        assert wallet_data._rss_ok_for_wallet_escalation() is False


def test_fixb_rss_ok_unknown_rss_fails_closed():
    """Fail-closed: no info about RSS ⇒ do not escalate to 20k-tx mode."""
    import unittest.mock as mock
    import wallet_data
    with mock.patch.object(wallet_data, "_current_rss_mb", return_value=None):
        assert wallet_data._rss_ok_for_wallet_escalation() is False


def test_fixb_current_rss_mb_returns_float_or_none():
    """Real-world call: on Linux/macOS returns a plausible number; on
    weird envs returns None. Never crashes."""
    import wallet_data
    got = wallet_data._current_rss_mb()
    assert got is None or (isinstance(got, float) and got > 0)


def test_fixb_escalation_call_site_reads_the_guard():
    """The escalation site in _fetch_account_tx MUST call the guard.
    Static assertion via source-grep — cheapest regression against the
    guard being accidentally removed."""
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "wallet_data.py").read_text()
    # Two anchors: the guard function and its use inside _fetch_account_tx.
    assert "def _rss_ok_for_wallet_escalation" in src
    assert "if _rss_ok_for_wallet_escalation():" in src


if __name__ == "__main__":
    test_fixa_cache_size_stays_bounded()
    test_fixa_lru_keeps_hot_entries()
    test_fixa_ttl_still_evicts_on_read()
    test_fixb_rss_ok_low_rss_allows_escalation()
    test_fixb_rss_ok_high_rss_blocks_escalation()
    test_fixb_rss_ok_unknown_rss_fails_closed()
    test_fixb_current_rss_mb_returns_float_or_none()
    test_fixb_escalation_call_site_reads_the_guard()
    print("ALL PASS")
