#!/usr/bin/env bash
#
# Twice-daily local mirror of JJ's memory dirs + workspace anchor .mds
# to DockVault. DockVault-only per doctrine: memory is
# personal-context + decision-log — never rides to B2.
#
# Sources:
#   1. ~/.openclaw/workspace/memory                            (daily notes + curated MEMORY.md if present)
#   2. ~/.claude/projects/-Users-charliebruce--openclaw-workspace/memory  (auto-memory: MEMORY.md, rules-full.md, feedback/project/reference *.md)
#   3. ~/.openclaw/workspace top-level *.md anchors (AGENTS.md, SOUL.md, USER.md, IDENTITY.md, TOOLS.md, HEARTBEAT.md)
#
# Cadence: 04:30 EDT + 16:30 EDT — a working day of memory
# must not ride on a single 04:00 snapshot (2026-08-31 ruling).
#
# Layout on dock:
#   ${DOCKVAULT_ROOT}/memory_mirror/current/<subpath>/...    ← latest
#   ${DOCKVAULT_ROOT}/memory_mirror/snapshots/YYYY-MM-DD/... ← --backup-dir of changed/deleted only
#
# Retention: prune snapshots/ dirs older than 90d at end of successful run.
#
# Doctrine parity with dockvault_mirror: loud-skip via preflight, always
# exit 0, monitor catches persistent silence via last_ok stamp freshness.

set -u
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
LOG_FILE="${LOG_DIR}/dockvault_memory_mirror.$(date +%Y-%m-%d).log"
LAUNCHD_STATE_DIR="/Users/charliebruce/xrpl_test/launchd_state"
mkdir -p "$LOG_DIR" "$LAUNCHD_STATE_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

# shellcheck disable=SC1091
source "/Users/charliebruce/xrpl_test/launchd/dockvault_preflight.sh"

log "dockvault_memory_mirror start (dest=${DOCKVAULT_ROOT}/memory_mirror)"

if ! command -v rclone >/dev/null 2>&1; then
  log "SKIP: rclone not on PATH"
  log "dockvault_memory_mirror end (rc=0, rclone-missing skip)"
  exit 0
fi

if ! dockvault_preflight; then
  log "dockvault_memory_mirror end (rc=0, preflight skip)"
  exit 0
fi

DEST_ROOT="${DOCKVAULT_ROOT}/memory_mirror"
SNAPSHOT_DATE="$(date -u +%F)"
BACKUP_DIR="${DEST_ROOT}/snapshots/${SNAPSHOT_DATE}"
mkdir -p "${DEST_ROOT}/current" "${DEST_ROOT}/snapshots"

# (source_path, dest_subpath). No filters — Charlie's 2026-08-31 ruling:
# add the ENTIRE ~/.openclaw/workspace/ tree (dock only). Defense-in-depth
# from B2 = `.openclaw/**` + `**/.openclaw/**` in b2_backup.excludes. Also
# absorbs the workspace/memory subdir (was source #1) — no need to sync
# it separately.
declare -a SOURCES=(
  "/Users/charliebruce/.openclaw/workspace|openclaw_workspace"
  "/Users/charliebruce/.claude/projects/-Users-charliebruce--openclaw-workspace/memory|claude_project_memory"
)

FAIL_COUNT=0
for entry in "${SOURCES[@]}"; do
  IFS='|' read -r SRC DST_SUB <<< "$entry"
  if [[ ! -d "$SRC" ]]; then
    log "  skip ${SRC} — not a directory"
    continue
  fi
  DST="${DEST_ROOT}/current/${DST_SUB}"
  BDIR="${BACKUP_DIR}/${DST_SUB}"
  mkdir -p "$DST"
  log "  sync ${SRC} -> ${DST} (backup-dir=${BDIR})"
  if rclone sync "$SRC" "$DST" \
      --delete-excluded \
      --backup-dir "$BDIR" \
      --transfers 4 \
      --checkers 8 \
      --fast-list \
      --timeout 30m \
      --contimeout 60s \
      --log-level INFO \
      --log-file "$LOG_FILE" 2>&1 | tee -a "$LOG_FILE"; then
    log "    ok"
  else
    rc=$?
    log "    FAIL rc=${rc}"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
done

# 90d retention on snapshots/. Empty snapshot dirs (no changes that day)
# are pruned by `-empty` as a courtesy so ls shows only real diff days.
PRUNED=0
if [[ -d "${DEST_ROOT}/snapshots" ]]; then
  # -mtime +90 uses dir mtime (updated when files land inside). Safe.
  while IFS= read -r -d '' d; do
    rm -rf "$d" && PRUNED=$((PRUNED + 1))
  done < <(find "${DEST_ROOT}/snapshots" -mindepth 1 -maxdepth 1 -type d -mtime +90 -print0 2>/dev/null)
  # Prune today's snapshot dir if empty (nothing changed).
  find "${DEST_ROOT}/snapshots" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null || true
fi
log "  retention: pruned ${PRUNED} snapshot dir(s) older than 90d"

if [[ $FAIL_COUNT -eq 0 ]]; then
  date -u +%s > "${LAUNCHD_STATE_DIR}/dockvault_memory_mirror_last_ok"
  log "  state: wrote last_ok -> ${LAUNCHD_STATE_DIR}/dockvault_memory_mirror_last_ok"
fi

log "dockvault_memory_mirror end (fails=${FAIL_COUNT})"
exit 0
