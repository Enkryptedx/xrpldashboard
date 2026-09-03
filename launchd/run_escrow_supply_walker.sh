#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.escrow_supply_walker.plist.
#
# Sums EscrowCreate objects across the /cold-storage cohort (minus RLUSD
# issuer) via LAN rippled and upserts the aggregate into the singleton
# escrow_supply_snapshot row in Neon. The /cold-storage route reads
# from the table instead of making 19 paginated account_objects RPCs
# per page render — kills ~52/hr walker_node_fallback rows for a total
# that changes once a month.
#
# Sourcing ~/.config/xrpldashboard/env makes DATABASE_URL + XRPL_LOCAL_NODE
# available. Same pattern as run_cold_storage_walker.sh.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/escrow_supply_walker.py"

exec "$PYTHON" "$SCRIPT"
