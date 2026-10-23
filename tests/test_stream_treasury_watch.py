"""xrpl_stream records EVERY payment touching a treasury wallet.

On 2026-10-20 a 10 XRP transfer Custody G -> Evernorth 2 was never written
to `events`: the stream recorded a payment only above WHALE_XRP_THRESHOLD_XRP
(50,000) or touching named_accounts.json, and the 13 treasury wallets live
in institutional_treasuries.WALLETS, not there. The /institutional card could
only honestly promise "under 50,000 XRP not shown".

TREASURY_WATCH reads WALLETS itself (owner: not by copying them into
named_accounts.json, which would also label them on /whales and the
homepage) and gates the existing `tagged` branch. These tests drive the
real whale_handler with a real-shaped validated-transaction message and
assert on the sqlite row it writes -- into a temp DB, with the Postgres
bridge stubbed out -- so the proof is end-to-end, not a string match.
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CUSTODY_G = "rGy4zJtGfGtF7dtjZmBraQTcfZSQgwqpaa"      # in WALLETS
EVERNORTH_2 = "rKXXrAgpkHQN8m4HxAQCYmDCPPUByc9mVq"    # in WALLETS
STRANGER = "rStranger000000000000000000000000"


@pytest.fixture(scope="module")
def stream(tmp_path_factory):
    """Import xrpl_stream with its sqlite files pointed at a temp dir and
    the pg bridge replaced by a recorder."""
    d = tmp_path_factory.mktemp("stream")
    os.environ["DATABASE_URL"] = ""
    sys.modules.pop("xrpl_stream", None)
    import xrpl_stream as x
    # re-point the long-lived connections at temp DBs
    x._events_conn = sqlite3.connect(str(d / "events.db"), isolation_level=None, check_same_thread=False)
    x._events_conn.execute(
        "CREATE TABLE IF NOT EXISTS events (tx_hash TEXT PRIMARY KEY, ledger_index INTEGER, ts INTEGER, "
        "type TEXT, from_addr TEXT, to_addr TEXT, amount_drops INTEGER, currency TEXT, issuer TEXT, raw_json TEXT)")
    written = []
    x.pgbridge = type("PB", (), {"write_event": staticmethod(lambda *a, **k: written.append(a))})()
    x._written = written
    return x


def _msg(frm, to, drops, h="A" * 64, ledger=107789040):
    return {"engine_result": "tesSUCCESS", "validated": True, "hash": h, "ledger_index": ledger,
            "transaction": {"TransactionType": "Payment", "Account": frm, "Destination": to,
                            "Amount": str(drops), "hash": h, "ledger_index": ledger},
            "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": str(drops), "AffectedNodes": []}}


def _rows(x):
    return x._events_conn.execute("SELECT tx_hash, type, from_addr, to_addr, amount_drops, currency FROM events").fetchall()


def test_treasury_watch_is_exactly_the_wallet_list(stream):
    import institutional_treasuries as T
    assert stream.TREASURY_WATCH == frozenset(a for a, _ in T.WALLETS)
    assert len(stream.TREASURY_WATCH) == 23
    assert "rDFrBJGy2QMNphpknEWLjmrDsavmUewVrh" in stream.TREASURY_WATCH  # 10 BitGo-created added 2026-10-22
    assert CUSTODY_G in stream.TREASURY_WATCH and EVERNORTH_2 in stream.TREASURY_WATCH


def test_treasury_wallets_are_not_in_named_accounts(stream):
    """The owner's constraint: watch them via the treasury list, never by
    adding them to named_accounts.json."""
    for a in stream.TREASURY_WATCH:
        assert a not in stream.NAMED_ACCOUNTS


def test_ten_xrp_treasury_internal_transfer_is_recorded(stream):
    """THE regression: 10 XRP, Custody G -> Evernorth 2, 2026-10-20 14:25 ET.
    Far below the 50,000 XRP whale threshold; neither side in
    named_accounts. Previously dropped on the floor."""
    stream._events_conn.execute("DELETE FROM events")
    stream.whale_handler(_msg(CUSTODY_G, EVERNORTH_2, 10_000_000, h="8CB7" + "0" * 60), {})
    rows = _rows(stream)
    assert rows == [("8CB7" + "0" * 60, "tagged", CUSTODY_G, EVERNORTH_2, 10_000_000, "XRP")]


def test_small_inbound_to_treasury_is_recorded(stream):
    stream._events_conn.execute("DELETE FROM events")
    stream.whale_handler(_msg(STRANGER, CUSTODY_G, 2_500_000, h="B" * 64), {})
    assert [(r[1], r[4]) for r in _rows(stream)] == [("tagged", 2_500_000)]


def test_large_treasury_payment_still_classifies_as_large_xfer(stream):
    """Size wins: the whale branch runs first, so a 200,000 XRP move keeps
    its 'large_xfer' type and the /whales feed sees it as before."""
    stream._events_conn.execute("DELETE FROM events")
    stream.whale_handler(_msg(CUSTODY_G, STRANGER, 200_000_000_000, h="C" * 64), {})
    assert [r[1] for r in _rows(stream)] == ["large_xfer"]


def test_unrelated_small_payment_is_still_ignored(stream):
    """The watch must not widen capture for the rest of the ledger."""
    stream._events_conn.execute("DELETE FROM events")
    stream.whale_handler(_msg(STRANGER, "rOther0000000000000000000000000000", 10_000_000, h="D" * 64), {})
    assert _rows(stream) == []


def test_pg_bridge_receives_the_tagged_row(stream):
    stream._events_conn.execute("DELETE FROM events"); stream._written.clear()
    stream.whale_handler(_msg(EVERNORTH_2, CUSTODY_G, 10_000_000, h="E" * 64), {})
    assert len(stream._written) == 1
    assert stream._written[0][3] == "tagged"


def test_loader_degrades_to_empty_not_crash(monkeypatch, stream):
    """If institutional_treasuries ever fails to import on the Lenovo, the
    stream must keep running (with no treasury watch), not die."""
    import builtins
    real = builtins.__import__
    def boom(name, *a, **k):
        if name == "institutional_treasuries":
            raise ImportError("simulated")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", boom)
    assert stream._load_treasury_watch() == frozenset()
