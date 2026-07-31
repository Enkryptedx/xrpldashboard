#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.bridge_signer_walker.plist.
#
# Sources ~/.config/xrpldashboard/env so DATABASE_URL is present in the
# walker process. Without it, db.pg_available() returns False, the
# walker early-exits with "Postgres not configured", and
# bridge_signer_history rows never get written. Mirrors the
# run_mpt_holders_refresh.sh pattern.
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

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/bridge_signer_walker.py"

exec "$PYTHON" "$SCRIPT"
