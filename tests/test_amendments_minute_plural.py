"""/amendments minute/minutes wording (Charlie 2026-10-07).

The roll-call card printed "in about 1 minutes", "0 minutes ago" and
"minutes remaining 1.0". Approved wording:
  1  -> "in about 1 minute" / "1 minute ago"
  0  -> "in less than a minute" / "less than a minute ago"
  2+ -> unchanged ("minutes")
Rendered through the real template with ngettext so translations keep a
plural form. Three sites: next-roll-call ETA, last-round age, and the
flag-ledger "minutes remaining" metric (rendered twice on the page).
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_amendments_page_changes import _full_roll_call, _render_amendments  # noqa: E402


def _text(html: str) -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _page(next_eta_min, age_min, minutes_remaining, stale=False):
    rc = _full_roll_call()
    rc["next_eta_min"] = next_eta_min
    rc["age_min"] = age_min
    rc["stale"] = stale  # the "last round observed … ago" line only renders when stale
    fc = {"next_flag_ledger": 107393279, "ledgers_remaining": 3,
          "minutes_remaining": minutes_remaining,
          "next_flag_iso": "2026-10-02T20:10:00Z", "seconds_per_ledger": 3.86}
    return _text(_render_amendments(rc, flag_counter=fc))


def test_zero_minutes():
    t = _page(0, 0, 0.5)
    assert ", in less than a minute." in t
    assert "last round observed less than a minute ago;" in _page(0, 0, 0.5, stale=True)
    assert "less than a minute remaining" in t
    assert "0 minutes" not in t and "in about 0 minutes" not in t


def test_one_minute():
    t = _page(1, 1, 1.0)
    assert ", in about 1 minute." in t
    assert "last round observed 1 minute ago;" in _page(1, 1, 1.0, stale=True)
    assert "1.0 minute remaining" in t
    assert "1 minutes" not in t and "1.0 minutes" not in t


def test_two_plus_minutes_unchanged():
    t = _page(2, 2, 10.7)
    assert ", in about 2 minutes." in t
    assert "last round observed 2 minutes ago;" in _page(2, 2, 10.7, stale=True)
    assert "10.7 minutes remaining" in t
    t12 = _page(12, 17, 12.0)
    assert ", in about 12 minutes." in t12
    assert "last round observed 17 minutes ago;" in _page(12, 17, 12.0, stale=True)
    assert "12.0 minutes remaining" in t12


def test_minutes_remaining_value_keeps_mono_markup():
    html = _render_amendments(_full_roll_call(), flag_counter={
        "next_flag_ledger": 107393279, "ledgers_remaining": 3,
        "minutes_remaining": 10.7, "next_flag_iso": "2026-10-02T20:10:00Z",
        "seconds_per_ledger": 3.86})
    assert '<span class="mono">10.7</span> minutes remaining' in html
    assert "&lt;span" not in html


def test_eta_none_prints_nothing():
    rc = _full_roll_call()
    rc["next_eta_min"] = None
    t = _text(_render_amendments(rc))
    assert "in about" not in t and "in less than a minute" not in t
