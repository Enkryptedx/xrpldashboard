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


def test_render_healthz_probe_exempt():
    """Render's HTTP health check hits /healthz continuously from a
    stable source-hash on ONE path. That's the exact shape the tracker
    catches — must be exempted BEFORE state is written, or the
    Render instance takes itself down with 429s. Triage 2026-09-21
    afternoon after Render alert."""
    tr = _fresh_tracker()
    now = 1_800_000_000.0
    tripped = False
    for i in range(300):
        # UA + path shape typical of Render's HTTP health check
        if tr.observe("render_health_hash", "/healthz", now=now + i,
                      user_agent="Go-http-client/1.1"):
            tripped = True
            break
    assert not tripped, (
        "/healthz probes must never trip the tracker regardless of "
        "hit count — Render's health check would take the instance "
        "down with 429s"
    )
    # The exemption is short-circuit: nothing recorded, no state at all
    snap = tr._snapshot("render_health_hash")
    assert snap == {}, (
        f"monitor probes must not write session state; got {snap!r}"
    )


def test_betterstack_uptime_probe_exempt():
    """BetterStack Uptime polls at cadence; UA fragment 'BetterStack'
    tags the hits. Same shape as Render's healthcheck."""
    tr = _fresh_tracker()
    now = 1_800_000_000.0
    for i in range(150):
        assert not tr.observe("betterstack_hash", "/",
                              now=now + i,
                              user_agent="Mozilla/5.0 BetterStack/1.0")


def test_our_own_canary_ua_exempt():
    """Our public-route-canary hits at a metronome cadence; the tracker
    must never trip on our own infrastructure UAs."""
    tr = _fresh_tracker()
    now = 1_800_000_000.0
    for i in range(150):
        assert not tr.observe("canary_hash", "/tokens",
                              now=now + i,
                              user_agent="xrpldashboard-public-route-canary/1.0")


def test_log_only_mode_reports_but_does_not_ban(monkeypatch, capsys):
    """Charlie ruling 2026-09-21 (post-Render-alert): every request-
    path filter ships in log-only mode for 24h first. In this mode a
    session that would normally trip is reported to the log but the
    tracker returns False so the request goes through unharmed. No
    banned_until stamp — flipping to enforce mode later must not
    surface phantom bans."""
    import importlib
    import logging
    monkeypatch.setenv("SCRAPER_TRACKER_LOG_ONLY", "1")
    import session_scraper_tracker as st_mod
    importlib.reload(st_mod)
    tr = st_mod.SessionScraperTracker()
    logger = logging.getLogger("session_scraper_tracker")
    records = []
    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    h = _Handler()
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    try:
        now = 1_800_000_000.0
        tripped = False
        for i in range(150):
            if tr.observe("shadow_hash", "/", now=now + i):
                tripped = True
                break
        assert not tripped, (
            "LOG_ONLY mode must NOT enforce — the tracker returns False "
            "so the request is served, and only the log records the shape"
        )
        # Confirm the SHADOW_TRIP log line fired at least once
        assert any("SHADOW_TRIP" in r for r in records), (
            f"expected a SHADOW_TRIP log line; got records={records!r}"
        )
        # And no ban was stamped — arming later must not surface phantom
        snap = tr._snapshot("shadow_hash")
        assert snap.get("banned_until", 0) == 0, (
            f"shadow mode must not stamp banned_until; got {snap!r}"
        )
    finally:
        logger.removeHandler(h)
        monkeypatch.delenv("SCRAPER_TRACKER_LOG_ONLY", raising=False)
        importlib.reload(st_mod)  # restore module default for other tests


def test_disguised_chrome_signature_log_only_by_default():
    """Charlie ruling 2026-09-21 (Mon PM, item 5, post-Render alert):
    the disguised-Chrome fleet signature ships log-only. Lightpanda UA
    + no static fetch + 50+ hits + ≤3 paths → SHADOW_TRIP line, no
    ban, request served. FLEET_DISGUISED_CHROME_ENFORCE default is off."""
    import logging
    tr = _fresh_tracker()
    records = []
    class _H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    logger = logging.getLogger("session_scraper_tracker")
    h = _H()
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    try:
        now = 1_800_000_000.0
        tripped = False
        for i in range(60):
            if tr.observe("lightpanda_hash", "/tokens", now=now + i,
                          user_agent="Mozilla/5.0 Lightpanda/1.0"):
                tripped = True
                break
        assert not tripped, "log-only default must not enforce"
        assert any("SHADOW_TRIP:disguised_chrome" in r for r in records), (
            f"expected disguised-Chrome SHADOW_TRIP; records={records!r}"
        )
    finally:
        logger.removeHandler(h)


def test_disguised_chrome_signature_exempts_real_browsers():
    """Even with Lightpanda-adjacent UA, a session that fetched a
    static asset is exempt (real-browser guard). Should not emit a
    SHADOW_TRIP line."""
    import logging
    tr = _fresh_tracker()
    records = []
    class _H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    logger = logging.getLogger("session_scraper_tracker")
    h = _H()
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    try:
        now = 1_800_000_000.0
        tr.observe("real_browser_hash", "/static/vendor/foo.css", now=now,
                   user_agent="Mozilla/5.0 HeadlessChrome/126.0")
        for i in range(60):
            tr.observe("real_browser_hash", "/tokens", now=now + 1 + i,
                       user_agent="Mozilla/5.0 HeadlessChrome/126.0")
        assert not any(
            "SHADOW_TRIP:disguised_chrome" in r
            for r in records
        ), (
            f"real-browser (fetched CSS) must not trip disguised-Chrome "
            f"even with HeadlessChrome UA; records={records!r}"
        )
    finally:
        logger.removeHandler(h)


def test_monitor_exempt_does_not_leak_to_normal_traffic():
    """Exempting monitor probes must not accidentally exempt the
    normal traffic from the same visitor_hash. A hash that ONLY hits
    monitor paths stays clean; a hash that ALSO scrapes normal paths
    should still trip."""
    tr = _fresh_tracker()
    now = 1_800_000_000.0
    # 200 monitor hits (should be no-ops)
    for i in range(200):
        tr.observe("mixed_hash", "/healthz", now=now + i,
                   user_agent="Go-http-client/1.1")
    # Now 150 scrape hits on a bare path with a real-browser UA
    tripped = False
    for i in range(150):
        if tr.observe("mixed_hash", "/", now=now + 300 + i,
                      user_agent="Mozilla/5.0"):
            tripped = True
            break
    assert tripped, (
        "monitor exemption must not spill onto normal traffic — a "
        "hash that scrapes / after probing /healthz should still trip"
    )


if __name__ == "__main__":
    test_brazil_poller_shape_trips_block()
    test_aion_bot_shape_two_paths_trips_block()
    test_real_browser_static_fetch_exempt()
    test_distinct_paths_over_threshold_stays_clean()
    test_window_trim_lets_hash_recover_after_pause()
    test_static_suffix_and_prefix_detection()
    test_empty_visitor_hash_never_trips()
    print("ALL PASS")
