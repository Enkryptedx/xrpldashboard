"""escrow_unlock_events — migration drift guard + UPSERT precedence.

Hermetic: source inspection and string normalisation. No DB, no network.

WHY A DRIFT GUARD EXISTS FOR THIS TABLE
---------------------------------------
The table has TWO definitions that must stay identical:

  * migrations/2026_11_01_escrow_unlock_events.sql — what the owner ran by
    hand in the Neon SQL Editor against production;
  * db._ESCROW_UNLOCK_EVENTS_DDL — what the owning walker executes every
    tick via ensure_escrow_unlock_events_table().

Production already has the migration's shape. If someone later adds a column
or a constraint to only ONE of the two, nothing fails loudly: CREATE TABLE
IF NOT EXISTS is a no-op against the existing table, so the walker would
keep running happily while writing rows that the migration's constraints
never agreed to — or, worse, the next fresh environment would get a
different table than production has. That is precisely the class of silent
gap that left evernorth_daily_snapshot absent from production on 2026-10-10
while every insert was swallowed by a best-effort except.

This guard makes the divergence a red test instead.
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import db  # noqa: E402

MIGRATION_PATH = REPO / "migrations" / "2026_11_01_escrow_unlock_events.sql"


def _strip_sql_comments(sql: str) -> str:
    """Drop `--` line comments. The migration carries a long rationale header
    and the db.py constant deliberately does not duplicate it, so comments
    are not part of the comparison."""
    return "\n".join(
        line.split("--")[0] for line in sql.splitlines()
    )


def _statements(sql: str) -> list[str]:
    """Normalise to a list of whitespace-collapsed, case-folded statements."""
    cleaned = _strip_sql_comments(sql)
    out = []
    for stmt in cleaned.split(";"):
        collapsed = re.sub(r"\s+", " ", stmt).strip().lower()
        if collapsed:
            out.append(collapsed)
    return out


@pytest.fixture(scope="module")
def migration_statements() -> list[str]:
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH}"
    return _statements(MIGRATION_PATH.read_text())


@pytest.fixture(scope="module")
def db_statements() -> list[str]:
    return _statements(db._ESCROW_UNLOCK_EVENTS_DDL)


class TestMigrationDriftGuard:
    def test_db_ddl_matches_the_applied_migration_exactly(
            self, migration_statements, db_statements):
        assert db_statements == migration_statements, (
            "db._ESCROW_UNLOCK_EVENTS_DDL has drifted from "
            "migrations/2026_11_01_escrow_unlock_events.sql.\n\n"
            "Production was migrated by hand from the .sql file, and the "
            "walker re-executes the db.py constant every tick. They must be "
            "the same statements, or a fresh environment gets a different "
            "table than production has.\n\n"
            f"only in db.py:     {[s for s in db_statements if s not in migration_statements]}\n"
            f"only in migration: {[s for s in migration_statements if s not in db_statements]}"
        )

    def test_both_define_exactly_one_table_and_three_indexes(
            self, migration_statements, db_statements):
        for label, stmts in (("migration", migration_statements),
                             ("db.py", db_statements)):
            tables = [s for s in stmts if s.startswith("create table")]
            indexes = [s for s in stmts if s.startswith("create index")]
            assert len(tables) == 1, f"{label}: expected 1 CREATE TABLE"
            assert len(indexes) == 3, (
                f"{label}: expected 3 CREATE INDEX (unlock_month, owner, "
                f"close_time) — the page groups by month, drills down by "
                f"owner, and the walker windows by close_time")

    def test_every_statement_is_idempotent(self, db_statements):
        """The walker runs this blob on EVERY tick. A statement without
        IF NOT EXISTS would raise on the second run and take the walker
        down with it."""
        for stmt in db_statements:
            assert "if not exists" in stmt, (
                f"non-idempotent statement in the walker's DDL: {stmt[:80]}")

    def test_all_four_constraints_are_present_in_both(
            self, migration_statements, db_statements):
        expected = [
            "escrow_unlock_events_tx_type_chk",
            "escrow_unlock_events_denom_chk",
            "escrow_unlock_events_source_chk",
            "escrow_unlock_events_amount_drops_chk",
        ]
        for label, stmts in (("migration", migration_statements),
                             ("db.py", db_statements)):
            blob = " ".join(stmts)
            for name in expected:
                assert name in blob, f"{label} is missing {name}"

    def test_unlock_month_is_a_stored_generated_column(self, db_statements):
        """The month label must be derived by Postgres, not written by the
        walker, so it cannot drift from close_time. It is also deliberately
        UTC-based: a monthly unlock lands at 00:00:10 UTC, which is the
        previous evening in ET."""
        blob = " ".join(db_statements)
        assert "generated always as" in blob
        assert "stored" in blob
        assert "date_trunc('month', close_time at time zone 'utc')" in blob

    def test_source_check_allows_any_witness_host(self, db_statements):
        """tools/anchor_nodes.py ships TWO full-history witnesses and the
        backfill falls back from one to the other mid-run. Pinning the
        constraint to a single literal would reject a legitimate fallback
        read."""
        blob = " ".join(db_statements)
        assert "source like 'witness:%'" in blob
        assert "'witness:s2-clio'" not in blob, (
            "constraint pinned to one witness literal — the xrplcluster "
            "fallback would be rejected mid-backfill")

    def test_primary_key_is_tx_hash(self, db_statements):
        """Owner ruling 2026-11-01: tx_hash, not (owner, sequence,
        observed_ledger_index). Escrow ledger objects carry no top-level
        Sequence field."""
        blob = " ".join(db_statements)
        assert "tx_hash text primary key" in blob
        assert "sequence" not in blob


class TestUpsertSourcePrecedence:
    """own-node must outrank any witness, and must never be downgraded."""

    @pytest.fixture(scope="class")
    def upsert_sql(self) -> str:
        """The function's source with Python string-literal boundaries removed.

        The statement is built from adjacent literals, so the raw source has
        `... now() " " WHERE ...` between fragments. Dropping the quote
        characters before collapsing whitespace reconstructs the SQL as the
        database actually receives it — otherwise an assertion spanning two
        literals fails for a formatting reason rather than a real one.
        """
        src = inspect.getsource(db.record_escrow_unlock_events)
        return re.sub(r"\s+", " ", src.replace('"', "")).lower()

    def test_upsert_is_keyed_on_tx_hash(self, upsert_sql):
        assert "on conflict (tx_hash) do update" in upsert_sql

    def test_update_is_gated_so_own_node_is_never_downgraded(self, upsert_sql):
        """The guard: update only when the STORED row is not own-node, or
        the INCOMING row is own-node. Without this WHERE clause, re-running
        the 12-month witness backfill would overwrite every sovereign read
        the daily walker had made."""
        assert "where escrow_unlock_events.source <> %s or excluded.source = %s" \
            in upsert_sql, (
                "the DO UPDATE ... WHERE precedence guard is missing or "
                "reworded — a witness backfill could overwrite own-node rows")

    def test_own_node_constant_is_used_rather_than_a_bare_literal(self):
        assert db.SOURCE_OWN_NODE == "own-node"
        src = inspect.getsource(db.record_escrow_unlock_events)
        assert "SOURCE_OWN_NODE" in src, (
            "precedence should reference db.SOURCE_OWN_NODE so the walker, "
            "the backfill and the constraint cannot disagree on spelling")

    def test_observed_at_is_refreshed_on_update(self, upsert_sql):
        assert "observed_at = now()" in upsert_sql

    def test_no_op_when_pg_unavailable(self, monkeypatch):
        """Must be safe to call in envs without Postgres (local dev, unit
        tests). Never raises, never connects."""
        def _boom():
            raise AssertionError("pg_connect should not be reached")

        monkeypatch.setattr(db, "pg_available", lambda: False)
        monkeypatch.setattr(db, "pg_connect", _boom)
        assert db.record_escrow_unlock_events(
            [{"tx_hash": "A" * 64, "tx_type": "EscrowFinish",
              "ledger_index": 1, "close_time": None, "denom": "XRP",
              "amount_drops": 1, "amount_json": "1",
              "escrow_owner": "r1", "tx_result": "tesSUCCESS",
              "source": "own-node"}]) is None
        assert db.read_escrow_unlock_events() == []
        assert db.read_escrow_unlock_months_present() == []

    def test_empty_row_list_writes_nothing_and_reports_zero(self, monkeypatch):
        def _boom():
            raise AssertionError("pg_connect should not be reached")

        monkeypatch.setattr(db, "pg_connect", _boom)
        assert db.record_escrow_unlock_events([]) == 0


class TestEnsureTableContract:
    def test_sets_lock_timeout_before_running_ddl(self):
        """ADD COLUMN takes ACCESS EXCLUSIVE and Postgres's lock queue is
        FIFO, so an ALTER appended here could queue ahead of every reader
        behind pg_dump's ACCESS SHARE during the 22:00 ET backup. The
        timeout must be set FIRST, not after."""
        src = inspect.getsource(db.ensure_escrow_unlock_events_table)
        assert "lock_timeout" in src
        assert src.index("lock_timeout") < src.index(
            "_ESCROW_UNLOCK_EVENTS_DDL")

    def test_lock_timeout_is_logged_as_its_own_category(self):
        src = inspect.getsource(db.ensure_escrow_unlock_events_table)
        assert "ensure_escrow_unlock_events_table_lock_timeout" in src, (
            "a deferred lock must not be logged under the same category as "
            "a genuinely broken statement")

    def test_returns_false_rather_than_raising_when_db_is_down(
            self, monkeypatch):
        monkeypatch.setattr(db, "_get_writer_conn", lambda: None)
        assert db.ensure_escrow_unlock_events_table() is False
