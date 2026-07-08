#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.coverage_register_walker.plist.
#
# Phase 1b history writer for the Coverage Register — reads Phase 0
# ledger_definitions + Phase 1a tx_type_seen/ledger_entry_type_seen +
# coverage_labels + walker_scope_declarations + walker_health, computes
# the three-way diff, and appends to coverage_register_history (writes
# when state differs OR >24h has elapsed).
#
# Own walker_health row (WALKER_NAME=coverage_register_walker) so this
# walker's own freshness is on its own line, not conflated with
# ledger_definitions_walker.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/coverage_register_walker.py"

exec "$PYTHON" "$SCRIPT"
