"""/changes + What's new: amendment activations (Charlie 2026-10-07).

Before: changes_builder hard-coded `amendments` into categories_no_change,
so an activation day read "No change: … amendments …". Now an amendment
whose amendment_majority_history.enabled_iso falls inside the day's
leaf-to-leaf window (previous distinct leaf's snapshot_taken_unix <
enabled <= today's) emits one item; otherwise the category stays quiet.

Hermetic: a fake PG cursor serves signed_snapshots, unl_snapshots and
amendment_majority_history. No DB, no network.

Cases: one in window (shows) · one outside (doesn't) · two in one window
(both show, ledger order — the Friday case) · none (no change) · strip
slotting · ET-first wording · proof link.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import changes_builder as CB  # noqa: E402

PD = "0F48FF561C709540328F31F1C97FD512ACC8B4E42138A161CB0E21ECA292540B"
FIXB = "14A2B45E48A4A124D1BBA657AC7B0DC3D5EA8C256C89E8F0D8142D32960A7944"
BATCH = "9F287AED3CDB50A7BD1ACEC24296A30C9B5230CCD136219317AC790E3B884377"

# Leaves: 9:00 PM ET = 01:00 UTC next day.
LEAF_OCT8 = 1791421200   # 2026-10-08T01:00:00Z = 9 PM ET Oct 7
LEAF_OCT9 = 1791507600   # 2026-10-09T01:00:00Z = 9 PM ET Oct 8
LEAF_OCT10 = 1791594000  # 2026-10-10T01:00:00Z = 9 PM ET Oct 9


def _leaf(taken, pools=30_368):
    # leaf_index derived from the instant so consecutive leaves chain cleanly
    # (otherwise a fake chain discontinuity anomaly would outrank the item).
    return {
        "snapshot_taken_unix": taken,
        "chain_root": "abc", "leaf_index": 100 + (taken - LEAF_OCT8) // 86400,
        "previous_leaf_hash": "p",
        "metrics": [
            {"name": "amm_pools_count", "value": pools, "source": "amm_ranked_finished.json"},
        ],
    }


def _row(name, h, enabled_iso, ledger, majority_iso):
    return (name, h, enabled_iso, ledger, majority_iso)


class _Cur:
    """Routes by SQL text: signed_snapshots / unl_snapshots by date param,
    amendment_majority_history by its enabled_iso filter."""
    def __init__(self, leaves, amend_rows):
        self.leaves, self.amend_rows, self._one, self._all = leaves, amend_rows, None, None

    def execute(self, sql, params=None):
        self._one = self._all = None
        if "signed_snapshots" in sql:
            v = self.leaves.get(params[0]); self._one = (v,) if v is not None else None
        elif "amendment_majority_history" in sql:
            assert "enabled_iso IS NOT NULL" in sql
            self._all = list(self.amend_rows)
        else:  # unl_snapshots etc.
            self._one = None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all or []


class _Conn:
    def __init__(self, cur): self.cur = cur
    def cursor(self): return self
    def __enter__(self): return self.cur
    def __exit__(self, *a): return False


class _Pg:
    def __init__(self, leaves, rows): self.leaves, self.rows = leaves, rows
    def __call__(self): return self
    def __enter__(self): return _Conn(_Cur(self.leaves, self.rows))
    def __exit__(self, *a): return False


def _build(date, leaves, rows):
    return CB.build_changes_for_date(date, pg_connect=_Pg(leaves, rows))


def _amend_items(env):
    return [c for c in env["changes"] if c["category"] == "amendments"]


# ------------------------------------------------------------ pure emitter
def test_pure_window_is_half_open_prev_excluded_today_included():
    rows = [{"name": "A", "hash": PD, "enabled_iso": "2026-10-08T01:00:00Z", "enabled_seen_ledger": 1},
            {"name": "B", "hash": FIXB, "enabled_iso": "2026-10-09T01:00:00Z", "enabled_seen_ledger": 2}]
    items = CB._amendment_enabled_deltas(rows, LEAF_OCT8, LEAF_OCT9)
    assert [i["amendment_name"] for i in items] == ["B"]   # == prev leaf is out, == today leaf is in


def test_pure_no_today_leaf_emits_nothing():
    rows = [{"name": "A", "hash": PD, "enabled_iso": "2026-10-08T21:42:21Z", "enabled_seen_ledger": 1}]
    assert CB._amendment_enabled_deltas(rows, LEAF_OCT8, None) == []


def test_pure_no_prev_leaf_uses_24h_window():
    rows = [{"name": "A", "hash": PD, "enabled_iso": "2026-10-08T21:42:21Z", "enabled_seen_ledger": 1},
            {"name": "Old", "hash": FIXB, "enabled_iso": "2026-10-07T12:00:00Z", "enabled_seen_ledger": 2}]
    items = CB._amendment_enabled_deltas(rows, None, LEAF_OCT9)
    assert [i["amendment_name"] for i in items] == ["A"]


# ---------------------------------------------------- (1) one in window
def test_one_enabled_in_window_shows_with_et_first_time_ledger_and_proof():
    rows = [_row("PermissionDelegationV1_1", PD, "2026-10-08T21:42:21Z", 107_523_650,
                 "2026-09-24T21:25:01Z")]
    env = _build(dt.date(2026, 10, 9),
                 {dt.date(2026, 10, 8): _leaf(LEAF_OCT8), dt.date(2026, 10, 9): _leaf(LEAF_OCT9, 30_369)},
                 rows)
    items = _amend_items(env)
    assert len(items) == 1
    it = items[0]
    assert it["line"] == (
        "Amendment enabled: PermissionDelegationV1_1 is live on mainnet — "
        "recorded by our node at ledger 107,523,650, 5:42 PM ET Oct 8 (21:42 UTC Oct 8).")
    assert it["detail"] == (
        "Majority reached 5:25 PM ET Sep 24 (21:25 UTC Sep 24); the 14-day window "
        "completed and the amendment is live on mainnet.")
    assert it["prove_url"] == "/amendments/2026-10-08"
    assert it["source"] == "amendment_majority_history"
    assert it["metric_type"] == "amendment_enabled"
    assert it["enabled_seen_ledger"] == 107_523_650
    assert "amendments" not in env["categories_no_change"]
    # still flags the other v2 slots
    assert "whales" in env["categories_no_change"]
    assert "curator_decisions" in env["categories_no_change"]


# --------------------------------------------------- (2) outside window
def test_enabled_outside_window_does_not_show_and_category_is_no_change():
    rows = [_row("PermissionDelegationV1_1", PD, "2026-10-08T21:42:21Z", 107_523_650,
                 "2026-09-24T21:25:01Z")]
    # The NEXT day's build (window 9 PM ET Oct 8 -> 9 PM ET Oct 9): PD is before it.
    env = _build(dt.date(2026, 10, 10),
                 {dt.date(2026, 10, 9): _leaf(LEAF_OCT9), dt.date(2026, 10, 10): _leaf(LEAF_OCT10, 30_370)},
                 rows)
    assert _amend_items(env) == []
    assert "amendments" in env["categories_no_change"]
    # And the PREVIOUS day's build (window ending 9 PM ET Oct 7): PD is after it.
    env2 = _build(dt.date(2026, 10, 8),
                  {dt.date(2026, 10, 7): _leaf(LEAF_OCT8 - 86400), dt.date(2026, 10, 8): _leaf(LEAF_OCT8, 30_369)},
                  rows)
    assert _amend_items(env2) == []
    assert "amendments" in env2["categories_no_change"]


# ------------------------------------------- (3) two in one window (Fri)
def test_two_enabled_in_one_window_both_show_in_ledger_order():
    rows = [
        _row("BatchV1_1", BATCH, "2026-10-09T15:12:40Z", 107_539_712, "2026-09-25T14:46:02Z"),
        _row("fixBatchV1_2", FIXB, "2026-10-09T14:38:10Z", 107_539_200, "2026-09-25T14:12:51Z"),
        # Thursday's PD must NOT leak into Friday's page.
        _row("PermissionDelegationV1_1", PD, "2026-10-08T21:42:21Z", 107_523_650, "2026-09-24T21:25:01Z"),
    ]
    env = _build(dt.date(2026, 10, 10),
                 {dt.date(2026, 10, 9): _leaf(LEAF_OCT9), dt.date(2026, 10, 10): _leaf(LEAF_OCT10, 30_370)},
                 rows)
    items = _amend_items(env)
    assert [i["amendment_name"] for i in items] == ["fixBatchV1_2", "BatchV1_1"]
    assert [i["enabled_seen_ledger"] for i in items] == [107_539_200, 107_539_712]
    assert items[0]["line"].endswith("10:38 AM ET Oct 9 (14:38 UTC Oct 9).")
    assert items[1]["line"].endswith("11:12 AM ET Oct 9 (15:12 UTC Oct 9).")
    assert all(i["prove_url"] == "/amendments/2026-10-09" for i in items)
    assert "amendments" not in env["categories_no_change"]


# ------------------------------------------------------------- (4) none
def test_no_enabled_rows_keeps_no_change_and_rest_of_feed_unchanged():
    leaves = {dt.date(2026, 10, 8): _leaf(LEAF_OCT8), dt.date(2026, 10, 9): _leaf(LEAF_OCT9, 30_369)}
    env = _build(dt.date(2026, 10, 9), leaves, [])
    assert _amend_items(env) == []
    assert env["categories_no_change"][:1] == ["registry"]
    assert "amendments" in env["categories_no_change"]
    # the AMM pools delta still emits exactly as before
    assert any(c["category"] == "amm" for c in env["changes"])


def test_loader_failure_is_soft():
    class BadCur:
        def execute(self, *a): raise RuntimeError("column does not exist")
    assert CB._load_enabled_amendments(BadCur()) == []


# ----------------------------------------------------------- strip + page
def test_strip_slots_activation_as_anomaly_first():
    rows = [_row("PermissionDelegationV1_1", PD, "2026-10-08T21:42:21Z", 107_523_650,
                 "2026-09-24T21:25:01Z")]
    env = _build(dt.date(2026, 10, 9),
                 {dt.date(2026, 10, 8): _leaf(LEAF_OCT8), dt.date(2026, 10, 9): _leaf(LEAF_OCT9, 40_000)},
                 rows)
    strip = CB.build_strip(env)
    slots = strip["slots"] if isinstance(strip, dict) else strip
    first = slots[0]
    assert first["category"] == "amendments" and first["kind"] == "anomaly"
    assert first["line"].startswith("Amendment enabled: PermissionDelegationV1_1 is live on mainnet")
    assert first["prove_url"] == "/amendments/2026-10-08"


def test_changes_page_renders_item_and_drops_amendments_from_no_change():
    import app
    from flask import render_template
    rows = [_row("PermissionDelegationV1_1", PD, "2026-10-08T21:42:21Z", 107_523_650,
                 "2026-09-24T21:25:01Z")]
    env = _build(dt.date(2026, 10, 9),
                 {dt.date(2026, 10, 8): _leaf(LEAF_OCT8), dt.date(2026, 10, 9): _leaf(LEAF_OCT9, 30_369)},
                 rows)
    with app.app.test_request_context("/changes/2026-10-09"):
        html = render_template("changes.html", envelope=env, strip=None,
                               prev_date="2026-10-08", next_date=None, is_latest=True,
                               disclosed_corrections=[])
    assert "Amendment enabled: PermissionDelegationV1_1 is live on mainnet" in html
    assert "5:42 PM ET Oct 8 (21:42 UTC Oct 8)" in html
    assert 'href="/amendments/2026-10-08"' in html
    import re
    quiet = re.search(r"No change:?</strong>:?\s*([^<]+)", html)
    assert quiet and "amendments" not in quiet.group(1)
