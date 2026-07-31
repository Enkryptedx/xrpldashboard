#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.xrpl_stream.plist.
#
# Why this exists: keeps secrets (DATABASE_URL) out of the plist, which
# now contains zero credentials and is safe to track. The env file lives
# at ~/.config/xrpldashboard/env (chmod 600, outside the repo).
#
# caffeinate -i -s prevents idle/system sleep while python is running so
# the live data feed survives lid-close / desk-idle.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG — launchd will retry per ThrottleInterval
fi

set -a  # auto-export sourced vars — 2026-07-31 BetterStack silent-skip fix
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/xrpl_stream.py"

exec /usr/bin/caffeinate -i -s "$PYTHON" "$SCRIPT"
