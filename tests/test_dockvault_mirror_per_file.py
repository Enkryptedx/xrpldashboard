"""Per-file source-vs-mirror comparison in dockvault_mirror_freshness_canary.

The canary used to compare the two trees' NEWEST file mtimes. That metric is
wrong in both directions, and both are pinned here:

  * false POSITIVE -- a quiet tree that gains one much-newer file reads as
    days behind even though every file is mirrored (observed 2026-11-06,
    xrpl_test_private_infra reported source_ahead_299581s while complete);
  * false NEGATIVE -- a mirror missing most of the tree still passes as long
    as one recently-copied file is new enough, because a single newest mtime
    says nothing about coverage.

The replacement asks the only question that matters: does every source file
that has had time to be copied exist in the mirror, no older than at source?
"""
from __future__ import annotations

import os

import pytest

import dockvault_mirror_freshness_canary as cv


NOW = 1_800_000_000
CYCLE = cv.MIRROR_CYCLE_SECONDS
OLD = NOW - (10 * 86400)      # long past the cycle: must be mirrored
FRESH = NOW - 60              # inside the cycle: not yet due


def _write(root: str, rel: str, mtime: int) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("x")
    os.utime(path, (mtime, mtime))


# A stand-in for launchd/b2_backup.excludes carrying one pattern of each
# shape the real file uses. The real file is gitignored (public-repo
# boundary) and exists on no CI runner, so tests must never read it.
FIXTURE_EXCLUDES = """\
# comment line, ignored
.git/**
__pycache__/**
*.pyc
venv/**
.DS_Store
*.swp
*.log
launchd_logs/**
.env
.env.*
*.prev
"""


@pytest.fixture()
def trees(tmp_path, monkeypatch):
    src = tmp_path / "src"
    mirror_root = tmp_path / "mirror"
    (src).mkdir()
    (mirror_root / "sub").mkdir(parents=True)
    excl = tmp_path / "b2_backup.excludes"
    excl.write_text(FIXTURE_EXCLUDES)
    monkeypatch.setattr(cv, "MIRROR_ROOT", str(mirror_root))
    monkeypatch.setattr(cv, "EXCLUDES_FILE", str(excl))
    return str(src), str(mirror_root / "sub")


def test_every_file_mirrored_is_ok(trees):
    src, dst = trees
    for rel in ("a.txt", "deep/b.txt"):
        _write(src, rel, OLD)
        _write(dst, rel, OLD)
    r = cv.check_tree(src, "sub", now_ts=NOW)
    assert r["ok"] is True, r
    assert r["age_seconds"] == 0


def test_quiet_tree_with_one_new_file_is_not_flagged(trees):
    """The 2026-11-06 false positive, pinned.

    Source newest is minutes old, mirror newest is 10 days old, so
    newest-vs-newest subtracts ~10 days. Every file is nonetheless present
    and current in the mirror, so the honest answer is OK.
    """
    src, dst = trees
    _write(src, "old.txt", OLD)
    _write(dst, "old.txt", OLD)
    _write(src, "just_changed.txt", FRESH)   # no mirror run due yet
    r = cv.check_tree(src, "sub", now_ts=NOW)
    assert r["ok"] is True, r
    assert "mirrored" in r["reason"]


def _seed_all(state_path, root, when):
    """Record every file under `root` as first seen at `when`.

    Needed since 2026-11-11: absence is judged on ARRIVAL time, so a path the
    canary has never seen before is seeded silently instead of flagged. Tests
    about genuinely overdue files therefore have to say the file has already
    been sitting there, which is what this does.
    """
    import json as _json
    import os as _os
    seen = {}
    for dp, _dn, fns in _os.walk(root):
        for fn in fns:
            seen[_os.path.relpath(_os.path.join(dp, fn), root)] = when
    _json.dump({"sub": seen}, open(state_path, "w"))


def test_missing_file_past_the_cycle_is_flagged(trees, tmp_path):
    src, dst = trees
    state = str(tmp_path / "fs.json")
    _write(src, "present.txt", OLD)
    _write(dst, "present.txt", OLD)
    _write(src, "lost.txt", OLD)             # never arrived
    _seed_all(state, src, NOW - 10 * CYCLE)
    r = cv.check_tree(src, "sub", now_ts=NOW, state_path=state)
    assert r["ok"] is False, r
    assert "missing_1" in r["reason"]
    assert "lost.txt" in r["reason"], "name the file, not just a count"


def test_mirror_copy_older_than_source_is_flagged(trees, tmp_path):
    src, dst = trees
    state = str(tmp_path / "fs.json")
    _write(src, "edited.txt", OLD + 5000)
    _write(dst, "edited.txt", OLD)           # stale copy
    _seed_all(state, src, NOW - 10 * CYCLE)
    r = cv.check_tree(src, "sub", now_ts=NOW, state_path=state)
    assert r["ok"] is False, r
    assert "older_1" in r["reason"]
    assert r["age_seconds"] == 5000


def test_coverage_hole_is_not_hidden_by_one_fresh_copy(trees, tmp_path):
    """The false negative the old metric allowed.

    One file copied seconds ago makes the mirror's newest mtime current, so
    newest-vs-newest reports caught up while most of the tree is absent.
    """
    src, dst = trees
    for i in range(20):
        _write(src, f"f{i}.txt", OLD)
    _write(src, "recent.txt", OLD)
    _write(dst, "recent.txt", NOW - 10)      # the only thing in the mirror
    assert max(cv._iter_files(dst).values()) > max(
        v for k, v in cv._iter_files(src).items() if k != "recent.txt"), (
        "precondition: mirror newest beats source newest, so the OLD metric "
        "would have passed this tree")
    state = str(tmp_path / "fs.json")
    _seed_all(state, src, NOW - 10 * CYCLE)
    r = cv.check_tree(src, "sub", now_ts=NOW, state_path=state)
    assert r["ok"] is False, r
    assert "missing_20" in r["reason"]


def test_sub_second_rounding_is_tolerated(trees):
    src, dst = trees
    _write(src, "a.txt", OLD)
    _write(dst, "a.txt", OLD - cv.MIRROR_MTIME_SLACK_SECONDS)
    assert cv.check_tree(src, "sub", now_ts=NOW)["ok"] is True


def test_excluded_names_are_not_required_in_the_mirror(trees):
    src, dst = trees
    _write(src, "a.txt", OLD)
    _write(dst, "a.txt", OLD)
    _write(src, ".DS_Store", OLD)
    _write(src, "mod.pyc", OLD)
    _write(src, "__pycache__/x.txt", OLD)
    assert cv.check_tree(src, "sub", now_ts=NOW)["ok"] is True


def test_patterns_come_from_the_exclude_file_not_a_hardcoded_list(trees):
    """The 2026-11-06 false positive: 1429 'missing' files on xrpl_test,
    headed by .env.example, which the real file excludes via `.env.*` while
    the hand-copied list knew only .git/__pycache__/venv/node_modules."""
    src, dst = trees
    _write(src, "a.txt", OLD)
    _write(dst, "a.txt", OLD)
    for rel in (".env.example", ".env", "worker.log", "launchd_logs/x.txt",
                "snapshot.prev", "deep/nested/other.log"):
        _write(src, rel, OLD)
    r = cv.check_tree(src, "sub", now_ts=NOW)
    assert r["ok"] is True, r


def test_a_real_missing_file_is_still_caught_alongside_excluded_ones(trees, tmp_path):
    """Guard the guard: broad excludes must not swallow genuine findings."""
    src, dst = trees
    state = str(tmp_path / "fs.json")
    _write(src, "keep.txt", OLD)
    _write(dst, "keep.txt", OLD)
    _write(src, "ignored.log", OLD)        # excluded
    _write(src, "really_missing.txt", OLD)  # not excluded
    _seed_all(state, src, NOW - 10 * CYCLE)
    r = cv.check_tree(src, "sub", now_ts=NOW, state_path=state)
    assert r["ok"] is False, r
    assert "missing_1" in r["reason"]
    assert "really_missing.txt" in r["reason"]
    assert "ignored.log" not in r["reason"]


def test_symlinks_are_skipped_because_rclone_does_not_copy_them(trees):
    """The 46-day 'missing' permissioned_domains_walker.plist.

    It is a symlink into ~/xrpl_test. rclone runs with neither --links nor
    --copy-links, so it never copies it; os.stat FOLLOWS the link and reports
    the target's mode, so S_ISREG passed and it read as a real missing file.
    """
    src, dst = trees
    _write(src, "real.txt", OLD)
    _write(dst, "real.txt", OLD)
    target = os.path.join(src, "real.txt")
    link = os.path.join(src, "link_to_real.plist")
    os.symlink(target, link)
    assert os.path.isfile(link), "precondition: stat() sees a regular file"
    matchers, prune, err = cv.load_exclude_matchers(cv.EXCLUDES_FILE)
    assert err is None
    assert "link_to_real.plist" not in cv._iter_files(src, matchers, prune)
    r = cv.check_tree(src, "sub", now_ts=NOW)
    assert r["ok"] is True, r


def test_unreadable_exclude_file_fails_loud_instead_of_excluding_nothing(
        trees, monkeypatch):
    """launchd/ is gitignored, so the real file is absent outside this Mac.
    Treating that as 'exclude nothing' would report thousands of phantom
    files and bury a real gap, so it must be a FAILED check."""
    src, dst = trees
    _write(src, "a.txt", OLD)
    _write(dst, "a.txt", OLD)
    monkeypatch.setattr(cv, "EXCLUDES_FILE", "/nonexistent/b2_backup.excludes")
    r = cv.check_tree(src, "sub", now_ts=NOW)
    assert r["ok"] is False
    assert r["reason"].startswith("excludes_unreadable")


@pytest.mark.parametrize("pattern,path,expected", [
    (".git/**", ".git/config", True),
    (".git/**", "a/b/.git/config", True),      # unanchored: any depth
    ("/.git/**", "a/.git/config", False),      # anchored: root only
    ("*.log", "deep/nested/x.log", True),
    ("*.log", "x.logger", False),
    (".env.*", ".env.example", True),
    (".env.*", ".environment", False),
    ("*.pyc", "pkg/mod.pyc", True),
    ("launchd_logs/**", "launchd_logs/a/b.txt", True),
    ("*.log", "token_names.json", False),
])
def test_rclone_glob_translation(pattern, path, expected):
    import re as _re
    rx = _re.compile(cv._rclone_glob_to_regex(pattern))
    assert bool(rx.match(path)) is expected, (pattern, path)


def test_empty_source_and_missing_mirror_keep_their_verdicts(trees):
    src, dst = trees
    assert cv.check_tree(src, "sub", now_ts=NOW)["reason"] == "source_empty"
    _write(src, "a.txt", OLD)
    r = cv.check_tree(src, "sub", now_ts=NOW)
    assert r["ok"] is False
    assert r["reason"] == "mirror_empty_but_source_has_files"
    r2 = cv.check_tree(src, "no_such_sub", now_ts=NOW)
    assert r2["ok"] is False and r2["reason"] == "mirror_missing"


def test_default_now_ts_runs_without_injection(trees):
    """Call check_tree the way the canary itself does: no now_ts.

    Every other test here injects now_ts, so the `int(_time.time())` default
    branch never executed and a missing `import time` shipped green -- the
    canary then crashed with NameError on its first real run. A test suite
    that only ever exercises the injected path cannot see that, so this test
    deliberately takes the default.
    """
    src, dst = trees
    _write(src, "a.txt", OLD)
    _write(dst, "a.txt", OLD)
    r = cv.check_tree(src, "sub")
    assert r["ok"] is True, r


# ── arrival time, not mtime (2026-11-11) ───────────────────────────────────
#
# mtime answers "when was this content last written". The canary needs "how
# long has this path existed HERE unmirrored". They diverge whenever a file
# arrives with an old timestamp: `mv` of a directory leaves the files' mtime
# AND ctime untouched, and `cp -p` restores the source mtime. On 2026-11-10
# that produced 71 phantom findings for files minutes old in their new home.
#
# Ancestor-directory ctime was rejected: log dirs churn constantly, so a
# genuinely missing file in launchd_logs would be permanently exempt. These
# tests pin both halves -- the moved file goes quiet, the busy folder does not.

def test_moved_old_file_is_not_flagged_until_a_cycle_passes(trees, tmp_path):
    src, dst = trees
    state = str(tmp_path / "first_seen.json")
    _write(src, "keep.txt", OLD)
    _write(dst, "keep.txt", OLD)
    # Arrives with an ancient mtime and no mirror copy, exactly like a file
    # inside a directory that was just `mv`d, or one placed with `cp -p`.
    _write(src, "archive/old_moved.png", OLD)

    first = cv.check_tree(src, "sub", now_ts=NOW, state_path=state)
    assert first["ok"] is True, f"a just-seen path must not alarm: {first}"

    # Still inside one cycle: silent.
    mid = cv.check_tree(src, "sub", now_ts=NOW + CYCLE - 10, state_path=state)
    assert mid["ok"] is True, mid

    # Past one cycle with the mirror still lacking it: now it is real.
    late = cv.check_tree(src, "sub", now_ts=NOW + CYCLE + 60, state_path=state)
    assert late["ok"] is False, late
    assert "old_moved.png" in late["reason"], late["reason"]


def test_missing_file_in_a_busy_folder_still_alarms(trees, tmp_path):
    """The case ancestor-dir-ctime would have silenced forever."""
    import json as _json
    import os as _os
    src, dst = trees
    state = str(tmp_path / "first_seen.json")
    _write(src, "keep.txt", OLD)
    _write(dst, "keep.txt", OLD)
    # NOT launchd_logs/: the fixture exclude file drops that path entirely,
    # so a test using it would pass for the wrong reason. Any churning,
    # non-excluded directory makes the point.
    _write(src, "busy_dir/worker.txt", OLD)

    # Seen well over a cycle ago, so it is genuinely overdue.
    _json.dump({"sub": {"keep.txt": NOW - 10 * CYCLE,
                        "busy_dir/worker.txt": NOW - 10 * CYCLE}},
               open(state, "w"))
    # Churn the parent directory's ctime, the way an active log dir does.
    _os.utime(_os.path.join(src, "busy_dir"), (NOW, NOW))
    _write(src, "busy_dir/another.txt", OLD)

    r = cv.check_tree(src, "sub", now_ts=NOW, state_path=state)
    assert r["ok"] is False, "a long-unmirrored file must alarm even in a busy dir"
    assert "worker.txt" in r["reason"], r["reason"]


def test_first_run_seeds_silently_and_writes_state(trees, tmp_path):
    import json as _json
    src, dst = trees
    state = str(tmp_path / "first_seen.json")
    _write(src, "a.txt", OLD)
    _write(dst, "a.txt", OLD)
    for i in range(5):
        _write(src, f"unmirrored_{i}.txt", OLD)

    r = cv.check_tree(src, "sub", now_ts=NOW, state_path=state)
    assert r["ok"] is True, "seeding must not alarm once per file"
    saved = _json.load(open(state))["sub"]
    assert saved["unmirrored_0.txt"] == NOW
    assert len(saved) == 6


def test_state_drops_paths_that_left_the_source(trees, tmp_path):
    import json as _json
    src, dst = trees
    state = str(tmp_path / "first_seen.json")
    _json.dump({"sub": {"ghost.txt": NOW - CYCLE}}, open(state, "w"))
    _write(src, "a.txt", OLD)
    _write(dst, "a.txt", OLD)
    cv.check_tree(src, "sub", now_ts=NOW, state_path=state)
    assert "ghost.txt" not in _json.load(open(state))["sub"]


def test_a_stale_mirror_copy_is_still_judged_on_mtime(trees, tmp_path):
    """Arrival time governs ABSENCE only. When both sides exist, a copy that
    is behind the source is still a finding regardless of when it arrived."""
    import json as _json
    src, dst = trees
    state = str(tmp_path / "first_seen.json")
    _write(src, "edited.txt", OLD + 5000)
    _write(dst, "edited.txt", OLD)
    _json.dump({"sub": {"edited.txt": NOW}}, open(state, "w"))
    r = cv.check_tree(src, "sub", now_ts=NOW, state_path=state)
    assert r["ok"] is False and "older_1" in r["reason"], r


def test_result_shape_is_unchanged_for_the_walker_health_plumbing(trees):
    src, dst = trees
    _write(src, "a.txt", OLD)
    _write(dst, "a.txt", OLD)
    r = cv.check_tree(src, "sub", now_ts=NOW)
    assert set(r) == {"label", "ceiling_seconds", "age_seconds", "ok", "reason"}
    assert r["label"] == "mirror:sub"
    assert r["ceiling_seconds"] == CYCLE
