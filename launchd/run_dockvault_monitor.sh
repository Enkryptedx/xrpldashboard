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
