#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.xrpl_stream_restart_rate_walker.plist.
#
# Hourly SSH to rippled-node (Lenovo) to count xrpl_stream restarts in
# rolling 24h + 7d windows. Writes walker_health row so the restart-rate
# is visible in Postgres instead of buried in xrpl_stream.log. Follows
# the log-analysis 2026-09-03 that found 1,218 watchdog-triggered
# restarts across 23 days that nobody was tracking.
#
# Sourcing ~/.config/xrpldashboard/env makes DATABASE_URL available so
# db.write_walker_health_* persist to Neon. Same env pattern as every
# other walker wrapper.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/xrpl_stream_restart_rate_walker.py"

exec "$PYTHON" "$SCRIPT"
