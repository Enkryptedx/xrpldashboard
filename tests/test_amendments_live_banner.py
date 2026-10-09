"""Tests for amendments_live_banner.recently_enabled — step 1 of branch
amendments-live-banner-2026-10-09 ("Just went LIVE" box on /amendments).

Fakes only: plain dicts shaped exactly like app._load_amendment_majority_
history() rows. No DB, no network, no Flask (owner rule 2026-10-08: local
tests never touch the production DATABASE_URL).
"""
import datetime as dt

from amendments_live_banner import (
    WINDOW_HOURS,
    live_fingerprint,
    recently_enabled,
)

NOW = dt.datetime(2026, 10, 9, 10, 30, 0, tzinfo=dt.timezone.utc)

# The real PermissionDelegationV1_1 activation (LEDGER TRUTH, verified in
# amendment_majority_history 2026-10-08 after the true-enable-time fix).
PD_HASH = "0F48FF561C709540328F31F1C97FD512ACC8B4E42138A161CB0E21ECA292540B"
PD_TX = "478284066BA0B83CC35CA1174F667F43D9577782F221440AFCC5541788F25A77"


def _row(hash_, name, enabled=True, enabled_iso=None, ledger=None,
         tx=None, enabled_close_iso=None):
    return {
        "hash": hash_, "name": name, "enabled": enabled,
        "enabled_iso": enabled_iso, "enabled_close_iso": enabled_close_iso,
        "enabled_seen_ledger": ledger, "enabled_tx_hash": tx,
        "majority_close_iso": "2026-09-24T21:25:01Z",
        "removed_iso": None, "active": not enabled,
    }


def _pd_row():
    return _row(PD_HASH, "PermissionDelegationV1_1",
                enabled_iso="2026-10-08T21:29:50Z",
                ledger=107524865, tx=PD_TX)


def test_permission_delegation_shows_now_with_true_ledger_time_and_tx():
    """The motivating case: PD went live 2026-10-08 21:29:50 UTC, ~13h
    before NOW, so it must be in the box with its true enable ledger and
    the EnableAmendment tx hash."""
    out = recently_enabled([_pd_row()], now=NOW)
    assert len(out) == 1
    e = out[0]
    assert e["name"] == "PermissionDelegationV1_1"
    assert e["enabled_iso"] == "2026-10-08T21:29:50Z"
    assert e["enabled_seen_ledger"] == 107524865
    assert e["enabled_tx_hash"] == PD_TX
    assert e["hash"] == PD_HASH


def test_empty_when_nothing_enabled_recently():
    """Box must hide entirely (empty list) rather than render a header
    with nothing under it."""
    assert recently_enabled([], now=NOW) == []
    old = _row("AAAA", "Ancient", enabled_iso="2026-01-01T00:00:00Z")
    assert recently_enabled([old], now=NOW) == []


def test_non_enabled_rows_ignored():
    """A row still counting down (enabled False) is not a live event."""
    rows = [_row("BBBB", "StillVoting", enabled=False,
                 enabled_iso=None)]
    assert recently_enabled(rows, now=NOW) == []


def test_newest_first_ordering():
    rows = [
        _row("OLD1", "Older", enabled_iso="2026-10-08T01:00:00Z"),
        _row("NEW1", "Newer", enabled_iso="2026-10-09T09:00:00Z"),
        _row("MID1", "Middle", enabled_iso="2026-10-08T20:00:00Z"),
    ]
    out = recently_enabled(rows, now=NOW)
    assert [e["name"] for e in out] == ["Newer", "Middle", "Older"]


def test_window_boundary_inclusive_at_48h_and_excludes_just_past():
    inside = (NOW - dt.timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    outside = (NOW - dt.timedelta(hours=WINDOW_HOURS, seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert len(recently_enabled([_row("IN", "Inside", enabled_iso=inside)], now=NOW)) == 1
    assert recently_enabled([_row("OUT", "Outside", enabled_iso=outside)], now=NOW) == []


def test_row_with_no_timestamp_is_dropped_never_guessed():
    """No enabled_iso and no enabled_close_iso: we cannot prove it is in
    the window, so it must not appear as "just went live"."""
    rows = [_row("CCCC", "NoTimestamp", enabled_iso=None)]
    assert recently_enabled(rows, now=NOW) == []


def test_enabled_close_iso_fallback_for_pre_fix_rows():
    """Rows written before the true-enable-time fix carry only the
    walker's observed close time; still eligible via the fallback."""
    close_iso = (NOW - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [_row("DDDD", "PreFixRow", enabled_iso=None,
                 enabled_close_iso=close_iso)]
    out = recently_enabled(rows, now=NOW)
    assert len(out) == 1
    assert out[0]["enabled_iso"] == close_iso


def test_future_dated_row_dropped():
    """Clock skew / bad row must never render as 'just went live'."""
    future = (NOW + dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert recently_enabled([_row("EEEE", "FromTheFuture", enabled_iso=future)], now=NOW) == []


def test_malformed_timestamp_dropped_without_raising():
    rows = [_row("FFFF", "BadTimestamp", enabled_iso="not-a-date")]
    assert recently_enabled(rows, now=NOW) == []


def test_duplicate_hash_keeps_only_newest():
    """A hash with two enabled epochs in the window renders once, newest."""
    rows = [
        _row("DUP", "Dup", enabled_iso="2026-10-08T05:00:00Z", ledger=1),
        _row("DUP", "Dup", enabled_iso="2026-10-09T05:00:00Z", ledger=2),
    ]
    out = recently_enabled(rows, now=NOW)
    assert len(out) == 1
    assert out[0]["enabled_seen_ledger"] == 2


def test_missing_ledger_or_tx_still_listed():
    """Name + time are enough to announce the activation; the template
    simply omits an absent ledger/tx rather than hiding the amendment."""
    rows = [_row("GGGG", "NoTxYet", enabled_iso="2026-10-09T08:00:00Z")]
    out = recently_enabled(rows, now=NOW)
    assert len(out) == 1
    assert out[0]["enabled_seen_ledger"] is None
    assert out[0]["enabled_tx_hash"] is None


def test_none_input_is_safe():
    assert recently_enabled(None, now=NOW) == []


# ── live_fingerprint: drives the 30s poll's reload decision ──

def _state(enabled_count=95, majority_hashes=()):
    return {
        "enabled_count": enabled_count,
        "majorities": [{"hash": h} for h in majority_hashes],
    }


def test_fingerprint_changes_when_enabled_count_changes():
    a = live_fingerprint(_state(95, ["AAA"]), [])
    b = live_fingerprint(_state(96, ["AAA"]), [])
    assert a != b


def test_fingerprint_changes_when_majorities_list_changes():
    a = live_fingerprint(_state(95, ["AAA", "BBB"]), [])
    b = live_fingerprint(_state(95, ["AAA"]), [])
    assert a != b


def test_fingerprint_changes_when_an_amendment_goes_live():
    """The activation case: hash leaves Majorities and appears in the live
    list. Both halves move, so the fingerprint must differ."""
    before = live_fingerprint(_state(94, ["PD"]), [])
    after = live_fingerprint(
        _state(95, []),
        [{"hash": "PD", "enabled_iso": "2026-10-08T21:29:50Z"}],
    )
    assert before != after


def test_fingerprint_stable_across_reordering():
    """An upstream reordering of identical data must NOT trigger a reload."""
    a = live_fingerprint(_state(95, ["AAA", "BBB"]), [])
    b = live_fingerprint(_state(95, ["BBB", "AAA"]), [])
    assert a == b


def test_fingerprint_stable_when_nothing_changes():
    live = [{"hash": "PD", "enabled_iso": "2026-10-08T21:29:50Z"}]
    assert live_fingerprint(_state(95, ["AAA"]), live) == \
        live_fingerprint(_state(95, ["AAA"]), live)


def test_fingerprint_case_insensitive_on_hashes():
    a = live_fingerprint(_state(95, ["abc"]), [])
    b = live_fingerprint(_state(95, ["ABC"]), [])
    assert a == b


def test_fingerprint_safe_on_empty_and_none():
    assert live_fingerprint(None, None)
    assert live_fingerprint({}, []) == "None||"


# ── pending: ledger says enabled, walker hasn't stamped it yet ──
# The majority walker ticks every ~15 min, so there is a gap between an
# activation and enabled_iso landing in the DB — exactly the window people
# are watching. Announce immediately, fill details in on the next tick.

def _open_row(hash_, name):
    """A tracked countdown row: no enabled stamp, not removed."""
    return _row(hash_, name, enabled=False, enabled_iso=None)


def test_enabled_on_ledger_with_open_row_is_pending_live_now():
    state = {
        "recognized_enabled": [{"hash": "PD", "name": "PermissionDelegationV1_1"}],
        "majorities": [],
    }
    out = recently_enabled([_open_row("PD", "PermissionDelegationV1_1")],
                           now=NOW, state=state)
    assert len(out) == 1
    e = out[0]
    assert e["pending"] is True
    assert e["name"] == "PermissionDelegationV1_1"
    # No guessed time/ledger/tx — the template shows the recording label.
    assert e["enabled_iso"] is None
    assert e["enabled_seen_ledger"] is None
    assert e["enabled_tx_hash"] is None


def test_enabled_on_ledger_while_still_in_majorities_is_pending():
    """Brief overlap: the hash is reported enabled but still sitting in
    the Majorities array."""
    state = {
        "recognized_enabled": [{"hash": "FB", "name": "fixBatchV1_2"}],
        "majorities": [{"hash": "FB", "name": "fixBatchV1_2"}],
    }
    out = recently_enabled([], now=NOW, state=state)
    assert len(out) == 1 and out[0]["pending"] is True
    assert out[0]["name"] == "fixBatchV1_2"


def test_long_enabled_amendments_do_not_flood_the_box():
    """recognized_enabled lists ALL ~95 enabled amendments. Without an
    open row or a Majorities entry, none of them are 'just went live'."""
    state = {
        "recognized_enabled": [{"hash": "OLD%d" % i, "name": "Old%d" % i}
                               for i in range(95)],
        "majorities": [],
    }
    assert recently_enabled([], now=NOW, state=state) == []


def test_already_stamped_hash_is_never_also_pending():
    """Once the walker stamps it, the real row wins — no duplicate row."""
    state = {
        "recognized_enabled": [{"hash": PD_HASH, "name": "PermissionDelegationV1_1"}],
        "majorities": [],
    }
    rows = [_pd_row(), _open_row(PD_HASH, "PermissionDelegationV1_1")]
    out = recently_enabled(rows, now=NOW, state=state)
    assert len(out) == 1
    assert out[0]["pending"] is False
    assert out[0]["enabled_seen_ledger"] == 107524865


def test_stamped_hash_outside_window_still_not_pending():
    """An old activation must not reappear as 'pending' just because it
    is in the live enabled set."""
    state = {
        "recognized_enabled": [{"hash": "AAAA", "name": "Ancient"}],
        "majorities": [{"hash": "AAAA", "name": "Ancient"}],
    }
    rows = [_row("AAAA", "Ancient", enabled_iso="2026-01-01T00:00:00Z")]
    assert recently_enabled(rows, now=NOW, state=state) == []


def test_pending_sorts_above_stamped():
    """A just-activated amendment is the freshest event on the page."""
    state = {
        "recognized_enabled": [{"hash": "NEW", "name": "JustNow"},
                               {"hash": PD_HASH, "name": "PermissionDelegationV1_1"}],
        "majorities": [{"hash": "NEW", "name": "JustNow"}],
    }
    out = recently_enabled([_pd_row()], now=NOW, state=state)
    assert [e["pending"] for e in out] == [True, False]
    assert out[0]["name"] == "JustNow"


def test_state_omitted_keeps_old_behaviour():
    """Callers that pass no state (and the DB-down path) are unchanged."""
    out = recently_enabled([_pd_row()], now=NOW)
    assert len(out) == 1 and out[0]["pending"] is False


def test_fingerprint_changes_when_pending_details_arrive():
    """The second reload: pending -> stamped must change the fingerprint,
    or the page would never pick up the real time/ledger/tx."""
    state = {"enabled_count": 95, "majorities": []}
    pending = [{"hash": PD_HASH, "enabled_iso": None, "pending": True}]
    stamped = [{"hash": PD_HASH, "enabled_iso": "2026-10-08T21:29:50Z",
                "pending": False}]
    assert live_fingerprint(state, pending) != live_fingerprint(state, stamped)


def test_fingerprint_stable_while_still_pending():
    """No reload loop while waiting for the walker."""
    state = {"enabled_count": 95, "majorities": []}
    pending = [{"hash": PD_HASH, "enabled_iso": None, "pending": True}]
    assert live_fingerprint(state, pending) == live_fingerprint(state, list(pending))
