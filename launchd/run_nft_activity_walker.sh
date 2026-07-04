#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.nft_activity_walker.plist.
#
# Runs in activity mode every 300 seconds — forward-ingest NFT txs from
# cursor_ledger toward HEAD-3. Routes through xrpl_client.get_client()
# (local rippled primary, s1/s2 fallback). Writes to nft_activity and
# advances nft_walker_state.cursor_ledger on every successful batch.
# walker_health row updated every run so /walker_health can surface
# staleness. Backfill and rollup modes run on separate plists (D2/D3).
#
# Same pattern as run_escrow_walker.sh / run_credentials_walker.sh.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/nft_activity_walker.py"

exec "$PYTHON" "$SCRIPT" --mode activity
