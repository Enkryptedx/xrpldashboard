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

# Give this process a CA bundle before any TLS call. Import for the side
# effect; see cert_env.py for the 30-day failure tally this fixes.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import cert_env  # noqa: F401
except ImportError:
    pass

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


def _load_xrpldashboard_env() -> None:
    """Source ~/.config/xrpldashboard/env into os.environ so BACKUP_BUCKET_PREFIX
    (and any other pins) are visible when this script is invoked directly by
    launchd — the plist does not use a shell wrapper. Silent no-op if the file
    is missing or unreadable; explicit os.environ entries always win."""
    env_file = os.environ.get(
        "XRPLDASHBOARD_ENV",
        os.path.expanduser("~/.config/xrpldashboard/env"),
    )
    try:
        with open(env_file, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


_load_xrpldashboard_env()

# Env-var names mirror the writer (launchd/run_pg_backup.sh): both sides read
# BACKUP_REMOTE and BACKUP_BUCKET_PREFIX so a single env-file pin binds both.
# 2026-08-22 drift repair: reader previously read PG_BACKUP_REMOTE/PG_BACKUP_BUCKET
# — different namespace, so the writer's pin never reached the reader, and the
# reader fell back to hostname derivation which is unstable under launchd.
_HOSTNAME_SHORT = os.uname().nodename.split(".")[0]
B2_REMOTE = os.environ.get("BACKUP_REMOTE", "b2crypt")
B2_BUCKET = os.environ.get(
    "BACKUP_BUCKET_PREFIX", f"xrpldashboard-backup-{_HOSTNAME_SHORT}"
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
    """Restore the dump, stripping Neon-only `pg_attver` attoptions.

    Why this is not a plain pg_restore any more (2026-11-17): Neon stamps
    `pg_attver` into attoptions for columns added through its metadata-only
    ADD COLUMN fast path, pg_dump emits `ALTER ... SET (pg_attver=...)`, and
    ANY vanilla Postgres rejects it with `unrecognized parameter "pg_attver"`.
    So we pipe pg_restore's SQL through the filter and apply it with psql
    under ON_ERROR_STOP=1, which keeps the old `--exit-on-error` guarantee:
    every OTHER error still aborts the restore.
    """
    from neon_dump_filter import filter_stream

    run([f"{PG_BIN}/createdb",
         "-h", "127.0.0.1", "-p", str(PORT),
         "-U", PG_USER, DB_NAME],
        capture=True)

    pr = subprocess.Popen(
        [f"{PG_BIN}/pg_restore", "--no-owner", "--no-acl", "-f", "-",
         str(dump_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ps = subprocess.Popen(
        [f"{PG_BIN}/psql", "-h", "127.0.0.1", "-p", str(PORT),
         "-U", PG_USER, "-d", DB_NAME,
         "-v", "ON_ERROR_STOP=1", "-q", "-f", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)

    dropped: list[str] = []
    try:
        for line in filter_stream(pr.stdout, dropped):
            ps.stdin.write(line)
    finally:
        ps.stdin.close()

    pr_err = pr.stderr.read()
    pr.wait()
    ps_out, ps_err = ps.communicate()

    print(f"[restore_test] pg_attver statements filtered: {len(dropped)}")
    for d in dropped:
        print(f"[restore_test]   dropped: {d[:140]}")

    if pr.returncode != 0:
        raise subprocess.CalledProcessError(
            pr.returncode, "pg_restore -f -", output="", stderr=pr_err)
    if ps.returncode != 0:
        # ON_ERROR_STOP=1 -- anything other than pg_attver still fails here.
        raise subprocess.CalledProcessError(
            ps.returncode, "psql (filtered restore)",
            output=ps_out, stderr=ps_err)


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
    _betterstack("", "ping ok", still="restore_test still PASS")


def _betterstack(suffix: str, label: str, still: str) -> None:
    """Hit the BetterStack heartbeat URL with curl.

    curl, not urllib: urllib under this venv had no CA bundle, so every ping
    since at least October died with CERTIFICATE_VERIFY_FAILED and was
    swallowed -- the dead-man's switch signalled nothing in either direction
    (2026-11-18). cert_env sets SSL_CERT_FILE for in-process TLS, and curl
    uses the system trust store, so this path no longer depends on either
    being right.
    """
    url = os.environ.get("BETTERSTACK_PG_RESTORE_TEST_URL", "").strip()
    if not url:
        return
    try:
        run(["curl", "-fsS", "-m", "10", "-X", "POST", url + suffix],
            capture=True)
        print(f"[restore_test] betterstack {label}")
    except Exception as e:
        print(f"[restore_test] betterstack {label} failed: {e} ({still})")


def _page_betterstack_failure(rc: int) -> None:
    """ACTIVE page on FAIL (Charlie, 2026-11-18).

    Previously a failure only withheld the heartbeat, so it waited out a
    1-day grace window and surfaced in an evening summary. POSTing to the
    heartbeat's /fail endpoint opens the incident immediately.
    """
    _betterstack("/fail", f"FAIL page sent (rc={rc})",
                 still="restore_test still FAIL")


def main() -> int:
    if _HAS_DB:
        _db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    rc = _run()
    if _HAS_DB:
        _db.write_walker_health_end(
            WALKER_NAME, ok=(rc == 0),
            message="PASS" if rc == 0 else f"FAIL rc={rc}",
        )
    if rc != 0:
        _page_betterstack_failure(rc)
    if rc == 0:
        _ping_betterstack_success()
        # 2026-09-09: stamp for dockvault_mirror_freshness_canary meta-watch.
        # Only on genuine PASS — teardown/restore failures leave the stamp
        # stale so the canary pages. Weekly cadence → 193h ceiling.
        try:
            _state_dir = "/Users/charliebruce/xrpl_test/launchd_state"
            os.makedirs(_state_dir, exist_ok=True)
            with open(os.path.join(_state_dir, "pg_restore_test_last_ok"), "w") as _f:
                _f.write(str(int(time.time())))
        except OSError as _e:
            print(f"[pg_restore_test] warn: could not write _last_ok: {_e}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
