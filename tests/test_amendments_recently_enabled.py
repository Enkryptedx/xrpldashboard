"""Data-driven "Recently enabled" list (branch
amendments-recently-enabled-2026-10-09).

Covers amendments_live_banner.recently_enabled_archive: the same LEDGER
TRUTH rows as the 48h "Just went LIVE" banner, but over a 30-day window,
newest first, minus whatever the banner already shows — so one activation
never appears twice on the page.

Fakes only: plain dicts shaped like app._load_amendment_majority_history()
rows. No DB, no network, no production DATABASE_URL (owner rule
2026-10-08).
"""
import datetime as dt

from amendments_live_banner import (
    ARCHIVE_WINDOW_HOURS,
    recently_enabled,
    recently_enabled_archive,
)

NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.timezone.utc)

PD_HASH = "0F48FF561C709540328F31F1C97FD512ACC8B4E42138A161CB0E21ECA292540B"
PD_TX = "478284066BA0B83CC35CA1174F667F43D9577782F221440AFCC5541788F25A77"


def _row(hash_, name, enabled_iso, ledger=None, tx=None, enabled=True):
    return {
        "hash": hash_, "name": name, "enabled": enabled,
        "enabled_iso": enabled_iso, "enabled_close_iso": None,
        "enabled_seen_ledger": ledger, "enabled_tx_hash": tx,
        "majority_close_iso": "2026-09-01T00:00:00Z",
        "removed_iso": None, "active": not enabled,
    }


def _ago(**kw):
    return (NOW - dt.timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_window_is_30_days():
    assert ARCHIVE_WINDOW_HOURS == 720


def test_lists_an_activation_from_ten_days_ago():
    rows = [_row("AAAA", "TenDaysAgo", _ago(days=10), ledger=107000000,
                 tx="ab" * 32)]
    out = recently_enabled_archive(rows, now=NOW)
    assert len(out) == 1
    assert out[0]["name"] == "TenDaysAgo"
    assert out[0]["enabled_seen_ledger"] == 107000000
    assert out[0]["enabled_tx_hash"] == "ab" * 32


def test_excludes_activations_older_than_30_days():
    rows = [_row("OLD", "ThirtyOneDays", _ago(days=31))]
    assert recently_enabled_archive(rows, now=NOW) == []


def test_newest_first():
    rows = [
        _row("A", "Oldest", _ago(days=20)),
        _row("B", "Newest", _ago(days=3)),
        _row("C", "Middle", _ago(days=9)),
    ]
    assert [e["name"] for e in recently_enabled_archive(rows, now=NOW)] == [
        "Newest", "Middle", "Oldest"]


def test_excludes_hashes_the_banner_is_already_showing():
    """PD went live inside 48h, so the banner owns it; it must not also
    appear in the archive list."""
    rows = [
        _row(PD_HASH, "PermissionDelegationV1_1", _ago(hours=14),
             ledger=107524865, tx=PD_TX),
        _row("BBBB", "FiveDaysAgo", _ago(days=5)),
    ]
    banner = recently_enabled(rows, now=NOW)
    assert [e["name"] for e in banner] == ["PermissionDelegationV1_1"]
    out = recently_enabled_archive(
        rows, exclude_hashes=[e["hash"] for e in banner], now=NOW)
    assert [e["name"] for e in out] == ["FiveDaysAgo"]


def test_exclusion_is_case_insensitive():
    rows = [_row("abcd", "Lower", _ago(days=2))]
    assert recently_enabled_archive(rows, exclude_hashes=["ABCD"],
                                    now=NOW) == []


def test_after_48h_it_moves_from_banner_into_the_list():
    """The handover: at 47h the banner owns it, at 49h the list does."""
    at47 = [_row("X", "Mover", _ago(hours=47))]
    at49 = [_row("X", "Mover", _ago(hours=49))]

    b47 = recently_enabled(at47, now=NOW)
    a47 = recently_enabled_archive(
        at47, exclude_hashes=[e["hash"] for e in b47], now=NOW)
    assert [e["name"] for e in b47] == ["Mover"] and a47 == []

    b49 = recently_enabled(at49, now=NOW)
    a49 = recently_enabled_archive(
        at49, exclude_hashes=[e["hash"] for e in b49], now=NOW)
    assert b49 == [] and [e["name"] for e in a49] == ["Mover"]


def test_non_enabled_rows_ignored():
    rows = [_row("Y", "StillVoting", None, enabled=False)]
    assert recently_enabled_archive(rows, now=NOW) == []


def test_undated_row_never_appears_in_a_dated_list():
    rows = [_row("Z", "NoTimestamp", None)]
    assert recently_enabled_archive(rows, now=NOW) == []


def test_pending_entries_never_reach_the_archive():
    """A ledger-enabled amendment the walker hasn't stamped has no time,
    so it must not appear in a dated archive list even though the banner
    would show it as pending."""
    open_row = _row("PENDING", "NotStampedYet", None, enabled=False)
    state = {"recognized_enabled": [{"hash": "PENDING", "name": "NotStampedYet"}],
             "majorities": [{"hash": "PENDING"}]}
    banner = recently_enabled([open_row], now=NOW, state=state)
    assert len(banner) == 1 and banner[0]["pending"] is True
    assert recently_enabled_archive([open_row], now=NOW) == []


def test_empty_and_none_safe():
    assert recently_enabled_archive([], now=NOW) == []
    assert recently_enabled_archive(None, now=NOW) == []
