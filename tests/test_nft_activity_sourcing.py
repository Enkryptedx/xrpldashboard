"""Forced-fallback + sourcing tests for nft_activity_walker (GAP-6).

GAP-6 (Charlie 2026-09-25): verified the live forward-walker (activity mode)
reads own-node-first via xrpl_client.XrplClient — it already did — and that
Clio is used only as the LABELED fallback list for history backfill. The gap
was (a) one walker_node_fallback row PER cascading call in activity mode
(685k rows during the 2026-09-19/20 post-outage catch-up) and (b) backfill
building a bare JsonRpcClient on Clio with no own-node attempt and no
sourcing flag. Both modes now share xrpl_client.RunFallbackSink (one row per
run) and stamp `sourcing=` into the walker_health message /nfts reads.

Same harness shape as tests/test_bridge_signer_sourcing.py. Hermetic: no DB
(the nft_walker_state / insert / cursor db calls are patched), no network.
pytest-collected; also runs standalone:
    ./venv/bin/python tests/test_nft_activity_sourcing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xrpl.models.requests import Ledger

import db
import xrpl_client as xc
import nft_activity_walker as naw


_VALIDATED = 107_500_000
_CURSOR = _VALIDATED - 20          # 5-ledger batch well below tip-3
_BATCH = 5


class _Resp:
    def __init__(self, result):
        self.result = result


def _canned(req):
    assert isinstance(req, Ledger)
    if req.ledger_index == "validated":
        return {"ledger_index": _VALIDATED}
    seq = int(req.ledger_index)
    txs = [{"hash": "B" * 64, "meta": {"TransactionResult": "tesSUCCESS"},
            "tx_json": {"TransactionType": "Payment"}}]
    if seq % 2 == 0:
        txs.append({"hash": f"{seq:064X}"[:64],
                    "meta": {"TransactionResult": "tesSUCCESS"},
                    "tx_json": {"TransactionType": "NFTokenMint",
                                "NFTokenID": "00" * 32}})
    return {"ledger": {"ledger_index": seq, "close_time": 843_000_000 + seq,
                       "transactions": txs}}


class _WriteRecorder:
    def __init__(self):
        self.rows = []

    def __call__(self, walker_name, reason):
        self.rows.append((walker_name, reason))
        return True


class _RpcRecorder:
    def __init__(self, local_ok):
        self.local_ok = local_ok
        self.urls = []

    def __call__(self, url, req):
        self.urls.append(url)
        if url == xc.LOCAL_NODE and not self.local_ok:
            raise ConnectionError("refused")
        return _Resp(_canned(req))


class _FakeDb:
    def __init__(self, state):
        self.state = state
        self.inserted = []
        self.cursor_writes = []
        self.backfill_writes = []

    def read_nft_walker_state(self, name):
        return dict(self.state) if self.state is not None else None

    def insert_nft_activity_batch(self, rows):
        self.inserted.extend(rows)
        return len(rows)

    def write_nft_walker_cursor(self, name, cursor, success=True):
        self.cursor_writes.append(cursor)
        return True

    def write_nft_walker_backfill_cursor(self, name, cursor, success=True):
        self.backfill_writes.append(cursor)
        return True


def _install(local_ok, recorder, fake_db):
    saved = {
        "_probe_local": xc._probe_local, "_post_rpc": xc._post_rpc,
        "_health": dict(xc._health), "sleep": xc.time.sleep,
        "write_walker_node_fallback": db.write_walker_node_fallback,
        "read_nft_walker_state": db.read_nft_walker_state,
        "insert_nft_activity_batch": db.insert_nft_activity_batch,
        "write_nft_walker_cursor": db.write_nft_walker_cursor,
        "write_nft_walker_backfill_cursor": db.write_nft_walker_backfill_cursor,
        "BATCH": naw.BATCH_LEDGERS,
    }
    rpc = _RpcRecorder(local_ok)
    xc._probe_local = (lambda: (True, "full")) if local_ok else \
        (lambda: (False, "unreachable:ConnectError"))
    xc._post_rpc = rpc
    xc._health.update({"checked_at": 0.0, "ok": False, "reason": "uninitialized"})
    xc.time.sleep = lambda _s: None
    db.write_walker_node_fallback = recorder
    db.read_nft_walker_state = fake_db.read_nft_walker_state
    db.insert_nft_activity_batch = fake_db.insert_nft_activity_batch
    db.write_nft_walker_cursor = fake_db.write_nft_walker_cursor
    db.write_nft_walker_backfill_cursor = fake_db.write_nft_walker_backfill_cursor
    naw.BATCH_LEDGERS = _BATCH

    def restore():
        xc._probe_local = saved["_probe_local"]
        xc._post_rpc = saved["_post_rpc"]
        xc._health.update(saved["_health"])
        xc.time.sleep = saved["sleep"]
        db.write_walker_node_fallback = saved["write_walker_node_fallback"]
        db.read_nft_walker_state = saved["read_nft_walker_state"]
        db.insert_nft_activity_batch = saved["insert_nft_activity_batch"]
        db.write_nft_walker_cursor = saved["write_nft_walker_cursor"]
        db.write_nft_walker_backfill_cursor = saved["write_nft_walker_backfill_cursor"]
        naw.BATCH_LEDGERS = saved["BATCH"]
    return rpc, restore


def _activity(local_ok):
    rec = _WriteRecorder()
    fdb = _FakeDb({"cursor_ledger": _CURSOR, "backfill_ledger": 1, "backfill_target": 1})
    rpc, restore = _install(local_ok, rec, fdb)
    try:
        ok, msg = naw.run_activity()
    finally:
        restore()
    return ok, msg, rec, rpc, fdb


def _backfill(local_ok, hi, target):
    rec = _WriteRecorder()
    fdb = _FakeDb({"cursor_ledger": _CURSOR, "backfill_ledger": hi, "backfill_target": target})
    rpc, restore = _install(local_ok, rec, fdb)
    try:
        ok, msg = naw.run_backfill()
    finally:
        restore()
    return ok, msg, rec, rpc, fdb


# ── Tests ─────────────────────────────────────────────────────────────
def test_activity_healthy_own_node_is_sovereign():
    ok, msg, rec, rpc, fdb = _activity(local_ok=True)
    assert ok, msg
    assert "sourcing=sovereign" in msg and "fallback_reason" not in msg
    assert rec.rows == [], rec.rows
    assert set(rpc.urls) == {xc.LOCAL_NODE}
    assert len(rpc.urls) == 1 + _BATCH            # validated + 5 ledgers
    assert fdb.cursor_writes == [_CURSOR + _BATCH]
    assert len(fdb.inserted) == 3                 # even seqs in the 5-ledger batch
    assert all(r["tx_type"] == "Mint" for r in fdb.inserted)


def test_activity_forced_fallback_one_row_per_run():
    ok, msg, rec, rpc, fdb = _activity(local_ok=False)
    assert ok, msg
    assert "sourcing=fallback-public-rpc" in msg
    assert "fallback_reason=unreachable:ConnectError" in msg
    # Exactly ONE row despite 6 cascading calls (was one per call pre-fix).
    assert len(rec.rows) == 1, rec.rows
    assert rec.rows[0] == ("nft_activity", "unreachable:ConnectError")
    public = [u for u in rpc.urls if u != xc.LOCAL_NODE]
    assert len(public) == 1 + _BATCH and set(public) == {xc.PUBLIC_NODES[0]}
    assert fdb.cursor_writes == [_CURSOR + _BATCH]
    assert len(fdb.inserted) == 3


def test_activity_before_after_envelope_diff():
    _, m_ok, rec_ok, _, fdb_ok = _activity(local_ok=True)
    _, m_bad, rec_bad, _, fdb_bad = _activity(local_ok=False)
    strip = lambda m: m.split(" sourcing=")[0]
    assert strip(m_ok) == strip(m_bad), (m_ok, m_bad)
    assert [r["tx_hash"] for r in fdb_ok.inserted] == [r["tx_hash"] for r in fdb_bad.inserted]
    assert (len(rec_ok.rows), len(rec_bad.rows)) == (0, 1)
    print(f"    before={m_ok.split(' sourcing=')[1]!r}  after={m_bad.split(' sourcing=')[1]!r}")


def test_backfill_uses_clio_archive_as_first_labeled_fallback():
    hi, target = 103_252_860, 103_252_853
    ok, msg, rec, rpc, fdb = _backfill(local_ok=False, hi=hi, target=target)
    assert ok, msg
    assert "reached_target" in msg or "caught_up" in msg, msg
    assert "sourcing=fallback-public-rpc" in msg
    assert len(rec.rows) == 1 and rec.rows[0][0] == "nft_activity", rec.rows
    public = [u for u in rpc.urls if u != xc.LOCAL_NODE]
    assert public and set(public) == {naw.BACKFILL_FALLBACK_URLS[0]}, set(public)
    assert "s2-clio.ripple.com" in naw.BACKFILL_FALLBACK_URLS[0]
    assert fdb.backfill_writes and fdb.backfill_writes[-1] <= target


def test_backfill_own_node_first_when_it_has_the_history():
    hi, target = 103_252_860, 103_252_853
    ok, msg, rec, rpc, fdb = _backfill(local_ok=True, hi=hi, target=target)
    assert ok, msg
    assert "sourcing=sovereign" in msg
    assert rec.rows == []
    assert set(rpc.urls) == {xc.LOCAL_NODE}


def test_backfill_caught_up_makes_no_rpc():
    ok, msg, rec, rpc, _ = _backfill(local_ok=False, hi=100, target=100)
    assert ok and "caught_up" in msg and "no rpc this run" in msg
    assert rpc.urls == [] and rec.rows == []


def test_sources_have_no_bare_public_client():
    import importlib.util
    root = os.path.dirname(naw.__file__)
    files = [naw.__file__, os.path.join(root, "scripts", "nft_activity_gap_fill.py")]
    for path in files:
        src = open(path, encoding="utf-8").read()
        lines = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
        assert not any("JsonRpcClient" in l for l in lines if "import" in l or "(" in l), path
        assigns = [l for l in lines if "=" in l]
        assert not any("s1.ripple.com" in l or "s2.ripple.com" in l for l in assigns), path
        assert not any("xrpscan" in l for l in lines), path
        assert "get_client(" in src, path
    # The Clio archive literal appears ONLY as the env-overridable default.
    walker_src = open(naw.__file__, encoding="utf-8").read()
    code = [l for l in walker_src.splitlines() if not l.lstrip().startswith("#")]
    assert sum("s2-clio.ripple.com" in l for l in code if "=" in l or "XRPL_BACKFILL_CLIO" in l) <= 1


TESTS = [
    ("activity_healthy_own_node_is_sovereign", test_activity_healthy_own_node_is_sovereign),
    ("activity_forced_fallback_one_row_per_run", test_activity_forced_fallback_one_row_per_run),
    ("activity_before_after_envelope_diff", test_activity_before_after_envelope_diff),
    ("backfill_uses_clio_archive_as_first_labeled_fallback",
     test_backfill_uses_clio_archive_as_first_labeled_fallback),
    ("backfill_own_node_first_when_it_has_the_history",
     test_backfill_own_node_first_when_it_has_the_history),
    ("backfill_caught_up_makes_no_rpc", test_backfill_caught_up_makes_no_rpc),
    ("sources_have_no_bare_public_client", test_sources_have_no_bare_public_client),
]


def main():
    pass_count = fail_count = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS {name}")
            pass_count += 1
        except AssertionError as e:
            print(f"  FAIL {name}: {e}")
            fail_count += 1
    print(f"\n== {pass_count} PASS / {fail_count} FAIL ==")
    return fail_count


if __name__ == "__main__":
    sys.exit(main())
