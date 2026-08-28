#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.mcp_ua_matrix_canary.plist.
#
# Why this exists: mcp_ua_matrix_canary.py writes results to walker_health via
# DATABASE_URL and pings BetterStack via BETTERSTACK_MCP_UA_MATRIX_CANARY_URL.
# Without the env file sourced, pg_available() returns False and the canary
# silently skips — the exact failure mode the canary exists to prevent.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# set -a: auto-export every variable set until `set +a`. Defense against
# non-exported entries in the env file being invisible to exec'd Python
# subprocesses (see run_is_bot_canary.sh header comment for prior incident).
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/scripts/mcp_ua_matrix_canary.py"

exec "$PYTHON" "$SCRIPT"
