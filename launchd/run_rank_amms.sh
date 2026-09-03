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
RANKED_PATH="/Users/charliebruce/xrpl_test/amm_ranked.json"

# Auto-reset when amm_ranked.json is > 20h old. rank_amms is checkpoint-
# persistent: once state.finished_at is set, subsequent runs no-op with
# "already finished / pass --reset to re-rank". Without periodic reset,
# the file staled to 3 days (2026-08-31 → 2026-09-03) and the daily
# signed-snapshot metrics amm_pools_count + amm_pools_total_tvl_usd
# started reflecting 3-day-old TVL. 20h keeps daily refresh comfortably
# inside the anchor-day freshness window.
ARGS=()
if [[ -f "$RANKED_PATH" ]]; then
  AGE_S=$(( $(date +%s) - $(stat -f %m "$RANKED_PATH") ))
  if (( AGE_S > 72000 )); then
    ARGS+=("--reset")
    echo "[$(date '+%F %T')] amm_ranked.json is ${AGE_S}s old (>20h) — passing --reset"
  fi
fi

exec "$PYTHON" "$SCRIPT" "${ARGS[@]}"
