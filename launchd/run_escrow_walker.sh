#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.escrow_walker.plist.
#
# Feeds the /cold-storage per-escrow browser + upcoming-releases calendar.
# Runs every 30 minutes. Each pass iterates the Ripple monthly-release
# cohort in named_accounts.json via account_objects(type=escrow), then
# replaces the escrows_snapshot table in Neon. Reads route through
# xrpl_client.get_client() (local rippled primary, s1/s2 fallback).
#
# Sourcing ~/.config/xrpldashboard/env makes DATABASE_URL available so
# db.replace_escrows_snapshot persists to Neon. Same pattern as
# run_rlusd_refresher.sh / run_credentials_walker.sh.
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
SCRIPT="/Users/charliebruce/xrpl_test/escrow_walker.py"

exec "$PYTHON" "$SCRIPT"
