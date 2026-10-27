#!/usr/bin/env python3
"""Run the repo's CI chain locally, step by step, exactly as ci.yml defines it
-- the pre-push gate for every xrpldashboard change.

    scripts/run_ci_chain.py --ref <sha-or-branch> --first
    scripts/run_ci_chain.py --ref <sha-or-branch> --rest
    scripts/run_ci_chain.py --ref <sha-or-branch>            # both

WHY THIS CLONES INSTEAD OF USING A WORKTREE (2026-10-27 incident)
-----------------------------------------------------------------
The previous driver ran the chain inside `git worktree add`'s linked
worktree. A linked worktree does NOT have its own object store -- it
shares the parent's, which `git rev-parse --git-common-dir` resolves to
`~/xrpl_test/.git`. ci.yml contains this step:

    git fetch --no-tags --depth=1 origin +refs/heads/main:refs/remotes/origin/main

On a GitHub runner that is correct and intended (branch builds are
shallow). Run locally inside a linked worktree, `--depth=1` writes
`.git/shallow` into the REAL repository. That is exactly what happened:
`~/xrpl_test` silently became a 1-commit shallow clone, every subsequent
`git pull --ff-only` died with "Not possible to fast-forward", and the
truncated parent pointers made `origin/main` look like a rewritten
history. Recovery was `git fetch --unshallow`, but the failure mode is
expensive precisely because it looks like remote history loss.

An independent clone has its OWN `.git`, so a `--depth=1` fetch inside it
can only ever shallow the throwaway copy. `git clone` of a local path
hardlinks objects, so this costs almost nothing on disk and is not slower
in practice.

Hard rule enforced below: this runner must never create a linked
worktree, and it refuses to run anywhere that shares an object store with
the source repository. tests/test_ci_chain_runner_isolation.py locks both.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import yaml  # requirements-dev provides it

# The ci.yml step whose name starts with this is the dependency install; the
# caller's venv IS the install, so it is skipped when running locally.
_SKIP_RUN_PREFIX = "pip install"


def ci_steps(ci_path: str = ".github/workflows/ci.yml"):
    """Enumerate ci.yml's run: steps in file order as (name, env, run).

    Driven off the workflow file itself, never a hand-copied list -- a step
    omitted here is a hole in the gate that still reports green.
    """
    with open(ci_path, encoding="utf-8") as fh:
        ci = yaml.safe_load(fh)
    (job,) = ci["jobs"].values()
    out = []
    for s in job["steps"]:
        if "run" not in s:
            continue
        if s["run"].strip().startswith(_SKIP_RUN_PREFIX):
            continue
        out.append((s.get("name", s["run"][:60]), s.get("env", {}) or {}, s["run"]))
    return out


def clone_argv(repo_root: str, ref: str, dest: str) -> list[str]:
    """The isolation-safe checkout command.

    Deliberately `git clone`, never `git worktree add`: the clone gets its
    own object store, so a --depth fetch inside it cannot reach the source
    repo. `--no-hardlinks` is NOT used -- hardlinked objects are shared
    storage but not a shared *shallow* boundary, and they keep this cheap.
    """
    return ["git", "clone", "--quiet", "--no-checkout", repo_root, dest]


def git_common_dir(path: str) -> str:
    """Absolute path of the object store backing `path`."""
    out = subprocess.run(
        ["git", "-C", path, "rev-parse", "--git-common-dir"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return os.path.realpath(os.path.join(path, out) if not os.path.isabs(out) else out)


def assert_isolated(dest: str, repo_root: str) -> None:
    """Refuse to run anywhere that shares an object store with repo_root.

    This is the guard that makes the shallow-fetch step harmless. A linked
    worktree of repo_root resolves to the same common dir and is rejected.
    """
    if git_common_dir(dest) == git_common_dir(repo_root):
        raise SystemExit(
            f"REFUSED: {dest} shares an object store with {repo_root}.\n"
            "ci.yml performs a --depth=1 fetch; running it here would write "
            ".git/shallow into the real repository. Use an independent clone."
        )


def prepare_clone(repo_root: str, ref: str, dest: str) -> None:
    subprocess.run(clone_argv(repo_root, ref, dest), check=True)
    subprocess.run(["git", "-C", dest, "checkout", "--quiet", "--detach", ref], check=True)
    assert_isolated(dest, repo_root)


def run_chain(workdir: str, mode: str, python_bin: str) -> int:
    steps = ci_steps(os.path.join(workdir, ".github", "workflows", "ci.yml"))
    sel = steps[:1] if mode == "first" else steps[1:] if mode == "rest" else steps
    n = len(sel)
    for i, (name, env, run) in enumerate(sel, 1):
        e = dict(os.environ)
        e["PATH"] = f"{os.path.dirname(python_bin)}:{e.get('PATH', '')}"
        e.update({k: str(v) for k, v in env.items()})
        t = time.time()
        r = subprocess.run(run, shell=True, env=e, cwd=workdir,
                           capture_output=True, text=True)
        dt = time.time() - t
        tail = (r.stdout + r.stderr).strip().splitlines()
        summary = next(
            (l for l in reversed(tail)
             if "passed" in l or "failed" in l or "error" in l.lower()), "")
        print(f"[{i}/{n}] {'ok  ' if r.returncode == 0 else 'FAIL'} {dt:6.1f}s {name}",
              flush=True)
        print(f"    {summary[:110]}", flush=True)
        if r.returncode != 0:
            print("\n".join(tail[-25:]))
            print(f"FAILED: {name}")
            return 1
    print("FULL CHAIN GREEN")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--dest", required=True)
    ap.add_argument("--python", required=True, help="venv python inside dest")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--first", action="store_true")
    g.add_argument("--rest", action="store_true")
    a = ap.parse_args()
    mode = "first" if a.first else "rest" if a.rest else "all"
    assert_isolated(a.dest, a.repo)
    return run_chain(a.dest, mode, a.python)


if __name__ == "__main__":
    sys.exit(main())
