#!/bin/bash
# Wrapper invoked by
#   com.charliebruce.xrpldashboard.token_issuer_flags_walker.plist.
#
# Fetches AccountRoot flags for every DISTINCT issuer in token_volume
# via LAN rippled and upserts them into token_issuer_flags_snapshot in
# Neon. The /token/<cur>/<iss> route reads from the table instead of
# firing one live AccountInfo RPC per page render — kills ~3/day
# walker_node_fallback rows for walker_name=token_page.
#
# Sourcing ~/.config/xrpldashboard/env makes DATABASE_URL available so
# db.replace_token_issuer_flags_snapshot persists to Neon, and makes
# XRPL_LOCAL_NODE point at Lenovo's LAN rippled endpoint. Same pattern
# as run_cold_storage_walker.sh / run_escrow_supply_walker.sh.
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
SCRIPT="/Users/charliebruce/xrpl_test/token_issuer_flags_walker.py"

exec "$PYTHON" "$SCRIPT"
