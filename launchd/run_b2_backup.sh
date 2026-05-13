#!/usr/bin/env bash
#
# Nightly cloud backup wrapper for the xrpldashboard Mac.
#
# Pushes a curated set of source directories to a Backblaze B2 bucket
# via rclone. The remote name is configurable; default is "b2crypt"
# which we recommend so that ~/.ssh and .env land encrypted at rest
# under a key only Charlie holds (B2 server-side encryption keeps
# the key on Backblaze).
#
# Designed to be safe to schedule before rclone is installed or
# configured: missing binary / missing remote → log a clear message
# and exit 0. Once setup completes, the next nightly run picks up
# automatically.

set -u
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
LOG_FILE="${LOG_DIR}/b2_backup.$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

REMOTE="${BACKUP_REMOTE:-b2crypt}"
HOST="$(hostname -s)"
BUCKET_PREFIX="${BACKUP_BUCKET_PREFIX:-xrpldashboard-backup-${HOST}}"
EXCLUDES="/Users/charliebruce/xrpl_test/launchd/b2_backup.excludes"

# Source roots -> remote subpaths. Add or comment out as needed.
declare -a SOURCES=(
  "/Users/charliebruce/xrpl_test:xrpl_test"
  "/Users/charliebruce/.ssh:dot_ssh"
)

log "b2_backup start (remote=${REMOTE}, bucket=${BUCKET_PREFIX})"

if ! command -v rclone >/dev/null 2>&1; then
  log "SKIP: rclone not installed. Run \`brew install rclone\` and \`rclone config\` to enable. (exit 0)"
  exit 0
fi

if ! rclone listremotes 2>/dev/null | grep -q "^${REMOTE}:$"; then
  log "SKIP: rclone remote '${REMOTE}:' not configured. Run \`rclone config\` to add it. (exit 0)"
  exit 0
fi

EXIT_STATUS=0
for entry in "${SOURCES[@]}"; do
  SRC="${entry%%:*}"
  DST_SUB="${entry##*:}"
  if [[ ! -d "$SRC" ]]; then
    log "  skip ${SRC} — not a directory"
    continue
  fi
  DST="${REMOTE}:${BUCKET_PREFIX}/${DST_SUB}"
  log "  sync ${SRC} -> ${DST}"
  if rclone sync "$SRC" "$DST" \
      --exclude-from "$EXCLUDES" \
      --transfers 4 \
      --checkers 8 \
      --fast-list \
      --log-level INFO \
      --log-file "$LOG_FILE" 2>&1 | tee -a "$LOG_FILE"; then
    log "    ok"
  else
    rc=$?
    log "    FAIL rc=${rc}"
    EXIT_STATUS=$rc
  fi
done

log "b2_backup end (rc=${EXIT_STATUS})"
exit $EXIT_STATUS
