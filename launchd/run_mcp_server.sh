#!/bin/bash
# Wrapper invoked by Claude Desktop (~/Library/Application Support/Claude/
# claude_desktop_config.json → mcpServers.xrpldashboard-mcp.command).
#
# Same env-sourcing pattern as every other walker wrapper in this dir:
# sources ~/.config/xrpldashboard/env so DATABASE_URL, XRPL_LOCAL_NODE,
# XRPL_NODE, and any other configured env vars are visible to the MCP
# server child process. Claude Desktop's launcher does NOT inherit the
# user shell env, so without this wrapper Postgres-backed tools return
# "DATABASE_URL not configured" and ledger-primitives tools silently fall
# back to public s1 (see 2026-08-02 Day 7 ship-gate demo trace).
#
# Exits non-zero if the env file is missing — Claude Desktop will surface
# "Server disconnected" rather than silently attach with a broken toolset.
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

# Claude Desktop's config sets MCP_TRANSPORT=stdio in the JSON env block;
# this wrapper preserves it. Explicit re-export for clarity.
export MCP_TRANSPORT="${MCP_TRANSPORT:-stdio}"

exec /Users/charliebruce/xrpl_test/venv/bin/python /Users/charliebruce/xrpl_test/mcp_server.py
