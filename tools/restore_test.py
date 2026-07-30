#!/usr/bin/env python3
"""Restore-test the latest Neon backup from B2.

Pulls the most recent neondb-*.dump from b2crypt:.../postgres/, spins
up an ephemeral local Postgres cluster on port 5439, restores into a
throwaway database, runs smoke queries, and tears everything down.

Cleanup runs in a finally block so we never leak a Postgres process
or temp directory, even on failure. The smoke queries are the
correctness layer that complements the daily freshness canary: the
canary catches "backups stopped running"; this catches "backups are
running but producing corrupt or empty dumps."

Invocation:
  Manual:   ./tools/restore_test.py
  Launchd:  com.charliebruce.xrpldashboard.pg_restore_test.plist (weekly)

Exit code 0 on all smoke queries passing, nonzero on any failure.
Stdout: human-readable summary. Stderr: errors only.

Closes STORAGE_INVENTORY_2026-05-25.md item 1 (Neon not pg_dumped)
with the correctness-verification layer.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

PG_BIN = "/opt/homebrew/opt/postgresql@17/bin"
RCLONE = "/opt/homebrew/bin/rclone"

# Walker health — optional (no-op if db.py not importable or PG not configured)
_REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, _REPO_ROOT)
try:
    import db as _db
    _HAS_DB = True
except ImportError:
    _HAS_DB = False
WALKER_NAME = "pg_restore_test"
WALKER_CADENCE_SECONDS = 604800  # weekly
B2_REMOTE = os.environ.get("PG_BACKUP_REMOTE", "b2crypt")
B2_BUCKET = os.environ.get(
    "PG_BACKUP_BUCKET", f"xrpldashboard-backup-{os.uname().nodename.split('.')[0]}"
)
B2_PREFIX = f"{B2_REMOTE}:{B2_BUCKET}/postgres"
PORT = 5439
DB_NAME = "neondb_restoretest"
PG_USER = "postgres"
FRESHNESS_WINDOW_DAYS = 7


def run(cmd, check=True, capture=False, env=None, input_=None):
    """Thin subprocess.run wrapper with sane defaults for this script."""
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        env=env,
        input=input_,
    )


def latest_dump_name() -> str:
    """Return the most recent neondb-*.dump filename in B2."""
    result = run(
        [RCLONE, "lsf", B2_PREFIX, "--include", "neondb-*.dump",
         "--files-only", "--format", "tp"],
        capture=True,
    )
    rows = [line for line in result.stdout.splitlines() if line.strip()]
    if not rows:
        raise RuntimeError(f"No neondb-*.dump files found in {B2_PREFIX}")
    rows.sort(reverse=True)  # ISO timestamps in name → lexicographic = chronological
    # Format "tp" prints "modtime;path" — split on ; and take path
    return rows[0].split(";", 1)[1]


def pull_dump(dump_name: str, dest: Path) -> Path:
    """Copy the dump from B2 to a local path. Returns the local path."""
    run([RCLONE, "copyto", f"{B2_PREFIX}/{dump_name}", str(dest)])
    return dest


def _pg_env() -> dict:
    """Env for pg_ctl/initdb subprocesses. macOS requires a valid LC_ALL or
    the postmaster fails with "became multithreaded during startup"."""
    env = os.environ.copy()
    env["LC_ALL"] = "en_US.UTF-8"
    return env


def init_cluster(data_dir: Path, log_path: Path) -> None:
    env = _pg_env()
    run([f"{PG_BIN}/initdb",
         "-D", str(data_dir),
         "--auth=trust",
         "--username", PG_USER,
         "--encoding=UTF-8",
         "--locale=C",
         "-N"],  # -N = skip fsync during initdb for speed
        capture=True, env=env)
    run([f"{PG_BIN}/pg_ctl",
         "-D", str(data_dir),
         "-l", str(log_path),
         "-o", f"-p {PORT} -k {data_dir.parent}",
         "-w",  # wait for ready
         "start"],
        capture=True, env=env)


def stop_cluster(data_dir: Path) -> None:
    if not (data_dir / "postmaster.pid").exists():
        return
    try:
        run([f"{PG_BIN}/pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"],
            capture=True, check=False, env=_pg_env())
    except Exception as e:
        print(f"WARN: pg_ctl stop raised {e}; attempting SIGTERM by PID", file=sys.stderr)
        pid_file = data_dir / "postmaster.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text().splitlines()[0])
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(2)
            except ProcessLookupError:
                pass


def psql_scalar(sql: str) -> str:
    """Run a single-value query against the restore-test DB. Returns stripped stdout."""
    result = run(
        [f"{PG_BIN}/psql",
         "-h", "127.0.0.1", "-p", str(PORT),
         "-U", PG_USER, "-d", DB_NAME,
         "-At", "-c", sql],
        capture=True,
    )
    return result.stdout.strip()


def restore_dump(dump_path: Path) -> None:
    run([f"{PG_BIN}/createdb",
         "-h", "127.0.0.1", "-p", str(PORT),
         "-U", PG_USER, DB_NAME],
        capture=True)
    run([f"{PG_BIN}/pg_restore",
         "-h", "127.0.0.1", "-p", str(PORT),
         "-U", PG_USER, "-d", DB_NAME,
         "--no-owner", "--no-acl",
         "--exit-on-error",
         str(dump_path)],
        capture=True)


def run_smoke_queries() -> list[tuple[str, str, bool, str]]:
    """Return [(name, value, ok, detail), ...]. ok=False fails the test."""
    now = int(time.time())
    freshness_cutoff_epoch = now - (FRESHNESS_WINDOW_DAYS * 86400)
    results = []

    tp_count = int(psql_scalar("SELECT COUNT(*) FROM token_prices"))
    results.append((
        "token_prices count", str(tp_count), tp_count > 0,
        "must be > 0 (current prod baseline: 490)",
    ))

    tp_max_ts = psql_scalar("SELECT COALESCE(MAX(snapshot_ts), 0) FROM token_prices")
    tp_max_int = int(tp_max_ts)
    tp_fresh = tp_max_int >= freshness_cutoff_epoch
    tp_age_h = (now - tp_max_int) / 3600 if tp_max_int else float("inf")
    results.append((
        "token_prices MAX(snapshot_ts)", str(tp_max_int), tp_fresh,
        f"age={tp_age_h:.1f}h (must be < {FRESHNESS_WINDOW_DAYS * 24}h)",
    ))

    events_count = int(psql_scalar("SELECT COUNT(*) FROM events"))
    results.append((
        "events count", str(events_count), events_count > 0,
        "must be > 0",
    ))

    unl_count = int(psql_scalar("SELECT COUNT(*) FROM unl_snapshots"))
    results.append((
        "unl_snapshots count", str(unl_count), unl_count >= 2,
        "must be >= 2 (current prod baseline: 4)",
    ))

    unl_max_date = psql_scalar("SELECT COALESCE(MAX(snapshot_date)::text, '')"
                               " FROM unl_snapshots")
    if unl_max_date:
        # snapshot_date is a date — compute age in days via PG itself for accuracy
        age_days = int(psql_scalar(
            "SELECT EXTRACT(DAY FROM (NOW() - MAX(snapshot_date)::timestamp))::int "
            "FROM unl_snapshots"
        ))
        unl_fresh = age_days <= FRESHNESS_WINDOW_DAYS
        results.append((
            "unl_snapshots MAX(snapshot_date)", unl_max_date, unl_fresh,
            f"age={age_days}d (must be <= {FRESHNESS_WINDOW_DAYS}d)",
        ))
    else:
        results.append((
            "unl_snapshots MAX(snapshot_date)", "(none)", False,
            "no rows — dump may be incomplete",
        ))

    return results


@contextmanager
def ephemeral_workspace():
    tmp = Path(tempfile.mkdtemp(prefix="pg_restore_test."))
    try:
        yield tmp
    finally:
        # Cleanup is best-effort but always attempted.
        data_dir = tmp / "data"
        if data_dir.exists():
            stop_cluster(data_dir)
        shutil.rmtree(tmp, ignore_errors=True)


def _run() -> int:
    t0 = time.time()
    print(f"[restore_test] starting at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(t0))}")
    print(f"[restore_test] B2 prefix: {B2_PREFIX}")

    try:
        dump_name = latest_dump_name()
    except Exception as e:
        print(f"FAIL: could not list dumps: {e}", file=sys.stderr)
        return 1
    print(f"[restore_test] latest dump: {dump_name}")

    with ephemeral_workspace() as tmp:
        data_dir = tmp / "data"
        log_path = tmp / "pg.log"
        dump_path = tmp / dump_name

        try:
            print(f"[restore_test] pulling dump → {dump_path}")
            pull_dump(dump_name, dump_path)
            dump_size_mb = dump_path.stat().st_size / 1024 / 1024
            print(f"[restore_test] dump size: {dump_size_mb:.2f} MB")

            print(f"[restore_test] initdb + start cluster on port {PORT}")
            init_cluster(data_dir, log_path)

            print(f"[restore_test] createdb + pg_restore")
            restore_dump(dump_path)

            print(f"[restore_test] running smoke queries")
            results = run_smoke_queries()
        except subprocess.CalledProcessError as e:
            print(f"FAIL: subprocess {e.cmd!r} exited {e.returncode}", file=sys.stderr)
            if e.stderr:
                print(f"--- stderr ---\n{e.stderr}", file=sys.stderr)
            return 2

    duration = time.time() - t0
    print("")
    print(f"{'check':40s}  {'value':24s}  {'ok':3s}  detail")
    print("-" * 100)
    all_ok = True
    for name, value, ok, detail in results:
        flag = "OK " if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"{name:40s}  {value:24s}  {flag}  {detail}")
    print("")
    print(f"[restore_test] duration: {duration:.1f}s")
    print(f"[restore_test] result: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 3


def _ping_betterstack_success() -> None:
    """POST to the BetterStack heartbeat URL — success path ONLY. Called
    only after a full PASS. A missed run = no ping = BetterStack incident
    within the heartbeat grace window (1 day for the weekly restore test).
    Silent skip when the env var is unset, so the script keeps working
    without the monitor. Network failure logs but does not fail the
    restore test — the walker_health row is still authoritative."""
    import urllib.request
    url = os.environ.get("BETTERSTACK_PG_RESTORE_TEST_URL", "").strip()
    if not url:
        return
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[restore_test] betterstack ping ok ({resp.status})")
    except Exception as e:
        print(f"[restore_test] betterstack ping failed: {e} (restore_test still PASS)")


def main() -> int:
    if _HAS_DB:
        _db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    rc = _run()
    if _HAS_DB:
        _db.write_walker_health_end(
            WALKER_NAME, ok=(rc == 0),
            message="PASS" if rc == 0 else f"FAIL rc={rc}",
        )
    if rc == 0:
        _ping_betterstack_success()
    return rc


if __name__ == "__main__":
    sys.exit(main())
