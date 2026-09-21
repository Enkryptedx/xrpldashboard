"""Session-scoped scraper block tracker tests (Charlie ruling
2026-09-21 Mon PM, item 5). Prove:

1. **Brazil poller shape** — one hash, 100+ hits, single path,
   within window → 429.
2. **AionBot shape** — one hash, 100+ hits, ≤2 paths → 429 (already
   handled by the fleet_signature block but the session tracker
   must catch it too, in case an AionBot variant strips the
   declaring UA).
3. **Real browser passes** — a session that fetches CSS/JS marks
   itself real; subsequent bare-path hits do not trip.
4. **Distinct-paths escape hatch** — a hash that visits >2 paths
   stays clean regardless of hit count (a real reader).
5. **Window trim** — hits older than WINDOW_SECONDS drop out; a
   scraper that pauses for 2h+ resets.
6. **Static-asset paths trigger the exemption**.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_tracker():
    from session_scraper_tracker import SessionScraperTracker
    return SessionScraperTracker()


def test_brazil_poller_shape_trips_block():
    """Sunday BR's `00d19bfc79` shape: 168 hits, all on `/`."""
    tr = _fresh_tracker()
    now = 1_800_000_000.0
    tripped = False
    for i in range(150):
        if tr.observe("brazil_hash", "/", now=now + i):
            tripped = True
            break
    assert tripped, "BR poller shape should trip inside 100 hits"


def test_aion_bot_shape_two_paths_trips_block():
    """AionBot pings /check + /check.json — 2 distinct paths, high
    volume. Threshold is ≤2 paths so exactly-2 still trips."""
    tr = _fresh_tracker()
    now = 1_800_000_000.0
    tripped = False
    for i in range(200):
        path = "/check" if i % 2 == 0 else "/check.json"
        if tr.observe("aion_hash", path, now=now + i * 3):
            tripped = True
            break
    assert tripped, "AionBot two-path shape should trip"


def test_real_browser_static_fetch_exempt():
    """A session that fetched a static asset stays exempt even under
    heavy /token/... polling — real users load CSS/JS on first paint."""
    tr = _fresh_tracker()
    now = 1_800_000_000.0
    tr.observe("browser_hash", "/static/vendor/geist.css", now=now)
    tripped = False
    for i in range(200):
        if tr.observe("browser_hash", "/token/524C555344000000000000000000000000000000/rLUSDtykL2NVz3HJe1Jqoc7dsxWFVcsmuK",
                      now=now + 1 + i):
            tripped = True
            break
    assert not tripped, (
        "Real-browser session (fetched CSS) must be exempt from the "
        "single-path threshold"
    )


def test_distinct_paths_over_threshold_stays_clean():
    """A reader who visits 3+ pages doesn't match the shape even at
    high volume."""
    tr = _fresh_tracker()
    now = 1_800_000_000.0
    paths = ["/", "/tokens", "/whales", "/amendments"]
    tripped = False
    for i in range(200):
        if tr.observe("reader_hash", paths[i % 4], now=now + i):
            tripped = True
            break
    assert not tripped, "Multi-path session should stay clean"


def test_window_trim_lets_hash_recover_after_pause():
    """A scraper that pauses ≥ WINDOW_SECONDS should reset — old hits
    fall out of the window and the counter starts fresh."""
    from session_scraper_tracker import WINDOW_SECONDS
    tr = _fresh_tracker()
    t0 = 1_800_000_000.0
    # First burst — trips
    for i in range(150):
        tripped = tr.observe("pauser_hash", "/", now=t0 + i)
        if tripped:
            break
    # Manually clear ban (this test is about window trim, not ban duration)
    tr._states["pauser_hash"].banned_until = 0.0
    # Wait past window
    t1 = t0 + WINDOW_SECONDS + 10
    # A single fresh hit should NOT re-trip immediately
    tripped_fresh = tr.observe("pauser_hash", "/", now=t1)
    assert not tripped_fresh, "single hit after window pause should not trip"


def test_static_suffix_and_prefix_detection():
    """Both /static/… prefix and .css/.js/etc. suffixes count as static."""
    from session_scraper_tracker import _is_static_path
    assert _is_static_path("/static/vendor/foo.css")
    assert _is_static_path("/favicon-32.png")
    assert _is_static_path("/robots.txt")
    assert _is_static_path("/tokens.js")
    assert _is_static_path("/style.css")
    assert not _is_static_path("/tokens")
    assert not _is_static_path("/check.json")
    assert not _is_static_path("/")


def test_empty_visitor_hash_never_trips():
    """No hash = no session state. Never trip."""
    tr = _fresh_tracker()
    now = 1_800_000_000.0
    for i in range(300):
        assert not tr.observe("", "/", now=now + i)
    assert not tr.observe(None, "/", now=now + 1000)


if __name__ == "__main__":
    test_brazil_poller_shape_trips_block()
    test_aion_bot_shape_two_paths_trips_block()
    test_real_browser_static_fetch_exempt()
    test_distinct_paths_over_threshold_stays_clean()
    test_window_trim_lets_hash_recover_after_pause()
    test_static_suffix_and_prefix_detection()
    test_empty_visitor_hash_never_trips()
    print("ALL PASS")
