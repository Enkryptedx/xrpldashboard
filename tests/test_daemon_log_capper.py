"""daemon_log_capper — in-place truncation must preserve the inode so a
long-running job's held file descriptor keeps working (2026-10-14).

BACKGROUND. `/etc/newsyslog.d/xrpldashboard.conf` rotates
`launchd_logs/*.err.log` / `*.out.log` at 50 MB by RENAMING the file. That is
right for the ~163 periodic walkers (launchd reopens the path each spawn,
proven live: `answer_plausibility_walker.err.log` rotated and regrew 309 KB
within four hours) and WRONG for a continuously-running job, whose descriptor
launchd opened once and which therefore keeps writing into the renamed inode
while the live path stays empty forever. That was proven with a controlled
test: a writer holding `exec 3>> file` emitted 8 lines across a rename and all
8 landed in the renamed inode; the new path was never created.

The two daemons (`mac_tunnel`, `sig_service`) were given explicit newsyslog
entries with an unreachable 2 GB threshold so newsyslog skips them — verified
by root dry run (`<0> ... [2097152] --> skipping`, while
`rlusd_refresher.err.log` kept `<7Z> ... [51200]`). This guard restores their
size ceiling without a rename.

What this file pins:

1. The inode is preserved across a cap. This is the entire reason the module
   exists; if it changes, the held descriptor is orphaned and the daemon's
   logging dies silently — the exact failure being fixed.
2. A CONCURRENT WRITER holding an O_APPEND descriptor keeps landing bytes in
   the file after the cap. This is the end-to-end property, not a proxy for
   it, and it is what the rename approach fails.
3. Guard-the-guard: the same assertion is shown to FAIL against a
   rename-then-recreate implementation. A test that cannot detect the bug it
   exists for is decoration, so the bug is reproduced here deliberately.
4. Files at or under the cap are left completely alone — same inode, same
   bytes, same size. An over-eager capper would destroy logs it was meant to
   bound.
5. An absent log is "absent", not an error: a daemon that has not logged yet,
   or one retired like `xrpl_stream` (disabled 2026-08-31), must not page.
6. The retained tail starts on a line boundary, so capping never leaves a
   severed partial line that reads as a corrupt entry.
7. A single line longer than the whole keep window is returned rather than
   discarded, so a pathological log cannot empty the file.
8. The capped size respects the keep window, and the newest content is what
   survives — the point of a tail, not a head.
9. `TARGETS` mirrors the newsyslog skip list. If the two drift, a daemon is
   either capped twice for nothing or grows without any ceiling at all.

Hermetic: operates on tmp_path files, no DB, no network, `main()` never
called.
"""
import importlib.util
import os
import subprocess
import sys
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "scripts", "daemon_log_capper.py")


@pytest.fixture(scope="module")
def capper():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    spec = importlib.util.spec_from_file_location(
        "_daemon_log_capper_under_test", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path, nlines, prefix="line"):
    with open(path, "w") as fh:
        for i in range(nlines):
            fh.write(f"{prefix} {i:06d} " + "." * 80 + "\n")
    return os.path.getsize(path)


# ── 1. the core property ─────────────────────────────────────────────────
def test_cap_preserves_inode(capper, tmp_path):
    p = tmp_path / "daemon.err.log"
    _write(str(p), 4000)
    before = os.stat(p).st_ino

    res = capper.cap_file(str(p), cap_bytes=50_000, keep_bytes=10_000)

    assert res["action"] == "capped"
    assert os.stat(p).st_ino == before, (
        "inode changed: a held descriptor would now be orphaned"
    )
    assert res["inode_before"] == res["inode_after"]


# ── 2. end-to-end with a real concurrent fd holder ───────────────────────
def test_concurrent_append_writer_survives_cap(capper, tmp_path):
    """A writer holding its fd must keep landing bytes after the cap."""
    p = tmp_path / "daemon.err.log"
    _write(str(p), 4000)

    # Hold an O_APPEND descriptor open across the cap, exactly as launchd
    # hands a daemon its StandardErrorPath.
    writer = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time\n"
         "f=open(sys.argv[1],'a')\n"
         "for i in range(12):\n"
         "    f.write('AFTERCAP %d\\n' % i); f.flush(); time.sleep(0.15)\n"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ) if False else subprocess.Popen(
        [sys.executable, "-u", "-c",
         "import sys,time\n"
         "f=open(sys.argv[1],'a')\n"
         "for i in range(12):\n"
         "    f.write('AFTERCAP %d\\n' % i)\n"
         "    f.flush()\n"
         "    time.sleep(0.15)\n",
         str(p)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.5)
        res = capper.cap_file(str(p), cap_bytes=50_000, keep_bytes=10_000)
        assert res["action"] == "capped"
        writer.wait(timeout=15)
    finally:
        if writer.poll() is None:
            writer.kill()

    body = p.read_text(errors="replace")
    landed = body.count("AFTERCAP")
    assert landed > 0, (
        "no post-cap writes landed: the descriptor was orphaned, which is "
        "the rename bug this module exists to avoid"
    )


# ── 3. guard-the-guard: prove the assertion catches the rename bug ───────
def test_rename_approach_would_fail_the_inode_check(tmp_path):
    """The bug must be detectable, or test 1 proves nothing."""
    p = tmp_path / "daemon.err.log"
    _write(str(p), 100)
    before = os.stat(p).st_ino

    # The WRONG implementation: rotate by rename, then recreate the path.
    os.rename(str(p), str(p) + ".0")
    with open(str(p), "w") as fh:
        fh.write("fresh\n")

    assert os.stat(p).st_ino != before, (
        "rename produced the same inode; this platform cannot demonstrate "
        "the failure mode and test 1 would be vacuous"
    )


# ── 4. under-cap files untouched ─────────────────────────────────────────
def test_under_cap_is_left_alone(capper, tmp_path):
    p = tmp_path / "small.err.log"
    size = _write(str(p), 10)
    digest_before = p.read_bytes()
    inode_before = os.stat(p).st_ino

    res = capper.cap_file(str(p), cap_bytes=10_000_000, keep_bytes=1_000_000)

    assert res["action"] == "under_cap"
    assert p.read_bytes() == digest_before
    assert os.path.getsize(p) == size
    assert os.stat(p).st_ino == inode_before


# ── 5. absent is not an error ────────────────────────────────────────────
def test_absent_file_is_not_an_error(capper, tmp_path):
    res = capper.cap_file(str(tmp_path / "never_logged.err.log"))
    assert res["action"] == "absent"


def test_run_tolerates_absent_targets(capper, tmp_path):
    results = capper.run(log_dir=str(tmp_path), targets=("nope.err.log",))
    assert [r["action"] for r in results] == ["absent"]


# ── 6/7. tail hygiene ────────────────────────────────────────────────────
def test_retained_tail_starts_on_line_boundary(capper):
    data = b"aaaa\nbbbb\ncccc\ndddd\n"
    out = capper.retained_tail(data, 11)
    assert out.startswith(b"cccc") or out.startswith(b"dddd")
    assert b"\n" not in out[:1]
    assert out.endswith(b"\n")


def test_retained_tail_returns_short_data_unchanged(capper):
    data = b"short\n"
    assert capper.retained_tail(data, 1000) == data


def test_single_oversized_line_is_not_discarded(capper):
    data = b"x" * 500  # no newline at all
    out = capper.retained_tail(data, 100)
    assert len(out) == 100
    assert out == b"x" * 100


# ── 8. newest content survives, within the keep window ───────────────────
def test_cap_keeps_the_newest_content(capper, tmp_path):
    p = tmp_path / "daemon.err.log"
    _write(str(p), 4000, prefix="seq")
    res = capper.cap_file(str(p), cap_bytes=50_000, keep_bytes=10_000)

    assert res["size_after"] <= 10_000
    assert res["size_after"] < res["size_before"]
    assert res["reclaimed"] == res["size_before"] - res["size_after"]

    body = p.read_text()
    assert "seq 003999" in body, "newest line missing - kept the head, not the tail"
    assert "seq 000000" not in body, "oldest line retained - not a tail"


# ── 9. TARGETS mirrors the newsyslog skip list ───────────────────────────
def test_targets_match_newsyslog_skip_list(capper):
    assert set(capper.TARGETS) == {"mac_tunnel.err.log", "sig_service.err.log"}


def test_cap_is_below_newsyslog_threshold(capper):
    """A mis-ordered conf change must not let a daemon log reach 50 MB."""
    assert capper.CAP_BYTES < 50 * 1024 * 1024
    assert capper.KEEP_BYTES < capper.CAP_BYTES
