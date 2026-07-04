#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.nft_activity_backfill.plist.
#
# Runs the NFT walker in backfill mode every 900 seconds. Walks
# nft_walker_state.backfill_ledger DOWN toward backfill_target (2026-04-01
# cutoff, ledger 103252853). Uses public Clio directly because local
# rippled ledger_history is only ~10k. See nft_activity_walker.py for
# BATCH/runtime budget constants and dedup semantics.
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

exec "$PYTHON" "$SCRIPT" --mode backfill
