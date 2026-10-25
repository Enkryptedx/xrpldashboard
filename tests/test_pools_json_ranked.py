"""/pools.json must be what it says it is: the top 50 AMM pools by TVL.

Until 2026-10-25 the route sliced [:50] of db.read_amm_ranked_pools(), a
SELECT with no ORDER BY, and signed the result as a ranked list. The heap
order was stable within an hourly snapshot and arbitrary across them; the
$7.78M top pool was absent from the published 50 while $0-TVL pools were
in it. These tests pin the ordering, the #1 row, and the SQL shape.
"""
import json
import os
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "")

import db  # noqa: E402
import app as app_module  # noqa: E402


FAKE = [
    # deliberately NOT in TVL order; includes NULL and zero TVL rows
    {"amm_account": "rMID", "pair": "XRP/MID", "tvl_usd": 886_390.15, "tvl_status": "estimated", "fee_pct": 0.5, "kind": "xrp", "asset_a": {"currency": "XRP"}, "asset_b": {"currency": "MID"}, "amount_a": 1, "amount_b": 2},
    {"amm_account": "rNULL", "pair": "ODL/BITx", "tvl_usd": None, "tvl_status": "unpriced", "fee_pct": 0.5, "kind": "iou", "asset_a": {}, "asset_b": {}, "amount_a": None, "amount_b": None},
    {"amm_account": "rTOP", "pair": "XRP/CRYPTO", "tvl_usd": 7_782_771.56, "tvl_status": "estimated", "fee_pct": 0.5, "kind": "xrp", "asset_a": {"currency": "XRP"}, "asset_b": {"currency": "CRYPTO"}, "amount_a": 3, "amount_b": 4},
    {"amm_account": "rZERO", "pair": "XRP/RUG", "tvl_usd": 0.0, "tvl_status": "unpriced", "fee_pct": 0.5, "kind": "xrp", "asset_a": {}, "asset_b": {}, "amount_a": 0, "amount_b": 0},
    {"amm_account": "rSECOND", "pair": "XRP/RLUSD", "tvl_usd": 5_251_914.34, "tvl_status": "exact", "fee_pct": 0.5, "kind": "xrp", "asset_a": {"currency": "XRP"}, "asset_b": {"currency": "RLUSD"}, "amount_a": 5, "amount_b": 6},
]


def _ranked_fake(columns=None, order_by_tvl=False, limit=None):
    rows = list(FAKE)
    if order_by_tvl:
        rows.sort(key=lambda r: (r["tvl_usd"] is None, -(r["tvl_usd"] or 0)))
    if limit is not None:
        rows = rows[:limit]
    return rows


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(db, "pg_available", lambda: True)
    calls = []
    def fake(columns=None, order_by_tvl=False, limit=None):
        calls.append({"order_by_tvl": order_by_tvl, "limit": limit})
        return _ranked_fake(columns, order_by_tvl, limit)
    monkeypatch.setattr(db, "read_amm_ranked_pools", fake)
    app_module.app.config["TESTING"] = True
    c = app_module.app.test_client()
    c._calls = calls
    return c


def _payload(client):
    r = client.get("/pools.json")
    assert r.status_code == 200
    d = json.loads(r.data)
    return d.get("data") or d, client._calls


def test_first_row_is_the_largest_pool_by_tvl(client):
    data, _ = _payload(client)
    pools = data["pools"]
    assert pools[0]["amm_account"] == "rTOP"
    assert pools[0]["pair"] == "XRP/CRYPTO"
    assert pools[0]["tvl_usd"] == pytest.approx(7_782_771.56)


def test_list_is_sorted_desc_by_tvl_with_nulls_last(client):
    data, _ = _payload(client)
    tvls = [p["tvl_usd"] for p in data["pools"]]
    # route emits float(tvl or 0), so NULL -> 0.0 and must sit at the end
    assert tvls == sorted(tvls, reverse=True)
    assert [p["amm_account"] for p in data["pools"]] == ["rTOP", "rSECOND", "rMID", "rZERO", "rNULL"]


def test_route_asks_the_database_for_ordered_top_50_not_a_python_slice(client):
    _, calls = _payload(client)
    assert calls == [{"order_by_tvl": True, "limit": 50}], calls
    src = (ROOT / "app.py").read_text()
    i = src.index("def pools_json():"); body = src[i:src.index("\ndef ", i + 10)]
    assert "ranked[:50]" not in body
    assert "read_amm_ranked_pools(order_by_tvl=True, limit=50)" in body


def test_read_amm_ranked_pools_sql_shape():
    import inspect
    src = inspect.getsource(db.read_amm_ranked_pools)
    assert 'sql += " ORDER BY tvl_usd DESC NULLS LAST"' in src
    assert 'sql += " LIMIT %s"' in src
    with pytest.raises(ValueError):
        db.pg_available = lambda: True
        db.read_amm_ranked_pools(limit=0)
