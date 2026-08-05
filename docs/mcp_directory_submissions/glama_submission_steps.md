# glama.ai — submission steps

Registry: https://glama.ai/mcp/servers  
Submission URL: https://glama.ai/mcp/servers (their "Add MCP Server" flow)

## What glama.ai does with the submission

glama.ai is a curated MCP directory. Submissions are typically gated by a lightweight review: they read `glama.json` from the repo root to identify maintainers, then link the listing to the GitHub repo. Unlike Smithery, glama does not (as of 2026-08) run a live scanner against the endpoint — the listing reflects what the maintainer states plus what the repo advertises.

## The manifest — already committed

[`glama.json`](../../glama.json) at repo root:

```json
{
  "$schema": "https://glama.ai/mcp/schemas/server.json",
  "maintainers": ["Enkryptedx"]
}
```

`Enkryptedx` is Charlie's GitHub handle. If a co-maintainer joins later, extend the array.

## The README connect snippet — already added

`README.md` has an "Agent tier (MCP)" section between "Quick links" and "## Pages" that carries the dogfooded config JSON, the /connect link, the beta window, the session rate limit, and the discovery manifest URLs. glama.ai reads the README when generating the listing preview, so this section is the source of truth they'll see.

## Steps for Charlie

### 1. Confirm the repo is public and up to date

```bash
gh repo view Enkryptedx/xrpldashboard --json visibility,pushedAt
# expect: visibility=PUBLIC, pushedAt within last few hours
```

If pushedAt is stale, `git push origin main` before submitting so glama sees the latest README + glama.json.

### 2. Submit at glama.ai/mcp/servers

1. Go to https://glama.ai/mcp/servers
2. Click "Add MCP Server" (or similar CTA)
3. Provide the GitHub repo URL: `https://github.com/Enkryptedx/xrpldashboard`
4. glama.ai reads `glama.json` and README, extracts the endpoint URL from the README's config snippet, and creates a draft listing
5. If they ask for additional metadata, fill from `anthropic_server.json`:
   - Description: same text as `anthropic_server.json` `description`
   - Endpoint: `https://mcp.xrpldashboard.com/mcp`
   - Transport: streamable-http
   - Tool count: 15
   - Auth: none

### 3. Watch for review

glama's review is not real-time. Expected outcome: listing appears within days, not hours. If they email a clarification question, the answers are all in `docs/AGENT_TIER_DESIGN.md` or the README.

### 4. Confirm the listing

Once listed, the URL will be similar to `https://glama.ai/mcp/servers/xrpldashboard` (or hash-based). Check that:

- Description matches ours (not an auto-summary that drifts from truth)
- Endpoint URL is exactly `https://mcp.xrpldashboard.com/mcp`
- Tool count reads 15
- Repo link points to `github.com/Enkryptedx/xrpldashboard`

## Post-submission

- Add the glama listing URL to `_LLMS_TXT` §For agent authors and to `_AGENTS_JSON.discovery_backlinks`. Same field as the Anthropic + Smithery backlinks.
- If glama's description drifts from ours, request a correction; the description field is where trust starts.

## Reversal path

Contact glama via their site to request delisting. The glama.json file staying in the repo is harmless if delisted; it just becomes a no-op pointer.

## Known caveats

- glama.ai may re-crawl README periodically. Keep the "Agent tier (MCP)" section stable — moving it or renaming headers may break their extraction.
- Unlike Smithery, glama doesn't hit the endpoint with a documented UA, so no allowlist is needed here.
