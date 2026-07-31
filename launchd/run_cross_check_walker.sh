#!/bin/bash
# Wrapper for com.charliebruce.xrpldashboard.cross_check_walker.plist.
#
# Truth Audit Layer 3 (External Legitimacy) cross-check walker.
# Compares the values the site computes against independent public sources.
# Disagreements write status='disagree' to cross_check_results (append-only);
# walker_health.ok reflects only whether the walker itself ran cleanly.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

set -a  # auto-export sourced vars — 2026-07-31 BetterStack silent-skip fix
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
export ETH_RPC  # make available to Python subprocess (env file sets but doesn't export)

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/cross_check_walker.py"

exec "$PYTHON" "$SCRIPT"
