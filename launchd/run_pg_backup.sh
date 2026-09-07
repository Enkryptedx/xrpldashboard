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

# TCC / write-roundtrip preflight (2026-09-07 hardening after the 9.5-hour
# silent block). macOS Sequoia+ Removable Volumes consent is per-binary +
# session-context sensitive; a launchd LaunchAgent may lack the consent
# even when the same binary has it under an interactive Terminal. A stale
# grant post-Homebrew-upgrade produces the same silent-block. This probe
# writes a canary + reads it back at t=0; if consent is missing or the
# volume is r/o, we fail LOUDLY here instead of hanging pg_dump for hours.
PROBE_FILE="${SPOOL_ROOT}/.tcc_probe"
PROBE_PAYLOAD="tcc-probe-$$-$(date -u +%s)"
if ! printf '%s\n' "$PROBE_PAYLOAD" > "$PROBE_FILE" 2>/dev/null; then
  log "FAIL: write-roundtrip preflight — cannot create ${PROBE_FILE}. "
  log "       Likely macOS Removable Volumes TCC consent missing for bash/pg_dump/rclone."
  log "       Grant: System Settings → Privacy & Security → Files and Folders →"
  log "              pg_dump + rclone + bash (all binaries the wrapper invokes) →"
  log "              tick Removable Volumes."
  exit 1
fi
_read_back="$(cat "$PROBE_FILE" 2>/dev/null || echo '')"
rm -f "$PROBE_FILE" 2>/dev/null || true
if [[ "$_read_back" != "$PROBE_PAYLOAD" ]]; then
  log "FAIL: write-roundtrip preflight — wrote payload but read back differed. "
  log "       Volume may be truncating writes or is intermittently unmounted."
  exit 1
fi
log "  preflight: write-roundtrip ok (TCC consent present, volume writable)"

# Housekeeping: purge any stale tmp files left by a killed prior run
# BEFORE this run begins so we don't accumulate uncleaned dumps if the
# upload trap fires or the machine reboots mid-run.
find "$SPOOL_ROOT" -maxdepth 1 -type f -name 'neondb-*.dump' -mtime +1 \
     -exec rm -f {} \; 2>/dev/null || true

TMPDUMP="${SPOOL_ROOT}/${DUMP_NAME}"
# Trap: on any exit path (success, failure, signal) remove the tmp dump
# AND kill the watchdog subshell if it's still running.
_cleanup() {
  rm -f "$TMPDUMP" 2>/dev/null || true
  if [[ -n "${WATCHDOG_PID:-}" ]]; then
    kill "$WATCHDOG_PID" 2>/dev/null || true
  fi
}
trap _cleanup EXIT

log "  dumping → ${TMPDUMP} (then uploading → ${DEST})"
START_EPOCH=$(date +%s)

# Stuck-write watchdog (2026-09-07 hardening). Runs in a subshell,
# samples $TMPDUMP mtime every 60s; if the mtime hasn't advanced for
# WATCHDOG_STALL_SECONDS, kills the parent wrapper process. That in turn
# triggers the EXIT trap which removes the (stalled) tmp file. This is
# the belt to the TCC preflight's suspenders: if consent revokes mid-
# write, or the volume drops mid-write, the wrapper dies in ≤N min
# instead of hanging for 9.5 h.
WATCHDOG_STALL_SECONDS=1200   # 20 min
(
  WRAPPER_PID=$$
  prev_mtime=0
  stall_since=0
  while sleep 60; do
    if ! kill -0 "$WRAPPER_PID" 2>/dev/null; then
      exit 0  # wrapper gone, watchdog quits
    fi
    if [[ ! -f "$TMPDUMP" ]]; then
      continue  # pre-dump-start; keep watching
    fi
    cur_mtime="$(stat -f %m "$TMPDUMP" 2>/dev/null || echo 0)"
    if (( cur_mtime > prev_mtime )); then
      prev_mtime=$cur_mtime
      stall_since=0
      continue
    fi
    if (( stall_since == 0 )); then
      stall_since=$(date +%s)
      continue
    fi
    stall_age=$(( $(date +%s) - stall_since ))
    if (( stall_age >= WATCHDOG_STALL_SECONDS )); then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WATCHDOG: TMPDUMP mtime stalled ${stall_age}s (threshold ${WATCHDOG_STALL_SECONDS}s) — killing wrapper pid ${WRAPPER_PID}" | tee -a "$LOG_FILE" >&2
      kill "$WRAPPER_PID" 2>/dev/null || true
      exit 1
    fi
  done
) &
WATCHDOG_PID=$!
log "  watchdog: pid=${WATCHDOG_PID} threshold=${WATCHDOG_STALL_SECONDS}s"

# 2026-08-30 wound: direct (non-pooler) endpoint required for pg_dump
# to honor PGOPTIONS=statement_timeout=0. Scope BACKUP ONLY; web app,
# walkers, canary keep pooler + 25s ceiling (Neon compute protection).
# 2026-09-07 hardening: parse DATABASE_URL into components and pass them
# via env vars, NEVER as a positional pg_dump argv. Previously
# `pg_dump ... "$DUMP_URL"` exposed the password to any process able to
# read /proc / `ps -eo command`. That leak class was demonstrated when
# a JJ diagnostic `ps` printed the URL into the transcript. Env-var
# passing keeps the secret out of argv entirely.
DUMP_URL_DIRECT="${DATABASE_URL/-pooler./.}"
# Parse postgresql://user:pass@host:port/db?query with bash regex.
if [[ "$DUMP_URL_DIRECT" =~ ^postgres(ql)?://([^:]+):([^@]+)@([^:/]+)(:([0-9]+))?/([^?]+)(\?(.*))?$ ]]; then
  PG_USER="${BASH_REMATCH[2]}"
  PG_PASS="${BASH_REMATCH[3]}"
  PG_HOST="${BASH_REMATCH[4]}"
  PG_PORT="${BASH_REMATCH[6]:-5432}"
  PG_DB="${BASH_REMATCH[7]}"
  # sslmode=require, channel_binding=require live in query string; forward via
  # PGSSLMODE / PGCHANNELBINDING which libpq honors.
  PG_QUERY="${BASH_REMATCH[9]:-}"
else
  log "FAIL: DATABASE_URL did not match postgres://user:pass@host:port/db shape — refusing to run"
  exit 1
fi
# --no-owner / --no-acl produce portable dumps that restore cleanly into
# a different cluster (Neon-specific role IDs would otherwise break local
# restore). Password via PGPASSWORD env; NOT in argv.
_pg_dump_argv_free() {
  # PG* env vars are the standard libpq mechanism — pg_dump reads them
  # directly. Argv contains only flags + path, never the DSN.
  PGHOST="$PG_HOST" \
  PGPORT="$PG_PORT" \
  PGDATABASE="$PG_DB" \
  PGUSER="$PG_USER" \
  PGPASSWORD="$PG_PASS" \
  PGSSLMODE="require" \
  PGCHANNELBINDING="require" \
  PGOPTIONS='-c statement_timeout=0' \
    pg_dump -Fc --no-owner --no-acl -f "$TMPDUMP"
}
if _pg_dump_argv_free; then
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
