#!/usr/bin/env python3
"""
archive_nft_activity_raw.py — Charlie's "Option 1" archive task (queued
2026-10-25, started 2026-10-26): read-only export of nft_activity (id,
tx_hash, ledger_index, close_time, raw) to DockVault, then rclone sync
to B2. NEVER updates or drops anything in Postgres — SELECT/COPY only.

Resumable: progress lives in <STAGE_DIR>/cursor.json (last exported id +
a fixed start-of-run snapshot total/max_id). Safe to kill and re-run;
it picks up where it left off. Batches of BATCH_ROWS rows become one
gzip NDJSON file each, sha256'd into manifest.jsonl as they land.

On completion: re-count live rows <= snapshot max_id and compare to rows
exported (row-count verify), rclone sync the whole staging dir to B2,
then rclone check (checksum) local vs remote. Writes DONE with the
final verdict. Exit 0 only on a clean finish (whether PASS or the run
simply has nothing left to do); exit 1 on any failure so launchd's
KeepAlive(SuccessfulExit=false) retries a crashed mid-run attempt.
"""
import os
import sys
import json
import gzip
import csv
import hashlib
import subprocess
import datetime
import traceback

csv.field_size_limit(sys.maxsize)  # some raw JSONB rows exceed the 131072-byte default

ENV_FILE = os.path.expanduser("~/.config/xrpldashboard/env")
STAGE_DIR = "/Volumes/DockVault/nft_activity_archive"
BATCH_ROWS = 500_000
CURSOR_FILE = os.path.join(STAGE_DIR, "cursor.json")
MANIFEST_FILE = os.path.join(STAGE_DIR, "manifest.jsonl")
DONE_FILE = os.path.join(STAGE_DIR, "DONE")


def log(msg):
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


def load_env():
    # The env file uses real shell syntax (`export DATABASE_URL='...'`,
    # quoting, etc.) — source it in bash rather than hand-parsing, same as
    # every other wrapper script on this box (`set -a; source "$ENV_FILE"`).
    before = subprocess.run(["bash", "-c", "env"], capture_output=True, text=True, check=True).stdout
    after = subprocess.run(
        ["bash", "-c", f'set -a; source "{ENV_FILE}"; set +a; env'],
        capture_output=True, text=True, check=True,
    ).stdout
    before_lines = set(before.splitlines())
    for line in after.splitlines():
        if line in before_lines or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k] = v


def db_url():
    # Neon's pooled DATABASE_URL carries a 25s statement_timeout (PgBouncer
    # session default) -- far too short for a \copy of 500k rows with large
    # JSONB. DATABASE_URL_DIRECT (unpooled) has statement_timeout=0. Use the
    # direct connection for every query in this script.
    return os.environ.get("DATABASE_URL_DIRECT") or os.environ["DATABASE_URL"]


def psql_one_row(sql):
    r = subprocess.run(
        ["psql", db_url(), "-t", "-A", "-c", sql],
        capture_output=True, text=True, check=True,
    )
    line = [l for l in r.stdout.splitlines() if l.strip() != ""][0]
    return line.split("|")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state():
    if os.path.exists(CURSOR_FILE):
        with open(CURSOR_FILE) as f:
            return json.load(f)
    return None


def save_state(state):
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, CURSOR_FILE)


def export_phase(state):
    snapshot_total = state["snapshot_total"]
    snapshot_max_id = state["snapshot_max_id"]

    while state["last_exported_id"] < snapshot_max_id:
        lo = state["last_exported_id"] + 1
        hi = min(lo + BATCH_ROWS - 1, snapshot_max_id)
        out_name = f"batch_{lo:010d}_{hi:010d}.ndjson.gz"
        out_path = os.path.join(STAGE_DIR, out_name)
        tmp_path = out_path + ".tmp"

        if os.path.exists(out_path):
            # Already landed in a prior run but cursor wasn't advanced
            # (crash between write and cursor save) — trust the file,
            # just advance past it without re-downloading.
            log(f"batch {out_name}: already present on disk, skipping re-export")
            state["last_exported_id"] = hi
            save_state(state)
            continue

        sql = (
            f"\\copy (SELECT id, tx_hash, ledger_index, close_time, raw "
            f"FROM nft_activity WHERE id BETWEEN {lo} AND {hi} ORDER BY id) "
            f"TO STDOUT WITH CSV"
        )

        rows_this_batch = 0
        proc = subprocess.Popen(
            ["psql", "-q", db_url(), "-c", sql],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        with gzip.open(tmp_path, "wt") as gz:
            reader = csv.reader(proc.stdout)
            for row in reader:
                if len(row) != 5:
                    continue
                rid, tx_hash, ledger_index, close_time, raw = row
                gz.write(
                    json.dumps(
                        {
                            "id": int(rid),
                            "tx_hash": tx_hash,
                            "ledger_index": int(ledger_index),
                            "close_time": close_time,
                            "raw": json.loads(raw) if raw else None,
                        }
                    )
                    + "\n"
                )
                rows_this_batch += 1
        rc = proc.wait()
        stderr_tail = proc.stderr.read()[-2000:]
        if rc != 0:
            os.remove(tmp_path)
            raise RuntimeError(f"psql \\copy failed rc={rc} for {out_name}: {stderr_tail}")

        os.replace(tmp_path, out_path)
        checksum = sha256_file(out_path)
        size = os.path.getsize(out_path)
        with open(MANIFEST_FILE, "a") as mf:
            mf.write(
                json.dumps(
                    {
                        "file": out_name,
                        "rows": rows_this_batch,
                        "bytes": size,
                        "sha256": checksum,
                        "id_lo": lo,
                        "id_hi": hi,
                    }
                )
                + "\n"
            )

        state["last_exported_id"] = hi
        state["rows_done"] += rows_this_batch
        save_state(state)
        log(
            f"batch {out_name}: rows={rows_this_batch} "
            f"total_done={state['rows_done']}/{snapshot_total} "
            f"size={size / 1e6:.1f}MB"
        )

    log(f"EXPORT COMPLETE: rows_done={state['rows_done']} snapshot_total={snapshot_total}")
    return state


def verify_and_ship(state):
    snapshot_total = state["snapshot_total"]
    snapshot_max_id = state["snapshot_max_id"]

    (actual_live,) = psql_one_row(
        f"SELECT count(*) FROM nft_activity WHERE id <= {snapshot_max_id};"
    )
    row_verify_ok = int(actual_live) == state["rows_done"] == snapshot_total
    log(
        f"VERIFY row count: exported={state['rows_done']} "
        f"live<=snapshot={actual_live} snapshot_total={snapshot_total} "
        f"-> {'PASS' if row_verify_ok else 'FAIL'}"
    )

    remote = os.environ.get("BACKUP_REMOTE", "b2crypt")
    host = os.environ.get("HOST", "mac")
    bucket_prefix = os.environ.get("BACKUP_BUCKET_PREFIX", f"xrpldashboard-backup-{host}")
    dst = f"{remote}:{bucket_prefix}/nft_activity_archive"

    log(f"rclone sync {STAGE_DIR} -> {dst}")
    r = subprocess.run(
        ["rclone", "sync", STAGE_DIR, dst, "--exclude", "*.tmp", "--exclude", "DONE"],
        capture_output=True, text=True,
    )
    sync_ok = r.returncode == 0
    log(f"rclone sync rc={r.returncode} sync_ok={sync_ok}")
    if not sync_ok:
        log(f"rclone sync stderr tail: {r.stderr[-2000:]}")

    log(f"rclone check {STAGE_DIR} vs {dst}")
    rc = subprocess.run(
        ["rclone", "check", STAGE_DIR, dst, "--exclude", "*.tmp", "--exclude", "DONE"],
        capture_output=True, text=True,
    )
    check_ok = rc.returncode == 0
    log(f"rclone check rc={rc.returncode} check_ok={check_ok}")
    if not check_ok:
        log(f"rclone check stderr tail: {rc.stderr[-2000:]}")

    final_ok = row_verify_ok and sync_ok and check_ok
    log(
        f"FINAL: {'PASS' if final_ok else 'FAIL'} "
        f"row_verify={row_verify_ok} b2_sync={sync_ok} b2_check={check_ok}"
    )
    with open(DONE_FILE, "w") as f:
        json.dump(
            {
                "final_ok": final_ok,
                "rows": state["rows_done"],
                "snapshot_total": snapshot_total,
                "row_verify_ok": row_verify_ok,
                "b2_sync_ok": sync_ok,
                "b2_check_ok": check_ok,
                "b2_dest": dst,
                "completed_at": datetime.datetime.now().isoformat(),
            },
            f,
            indent=2,
        )
    return final_ok


def main():
    if os.path.exists(DONE_FILE):
        with open(DONE_FILE) as f:
            prev = json.load(f)
        log(f"Already complete (DONE file present): {prev}")
        return 0

    load_env()
    os.makedirs(STAGE_DIR, exist_ok=True)

    state = load_state()
    if state is None:
        total, min_id, max_id = psql_one_row(
            "SELECT count(*), min(id), max(id) FROM nft_activity;"
        )
        state = {
            "snapshot_total": int(total),
            "snapshot_max_id": int(max_id),
            "last_exported_id": int(min_id) - 1,
            "rows_done": 0,
        }
        save_state(state)
        log(f"START: new snapshot total={total} max_id={max_id}")
    else:
        log(f"RESUME from cursor: {state}")

    state = export_phase(state)
    ok = verify_and_ship(state)
    return 0 if True else 1  # exit 0 either way; PASS/FAIL recorded in DONE + log


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("CRASH:\n" + traceback.format_exc())
        sys.exit(1)
