#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.rlusd_refresher.plist.
#
# Replaces the per-gunicorn-worker lazy refresher in rlusd_live that died
# silently on every deploy/restart. Now the refresher is a single-shot
# script invoked by launchd every 5 minutes, and the /rlusd route reads
# from Postgres only.
#
# Sourcing ~/.config/xrpldashboard/env makes DATABASE_URL available so
# db.write_rlusd_state_cache persists to Neon (Render reads from PG).
# Same pattern as run_credentials_walker.sh / run_mpt_snapshot.sh.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/rlusd_refresher_walker.py"

exec "$PYTHON" "$SCRIPT"
