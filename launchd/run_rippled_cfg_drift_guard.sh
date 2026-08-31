#!/usr/bin/env bash
#
# Hourly guard: verify Lenovo's /etc/rippled/rippled.cfg port stanzas
# remain sovereign-safe. Reads the live cfg via `ssh rippled-node cat` —
# file is world-readable, no sudo needed for reads.
#
# Watches ip= and admin= lines on:
#   [port_rpc_admin_local]  → 127.0.0.1 / 127.0.0.1
#   [port_rpc_public_lan]   → 192.168.40.95 / admin-absent
#   [port_rpc_public]       → 127.0.0.1     / admin-absent
#                              (absent stanza OK pre-tunnel — Step 1)
#
# Hard prerequisite for tunnel Step 1 per
# triage/TUNNEL_DESIGN_PACK_2026-08-31.md.
#
# Wrapper timeout belt: 90s. SSH connect (8s) + cat (~1s) + parse + DB
# write is usually <5s; 90s covers a wedged network / DNS edge case.

set -u
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
LOG_FILE="${LOG_DIR}/rippled_cfg_drift_guard.$(date +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

# Source env for DATABASE_URL_DIRECT + DATABASE_URL (walker_health writes).
ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  log "ERROR: env file missing or unreadable: $ENV_FILE"
  exit 78  # EX_CONFIG
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

log "rippled_cfg_drift_guard start (wrapper_timeout=90s)"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/rippled_cfg_drift_guard.py"

perl -e 'alarm shift; exec @ARGV' 90 "$PYTHON" "$SCRIPT" 2>&1 | tee -a "$LOG_FILE"
RC=${PIPESTATUS[0]}

log "rippled_cfg_drift_guard end (rc=${RC})"
exit "$RC"
