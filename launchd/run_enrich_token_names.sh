#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.enrich_token_names.plist.
#
# Weekly token-name enrichment: for each ranked AMM, pull the issuer's
# xrp-ledger.toml [[CURRENCIES]] / [[TOKENS]] block and populate
# token_names in Postgres so /pools + /token render human-readable
# labels instead of raw currency codes.
#
# The wrapper is required because launchd doesn't inherit the login
# shell env. Without sourcing ~/.config/xrpldashboard/env, DATABASE_URL
# is unset — db._get_writer_conn() returns None — and both the
# walker_health writes and the token_names writes silently no-op.
# Same latent bug as lending_snapshot (surfaced 2026-07-08). No
# walker_health row for enrich_token_names existed at all pre-fix,
# which is how it was noticed on the sweep.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/enrich_token_names.py"

exec "$PYTHON" "$SCRIPT"
