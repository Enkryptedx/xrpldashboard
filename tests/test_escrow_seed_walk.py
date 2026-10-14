"""escrow_seed_walk — stage 1 of the escrow incremental redesign (2026-10-14).

Design: docs/ESCROW_INCREMENTAL_DESIGN_2026-10-14.md.

`supply_escrow_walker` recomputed XRP escrow supply by enumerating the whole
ledger state tree every hour. Cost scales with total state size, not escrow
count — measured 123 escrow objects in 500 pages against a ~78,215-page walk,
so ~99.75% of the traversal is discarded. Throughput fell 90.9 → 23.7 pages/s
while state grew only +0.4%, the walk stopped finishing inside its hard cap,
and the walker was paused.

This file pins the seed's correctness properties. The owner's requirements are
the first four and they are the ones that make the stage-2 delta stream sound:

1. **The pin is provable.** `parse_complete_ledgers` / `ledger_in_retained_history`
   decide whether the pinned ledger N is still in the node's retained history.
   Unparseable or empty input must read as NOT retained — "cannot prove
   retention" has to fail closed, because the alternative is trusting a set
   that may be a blend of two ledger states.
2. **Off-peak gating.** `node_is_off_peak` mirrors census_watcher's gate
   (load_factor <= 5.0, server_state in {full, proposing}). An unknown
   server_state is NOT good: absence of evidence is not evidence of health.
   This replaces the hard cap, which is what truncated walks mid-flight.
3. **Only real XRP Escrow objects count.** `escrow_row_from_state_entry`
   accepts `LedgerEntryType == "Escrow"` and nothing else. TokenEscrow is a
   DIFFERENT entry type (the 2026-07-12 census found 404 of those) and an IOU
   `Amount` arrives as a dict — coercing either into drops would corrupt the
   supply figure, which is the exact class of error this redesign exists to
   remove.
4. **Resumability is page-aligned.** Rows carry their own `escrow_index` so
   the UPSERT makes a re-read of an already-checkpointed page idempotent
   rather than a primary-key error. Overlap at the resume boundary is
   expected, not exceptional.
5. **db API contract.** Every `db.X` the module calls must exist. Twice today
   a call to a non-existent db helper sat inside a best-effort `except` and
   reported success: `write_walker_health_finish` (capper, shipped) and
   psycopg's `Json` adapter (caught pre-ship — db.py's house pattern is
   `json.dumps` + `::jsonb`). An AST scan generalises where a hardcoded list
   drifts.
6. **The DDL and its migration agree.** A table whose shape depends on which
   path created it is a latent production/dev divergence.

Hermetic: pure functions and fakes only. No DB, no network, `main()` never
called.
"""
import ast
import importlib.util
import os
import re
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "scripts", "escrow_seed_walk.py")
MIGRATION_PATH = os.path.join(
    REPO_ROOT, "migrations", "2026_10_14_escrow_set_and_seed_progress.sql"
)


@pytest.fixture(scope="module")
def seed():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    spec = importlib.util.spec_from_file_location(
        "_escrow_seed_walk_under_test", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 1. the pin must be provable, and fail closed ─────────────────────────
def test_parse_complete_ledgers_single_range(seed):
    assert seed.parse_complete_ledgers("107568362-107658759") == [
        (107568362, 107658759)
    ]


def test_parse_complete_ledgers_multiple_ranges(seed):
    assert seed.parse_complete_ledgers("10-20,30-40") == [(10, 20), (30, 40)]


def test_parse_complete_ledgers_bare_single_ledger(seed):
    assert seed.parse_complete_ledgers("42") == [(42, 42)]


@pytest.mark.parametrize("value", ["", None, "empty", "none", "garbage",
                                   "20-10", 12345, "a-b"])
def test_parse_complete_ledgers_unusable_is_empty(seed, value):
    """Reversed, empty and non-string inputs must not yield a range."""
    assert seed.parse_complete_ledgers(value) == []


def test_ledger_in_retained_history_inside(seed):
    assert seed.ledger_in_retained_history("100-200", 150) is True


def test_ledger_in_retained_history_boundaries_inclusive(seed):
    assert seed.ledger_in_retained_history("100-200", 100) is True
    assert seed.ledger_in_retained_history("100-200", 200) is True


def test_ledger_in_retained_history_outside(seed):
    assert seed.ledger_in_retained_history("100-200", 99) is False
    assert seed.ledger_in_retained_history("100-200", 201) is False


def test_ledger_in_retained_history_fails_closed(seed):
    """Cannot prove retention => NOT retained. Never trust a blended set."""
    assert seed.ledger_in_retained_history("empty", 150) is False
    assert seed.ledger_in_retained_history(None, 150) is False
    assert seed.ledger_in_retained_history("garbage", 150) is False


def test_ledger_in_retained_history_uses_real_measured_window(seed):
    """The 2026-10-14 reading: 107568362-107658759, span 90,397 ledgers."""
    cl = "107568362-107658759"
    assert seed.ledger_in_retained_history(cl, 107658759) is True
    assert seed.ledger_in_retained_history(cl, 107568361) is False
    (lo, hi), = seed.parse_complete_ledgers(cl)
    assert hi - lo == 90397


# ── 2. off-peak gate ─────────────────────────────────────────────────────
@pytest.mark.parametrize("state", ["full", "proposing"])
def test_off_peak_accepts_good_states_when_idle(seed, state):
    assert seed.node_is_off_peak({"server_state": state, "load_factor": 1}) is True


def test_off_peak_rejects_high_load(seed):
    assert seed.node_is_off_peak(
        {"server_state": "full", "load_factor": 99}
    ) is False


def test_off_peak_boundary_is_inclusive(seed):
    assert seed.node_is_off_peak(
        {"server_state": "full", "load_factor": 5.0}
    ) is True
    assert seed.node_is_off_peak(
        {"server_state": "full", "load_factor": 5.1}
    ) is False


@pytest.mark.parametrize("state", ["syncing", "connected", "tracking",
                                   None, "", "FULL"])
def test_off_peak_rejects_unknown_states(seed, state):
    """An unknown state is not evidence of health."""
    assert seed.node_is_off_peak(
        {"server_state": state, "load_factor": 1}
    ) is False


def test_off_peak_missing_load_factor_treated_as_idle(seed):
    """rippled omits load_factor when unloaded."""
    assert seed.node_is_off_peak({"server_state": "full"}) is True


def test_off_peak_rejects_unparseable_load_and_non_dict(seed):
    assert seed.node_is_off_peak(
        {"server_state": "full", "load_factor": "lots"}
    ) is False
    assert seed.node_is_off_peak(None) is False


# ── 3. only real XRP Escrow objects count ────────────────────────────────
def _escrow_entry(**over):
    entry = {
        "LedgerEntryType": "Escrow",
        "index": "ABC123",
        "Account": "rOwner",
        "Destination": "rDest",
        "Amount": "1000000",
        "Sequence": 7,
    }
    entry.update(over)
    return entry


def test_escrow_row_maps_fields(seed):
    row = seed.escrow_row_from_state_entry(_escrow_entry())
    assert row["escrow_index"] == "ABC123"
    assert row["owner"] == "rOwner"
    assert row["destination"] == "rDest"
    assert row["amount_drops"] == 1000000
    assert row["sequence_num"] == 7


def test_token_escrow_is_rejected(seed):
    """TokenEscrow is a different entry type; 404 existed at the 07-12 census."""
    assert seed.escrow_row_from_state_entry(
        _escrow_entry(LedgerEntryType="TokenEscrow")
    ) is None


@pytest.mark.parametrize("etype", ["AccountRoot", "RippleState", "Offer", None])
def test_other_entry_types_rejected(seed, etype):
    assert seed.escrow_row_from_state_entry(
        _escrow_entry(LedgerEntryType=etype)
    ) is None


def test_iou_amount_is_rejected_not_coerced(seed):
    """A dict Amount is an IOU. Treating it as drops would corrupt supply."""
    assert seed.escrow_row_from_state_entry(
        _escrow_entry(Amount={"currency": "USD", "value": "5",
                              "issuer": "rIss"})
    ) is None


@pytest.mark.parametrize("amount", [None, "", "abc", []])
def test_unusable_amount_rejected(seed, amount):
    assert seed.escrow_row_from_state_entry(
        _escrow_entry(Amount=amount)
    ) is None


def test_missing_index_or_owner_rejected(seed):
    e = _escrow_entry(); e.pop("index")
    assert seed.escrow_row_from_state_entry(e) is None
    e = _escrow_entry(); e.pop("Account")
    assert seed.escrow_row_from_state_entry(e) is None


def test_non_dict_entry_rejected(seed):
    assert seed.escrow_row_from_state_entry("nope") is None
    assert seed.escrow_row_from_state_entry(None) is None


# ── 4. page mapping / resumability shape ─────────────────────────────────
def test_rows_from_page_filters_non_escrow(seed):
    page = [
        _escrow_entry(index="A"),
        _escrow_entry(index="B", LedgerEntryType="AccountRoot"),
        _escrow_entry(index="C", LedgerEntryType="TokenEscrow"),
        _escrow_entry(index="D"),
    ]
    rows = seed.rows_from_page(page)
    assert [r["escrow_index"] for r in rows] == ["A", "D"]


def test_rows_from_page_handles_empty_and_none(seed):
    assert seed.rows_from_page([]) == []
    assert seed.rows_from_page(None) == []


def test_rows_carry_index_so_upsert_is_idempotent(seed):
    """Page-aligned resume re-reads a page; every row needs its key."""
    rows = seed.rows_from_page([_escrow_entry(index="X")])
    assert all(r.get("escrow_index") for r in rows)


# ── 5. db API contract (both of today's bugs were this shape) ────────────
def _db_attrs_called(path):
    with open(path) as fh:
        tree = ast.parse(fh.read())
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "db"
    }


def test_every_db_attribute_called_actually_exists(seed):
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    import db as real_db

    called = _db_attrs_called(MODULE_PATH)
    assert called, "no db.* references found - the scan broke, not the code"
    missing = sorted(n for n in called if not hasattr(real_db, n))
    assert not missing, f"module calls db functions that do not exist: {missing}"


def test_required_db_funcs_covers_what_the_module_calls(seed):
    called = _db_attrs_called(MODULE_PATH)
    assert called <= set(seed.REQUIRED_DB_FUNCS), (
        "db calls not covered by REQUIRED_DB_FUNCS: "
        f"{sorted(called - set(seed.REQUIRED_DB_FUNCS))}"
    )


def test_missing_db_funcs_detects_absence(seed):
    class Stub:
        pass

    missing = seed.missing_db_funcs(Stub())
    assert "escrow_seed_checkpoint" in missing
    assert "write_walker_health_end" in missing


def test_db_py_does_not_use_unimported_json_adapter():
    """db.py's house pattern is json.dumps + ::jsonb; Json() is not imported."""
    with open(os.path.join(REPO_ROOT, "db.py")) as fh:
        src = fh.read()
    assert "Json(" not in src, (
        "psycopg's Json adapter is not imported in db.py; use "
        "json.dumps(...) with a ::jsonb cast"
    )


# ── 6. DDL and migration must agree ──────────────────────────────────────
def _tables_in(sql):
    return set(
        re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", sql, re.IGNORECASE)
    )


def _indexes_in(sql):
    return set(
        re.findall(r"CREATE INDEX IF NOT EXISTS\s+(\w+)", sql, re.IGNORECASE)
    )


def test_db_ddl_matches_migration_file():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    import db as real_db

    with open(MIGRATION_PATH) as fh:
        migration = fh.read()
    ddl = real_db._ESCROW_SET_DDL

    assert _tables_in(ddl) == _tables_in(migration) == {
        "escrow_set", "escrow_seed_progress"
    }
    assert _indexes_in(ddl) == _indexes_in(migration)


def test_seed_statuses_match_the_check_constraint():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    import db as real_db

    assert set(real_db.ESCROW_SEED_STATUSES) == {
        "running", "completed", "abandoned"
    }
    assert "running" in real_db._ESCROW_SET_DDL


def test_escrow_seed_finish_rejects_invalid_status():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    import db as real_db

    with pytest.raises(ValueError):
        real_db.escrow_seed_finish(1, "donezo")


# ── httpx silenced at import scope (rlusd_refresher lesson) ──────────────
def test_httpx_silenced_at_import_scope():
    """Tens of thousands of ledger_data calls; INFO request logs are the leak.

    rlusd_refresher.err.log reached 80 MB this way (536,002 of 536,573 lines
    were httpx INFO). Silencing must sit at module scope, not inside a
    function that may not run.
    """
    with open(MODULE_PATH) as fh:
        tree = ast.parse(fh.read())
    module_level_calls = [
        n for n in tree.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
    ]
    src = ast.unparse(ast.Module(body=module_level_calls, type_ignores=[]))
    assert "httpx" in src and "WARNING" in src
