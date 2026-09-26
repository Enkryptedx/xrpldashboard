"""new_accounts — pure row extraction + summary shaping (no DB, no RPC)."""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import new_accounts_walker as W  # noqa: E402
import db  # noqa: E402

NEW = "rNEWacct11111111111111111111111111"
FUNDER = "rFUNDER1111111111111111111111111111"


def _tx(hash_="AB" * 32, created=True, result="tesSUCCESS", v2=True, amount="20000000"):
    meta = {"TransactionResult": result, "delivered_amount": amount, "AffectedNodes": [
        {"ModifiedNode": {"LedgerEntryType": "AccountRoot", "FinalFields": {"Account": FUNDER}}},
    ]}
    if created:
        meta["AffectedNodes"].append(
            {"CreatedNode": {"LedgerEntryType": "AccountRoot", "NewFields": {"Account": NEW, "Balance": amount}}})
    body = {"TransactionType": "Payment", "Account": FUNDER, "Destination": NEW, "Amount": amount}
    if v2:
        return {"hash": hash_, "meta": meta, "tx_json": body}
    legacy = dict(body); legacy["hash"] = hash_; legacy["metaData"] = meta
    return legacy


def test_rows_from_ledger_v2_and_legacy():
    rows = W.rows_from_ledger(107_000_000, 812_000_000, [_tx(), _tx(hash_="CD" * 32, v2=False)])
    assert len(rows) == 2
    r = rows[0]
    assert r["address"] == NEW and r["funder"] == FUNDER and r["amount_drops"] == 20_000_000
    assert r["ledger_index"] == 107_000_000 and r["close_time"] == 812_000_000
    assert r["first_seen_iso"] == "2025-09-24T03:33:20Z"  # ripple epoch 812,000,000 + 946,684,800
    assert r["source"] == "own_node_stream"


def test_failed_or_non_creating_tx_yield_nothing():
    assert W.rows_from_ledger(1, 812_000_000, [_tx(created=False)]) == []
    assert W.rows_from_ledger(1, 812_000_000, [_tx(result="tecUNFUNDED")]) == []
    assert W.rows_from_ledger(1, 812_000_000, []) == []


def test_issued_currency_amount_is_none_but_row_kept():
    tx = _tx(amount="20000000")
    tx["meta"]["delivered_amount"] = {"currency": "USD", "issuer": FUNDER, "value": "1"}
    tx["tx_json"]["Amount"] = {"currency": "USD", "issuer": FUNDER, "value": "1"}
    rows = W.rows_from_ledger(1, 812_000_000, [tx])
    assert len(rows) == 1 and rows[0]["amount_drops"] is None


class _Cur:
    def __init__(self, answers):
        self.answers = list(answers); self.sql = []
    def execute(self, q, p=None):
        self.sql.append(q)
    def fetchone(self):
        return self.answers.pop(0)
    def fetchall(self):
        return self.answers.pop(0)


def test_summary_shape():
    now = 1790400000  # 2026-09-26 05:20:00Z
    cur = _Cur([
        (1234, 843700000, 843715000),        # total, first, last close_time (ripple)
        (321,), (1234,),                      # 24h, 7d
        [("2026-09-25", 900), ("2026-09-26", 334)],
        ("2026-09-25", 900),
    ])
    s = db.read_new_accounts_summary(cur, now_unix=now)
    assert s["total"] == 1234 and s["count_24h"] == 321 and s["count_7d"] == 1234
    assert s["daily"][0] == ("2026-09-25", 900)
    assert s["highest_day"] == {"date": "2026-09-25", "count": 900}
    assert s["first_seen_iso"].startswith("2026-09-2") and s["latest_age_s"] >= 0
    assert any("WHERE d < %s" in q for q in cur.sql)  # today excluded from "highest full day"


def test_summary_none_when_empty():
    cur = _Cur([(0, None, None)])
    assert db.read_new_accounts_summary(cur, now_unix=1790400000) is None
