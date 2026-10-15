"""escrow_seed_walk — stage 2b: the throughput number cannot overstate itself.

THE BUG. `pages_done` is cumulative across every attempt on a seed (it is
restored from the checkpoint on resume). `elapsed` covers only the current
invocation. The completed-message divided one by the other.

Measured 2026-10-15: the resumed run reported **36.7 pages/s** — 78,258
cumulative pages over its own 2,133s — when it had actually walked 19,110
pages in that window, i.e. **9.0 pages/s**.

Why that mattered more than a wrong log line:

* The true rate was ~5x below the 44.1 pages/s measured the day before, so
  the walk was badly degraded. The reported number made it look *better*
  than the healthy baseline, which is the one direction an error must never
  go — a throughput metric that lies upward turns a regression into a
  non-event.
* It was the figure that made the 7200s cap look generous. At 8.6 pages/s a
  cold 78,258-page walk needs ~2h32m, so the 00:30 attempt could never have
  finished inside 2h. Sizing the cap off 36.7 pages/s would have "proved"
  2h was ample and shipped the same failure again.

So the fix is not merely a corrected divisor: cumulative and this-run are
kept visibly separate in the message, and the rate is derived only from the
pages this invocation actually walked.

Hermetic: pure function. No DB, no network, `main()` never called.
"""
import importlib.util
import inspect
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "scripts", "escrow_seed_walk.py")


@pytest.fixture(scope="module")
def seed():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    spec = importlib.util.spec_from_file_location(
        "_escrow_seed_rate_under_test", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── the incident, encoded ────────────────────────────────────────────
class TestTheResumedRunIncident:
    """The real 2026-10-15 numbers, as a permanent regression anchor."""

    CUMULATIVE = 78258
    THIS_RUN = 19110
    ELAPSED = 2133.0

    def test_reports_the_true_rate(self, seed):
        out = seed.format_walk_progress(
            self.CUMULATIVE, self.THIS_RUN, self.ELAPSED)
        assert "9.0 pages/s" in out

    def test_does_not_report_the_inflated_rate(self, seed):
        """78258/2133 = 36.7 — the number that made a degraded walk look
        healthy and a too-small cap look generous."""
        out = seed.format_walk_progress(
            self.CUMULATIVE, self.THIS_RUN, self.ELAPSED)
        assert "36.7" not in out

    def test_keeps_cumulative_and_this_run_both_visible(self, seed):
        """A reader must be able to see progress AND throughput without
        being able to confuse one for the other."""
        out = seed.format_walk_progress(
            self.CUMULATIVE, self.THIS_RUN, self.ELAPSED)
        assert "78258 pages total" in out
        assert "19110 this run" in out
        assert "2133s" in out

    def test_rate_is_derived_from_this_run_not_cumulative(self, seed):
        """Same elapsed, same cumulative, different this-run => different
        rate. Pins that the divisor's numerator is the per-invocation count."""
        a = seed.format_walk_progress(78258, 19110, 2133.0)
        b = seed.format_walk_progress(78258, 38220, 2133.0)
        assert "9.0 pages/s" in a
        assert "17.9 pages/s" in b


# ── fresh walk: short form ───────────────────────────────────────────
class TestFreshWalk:
    def test_short_form_when_nothing_resumed(self, seed):
        """pages_this_run == pages_done, so "N total, N this run" is noise."""
        out = seed.format_walk_progress(19110, 19110, 2134.0)
        assert out == "19110 pages, 2134s, 9.0 pages/s"
        assert "total" not in out
        assert "this run" not in out

    def test_full_cold_walk_at_the_measured_rate(self, seed):
        """78,258 pages at ~8.6 pages/s is ~2h32m — the arithmetic that says
        a 2h cap cannot complete a cold seed."""
        out = seed.format_walk_progress(78258, 78258, 9100.0)
        assert "8.6 pages/s" in out


# ── division safety ──────────────────────────────────────────────────
class TestElapsedGuards:
    @pytest.mark.parametrize("elapsed", [0, 0.0, -1.0])
    def test_non_positive_elapsed_yields_zero_not_an_exception(self, seed,
                                                               elapsed):
        out = seed.format_walk_progress(100, 100, elapsed)
        assert "0.0 pages/s" in out

    def test_zero_pages_is_not_an_error(self, seed):
        out = seed.format_walk_progress(0, 0, 10.0)
        assert "0.0 pages/s" in out

    def test_sub_second_elapsed_does_not_explode(self, seed):
        out = seed.format_walk_progress(5, 5, 0.1)
        assert "50.0 pages/s" in out


# ── the call site ────────────────────────────────────────────────────
class TestCallSite:
    def test_baseline_captured_before_the_loop(self, seed):
        """pages_at_start must be taken AFTER resume restores pages_done and
        BEFORE the walk begins, or the delta is meaningless."""
        src = inspect.getsource(seed.main)
        assert "pages_at_start = pages_done" in src
        assert src.index("pages_at_start = pages_done") < src.index(
            "t0 = time.time()"
        ), "baseline must be captured before the clock starts"

    def test_message_uses_the_helper(self, seed):
        src = inspect.getsource(seed.main)
        assert "format_walk_progress(" in src
        assert "pages_done - pages_at_start" in src

    def test_old_buggy_expression_is_gone(self):
        """Guard against the division coming back by hand."""
        with open(MODULE_PATH) as fh:
            src = fh.read()
        assert "pages_done / elapsed" not in src
