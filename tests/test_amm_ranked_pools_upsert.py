"""amm_ranked_pools: upsert/diff replaces the delete-and-rebuild
(branch amm-ranked-pools-upsert-2026-10-09).

Fakes only — a recording cursor stands in for psycopg, so nothing here
touches Neon, our node, or any DATABASE_URL (owner rule 2026-10-08).

Why this exists: `replace_amm_ranked_pools` used to run
`DELETE FROM amm_ranked_pools` (no WHERE) and then `cur.executemany` with
one INSERT per row. Against a 30k-row table that produced 2.047 BILLION
inserts and 2.047 billion deletes lifetime, and 152,523,005 separate
INSERT executions in pg_stat_statements. These tests pin the new
behaviour AND pin the old pattern as gone, so it cannot creep back.
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
        self.rowcount = 7          # pretend 7 stale pools were removed

    def execute(self, sql, params=None):
        self.sink.append((" ".join(str(sql).split()), params))

    def executemany(self, sql, seq):
        self.sink.append(("EXECUTEMANY " + " ".join(str(sql).split()), list(seq)))

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


# ── the old pattern must be gone ──

def test_no_unqualified_delete_of_the_whole_table(sink):
    db.replace_amm_ranked_pools([_pool("rA"), _pool("rB")])
    for s in _sql(sink):
        assert s != "DELETE FROM amm_ranked_pools", "full-table wipe is back"
    deletes = [s for s in _sql(sink) if s.startswith("DELETE FROM amm_ranked_pools")]
    assert len(deletes) == 1
    assert "WHERE snapshot_ts <" in deletes[0], "delete must be scoped"


def test_executemany_is_no_longer_used(sink):
    db.replace_amm_ranked_pools([_pool("rA"), _pool("rB")])
    assert not any(s.startswith("EXECUTEMANY") for s in _sql(sink)), \
        "one statement per row was the original cost bug"


# ── upsert shape ──

def test_insert_uses_on_conflict_upsert(sink):
    db.replace_amm_ranked_pools([_pool("rA")])
    ins = [s for s in _sql(sink) if s.startswith("INSERT INTO amm_ranked_pools")]
    assert len(ins) == 1
    assert "ON CONFLICT (amm_account) DO UPDATE SET" in ins[0]
    # every non-key column must be refreshed, or stale values would persist
    for col in ("pair", "fee_pct", "fee_raw", "amount_a", "amount_b", "asset_a",
                "asset_b", "tvl_usd", "tvl_status", "kind", "lp_token_value",
                "snapshot_ts"):
        assert f"{col} = EXCLUDED.{col}" in ins[0], f"{col} not refreshed"


def test_jsonb_casts_survive(sink):
    db.replace_amm_ranked_pools([_pool("rA")])
    ins = next(s for s in _sql(sink) if s.startswith("INSERT INTO"))
    assert ins.count("::jsonb") == 2


def test_placeholder_count_matches_columns():
    assert db._AMM_ROW_PLACEHOLDER.count("%s") == 13


# ── batching ──

def test_batches_instead_of_one_statement_per_row(sink):
    db.replace_amm_ranked_pools([_pool(f"r{i}") for i in range(2500)])
    ins = [s for s in _sql(sink) if s.startswith("INSERT INTO amm_ranked_pools")]
    assert len(ins) == 3, "2500 rows at chunk 1000 => 3 statements"


def test_realistic_pool_count_is_about_thirty_statements(sink):
    db.replace_amm_ranked_pools([_pool(f"r{i}") for i in range(30365)])
    ins = [s for s in _sql(sink) if s.startswith("INSERT INTO amm_ranked_pools")]
    assert len(ins) == 31          # was 30,365 separate executions
    assert db._AMM_UPSERT_CHUNK == 1000


def test_params_are_flat_and_sized_per_chunk(sink):
    db.replace_amm_ranked_pools([_pool(f"r{i}") for i in range(3)])
    _, params = next((s, p) for s, p in sink if s.startswith("INSERT INTO"))
    assert len(params) == 3 * 13


# ── dedupe + null key ──

def test_duplicate_accounts_are_deduped_last_wins(sink):
    db.replace_amm_ranked_pools([_pool("rA", pair="OLD"), _pool("rA", pair="NEW")])
    res = db.replace_amm_ranked_pools([_pool("rA", pair="OLD"),
                                       _pool("rA", pair="NEW")])
    assert res["upserted"] == 1, "two VALUES groups with one key would error"
    _, params = next((s, p) for s, p in sink if s.startswith("INSERT INTO"))
    assert "NEW" in params and "OLD" not in params


def test_row_without_account_is_skipped_and_counted(sink):
    res = db.replace_amm_ranked_pools([_pool("rA"), _pool(None), _pool("")])
    assert res["upserted"] == 1
    assert res["skipped_no_account"] == 2


def test_all_rows_skipped_writes_nothing(sink):
    res = db.replace_amm_ranked_pools([_pool(None)])
    assert res == {"upserted": 0, "deleted": 0, "skipped_no_account": 1}
    assert sink == []


# ── same observable result as the old rebuild ──

def test_one_snapshot_ts_shared_by_every_row(sink):
    db.replace_amm_ranked_pools([_pool(f"r{i}") for i in range(5)])
    _, params = next((s, p) for s, p in sink if s.startswith("INSERT INTO"))
    stamps = {params[i] for i in range(12, len(params), 13)}
    assert len(stamps) == 1, "readers key on MAX(snapshot_ts)"


def test_stale_delete_uses_the_same_snapshot_ts(sink):
    db.replace_amm_ranked_pools([_pool("rA")])
    _, ins_params = next((s, p) for s, p in sink if s.startswith("INSERT INTO"))
    del_sql, del_params = next((s, p) for s, p in sink if s.startswith("DELETE"))
    assert del_params == (ins_params[12],)


def test_delete_runs_after_the_inserts(sink):
    db.replace_amm_ranked_pools([_pool(f"r{i}") for i in range(1500)])
    kinds = [s.split()[0] for s in _sql(sink)]
    assert kinds.count("DELETE") == 1
    assert kinds.index("DELETE") == len(kinds) - 1, \
        "deleting first would reintroduce the empty-table window"


def test_return_reports_deleted_count(sink):
    res = db.replace_amm_ranked_pools([_pool("rA")])
    assert res["deleted"] == 7        # _FakeCursor.rowcount


# ── preserved guarantees ──

def test_empty_input_is_still_a_skip_not_a_wipe(sink):
    assert db.replace_amm_ranked_pools([]) is None
    assert sink == []


def test_missing_writer_conn_still_raises(monkeypatch):
    monkeypatch.setattr(db, "_get_writer_conn", lambda: None)
    with pytest.raises(db.WriterConnUnavailable):
        db.replace_amm_ranked_pools([_pool("rA")])


def test_failure_is_still_fail_loud_and_drops_the_conn(monkeypatch):
    dropped = []

    class _Boom(_FakeConn):
        def cursor(self):
            raise RuntimeError("neon down")

    monkeypatch.setattr(db, "_get_writer_conn", lambda: _Boom([]))
    monkeypatch.setattr(db, "_drop_writer_conn", lambda: dropped.append(1))
    with pytest.raises(Exception):
        db.replace_amm_ranked_pools([_pool("rA")])
    assert dropped == [1], "writer conn must be dropped on failure"


def test_unique_index_ddl_is_present():
    """ON CONFLICT (amm_account) needs the unique index to exist."""
    ddl = db.SCHEMA_SQL if hasattr(db, "SCHEMA_SQL") else ""
    if not ddl:
        src = open(os.path.join(os.path.dirname(__file__), "..", "db.py"),
                   encoding="utf-8").read()
        ddl = src
    assert "amm_ranked_pools_account_uidx" in ddl
    assert "CREATE UNIQUE INDEX IF NOT EXISTS amm_ranked_pools_account_uidx" in ddl
