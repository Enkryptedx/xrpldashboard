"""refresh_bot_hash_tables: the pattern arms run on a ts watermark (2026-11-14).

The two PATTERN arms (row_pred: 152 path LIKE + 55 user_agent ILIKE) had no ts
bound and rescanned all of page_views every 5 minutes -- a Parallel Seq Scan
over 1,024,161 rows / 626 MB, plan cost 251,671. It tripped statement_timeout
once on 2026-11-10 and then 3 times consecutively on 2026-11-14 (~25-26 s
against a ~25 s cap), flapping as the table grew past the limit.

row_pred is MONOTONIC -- a per-row predicate, and page_views rows are never
updated or deleted, so once a hash has one matching row it matches forever.
That makes a forward-only window EXACT for those arms. The WINDOWED arms
(growth 30 d, contact 400 d, claims 400 d) use exact-equality HAVING clauses
and so must keep recomputing and diffing; provenance markers are what let the
two kinds share one table without the diff deleting the other's rows.

What this file pins, driving the real function through a fake cursor that
models page_view_bot_hashes as CONTENTS (not just SQL text):

1. A new matching row past the watermark is caught incrementally, and the
   window is bounded above by a ceiling read off the ts index.
2. A changed pattern list invalidates the watermark and forces ONE full
   rescan, under a raised statement_timeout applied with SET LOCAL so it
   covers that run only.
3. A pattern-owned hash absent from the windowed desired set is NEVER
   un-tagged -- the DELETE is scoped by provenance.
4. is_bot_canary stays independent: it re-derives from page_views and must
   not read the writer's output table.

Hermetic: no DB, no network.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_REPO, os.path.join(_REPO, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db  # noqa: E402

SIG_KEY = db.BOT_HASH_PATTERN_SIG_KEY
WM_KEY = db.BOT_HASH_PATTERN_WATERMARK_KEY


class FakeCursor:
    """Interprets the statements refresh_bot_hash_tables emits.

    page_view_bot_hashes is modelled as {(hash_type, hash): {markers}} so the
    provenance assertions are about final CONTENTS. The pattern arms are
    modelled by their ts WINDOW, not by re-implementing 207 LIKE patterns --
    the window is what is under test, so each simulated page_views row simply
    declares whether row_pred matches it.
    """

    def __init__(self, rows, table, meta, ceiling, desired,
                 columns_present=("pattern_owned", "window_owned"),
                 ddl_raises=None):
        self.pv = rows              # [{ts, visitor_hash, ip_day_hash, match}]
        self.table = dict(table)    # {(hash_type, hash): {pattern, window}}
        self.meta = dict(meta)
        self.ceiling = ceiling
        self.injected_desired = set(desired)
        self.columns_present = tuple(columns_present)
        self.ddl_raises = ddl_raises
        self.desired: set = set()
        self.statements: list[tuple[str, list]] = []
        self.timeout_set: list = []
        self.lock_timeout_set: list = []
        self.meta_writes: list = []
        self._result: list = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        params = list(params or [])
        flat = " ".join(sql.split())
        self.statements.append((flat, params))
        self._result = []
        self.rowcount = 0

        if flat.startswith("CREATE TABLE") or flat.startswith("CREATE TEMP TABLE"):
            return
        if flat.startswith("ALTER TABLE page_view_bot_hashes"):
            assert "ADD COLUMN IF NOT EXISTS" in flat, flat
            assert "DEFAULT FALSE" in flat, (
                "a constant default is what keeps the migration metadata-only")
            if self.ddl_raises is not None:
                raise self.ddl_raises
            return
        if "FROM information_schema.columns" in flat:
            assert "page_view_bot_hashes" in flat, flat
            self._result = [(c,) for c in self.columns_present]
            return
        if flat.startswith("SET LOCAL statement_timeout"):
            self.timeout_set.append(params[0])
            return
        if flat.startswith("SET LOCAL lock_timeout"):
            self.lock_timeout_set.append(flat)
            return
        if "FROM page_view_classification_meta" in flat and flat.startswith("SELECT"):
            self._result = [(k, v) for k, v in self.meta.items() if k in params]
            return
        if flat.startswith("INSERT INTO page_view_classification_meta"):
            self.meta_writes.append(params)
            for i in range(0, len(params), 2):
                self.meta[params[i]] = params[i + 1]
            return
        if "COALESCE(MAX(ts), 0) FROM page_views" in flat:
            self._result = [(self.ceiling,)]
            return

        # ── pattern arms ────────────────────────────────────────────────
        if "hash, pattern_owned)" in flat:
            assert "ON CONFLICT (hash_type, hash) DO UPDATE" in flat, flat
            col = "visitor_hash" if "DISTINCT %s, visitor_hash" in flat else "ip_day_hash"
            windowed = "AND ts > %s AND ts <= %s" in flat
            lo, hi = (params[-2], params[-1]) if windowed else (None, None)
            hash_type = params[0]
            touched = 0
            for r in self.pv:
                if not r["match"] or not r.get(col):
                    continue
                if windowed and not (lo < r["ts"] <= hi):
                    continue
                key = (hash_type, r[col])
                cell = self.table.setdefault(key, {"pattern": False, "window": False})
                if not cell["pattern"]:
                    cell["pattern"] = True
                    touched += 1
            self.rowcount = touched
            return

        # ── windowed arms build the diff set ───────────────────────────
        if flat.startswith("DELETE FROM _desired_bot_hashes"):
            self.desired = set()
            return
        if flat.startswith("INSERT INTO _desired_bot_hashes"):
            self.desired |= self.injected_desired
            return
        if "hash, window_owned)" in flat:
            assert "FROM _desired_bot_hashes" in flat, flat
            n = 0
            for key in self.desired:
                cell = self.table.setdefault(key, {"pattern": False, "window": False})
                if not cell["window"]:
                    cell["window"] = True
                    n += 1
            self.rowcount = n
            return
        if flat.startswith("UPDATE page_view_bot_hashes p SET window_owned = FALSE"):
            assert "p.window_owned AND p.pattern_owned" in flat, flat
            for key, cell in self.table.items():
                if cell["window"] and cell["pattern"] and key not in self.desired:
                    cell["window"] = False
            return
        if flat.startswith("DELETE FROM page_view_bot_hashes p"):
            assert "p.window_owned AND NOT p.pattern_owned" in flat, (
                "the DELETE must be scoped by provenance", flat)
            doomed = [k for k, c in self.table.items()
                      if c["window"] and not c["pattern"] and k not in self.desired]
            for k in doomed:
                del self.table[k]
            self.rowcount = len(doomed)
            return

        # scanner-combo arms: out of scope here, answer empty
        if flat.startswith("SELECT"):
            self._result = []
            return
        if flat.startswith(("INSERT", "DELETE", "UPDATE")):
            return
        raise AssertionError(f"unmodelled SQL: {flat[:90]}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    # helpers
    def arm_sql(self, marker):
        return [s for s, _ in self.statements if marker in s]


def _run(monkeypatch, *, rows, table=None, meta=None, ceiling=5000, desired=(),
         columns_present=("pattern_owned", "window_owned"), ddl_raises=None):
    cur = FakeCursor(rows, table or {}, meta or {}, ceiling, desired,
                     columns_present=columns_present, ddl_raises=ddl_raises)

    class _Conn:
        rolled_back = False

        def cursor(self):
            return cur

        def commit(self):
            pass

        def rollback(self):
            type(self).rolled_back = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    cur.conn = _Conn

    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "pg_connect", lambda: _Conn())
    db.refresh_bot_hash_tables()
    return cur


def _pv(ts, vh=None, idh=None, match=True):
    return {"ts": ts, "visitor_hash": vh, "ip_day_hash": idh, "match": match}


# ── 1. a new row is caught incrementally ─────────────────────────────────

def test_new_row_past_the_watermark_is_caught_incrementally(monkeypatch):
    sig = db.bot_pattern_signature()
    cur = _run(
        monkeypatch,
        rows=[_pv(1000, vh="old"), _pv(4000, vh="fresh", idh="fresh_ip")],
        meta={SIG_KEY: sig, WM_KEY: "3000"},
        ceiling=5000,
    )
    # The window ran, not a full rescan.
    assert cur.timeout_set == [], "an incremental run must not raise the cap"
    arms = cur.arm_sql("hash, pattern_owned)")
    assert len(arms) == 2, arms
    for s in arms:
        assert "AND ts > %s AND ts <= %s" in s, s

    assert ("visitor", "fresh") in cur.table
    assert ("ip_day", "fresh_ip") in cur.table
    # 'old' sits behind the watermark: already derived on an earlier run, and
    # monotonicity is why not re-deriving it loses nothing.
    assert ("visitor", "old") not in cur.table


def test_window_is_bounded_by_a_ceiling_and_the_watermark_advances_to_it(monkeypatch):
    """A row arriving mid-transaction must be picked up NEXT run, not skipped
    by a watermark that claimed to cover it."""
    sig = db.bot_pattern_signature()
    cur = _run(
        monkeypatch,
        rows=[_pv(4000, vh="inside"), _pv(9999, vh="arrived_later")],
        meta={SIG_KEY: sig, WM_KEY: "3000"},
        ceiling=5000,
    )
    assert ("visitor", "inside") in cur.table
    assert ("visitor", "arrived_later") not in cur.table, (
        "above the ceiling: must wait for the next run")
    assert cur.meta[WM_KEY] == "5000", cur.meta
    assert cur.meta[SIG_KEY] == sig


# ── 2. a pattern change triggers the full rescan ─────────────────────────

@pytest.mark.parametrize("meta", [
    {},                                             # first run after deploy
    {"bot_hash_pattern_sig": "stale0000000000", "bot_hash_pattern_watermark_ts": "3000"},
    {"bot_hash_pattern_sig": None, "bot_hash_pattern_watermark_ts": "3000"},
])
def test_changed_or_missing_signature_forces_one_full_rescan(monkeypatch, meta):
    cur = _run(
        monkeypatch,
        rows=[_pv(10, vh="ancient"), _pv(4000, vh="recent")],
        meta={k: v for k, v in meta.items() if v is not None},
        ceiling=5000,
    )
    arms = cur.arm_sql("hash, pattern_owned)")
    assert len(arms) == 2, arms
    for s in arms:
        assert "AND ts > %s" not in s, "a rescan must be unbounded"
    # Raised for THIS run only -- SET LOCAL dies with the transaction.
    assert cur.timeout_set == [db.BOT_HASH_FULL_RESCAN_TIMEOUT_MS] , cur.timeout_set
    assert any("SET LOCAL statement_timeout" in s for s, _ in cur.statements)
    # The whole history is re-examined, so the oldest row is picked up.
    assert ("visitor", "ancient") in cur.table
    assert ("visitor", "recent") in cur.table
    # And the signature is recorded so the NEXT run goes incremental.
    assert cur.meta[SIG_KEY] == db.bot_pattern_signature()


def test_signature_tracks_content_not_order(monkeypatch):
    base = db.bot_pattern_signature()
    monkeypatch.setattr(db, "BOT_UA_PATTERNS", tuple(reversed(db.BOT_UA_PATTERNS)))
    assert db.bot_pattern_signature() == base, "reordering is cosmetic"
    monkeypatch.setattr(db, "BOT_UA_PATTERNS", db.BOT_UA_PATTERNS + ("%newbot%",))
    assert db.bot_pattern_signature() != base, "adding a pattern must invalidate"


def test_adding_a_pattern_makes_the_next_run_rescan(monkeypatch):
    """The end-to-end shape of requirement 1: a pattern edit alone (no meta
    surgery) is enough to force the rescan."""
    monkeypatch.setattr(db, "BOT_UA_PATTERNS", db.BOT_UA_PATTERNS + ("%brandnewbot%",))
    cur = _run(
        monkeypatch,
        rows=[_pv(10, vh="ancient")],
        meta={SIG_KEY: "whatever_was_stored", WM_KEY: "3000"},
        ceiling=5000,
    )
    assert cur.timeout_set == [db.BOT_HASH_FULL_RESCAN_TIMEOUT_MS]
    assert ("visitor", "ancient") in cur.table


# ── 3. the window never un-tags anything ─────────────────────────────────

def test_pattern_owned_rows_survive_a_window_that_no_longer_wants_them(monkeypatch):
    """The regression this whole design exists to prevent: the windowed arms'
    diff must not delete a hash the pattern arms derived."""
    existing = {
        ("visitor", "pattern_only"): {"pattern": True, "window": False},
        ("visitor", "both"): {"pattern": True, "window": True},
        ("visitor", "window_only"): {"pattern": False, "window": True},
    }
    cur = _run(
        monkeypatch,
        rows=[],                      # nothing new matches
        table=existing,
        meta={SIG_KEY: db.bot_pattern_signature(), WM_KEY: "3000"},
        ceiling=5000,
        desired=set(),                # windowed arms now want NOTHING
    )
    assert ("visitor", "pattern_only") in cur.table, "un-tagged a pattern row"
    assert ("visitor", "both") in cur.table, "un-tagged a dual-owned row"
    # Only a purely windowed row may be removed -- that is the un-flagging
    # the contact/growth exact-equality rules depend on.
    assert ("visitor", "window_only") not in cur.table
    # The marker is cleared on the dual-owned row, so it stays truthful.
    assert cur.table[("visitor", "both")]["window"] is False
    assert cur.table[("visitor", "both")]["pattern"] is True


def test_migration_default_state_deletes_nothing(monkeypatch):
    """Both columns default FALSE, so between the migration and the first run
    the DELETE owns nothing. Pins that ordering as safe."""
    existing = {
        ("visitor", "a"): {"pattern": False, "window": False},
        ("ip_day", "b"): {"pattern": False, "window": False},
    }
    cur = _run(
        monkeypatch,
        rows=[],
        table=existing,
        meta={SIG_KEY: db.bot_pattern_signature(), WM_KEY: "3000"},
        ceiling=5000,
        desired=set(),
    )
    assert ("visitor", "a") in cur.table
    assert ("ip_day", "b") in cur.table


# ── 4. DDL is catalog-gated, never issued per tick ───────────────────────
#
# Mirrors the three tests the amendment_majority_walker fix shipped with
# (owner ruling 2026-11-02). The first cut of THIS change reintroduced the
# same defect: two bare ADD COLUMN IF NOT EXISTS on every 5-minute tick.
# ADD COLUMN takes ACCESS EXCLUSIVE even when the column already exists, and
# Postgres's lock queue is FIFO, so inside the 22:00 ET pg_dump window the
# ALTER parks every later reader behind itself. That cost the majority walker
# 126 aborted ticks.

def test_refresh_checks_the_catalog_before_any_ddl(monkeypatch):
    """The provenance step must OPEN with a catalog read, not a DDL statement.
    information_schema.columns takes no lock on the table."""
    cur = _run(
        monkeypatch, rows=[], columns_present=(),
        meta={SIG_KEY: db.bot_pattern_signature(), WM_KEY: "3000"},
    )
    sqls = [s for s, _ in cur.statements]
    catalog = [i for i, s in enumerate(sqls)
               if "FROM information_schema.columns" in s]
    alters = [i for i, s in enumerate(sqls) if s.startswith("ALTER TABLE")]
    assert catalog, "no catalog read was issued"
    assert alters, "with both columns absent the ALTER must still run"
    assert catalog[0] < alters[0], (
        "the catalog read must precede any DDL", sqls[:6])
    first = sqls[catalog[0]]
    assert "page_view_bot_hashes" in first
    assert "pattern_owned" in first and "window_owned" in first


def test_refresh_skips_the_alter_entirely_when_the_columns_exist(monkeypatch):
    """Production steady state from the first run onward: both columns are
    present, so no ADD COLUMN is issued at all and nothing in the tick can
    take ACCESS EXCLUSIVE."""
    cur = _run(
        monkeypatch, rows=[_pv(4000, vh="x")],
        columns_present=("pattern_owned", "window_owned"),
        meta={SIG_KEY: db.bot_pattern_signature(), WM_KEY: "3000"},
    )
    alters = [s for s, _ in cur.statements if s.startswith("ALTER TABLE")]
    assert alters == [], f"expected no DDL in steady state, got {alters}"
    assert cur.lock_timeout_set == [], (
        "no lock_timeout is needed when no DDL runs")
    # ...and the tick still did its real work.
    assert ("visitor", "x") in cur.table


@pytest.mark.parametrize("present", [(), ("pattern_owned",), ("window_owned",)])
def test_alter_is_preceded_by_lock_timeout_when_columns_missing(
    monkeypatch, present
):
    """On a DB that genuinely lacks a column the DDL still runs -- but it must
    never queue an ACCESS EXCLUSIVE lock in front of the analytics readers,
    so SET LOCAL lock_timeout = '2s' is issued immediately before it. A
    partially-migrated table must add only what is missing."""
    cur = _run(
        monkeypatch, rows=[], columns_present=present,
        meta={SIG_KEY: db.bot_pattern_signature(), WM_KEY: "3000"},
    )
    sqls = [s for s, _ in cur.statements]
    alter_idx = [i for i, s in enumerate(sqls) if s.startswith("ALTER TABLE")]
    assert alter_idx, "expected the ALTER to run when a column is absent"
    preceding = sqls[alter_idx[0] - 1]
    assert "lock_timeout" in preceding and "2s" in preceding, preceding
    assert "SET LOCAL" in preceding, preceding
    # Only the genuinely-missing columns are added.
    added = {c for c in ("pattern_owned", "window_owned")
             if any(c in s for s in sqls if s.startswith("ALTER TABLE"))}
    assert added == {"pattern_owned", "window_owned"} - set(present), added


def test_a_locked_out_ddl_defers_instead_of_corrupting_the_tick(monkeypatch):
    """Unlike the majority walker, this tick's real work READS the new
    columns, so it cannot carry on without them. A lock timeout must roll
    back and leave the next tick to retry -- never run the arms against a
    table that lacks the columns."""
    class _LockNotAvailable(Exception):
        pass

    cur = _run(
        monkeypatch, rows=[_pv(4000, vh="x")], columns_present=(),
        meta={SIG_KEY: db.bot_pattern_signature(), WM_KEY: "3000"},
        ddl_raises=_LockNotAvailable("canceling statement due to lock timeout"),
    )
    assert cur.conn.rolled_back, "a deferred DDL must roll back"
    # The arms never ran, so nothing was written and the watermark did not move.
    assert cur.table == {}
    assert cur.meta_writes == [], "the watermark must not advance on a deferral"
    assert db._bot_hash_table_ready is False, (
        "readers must fall back to the legacy predicate meanwhile")


# ── 5. the canary stays independent ──────────────────────────────────────

def test_is_bot_canary_still_rederives_from_page_views():
    """Requirement 3. The canary must not start trusting the writer's output
    just because that output grew provenance columns -- that would make it
    tautological and it would stop catching writer drift."""
    import is_bot_canary as canary
    frag, _ = canary._legacy_bot_pred()
    assert "page_view_bot_hashes" not in frag
    for marker in ("pattern_owned", "window_owned",
                   "bot_hash_pattern_sig", "bot_hash_pattern_watermark_ts"):
        assert marker not in frag, (
            f"the canary must not read the writer's {marker}")
