"""L1 pager suppression tests.

The suppression layer sits on top of the fire-detection layer tested in
test_l1_pager_sovereignty.py. Its job is to end the page-storm that
follows a real sovereignty loss (or any long-lived alert): first-fire
pages immediately, unchanged conditions get throttled to one re-page
per SIX HOURS, suppressed re-pages accumulate into ONE digest line per
tick, and three classes of event always break through the throttle —
new alerts, fingerprint changes (reason/message flip), and recoveries.

Fixtures replay:
  1. First fire pages immediately.
  2. Unchanged condition suppressed within the throttle window; row lands
     in the digest instead.
  3. Digest message is emitted exactly once per tick when ≥1 suppression
     happened.
  4. A brand-new condition breaks through even while other suppressed
     alerts fill the digest.
  5. Recovery (alert clears) pages immediately regardless of throttle.
  6. Changed fingerprint (sample_reason flip on an existing alert)
     breaks through immediately.
  7. Unchanged condition past the throttle window re-pages full-volume
     and resets the throttle clock.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import l1_pager


THROTTLE_SEC = l1_pager.DEFAULT_REMINDER_INTERVAL_SEC  # 6h


@pytest.fixture
def sent(monkeypatch):
    """Capture every send_telegram call. Returns the list; tests inspect
    it after reconcile() runs. Nothing hits the network."""
    captured: list[str] = []

    def _fake_send(text, dry_run=False):
        captured.append(text)
        return True, "captured"

    monkeypatch.setattr(l1_pager, "send_telegram", _fake_send)
    return captured


def _sovereignty_alert(walker: str = "escrow_walker",
                       reason: str = "unreachable:ConnectError",
                       recent_count: int = 96) -> dict:
    return {
        "id": f"sovereignty_loss:{walker}",
        "walker": walker,
        "recent_count": recent_count,
        "window_hours": 6,
        "earliest_age_s": 22 * 3600,
        "latest_age_s": 300,
        "sample_reason": reason,
    }


def _empty_state() -> dict:
    return {"active_alerts": {}, "last_heartbeat_sent": None}


NOW = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# 1. First fire → immediate page
# ─────────────────────────────────────────────────────────────────────

def test_first_fire_pages_immediately(sent):
    state = _empty_state()
    alert = _sovereignty_alert()
    l1_pager.reconcile(state, [alert], NOW, THROTTLE_SEC, dry_run=False)

    assert len(sent) == 1
    assert "SOVEREIGNTY LOST" in sent[0]
    assert "escrow_walker" in sent[0]
    # State recorded, fingerprint set.
    assert "sovereignty_loss:escrow_walker" in state["active_alerts"]
    entry = state["active_alerts"]["sovereignty_loss:escrow_walker"]
    assert entry["fingerprint"] == l1_pager._alert_fingerprint(alert)


# ─────────────────────────────────────────────────────────────────────
# 2. Unchanged within throttle → suppressed to digest
# ─────────────────────────────────────────────────────────────────────

def test_unchanged_condition_suppressed_within_throttle(sent):
    # Seed state as if we paged 1h ago (well inside the 6h throttle).
    one_hour_ago = (NOW - dt.timedelta(hours=1)).isoformat()
    alert = _sovereignty_alert()
    state = {
        "active_alerts": {
            alert["id"]: {
                "first_fired": one_hour_ago,
                "last_reminder": one_hour_ago,
                "fingerprint": l1_pager._alert_fingerprint(alert),
                "snapshot": alert,
            },
        },
        "last_heartbeat_sent": None,
    }

    l1_pager.reconcile(state, [alert], NOW, THROTTLE_SEC, dry_run=False)

    # Exactly one message: the digest. No full re-page.
    assert len(sent) == 1
    assert "Still-active, unchanged" in sent[0]
    assert "sovereignty_loss:escrow_walker" in sent[0]
    # last_reminder NOT bumped (throttle preserves the original clock).
    assert state["active_alerts"][alert["id"]]["last_reminder"] == one_hour_ago


# ─────────────────────────────────────────────────────────────────────
# 3. Digest emitted exactly once per tick, aggregating all suppressions
# ─────────────────────────────────────────────────────────────────────

def test_digest_line_emitted_once_per_tick(sent):
    one_hour_ago = (NOW - dt.timedelta(hours=1)).isoformat()
    a1 = _sovereignty_alert("escrow_walker")
    a2 = _sovereignty_alert("nft_activity")
    a3 = _sovereignty_alert("rlusd_refresher")
    state = {
        "active_alerts": {
            a["id"]: {
                "first_fired": one_hour_ago,
                "last_reminder": one_hour_ago,
                "fingerprint": l1_pager._alert_fingerprint(a),
                "snapshot": a,
            }
            for a in (a1, a2, a3)
        },
        "last_heartbeat_sent": None,
    }

    l1_pager.reconcile(state, [a1, a2, a3], NOW, THROTTLE_SEC,
                       dry_run=False)

    # ONE digest, all three walkers in it, no full pages.
    assert len(sent) == 1
    digest = sent[0]
    assert "Still-active, unchanged" in digest
    for w in ("escrow_walker", "nft_activity", "rlusd_refresher"):
        assert f"sovereignty_loss:{w}" in digest
    # No "SOVEREIGNTY LOST" full-page bodies in this tick.
    assert "SOVEREIGNTY LOST" not in digest


# ─────────────────────────────────────────────────────────────────────
# 4. New condition breaks through even amid suppressions
# ─────────────────────────────────────────────────────────────────────

def test_new_condition_breaks_through_immediately(sent):
    one_hour_ago = (NOW - dt.timedelta(hours=1)).isoformat()
    old = _sovereignty_alert("escrow_walker")
    state = {
        "active_alerts": {
            old["id"]: {
                "first_fired": one_hour_ago,
                "last_reminder": one_hour_ago,
                "fingerprint": l1_pager._alert_fingerprint(old),
                "snapshot": old,
            },
        },
        "last_heartbeat_sent": None,
    }
    fresh = _sovereignty_alert("nft_activity")  # not in state — brand new

    l1_pager.reconcile(state, [old, fresh], NOW, THROTTLE_SEC,
                       dry_run=False)

    # Two sends: the fresh full-page + the digest for the throttled one.
    assert len(sent) == 2
    full_page = next(m for m in sent if "SOVEREIGNTY LOST" in m)
    digest = next(m for m in sent if "Still-active" in m)
    assert "nft_activity" in full_page
    assert "sovereignty_loss:escrow_walker" in digest
    assert "sovereignty_loss:nft_activity" not in digest


# ─────────────────────────────────────────────────────────────────────
# 5. Recovery breaks through immediately
# ─────────────────────────────────────────────────────────────────────

def test_recovery_breaks_through_immediately(sent):
    one_hour_ago = (NOW - dt.timedelta(hours=1)).isoformat()
    old = _sovereignty_alert("escrow_walker")
    state = {
        "active_alerts": {
            old["id"]: {
                "first_fired": one_hour_ago,
                "last_reminder": one_hour_ago,
                "fingerprint": l1_pager._alert_fingerprint(old),
                "snapshot": old,
            },
        },
        "last_heartbeat_sent": None,
    }

    # Alert absent from current → cleared.
    l1_pager.reconcile(state, [], NOW, THROTTLE_SEC, dry_run=False)

    assert len(sent) == 1
    assert "RECOVERED" in sent[0]
    assert "sovereignty_loss:escrow_walker" in sent[0]
    assert old["id"] not in state["active_alerts"]


# ─────────────────────────────────────────────────────────────────────
# 6. Fingerprint change (reason flip) breaks through immediately
# ─────────────────────────────────────────────────────────────────────

def test_changed_fingerprint_breaks_through_immediately(sent):
    one_hour_ago = (NOW - dt.timedelta(hours=1)).isoformat()
    old = _sovereignty_alert(reason="unreachable:ConnectError")
    state = {
        "active_alerts": {
            old["id"]: {
                "first_fired": one_hour_ago,
                "last_reminder": one_hour_ago,
                "fingerprint": l1_pager._alert_fingerprint(old),
                "snapshot": old,
            },
        },
        "last_heartbeat_sent": None,
    }
    # Same walker, different failure mode.
    changed = _sovereignty_alert(reason="local_degraded:notSynced")

    l1_pager.reconcile(state, [changed], NOW, THROTTLE_SEC,
                       dry_run=False)

    assert len(sent) == 1
    assert "Changed" in sent[0]
    assert "SOVEREIGNTY LOST" in sent[0]
    # last_reminder AND fingerprint updated to the new state.
    entry = state["active_alerts"][old["id"]]
    assert entry["last_reminder"] == NOW.isoformat()
    assert entry["fingerprint"] == l1_pager._alert_fingerprint(changed)


# ─────────────────────────────────────────────────────────────────────
# 7. Past-throttle unchanged → full re-page + throttle-clock reset
# ─────────────────────────────────────────────────────────────────────

def test_unchanged_past_throttle_repages_and_resets(sent):
    # last paged 7h ago — throttle expired
    seven_hours_ago = (NOW - dt.timedelta(hours=7)).isoformat()
    alert = _sovereignty_alert()
    state = {
        "active_alerts": {
            alert["id"]: {
                "first_fired": seven_hours_ago,
                "last_reminder": seven_hours_ago,
                "fingerprint": l1_pager._alert_fingerprint(alert),
                "snapshot": alert,
            },
        },
        "last_heartbeat_sent": None,
    }

    l1_pager.reconcile(state, [alert], NOW, THROTTLE_SEC, dry_run=False)

    assert len(sent) == 1
    assert "Still active" in sent[0]
    assert "SOVEREIGNTY LOST" in sent[0]
    assert state["active_alerts"][alert["id"]]["last_reminder"] == NOW.isoformat()


# ─────────────────────────────────────────────────────────────────────
# 7b. Digest fingerprint gate — one suppressed alert = one notice, not
#     one per ~20-min tick (2026-08-30 fix; Charlie got 16 identical
#     "🔇 still-active" notices between 01:48 and 06:52 ET).
# ─────────────────────────────────────────────────────────────────────

def test_digest_notice_not_re_emitted_when_set_unchanged(sent):
    one_hour_ago = (NOW - dt.timedelta(hours=1)).isoformat()
    alert = _sovereignty_alert()
    state = {
        "active_alerts": {
            alert["id"]: {
                "first_fired": one_hour_ago,
                "last_reminder": one_hour_ago,
                "fingerprint": l1_pager._alert_fingerprint(alert),
                "snapshot": alert,
            },
        },
        "last_heartbeat_sent": None,
        "last_digest_fingerprint": None,
    }

    # Tick 1 (t+1h): suppressed set enters → one digest notice.
    l1_pager.reconcile(state, [alert], NOW, THROTTLE_SEC, dry_run=False)
    assert len(sent) == 1
    assert "Still-active, unchanged" in sent[0]
    fp_after_tick1 = state["last_digest_fingerprint"]
    assert fp_after_tick1

    # Ticks 2 and 3 (t+1h20, t+1h40) — same suppressed set, still inside
    # the 6h throttle. NO additional digest notices should fire.
    for minutes in (20, 40):
        later = NOW + dt.timedelta(minutes=minutes)
        l1_pager.reconcile(state, [alert], later, THROTTLE_SEC,
                           dry_run=False)
    assert len(sent) == 1, (
        f"expected exactly 1 digest across 3 ticks with same set, "
        f"got {len(sent)}: {sent!r}"
    )
    # Fingerprint stable across ticks (proves the gate identifies the
    # set as unchanged).
    assert state["last_digest_fingerprint"] == fp_after_tick1


def test_digest_re_emits_when_new_alert_joins_suppressed_set(sent):
    one_hour_ago = (NOW - dt.timedelta(hours=1)).isoformat()
    a1 = _sovereignty_alert("escrow_walker")
    state = {
        "active_alerts": {
            a1["id"]: {
                "first_fired": one_hour_ago,
                "last_reminder": one_hour_ago,
                "fingerprint": l1_pager._alert_fingerprint(a1),
                "snapshot": a1,
            },
        },
        "last_heartbeat_sent": None,
        "last_digest_fingerprint": None,
    }

    # Tick 1: only a1 suppressed → digest 1.
    l1_pager.reconcile(state, [a1], NOW, THROTTLE_SEC, dry_run=False)
    assert len(sent) == 1
    assert "sovereignty_loss:escrow_walker" in sent[0]

    # Tick 2 (t+20m): a2 first-fires (full page) AND a1 still suppressed
    # → set has grown. Expect fresh-alert page + a new digest with both.
    a2 = _sovereignty_alert("nft_activity")
    later = NOW + dt.timedelta(minutes=20)
    l1_pager.reconcile(state, [a1, a2], later, THROTTLE_SEC,
                       dry_run=False)
    # Tick 3 (t+40m): both now suppressed → digest set = {a1, a2}. Same
    # set on the next tick should not re-emit.
    later2 = NOW + dt.timedelta(minutes=40)
    l1_pager.reconcile(state, [a1, a2], later2, THROTTLE_SEC,
                       dry_run=False)

    digests = [m for m in sent if "Still-active, unchanged" in m]
    # Exactly 2 digests: one for {a1} on tick 1, one for {a1, a2} on
    # tick 2. Tick 3 has identical set → suppressed by fingerprint gate.
    assert len(digests) == 2, f"digest count: {len(digests)} :: {digests!r}"
    assert "sovereignty_loss:nft_activity" not in digests[0]
    assert "sovereignty_loss:nft_activity" in digests[1]


def test_digest_fingerprint_clears_on_recovery(sent):
    one_hour_ago = (NOW - dt.timedelta(hours=1)).isoformat()
    alert = _sovereignty_alert()
    state = {
        "active_alerts": {
            alert["id"]: {
                "first_fired": one_hour_ago,
                "last_reminder": one_hour_ago,
                "fingerprint": l1_pager._alert_fingerprint(alert),
                "snapshot": alert,
            },
        },
        "last_heartbeat_sent": None,
        "last_digest_fingerprint": None,
    }

    # Tick 1: suppress and record fingerprint.
    l1_pager.reconcile(state, [alert], NOW, THROTTLE_SEC, dry_run=False)
    assert state["last_digest_fingerprint"]

    # Tick 2: alert cleared. Recovery fires; fingerprint resets.
    later = NOW + dt.timedelta(minutes=20)
    l1_pager.reconcile(state, [], later, THROTTLE_SEC, dry_run=False)
    assert state["last_digest_fingerprint"] is None
    # Next fresh alert should get its own digest even with the same id.
    later2 = later + dt.timedelta(hours=1)
    fresh_seed_ts = (later2 - dt.timedelta(hours=1)).isoformat()
    state["active_alerts"][alert["id"]] = {
        "first_fired": fresh_seed_ts,
        "last_reminder": fresh_seed_ts,
        "fingerprint": l1_pager._alert_fingerprint(alert),
        "snapshot": alert,
    }
    l1_pager.reconcile(state, [alert], later2, THROTTLE_SEC, dry_run=False)
    assert any("Still-active, unchanged" in m for m in sent[-2:])


# ─────────────────────────────────────────────────────────────────────
# 8. Sovereignty alert body surfaces both earliest and latest event age
#    (the echo-vs-fire hygiene item from the 2026-08-17 saga)
# ─────────────────────────────────────────────────────────────────────

def test_sovereignty_alert_body_shows_latest_event_age():
    alert = _sovereignty_alert()
    alert["earliest_age_s"] = 22 * 3600
    alert["latest_age_s"] = 4 * 3600 + 12 * 60   # 4.2h ago → echo-looking
    body = l1_pager.format_alert(alert)
    assert "earliest event 22" in body
    assert "latest event 4.2h" in body

    # Fresh-fire shape: latest event 3 minutes ago
    alert["latest_age_s"] = 180
    body = l1_pager.format_alert(alert)
    assert "latest event 3m ago" in body
