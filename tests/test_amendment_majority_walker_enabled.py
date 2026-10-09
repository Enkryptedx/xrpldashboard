"""amendment_majority_walker — ENABLED vs LOST (Charlie fix 2026-10-07).

Before this fix every open history row whose hash left the ledger's
Majorities array was stamped removed_* ("majority lost"), including the one
case that matters most: the 14-day window completing and the amendment
turning ON (hash moves Majorities -> Amendments). These tests are hermetic:
no DB, no network. The RPC layer (`_post`) and `db.pg_connect` are replaced
with fakes that record every SQL statement + its parameters.

(a) hash moves to Amendments            -> enabled_* stamped, removed_* untouched
(b) hash vanishes from BOTH arrays      -> removed_* stamped, enabled_* untouched
(c) two amendments enable on DIFFERENT  -> each row carries its OWN ledger
    flag ledgers (two walker runs)         index + close time
Plus: the /amendments history row and the dated permalink render the
enabled state (ledger + time, ET first, UTC in parens) and never say "lost".
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "scripts"))

import amendment_majority_walker as W  # noqa: E402

PD = "0F48FF561C709540328F31F1C97FD512ACC8B4E42138A161CB0E21ECA292540B"
FIXB = "14A2B45E48A4A124D1BBA657AC7B0DC3D5EA8C256C89E8F0D8142D32960A7944"
BATCH = "9F287AED3CDB50A7BD1ACEC24296A30C9B5230CCD136219317AC790E3B884377"
PD_CT = 843600301      # 2026-09-24T21:25:01Z
FIXB_CT = 843660771    # 2026-09-25T14:12:51Z
BATCH_CT = 843662762   # 2026-09-25T14:46:02Z


# --------------------------------------------------------------------- fakes
class _FakeCursor:
    def __init__(self, open_rows):
        self.open_rows = open_rows
        self.executed = []  # (sql, params)
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        self._last = sql

    def fetchall(self):
        if "removed_seen_ledger IS NULL" in self._last and "SELECT" in self._last:
            return list(self.open_rows)
        return []

    def fetchone(self):
        return None  # _vote_at -> (None, None)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self.cur = cur
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ledger_entry(majorities, enabled):
    return {
        "ledger_index": None,
        "node": {
            "Majorities": [{"Majority": {"Amendment": h, "CloseTime": ct}}
                           for h, ct in majorities],
            "Amendments": list(enabled),
        },
    }


def _install_rpc(monkeypatch, *, ledger_index, ledger_close, majorities, enabled):
    """Patch W._post so read_state() sees a fixed ledger."""
    le = _ledger_entry(majorities, enabled)
    le["ledger_index"] = ledger_index

    def fake_post(method, params):
        if method == "feature":
            return {"features": {
                PD: {"name": "PermissionDelegationV1_1"},
                FIXB: {"name": "fixBatchV1_2"},
                BATCH: {"name": "BatchV1_1"},
            }}, "fake"
        if method == "ledger_entry":
            return le, "fake"
        if method == "ledger":
            return {"ledger": {"close_time": ledger_close}}, "fake"
        raise AssertionError(method)

    monkeypatch.setattr(W, "_post", fake_post)


def _install_db(monkeypatch, open_rows):
    cur = _FakeCursor(open_rows)
    conn = _FakeConn(cur)
    monkeypatch.setattr(W.db, "pg_connect", lambda: conn)
    return cur, conn


def _updates(cur, column):
    """UPDATE statements that SET the given column, as (params) list."""
    return [p for sql, p in cur.executed
            if sql.startswith("UPDATE amendment_majority_history")
            and f"SET {column}" in sql]


# ---------------------------------------------------------- pure classifier
def test_classify_departures_pure():
    open_rows = [(PD, PD_CT), (FIXB, FIXB_CT), (BATCH, BATCH_CT)]
    present = {(BATCH, BATCH_CT)}           # Batch still counting down
    enabled = {PD}                           # PD moved to Amendments
    went_enabled, went_lost = W.classify_departures(open_rows, present, enabled)
    assert went_enabled == [(PD, PD_CT)]
    assert went_lost == [(FIXB, FIXB_CT)]    # in neither array -> lost


def test_classify_is_case_insensitive_on_hash():
    went_enabled, went_lost = W.classify_departures(
        [(PD.lower(), PD_CT)], set(), {PD})
    assert went_enabled == [(PD.lower(), PD_CT)] and went_lost == []


# ------------------------------------------------------------ (a) ENABLED
def test_a_hash_moves_to_amendments_is_recorded_enabled_not_lost(monkeypatch):
    # Ledger after PD's enabling flag ledger: PD gone from Majorities, present
    # in Amendments; the two Friday amendments still in Majorities.
    _install_rpc(monkeypatch, ledger_index=107_523_650, ledger_close=844_809_321,
                 majorities=[(FIXB, FIXB_CT), (BATCH, BATCH_CT)], enabled=[PD])
    cur, conn = _install_db(monkeypatch, open_rows=[
        (PD, PD_CT), (FIXB, FIXB_CT), (BATCH, BATCH_CT)])

    res = W.run()

    assert res["enabled"] == 1 and res["removed"] == 0
    en = _updates(cur, "enabled_seen_ledger")
    assert len(en) == 1
    lidx, lclose, liso, _tx, _now, h, ct = en[0]  # 2026-10-08: + enabled_tx_hash
    assert (h, ct) == (PD, PD_CT)
    assert lidx == 107_523_650 and lclose == 844_809_321
    assert liso == W._iso(844_809_321)
    assert _updates(cur, "removed_seen_ledger") == []
    assert conn.committed
    # The two still-present rows were upserted (last_seen advanced), not closed.
    inserts = [p for sql, p in cur.executed if sql.startswith("INSERT INTO")]
    assert {p[0] for p in inserts} == {FIXB, BATCH}


def test_enabled_sql_guards_do_not_restamp_or_double_close(monkeypatch):
    _install_rpc(monkeypatch, ledger_index=1, ledger_close=1,
                 majorities=[], enabled=[PD])
    cur, _ = _install_db(monkeypatch, open_rows=[(PD, PD_CT)])
    W.run()
    sel = [sql for sql, _ in cur.executed if sql.startswith("SELECT amendment_hash")]
    assert sel and "enabled_seen_ledger IS NULL" in sel[0], \
        "already-enabled rows must not be re-selected as open"
    for sql, _ in cur.executed:
        if sql.startswith("UPDATE"):
            assert "removed_seen_ledger IS NULL" in sql
            assert "enabled_seen_ledger IS NULL" in sql


# --------------------------------------------------------------- (b) LOST
def test_b_hash_in_neither_array_is_recorded_lost(monkeypatch):
    # Support dropped: Batch left Majorities and is NOT in Amendments.
    _install_rpc(monkeypatch, ledger_index=107_228_160, ledger_close=843_662_762,
                 majorities=[(PD, PD_CT), (FIXB, FIXB_CT)], enabled=[])
    cur, _ = _install_db(monkeypatch, open_rows=[
        (PD, PD_CT), (FIXB, FIXB_CT), (BATCH, 842_796_401)])

    res = W.run()

    assert res["removed"] == 1 and res["enabled"] == 0
    rm = _updates(cur, "removed_seen_ledger")
    assert len(rm) == 1
    lidx, lclose, _iso, _now, h, ct = rm[0]
    assert (h, ct) == (BATCH, 842_796_401)
    assert lidx == 107_228_160 and lclose == 843_662_762
    assert _updates(cur, "enabled_seen_ledger") == []


def test_b_regained_epoch_does_not_mark_old_epoch_enabled(monkeypatch):
    # Classic reset: same hash, NEW CloseTime in Majorities. The OLD epoch
    # row must be stamped LOST (the hash is not enabled), never ENABLED.
    _install_rpc(monkeypatch, ledger_index=5, ledger_close=5,
                 majorities=[(PD, PD_CT)], enabled=[])
    cur, _ = _install_db(monkeypatch, open_rows=[(PD, 843_305_920)])  # old epoch
    res = W.run()
    assert res["removed"] == 1 and res["enabled"] == 0
    assert _updates(cur, "removed_seen_ledger")[0][4:] == (PD, 843_305_920)


# -------------------------------------- (c) two enabling on different flags
def test_c_two_amendments_enable_on_different_flag_ledgers(monkeypatch):
    # Run 1 — Fri Oct 9 ~14:29Z flag: fixBatchV1_2 enabled, Batch still counting.
    _install_rpc(monkeypatch, ledger_index=107_539_200, ledger_close=844_870_200,
                 majorities=[(BATCH, BATCH_CT)], enabled=[PD, FIXB])
    cur1, _ = _install_db(monkeypatch, open_rows=[(FIXB, FIXB_CT), (BATCH, BATCH_CT)])
    r1 = W.run()
    assert r1 == {"ledger": 107_539_200, "present": 1, "wrote": 1,
                  "enabled": 1, "removed": 0, "backfilled": 0}
    en1 = _updates(cur1, "enabled_seen_ledger")
    # 2026-10-08: params gained enabled_tx_hash at index 3; hash/ct moved to 5/6.
    # The fake RPC serves no transactions, so the walker falls back to the
    # observed ledger — exactly the pre-change values.
    assert [(p[5], p[6], p[0], p[1]) for p in en1] == \
        [(FIXB, FIXB_CT, 107_539_200, 844_870_200)]

    # Run 2 — ~34 min later, next-but-one flag ledger: Batch enabled too.
    # The FIXB row is already closed (enabled_seen_ledger set), so the DB's
    # open-row query no longer returns it.
    _install_rpc(monkeypatch, ledger_index=107_539_712, ledger_close=844_872_240,
                 majorities=[], enabled=[PD, FIXB, BATCH])
    cur2, _ = _install_db(monkeypatch, open_rows=[(BATCH, BATCH_CT)])
    r2 = W.run()
    assert r2 == {"ledger": 107_539_712, "present": 0, "wrote": 0,
                  "enabled": 1, "removed": 0, "backfilled": 0}
    en2 = _updates(cur2, "enabled_seen_ledger")
    assert [(p[5], p[6], p[0], p[1]) for p in en2] == \
        [(BATCH, BATCH_CT, 107_539_712, 844_872_240)]

    # Different ledgers, different close times — never shared.
    assert en1[0][0] != en2[0][0] and en1[0][1] != en2[0][1]
    # Nothing was ever marked lost across either run.
    assert _updates(cur1, "removed_seen_ledger") == []
    assert _updates(cur2, "removed_seen_ledger") == []


def test_read_state_returns_enabled_set_uppercased(monkeypatch):
    _install_rpc(monkeypatch, ledger_index=9, ledger_close=9,
                 majorities=[(BATCH, BATCH_CT)], enabled=[PD.lower()])
    lidx, lclose, _src, maj, enabled = W.read_state()
    assert lidx == 9 and lclose == 9
    assert maj == {BATCH: ("BatchV1_1", BATCH_CT)}
    assert enabled == {PD}


# ----------------------------------------------------- page + permalink text
def _enabled_row():
    return {
        "name": "PermissionDelegationV1_1", "hash": PD,
        "majority_close_iso": "2026-09-24T21:25:01Z",
        "activation_eta_iso": "2026-10-08T21:25:01Z",
        "first_seen_iso": "2026-09-24T21:25:10Z",
        "first_seen_ledger": 107212033,
        "first_seen_close_iso": "2026-09-24T21:25:10Z",
        "first_seen_is_flag": True,
        "removed_iso": None, "removed_seen_ledger": None, "removed_close_iso": None,
        "active": False, "enabled": True,
        "enabled_iso": "2026-10-08T21:42:21Z",
        "enabled_seen_ledger": 107523650,
        "enabled_close_iso": "2026-10-08T21:42:21Z",
        "vote_count_at_first": 29, "unl_threshold": 28,
        "correction_note": None,
    }


def _render_history(rows):
    import app
    from flask import render_template
    state = {
        "ok": True, "enabled_count": 95, "in_flight_count": 9,
        "ledger_index": 107523650, "recognized_enabled": [],
        "unrecognized_enabled": [], "unrecognized_enabled_count": 0,
        "in_flight": [], "superseded": [], "majorities": [],
        "network_votes_source": {}, "in_development": [],
    }
    with app.app.test_request_context("/amendments"):
        return render_template(
            "amendments.html", state=state, majority_history=rows,
            page_sourcing="sovereign", roll_call=None, flag_counter=None,
            cite=None, cache_ttl_seconds=300)


def test_history_row_renders_enabled_state_et_first_and_never_lost():
    html = _render_history([_enabled_row()])
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    # Find THIS amendment's history row and check its wording.
    i = text.index("PermissionDelegationV1_1 enabled on ledger")
    seg = text[i:i + 900]
    assert "Enabled Thu Oct 8, 5:42 PM ET (21:42 UTC)" in seg
    assert "the 14-day window completed and the amendment is live on mainnet" in seg
    assert "recorded enabled by our node at ledger 107,523,650" in seg
    assert "first seen in the Amendments array at ledger 107523650" in seg
    assert "majority lost" not in seg
    assert "Lost" not in seg
    assert "Projected activation" not in seg
    assert "support dropped" not in seg
    assert 'data-enabled-iso="2026-10-08T21:42:21Z"' in html
    # An enabled epoch must not carry a live activation countdown attribute.
    assert 'data-activation-iso="2026-10-08T21:25:01Z"' not in html


def test_history_row_lost_wording_unchanged_for_real_loss():
    row = _enabled_row()
    row.update({"enabled": False, "enabled_iso": None, "enabled_seen_ledger": None,
                "enabled_close_iso": None,
                "removed_iso": "2026-09-23T12:47:20Z", "removed_seen_ledger": 107181569,
                "removed_close_iso": "2026-09-23T12:47:20Z"})
    html = _render_history([row])
    assert "majority lost — superseded" in html
    assert "enabled on ledger" not in html


def test_permalink_emits_enabled_event_for_that_utc_day():
    import amendments_permalink as P
    ev = P.majority_events_for_day([_enabled_row()], "2026-10-08")
    kinds = [e["kind"] for e in ev]
    assert kinds == ["enabled"]
    assert ev[0]["ledger"] == 107523650
    assert ev[0]["close_iso"] == "2026-10-08T21:42:21Z"
    assert "majority lost" not in kinds
    # The gained event lives on its own day, not on activation day.
    assert [e["kind"] for e in P.majority_events_for_day([_enabled_row()], "2026-09-24")] \
        == ["majority gained"]


def test_route_drops_enabled_hash_from_roll_call_headline_map():
    """The 'Holding / Countdown restarted' headline map must not carry an
    enabled hash (it is neither). Mirrors the loop in app.amendments()."""
    rows = [_enabled_row(), {**_enabled_row(), "enabled": False, "active": False,
                             "removed_iso": "2026-09-23T12:47:20Z",
                             "majority_close_iso": "2026-09-21T11:18:40Z"}]
    majority_active, enabled_hist = {}, set()
    for mh in rows:
        h = mh["hash"].upper()
        if mh.get("enabled"):
            enabled_hist.add(h)
            continue
        if h not in majority_active:
            majority_active[h] = bool(mh.get("active"))
        elif mh.get("active"):
            majority_active[h] = True
    for h in enabled_hist:
        majority_active.pop(h, None)
    assert majority_active == {}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
