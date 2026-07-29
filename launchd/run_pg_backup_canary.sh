#!/usr/bin/env bash
#
# Daily freshness canary for the Postgres backup pipeline.
#
# Lists b2crypt:.../postgres/ for the most recent neondb-*.dump,
# parses the embedded UTC timestamp from the filename, computes age
# in hours, and exits nonzero if age exceeds CANARY_MAX_AGE_HOURS
# (default 25 — gives the 03:30 backup an hour of slack).
#
# Filename parsing beats rclone-modtime parsing here: crypt remotes
# can proxy/lose modtime metadata, but the filename is deterministic
# (set by run_pg_backup.sh at dump time) and survives the encryption
# boundary intact.
#
# Pairs with the weekly restore-test: the canary answers "are
# backups running?", the restore-test answers "are backups usable?"
#
# Override CANARY_MAX_AGE_HOURS for testing (e.g. =0 to force a
# stale-case failure during verification).

set -euo pipefail
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
LOG_FILE="${LOG_DIR}/pg_backup_canary.$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

REMOTE="${BACKUP_REMOTE:-b2crypt}"
HOST="$(hostname -s)"
BUCKET_PREFIX="${BACKUP_BUCKET_PREFIX:-xrpldashboard-backup-${HOST}}"
DEST_PREFIX="${REMOTE}:${BUCKET_PREFIX}/postgres"
MAX_AGE_HOURS="${CANARY_MAX_AGE_HOURS:-25}"
REPO_ROOT="/Users/charliebruce/xrpl_test"

# Source env for DATABASE_URL so we can write walker_health.
ENV_FILE="${XRPLDASHBOARD_ENV:-/Users/charliebruce/.config/xrpldashboard/env}"
# shellcheck disable=SC1090
[[ -r "$ENV_FILE" ]] && source "$ENV_FILE" || true
export DATABASE_URL="${DATABASE_URL:-}"

log "pg_backup_canary start (prefix=${DEST_PREFIX}, max_age=${MAX_AGE_HOURS}h)"
python3 -c "
import sys; sys.path.insert(0,'$REPO_ROOT')
import db; db.write_walker_health_start('pg_backup_canary', cadence_seconds=86400)
" 2>/dev/null || true

if ! command -v rclone >/dev/null 2>&1; then
  log "FAIL: rclone not on PATH."
  exit 1
fi

# rclone lsf with --files-only --include returns plain filenames only.
# sort -r gives lexicographic descending = chronologically newest first
# (YYYYMMDDTHHMMSSZ names sort correctly as strings).
# NOTE: --order-by name,desc is a transfer-order flag in rclone ≥1.59, NOT
# an lsf output sort — it returned the OLDEST file first on v1.74.1.
LATEST="$(rclone lsf "$DEST_PREFIX" \
            --files-only --include "neondb-*.dump" \
            2>/dev/null | sort -r | head -1)"

if [[ -z "$LATEST" ]]; then
  log "FAIL: no neondb-*.dump files in ${DEST_PREFIX}"
  python3 -c "import sys; sys.path.insert(0,'$REPO_ROOT'); import db; db.write_walker_health_end('pg_backup_canary', ok=False, message='no dump files found')" 2>/dev/null || true
  exit 2
fi

# Filename pattern: neondb-YYYYMMDDTHHMMSSZ.dump
# Extract the timestamp segment.
TS_STR="$(echo "$LATEST" | sed -nE 's/^neondb-([0-9]{8}T[0-9]{6}Z)\.dump$/\1/p')"
if [[ -z "$TS_STR" ]]; then
  log "FAIL: latest filename ${LATEST} does not match neondb-YYYYMMDDTHHMMSSZ.dump"
  python3 -c "import sys; sys.path.insert(0,'$REPO_ROOT'); import db; db.write_walker_health_end('pg_backup_canary', ok=False, message='filename parse failed: $LATEST')" 2>/dev/null || true
  exit 3
fi

# macOS date: -j (don't set system clock), -u (interpret/output UTC),
# -f (input format). +%s outputs epoch seconds.
FILE_EPOCH="$(date -j -u -f "%Y%m%dT%H%M%SZ" "$TS_STR" "+%s")"
NOW_EPOCH="$(date -u +%s)"
AGE_SEC=$((NOW_EPOCH - FILE_EPOCH))
AGE_HOURS=$((AGE_SEC / 3600))
# Floor at 0 to handle minor clock skew if a backup file's embedded
# timestamp lands slightly in the future relative to the canary host.
if (( AGE_HOURS < 0 )); then AGE_HOURS=0; fi

log "  latest=${LATEST}  age=${AGE_HOURS}h"

if (( AGE_HOURS > MAX_AGE_HOURS )); then
  log "FAIL: latest backup is ${AGE_HOURS}h old (threshold: ${MAX_AGE_HOURS}h)"
  python3 -c "import sys; sys.path.insert(0,'$REPO_ROOT'); import db; db.write_walker_health_end('pg_backup_canary', ok=False, message='backup ${AGE_HOURS}h old > ${MAX_AGE_HOURS}h threshold')" 2>/dev/null || true
  log "pg_backup_canary end (rc=4)"
  exit 4
fi

log "  ok (${AGE_HOURS}h <= ${MAX_AGE_HOURS}h)"
python3 -c "import sys; sys.path.insert(0,'$REPO_ROOT'); import db; db.write_walker_health_end('pg_backup_canary', ok=True, message='${LATEST} age=${AGE_HOURS}h')" 2>/dev/null || true
log "pg_backup_canary end (rc=0)"
exit 0
