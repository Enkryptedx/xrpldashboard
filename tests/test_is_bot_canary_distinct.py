"""is_bot_canary session-arm dedupe — regression guard (2026-10-10).

Guards the fix for the BetterStack `is_bot_canary` incident of 2026-10-09.

The canary's authoritative comparison arm (`_legacy_bot_pred`) links a row
to a bot session via two `IN (subquery)` arms over the WHOLE of page_views
(deliberately unbounded: a visitor seen botting at ANY time taints the
session). Those subqueries were undeduped, so Postgres materialised every
matching hash ROW — ~150k visitor_hash and ~134k ip_day_hash at 2026-10-10 —
and rescanned that list per outer row. Plan cost for the trailing-7d count
reached 716,462,641 and the statement blew the 25s `statement_timeout` set
in `db.rpc_loop_safe_pg_connect()`: two consecutive canary FAILs (Oct 9
21:32 and 23:32 ET), last green Oct 8. Deduping the two subqueries drops
the same plan to 484,683 (~1,477x) and returns identical rows.

What this file pins:

1. Both session arms select DISTINCT.
2. The generated SQL is byte-identical to the PRE-FIX SQL once the two
   DISTINCT tokens are removed — a golden fixture taken from the commit
   before the fix (`tests/fixtures/is_bot_canary_legacy_pred.json`). This
   is the semantics proof: `x IN (SELECT c ...)` and
   `x IN (SELECT DISTINCT c ...)` test membership, so duplicates in the
   subquery cannot change which outer rows match. The fix adds a planner
   hint and alters nothing else about the authoritative arm.
3. The bound parameters are unchanged — same count, same order. A params
   drift would silently re-point the classifier.
4. All four classifier arms survive (row, session, scanner, cohort). The
   canary is worthless if a "speedup" quietly dropped one.
5. The counting query still binds exactly the two ts bounds followed by
   the predicate params, and still returns the DB's count untouched
   (driven through a fake connection).

Hermetic: no DB, no network. `db` is imported for its pattern tuples only.
"""
import importlib.util
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import db  # noqa: E402  (pattern tuples only — no connection is opened)

_FIXTURE = os.path.join(
    _REPO, "tests", "fixtures", "is_bot_canary_legacy_pred.json"
)


def _load_canary():
    """Import scripts/is_bot_canary.py by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "is_bot_canary_under_test",
        os.path.join(_REPO, "scripts", "is_bot_canary.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def canary():
    return _load_canary()


@pytest.fixture(scope="module")
def built(canary):
    frag, params = canary._legacy_bot_pred()
    return frag, list(params)


@pytest.fixture(scope="module")
def legacy():
    with open(_FIXTURE) as fh:
        return json.load(fh)


# ── 1. the arms are deduped ──────────────────────────────────────────────
def test_both_session_arms_select_distinct(built):
    frag, _ = built
    assert "SELECT DISTINCT visitor_hash FROM page_views" in frag
    assert "SELECT DISTINCT ip_day_hash FROM page_views" in frag


def test_distinct_appears_exactly_twice(built):
    """Only the two session arms get the hint. The scanner arm reads a small
    confirmed ledger and the cohort arm a pkey index scan — neither needs it,
    and a stray DISTINCT elsewhere would mean the edit slipped."""
    frag, _ = built
    assert frag.count("DISTINCT") == 2


# ── 2. semantics proof: identical to pre-fix SQL minus the hint ──────────
def test_fragment_matches_pre_fix_sql_once_distinct_removed(built, legacy):
    frag, _ = built
    assert frag.replace("SELECT DISTINCT ", "SELECT ") == legacy["fragment"]


def test_pre_fix_fixture_really_is_undeduped(legacy):
    """Guards the guard: if the fixture were regenerated from the FIXED code
    the test above would compare the change against itself and pass forever."""
    assert "DISTINCT" not in legacy["fragment"]


# ── 3. parameters unchanged ──────────────────────────────────────────────
def test_params_identical_to_pre_fix(built, legacy):
    _, params = built
    assert params == legacy["params"]


def test_params_are_three_rounds_of_row_pred_patterns(built):
    """row_pred is bound three times: outer, session-visitor, session-ip_day.
    Pins the count against the pattern tuples so adding a bot pattern can
    never desynchronise the arms without this failing."""
    _, params = built
    one_round = list(db.BOT_PATH_PATTERNS) + list(db.BOT_UA_PATTERNS)
    assert params == one_round * 3


# ── 4. all four classifier arms survive ──────────────────────────────────
def test_all_four_arms_present(built):
    frag, _ = built
    assert frag.startswith("AND NOT ")
    # row arm
    assert "path LIKE %s" in frag
    assert "COALESCE(user_agent, '') ILIKE %s" in frag
    # session arm (both keys)
    assert "visitor_hash IS NOT NULL AND visitor_hash IN (" in frag
    assert "ip_day_hash IS NOT NULL AND ip_day_hash IN (" in frag
    # scanner arm — the persistent confirmed ledger, not the 7d snapshot
    assert "FROM page_view_scanner_combos_confirmed" in frag
    # cohort arm
    assert "FROM burst_cohort_days" in frag


def test_session_arms_stay_unbounded_in_time(built):
    """The absence of a ts bound inside the session subqueries is the
    invariant, not an oversight: narrowing them would change which sessions
    count as bot and quietly weaken the canary. A ts bound was the rejected
    alternative fix on 2026-10-10 — this pins the rejection."""
    frag, _ = built
    for key in ("visitor_hash", "ip_day_hash"):
        start = frag.index(f"SELECT DISTINCT {key} FROM page_views")
        sub = frag[start:frag.index("))", start)]
        assert "ts >" not in sub and "ts <" not in sub and "ts >=" not in sub


# ── 5. the counting query is unchanged in shape and result ───────────────
class _FakeCursor:
    def __init__(self, store):
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._store["sql"] = sql
        self._store["params"] = list(params or [])

    def fetchone(self):
        return (4242,)


class _FakeConn:
    """Records the SQL instead of running it — no DB, no network."""

    def __init__(self):
        self.store = {}

    def cursor(self):
        return _FakeCursor(self.store)


def test_count_query_binds_ts_bounds_then_predicate_params(canary, built):
    frag, params = built
    conn = _FakeConn()
    canary._count_human_predicate(conn, 1_700_000_000, 1_700_086_400)
    sql = conn.store["sql"]
    assert "SELECT COUNT(*) FROM page_views" in sql
    assert "WHERE ts >= %s AND ts < %s" in sql
    assert frag in sql
    assert conn.store["params"] == [1_700_000_000, 1_700_086_400] + params


def test_count_query_returns_db_count_untouched(canary):
    conn = _FakeConn()
    got = canary._count_human_predicate(conn, 1_700_000_000, 1_700_086_400)
    assert got == 4242


def test_ts_bounds_are_coerced_to_int(canary):
    """Floats from time.time() must not reach the driver — an int bound is
    what matches page_views_ts_idx cleanly."""
    conn = _FakeConn()
    canary._count_human_predicate(conn, 1_700_000_000.9, 1_700_086_400.4)
    assert conn.store["params"][:2] == [1_700_000_000, 1_700_086_400]
