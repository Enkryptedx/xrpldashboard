#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.verify_toml.plist.
#
# Weekly first-party identity scanner. Discovers new XRPL accounts via
# xrp-ledger.toml and their on-chain Domain field; writes to
# named_accounts.json AND (via db.upsert_account_label) to Postgres
# account_labels.
#
# The wrapper is required because launchd doesn't inherit the login
# shell env. Without sourcing ~/.config/xrpldashboard/env, DATABASE_URL
# is unset — db._get_writer_conn() returns None — and both the
# walker_health writes AND the account_labels dual-writes silently
# no-op. Same latent bug as lending_snapshot (surfaced 2026-07-08).
# Named-accounts.json still writes fine because that's a local file
# path with no DB dep; the Postgres side was the silent half.
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
SCRIPT="/Users/charliebruce/xrpl_test/verify_toml_accounts.py"

exec "$PYTHON" "$SCRIPT"
