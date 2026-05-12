#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.amm_tvl_recorder.plist.
#
# Why this exists: amm_tvl_recorder.py writes per-pool TVL rows to Postgres
# via db.write_amm_tvl_snapshot. Without DATABASE_URL in the environment,
# db.pg_available() returns False and the worker silently no-ops — we'd
# accumulate zero history while the launchd log looked healthy. Sourcing
# the env file here makes every scheduled run actually persist.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG — launchd will retry per StartInterval
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/amm_tvl_recorder.py"

exec "$PYTHON" "$SCRIPT"
