#!/usr/bin/env bash
#
# Hourly guard: verifies the 5 XRPL_ env vars in ~/.config/xrpldashboard/env
# all point at :5006 (Lenovo LAN non-admin RPC port). Any drift lifts
# walker_health.findings_count > 0 → BetterStack page.
#
# Codified 2026-08-31 after accidental scrollback-paste of a Wave-0 step (d)
# `mv env.bak-2026-08-31 env` reversibility command reverted XRPL_LOCAL_NODE
# :5006 → :5005 undetected for ~40 min. Value-drift detection only —
# no bak-file fingerprinting per doctrine (catch drift regardless of cause).
#
# Wrapper timeout belt: 60s Perl SIGALRM. env-file read is sub-second in
# practice; 60s covers a wedged NFS/APFS edge case.

set -u
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
LOG_FILE="${LOG_DIR}/xrpl_env_drift_guard.$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

# Source env file so DATABASE_URL_DIRECT + DATABASE_URL reach the walker's
# db.write_walker_health_* calls. The walker itself opens ENV_FILE
# directly to check XRPL_ vars — sourcing here is for DB writes only.
ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  log "ERROR: env file missing or unreadable: $ENV_FILE"
  exit 78  # EX_CONFIG
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

log "xrpl_env_drift_guard start (wrapper_timeout=60s)"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/xrpl_env_drift_guard.py"

perl -e 'alarm shift; exec @ARGV' 60 "$PYTHON" "$SCRIPT" 2>&1 | tee -a "$LOG_FILE"
RC=${PIPESTATUS[0]}

log "xrpl_env_drift_guard end (rc=${RC})"
exit "$RC"
