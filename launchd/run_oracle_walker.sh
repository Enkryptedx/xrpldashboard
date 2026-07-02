#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.oracle_walker.plist.
#
# Feeds the /price-data explainer. Runs every 30 minutes. Each pass
# iterates the tracked oracle cohort (category=oracle in named_accounts.json
# — DIA today) via account_objects(type=oracle), decodes hex Provider/URI
# and hex-encoded AssetPrice, then replaces the oracles_snapshot table in
# Neon. Reads route through xrpl_client.get_client() (local rippled
# primary, s1/s2 fallback).
#
# Sourcing ~/.config/xrpldashboard/env makes DATABASE_URL available so
# db.replace_oracles_snapshot persists to Neon. Same pattern as
# run_escrow_walker.sh / run_credentials_walker.sh.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/oracle_walker.py"

exec "$PYTHON" "$SCRIPT"
