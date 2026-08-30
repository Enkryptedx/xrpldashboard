#!/usr/bin/env bash
#
# Hourly heartbeat for DockVault. Loud skip on any failure; pings
# BetterStack heartbeat URL on success. Missing heartbeat -> alert.
#
# Two checks (both via dockvault_preflight): mount presence + active
# write-test round-trip. The write-test catches the exact silent-hang
# class from the 2026-08-23 dock revival — mount can be present while
# volume is inaccessible due to FDA gap, Spotlight lock, or half-
# unlocked keychain.
#
# DOCKVAULT_MONITOR_HEARTBEAT_URL env var must be set (BetterStack
# heartbeat URL) for the ping to fire. If unset, script runs the
# checks + logs but doesn't ping — safe for dry-run / pre-config.

set -u
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
LOG_FILE="${LOG_DIR}/dockvault_monitor.$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

if [[ -f "$HOME/.config/xrpldashboard/env" ]]; then
  set -a; . "$HOME/.config/xrpldashboard/env"; set +a
fi

# shellcheck disable=SC1091
source "/Users/charliebruce/xrpl_test/launchd/dockvault_preflight.sh"

log "dockvault_monitor start"

if ! dockvault_preflight; then
  log "dockvault_monitor end (rc=0, preflight fail — NO heartbeat ping)"
  exit 0
fi

log "  preflight ok — mount + write round-trip succeeded"

# B2 ruling 2026-08-28: check that mirror + neon_dump have written a
# fresh last_completed_ok stamp in the last 26h. If either is stale (or
# missing), WITHHOLD the BetterStack heartbeat so the missing-heartbeat
# alarm fires. Catches the persistent-silent-skip class that always-exit-0
# would otherwise hide (e.g. FDA revoked, remote un-configured, mount
# present but sync loop no-ops for days).
LAUNCHD_STATE_DIR="/Users/charliebruce/xrpl_test/launchd_state"
STALE_THRESHOLD=93600  # 26h — mirror runs nightly, so 24h + 2h grace.
NOW=$(date -u +%s)
STALE_JOB=""
for job in mirror neon_dump; do
  state_file="${LAUNCHD_STATE_DIR}/dockvault_${job}_last_ok"
  if [[ ! -f "$state_file" ]]; then
    STALE_JOB="${job} (last_ok never written)"
    break
  fi
  last=$(cat "$state_file" 2>/dev/null || echo 0)
  age=$((NOW - last))
  if (( age > STALE_THRESHOLD )); then
    STALE_JOB="${job} (${age}s since last_ok, threshold ${STALE_THRESHOLD}s)"
    break
  fi
done

if [[ -n "$STALE_JOB" ]]; then
  log "  WITHHOLDING heartbeat: ${STALE_JOB}"
  log "dockvault_monitor end (rc=0, stale-job — NO heartbeat ping)"
  exit 0
fi
log "  freshness ok — mirror + neon_dump both stamped <26h ago"

HB_URL="${DOCKVAULT_MONITOR_HEARTBEAT_URL:-}"
if [[ -z "$HB_URL" ]]; then
  log "  heartbeat URL unset (DOCKVAULT_MONITOR_HEARTBEAT_URL) — skipping ping"
  log "  add to ~/.config/xrpldashboard/env once BetterStack monitor is created"
  log "dockvault_monitor end (rc=0, no-url)"
  exit 0
fi

if curl -fsS --max-time 10 "$HB_URL" >/dev/null 2>&1; then
  log "  betterstack heartbeat ok"
else
  rc=$?
  log "  WARN: betterstack heartbeat curl failed rc=${rc}"
fi

log "dockvault_monitor end (rc=0)"
exit 0
