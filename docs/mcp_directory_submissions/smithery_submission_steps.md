# Smithery — submission steps

Registry: https://smithery.ai  
Submission URL: https://smithery.ai/new (URL-based flow — no repo push, no publish CLI)

## What Smithery does with the submission

Smithery accepts a public MCP endpoint URL, then a Cloudflare Worker with UA `SmitheryBot/1.0 (+https://smithery.ai)` scans the server. The scan performs `initialize` → `tools/list` and enumerates each tool schema for their directory page. Not human-gated; the crawl decides.

## Why the pre-flight limiter change matters here

The Smithery crawler burns tool calls to enumerate tools. Our public MCP endpoint enforces `600 tool calls / hour / session` and returns HTTP 429 on breach. If the scan tripped the limit mid-crawl, the listing would ship with a partial tool list — undercutting the whole point of "15 read-only tools" as our discovery pitch.

Shipped at commit `90c5450` (main): `should_bypass_rate_limit()` in `mcp_session_rate_limit.py` matches `SmitheryBot` in the User-Agent (case-insensitive substring), stamps a `walker_health` row on match, and skips the limiter for that call. Default allowlist is `("SmitheryBot",)`; extend via `MCP_SESSION_LIMIT_UA_ALLOWLIST` env (never replaces default). Test coverage: `tests/test_mcp_session_rate_limit.py` (20 pass).

**Daemon status:** Lenovo (`192.168.40.95`) has the code at `/home/charlie/xrpldashboard`, but `mcp-server.service` needs a manual `sudo systemctl restart mcp-server` to load it. Do this BEFORE submitting to Smithery, otherwise the crawler hits the old code and the allowlist doesn't apply.

## Steps for Charlie

### 1. Restart the daemon on Lenovo

```bash
ssh charlie@192.168.40.95
sudo systemctl restart mcp-server
systemctl status mcp-server --no-pager | head -20
# expect: Active: active (running), NRestarts=0 since restart
```

Then verify the bypass path is live by hitting the public URL from your Mac with the UA:

```bash
curl -sS -X POST https://mcp.xrpldashboard.com/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'User-Agent: SmitheryBot/1.0 (+https://smithery.ai)' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","clientInfo":{"name":"smithery-dryrun","version":"0"},"capabilities":{}}}' | head -5
# expect: HTTP 200 with initialize result
```

Then check for the bypass stamp on Lenovo:

```bash
psql "$DATABASE_URL" -c "SELECT walker_name, ok, last_message, last_run_at FROM walker_health WHERE walker_name = 'mcp_session_rate_limit_bypass' ORDER BY last_run_at DESC LIMIT 3;"
# expect: at least one row with matched_ua=smitherybot in last_message
```

If no row appears after the curl, the daemon is running old code — restart again.

### 2. Submit at smithery.ai/new

1. Go to https://smithery.ai/new
2. Fill in:
   - **Server URL:** `https://mcp.xrpldashboard.com/mcp`
   - **Name:** `xrpldashboard`
   - **Description:** Copy from `anthropic_server.json` `description` field for consistency
   - **Transport:** streamable-http
   - **Auth:** None
3. Submit. Smithery queues the SmitheryBot scan.

### 3. Watch the scan

The scan usually completes within minutes. Monitor:

```bash
# On Lenovo, tail the mcp-server logs
sudo journalctl -u mcp-server -f | grep -i smithery
```

You should see the initialize + tools/list round-trip logged. If the walker_health bypass stamp count jumps by 15+ (one per tool enumerated), the allowlist held.

### 4. Confirm the listing

Once the scan finishes, the listing appears at `https://smithery.ai/server/xrpldashboard` (or similar slug). Check that all 15 tools appear with their schemas.

## Post-submission

- Add the Smithery listing URL to `_LLMS_TXT` §For agent authors and to `_AGENTS_JSON.discovery_backlinks` (same field as the Anthropic registry backlink).
- Report the SmitheryBot allowlist behavior in the distribution-day report: how many bypass stamps landed, whether any 429 slipped through.

## Reversal path

Smithery listings are removable via the account dashboard. Delisting doesn't affect the endpoint — it just removes the discovery surface.

## Known caveats

- Smithery's exact scan cadence and re-scan interval are not publicly documented. Assume they may re-scan periodically; the UA allowlist covers repeat visits.
- If Smithery changes their UA string, the allowlist misses. `MCP_SESSION_LIMIT_UA_ALLOWLIST` env is the escape hatch — extend it without a redeploy of the code.
