#!/usr/bin/env bash
#
# Nightly Postgres dump → encrypted B2.
#
# Streams pg_dump custom-format output directly through rclone rcat to
# the b2crypt remote — no plaintext ever touches local disk. Closes
# the #1 storage gap from STORAGE_INVENTORY_2026-05-25.md (Neon is
# not pg_dumped, free tier offers 1 day PITR).
#
# Retention: 30 days. Older dumps deleted after a successful upload.
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
RETENTION_DAYS="${PG_BACKUP_RETENTION_DAYS:-30}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP_NAME="neondb-${TS}.dump"
DEST="${DEST_PREFIX}/${DUMP_NAME}"

log "pg_backup start (remote=${REMOTE}, bucket=${BUCKET_PREFIX}, retention=${RETENTION_DAYS}d)"

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

log "  pruning dumps older than ${RETENTION_DAYS}d"
if rclone delete "$DEST_PREFIX" --min-age "${RETENTION_DAYS}d" \
    --log-level INFO --log-file "$LOG_FILE"; then
  log "  prune ok"
else
  rc=$?
  log "WARN: prune failed rc=${rc} (dump succeeded — non-fatal)"
fi

log "pg_backup end (rc=0)"
exit 0
