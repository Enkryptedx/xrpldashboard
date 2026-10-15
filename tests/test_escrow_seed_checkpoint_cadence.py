"""escrow_seed_walk — stage 2c: checkpoint cadence 1 → 50.

THE MEASUREMENT. The constant said "every page" and justified it as "cheap
relative to an RPC round trip". Measured 2026-10-15, that was false by ~1.7x
in the wrong direction:

    Neon round-trip (persistent conn)   27.4 ms median  (min 23.5, max 417.3)
    local ledger_data RPC                16.4 ms

The checkpoint cost MORE than the call it was being excused against. Over a
full 78,258-page walk: 78,258 x 27.4 ms = ~2,144 s, about 36 minutes (~23% of
the walk) spent waiting on Postgres instead of reading the ledger. At 50 it is
~1,566 writes, about 43 s.

WHY 50 IS SAFE, not merely cheaper. Three properties carry it, and each is
pinned below because raising the cadence is only defensible while they hold:

1. The FINAL page always checkpoints (`or not marker`), so a walk whose page
   count is not a multiple of the cadence cannot close with a stale total.
2. Re-walking a lost window is idempotent — escrow_set UPSERTs on
   escrow_index, so overlap at a resume boundary is expected rather than a
   primary-key error (stage-1 property).
3. The marker and the rejection tally are written in ONE statement, so they
   rewind together; a replayed page cannot double-count rejections.

Worst case is therefore re-reading ≤49 pages: a few seconds at any observed
rate (8.6 pages/s degraded, ~50 warm). Only a hard kill can lose a window at
all, since the exception path checkpoints before returning.

Hermetic: pure predicate + fake cursors. No DB, no network, main() never
called.
"""
import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "scripts", "escrow_seed_walk.py")


def _load():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    spec = importlib.util.spec_from_file_location(
        "_escrow_seed_cadence_under_test", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seed():
    return _load()


@pytest.fixture
def real_db():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    import db
    return db


class FakeCursor:
    def __init__(self):
        self.statements = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))


class FakeConn:
    def __init__(self):
        self.cur = FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur


# ── the new default ─────────────────────────────────────────────────
class TestDefaultCadence:
    def test_default_is_fifty(self, seed):
        assert seed.CHECKPOINT_EVERY_PAGES == 50

    def test_false_cheapness_claim_is_gone(self):
        """It cost 1.7x MORE than the RPC it was excused against."""
        with open(MODULE_PATH) as fh:
            src = fh.read()
        assert "Cheap relative to an RPC round trip" not in src

    def test_measured_latencies_recorded_in_source(self):
        """The decision rests on measurements; keep them next to the number
        so a future edit cannot silently discard the evidence."""
        with open(MODULE_PATH) as fh:
            src = fh.read()
        assert "27.4 ms" in src
        assert "16.4 ms" in src


# ── property 1: the final page always checkpoints ───────────────────
class TestFinalPageAlwaysCheckpoints:
    def test_exhausted_walk_checkpoints_on_any_page_number(self, seed):
        """marker falsy = walk exhausted. Must checkpoint regardless of
        where the page count happens to land."""
        for pages in (1, 49, 50, 78257, 78258):
            assert seed.should_checkpoint(pages, None, every=50) is True

    @pytest.mark.parametrize("empty", [None, "", {}, []])
    def test_all_falsy_markers_count_as_exhausted(self, seed, empty):
        assert seed.should_checkpoint(7, empty, every=50) is True

    def test_non_multiple_mid_walk_does_not_checkpoint(self, seed):
        assert seed.should_checkpoint(49, "marker", every=50) is False

    def test_multiple_mid_walk_checkpoints(self, seed):
        assert seed.should_checkpoint(50, "marker", every=50) is True
        assert seed.should_checkpoint(78250, "marker", every=50) is True


class TestCumulativeCountOnResume:
    """pages_done is cumulative and restored from the checkpoint, so the
    modulo lands wherever the interrupted attempt left it. That is fine —
    but it means the first post-resume checkpoint can be up to 49 pages in,
    which is exactly the bounded loss being accepted."""

    def test_resumed_offset_behaves(self, seed):
        assert seed.should_checkpoint(59148, "marker", every=50) is False
        assert seed.should_checkpoint(59150, "marker", every=50) is True

    def test_window_never_exceeds_the_cadence(self, seed):
        """No more than `every - 1` consecutive pages can pass without a
        checkpoint. This is the loss bound, asserted rather than asserted-at."""
        gap = 0
        worst = 0
        for pages in range(59148, 59148 + 500):
            if seed.should_checkpoint(pages, "marker", every=50):
                worst = max(worst, gap)
                gap = 0
            else:
                gap += 1
        assert worst <= 49


# ── the env override and its zero guard ─────────────────────────────
class TestEnvOverride:
    def test_override_respected(self, monkeypatch):
        monkeypatch.setenv("ESCROW_SEED_CHECKPOINT_EVERY", "7")
        assert _load().CHECKPOINT_EVERY_PAGES == 7

    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_zero_or_negative_floors_to_one(self, monkeypatch, value):
        """`pages_done % 0` would raise ZeroDivisionError inside the walk
        loop — a mis-set env var must not be able to kill the walker."""
        monkeypatch.setenv("ESCROW_SEED_CHECKPOINT_EVERY", value)
        mod = _load()
        assert mod.CHECKPOINT_EVERY_PAGES == 1
        assert mod.should_checkpoint(1, "marker") is True

    def test_predicate_floors_a_zero_argument_too(self, seed):
        assert seed.should_checkpoint(3, "marker", every=0) is True


# ── property 3: marker and tally rewind together ────────────────────
class TestMarkerAndTallyAreAtomic:
    def test_written_in_one_statement(self, real_db, monkeypatch):
        """If these were two statements, a kill between them would leave a
        marker and a tally describing different page counts — and a replayed
        page would then double-count rejections."""
        from collections import Counter
        conn = FakeConn()
        monkeypatch.setattr(real_db, "_get_writer_conn", lambda: conn)
        real_db.escrow_seed_checkpoint(
            1, {"m": "x"}, 59150, 11094,
            rejections=Counter({"iou_amount": 675}),
            entry_types=Counter({"Escrow": 11769}),
        )
        assert len(conn.cur.statements) == 1, (
            "marker and tally must share one UPDATE so they rewind in lockstep"
        )
        sql = conn.cur.statements[0][0]
        assert "marker=" in sql
        assert "rejections=" in sql
        assert "entry_types=" in sql


# ── the loop actually uses the predicate ────────────────────────────
def test_main_uses_the_predicate(seed):
    import inspect
    src = inspect.getsource(seed.main)
    assert "should_checkpoint(pages_done, marker)" in src
    assert "% CHECKPOINT_EVERY_PAGES" not in src
