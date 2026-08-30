#!/usr/bin/env bash
#
# Pull-back mirror of the latest Neon pg_dump from Backblaze B2 to DockVault.
#
# Charlie's 2026-08-23 ruling (D1 = 1a-iii): leave the proven pg_backup.sh
# path UNTOUCHED, act as a pure downstream reader of what B2 already
# validated. Costs ~2x bandwidth for the dump (up then back down) —
# negligible at ~300MB compressed. Zero risk of breaking the primary
# B2 backup path.
#
# Retention: 90 daily + 12 monthly-anchored, per D5 ruling.
# (Backblaze's own retention is separately 14 nightly + 3 monthly, per
# run_pg_backup.sh — DockVault holds deeper history.)
#
# Idempotency: pulls the LATEST available dump on B2 that isn't already
# on DockVault. Safe to run multiple times per day — no-ops if already
# in sync. Robust against the actual pg_backup schedule (22:00 EDT nightly
# per its plist, filename UTC-stamped ~02:00 UTC): doesn't assume a
# specific landing time, just pulls whatever's newest.

set -u
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
LOG_FILE="${LOG_DIR}/dockvault_neon_dump.$(date +%Y-%m-%d).log"
LAUNCHD_STATE_DIR="/Users/charliebruce/xrpl_test/launchd_state"
mkdir -p "$LOG_DIR" "$LAUNCHD_STATE_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

# Source pinned env (BACKUP_REMOTE, BACKUP_BUCKET_PREFIX). Same lesson
# as run_b2_backup.sh + run_pg_backup.sh: without this pin, hostname
# flip re-points to orphan bucket.
if [[ -f "$HOME/.config/xrpldashboard/env" ]]; then
  set -a; . "$HOME/.config/xrpldashboard/env"; set +a
fi

# shellcheck disable=SC1091
source "/Users/charliebruce/xrpl_test/launchd/dockvault_preflight.sh"

REMOTE="${BACKUP_REMOTE:-b2crypt}"
HOST="$(hostname -s)"
BUCKET_PREFIX="${BACKUP_BUCKET_PREFIX:-xrpldashboard-backup-${HOST}}"
SRC_PREFIX="${REMOTE}:${BUCKET_PREFIX}/postgres"
LOCAL_DIR="${DOCKVAULT_ROOT}/neon_dumps"
KEEP_DAILY="${DOCKVAULT_NEON_DAILY:-90}"
KEEP_MONTHLY="${DOCKVAULT_NEON_MONTHLY:-12}"

log "dockvault_neon_dump start (src=${SRC_PREFIX}, dest=${LOCAL_DIR}, keep=${KEEP_DAILY}d+${KEEP_MONTHLY}m)"

if ! command -v rclone >/dev/null 2>&1; then
  log "SKIP: rclone not on PATH"
  log "dockvault_neon_dump end (rc=0, rclone-missing skip)"
  exit 0
fi

if ! rclone listremotes 2>/dev/null | grep -q "^${REMOTE}:$"; then
  log "SKIP: rclone remote '${REMOTE}:' not configured"
  log "dockvault_neon_dump end (rc=0, remote-missing skip)"
  exit 0
fi

if ! dockvault_preflight; then
  log "dockvault_neon_dump end (rc=0, preflight skip)"
  exit 0
fi

mkdir -p "$LOCAL_DIR"

# Find the latest dump on B2 by name (naming: neondb-YYYYMMDDTHHMMSSZ.dump).
LATEST="$(rclone lsf "$SRC_PREFIX" --files-only --include "neondb-*.dump" --timeout 30m --contimeout 60s 2>/dev/null | sort -r | head -1)"

if [[ -z "$LATEST" ]]; then
  log "SKIP: no dumps found at ${SRC_PREFIX}"
  log "dockvault_neon_dump end (rc=0, no-source skip)"
  exit 0
fi

log "  latest on B2: ${LATEST}"

SUCCESS=0
if [[ -f "${LOCAL_DIR}/${LATEST}" ]]; then
  log "  already have ${LATEST} locally — pull-back no-op"
  SUCCESS=1
else
  log "  pulling ${LATEST} -> ${LOCAL_DIR}/"
  if rclone copy "${SRC_PREFIX}/${LATEST}" "$LOCAL_DIR/" \
      --timeout 30m \
      --contimeout 60s \
      --log-level INFO \
      --log-file "$LOG_FILE" 2>&1 | tee -a "$LOG_FILE"; then
    log "  ok — pull-back complete"
    SUCCESS=1
  else
    rc=$?
    log "  FAIL: pull-back rc=${rc} (exit 0, monitor catches persistent fail)"
    log "dockvault_neon_dump end (rc=0, pull-fail)"
    exit 0
  fi
fi

# Prune: keep KEEP_DAILY days of daily + KEEP_MONTHLY monthly-anchored.
# Same shape as run_pg_backup.sh prune, just larger windows.
python3 - "$LOCAL_DIR" "$KEEP_DAILY" "$KEEP_MONTHLY" "$LOG_FILE" <<'PY' || log "WARN: prune non-zero (non-fatal)"
import os, re, signal, sys
from datetime import datetime, timezone

local_dir, keep_daily, keep_monthly, log_file = \
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(log_file, "a") as f:
        f.write(line + "\n")

# A3 ruling 2026-08-28: 60s per-op alarm belt on the prune loop. Catches
# the exact silent-hang class from the 2026-08-25 22h prune stall — a
# blocked os.remove (Spotlight lock, ejected mid-op, half-mounted volume)
# no longer wedges the whole job forever.
class RemoveTimeout(Exception):
    pass

def _on_alarm(signum, frame):
    raise RemoveTimeout()

signal.signal(signal.SIGALRM, _on_alarm)

pat = re.compile(r"^neondb-(\d{4})(\d{2})(\d{2})T\d{6}Z\.dump$")
files = []
for f in sorted(os.listdir(local_dir), reverse=True):
    m = pat.match(f)
    if m:
        files.append((f, m.group(1), m.group(2)))

keep = set(f[0] for f in files[:keep_daily])

monthly_seen = {}
for f, y, mo in files[keep_daily:]:
    ym = f"{y}-{mo}"
    if ym not in monthly_seen:
        monthly_seen[ym] = f
keep.update(list(monthly_seen.values())[:keep_monthly])

to_delete = [f[0] for f in files if f[0] not in keep]
if not to_delete:
    log(f"  prune ok (kept {len(keep)}, nothing to delete)")
    sys.exit(0)

errors = 0
for name in to_delete:
    signal.alarm(60)
    try:
        os.remove(os.path.join(local_dir, name))
        signal.alarm(0)
    except RemoveTimeout:
        log(f"  prune WARN: 60s timeout removing {name} (skipping, will retry next run)")
        errors += 1
    except Exception as e:
        signal.alarm(0)
        log(f"  prune WARN: could not delete {name}: {e}")
        errors += 1

if errors:
    log(f"  prune partial ({errors} errors, kept {len(keep)}, deleted {len(to_delete)-errors})")
    sys.exit(1)
log(f"  prune ok (kept {len(keep)}, deleted {len(to_delete)})")
PY

if [[ $SUCCESS -eq 1 ]]; then
  # B2 ruling 2026-08-28: last_completed_ok stamp for the hourly monitor's
  # stale-check. Only written on genuine sync-current (pull-back complete
  # or file already local); skipped on all fail/no-source paths so persistent
  # silent skip surfaces as a WITHHELD BetterStack heartbeat.
  date -u +%s > "${LAUNCHD_STATE_DIR}/dockvault_neon_dump_last_ok"
  log "  state: wrote last_ok -> ${LAUNCHD_STATE_DIR}/dockvault_neon_dump_last_ok"
fi

log "dockvault_neon_dump end (rc=0)"
exit 0
