#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.rank_amms.plist.
#
# Why this exists: rank_amms.py writes its progress to Postgres via
# db.write_heartbeat("amm_ranker"). Without DATABASE_URL in the environment,
# db.pg_available() returns False and the heartbeat silently lands in local
# SQLite only. Render reads from Neon and the /health Pool Tracker section
# freezes at the stalest known timestamp. Sourcing the env file here makes
# every scheduled run mirror its progress into Neon.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG — launchd will retry per StartInterval
fi

set -a  # auto-export sourced vars — 2026-07-31 BetterStack silent-skip fix
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/rank_amms.py"

exec "$PYTHON" "$SCRIPT"
