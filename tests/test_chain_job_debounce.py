"""Chain-job debounce: a chain job never re-signs a date.

Filed 2026-09-19 after the anchor-#7 pull-plug test. When the Mac
auto-rebooted, signed_snapshot's RunAtLoad=true fired the walker; it
recomputed today's leaf against fresh post-boot walker inputs and wrote
a new chain_root (4d8c2c9a) that orphaned the morning's b202095a leaf
already sealed on-ledger via anchor #7. An hour of manual disk + PG +
chain.json restoration followed.

The debounce: signed_snapshot.py and signed_registry_snapshot.py refuse
to write a leaf for a date that already has one, unless --force. The
skip path exits 0 without touching walker_health, so real silence
(walker never fires) still pages via the freshness canary; benign
RunAtLoad double-fire is a silent no-op.

This test locks the behavior in as a regression guard.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = REPO_ROOT / "venv" / "bin" / "python"


def _run(walker: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke a walker script via the project venv. Sources
    ~/.config/xrpldashboard/env so DATABASE_URL and SIGNING_KEY_PASSPHRASE
    are available. Returns (returncode, stdout, stderr) on the completed
    process for the caller to assert against."""
    cmd = (
        f"set -a; . \"$HOME/.config/xrpldashboard/env\"; set +a; "
        f"{PY} {REPO_ROOT / walker} " + " ".join(args)
    )
    return subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True, env=env, timeout=60,
    )


def _mtime(p: Path) -> float:
    return p.stat().st_mtime


class TestSignedSnapshotDebounce:
    """A chain job never re-signs a date. Second run on the same date
    must be a no-op — skip message on stdout, exit 0, disk untouched."""

    LEAF_DATE = "2026-09-19"

    @pytest.fixture
    def leaf_path(self):
        p = REPO_ROOT / "signed_snapshots" / f"{self.LEAF_DATE}.json"
        if not p.exists():
            pytest.skip(f"{p} not present in this checkout; debounce test needs a real leaf")
        return p

    def test_second_run_skips_and_exits_0(self, leaf_path):
        before_mtime = _mtime(leaf_path)
        r = _run("signed_snapshot.py", "--date", self.LEAF_DATE)
        assert r.returncode == 0, (
            f"signed_snapshot.py exited {r.returncode} on debounce path; "
            f"stderr:\n{r.stderr}"
        )
        assert "leaf exists, skipping" in r.stdout, (
            f"expected 'leaf exists, skipping' on stdout; got:\n{r.stdout}"
        )
        # Disk file is NOT touched by the skip path.
        assert _mtime(leaf_path) == before_mtime, (
            f"disk file was written by the skip path — mtime changed "
            f"({before_mtime} → {_mtime(leaf_path)})"
        )

    def test_force_flag_documented(self):
        r = _run("signed_snapshot.py", "--help")
        assert r.returncode == 0
        assert "--force" in r.stdout, (
            f"--force not exposed in --help; debounce escape hatch missing:\n"
            f"{r.stdout}"
        )


class TestSignedRegistrySnapshotDebounce:
    """Same rule for the registry walker. Second run on the same date
    must skip — disk untouched, exit 0."""

    @pytest.fixture
    def snapshot_path(self):
        # Registry walker auto-picks today's UTC date; test today's file.
        import datetime as dt
        date_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        p = REPO_ROOT / "signed_registry_snapshots" / f"{date_str}.json"
        if not p.exists():
            pytest.skip(
                f"{p} not present; registry debounce test needs today's snapshot"
            )
        return p

    def test_second_run_skips_and_exits_0(self, snapshot_path):
        before_mtime = _mtime(snapshot_path)
        r = _run("signed_registry_snapshot.py")
        assert r.returncode == 0, (
            f"signed_registry_snapshot.py exited {r.returncode} on debounce path; "
            f"stderr:\n{r.stderr}"
        )
        assert "snapshot exists, skipping" in r.stdout, (
            f"expected 'snapshot exists, skipping' on stdout; got:\n{r.stdout}"
        )
        assert _mtime(snapshot_path) == before_mtime, (
            f"disk file was written by the skip path — mtime changed"
        )

    def test_force_flag_documented(self):
        r = _run("signed_registry_snapshot.py", "--help")
        assert r.returncode == 0
        assert "--force" in r.stdout, (
            f"--force not exposed in --help; debounce escape hatch missing:\n"
            f"{r.stdout}"
        )
