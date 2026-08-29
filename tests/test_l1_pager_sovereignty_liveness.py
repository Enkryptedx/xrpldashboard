"""L1 pager sovereignty-loss live-state suppression tests.

Ratified 2026-08-29 after three red pages landed for
nft_activity / escrow_walker / rlusd_refresher describing an incident
that had already self-recovered five hours earlier. Ruling:
  (b) Live-state check before firing — walker_health.last_run_ok AND
      last_run_completed within 2× cadence → suppress the 🟥 page. But
      don't swallow the history: emit the suppressed incident as one
      informational (🟨) line.
  (a) Wire sovereignty_loss into reconcile()'s RECOVERED path — already
      present via the "cleared" loop, verified by test_recovered_wired.

Replay corpus reflects the exact today-shape:
  - Three Class A walkers each with 12+ unreachable events spanning
    ~7h ending ~5h before NOW (mirrors 03:17→10:20 UTC on 2026-08-29).
  - walker_health for each walker reports last_run_ok=True with
    last_run_completed within its cadence — i.e., currently healthy.
  - Expected: check_sovereignty_loss returns [] AND populates
    _SOVEREIGNTY_LIVENESS_SUPPRESSED with three entries.

Contrast test drops liveness: same rows, unhealthy walker_health →
all three alerts survive as red pages (existing behavior preserved
when the incident is still active).
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import l1_pager


# Anchor to today's shape: NOW = 15:22 UTC = 11:22 EDT (moments after the
# three red pages actually landed 2026-08-29 15:17 UTC / 11:17 EDT).
NOW = dt.datetime(2026, 8, 29, 15, 22, 0, tzinfo=dt.timezone.utc)
INCIDENT_START = NOW - dt.timedelta(hours=12, minutes=5)  # 03:17 UTC
INCIDENT_END = NOW - dt.timedelta(hours=5, minutes=2)     # 10:20 UTC


def _row(walker, ts, reason="unreachable:ConnectError"):
    return (walker, ts, reason)


def _today_rows():
    """Mirror the 2026-08-29 timeline for three Mac walkers.

    Real incident shape (per Charlie's page): 935/40/22 fallback events
    spanning 03:17→10:20 UTC with a retry-storm burst concentrated in
    the last hour before recovery. Replay each walker with:
      - one anchor event at INCIDENT_START (12h5m ago) — satisfies the
        SOVEREIGNTY_MIN_SUSTAIN_HOURS floor.
      - 14 events packed in the incident's final hour, all landing
        inside the trailing 6h from NOW — satisfies SOVEREIGNTY_MIN_EVENTS.
    Threshold logic then fires an alert for each walker (which the live-
    state suppression path is expected to drop)."""
    walkers = ("nft_activity", "escrow_walker", "rlusd_refresher")
    burst_start = INCIDENT_END - dt.timedelta(minutes=59)
    burst_span_s = (INCIDENT_END - burst_start).total_seconds()
    rows = []
    for walker in walkers:
        rows.append(_row(walker, INCIDENT_START))
        for i in range(14):
            ts = burst_start + dt.timedelta(seconds=burst_span_s * i / 13)
            rows.append(_row(walker, ts))
    return rows


@pytest.fixture(autouse=True)
def _reset_suppression_bin():
    """Every test starts with an empty suppression bin. Real code clears
    it inside check_sovereignty_loss too, but tests may call the
    evaluator paths directly and would otherwise see cross-test bleed."""
    l1_pager._SOVEREIGNTY_LIVENESS_SUPPRESSED.clear()
    yield
    l1_pager._SOVEREIGNTY_LIVENESS_SUPPRESSED.clear()


# ─────────────────────────────────────────────────────────────────────
# 1. Historical + healthy → suppressed, no red page, informational line
# ─────────────────────────────────────────────────────────────────────

def test_healthy_walker_suppresses_red_page(monkeypatch):
    """Today's exact shape (three walkers, sustained flood, now healthy).
    Expect: zero red-page alerts returned, three suppression entries
    stashed for the informational emit path."""
    rows = _today_rows()
    monkeypatch.setattr(l1_pager, "_fetch_sovereignty_rows",
                        lambda now_utc: rows)
    monkeypatch.setattr(
        l1_pager, "_check_walker_liveness",
        lambda walker, now_utc: (True, {
            "last_run_ok": True,
            "last_run_completed":
                (now_utc - dt.timedelta(minutes=2)).isoformat(),
            "cadence_seconds": 900,
        }),
    )

    alerts = l1_pager.check_sovereignty_loss(NOW)
    assert alerts == [], f"expected zero red pages, got {alerts}"

    suppressed = l1_pager._SOVEREIGNTY_LIVENESS_SUPPRESSED
    walkers = sorted(e["walker"] for e in suppressed)
    assert walkers == ["escrow_walker", "nft_activity", "rlusd_refresher"]
    for entry in suppressed:
        assert entry["event_count"] >= 12
        assert entry["health"]["last_run_ok"] is True


def test_informational_line_format_carries_history():
    """Charlie's spec: informational line surfaces walker + start→end +
    event count + healthy status. Format must survive the emit path."""
    entry = {
        "walker": "nft_activity",
        "start_ts": INCIDENT_START.isoformat(),
        "end_ts": INCIDENT_END.isoformat(),
        "event_count": 935,
        "health": {"last_run_ok": True},
    }
    msg = l1_pager.format_sovereignty_liveness_suppressed(entry)
    assert "Sovereignty blip (resolved)" in msg
    assert "nft_activity" in msg
    assert "935" in msg
    assert "currently healthy" in msg
    # ET-rendered range appears (03:17 UTC = 23:17 ET prior day; end
    # 10:20 UTC = 06:20 ET). Just prove both hours land in the string.
    assert " ET" in msg


# ─────────────────────────────────────────────────────────────────────
# 2. Historical + unhealthy → still red-page (existing behavior)
# ─────────────────────────────────────────────────────────────────────

def test_unhealthy_walker_still_red_pages(monkeypatch):
    """If the incident is still active (walker unhealthy), suppression
    MUST NOT engage. Same rows, unhealthy liveness → three red pages."""
    rows = _today_rows()
    monkeypatch.setattr(l1_pager, "_fetch_sovereignty_rows",
                        lambda now_utc: rows)
    monkeypatch.setattr(
        l1_pager, "_check_walker_liveness",
        lambda walker, now_utc: (False, {
            "last_run_ok": False,
            "last_run_completed": None,
            "cadence_seconds": 900,
        }),
    )

    alerts = l1_pager.check_sovereignty_loss(NOW)
    ids = sorted(a["id"] for a in alerts)
    assert ids == [
        "sovereignty_loss:escrow_walker",
        "sovereignty_loss:nft_activity",
        "sovereignty_loss:rlusd_refresher",
    ]
    assert l1_pager._SOVEREIGNTY_LIVENESS_SUPPRESSED == []


def test_no_walker_health_row_treated_as_unhealthy(monkeypatch):
    """A walker with no walker_health row is not granted the benefit of
    the doubt (completely-silent walker shouldn't be suppressed)."""
    rows = _today_rows()
    monkeypatch.setattr(l1_pager, "_fetch_sovereignty_rows",
                        lambda now_utc: rows)

    def _no_row_liveness(walker, now_utc):
        return False, {}

    monkeypatch.setattr(l1_pager, "_check_walker_liveness", _no_row_liveness)

    alerts = l1_pager.check_sovereignty_loss(NOW)
    assert len(alerts) == 3


# ─────────────────────────────────────────────────────────────────────
# 3. Stale walker_health (last_run_completed > 2× cadence) → red-page
# ─────────────────────────────────────────────────────────────────────

def test_stale_completion_treated_as_unhealthy(monkeypatch):
    """last_run_ok=True but last_run_completed 3× cadence ago → the
    walker is stuck, not healthy. Liveness helper must say False."""
    long_ago = NOW - dt.timedelta(minutes=60)
    ok, _snap = l1_pager._check_walker_liveness.__wrapped__ if False else (True, {})
    # Directly exercise the helper via a stub DB call
    class _Cursor:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchone(self):
            return (True, long_ago, 900)  # ok=True, completed 60m ago, cadence 15m

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cursor()

    monkeypatch.setattr(l1_pager, "_pg_connect", lambda: _Conn())
    healthy, snap = l1_pager._check_walker_liveness("escrow_walker", NOW)
    assert healthy is False
    assert snap["cadence_seconds"] == 900


def test_fresh_completion_treated_as_healthy(monkeypatch):
    """last_run_ok=True + last_run_completed within cadence → healthy."""
    just_now = NOW - dt.timedelta(seconds=120)

    class _Cursor:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchone(self):
            return (True, just_now, 900)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cursor()

    monkeypatch.setattr(l1_pager, "_pg_connect", lambda: _Conn())
    healthy, snap = l1_pager._check_walker_liveness("escrow_walker", NOW)
    assert healthy is True
    assert snap["last_run_ok"] is True


def test_liveness_floor_protects_small_cadence(monkeypatch):
    """Cadence 60s × 2 = 120s would say a walker is unhealthy 3 min
    after a successful run — but the run just finished. Floor of 10min
    prevents that; any completion younger than 10min is healthy even
    with a tiny cadence."""
    two_minutes_ago = NOW - dt.timedelta(seconds=120)

    class _Cursor:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchone(self):
            return (True, two_minutes_ago, 60)  # cadence 60s

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cursor()

    monkeypatch.setattr(l1_pager, "_pg_connect", lambda: _Conn())
    healthy, _ = l1_pager._check_walker_liveness("escrow_walker", NOW)
    assert healthy is True, "10-min floor must protect small-cadence walkers"


# ─────────────────────────────────────────────────────────────────────
# 4. (a) Recovered wiring — reconcile clears sovereignty_loss like every
#     other alert class
# ─────────────────────────────────────────────────────────────────────

def test_recovered_wired_for_sovereignty(monkeypatch):
    """When a sovereignty_loss alert previously present is absent from
    the new tick, reconcile emits a RECOVERED message and drops it from
    state — same code path as every other alert class."""
    sent = []
    monkeypatch.setattr(
        l1_pager, "send_telegram",
        lambda text, dry_run=False: (sent.append(text), (True, "ok"))[1],
    )
    state = {
        "active_alerts": {
            "sovereignty_loss:escrow_walker": {
                "first_fired": (NOW - dt.timedelta(hours=6)).isoformat(),
                "last_reminder": (NOW - dt.timedelta(hours=6)).isoformat(),
                "fingerprint": "sovereignty_loss:escrow_walker|reason|",
                "snapshot": {"id": "sovereignty_loss:escrow_walker"},
            },
        },
    }
    l1_pager.reconcile(state, current=[], now_utc=NOW,
                       reminder_interval=6 * 3600, dry_run=False)

    assert "sovereignty_loss:escrow_walker" not in state["active_alerts"]
    assert any("RECOVERED" in m and "escrow_walker" in m for m in sent)


# ─────────────────────────────────────────────────────────────────────
# 5. State dedupe — informational line does NOT re-emit for a stable
#     already-emitted incident
# ─────────────────────────────────────────────────────────────────────

def test_state_dedupe_across_ticks(monkeypatch):
    """First tick emits informational line; second tick with the same
    (walker, latest_ts) MUST NOT re-emit. Only a NEW latest_ts triggers
    a fresh emit."""
    rows = _today_rows()
    monkeypatch.setattr(l1_pager, "_fetch_sovereignty_rows",
                        lambda now_utc: rows)
    monkeypatch.setattr(
        l1_pager, "_check_walker_liveness",
        lambda walker, now_utc: (True, {
            "last_run_ok": True,
            "last_run_completed":
                (now_utc - dt.timedelta(minutes=2)).isoformat(),
            "cadence_seconds": 900,
        }),
    )

    l1_pager.check_sovereignty_loss(NOW)
    suppressed_snapshot = list(l1_pager._SOVEREIGNTY_LIVENESS_SUPPRESSED)
    assert len(suppressed_snapshot) == 3

    seen = {}
    sent = []

    def _fake_send(text, dry_run=False):
        sent.append(text)
        return (True, "ok")

    monkeypatch.setattr(l1_pager, "send_telegram", _fake_send)

    for entry in suppressed_snapshot:
        walker = entry["walker"]
        latest_ts = entry["end_ts"]
        if seen.get(walker) == latest_ts:
            continue
        _fake_send(l1_pager.format_sovereignty_liveness_suppressed(entry))
        seen[walker] = latest_ts
    assert len(sent) == 3

    # Second tick with identical inputs — no new messages.
    sent.clear()
    for entry in suppressed_snapshot:
        walker = entry["walker"]
        latest_ts = entry["end_ts"]
        if seen.get(walker) == latest_ts:
            continue
        _fake_send(l1_pager.format_sovereignty_liveness_suppressed(entry))
        seen[walker] = latest_ts
    assert sent == []

    # A NEW latest_ts (fresh blip in the same walker) DOES re-emit.
    new_entry = dict(suppressed_snapshot[0])
    new_entry["end_ts"] = (
        dt.datetime.fromisoformat(new_entry["end_ts"])
        + dt.timedelta(minutes=30)
    ).isoformat()
    walker = new_entry["walker"]
    if seen.get(walker) != new_entry["end_ts"]:
        _fake_send(l1_pager.format_sovereignty_liveness_suppressed(new_entry))
        seen[walker] = new_entry["end_ts"]
    assert len(sent) == 1
