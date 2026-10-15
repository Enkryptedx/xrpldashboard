"""escrow_seed_walk — stage 2d: overlap guard + liveness output.

Both halves come from the same 2026-10-15 incident.

THE OVERLAP. The 00:30 attempt was SIGKILLed by its 7200s cap at ~06:30:22Z
and the 02:30 retry began at 06:30:30Z — an **8-second** gap, with nothing
preventing them from running together. Two walks against the same seed row
both write the same `marker` column, so the resume point would ping-pong
between two positions and neither walk's page count would mean anything.

WHY A PLAIN mkdir LOCK WOULD HAVE MADE THINGS WORSE. The failure mode being
defended against is a KILL, and a killed process cannot remove its own lock
directory. The house pattern (run_supply_escrow_walker.sh) is `mkdir` +
`trap rmdir EXIT` — correct there, but applied here it would leave the lock
held forever by a dead pid after exactly the event it was added for, turning
a recoverable interruption into a walker that never runs again. So the pid is
recorded and a lock whose pid is gone is stolen, loudly.

THE SILENCE. The walk emitted ONE line at 00:30:03 and nothing for the next
two hours. Both 7200s timers were armed against that silence and the walk was
indistinguishable from a hang to anything watching it. Note the time-based
threshold is the half that matters: a page-count threshold is a function of
throughput, so the slower the node gets the longer the walk goes quiet — and
a walk slow enough to trip a no-output timer is precisely the one that most
needs to prove it is alive.

Hermetic: real temp dirs, injected pid-liveness, pure predicates. No DB, no
network, main() never called.
"""
import importlib.util
import inspect
import os
import stat
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "scripts", "escrow_seed_walk.py")


@pytest.fixture(scope="module")
def seed():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    spec = importlib.util.spec_from_file_location(
        "_escrow_seed_lock_under_test", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lock_path(tmp_path):
    return str(tmp_path / "escrow_seed_walk.lock")


ALIVE = lambda _pid: True        # noqa: E731
DEAD = lambda _pid: False        # noqa: E731


# ── acquiring ───────────────────────────────────────────────────────
class TestAcquire:
    def test_free_lock_is_acquired_and_pid_recorded(self, seed, lock_path):
        assert seed.acquire_lock(lock_path, pid=4242) is True
        assert os.path.isdir(lock_path)
        assert seed.read_lock_pid(lock_path) == 4242

    def test_live_holder_blocks_second_instance(self, seed, lock_path):
        """The 8-second overlap window, closed."""
        assert seed.acquire_lock(lock_path, pid=1111, pid_alive=ALIVE) is True
        assert seed.acquire_lock(lock_path, pid=2222, pid_alive=ALIVE) is False
        assert seed.read_lock_pid(lock_path) == 1111, "holder must be intact"

    def test_dead_holder_is_stolen(self, seed, lock_path):
        """A SIGKILLed run cannot clean up. If its lock were respected the
        walker would never run again."""
        assert seed.acquire_lock(lock_path, pid=1111, pid_alive=ALIVE) is True
        assert seed.acquire_lock(lock_path, pid=2222, pid_alive=DEAD) is True
        assert seed.read_lock_pid(lock_path) == 2222

    def test_same_pid_reentry_is_not_a_steal(self, seed, lock_path):
        assert seed.acquire_lock(lock_path, pid=1111, pid_alive=ALIVE) is True
        assert seed.acquire_lock(lock_path, pid=1111, pid_alive=ALIVE) is True
        assert seed.read_lock_pid(lock_path) == 1111

    def test_lock_dir_without_pid_file_is_stolen(self, seed, lock_path):
        """A half-created lock (killed between mkdir and the pid write) must
        not be mistaken for a live holder."""
        os.mkdir(lock_path)
        assert seed.acquire_lock(lock_path, pid=3333, pid_alive=ALIVE) is True
        assert seed.read_lock_pid(lock_path) == 3333

    def test_unidentifiable_lock_is_not_retained(self, seed, tmp_path):
        """If the pid cannot be recorded, hold nothing: a lock whose owner is
        unknown can never be told apart from a stale one."""
        d = tmp_path / "ro.lock"
        d.mkdir()
        d.chmod(stat.S_IRUSR | stat.S_IXUSR)       # r-x: cannot create files
        try:
            assert seed.acquire_lock(str(d), pid=5555,
                                     pid_alive=ALIVE) is False
            # It must also not LEAVE the lock behind: release_lock runs on
            # this path, so the directory is gone rather than held by nobody.
            assert not d.exists()
        finally:
            # Guarded: the assertion above proves the dir is normally gone by
            # now, and an unconditional chmod would raise FileNotFoundError
            # and mask the real result.
            if d.exists():
                d.chmod(stat.S_IRWXU)


# ── releasing ───────────────────────────────────────────────────────
class TestRelease:
    def test_release_removes_the_lock(self, seed, lock_path):
        seed.acquire_lock(lock_path, pid=4242)
        seed.release_lock(lock_path)
        assert not os.path.exists(lock_path)

    def test_release_is_idempotent_and_never_raises(self, seed, lock_path):
        seed.release_lock(lock_path)
        seed.acquire_lock(lock_path, pid=1)
        seed.release_lock(lock_path)
        seed.release_lock(lock_path)

    def test_released_lock_is_reacquirable(self, seed, lock_path):
        seed.acquire_lock(lock_path, pid=1111, pid_alive=ALIVE)
        seed.release_lock(lock_path)
        assert seed.acquire_lock(lock_path, pid=2222, pid_alive=ALIVE) is True


class TestPidHelpers:
    def test_read_missing_pid_is_none(self, seed, lock_path):
        assert seed.read_lock_pid(lock_path) is None

    def test_read_garbage_pid_is_none(self, seed, lock_path):
        os.mkdir(lock_path)
        with open(os.path.join(lock_path, "pid"), "w") as fh:
            fh.write("not-a-pid")
        assert seed.read_lock_pid(lock_path) is None

    def test_own_pid_is_alive(self, seed):
        assert seed._pid_alive(os.getpid()) is True

    def test_absurd_pid_is_not_alive(self, seed):
        assert seed._pid_alive(999999999) is False

    @pytest.mark.parametrize("junk", [None, "abc", ""])
    def test_unparseable_pid_is_not_alive(self, seed, junk):
        assert seed._pid_alive(junk) is False


# ── progress output ─────────────────────────────────────────────────
class TestShouldLogProgress:
    def test_page_threshold_fires(self, seed):
        assert seed.should_log_progress(2000, 0.0) is True

    def test_below_both_thresholds_stays_quiet(self, seed):
        assert seed.should_log_progress(10, 5.0) is False

    def test_time_threshold_fires_on_a_slow_node(self, seed):
        """THE no-output defuse. Ten pages in 130s would never reach a
        2000-page threshold, and that is exactly the walk most likely to be
        killed for looking hung."""
        assert seed.should_log_progress(10, 130.0) is True

    def test_two_hours_of_silence_is_now_impossible(self, seed):
        """The 00:30 attempt ran ~7,195s emitting one line. At a 120s
        threshold that span cannot pass without output."""
        assert seed.should_log_progress(0, 7195.0) is True
        assert seed.should_log_progress(1, 120.0) is True

    def test_defaults(self, seed):
        assert seed.PROGRESS_EVERY_PAGES == 2000
        assert seed.PROGRESS_EVERY_SECONDS == 120.0

    @pytest.mark.parametrize("pages,secs", [(0, 0), (-1, -1)])
    def test_disabled_halves_do_not_fire(self, seed, pages, secs):
        assert seed.should_log_progress(10 ** 6, 10 ** 6,
                                        every_pages=pages,
                                        every_seconds=secs) is False

    def test_either_half_can_be_disabled_alone(self, seed):
        assert seed.should_log_progress(5000, 1.0, every_pages=0,
                                        every_seconds=120) is False
        assert seed.should_log_progress(5000, 1.0, every_pages=2000,
                                        every_seconds=0) is True


# ── wiring ──────────────────────────────────────────────────────────
class TestWiring:
    def test_lock_taken_before_any_health_write(self, seed):
        """A second instance must not stamp run_in_progress over the
        holder's row, so the lock precedes write_walker_health_start."""
        src = inspect.getsource(seed.main)
        assert src.index("acquire_lock()") < src.index(
            "write_walker_health_start"
        )

    def test_skip_is_a_clean_no_op(self, seed):
        """An overlapping fire is the lock working, not a failure to page, so
        the skip returns 0 rather than 1."""
        lines = inspect.getsource(seed.main).splitlines()
        i = next(n for n, line in enumerate(lines)
                 if "if not acquire_lock():" in line)
        assert lines[i + 1].strip() == "return 0"

    def test_release_registered_with_atexit(self, seed):
        src = inspect.getsource(seed.main)
        assert "atexit.register(release_lock)" in src

    def test_loop_emits_progress(self, seed):
        src = inspect.getsource(seed.main)
        assert "should_log_progress(" in src
        assert "last_log_pages, last_log_time = pages_done, now" in src
