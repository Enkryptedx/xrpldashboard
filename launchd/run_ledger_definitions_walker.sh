#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.ledger_definitions_walker.plist.
#
# Phase 0 of Total Ledger Awareness: pulls server_definitions from the
# LOCAL rippled and refreshes the ledger_definitions singleton in Neon.
# Local-only by design — a public node returns ITS build's vocabulary,
# not ours. If localhost:5005 is down we degrade to STALE via
# walker_health, never fall back to s1/s2 for this call.
#
# Sourcing ~/.config/xrpldashboard/env makes DATABASE_URL available so
# db.write_ledger_definitions persists to Neon.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/ledger_definitions_walker.py"

exec "$PYTHON" "$SCRIPT"
