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

REPO_ROOT="/Users/charliebruce/xrpl_test"
# Explicit venv Python: launchd PATH-resolved `python3` is the Homebrew
# system interpreter, which does NOT have psycopg installed. db.py's
# guarded `import psycopg` then falls to `psycopg = None`, and every
# walker_health write silently no-ops. Six days of invisible failures
# (2026-07-24 → 2026-07-29) is the cost of that path resolution.
VENV_PY="${REPO_ROOT}/venv/bin/python"

# Source env for DATABASE_URL + BACKUP_BUCKET_PREFIX pin.
# NOTE: env sourcing MUST precede any parameter-default derivation below.
# 2026-08-10: previously HOST/BUCKET_PREFIX were computed before source,
# so the pin was never read. Canary flapped in lockstep with writer under
# launchd hostname-resolution variance — both wrong together = silent pass.
ENV_FILE="${XRPLDASHBOARD_ENV:-/Users/charliebruce/.config/xrpldashboard/env}"
set -a  # auto-export sourced vars — 2026-07-31 BetterStack silent-skip fix
# shellcheck disable=SC1090
[[ -r "$ENV_FILE" ]] && source "$ENV_FILE" || true
set +a
export DATABASE_URL="${DATABASE_URL:-}"

REMOTE="${BACKUP_REMOTE:-b2crypt}"
HOST="$(hostname -s)"
BUCKET_PREFIX="${BACKUP_BUCKET_PREFIX:-xrpldashboard-backup-${HOST}}"
DEST_PREFIX="${REMOTE}:${BUCKET_PREFIX}/postgres"
MAX_AGE_HOURS="${CANARY_MAX_AGE_HOURS:-25}"
PIN_SOURCE="${BACKUP_BUCKET_PREFIX:+env}"
PIN_SOURCE="${PIN_SOURCE:-hostname_fallback}"
# Divergence-scan controls: check for any dump landing in a NON-pinned
# xrpldashboard-backup-* bucket in the last DIVERGENCE_MAX_AGE_HOURS hours.
# Catches the writer-canary lockstep blind spot (both computing the same
# wrong bucket = both silent-green). Set to 0 to skip the scan.
DIVERGENCE_MAX_AGE_HOURS="${CANARY_DIVERGENCE_MAX_AGE_HOURS:-25}"

log "pg_backup_canary start (prefix=${DEST_PREFIX}, hostname=${HOST}, pin_source=${PIN_SOURCE}, max_age=${MAX_AGE_HOURS}h, divergence_scan_window=${DIVERGENCE_MAX_AGE_HOURS}h)"

# Race guard — 2026-08-20 storm-power / RunAtLoad catch-up.
# With RunAtLoad=true on both pg_backup + pg_backup_canary, a Charlie
# login after a reboot fires them together. If the canary wins the race,
# the previous day's dump might read 24h+ old = false-positive FAIL while
# a new dump is actively uploading in the paired wrapper. Defer verdict:
# ping BetterStack heartbeat (system healthy — backup in flight) + exit 0.
BACKUP_PID="$(pgrep -f 'bash .*run_pg_backup\.sh$' 2>/dev/null | head -1 || true)"
if [[ -n "$BACKUP_PID" ]]; then
  log "  race guard: pg_backup wrapper currently running (pid=${BACKUP_PID}); deferring staleness verdict"
  if [[ -n "${BETTERSTACK_PG_BACKUP_CANARY_URL:-}" ]]; then
    curl -fsS -m 10 "$BETTERSTACK_PG_BACKUP_CANARY_URL" >/dev/null \
      && log "  betterstack ping ok (deferred)" \
      || log "  betterstack ping failed (deferred — still PASS)"
  fi
  log "pg_backup_canary end (rc=0, deferred by race guard)"
  exit 0
fi

"$VENV_PY" -c "
import sys; sys.path.insert(0,'$REPO_ROOT')
import db; db.write_walker_health_start('pg_backup_canary', cadence_seconds=86400)
" 2>>"$LOG_FILE" || log "  walker_health start-write failed (canary still PASS)"

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
  "$VENV_PY" -c "import sys; sys.path.insert(0,'$REPO_ROOT'); import db; db.write_walker_health_end('pg_backup_canary', ok=False, message='no dump files found')" 2>>"$LOG_FILE" || log "  walker_health end-write failed"
  exit 2
fi

# Filename pattern: neondb-YYYYMMDDTHHMMSSZ.dump
# Extract the timestamp segment.
TS_STR="$(echo "$LATEST" | sed -nE 's/^neondb-([0-9]{8}T[0-9]{6}Z)\.dump$/\1/p')"
if [[ -z "$TS_STR" ]]; then
  log "FAIL: latest filename ${LATEST} does not match neondb-YYYYMMDDTHHMMSSZ.dump"
  "$VENV_PY" -c "import sys; sys.path.insert(0,'$REPO_ROOT'); import db; db.write_walker_health_end('pg_backup_canary', ok=False, message='filename parse failed: $LATEST')" 2>>"$LOG_FILE" || log "  walker_health end-write failed"
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
  "$VENV_PY" -c "import sys; sys.path.insert(0,'$REPO_ROOT'); import db; db.write_walker_health_end('pg_backup_canary', ok=False, message='backup ${AGE_HOURS}h old > ${MAX_AGE_HOURS}h threshold')" 2>>"$LOG_FILE" || log "  walker_health end-write failed"
  log "pg_backup_canary end (rc=4)"
  exit 4
fi

log "  ok (${AGE_HOURS}h <= ${MAX_AGE_HOURS}h)"

# Two-sided divergence scan — 2026-08-10.
# The single-bucket freshness check above cannot detect "writer landed a
# dump in the WRONG bucket" if the canary reads from the same wrong bucket
# (both derive BUCKET_PREFIX from the same source; both go wrong together).
# Enumerate every xrpldashboard-backup-* bucket on the remote, and alarm
# if any bucket OTHER THAN the pinned one contains a dump written in the
# last DIVERGENCE_MAX_AGE_HOURS hours.
if (( DIVERGENCE_MAX_AGE_HOURS > 0 )); then
  log "  divergence-scan: listing ${REMOTE}: for xrpldashboard-backup-* buckets"
  # rclone lsjson at the remote root lists buckets (b2 crypt-remote proxies
  # bucket enumeration transparently). We list --dirs-only, filter by prefix.
  DIVERGENCE_HITS=""
  while IFS= read -r other_bucket; do
    [[ -z "$other_bucket" ]] && continue
    [[ "$other_bucket" == "$BUCKET_PREFIX" ]] && continue
    other_prefix="${REMOTE}:${other_bucket}/postgres"
    other_latest="$(rclone lsf "$other_prefix" --files-only \
                      --include "neondb-*.dump" 2>/dev/null | sort -r | head -1)"
    [[ -z "$other_latest" ]] && continue
    other_ts="$(echo "$other_latest" | sed -nE 's/^neondb-([0-9]{8}T[0-9]{6}Z)\.dump$/\1/p')"
    [[ -z "$other_ts" ]] && continue
    other_epoch="$(date -j -u -f "%Y%m%dT%H%M%SZ" "$other_ts" "+%s" 2>/dev/null || echo 0)"
    (( other_epoch == 0 )) && continue
    other_age_hours=$(( (NOW_EPOCH - other_epoch) / 3600 ))
    (( other_age_hours < 0 )) && other_age_hours=0
    if (( other_age_hours <= DIVERGENCE_MAX_AGE_HOURS )); then
      log "  divergence FOUND: ${other_bucket} has ${other_latest} age=${other_age_hours}h"
      DIVERGENCE_HITS+="${other_bucket}(${other_age_hours}h) "
    fi
  done < <(rclone lsf "${REMOTE}:" --dirs-only 2>/dev/null | sed 's:/$::' | grep -E '^xrpldashboard-backup-')

  if [[ -n "$DIVERGENCE_HITS" ]]; then
    log "FAIL: writer divergence — dump(s) landed outside pinned bucket in last ${DIVERGENCE_MAX_AGE_HOURS}h: ${DIVERGENCE_HITS}"
    "$VENV_PY" -c "import sys; sys.path.insert(0,'$REPO_ROOT'); import db; db.write_walker_health_end('pg_backup_canary', ok=False, message='divergence: ${DIVERGENCE_HITS}')" 2>>"$LOG_FILE" || log "  walker_health end-write failed"
    log "pg_backup_canary end (rc=5)"
    exit 5
  fi
  log "  divergence-scan clean (no non-pinned buckets have dumps <${DIVERGENCE_MAX_AGE_HOURS}h)"
fi

"$VENV_PY" -c "import sys; sys.path.insert(0,'$REPO_ROOT'); import db; db.write_walker_health_end('pg_backup_canary', ok=True, message='${LATEST} age=${AGE_HOURS}h')" 2>>"$LOG_FILE" || log "  walker_health end-write failed"

# BetterStack heartbeat — success-path ONLY (literal last line before rc=0
# exit). Every non-zero exit above skips this line, so a failed canary =
# no ping = BetterStack incident within the heartbeat's grace window.
# Missing URL = silent skip (script keeps working without the monitor;
# never fail the canary just because the monitor URL isn't set).
if [[ -n "${BETTERSTACK_PG_BACKUP_CANARY_URL:-}" ]]; then
  curl -fsS -m 10 "$BETTERSTACK_PG_BACKUP_CANARY_URL" >/dev/null \
    && log "  betterstack ping ok" \
    || log "  betterstack ping failed (canary still PASS)"
fi

log "pg_backup_canary end (rc=0)"
exit 0
