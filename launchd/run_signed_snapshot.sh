#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.signed_snapshot.plist.
#
# Sources ~/.config/xrpldashboard/env so:
#   - SIGNING_KEY_PASSPHRASE is in env for signed_snapshot.py to decrypt
#     the Ed25519 private key (the encrypted PEM lives in the same dir).
#   - DATABASE_URL is available if a future schema version reads metrics
#     from Postgres rather than disk files.
#
# Exits non-zero if env or key file is missing — launchd will retry per
# the plist's schedule rather than silently writing an unsigned snapshot.
set -euo pipefail

ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[$(date '+%F %T')] ERROR: env file missing or unreadable: $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

if [[ -z "${SIGNING_KEY_PASSPHRASE:-}" ]]; then
  echo "[$(date '+%F %T')] ERROR: SIGNING_KEY_PASSPHRASE missing from env" >&2
  exit 78
fi

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
SCRIPT="/Users/charliebruce/xrpl_test/signed_snapshot.py"

exec "$PYTHON" "$SCRIPT"
