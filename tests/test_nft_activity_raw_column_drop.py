"""nft_activity: stop writing `raw`, then drop the column
(branch drop-nft-activity-raw-2026-10-26).

Charlie GO 2026-10-26, after the full-column archive to B2 was verified
(9,716,760/9,716,760 rows, per-batch sha256 match, rclone cryptcheck 0
differences) and nothing in the repo was found reading `raw` off
nft_activity (every SELECT against the table was grepped; the only hit
anywhere was the INSERT that wrote it).

Fence-post: the walker must stop WRITING raw before the migration DROPs
the column, or a late-landing row would 500. These tests pin that the
write path is gone from both ends (row-shaping in nft_activity_walker.py,
INSERT in db.py), independent of whether the migration has run yet in any
given environment.

Hermetic: no DB, no network.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import nft_activity_walker as naw  # noqa: E402


# ── row-shaping: nft_activity_walker.py no longer carries `raw` ──

def test_nft_rows_from_ledger_no_longer_includes_raw():
    txs = [
        {"hash": "A" * 64, "meta": {"TransactionResult": "tesSUCCESS"},
         "tx_json": {"TransactionType": "NFTokenMint", "NFTokenID": "00" * 32}},
    ]
    rows = naw._nft_rows_from_ledger(123, 843_000_000, txs)
    assert len(rows) == 1
    assert "raw" not in rows[0], "raw must be gone from the row shape, not just unused"


def test_nft_rows_from_ledger_row_keys_are_exactly_the_db_columns():
    txs = [
        {"hash": "B" * 64, "meta": {"TransactionResult": "tesSUCCESS"},
         "tx_json": {"TransactionType": "NFTokenMint", "NFTokenID": "00" * 32}},
    ]
    rows = naw._nft_rows_from_ledger(456, 843_000_000, txs)
    assert set(rows[0].keys()) == {
        "ledger_index", "close_time", "tx_hash", "tx_type", "nftoken_id",
        "issuer", "taxon", "buyer", "seller", "price_drops", "currency",
        "currency_issuer", "is_broker",
    }


# ── INSERT path: db.py no longer writes raw ──

class _FakeCursor:
    def __init__(self, sink):
        self.sink = sink
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.sink.append((" ".join(str(sql).split()), params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, sink):
        self.sink = sink

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self.sink)


@pytest.fixture
def sink(monkeypatch):
    import db
    calls = []
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "pg_connect", lambda: _FakeConn(calls))
    return calls


def _row(**over):
    r = {"ledger_index": 1, "close_time": 843_000_000, "tx_hash": "C" * 64,
         "tx_type": "Mint", "nftoken_id": "00" * 32, "issuer": "rI",
         "taxon": 0, "buyer": None, "seller": None, "price_drops": None,
         "currency": None, "currency_issuer": None, "is_broker": False}
    r.update(over)
    return r


def test_insert_no_longer_references_raw_column(sink):
    import db
    db.insert_nft_activity_batch([_row()])
    ins = next(s for s, _ in sink if s.startswith("INSERT INTO nft_activity"))
    assert "raw" not in ins, "INSERT must not reference the dropped column"
    assert "::jsonb" not in ins, "the jsonb cast was only ever for raw"


def test_insert_params_count_matches_13_non_raw_columns(sink):
    import db
    db.insert_nft_activity_batch([_row()])
    _, params = next((s, p) for s, p in sink if s.startswith("INSERT INTO"))
    assert len(params) == 13


def test_insert_ignores_a_stray_raw_key_rather_than_erroring(sink):
    """A caller that still passes `raw` (an un-updated fork, a stale
    import) must not crash -- it's simply ignored."""
    import db
    db.insert_nft_activity_batch([_row(raw={"TransactionType": "NFTokenMint"})])
    ins = next(s for s, _ in sink if s.startswith("INSERT INTO nft_activity"))
    assert "raw" not in ins


def test_insert_still_upserts_on_conflict_do_nothing(sink):
    import db
    db.insert_nft_activity_batch([_row()])
    ins = next(s for s, _ in sink if s.startswith("INSERT INTO"))
    assert "ON CONFLICT (tx_hash) DO NOTHING" in ins


# ── schema / migration ──

def test_ddl_no_longer_declares_a_raw_column():
    src = open(os.path.join(os.path.dirname(__file__), "..", "db.py"),
               encoding="utf-8").read()
    start = src.index("CREATE TABLE IF NOT EXISTS nft_activity (")
    end = src.index(");", start)
    block = src[start:end]
    assert "raw" not in block.lower(), \
        "nft_activity DDL must not declare raw so a fresh DB matches prod post-drop"


def test_migration_file_exists_and_drops_the_column():
    path = os.path.join(os.path.dirname(__file__), "..", "migrations",
                         "2026_10_26_drop_nft_activity_raw.sql")
    assert os.path.exists(path), "migration file must exist"
    sql = open(path, encoding="utf-8").read()
    assert "ALTER TABLE nft_activity DROP COLUMN IF EXISTS raw" in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql, "migration must be transactional"


def test_migration_is_idempotent_via_if_exists():
    path = os.path.join(os.path.dirname(__file__), "..", "migrations",
                         "2026_10_26_drop_nft_activity_raw.sql")
    sql = open(path, encoding="utf-8").read()
    assert "DROP COLUMN IF EXISTS raw" in sql, \
        "must be IF EXISTS so a re-run or already-migrated DB is a no-op"
