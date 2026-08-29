"""L1 pager sovereignty-loss check tests.

The sovereignty_loss rule is L1's answer to the 43-day silent Mac walker
fallback observed mid-2026 (framework Python 3.14 + launchd + utun4 VPN
routes → EHOSTUNREACH to 192.168.40.95). Rule fires when a Class A
(Mac-hosted) walker has ≥12 unreachable/local_* fallback events in the
trailing 6h AND the earliest such event within the trailing 24h is
≥12h old. Class A allowlist (six walkers) excludes Render structural
Class B noise (check_page/token_page/unknown).

Fixtures replay:
  - The July flood shape (steady 60+ events/24h from the affected walker)
    → fires.
  - Render Class B baseline (400+/24h from check_page/token_page)
    → silent (allowlist skips them even if input is passed).
  - Transient blip (20 events all in trailing 30min, MIN(ts) recent)
    → silent (sustain check refuses to fire on a blip).
  - Below-rate long tail (5 events over 20h, MIN(ts) 20h old)
    → silent (rate check refuses to fire without density).
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import l1_pager


NOW = dt.datetime(2026, 7, 5, 0, 0, 0, tzinfo=dt.timezone.utc)


def _row(walker: str, minutes_ago: float,
         reason: str = "unreachable:ConnectError") -> tuple:
    return (walker, NOW - dt.timedelta(minutes=minutes_ago), reason)


# ─────────────────────────────────────────────────────────────────────
# 1. July flood shape — fires
# ─────────────────────────────────────────────────────────────────────

def test_fires_on_july_flood_shape():
    """The observed 2026-07 flood: steady stream of unreachable errors
    from escrow_walker every ~15 minutes, running for a day. Rule must
    fire (this is the whole reason the check exists)."""
    # 96 events spaced 15 minutes apart, spanning 24h back to 15min ago
    rows = [
        _row("escrow_walker", minutes_ago=(24 * 60 - i * 15))
        for i in range(96)
    ]
    alerts = l1_pager._evaluate_sovereignty_rows(rows, NOW)
    assert len(alerts) == 1, alerts
    alert = alerts[0]
    assert alert["id"] == "sovereignty_loss:escrow_walker"
    assert alert["walker"] == "escrow_walker"
    # Trailing 6h at 15-min cadence = 24 events. Threshold ≥12 → pass.
    assert alert["recent_count"] >= 12
    assert alert["window_hours"] == l1_pager.SOVEREIGNTY_WINDOW_HOURS
    # Earliest event was ~24h ago (well past 12h sustain floor)
    assert alert["earliest_age_s"] > 12 * 3600
    assert "unreachable" in alert["sample_reason"]

    # Format check — human message must name the walker + surface the
    # sustain evidence so an on-call operator immediately knows this
    # isn't a blip.
    msg = l1_pager.format_alert(alert)
    assert "SOVEREIGNTY LOST" in msg
    assert "escrow_walker" in msg
    assert "sustained, not a blip" in msg


# ─────────────────────────────────────────────────────────────────────
# 2. Multi-walker flood — each walker gets its own alert
# ─────────────────────────────────────────────────────────────────────

def test_fires_per_walker_on_multi_walker_flood():
    """If the interpreter/routing bug takes down multiple walkers at
    once (exactly what happened mid-2026), we want a separate alert per
    walker so the recovery is trackable per-walker."""
    rows = []
    for walker in ("escrow_walker", "oracle_walker", "rlusd_refresher"):
        for i in range(96):
            rows.append(_row(walker, minutes_ago=(24 * 60 - i * 15)))
    alerts = l1_pager._evaluate_sovereignty_rows(rows, NOW)
    ids = sorted(a["id"] for a in alerts)
    assert ids == [
        "sovereignty_loss:escrow_walker",
        "sovereignty_loss:oracle_walker",
        "sovereignty_loss:rlusd_refresher",
    ]


# ─────────────────────────────────────────────────────────────────────
# 3. Render structural walkers (Class B) — silent even if fed as input
# ─────────────────────────────────────────────────────────────────────

def test_silent_on_render_class_b_baseline():
    """check_page/token_page/unknown are the Render→Lenovo hairpin — a
    known structural problem in a separate scope. The allowlist inside
    _evaluate_sovereignty_rows must silently drop them even if a future
    query change accidentally includes them."""
    rows = []
    for walker in ("check_page", "token_page", "unknown"):
        for i in range(200):
            rows.append(_row(walker, minutes_ago=(24 * 60 - i * 7)))
    alerts = l1_pager._evaluate_sovereignty_rows(rows, NOW)
    assert alerts == []


def test_silent_on_render_flask_modules_cold_storage_escrow_supply():
    """cold_storage / escrow_supply are Flask modules imported by app.py
    on Render (not Mac walkers). Their fallback events are Render→Lenovo
    hairpin — structurally impossible for AWS to reach 192.168.40.95.
    Mis-shelved in Class A until 2026-08-17; regression test so they stay
    in Class B. Flood-shape input from either name must NOT fire, while
    an equivalent shape from a real Mac walker DOES."""
    # Flood shape from the mis-shelved names — must be silent
    for name in ("cold_storage", "escrow_supply"):
        rows = [_row(name, minutes_ago=(24 * 60 - i * 15)) for i in range(96)]
        assert l1_pager._evaluate_sovereignty_rows(rows, NOW) == [], name
        assert name in l1_pager.SOVEREIGNTY_CLASS_B_WALKERS
        assert name not in l1_pager.SOVEREIGNTY_CLASS_A_WALKERS
    # Positive control: same shape from a real Mac walker still fires
    rows = [_row("escrow_walker", minutes_ago=(24 * 60 - i * 15))
            for i in range(96)]
    alerts = l1_pager._evaluate_sovereignty_rows(rows, NOW)
    assert len(alerts) == 1
    assert alerts[0]["walker"] == "escrow_walker"


def test_silent_on_mixed_class_a_and_class_b():
    """Under-threshold Class A + noisy Class B → no alert."""
    rows = []
    # Below-rate Class A (only 5 events in 24h)
    for i in range(5):
        rows.append(_row("escrow_walker", minutes_ago=i * 240))
    # Massive Class B noise
    for i in range(500):
        rows.append(_row("check_page", minutes_ago=i * 3))
    alerts = l1_pager._evaluate_sovereignty_rows(rows, NOW)
    assert alerts == []


# ─────────────────────────────────────────────────────────────────────
# 4. Transient blip — sustain check refuses to fire
# ─────────────────────────────────────────────────────────────────────

def test_silent_on_transient_blip():
    """20 fallback events, but all in the trailing 30min (MIN(ts) 30min
    old). Rate meets threshold but sustain does not → no alert. This is
    the "the internet had a bad minute" case; not worth a page."""
    rows = [_row("escrow_walker", minutes_ago=(30 - i * 1.5))
            for i in range(20)]
    alerts = l1_pager._evaluate_sovereignty_rows(rows, NOW)
    assert alerts == []


def test_silent_on_blip_that_just_crossed_the_hour_line():
    """15 events in the trailing 6h, MIN(ts) 8h old (not yet 12h).
    Rate OK but sustain not — should still be silent."""
    rows = [_row("escrow_walker", minutes_ago=(8 * 60 - i * 30))
            for i in range(15)]
    alerts = l1_pager._evaluate_sovereignty_rows(rows, NOW)
    # Only 4 events in trailing 6h (with 30-min spacing starting 8h ago)
    # — actually let's inspect: events at 8h, 7.5h, 7h, 6.5h, 6h, 5.5h,
    # ... 0.5h ago. Events with minutes_ago <= 360 are in the window.
    # 8*60 - i*30 <= 360 → i >= 4. So 11 events in trailing 6h (i=4..14).
    # That's below the 12 threshold anyway. This test proves nothing new.
    # Replace with a stricter case:
    rows = [_row("escrow_walker", minutes_ago=(11.9 * 60 - i * 30))
            for i in range(24)]
    alerts = l1_pager._evaluate_sovereignty_rows(rows, NOW)
    # MIN(ts) is 11.9h ago — under 12h sustain floor → no alert
    assert alerts == []


# ─────────────────────────────────────────────────────────────────────
# 5. Below-rate long tail — rate check refuses to fire
# ─────────────────────────────────────────────────────────────────────

def test_silent_on_below_rate_long_tail():
    """5 events spread over 20h. MIN(ts) is old enough (sustain passes)
    but only 5 events total → recent 6h has ≤2 → rate below threshold."""
    rows = [_row("escrow_walker", minutes_ago=(20 * 60 - i * 240))
            for i in range(5)]
    alerts = l1_pager._evaluate_sovereignty_rows(rows, NOW)
    assert alerts == []


# ─────────────────────────────────────────────────────────────────────
# 6. Reason filter — non-sovereignty reasons don't count
# ─────────────────────────────────────────────────────────────────────
# Note: the SQL WHERE clause filters reasons; here we prove the Python
# side doesn't accidentally count rows that snuck through. Every reason
# in the input list is `unreachable:ConnectError` by _row default, so
# use a synthetic "load_shed" reason to test the filter.
# Actually the Python side does NOT re-filter reasons — the SQL does.
# So this test documents that expectation: rows arrive already reason-
# filtered by SQL, and the Python side trusts them.


def test_python_side_trusts_sql_reason_filter():
    """The SQL WHERE clause is authoritative for reason filtering; the
    Python evaluator does not re-check reasons. Documenting this so a
    future refactor doesn't silently drop the filter."""
    rows = [
        _row("escrow_walker", minutes_ago=(24 * 60 - i * 15),
             reason="load_shed:foo")
        for i in range(96)
    ]
    alerts = l1_pager._evaluate_sovereignty_rows(rows, NOW)
    # 96 events span the sustain floor; even with an unfiltered reason
    # (load_shed rather than unreachable/local_*), if SQL let it through
    # Python fires. Real SQL WHERE prevents this class from reaching us.
    assert len(alerts) == 1


# ─────────────────────────────────────────────────────────────────────
# 7. Recovery — walker drops off input → no alert (reconcile clears)
# ─────────────────────────────────────────────────────────────────────

def test_recovery_walker_absent_from_input_yields_no_alert():
    """When the walker restores sovereignty, no new fallback rows land.
    Empty input → no alerts. `reconcile` in the main loop takes over from
    there to emit the RECOVERED message on the next tick."""
    alerts = l1_pager._evaluate_sovereignty_rows([], NOW)
    assert alerts == []


# ─────────────────────────────────────────────────────────────────────
# 8. Charlie's "would have paged 4h into July flood" claim
# ─────────────────────────────────────────────────────────────────────

def test_when_july_flood_would_first_page():
    """Charlie's memo said the pager would have paged ~4h into the flood.
    Under the *approved* rule (≥12 events/6h AND MIN(ts) ≥12h old), the
    sustain floor delays first fire to hour 12 of the flood — the
    "4h" number in earlier notes assumed a rate-only rule. Under the
    ratified rule, page at hour ~12. Test replays the exact flood shape
    at multiple checkpoints so the timing is unambiguous."""
    # Simulate 1 event every 15 minutes from t0. Snapshot the alert
    # state at t=4h, t=11h, t=12h, t=13h. Expect silence at 4h/11h,
    # fire at 12h+.
    flood_start = NOW - dt.timedelta(hours=24)  # anchor
    events = [flood_start + dt.timedelta(minutes=15 * i)
              for i in range(96)]

    # helper: snapshot at wall-clock `now` — rows up to now
    def evaluate_at(now):
        rows = [("escrow_walker", ts, "unreachable:ConnectError")
                for ts in events if ts <= now]
        return l1_pager._evaluate_sovereignty_rows(rows, now)

    # Hour 4 of flood — silent (sustain floor not met)
    silent_at = flood_start + dt.timedelta(hours=4)
    assert evaluate_at(silent_at) == []

    # Hour 11 — still silent
    silent_at = flood_start + dt.timedelta(hours=11)
    assert evaluate_at(silent_at) == []

    # Hour 12.5 — sustain floor now met + rate has been steady
    fire_at = flood_start + dt.timedelta(hours=12, minutes=30)
    alerts = evaluate_at(fire_at)
    assert len(alerts) == 1
    assert alerts[0]["walker"] == "escrow_walker"

    # 43 days into flood — obviously still firing (proof the rule
    # doesn't self-mute once the sustain check keeps compounding).
    # Note: input rows here are only from the first 24h since the
    # SQL query is bounded to trailing 24h — that's realistic.
    far_future = flood_start + dt.timedelta(days=43)
    rows_last_24h = [
        ("escrow_walker", far_future - dt.timedelta(minutes=15 * i),
         "unreachable:ConnectError")
        for i in range(96)
    ]
    alerts = l1_pager._evaluate_sovereignty_rows(rows_last_24h, far_future)
    assert len(alerts) == 1
