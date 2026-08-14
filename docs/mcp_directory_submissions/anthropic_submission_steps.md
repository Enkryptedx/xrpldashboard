# Anthropic Official MCP Registry — submission steps

> **2026-08-12 update:** Republish flow (v1.29.0 → v1.29.1) for the
> ref-tagged URL now lives in `EXPANSION_2026-08-13.md` §1. This file
> is the first-time-publish reference, retained for the history it
> encodes. Use the expansion doc for the republish.

Registry: https://registry.modelcontextprotocol.io  
Namespace we want: `com.xrpldashboard/*` (DNS-verified, not the free `io.github.enkryptedx/*` GitHub-verified path — the com. namespace matches the site's identity and is our moat).

## The manifest — already drafted
[`docs/mcp_directory_submissions/anthropic_server.json`](./anthropic_server.json). Points at the live public URL (dogfooded 2026-08-05 at commit `90c5450`).

## Steps for Charlie

### 1. Install the publisher CLI
```bash
brew install modelcontextprotocol/tap/mcp-publisher
# or (npm path — pick one)
npm install -g @modelcontextprotocol/publisher
```

### 2. Generate an Ed25519 keypair for DNS auth
```bash
# One-liner: prints private key (HEX) + public key (base64)
mcp-publisher keygen
# Output:
#   private_key: <64 hex chars>  ← save to ~/.config/mcp-publisher/xrpldashboard.key
#   public_key:  <base64 string>  ← goes in the DNS record below
```
Save the private key somewhere durable (1Password, encrypted disk). If it leaks, someone else can publish `com.xrpldashboard/*` — same class of risk as an ssh key.

### 3. Add the DNS TXT record

**On Cloudflare** (Newmediaconceptz account → `xrpldashboard.com` zone → DNS → Add record):

- **Type:** TXT
- **Name:** `@` (apex, `xrpldashboard.com`)
- **Content:** `v=MCPv1; k=ed25519; p=<PUBLIC_KEY_BASE64_FROM_STEP_2>`
- **TTL:** Auto (5min)
- **Proxy status:** DNS only (TXT records aren't proxied by CF anyway)

Verify from another shell:
```bash
dig +short TXT xrpldashboard.com | grep MCPv1
```

### 4. Authenticate + publish

```bash
cd ~/xrpl_test
mcp-publisher login dns \
    --domain=xrpldashboard.com \
    --private-key=$(cat ~/.config/mcp-publisher/xrpldashboard.key)

mcp-publisher publish docs/mcp_directory_submissions/anthropic_server.json
```

Expected output on success: registration accepted, listing appears at `https://registry.modelcontextprotocol.io/servers?namespace=com.xrpldashboard`.

### 5. Confirm the listing
```bash
curl -s https://registry.modelcontextprotocol.io/api/v0/servers?search=xrpldashboard | jq
```

## Wait / review model

The MCP Registry is a **self-published registry** (like npm) — there is no gated human review queue. Publish → listed. This is different from Smithery / glama.ai which do gate.

The exception: if the server URL fails a health probe from the registry side, it may be flagged. Ours is currently live + returning valid JSON-RPC 2.0 on `initialize` (verified 2026-08-05 at 07:15 EDT + again at 11:55 EDT via distribution-day dogfood). Expected outcome: publish succeeds first try.

## Reversal path

- `mcp-publisher unpublish com.xrpldashboard/xrpldashboard-mcp` removes the entry. Namespace remains ours (DNS record stays).
- To fully release the namespace: delete the DNS TXT record. After propagation, future auth attempts on the namespace fail.

## Post-publish

Add the registry-side listing URL to `_LLMS_TXT` §For agent authors and to `_AGENTS_JSON.discovery_backlinks` (add the field if it doesn't exist; sibling of the `mcp_servers` entry). This closes the loop so agents finding us via the registry see the same address as agents finding us via `agents.json`.
