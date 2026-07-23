#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.is_bot_canary.plist.
#
# Why this exists: is_bot_canary.py compares column vs live-predicate counts
# and writes results to walker_health via DATABASE_URL. Without the env file
# sourced, pg_available() returns False and the canary silently skips — which
# is the exact failure mode the canary exists to prevent.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/scripts/is_bot_canary.py"

exec "$PYTHON" "$SCRIPT"
