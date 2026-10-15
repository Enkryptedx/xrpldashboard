"""escrow_seed_walk — stage 2a: the rejection tally is COUNTED and DURABLE.

Stage 1 proved the right entries are REJECTED (TokenEscrow, IOU dict Amounts).
tests/test_escrow_seed_walk.py §3 pins that behaviour. This file pins that the
rejections are **counted**, **attributed to a reason**, and **survive a kill**.

Why it exists, concretely. On 2026-10-15 the seed's 00:30 ET attempt was
SIGKILLed by its 7200s job cap after 59,148 pages. The tally lived only in a
stopgap wrapper's in-process Counter, and `escrow_set` stores ACCEPTED rows
only — so nothing on disk could say how many entries had been thrown away.
Recovering the number cost a full read-only re-walk of the same pinned ledger
(675 rejections, all iou_amount; trusted because the re-walk's accepted count
came back at exactly 11,094). The number the resumed run *did* report, 261, was
silently partial — it covered only its own 19,110 of 78,258 pages.

So an in-memory tally is wrong in two independent ways: it vanishes on a kill,
and it under-reports after a resume. Both are locked here.

Also locked: ONE source of truth for the accept/reject decision. The stopgap
wrapper carried its own `classify_rejection` mirroring the mapper's rules, and
they had already drifted — the wrapper checked missing-index before
unparseable-amount while the mapper does the reverse, so an entry with both
faults was labelled differently by the counter than by the code that rejected
it. `map_state_entry` now returns the reason from the same branch that makes
the decision.

Hermetic: pure functions and fake cursors. No DB, no network, `main()` never
called.
"""
import importlib.util
import inspect
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "scripts", "escrow_seed_walk.py")
MIGRATION_PATH = os.path.join(
    REPO_ROOT, "migrations",
    "2026_10_15_escrow_seed_progress_rejections.sql",
)
NEW_COLUMNS = ("rejections", "rejections_total", "entry_types")


@pytest.fixture(scope="module")
def seed():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    spec = importlib.util.spec_from_file_location(
        "_escrow_seed_tally_under_test", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def real_db():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    import db
    return db


class FakeCursor:
    def __init__(self, rows=(), rowcount=1):
        self.rows = list(rows)
        self.statements = []
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows=(), rowcount=1):
        self.cur = FakeCursor(rows, rowcount)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur


def _escrow(**over):
    base = {
        "LedgerEntryType": "Escrow",
        "Amount": "1000000",
        "index": "ABC123",
        "Account": "rOwner",
    }
    base.update(over)
    return base


# ── 1. every rejection reason, from the deciding branch ─────────────
class TestEveryRejectionReasonIsNamed:
    def test_accept_returns_row_and_no_reason(self, seed):
        row, reason = seed.map_state_entry(_escrow())
        assert reason is None
        assert row["amount_drops"] == 1000000
        assert row["escrow_index"] == "ABC123"

    def test_not_a_dict(self, seed):
        for junk in (None, "escrow", 42, ["x"]):
            assert seed.map_state_entry(junk) == (None, "not_a_dict")

    def test_token_escrow(self, seed):
        row, reason = seed.map_state_entry(
            _escrow(LedgerEntryType="TokenEscrow"))
        assert (row, reason) == (None, "token_escrow")

    def test_other_type_names_the_actual_type(self, seed):
        """A catch-all bucket hides what arrived; the type is appended."""
        row, reason = seed.map_state_entry(
            _escrow(LedgerEntryType="RippleState"))
        assert row is None
        assert reason == "other_type:RippleState"

    def test_iou_amount(self, seed):
        row, reason = seed.map_state_entry(
            _escrow(Amount={"currency": "USD", "value": "5",
                            "issuer": "rIssuer"}))
        assert (row, reason) == (None, "iou_amount")

    @pytest.mark.parametrize("amount", [None, "", "abc", "1.5x"])
    def test_unparseable_amount(self, seed, amount):
        row, reason = seed.map_state_entry(_escrow(Amount=amount))
        assert (row, reason) == (None, "unparseable_amount")

    def test_missing_index(self, seed):
        row, reason = seed.map_state_entry(_escrow(index=None))
        assert (row, reason) == (None, "missing_index_or_owner")

    def test_missing_owner(self, seed):
        row, reason = seed.map_state_entry(_escrow(Account=None))
        assert (row, reason) == (None, "missing_index_or_owner")

    def test_every_declared_reason_is_reachable(self, seed):
        """No dead reason in REJECTION_REASONS, and no reason produced that
        isn't declared. `other_type` is a documented prefix."""
        produced = set()
        for entry in (None,
                      _escrow(LedgerEntryType="TokenEscrow"),
                      _escrow(LedgerEntryType="Offer"),
                      _escrow(Amount={"currency": "USD"}),
                      _escrow(Amount="nope"),
                      _escrow(index=None)):
            _row, reason = seed.map_state_entry(entry)
            produced.add(reason.split(":", 1)[0])
        assert produced == set(seed.REJECTION_REASONS)


# ── 2. one source of truth (the wrapper's drift bug) ────────────────
class TestSingleSourceOfTruth:
    def test_public_mapper_delegates(self, seed):
        """escrow_row_from_state_entry returns None exactly when
        map_state_entry supplies a reason — they cannot disagree."""
        cases = [
            _escrow(), None, "junk",
            _escrow(LedgerEntryType="TokenEscrow"),
            _escrow(LedgerEntryType="Offer"),
            _escrow(Amount={"currency": "USD"}),
            _escrow(Amount="abc"),
            _escrow(index=None),
            _escrow(Account=None),
        ]
        for entry in cases:
            row, reason = seed.map_state_entry(entry)
            legacy = seed.escrow_row_from_state_entry(entry)
            assert (legacy is None) == (reason is not None)
            assert legacy == row

    def test_amount_fault_outranks_index_fault(self, seed):
        """The stopgap wrapper got this pair backwards. The reason must come
        from whichever branch actually rejects first."""
        row, reason = seed.map_state_entry(
            _escrow(Amount={"currency": "USD"}, index=None))
        assert (row, reason) == (None, "iou_amount")


# ── 3. counting happens where entries are mapped ────────────────────
class TestRowsFromPageCounts:
    def test_tally_and_histogram_incremented(self, seed):
        from collections import Counter
        tally, types = Counter(), Counter()
        page = [
            _escrow(index="A"),
            _escrow(index="B"),
            _escrow(LedgerEntryType="TokenEscrow", index="C"),
            _escrow(Amount={"currency": "USD"}, index="D"),
            _escrow(Amount={"currency": "EUR"}, index="E"),
            "junk",
        ]
        rows = seed.rows_from_page(page, tally, types)
        assert len(rows) == 2
        assert dict(tally) == {
            "token_escrow": 1, "iou_amount": 2, "not_a_dict": 1,
        }
        assert types["Escrow"] == 4
        assert types["TokenEscrow"] == 1
        assert types["<non_dict>"] == 1

    def test_counters_optional(self, seed):
        """Stage-1 call shape still works and cannot blow up on None."""
        assert len(seed.rows_from_page([_escrow()])) == 1
        assert seed.rows_from_page(None) == []
        assert seed.rows_from_page([]) == []

    def test_rejection_cannot_be_dropped_uncounted(self, seed):
        from collections import Counter
        tally = Counter()
        page = [_escrow(Amount={"currency": "USD"}) for _ in range(7)]
        rows = seed.rows_from_page(page, tally, Counter())
        assert rows == []
        assert sum(tally.values()) == 7


class TestFormatTally:
    def test_zero_reads_cleanly(self, seed):
        from collections import Counter
        assert seed.format_tally(Counter()) == "rejected 0"

    def test_deterministic_and_detailed(self, seed):
        from collections import Counter
        out = seed.format_tally(Counter({"iou_amount": 261,
                                         "token_escrow": 2}))
        assert out == "rejected 263 (iou_amount=261, token_escrow=2)"


# ── 4. durability: checkpoint carries the tally ─────────────────────
class TestCheckpointPersistsTally:
    def test_tally_written_with_the_marker(self, real_db, monkeypatch):
        from collections import Counter
        conn = FakeConn()
        monkeypatch.setattr(real_db, "_get_writer_conn", lambda: conn)
        ok = real_db.escrow_seed_checkpoint(
            1, {"m": "x"}, 59148, 11094,
            rejections=Counter({"iou_amount": 675}),
            entry_types=Counter({"Escrow": 11769}),
        )
        assert ok is True
        sql, params = conn.cur.statements[0]
        assert "rejections=COALESCE(%s::jsonb, rejections)" in sql
        assert "rejections_total=COALESCE(%s, rejections_total)" in sql
        assert "entry_types=COALESCE(%s::jsonb, entry_types)" in sql
        assert '"iou_amount": 675' in params[3]
        assert params[4] == 675          # total derived, not trusted separately

    def test_none_leaves_stored_values_untouched(self, real_db, monkeypatch):
        """A caller that doesn't track the tally must not blank it."""
        conn = FakeConn()
        monkeypatch.setattr(real_db, "_get_writer_conn", lambda: conn)
        real_db.escrow_seed_checkpoint(1, None, 10, 2)
        params = conn.cur.statements[0][1]
        assert params[3] is None and params[4] is None and params[5] is None

    def test_finish_stamps_final_tally(self, real_db, monkeypatch):
        from collections import Counter
        conn = FakeConn()
        monkeypatch.setattr(real_db, "_get_writer_conn", lambda: conn)
        real_db.escrow_seed_finish(
            1, "completed", "msg",
            rejections=Counter({"iou_amount": 936}),
            entry_types=Counter({"Escrow": 15553}),
        )
        sql, params = conn.cur.statements[0]
        assert "rejections=COALESCE" in sql
        assert params[3] == 936

    def test_finish_still_validates_status(self, real_db):
        with pytest.raises(ValueError):
            real_db.escrow_seed_finish(1, "donezo")


# ── 5. durability: resume carries the tally FORWARD ─────────────────
class TestResumeCarriesTallyForward:
    def test_resumable_selects_the_tally_columns(self, real_db, monkeypatch):
        conn = FakeConn(rows=[(1, 107665243, None, 59148, 11094,
                               {"iou_amount": 675}, {"Escrow": 11769})])
        monkeypatch.setattr(real_db, "_get_writer_conn", lambda: conn)
        row = real_db.escrow_seed_resumable()
        sql = conn.cur.statements[0][0]
        assert "rejections" in sql and "entry_types" in sql
        assert len(row) == 7, "resume must hand back both tally columns"

    def test_main_seeds_counters_from_stored(self, seed):
        """THE regression guard. Without this the final number describes only
        the resumed slice — the 2026-10-15 report accounted for 19,110 of
        78,258 pages and read as if it were complete."""
        src = inspect.getsource(seed.main)
        assert "tally.update(stored_rejections" in src
        assert "entry_types.update(stored_entry_types" in src

    def test_fresh_attempt_starts_counters_empty(self, seed):
        src = inspect.getsource(seed.main)
        assert "tally, entry_types = Counter(), Counter()" in src


# ── 6. the ALTER is safe and self-applying ──────────────────────────
class TestSchemaAdditive:
    def test_columns_in_both_ddl_and_migration(self, real_db):
        migration = open(MIGRATION_PATH).read()
        for col in NEW_COLUMNS:
            assert col in real_db._ESCROW_SET_DDL, f"{col} missing from DDL"
            assert col in migration, f"{col} missing from migration"

    def test_alters_are_additive_and_idempotent(self, real_db):
        migration = open(MIGRATION_PATH).read()
        assert migration.count("ADD COLUMN IF NOT EXISTS") == len(NEW_COLUMNS)
        assert real_db._ESCROW_SET_DDL.count(
            "ADD COLUMN IF NOT EXISTS") == len(NEW_COLUMNS)
        # Additive only: a stage-2 migration must never drop or retype.
        for banned in ("DROP COLUMN", "ALTER COLUMN", "NOT NULL"):
            assert banned not in migration

    def test_lock_timeout_precedes_the_ddl(self, real_db, monkeypatch):
        """The ORDERING is the fix. ADD COLUMN takes ACCESS EXCLUSIVE and the
        lock queue is FIFO, so an un-timed-out ALTER during the 22:00 ET
        pg_dump parks every later reader behind it."""
        conn = FakeConn()
        monkeypatch.setattr(real_db, "_get_writer_conn", lambda: conn)
        assert real_db.ensure_escrow_set_tables() is True
        stmts = [s[0] for s in conn.cur.statements]
        assert "SET lock_timeout" in stmts[0]
        assert "CREATE TABLE IF NOT EXISTS escrow_set" in stmts[1]

    def test_lock_timeout_gets_its_own_log_category(self, real_db,
                                                    monkeypatch):
        """Conflating 'deferred, retries next tick' with a real failure lets a
        genuinely broken ALTER hide as 'retry later'."""
        seen = []
        monkeypatch.setattr(real_db, "_log_err",
                            lambda cat, exc: seen.append(cat))
        monkeypatch.setattr(real_db, "_is_lock_timeout", lambda exc: True)

        class Boom(FakeConn):
            def cursor(self):
                raise RuntimeError("could not obtain lock")

        monkeypatch.setattr(real_db, "_get_writer_conn", lambda: Boom())
        assert real_db.ensure_escrow_set_tables() is False
        assert seen == ["ensure_escrow_set_tables_lock_timeout"]

    def test_other_errors_keep_the_generic_category(self, real_db,
                                                    monkeypatch):
        seen = []
        monkeypatch.setattr(real_db, "_log_err",
                            lambda cat, exc: seen.append(cat))
        monkeypatch.setattr(real_db, "_is_lock_timeout", lambda exc: False)

        class Boom(FakeConn):
            def cursor(self):
                raise RuntimeError("something else")

        monkeypatch.setattr(real_db, "_get_writer_conn", lambda: Boom())
        assert real_db.ensure_escrow_set_tables() is False
        assert seen == ["ensure_escrow_set_tables_failed"]


# ── 7. the stopgap wrapper is gone ──────────────────────────────────
def test_stopgap_wrapper_is_deleted():
    """Charlie's ruling 2026-10-14 18:23 ET: the one-night instrumentation
    must not become an orphan — a second, unversioned code path that quietly
    becomes load-bearing because it was easier to keep than to fold in."""
    workspace = os.path.join(
        os.path.expanduser("~"), ".openclaw", "workspace", "scripts")
    for name in ("escrow_seed_run_once.py", "escrow_seed_run_once.sh"):
        assert not os.path.exists(os.path.join(workspace, name)), (
            f"{name} still exists; its counting now lives in "
            f"scripts/escrow_seed_walk.py and it must be deleted"
        )
