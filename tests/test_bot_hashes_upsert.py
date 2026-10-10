"""db._sync_key_table — diff-based sync replacing delete-and-rebuild (2026-10-10).

`refresh_bot_hash_tables()` used to `DELETE FROM page_view_bot_hashes` and
re-INSERT every row every cycle. Measured on prod 2026-10-10: 79,901 rows
held, exactly 6 changed per cycle (0.008%, 6 added / 0 removed). At the
writer's 5-minute cadence (288 cycles/day) that was ~46.0M row-writes/day to
apply ~1,700 real ones, matching the ~360M insert / ~355M delete counters in
pg_stat_statements over ~15.5 days. It also kept autovacuum clearing ~80k
dead tuples every 5 minutes and left the table at 47 MB for ~80k rows of two
short text columns.

The replacement writes only the difference. These tests drive it with a fake
cursor that models a real table as a set of key tuples and interprets the
three statements the function emits, so the FINAL TABLE CONTENTS can be
asserted — not just the SQL text.

What this file pins:

1. Final contents equal the desired set for adds, removes, mixed, no-change,
   full-populate and full-drain cycles. The old rebuild's postcondition was
   by definition exactly `desired` (it deleted everything then inserted
   `desired`), so `final == desired` IS the before/after equivalence check.
2. A no-change cycle issues ZERO write statements. This is the entire point
   of the change and the one assertion that would have failed before it.
3. Write volume equals the true delta, not the table size.
4. No TRUNCATE is ever emitted (it takes AccessExclusive and would queue
   behind the analytics render, then die at statement_timeout).
5. The insert arm carries ON CONFLICT DO NOTHING, so a retried or
   overlapping cycle is idempotent rather than a primary-key error.
6. Batching respects the Postgres int16 parameter ceiling that caused the
   silent no-op incident of 2026-09-05.
7. It works for both call sites' key shapes — (hash_type, hash) and
   (path, user_agent).

Hermetic: no DB, no network.
"""
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import db  # noqa: E402  (string builder only — no connection is opened)

_BOT_HASHES = ("page_view_bot_hashes", ("hash_type", "hash"))
_SCANNER = ("page_view_scanner_combos", ("path", "user_agent"))


class FakeCursor:
    """Models one keyed table as a set, interpreting the emitted SQL.

    Only the three statement shapes `_sync_key_table` produces are
    understood; anything else raises, so a future change that emits a
    TRUNCATE or an un-guarded INSERT fails here loudly instead of silently
    passing a text-only assertion.
    """

    def __init__(self, table, cols, rows=()):
        self.table = table
        self.cols = cols
        self.rows = set(rows)
        self.statements = []
        self._result = []

    # -- driver surface ---------------------------------------------------
    def execute(self, sql, params=None):
        params = list(params or [])
        flat = " ".join(sql.split())
        self.statements.append((flat, params))
        pairs = [(params[i], params[i + 1]) for i in range(0, len(params), 2)]
        if flat.startswith("SELECT"):
            assert f"FROM {self.table}" in flat
            self._result = sorted(self.rows)
        elif flat.startswith("INSERT"):
            assert "ON CONFLICT" in flat and "DO NOTHING" in flat
            self.rows |= set(pairs)  # DO NOTHING == ignore existing keys
        elif flat.startswith("DELETE"):
            assert "WHERE" in flat, "a bare DELETE would be the old rebuild"
            self.rows -= set(pairs)
        else:
            raise AssertionError(f"unexpected SQL: {flat[:70]}")

    def fetchall(self):
        return list(self._result)

    # -- assertions helpers ----------------------------------------------
    @property
    def writes(self):
        return [s for s, _ in self.statements
                if s.startswith(("INSERT", "DELETE"))]

    @property
    def row_writes(self):
        return sum(len(p) // 2 for s, p in self.statements
                   if s.startswith(("INSERT", "DELETE")))


def _sync(table_spec, current, desired):
    table, cols = table_spec
    cur = FakeCursor(table, cols, current)
    added, removed = db._sync_key_table(cur, table, cols, set(desired))
    return cur, added, removed


def _hashes(*pairs):
    return {(t, h) for t, h in pairs}


# ── 1. final contents match desired, every cycle shape ───────────────────
_BASE = _hashes(("visitor", "a"), ("visitor", "b"), ("ip_day", "x"))

_SCENARIOS = {
    "adds_only": (_BASE, _BASE | _hashes(("visitor", "c"), ("ip_day", "y"))),
    "removes_only": (_BASE, _hashes(("visitor", "a"))),
    "mixed": (_BASE, _hashes(("visitor", "a"), ("ip_day", "z"))),
    "no_change": (_BASE, _BASE),
    "full_populate": (set(), _BASE),
    "full_drain": (_BASE, set()),
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_final_contents_equal_desired(name):
    """The old rebuild deleted everything then inserted `desired`, so its
    postcondition was exactly `desired`. Asserting final == desired is
    therefore the before/after equivalence check, for every cycle shape."""
    current, desired = _SCENARIOS[name]
    cur, _, _ = _sync(_BOT_HASHES, current, desired)
    assert cur.rows == desired


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_reported_counts_match_the_real_delta(name):
    current, desired = _SCENARIOS[name]
    cur, added, removed = _sync(_BOT_HASHES, current, desired)
    assert added == len(desired - current)
    assert removed == len(current - desired)


# ── 2. the no-change cycle writes nothing ────────────────────────────────
def test_no_change_cycle_issues_zero_writes():
    """The whole point. Before this change an unchanged cycle still rewrote
    every row; now it must emit only the SELECT."""
    cur, added, removed = _sync(_BOT_HASHES, _BASE, _BASE)
    assert (added, removed) == (0, 0)
    assert cur.writes == []
    assert cur.row_writes == 0
    assert len(cur.statements) == 1
    assert cur.statements[0][0].startswith("SELECT")
    assert cur.rows == _BASE


def test_prod_shaped_cycle_writes_six_rows_not_eighty_thousand():
    """Prod shape on 2026-10-10: ~79,901 rows held, 6 added, 0 removed. The
    rebuild wrote ~159,800 rows to achieve this; the sync must write 6."""
    current = {("visitor", f"v{i}") for i in range(43895)}
    current |= {("ip_day", f"i{i}") for i in range(36006)}
    desired = current | {("visitor", f"new{i}") for i in range(3)}
    desired |= {("ip_day", f"new{i}") for i in range(3)}
    cur, added, removed = _sync(_BOT_HASHES, current, desired)
    assert (added, removed) == (6, 0)
    assert cur.row_writes == 6
    assert cur.rows == desired


# ── 3. write volume tracks the delta, not the table ──────────────────────
def test_write_volume_is_independent_of_table_size():
    small = {("visitor", f"v{i}") for i in range(10)}
    big = {("visitor", f"v{i}") for i in range(5000)}
    one_more = lambda s: s | {("ip_day", "brand-new")}  # noqa: E731
    cur_small, _, _ = _sync(_BOT_HASHES, small, one_more(small))
    cur_big, _, _ = _sync(_BOT_HASHES, big, one_more(big))
    assert cur_small.row_writes == cur_big.row_writes == 1


# ── 4. no TRUNCATE, no bare DELETE ───────────────────────────────────────
@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_never_emits_truncate_or_bare_delete(name):
    current, desired = _SCENARIOS[name]
    cur, _, _ = _sync(_BOT_HASHES, current, desired)
    for stmt, _params in cur.statements:
        assert "TRUNCATE" not in stmt
    for stmt in cur.writes:
        if stmt.startswith("DELETE"):
            assert "WHERE" in stmt


# ── 5. idempotency ───────────────────────────────────────────────────────
def test_insert_arm_is_on_conflict_do_nothing():
    cur, _, _ = _sync(_BOT_HASHES, _BASE, _BASE | _hashes(("visitor", "c")))
    inserts = [s for s in cur.writes if s.startswith("INSERT")]
    assert inserts and all(
        "ON CONFLICT (hash_type, hash) DO NOTHING" in s for s in inserts
    )


def test_second_identical_sync_is_a_no_op():
    """A retried or overlapping cycle must converge, not raise."""
    cur = FakeCursor(*_BOT_HASHES, rows=_BASE)
    table, cols = _BOT_HASHES
    desired = _BASE | _hashes(("ip_day", "y"))
    db._sync_key_table(cur, table, cols, desired)
    first = len(cur.statements)
    added, removed = db._sync_key_table(cur, table, cols, desired)
    assert (added, removed) == (0, 0)
    assert len(cur.statements) == first + 1  # just the SELECT
    assert cur.rows == desired


# ── 6. the int16 parameter ceiling (incident 2026-09-05) ─────────────────
def test_batches_respect_the_wire_protocol_param_ceiling():
    """A single INSERT VALUES cannot carry more than 65535 params (32767
    key pairs). Exceeding it raised an error that _log_err swallowed, so the
    refresh silently no-opped. Every emitted statement must stay under it."""
    desired = {("visitor", f"v{i}") for i in range(db._SYNC_CHUNK * 2 + 37)}
    cur, added, _ = _sync(_BOT_HASHES, set(), desired)
    assert added == len(desired)
    assert cur.rows == desired
    assert len(cur.writes) == 3  # ceil((2*CHUNK+37)/CHUNK)
    for _stmt, params in cur.statements:
        assert len(params) <= 65535


def test_chunk_ceiling_is_below_the_hard_limit():
    assert db._SYNC_CHUNK * 2 <= 65535


def test_deletes_are_chunked_too():
    current = {("visitor", f"v{i}") for i in range(db._SYNC_CHUNK + 5)}
    cur, _, removed = _sync(_BOT_HASHES, current, set())
    assert removed == len(current)
    assert cur.rows == set()
    assert len([s for s in cur.writes if s.startswith("DELETE")]) == 2


# ── 7. both call sites' key shapes ───────────────────────────────────────
def test_scanner_combos_key_shape_works():
    current = {("/a", "UA-1"), ("/b", "UA-2")}
    desired = {("/a", "UA-1"), ("/c", "UA-3")}
    cur, added, removed = _sync(_SCANNER, current, desired)
    assert cur.rows == desired
    assert (added, removed) == (1, 1)
    inserts = [s for s in cur.writes if s.startswith("INSERT")]
    assert all(
        "ON CONFLICT (path, user_agent) DO NOTHING" in s for s in inserts
    )


def test_select_reads_the_named_table_and_columns():
    cur, _, _ = _sync(_SCANNER, set(), {("/a", "UA-1")})
    select = cur.statements[0][0]
    assert select == "SELECT path, user_agent FROM page_view_scanner_combos"


def test_refresh_calls_sync_for_both_tables():
    """Guards the wiring: the helper is useless if a call site still rebuilds."""
    import inspect
    src = inspect.getsource(db.refresh_bot_hash_tables)
    assert src.count("_sync_key_table(") == 2
    assert "DELETE FROM page_view_bot_hashes" not in src
    assert "DELETE FROM page_view_scanner_combos" not in src
    assert "idx_pvbh_lookup" not in src.replace(
        "# table gets. idx_pvbh_lookup duplicated that key exactly,", ""
    )
