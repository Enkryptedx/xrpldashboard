#!/usr/bin/env bash
#
# Nightly Postgres dump → encrypted B2.
#
# Streams pg_dump custom-format output directly through rclone rcat to
# the b2crypt remote — no plaintext ever touches local disk. Closes
# the #1 storage gap from STORAGE_INVENTORY_2026-05-25.md (Neon is
# not pg_dumped, free tier offers 1 day PITR).
#
# Retention: 14 nightly + 3 monthly. After a successful upload, keeps
# the 14 most-recent dumps unconditionally, plus the newest dump per
# calendar month for up to 3 older months. Everything else is deleted.
# Without pruning, uncapped storage accumulates ~3 GB/night indefinitely.
#
# DATABASE_URL is sourced from ~/.config/xrpldashboard/env (the same
# env file every xrpldashboard worker uses, kept outside the repo).
#
# Schedule: 03:30 local via com.charliebruce.xrpldashboard.pg_backup.
# 30 min after b2_backup at 03:00 — avoids bandwidth contention.

set -euo pipefail
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
LOG_FILE="${LOG_DIR}/pg_backup.$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

ENV_FILE="${XRPLDASHBOARD_ENV:-/Users/charliebruce/.config/xrpldashboard/env}"
REMOTE="${BACKUP_REMOTE:-b2crypt}"
HOST="$(hostname -s)"
BUCKET_PREFIX="${BACKUP_BUCKET_PREFIX:-xrpldashboard-backup-${HOST}}"
DEST_PREFIX="${REMOTE}:${BUCKET_PREFIX}/postgres"
PG_BACKUP_NIGHTLY="${PG_BACKUP_NIGHTLY:-14}"
PG_BACKUP_MONTHLY="${PG_BACKUP_MONTHLY:-3}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP_NAME="neondb-${TS}.dump"
DEST="${DEST_PREFIX}/${DUMP_NAME}"

log "pg_backup start (remote=${REMOTE}, bucket=${BUCKET_PREFIX}, keep=${PG_BACKUP_NIGHTLY}n+${PG_BACKUP_MONTHLY}m)"

if ! command -v pg_dump >/dev/null 2>&1; then
  log "FAIL: pg_dump not on PATH. Run 'brew install postgresql@17'."
  exit 1
fi

if ! command -v rclone >/dev/null 2>&1; then
  log "FAIL: rclone not on PATH."
  exit 1
fi

if [[ ! -r "$ENV_FILE" ]]; then
  log "FAIL: env file not readable at ${ENV_FILE}"
  exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

if [[ -z "${DATABASE_URL:-}" ]]; then
  log "FAIL: DATABASE_URL not set after sourcing ${ENV_FILE}"
  exit 1
fi

if ! rclone listremotes 2>/dev/null | grep -q "^${REMOTE}:$"; then
  log "FAIL: rclone remote '${REMOTE}:' not configured."
  exit 1
fi

log "  dumping → ${DEST}"
START_EPOCH=$(date +%s)

# Stream: pg_dump → rclone rcat. set -o pipefail makes the pipeline
# fail-fast if either side errors. --no-owner / --no-acl produce
# portable dumps that restore cleanly into a different cluster
# (Neon-specific role IDs would otherwise break local restore).
if pg_dump -Fc --no-owner --no-acl "$DATABASE_URL" \
    | rclone rcat "$DEST" --log-level INFO --log-file "$LOG_FILE"; then
  END_EPOCH=$(date +%s)
  DURATION=$((END_EPOCH - START_EPOCH))
  SIZE_BYTES="$(rclone size "$DEST" --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["bytes"])' \
    || echo "unknown")"
  log "  ok  size=${SIZE_BYTES} bytes  duration=${DURATION}s"
else
  rc=$?
  log "FAIL: dump+upload pipeline exited rc=${rc}"
  # Best-effort: try to clean a partial upload if one exists.
  rclone delete "$DEST" 2>/dev/null || true
  exit "$rc"
fi

log "  pruning: keep ${PG_BACKUP_NIGHTLY} nightly + ${PG_BACKUP_MONTHLY} monthly"
if python3 - "$DEST_PREFIX" "$PG_BACKUP_NIGHTLY" "$PG_BACKUP_MONTHLY" \
       "$LOG_FILE" <<'PY'
import subprocess, re, sys, os
from datetime import datetime, timezone

dest_prefix, nightly, monthly_keep, log_file = \
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(log_file, "a") as f:
        f.write(line + "\n")

result = subprocess.run(
    ["rclone", "lsf", dest_prefix, "--files-only", "--include", "neondb-*.dump"],
    capture_output=True, text=True)
if result.returncode != 0:
    log(f"PRUNE FAIL: lsf returned rc={result.returncode}: {result.stderr.strip()}")
    sys.exit(result.returncode)

pat = re.compile(r"^neondb-(\d{4})(\d{2})\d{2}T\d{6}Z\.dump$")
files = sorted(
    [f.strip() for f in result.stdout.splitlines() if pat.match(f.strip())],
    reverse=True)  # newest first

keep = set(files[:nightly])

monthly_seen: dict[str, str] = {}
for f in files[nightly:]:
    m = pat.match(f)
    if m:
        ym = f"{m.group(1)}-{m.group(2)}"
        if ym not in monthly_seen:
            monthly_seen[ym] = f
keep.update(list(monthly_seen.values())[:monthly_keep])

to_delete = [f for f in files if f not in keep]
if not to_delete:
    log(f"  prune ok (kept {len(keep)}, nothing to delete)")
    sys.exit(0)

errors = 0
for f in to_delete:
    r = subprocess.run(["rclone", "delete", f"{dest_prefix}/{f}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  prune WARN: could not delete {f}: {r.stderr.strip()}")
        errors += 1

if errors:
    log(f"  prune partial ({errors} errors, kept {len(keep)}, deleted {len(to_delete)-errors})")
    sys.exit(1)
log(f"  prune ok (kept {len(keep)}, deleted {len(to_delete)})")
PY
then
  : # prune logged its own result above
else
  log "WARN: prune script exited non-zero (dump succeeded — non-fatal)"
fi

log "pg_backup end (rc=0)"
exit 0
