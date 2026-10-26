"""rank_amms.py: flock-based lock prevents concurrent instances
(branch rank-amms-lockfile-2026-10-26).

Charlie incident, 2026-10-26: a manually-started `rank_amms.py --reset`
and launchd's own independently-scheduled hourly tick ran concurrently
against the same checkpoint files and raced each other's tmp-file
rename -- one crashed with `FileNotFoundError: amm_ranked.json.tmp ->
amm_ranked.json`. launchd never double-fires ITS OWN job while an
instance it started is still alive; the hole is a process it didn't
start. A flock on a lock file closes that hole regardless of how the
second invocation got launched.

Hermetic: uses a real temp file for the actual flock exercise (flock
semantics can't be meaningfully faked), but never touches the real
amm_rank.lock path, amm_index.json, Postgres, or the network.
"""
import fcntl
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import rank_amms as ra  # noqa: E402


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    p = str(tmp_path / "amm_rank.lock")
    monkeypatch.setattr(ra, "LOCK_PATH", p)
    return p


def test_acquire_lock_succeeds_when_unheld(lock_path):
    fh = ra.acquire_lock()
    assert fh is not None
    fh.close()


def test_acquire_lock_writes_pid_and_timestamp(lock_path):
    fh = ra.acquire_lock()
    assert fh is not None
    fh.close()
    content = open(lock_path).read()
    assert str(os.getpid()) in content
    assert "T" in content  # ISO timestamp marker


def test_acquire_lock_fails_when_already_held(lock_path):
    fh1 = ra.acquire_lock()
    assert fh1 is not None
    try:
        fh2 = ra.acquire_lock()
        assert fh2 is None, "a second acquire must fail while the first fd is open"
    finally:
        fh1.close()


def test_lock_releases_on_fd_close(lock_path):
    fh1 = ra.acquire_lock()
    assert fh1 is not None
    fh1.close()
    fh2 = ra.acquire_lock()
    assert fh2 is not None, "closing the fd must release the flock immediately"
    fh2.close()


def test_lock_releases_on_process_death(lock_path):
    """The real incident scenario: a holder crashes/dies without a clean
    shutdown. A crash-exited process has its fds closed by the kernel,
    so the lock must NOT remain held -- this is flock's actual
    guarantee (unlike a plain pidfile-existence check, which would
    wrongly report the lock as still held after a crash)."""
    holder_pid = subprocess.Popen(
        [sys.executable, "-c",
         f"import fcntl, time; f = open({lock_path!r}, 'a+'); "
         f"fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB); "
         f"import os; os.kill(os.getpid(), 9)"],
    )
    holder_pid.wait(timeout=5)
    fh = ra.acquire_lock()
    assert fh is not None, "flock must release when the holder process is killed"
    fh.close()


def test_main_exits_0_without_doing_work_when_lock_held(lock_path, monkeypatch, tmp_path):
    """The actual fix: a second invocation that can't get the lock logs
    and returns 0 (forward progress / benign, same as the existing
    'already finished' early-return) rather than proceeding to race the
    checkpoint files."""
    holder_fh = open(lock_path, "a+")
    fcntl.flock(holder_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        called = []
        monkeypatch.setattr(ra, "parse_args", lambda: called.append(1))
        monkeypatch.setattr(sys, "argv", ["rank_amms.py"])
        rc = ra.main()
        assert rc == 0
        assert called == [], "main() must return before doing any real work when locked out"
    finally:
        holder_fh.close()
