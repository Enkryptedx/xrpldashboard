"""The local CI chain runner must never share an object store with the repo.

2026-10-27 incident: the old driver ran the chain inside `git worktree add`'s
linked worktree. A linked worktree shares the parent's object store, and
ci.yml contains

    git fetch --no-tags --depth=1 origin +refs/heads/main:refs/remotes/origin/main

so that step wrote `.git/shallow` into ~/xrpl_test itself. The repo silently
became a 1-commit shallow clone, `git pull --ff-only` started failing with
"Not possible to fast-forward", and truncated parent pointers made
`origin/main` look like a rewritten history.

These tests lock the two properties that make that impossible:
  1. the runner clones (own object store), never worktrees;
  2. it refuses outright to run anywhere sharing the repo's object store.

Hermetic: pure text/AST checks plus throwaway local git repos. No DB, no
network, no touching the real repository.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER_PATH = os.path.join(REPO_ROOT, "scripts", "run_ci_chain.py")
CI_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")


def _load():
    spec = importlib.util.spec_from_file_location("run_ci_chain_under_test",
                                                  RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = _load()


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


@pytest.fixture
def source_repo(tmp_path):
    """A throwaway git repo standing in for ~/xrpl_test."""
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "--quiet", "-b", "main", cwd=src)
    _git("config", "user.email", "t@example.invalid", cwd=src)
    _git("config", "user.name", "t", cwd=src)
    (src / "f.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "f.txt", cwd=src)
    _git("commit", "--quiet", "-m", "init", cwd=src)
    return src


# ------------------------------------------------------- the hazard is real

def test_ci_yml_still_contains_a_shallow_fetch():
    """If this ever stops being true the guard is still correct, but the
    rationale in the runner's header should be revisited rather than quietly
    rotting."""
    ci = open(CI_PATH, encoding="utf-8").read()
    assert "--depth=1" in ci, (
        "ci.yml no longer shallow-fetches; re-read run_ci_chain.py's header"
    )


# ------------------------------------------------- clone, never worktree

def test_runner_never_creates_a_linked_worktree():
    """Check EXECUTED commands, not prose.

    A plain substring scan is wrong here: the runner's header deliberately
    explains the `git worktree add` hazard, so a text search flags the
    documentation that exists to prevent the bug. Walk the AST and look
    only at string constants that are arguments to a call.
    """
    tree = ast.parse(open(RUNNER_PATH, encoding="utf-8").read())

    def literals_in(node):
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                yield child.value

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in node.args:
            for lit in literals_in(arg):
                if "worktree" in lit:
                    offenders.append(lit)
    assert not offenders, (
        f"runner passes {offenders} to a call -- a linked worktree shares .git"
    )


def test_the_hazard_is_still_documented_in_the_runner():
    """The prose the AST test deliberately ignores must actually be there."""
    src = open(RUNNER_PATH, encoding="utf-8").read()
    assert "worktree" in src, "the why must survive next to the guard"
    assert ".git/shallow" in src


def test_clone_argv_uses_git_clone():
    argv = runner.clone_argv("/src", "abc123", "/dest")
    assert argv[:2] == ["git", "clone"]
    assert "worktree" not in argv
    assert "/src" in argv and "/dest" in argv


# ------------------------------------------------- the refusal guard

def test_assert_isolated_rejects_a_linked_worktree(source_repo, tmp_path):
    """The exact 2026-10-27 configuration must be refused."""
    wt = tmp_path / "linked_wt"
    _git("worktree", "add", "--quiet", "--detach", str(wt), "HEAD",
         cwd=source_repo)
    with pytest.raises(SystemExit) as e:
        runner.assert_isolated(str(wt), str(source_repo))
    assert "shares an object store" in str(e.value)


def test_assert_isolated_accepts_an_independent_clone(source_repo, tmp_path):
    dest = tmp_path / "clone"
    _git("clone", "--quiet", str(source_repo), str(dest))
    # Must not raise.
    runner.assert_isolated(str(dest), str(source_repo))


def test_clone_really_has_its_own_object_store(source_repo, tmp_path):
    """A --depth fetch in the clone must not be able to reach the source."""
    dest = tmp_path / "clone2"
    _git("clone", "--quiet", str(source_repo), str(dest))
    assert runner.git_common_dir(str(dest)) != runner.git_common_dir(str(source_repo))
    # And the linked-worktree case genuinely does collide, proving the
    # distinction this module relies on is real and not assumed.
    wt = tmp_path / "wt2"
    _git("worktree", "add", "--quiet", "--detach", str(wt), "HEAD",
         cwd=source_repo)
    assert runner.git_common_dir(str(wt)) == runner.git_common_dir(str(source_repo))


def test_shallow_fetch_in_clone_does_not_shallow_the_source(source_repo, tmp_path):
    """End-to-end proof of the actual failure mode, locally reproduced."""
    dest = tmp_path / "clone3"
    _git("clone", "--quiet", str(source_repo), str(dest))
    subprocess.run(
        ["git", "fetch", "--no-tags", "--depth=1", "origin",
         "+refs/heads/main:refs/remotes/origin/main"],
        cwd=dest, check=True, capture_output=True, text=True,
    )
    assert (dest / ".git" / "shallow").exists(), "clone should be the one shallowed"
    assert not (source_repo / ".git" / "shallow").exists(), (
        "the SOURCE repo must never be shallowed -- this is the whole bug"
    )


# ------------------------------------------------- step enumeration

def test_ci_steps_are_driven_from_the_workflow_file():
    steps = runner.ci_steps(CI_PATH)
    assert len(steps) > 50, "chain should enumerate every run: step in ci.yml"
    names = [n for n, _e, _r in steps]
    assert any("Trust-critical" in n for n in names)


def test_ci_steps_skips_the_dependency_install():
    for _name, _env, run in runner.ci_steps(CI_PATH):
        assert not run.strip().startswith("pip install"), (
            "the caller's venv is the install; that step must be skipped"
        )
