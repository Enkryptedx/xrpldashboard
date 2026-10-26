"""amm_ranked_pools_staging + the atomic rename swap
(branch amm-ranked-pools-staging-swap-2026-10-26).

Fakes only — a recording cursor stands in for psycopg, so nothing here
touches Neon, our node, or any DATABASE_URL (owner rule 2026-10-08).

Why this exists: `_mirror_to_postgres` used to upsert every rank_amms
checkpoint straight into the public `amm_ranked_pools` table. Its own
"delete whatever vanished" clause scopes on `snapshot_ts < this call's
timestamp`, and since a checkpoint mid-pass only carries the subset of
pools ranked so far, that delete wiped every pool not yet re-ranked THIS
pass — live `/pools` undercounted (9,645 vs ~30,295) for most of every
~2.5h pass. The fix: checkpoints write to a scratch
`amm_ranked_pools_staging` table instead (same upsert/delete shape, now
harmless because nothing public reads it), and only a completed pass
promotes it with one atomic 3-way rename. These tests pin:
  * the staging upsert has the identical shape to the live-table one
    (same columns, same batching, same dedupe) — it's a retarget, not a
    rewrite;
  * the swap is exactly the 3 renames in the documented order, inside one
    transaction, and never touches row data;
  * nothing here can accidentally fall back to writing the live table.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db  # noqa: E402


class _FakeCursor:
    def __init__(self, sink):
        self.sink = sink
        self.rowcount = 4          # pretend 4 stale pools were removed

    def execute(self, sql, params=None):
        self.sink.append((" ".join(str(sql).split()), params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, sink):
        self.sink = sink

    def transaction(self):
        return _FakeTxn()

    def cursor(self):
        return _FakeCursor(self.sink)


def _pool(acct, **over):
    r = {"amm_account": acct, "pair": "XRP/USD", "fee_pct": 0.3, "fee_raw": 300,
         "amount_a": 1.0, "amount_b": 2.0,
         "asset_a": {"currency": "XRP"},
         "asset_b": {"currency": "USD", "issuer": "rI"},
         "tvl_usd": 10.0, "tvl_status": "priced", "kind": "amm",
         "lp_token_value": 1.5}
    r.update(over)
    return r


@pytest.fixture
def sink(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "_get_writer_conn", lambda: _FakeConn(calls))
    return calls


def _sql(sink):
    return [s for s, _ in sink]


# ── staging upsert never touches the public table ──

def test_staging_upsert_never_targets_the_public_table(sink):
    db.replace_amm_ranked_pools_staging([_pool("rA"), _pool("rB")])
    for s in _sql(sink):
        assert "amm_ranked_pools_staging" in s or "amm_ranked_pools " not in s + " ", (
            "a staging-table write must never touch the public table: " + s)
    ins = [s for s in _sql(sink) if s.startswith("INSERT INTO")]
    assert len(ins) == 1
    assert ins[0].startswith("INSERT INTO amm_ranked_pools_staging")


def test_staging_delete_is_scoped_to_staging_table(sink):
    db.replace_amm_ranked_pools_staging([_pool("rA")])
    deletes = [s for s in _sql(sink) if s.startswith("DELETE FROM")]
    assert len(deletes) == 1
    assert deletes[0].startswith("DELETE FROM amm_ranked_pools_staging")
    assert "WHERE snapshot_ts <" in deletes[0]


def test_staging_insert_uses_on_conflict_upsert(sink):
    db.replace_amm_ranked_pools_staging([_pool("rA")])
    ins = next(s for s in _sql(sink) if s.startswith("INSERT INTO"))
    assert "ON CONFLICT (amm_account) DO UPDATE SET" in ins
    for col in ("pair", "fee_pct", "fee_raw", "amount_a", "amount_b", "asset_a",
                "asset_b", "tvl_usd", "tvl_status", "kind", "lp_token_value",
                "snapshot_ts"):
        assert f"{col} = EXCLUDED.{col}" in ins, f"{col} not refreshed"


def test_staging_reuses_the_same_placeholder_and_chunk_size(sink):
    # Pins that this is a retarget of the exact same batching, not a
    # parallel implementation that could drift.
    db.replace_amm_ranked_pools_staging([_pool(f"r{i}") for i in range(2500)])
    ins = [s for s in _sql(sink) if s.startswith("INSERT INTO")]
    assert len(ins) == 3, "2500 rows at chunk 1000 => 3 statements"


def test_staging_dedupes_last_wins(sink):
    res = db.replace_amm_ranked_pools_staging(
        [_pool("rA", pair="OLD"), _pool("rA", pair="NEW")])
    assert res["upserted"] == 1
    _, params = next((s, p) for s, p in sink if s.startswith("INSERT INTO"))
    assert "NEW" in params and "OLD" not in params


def test_staging_row_without_account_is_skipped(sink):
    res = db.replace_amm_ranked_pools_staging([_pool("rA"), _pool(None)])
    assert res["upserted"] == 1
    assert res["skipped_no_account"] == 1


def test_staging_empty_input_is_a_skip_not_a_wipe(sink):
    assert db.replace_amm_ranked_pools_staging([]) is None
    assert sink == []


def test_staging_missing_writer_conn_raises(monkeypatch):
    monkeypatch.setattr(db, "_get_writer_conn", lambda: None)
    with pytest.raises(db.WriterConnUnavailable):
        db.replace_amm_ranked_pools_staging([_pool("rA")])


def test_staging_failure_drops_the_conn(monkeypatch):
    dropped = []

    class _Boom(_FakeConn):
        def cursor(self):
            raise RuntimeError("neon down")

    monkeypatch.setattr(db, "_get_writer_conn", lambda: _Boom([]))
    monkeypatch.setattr(db, "_drop_writer_conn", lambda: dropped.append(1))
    with pytest.raises(Exception):
        db.replace_amm_ranked_pools_staging([_pool("rA")])
    assert dropped == [1]


# ── the swap itself ──

def test_swap_is_exactly_three_renames_in_order(sink):
    db.swap_amm_ranked_pools_staging()
    stmts = [s for s, _ in sink]
    assert stmts == [
        "ALTER TABLE amm_ranked_pools RENAME TO amm_ranked_pools_old",
        "ALTER TABLE amm_ranked_pools_staging RENAME TO amm_ranked_pools",
        "ALTER TABLE amm_ranked_pools_old RENAME TO amm_ranked_pools_staging",
    ], "swap must be exactly these 3 renames, in this order"


def test_swap_runs_inside_one_transaction(monkeypatch, sink):
    entered = []

    class _TrackedTxn(_FakeTxn):
        def __enter__(self):
            entered.append(1)
            return self

    class _TrackedConn(_FakeConn):
        def transaction(self):
            return _TrackedTxn()

    monkeypatch.setattr(db, "_get_writer_conn", lambda: _TrackedConn(sink))
    db.swap_amm_ranked_pools_staging()
    assert entered == [1], "all 3 renames must share one transaction() block"


def test_swap_never_copies_or_touches_row_data(sink):
    db.swap_amm_ranked_pools_staging()
    for s, params in sink:
        assert s.startswith("ALTER TABLE"), f"swap issued non-DDL statement: {s}"
        assert params is None


def test_swap_returns_true_on_success(sink):
    assert db.swap_amm_ranked_pools_staging() is True


def test_swap_missing_writer_conn_raises(monkeypatch):
    monkeypatch.setattr(db, "_get_writer_conn", lambda: None)
    with pytest.raises(db.WriterConnUnavailable):
        db.swap_amm_ranked_pools_staging()


def test_swap_failure_drops_the_conn(monkeypatch):
    dropped = []

    class _Boom(_FakeConn):
        def cursor(self):
            raise RuntimeError("neon down mid-rename")

    monkeypatch.setattr(db, "_get_writer_conn", lambda: _Boom([]))
    monkeypatch.setattr(db, "_drop_writer_conn", lambda: dropped.append(1))
    with pytest.raises(Exception):
        db.swap_amm_ranked_pools_staging()
    assert dropped == [1]


def test_staging_schema_ddl_is_present():
    src = open(os.path.join(os.path.dirname(__file__), "..", "db.py"),
               encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS amm_ranked_pools_staging" in src
    assert "CREATE UNIQUE INDEX IF NOT EXISTS amm_ranked_pools_staging_account_uidx" in src
