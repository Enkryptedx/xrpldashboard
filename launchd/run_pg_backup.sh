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

if [[ ! -r "$ENV_FILE" ]]; then
  log "FAIL: env file not readable at ${ENV_FILE}"
  exit 1
fi
set -a  # auto-export sourced vars — 2026-07-31 BetterStack silent-skip fix
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# NOTE: env sourcing MUST precede any parameter-default derivation below.
# 2026-08-10: previously HOST/BUCKET_PREFIX were computed before source,
# so the BACKUP_BUCKET_PREFIX pin in env was never read — bucket fell back
# to hostname derivation which varied under launchd context (returned "Mac"
# on some days, "Charlies-Mac-mini" on others). Landed 2 dumps in the wrong
# bucket 2026-08-07 and 2026-08-08 before discovery.
REMOTE="${BACKUP_REMOTE:-b2crypt}"
HOST="$(hostname -s)"
BUCKET_PREFIX="${BACKUP_BUCKET_PREFIX:-xrpldashboard-backup-${HOST}}"
DEST_PREFIX="${REMOTE}:${BUCKET_PREFIX}/postgres"
PG_BACKUP_NIGHTLY="${PG_BACKUP_NIGHTLY:-14}"
PG_BACKUP_MONTHLY="${PG_BACKUP_MONTHLY:-3}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP_NAME="neondb-${TS}.dump"
DEST="${DEST_PREFIX}/${DUMP_NAME}"

# Self-diagnostic: "env" if the pin was read from env, "hostname_fallback" if not.
# Historical logs before 2026-08-10 fix always show "hostname_fallback" — the pin was never applied.
PIN_SOURCE="${BACKUP_BUCKET_PREFIX:+env}"
PIN_SOURCE="${PIN_SOURCE:-hostname_fallback}"
log "pg_backup start (remote=${REMOTE}, bucket=${BUCKET_PREFIX}, hostname=${HOST}, pin_source=${PIN_SOURCE}, keep=${PG_BACKUP_NIGHTLY}n+${PG_BACKUP_MONTHLY}m)"

# Idempotency guard — 2026-08-20 storm-power / RunAtLoad catch-up.
# With RunAtLoad=true on the plist, this wrapper fires at every LaunchAgent
# load (Charlie login after reboot). If today's dump already landed via the
# scheduled 03:30 fire (or a prior manual kick / prior login this day),
# skip work + exit 0. TODAY_PREFIX matches "neondb-YYYYMMDDT" in UTC to
# align with the dump filename convention set by TS above.
TODAY_PREFIX="neondb-$(date -u +%Y%m%d)T"
if command -v rclone >/dev/null 2>&1 && \
   rclone listremotes 2>/dev/null | grep -q "^${REMOTE}:$"; then
  EXISTING="$(rclone lsf "$DEST_PREFIX" --files-only \
                --include "${TODAY_PREFIX}*" 2>/dev/null | sort -r | head -1 || true)"
  if [[ -n "$EXISTING" ]]; then
    log "  catch-up guard: today's dump already exists (${EXISTING}) — skipping"
    log "pg_backup end (rc=0, skipped by catch-up guard)"
    exit 0
  fi
fi

if ! command -v pg_dump >/dev/null 2>&1; then
  log "FAIL: pg_dump not on PATH. Run 'brew install postgresql@17'."
  exit 1
fi

if ! command -v rclone >/dev/null 2>&1; then
  log "FAIL: rclone not on PATH."
  exit 1
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
  log "FAIL: DATABASE_URL not set after sourcing ${ENV_FILE}"
  exit 1
fi

if ! rclone listremotes 2>/dev/null | grep -q "^${REMOTE}:$"; then
  log "FAIL: rclone remote '${REMOTE}:' not configured."
  exit 1
fi

# 2026-09-06 A+B hardening (Charlie ruling on PG_BACKUP_HARDENING_PROPOSAL):
# spool the dump to DockVault first, then upload with `rclone copy` (chunked
# multipart with per-chunk retries) instead of streaming through rcat.
# Turns "one B2 503 wipes the entire 5000s dump" (09-04 signature) into
# "one B2 503 retries chunk N without re-dumping". A: retry budget bumped
# from rcat default 3 to copy 10 with 30s sleep. B: dump preserved on disk
# so a failed upload doesn't cost the dump time.
SPOOL_ROOT="/Volumes/DockVault/neon_dumps/tmp"
# Precheck: DockVault must be mounted + have ≥15 GB free (dump is ~6.6 GB
# and growing; headroom for one dump + safety). Skip loud if not — do NOT
# silently fall back to /tmp (macOS /tmp is on the startup disk and a 7 GB
# write could push cache/log processes to disk-full).
if ! mkdir -p "$SPOOL_ROOT" 2>/dev/null; then
  log "FAIL: cannot create spool root ${SPOOL_ROOT} (DockVault not mounted?)"
  exit 1
fi
SPOOL_FREE_KB="$(df -k "$SPOOL_ROOT" | awk 'NR==2 {print $4}')"
if [[ -z "$SPOOL_FREE_KB" ]] || (( SPOOL_FREE_KB < 15 * 1024 * 1024 )); then
  log "FAIL: spool ${SPOOL_ROOT} has <15 GB free (${SPOOL_FREE_KB} KB)"
  exit 1
fi

# Housekeeping: purge any stale tmp files left by a killed prior run
# BEFORE this run begins so we don't accumulate uncleaned dumps if the
# upload trap fires or the machine reboots mid-run.
find "$SPOOL_ROOT" -maxdepth 1 -type f -name 'neondb-*.dump' -mtime +1 \
     -exec rm -f {} \; 2>/dev/null || true

TMPDUMP="${SPOOL_ROOT}/${DUMP_NAME}"
# Trap: on any exit path (success, failure, signal) remove the tmp dump.
# Guarantees zero orphans in the spool even if pg_dump / rclone crash.
trap 'rm -f "$TMPDUMP" 2>/dev/null || true' EXIT

log "  dumping → ${TMPDUMP} (then uploading → ${DEST})"
START_EPOCH=$(date +%s)

# Direct (non-pooler) endpoint for pg_dump — 2026-08-30 wound.
# Neon's PgBouncer pooler enforces server-side statement_timeout=25s at
# connection setup and rejects the startup `options=` parameter, so
# PGOPTIONS/SET statement_timeout=0 never reaches the backend through the
# pooler. Full-table dumps of nft_activity/events grew past the 25s budget
# → PQgetCopyData/PQgetResult failures. The direct endpoint bypasses
# PgBouncer and honors PGOPTIONS.
# Scope: BACKUP ONLY. Web app, walkers, canary, everything else keeps
# the pooler + 25s ceiling untouched (Neon compute protection).
DUMP_URL="${DATABASE_URL/-pooler./.}"

# --no-owner / --no-acl produce portable dumps that restore cleanly into
# a different cluster (Neon-specific role IDs would otherwise break local
# restore).
if PGOPTIONS='-c statement_timeout=0' \
   pg_dump -Fc --no-owner --no-acl -f "$TMPDUMP" "$DUMP_URL"; then
  DUMP_EPOCH=$(date +%s)
  DUMP_DURATION=$((DUMP_EPOCH - START_EPOCH))
  LOCAL_BYTES="$(stat -f %z "$TMPDUMP" 2>/dev/null || echo unknown)"
  log "  pg_dump ok  size=${LOCAL_BYTES} bytes  duration=${DUMP_DURATION}s"
else
  rc=$?
  log "FAIL: pg_dump exited rc=${rc} — no upload attempted"
  exit "$rc"
fi

# rclone copy: chunked multipart with per-chunk retries. --retries 10 +
# --retries-sleep 30s gives a 5-min window for B2 to recover from a
# 'no tomes available' (09-04 signature). --low-level-retries 20 covers
# HTTP-layer transients within each chunk attempt. --checksum verifies
# the uploaded object matches the local file (belt-and-braces vs a
# truncated write).
if rclone copy "$TMPDUMP" "$DEST_PREFIX/" \
    --log-level INFO --log-file "$LOG_FILE" \
    --retries 10 --retries-sleep 30s \
    --low-level-retries 20 --checksum; then
  END_EPOCH=$(date +%s)
  UPLOAD_DURATION=$((END_EPOCH - DUMP_EPOCH))
  TOTAL_DURATION=$((END_EPOCH - START_EPOCH))
  SIZE_BYTES="$(rclone size "$DEST" --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["bytes"])' \
    || echo "unknown")"
  log "  upload ok  size=${SIZE_BYTES} bytes  upload=${UPLOAD_DURATION}s  total=${TOTAL_DURATION}s"
else
  rc=$?
  log "FAIL: rclone copy exited rc=${rc} after retry budget"
  # Best-effort: try to clean a partial upload if one exists on B2.
  # The local $TMPDUMP is preserved by the EXIT trap only being registered
  # to `rm`; on rc≠0 we leave it in place for manual retry / diagnosis
  # — override the trap here so re-runs can retry the upload without
  # re-dumping.
  trap - EXIT
  log "  local dump preserved at ${TMPDUMP} — re-run for upload retry"
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
