"""db._bot_filter_sql tier-4 fallback: session-arm dedupe (2026-10-10).

Follow-up to the `is_bot_canary` BetterStack incident of 2026-10-09. That
canary's authoritative arm linked bot sessions with two `IN (subquery)`
arms over the WHOLE of page_views, undeduped — Postgres materialised every
matching hash ROW (~150k visitor_hash, ~134k ip_day_hash) and rescanned the
list per outer row, giving a 716,462,641 plan cost that blew the 25s
`statement_timeout` twice. `SELECT DISTINCT` dropped it to 484,683.

`db._bot_filter_sql()` carries the IDENTICAL pattern in its tier-4
last-resort fallback. It is **dormant**: tier 1 (`_is_bot_column_ready`,
hardcoded True at module level) serves production with a plain
`AND is_bot IS NOT TRUE` partial-index scan, so the 716M plan was never
observed on a live page. Any flip of that flag would hand the site the bad
plan, so it is deduped here pre-emptively.

Because the tier is dormant, these tests must force it: `_is_bot_column_ready`
and `_bot_hash_table_ready` False, `precomputed=None`.

What this file pins:

1. Both tier-4 session arms select DISTINCT.
2. For every tier-4 shape (human/bot x cohort-on/off), the generated SQL is
   byte-identical to the PRE-FIX SQL once the two `SELECT DISTINCT` tokens
   are reduced to `SELECT` — golden fixture
   (`tests/fixtures/bot_filter_tier4_legacy.json`) generated from the commit
   before the fix. `x IN (SELECT c)` and `x IN (SELECT DISTINCT c)` test
   membership, so duplicates cannot change which outer rows match: the
   classification contract is provably unchanged.
3. Parameters unchanged — same count, same order, including the scanner
   arm's trailing ts threshold. The clock is frozen so that bound is
   deterministic.
4. The scanner arm's PRE-EXISTING `COUNT(DISTINCT visitor_hash)` is
   untouched. Tier 4 already contained one DISTINCT before this change, so
   a naive "count the DISTINCTs" assertion would be wrong — the counts below
   are deliberate (1 before, 3 after).
5. Tiers 1-3 are not touched by this change — tier 1 in particular still
   returns the bare column predicate with no subquery at all.
6. The session arms stay unbounded in time (the rejected alternative fix).

Hermetic: no DB, no network. Module flags are set via monkeypatch so they
are restored for every other test sharing the process.
"""
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import db  # noqa: E402  (string builder only — no connection is opened)

_FIXTURE = os.path.join(
    _REPO, "tests", "fixtures", "bot_filter_tier4_legacy.json"
)

#: Must match the clock frozen when the fixture was generated.
_FROZEN_NOW = 1700000000.0

#: (fixture key, kind, _burst_cohort_table_ready)
_SHAPES = [
    ("human_cohort_off", "human", False),
    ("human_cohort_on", "human", True),
    ("bot_cohort_on", "bot", True),
]


@pytest.fixture(scope="module")
def legacy():
    with open(_FIXTURE) as fh:
        return json.load(fh)


@pytest.fixture
def tier4(monkeypatch):
    """Force the dormant tier-4 path and freeze the clock.

    Tier order in _bot_filter_sql: is_bot column -> bot-hash tables ->
    precomputed literals -> this fallback. Both flags must be False and
    `precomputed` left None to reach it.
    """
    monkeypatch.setattr(db, "_is_bot_column_ready", False)
    monkeypatch.setattr(db, "_bot_hash_table_ready", False)
    monkeypatch.setattr(db.time, "time", lambda: _FROZEN_NOW)

    def build(kind="human", cohort=False):
        monkeypatch.setattr(db, "_burst_cohort_table_ready", cohort)
        frag, params = db._bot_filter_sql(kind)
        return frag, list(params)

    return build


# ── 1. the arms are deduped ──────────────────────────────────────────────
@pytest.mark.parametrize("kind,cohort", [(k, c) for _, k, c in _SHAPES])
def test_both_session_arms_select_distinct(tier4, kind, cohort):
    frag, _ = tier4(kind, cohort)
    assert "SELECT DISTINCT visitor_hash FROM page_views" in frag
    assert "SELECT DISTINCT ip_day_hash FROM page_views" in frag


def test_no_undeduped_session_subquery_remains(tier4):
    frag, _ = tier4("human", True)
    assert "SELECT visitor_hash FROM page_views" not in frag
    assert "SELECT ip_day_hash FROM page_views" not in frag


# ── 2. semantics proof: identical to pre-fix SQL minus the hint ──────────
@pytest.mark.parametrize("key,kind,cohort", _SHAPES)
def test_fragment_matches_pre_fix_sql_once_distinct_removed(
    tier4, legacy, key, kind, cohort
):
    frag, _ = tier4(kind, cohort)
    assert frag.replace("SELECT DISTINCT ", "SELECT ") == legacy[key]["fragment"]


@pytest.mark.parametrize("key,kind,cohort", _SHAPES)
def test_pre_fix_fixture_really_is_undeduped(legacy, key, kind, cohort):
    """Guards the guard: a fixture regenerated from the FIXED code would make
    the comparison above compare the change against itself and pass forever."""
    assert "SELECT DISTINCT " not in legacy[key]["fragment"]


# ── 3. parameters unchanged ──────────────────────────────────────────────
@pytest.mark.parametrize("key,kind,cohort", _SHAPES)
def test_params_identical_to_pre_fix(tier4, legacy, key, kind, cohort):
    _, params = tier4(kind, cohort)
    assert params == legacy[key]["params"]


def test_params_are_three_rounds_plus_scanner_ts(tier4):
    """row_pred is bound three times (outer, session-visitor, session-ip_day)
    and the scanner arm appends its 7d ts threshold last. Pins the shape so a
    new bot pattern can never desynchronise the arms silently."""
    _, params = tier4("human", True)
    one_round = list(db.BOT_PATH_PATTERNS) + list(db.BOT_UA_PATTERNS)
    assert params == one_round * 3 + [int(_FROZEN_NOW) - 7 * 86400]


# ── 4. the scanner arm's pre-existing DISTINCT is untouched ──────────────
def test_scanner_arm_count_distinct_survives(tier4):
    frag, _ = tier4("human", True)
    assert "COUNT(*) <= COUNT(DISTINCT visitor_hash) * 1.10" in frag


def test_distinct_counts_are_exactly_one_before_and_three_after(tier4, legacy):
    """Tier 4 already had ONE DISTINCT (the scanner aggregate) before this
    change. Documented explicitly so nobody 'fixes' a count assertion by
    deleting the scanner arm's DISTINCT."""
    assert legacy["human_cohort_on"]["fragment"].count("DISTINCT") == 1
    frag, _ = tier4("human", True)
    assert frag.count("DISTINCT") == 3
    assert frag.count("SELECT DISTINCT") == 2


# ── 5. tiers 1-3 untouched ───────────────────────────────────────────────
def test_tier1_column_path_unchanged_and_subquery_free(monkeypatch):
    """What production actually runs. A dedupe in the dormant fallback must
    not perturb it, and it must stay a bare column predicate."""
    monkeypatch.setattr(db, "_is_bot_column_ready", True)
    human, hp = db._bot_filter_sql("human")
    bot, bp = db._bot_filter_sql("bot")
    assert (human, hp) == ("AND is_bot IS NOT TRUE", [])
    assert (bot, bp) == ("AND is_bot = TRUE", [])
    assert "FROM page_views" not in human
    assert "DISTINCT" not in human


def test_tier2_hash_table_path_has_no_page_views_subquery(monkeypatch):
    monkeypatch.setattr(db, "_is_bot_column_ready", False)
    monkeypatch.setattr(db, "_bot_hash_table_ready", True)
    frag, _ = db._bot_filter_sql("human")
    assert "FROM page_view_bot_hashes" in frag
    assert "FROM page_views" not in frag


def test_kind_all_is_still_a_no_op(monkeypatch):
    monkeypatch.setattr(db, "_is_bot_column_ready", False)
    monkeypatch.setattr(db, "_bot_hash_table_ready", False)
    assert db._bot_filter_sql("all") == ("", [])


def test_lite_filter_has_no_session_subqueries(monkeypatch):
    """_bot_filter_sql_lite is the /analytics/live path and is row-only by
    design — it never had the pattern, so it needs no dedupe."""
    monkeypatch.setattr(db.time, "time", lambda: _FROZEN_NOW)
    frag, _ = db._bot_filter_sql_lite("human")
    assert "FROM page_views" not in frag
    assert "DISTINCT" not in frag


# ── 6. the unbounded window is the invariant ─────────────────────────────
def test_session_arms_stay_unbounded_in_time(tier4):
    """Narrowing these subqueries with a ts bound would change which sessions
    classify as bot — a silent behaviour change dressed as an optimisation.
    It was the rejected alternative on 2026-10-10; this pins the rejection."""
    frag, _ = tier4("human", True)
    for key in ("visitor_hash", "ip_day_hash"):
        start = frag.index(f"SELECT DISTINCT {key} FROM page_views")
        sub = frag[start:frag.index("))", start)]
        assert "ts >" not in sub and "ts <" not in sub
