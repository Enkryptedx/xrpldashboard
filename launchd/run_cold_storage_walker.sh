#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.cold_storage_walker.plist.
#
# Fetches balances for the /cold-storage cohort via LAN rippled and
# upserts them into cold_storage_snapshot in Neon. The /cold-storage
# route reads from the table instead of making 21 live account_info
# RPCs per page render — kills ~214/hr walker_node_fallback rows.
#
# Sourcing ~/.config/xrpldashboard/env makes DATABASE_URL available
# so db.replace_cold_storage_snapshot persists to Neon, and makes
# XRPL_LOCAL_NODE point at Lenovo's LAN rippled endpoint. Same
# pattern as run_oracle_walker.sh / run_escrow_supply_walker.sh.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

set -a  # auto-export sourced vars — matches other walker wrappers
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/cold_storage_walker.py"

exec "$PYTHON" "$SCRIPT"
