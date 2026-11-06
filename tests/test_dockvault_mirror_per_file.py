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


@pytest.fixture()
def trees(tmp_path, monkeypatch):
    src = tmp_path / "src"
    mirror_root = tmp_path / "mirror"
    (src).mkdir()
    (mirror_root / "sub").mkdir(parents=True)
    monkeypatch.setattr(cv, "MIRROR_ROOT", str(mirror_root))
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


def test_missing_file_past_the_cycle_is_flagged(trees):
    src, dst = trees
    _write(src, "present.txt", OLD)
    _write(dst, "present.txt", OLD)
    _write(src, "lost.txt", OLD)             # never arrived
    r = cv.check_tree(src, "sub", now_ts=NOW)
    assert r["ok"] is False, r
    assert "missing_1" in r["reason"]
    assert "lost.txt" in r["reason"], "name the file, not just a count"


def test_mirror_copy_older_than_source_is_flagged(trees):
    src, dst = trees
    _write(src, "edited.txt", OLD + 5000)
    _write(dst, "edited.txt", OLD)           # stale copy
    r = cv.check_tree(src, "sub", now_ts=NOW)
    assert r["ok"] is False, r
    assert "older_1" in r["reason"]
    assert r["age_seconds"] == 5000


def test_coverage_hole_is_not_hidden_by_one_fresh_copy(trees):
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
    r = cv.check_tree(src, "sub", now_ts=NOW)
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


def test_result_shape_is_unchanged_for_the_walker_health_plumbing(trees):
    src, dst = trees
    _write(src, "a.txt", OLD)
    _write(dst, "a.txt", OLD)
    r = cv.check_tree(src, "sub", now_ts=NOW)
    assert set(r) == {"label", "ceiling_seconds", "age_seconds", "ok", "reason"}
    assert r["label"] == "mirror:sub"
    assert r["ceiling_seconds"] == CYCLE
