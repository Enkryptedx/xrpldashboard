#!/usr/bin/env bash
#
# Nightly local mirror of ~/xrpl_test + ~/.ssh to DockVault.
#
# Complements run_b2_backup.sh: same source set + same excludes, but
# destination is the local APFS-encrypted DockVault instead of Backblaze
# B2. Gives cloud-independent recovery — if Backblaze account is locked,
# network is down, or B2 has a regional outage, restore from the dock.
#
# Uses rclone sync (single-copy mirror semantics: current-state only,
# no history — Backblaze holds the versioned history + retention.)
# DockVault is for FAST local recovery, not archaeology.
#
# Doctrine: no unwatched writers. This script uses dockvault_preflight
# to loud-skip if the volume is not mounted or not writeable in this
# launchd context — never a silent hang. Monitor script catches
# persistent skip via missing BetterStack heartbeat.

set -u
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
LOG_FILE="${LOG_DIR}/dockvault_mirror.$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

# Source shared preflight helper. Defines dockvault_preflight() and
# exports DOCKVAULT_ROOT.
# shellcheck disable=SC1091
source "/Users/charliebruce/xrpl_test/launchd/dockvault_preflight.sh"

log "dockvault_mirror start (dest=${DOCKVAULT_ROOT}/xrpl_mirror)"

if ! command -v rclone >/dev/null 2>&1; then
  log "SKIP: rclone not on PATH (install with \`brew install rclone\`)"
  log "dockvault_mirror end (rc=0, rclone-missing skip)"
  exit 0
fi

if ! dockvault_preflight; then
  log "dockvault_mirror end (rc=0, preflight skip)"
  exit 0
fi

DEST_ROOT="${DOCKVAULT_ROOT}/xrpl_mirror"
EXCLUDES="/Users/charliebruce/xrpl_test/launchd/b2_backup.excludes"

# Source roots -> subpaths. Deliberately parity with run_b2_backup.sh
# so a restore from either dock or B2 lands the same tree layout.
declare -a SOURCES=(
  "/Users/charliebruce/xrpl_test:xrpl_test"
  "/Users/charliebruce/.ssh:dot_ssh"
)

mkdir -p "$DEST_ROOT"

FAIL_COUNT=0
for entry in "${SOURCES[@]}"; do
  SRC="${entry%%:*}"
  DST_SUB="${entry##*:}"
  if [[ ! -d "$SRC" ]]; then
    log "  skip ${SRC} — not a directory"
    continue
  fi
  DST="${DEST_ROOT}/${DST_SUB}"
  mkdir -p "$DST"
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
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
done

log "dockvault_mirror end (fails=${FAIL_COUNT})"
# Per launchd retry-storm avoidance doctrine: always exit 0.
# The log is the source of truth; heartbeat monitor catches persistent fail.
exit 0
