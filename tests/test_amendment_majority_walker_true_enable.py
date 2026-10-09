"""amendment_majority_walker: record the TRUE enable point (Charlie 2026-10-08).

PermissionDelegationV1_1 was enabled by an EnableAmendment pseudo-tx in
ledger 107,524,865 (close 2026-10-08T21:29:50Z); the 15-minute walker
stamped the first ledger it *observed* the hash in Amendments (107,524,984,
21:37:31Z). Now: when stamping ENABLED, walk back over flag ledgers to find
the pseudo-tx and record its ledger / close / hash; rows already stamped
without a tx hash are corrected on the next run. Hermetic: fake RPC + DB.
"""
from __future__ import annotations
import os, sys
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "scripts"))
import amendment_majority_walker as W  # noqa: E402
from tests.test_amendment_majority_walker_enabled import (  # noqa: E402
    PD, PD_CT, BATCH, BATCH_CT, _FakeCursor, _FakeConn, _ledger_entry, _updates)

PD_HASH = PD
TRUE_LEDGER = 107_524_865          # flag 107,524,864 + 1
TRUE_CLOSE = 844_810_190           # 2026-10-08T21:29:50Z in XRPL epoch
TRUE_TX = "478284066BA0B83CC35CA1174F667F43D9577782F221440AFCC5541788F25A77"
OBSERVED_LEDGER = 107_524_984
OBSERVED_CLOSE = 844_810_651        # 21:37:31Z


def _rpc(monkeypatch, *, enabled, majorities, observed=OBSERVED_LEDGER, with_tx=True,
         flags=None, calls=None):
    le = _ledger_entry(majorities, enabled); le["ledger_index"] = observed

    def fake_post(method, params):
        if calls is not None:
            calls.append((method, dict(params)))
        if method == "feature":
            return {"features": {PD: {"name": "PermissionDelegationV1_1"}, BATCH: {"name": "BatchV1_1"}}}, "fake"
        if method == "ledger_entry":
            return le, "fake"
        if method == "ledger":
            li = params.get("ledger_index")
            if li == observed:
                return {"ledger": {"close_time": OBSERVED_CLOSE, "ledger_index": observed}}, "fake"
            if params.get("transactions") and li == TRUE_LEDGER and with_tx:
                tx = {"hash": TRUE_TX, "tx_json": {"TransactionType": "EnableAmendment", "Amendment": PD_HASH}}
                if flags is not None:
                    tx["tx_json"]["Flags"] = flags
                return {"ledger": {"close_time": TRUE_CLOSE, "ledger_index": li,
                                   "transactions": [tx, {"hash": "X", "tx_json": {"TransactionType": "Payment"}}]}}, "fake"
            return {"ledger": {"close_time": 1, "ledger_index": li, "transactions": []}}, "fake"
        raise AssertionError(method)
    monkeypatch.setattr(W, "_post", fake_post)


def test_find_enable_amendment_tx_walks_back_to_flag_plus_one(monkeypatch):
    calls = []
    _rpc(monkeypatch, enabled={PD}, majorities=[], calls=calls)
    r = W.find_enable_amendment_tx(PD_HASH, OBSERVED_LEDGER)
    assert r == {"ledger": TRUE_LEDGER, "close_time": TRUE_CLOSE, "tx_hash": TRUE_TX}
    scanned = [c[1]["ledger_index"] for c in calls if c[1].get("transactions")]
    assert scanned == [TRUE_LEDGER]                 # first candidate below observed


def test_find_ignores_got_lost_majority_pseudo_txs(monkeypatch):
    _rpc(monkeypatch, enabled={PD}, majorities=[], flags=W.TF_GOT_MAJORITY)
    assert W.find_enable_amendment_tx(PD_HASH, OBSERVED_LEDGER, max_back=3) is None


def test_find_returns_none_when_not_found_or_rpc_fails(monkeypatch):
    _rpc(monkeypatch, enabled={PD}, majorities=[], with_tx=False)
    assert W.find_enable_amendment_tx(PD_HASH, OBSERVED_LEDGER, max_back=3) is None
    monkeypatch.setattr(W, "_post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert W.find_enable_amendment_tx(PD_HASH, OBSERVED_LEDGER, max_back=3) is None
    assert W.find_enable_amendment_tx(PD_HASH, None) is None


class _Cur(_FakeCursor):
    """open rows for step 2; backfill rows for step 3."""
    def __init__(self, open_rows, backfill_rows):
        super().__init__(open_rows); self.backfill_rows = backfill_rows
    def fetchall(self):
        if "enabled_tx_hash IS NULL" in self._last and "SELECT" in self._last:
            return list(self.backfill_rows)
        return super().fetchall()


def _db(monkeypatch, open_rows, backfill_rows=()):
    cur = _Cur(open_rows, list(backfill_rows)); conn = _FakeConn(cur)
    monkeypatch.setattr(W.db, "pg_connect", lambda: conn); return cur


def test_run_stamps_true_enable_ledger_time_and_tx(monkeypatch):
    _rpc(monkeypatch, enabled={PD}, majorities=[(BATCH, BATCH_CT)])
    cur = _db(monkeypatch, open_rows=[(PD, PD_CT), (BATCH, BATCH_CT)])
    out = W.run()
    assert out["enabled"] == 1 and out["removed"] == 0
    ups = _updates(cur, "enabled_seen_ledger")
    assert len(ups) == 1
    e_ledger, e_close, e_iso, e_tx, _now, h, ct = ups[0]
    assert (e_ledger, e_close, e_tx, h, ct) == (TRUE_LEDGER, TRUE_CLOSE, TRUE_TX, PD, PD_CT)
    assert e_iso == "2026-10-08T21:29:50Z"           # true time, not the observed 21:37:31Z


def test_run_falls_back_to_observed_when_tx_not_found(monkeypatch):
    _rpc(monkeypatch, enabled={PD}, majorities=[], with_tx=False)
    monkeypatch.setattr(W, "MAX_FLAG_LEDGERS_BACK", 2)
    cur = _db(monkeypatch, open_rows=[(PD, PD_CT)])
    W.run()
    e_ledger, e_close, e_iso, e_tx, *_ = _updates(cur, "enabled_seen_ledger")[0]
    assert (e_ledger, e_close, e_tx) == (OBSERVED_LEDGER, OBSERVED_CLOSE, None)
    assert e_iso == "2026-10-08T21:37:31Z"


def test_run_backfills_previously_observed_row(monkeypatch):
    """The live PD row today: enabled_seen_ledger=107524984, no tx. Next run
    corrects it to 107,524,865 / 21:29:50Z / tx 4782…."""
    _rpc(monkeypatch, enabled={PD}, majorities=[])
    cur = _db(monkeypatch, open_rows=[], backfill_rows=[(PD, PD_CT, OBSERVED_LEDGER)])
    out = W.run()
    assert out["backfilled"] == 1 and out["enabled"] == 0
    fixes = [p for sql, p in cur.executed if "SET enabled_seen_ledger" in sql and "enabled_tx_hash IS NULL" in sql and "removed_seen_ledger IS NULL" not in sql]
    assert len(fixes) == 1
    e_ledger, e_close, e_iso, e_tx, _now, h, ct = fixes[0]
    assert (e_ledger, e_iso, e_tx, h, ct) == (TRUE_LEDGER, "2026-10-08T21:29:50Z", TRUE_TX, PD, PD_CT)


def test_backfill_leaves_row_alone_when_tx_not_found(monkeypatch):
    _rpc(monkeypatch, enabled={PD}, majorities=[], with_tx=False)
    monkeypatch.setattr(W, "MAX_FLAG_LEDGERS_BACK", 2)
    cur = _db(monkeypatch, open_rows=[], backfill_rows=[(PD, PD_CT, OBSERVED_LEDGER)])
    out = W.run()
    assert out["backfilled"] == 0
    assert not [1 for sql, _ in cur.executed if "SET enabled_seen_ledger" in sql]


def test_page_renders_true_enable_point_with_tx_link(monkeypatch):
    """Real /amendments route + template: when enabled_tx_hash is set the row
    says 'enabled at ledger 107,524,865' with the EnableAmendment link and
    no longer 'recorded enabled by our node'."""
    import app as app_mod
    state = {"ok": True, "enabled_count": 95, "in_flight_count": 12, "ledger_index": 107525000,
             "recognized_enabled": [{"hash": PD, "name": "PermissionDelegationV1_1"}],
             "unrecognized_enabled": [], "unrecognized_enabled_count": 0, "in_flight": [], "superseded": [],
             "majorities": [], "network_votes_source": {}, "in_development": [], "sourcing": "sovereign",
             "cached_age_seconds": 0.0}
    row = {"name": "PermissionDelegationV1_1", "hash": PD, "majority_close_iso": "2026-09-24T21:25:01Z",
           "activation_eta_iso": "2026-10-08T21:25:01Z", "first_seen_iso": "2026-09-24T21:40:00Z",
           "removed_iso": None, "active": False, "enabled": True,
           "enabled_seen_ledger": TRUE_LEDGER, "enabled_close_iso": "2026-10-08T21:29:50Z",
           "enabled_iso": "2026-10-08T21:29:50Z", "enabled_tx_hash": TRUE_TX,
           "vote_count_at_first": 30, "unl_threshold": 28, "first_seen_ledger": 107340000,
           "first_seen_close_iso": "2026-09-24T21:40:00Z", "removed_seen_ledger": None,
           "removed_close_iso": None, "correction_note": None}
    monkeypatch.setattr(app_mod, "fetch_amendments_state_cached", lambda *a, **k: dict(state))
    monkeypatch.setattr(app_mod, "_load_amendment_majority_history", lambda *a, **k: [dict(row)])
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        html = c.get("/amendments").data.decode()
    assert "enabled at ledger</span> 107,524,865" in html.replace("</span>", "</span>") or "enabled at ledger 107,524,865" in html
    assert f"https://livenet.xrpl.org/transactions/{TRUE_TX}" in html
    assert "5:29:50 PM" in html or "5:29 PM" in html
    assert "recorded enabled by our node" not in html
    assert "first seen in the Amendments array" not in html
    # without a tx hash the honest observed wording stays
    row2 = dict(row, enabled_tx_hash=None, enabled_seen_ledger=OBSERVED_LEDGER, enabled_iso="2026-10-08T21:37:31Z")
    monkeypatch.setattr(app_mod, "_load_amendment_majority_history", lambda *a, **k: [row2])
    with app_mod.app.test_client() as c:
        html2 = c.get("/amendments").data.decode()
    assert "recorded enabled by our node at ledger" in html2 and "livenet.xrpl.org/transactions" not in html2


def test_run_applies_additive_column_first(monkeypatch):
    _rpc(monkeypatch, enabled=set(), majorities=[])
    cur = _db(monkeypatch, open_rows=[])
    W.run()
    first_sql = cur.executed[0][0]
    assert first_sql.startswith("ALTER TABLE amendment_majority_history ADD COLUMN IF NOT EXISTS enabled_tx_hash")
