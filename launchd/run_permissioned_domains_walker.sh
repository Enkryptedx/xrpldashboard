#!/bin/bash
# Wrapper invoked by com.charliebruce.xrpldashboard.permissioned_domains_walker.plist.
#
# Single-shot launchd-triggered runner for the XLS-80 PermissionedDomains
# walker. Mirrors run_credentials_walker.sh.
#
# Sourcing ~/.config/xrpldashboard/env makes DATABASE_URL available so the
# walker's db.write_permissioned_domains() persists to Neon. Without it
# every helper in db.py silently no-ops and the walker runs to completion
# but writes nothing — defeating the audit-trail purpose of the
# permissioned_domain_walker_runs table.
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
SCRIPT="/Users/charliebruce/xrpl_test/permissioned_domains_walker.py"

exec "$PYTHON" "$SCRIPT"
