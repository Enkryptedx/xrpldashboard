# MCP directory expansion — 3 → 9 (staged 2026-08-12)

The three live directories from 2026-08-05 (Anthropic MCP Registry, Smithery,
glama.ai) get republished with per-directory `?ref=<slug>` URLs; six new
directories get first-time submissions. Ref-tag capture at the MCP session
layer + /connect/<slug> redirect fallback both shipped 2026-08-12 (see
`mcp_session_rate_limit.py` §Ref-tag capture and `app.py` §
`connect_redirect`). October's revenue read reads one grep:

    SELECT COUNT(DISTINCT session_key)
    FROM walker_health
    WHERE walker_name = 'mcp_session_start'
      AND message LIKE '%ref=<slug>%'
      AND last_success_at > NOW() - INTERVAL '<window>';

Belt-and-suspenders `/connect/<slug>` also stamps a `mcp_connect_redirect`
walker_health row per click, so an agent that fetches the URL but never
calls a tool is still counted at the door.

## Execution log — 2026-08-14

Census-to-execution reality check. Directory landscape moved between planning (2026-08-12) and fire (2026-08-14).

| Directory | Verdict | Detail |
|-----------|---------|--------|
| Anthropic MCP Registry | **SHIPPED** | v1.29.1 published via `mcp-publisher`, `?ref=anthropic` live. Canonical registry done. |
| Smithery | **EXISTING-LISTING-LIVE / EDIT-BLOCKED** | Listing confirmed live (`smithery.ai/server/xrpldashboard`, "Read-only XRP Ledger MCP tools with proof-annotation envelopes and signed daily snapshots"). `mcp-publisher publish` 404'd post Smithery→Arcade.dev acquisition. Endpoint URL edit path unknown under new ownership. Backlog: find Arcade account edit path, update URL to `?ref=smithery`. |
| Glama | **FRESH-SUBMIT-PENDING** | 2026-08-05 submission did not land (0 results in 72,373-server index). "Add Server" button present and free. Quick path tonight or tomorrow. |
| MCP.so | **PAYWALLED-DROPPED** | $39 fee to list. Not in plan. Drop without regret — paid listings carry less discovery signal anyway. |
| AllMCPs | **BROKEN-RETRY-LATER** | Cloudflare Turnstile failure + "Unexpected error" on form submit. Site broken as of tonight. Retry when fixed. |
| MCP Market | **PAYWALLED-DROPPED** | $29 fee to list. Not in plan. Dropped. |
| PulseMCP | **PAUSED-WATCH** | New submissions paused "until mid-August" per live banner (2026-08-14). May open any day. Daily-poll trigger unchanged. |

**Lesson filed:** Directory landscape moves fast. Between census (2026-08-12) and execution (2026-08-14): two paywalls appeared or were confirmed, one site broke, one platform completed an acquisition that broke the publisher flow, and one Aug-5 submission silently failed to land. Re-verify each target before the browser session, not before the planning session.

**Net result:** Anthropic (canonical, highest value) SHIPPED. Three dropped cleanly. Two pending quick actions (Glama fresh submit, Smithery edit path). One paused watch (PulseMCP). Ref-counter live in production from all three, counting from first arrival.

---

## Slug table (canonical)

| # | Directory                     | Slug        | Live-primary URL                                              | Fallback URL                              |
|---|-------------------------------|-------------|---------------------------------------------------------------|-------------------------------------------|
| 1 | Anthropic MCP Registry        | `anthropic` | `https://mcp.xrpldashboard.com/mcp?ref=anthropic`             | `https://xrpldashboard.com/connect/anthropic` |
| 2 | Smithery                      | `smithery`  | `https://mcp.xrpldashboard.com/mcp?ref=smithery`              | `https://xrpldashboard.com/connect/smithery`  |
| 3 | glama.ai                      | `glama`     | `https://mcp.xrpldashboard.com/mcp?ref=glama`                 | `https://xrpldashboard.com/connect/glama`     |
| 4 | MCP.so                        | `mcpso`     | `https://mcp.xrpldashboard.com/mcp?ref=mcpso`                 | `https://xrpldashboard.com/connect/mcpso`     |
| 5 | AllMCPs                       | `allmcps`   | `https://mcp.xrpldashboard.com/mcp?ref=allmcps`               | `https://xrpldashboard.com/connect/allmcps`   |
| 6 | MCP Market                    | `mcpmarket` | `https://mcp.xrpldashboard.com/mcp?ref=mcpmarket`             | `https://xrpldashboard.com/connect/mcpmarket` |
| — | PulseMCP (waitlist watch)     | `pulse`     | `https://mcp.xrpldashboard.com/mcp?ref=pulse`                 | `https://xrpldashboard.com/connect/pulse`     |
| — | Cursor MCP directory (queued) | `cursor`    | `https://mcp.xrpldashboard.com/mcp?ref=cursor`                | `https://xrpldashboard.com/connect/cursor`    |
| — | OpenAI directory (research)   | `openai`    | `https://mcp.xrpldashboard.com/mcp?ref=openai`                | `https://xrpldashboard.com/connect/openai`    |

Reserved slugs (not directories):
- `direct` — URL typed / shared in DMs (organic baseline)
- `readme` — click from repo README badge

Handshake verdict 2026-08-12: mcp-remote@latest preserves the full URL
including query params (verified by running `npx mcp-remote@latest
'https://mcp.xrpldashboard.com/mcp?ref=test-handshake-mcpremote'` — log
line `Connecting to remote server:` echoed the ref). **Primary path
works** for Cursor + Claude Desktop; /connect/<slug> is defense in depth.

Copy discipline for every submission:
- Sovereignty pills exact (`local_rippled` / `local_rippled_stream_capture`
  / `neon_postgres` / `signed_snapshot_walker` — see
  `AGENT_TIER_MCP_INVENTORY` in `app.py`).
- Beta window `2026-09` verbatim.
- Rate limit `600 tool calls/hour/session, enforced (HTTP 429 with
  Retry-After)` verbatim.
- Tool count **15** (the six new tools that would take us to 20 are
  post-Batch-B — do NOT preview them in submissions).
- Absolutely no `x402` / `RLUSD` / `USDC` / `paid tier` / `402` copy.
  Free tier is the whole face until October's flip decision.

---

## 1. Anthropic MCP Registry — REPUBLISH (v1.29.0 → v1.29.1)

Delta: `remotes[0].url` bumped to `?ref=anthropic`. Registry rejects
same-version republish; version bumped to 1.29.1 in
`anthropic_server.json`.

Preflight (dry-run — do not publish yet):

    cd ~/xrpldashboard
    mcp-publisher --version          # confirm CLI still on 1.8.x
    dig +short TXT xrpldashboard.com | grep 'v=MCPv1'
                                     # confirm DNS TXT still resolves
    jq . docs/mcp_directory_submissions/anthropic_server.json
                                     # confirm JSON parses + version=1.29.1

Publish:

    cd ~/xrpldashboard
    mcp-publisher login dns          # re-auth via DNS TXT (interactive)
    mcp-publisher publish -f docs/mcp_directory_submissions/anthropic_server.json

Post-publish verify:

    curl -s 'https://registry.modelcontextprotocol.io/v0/servers?search=xrpldashboard' \
      | jq '.servers[] | select(.name == "com.xrpldashboard/xrpldashboard-mcp") | {version, remotes}'

Expect: latest entry shows `version: "1.29.1"` and the ref-tagged URL.

**Reversal path:** re-publish v1.29.2 with the pre-ref URL if the flip
needs unwinding. Old versions remain listed but not `isLatest`.

---

## 2. Smithery — CLAIM VERIFIED BADGE

Listing URL already live: `https://smithery.ai/servers/xrpldashboard/xrpldashboard`.

Steps (Charlie's browser, ~5 min):
1. Sign in at smithery.ai with the account that owns the `xrpldashboard`
   namespace.
2. Server Settings → **Verification checklist**. Two boxes:
   - **Domain ownership** — Smithery requests a DNS TXT record. Same
     pattern as Anthropic (apex TXT), so it can co-exist with the
     `v=MCPv1;` record already published for Anthropic. Add the second
     TXT record via Cloudflare DNS-only.
   - **Working endpoint** — Smithery re-scans the server; the new
     ref-tagged URL should surface in the checklist as a success.
3. Update the Smithery URL in the listing to
   `https://mcp.xrpldashboard.com/mcp?ref=smithery` (Server Settings →
   Endpoint URL).
4. Save → wait for the "Verified" badge to appear on the listing card.

**Reversal path:** delete the DNS TXT record for the Smithery
verification (co-existing with Anthropic's) if we ever need to
un-verify. The listing stays live either way.

---

## 3. glama.ai — PROMOTE TO OFFICIAL TIER + REF URL

Listing URL: check `https://glama.ai/mcp/servers` after
2026-08-05 submission processed.

Steps (Charlie's browser, ~5 min):
1. Sign in at glama.ai with the GitHub account listed in
   `glama.json.maintainers[0]` (`Enkryptedx`).
2. Locate the `xrpldashboard-mcp` listing → Edit.
3. Server URL → `https://mcp.xrpldashboard.com/mcp?ref=glama`.
4. Tier request → "Official" (glama's terminology may vary — look
   for the checkbox that confirms maintainer ownership + valid docs).
5. README badge — copy the markdown snippet glama offers:

        [![glama MCP server](https://glama.ai/mcp/servers/xrpldashboard-mcp/badge)](https://glama.ai/mcp/servers/xrpldashboard-mcp)

   Add to README.md at the top of the Agent-tier section (see
   README badge diff — commit that lands with this expansion).
6. Save → the Official-tier check may be async (email review).

**Reversal path:** revert the README badge commit; remove the badge
markdown from glama's edit form.

---

## 4. MCP.so — FRESH SUBMISSION

Submit at: `https://mcp.so/submit` (or the "Submit" button on
`https://mcp.so`).

Form fields (Charlie's browser, ~10 min):
- **Name:** `xrpldashboard MCP`
- **Description** (one line, ≤120 chars):
  `Read-only XRP Ledger MCP tools with proof-annotation envelopes and signed daily snapshots. Public beta 2026-09.`
- **URL (server):** `https://mcp.xrpldashboard.com/mcp?ref=mcpso`
- **Documentation URL:** `https://xrpldashboard.com/connect#connect-in-60-seconds`
- **Repository:** `https://github.com/Enkryptedx/xrpldashboard`
- **Category:** blockchain / finance (whichever MCP.so labels their
  crypto vertical).
- **Tags:** `xrpl`, `xrp`, `mcp`, `signed-snapshots`, `read-only`,
  `on-chain-audit`, `rlusd`.
- **License:** as declared in repo LICENSE file.
- **Tool count:** 15.

MCP.so auto-publishes on save (no gated review). Post-save, verify
listing appears in a search for "xrpl".

**Reversal path:** MCP.so submission form typically has a "Delete
listing" button visible to the submitter; use it to unpublish.

---

## 5. AllMCPs.com — FRESH SUBMISSION + README BADGE

Submit at: `https://allmcps.com/submit` (verify current URL on the
homepage — form location can move).

Form fields (Charlie's browser, ~10 min):
- **Server name:** `xrpldashboard`
- **Description:** same one-liner as MCP.so (≤120 chars).
- **Server URL:** `https://mcp.xrpldashboard.com/mcp?ref=allmcps`
- **Docs:** `https://xrpldashboard.com/connect#connect-in-60-seconds`
- **Repo:** `https://github.com/Enkryptedx/xrpldashboard`
- **Category:** blockchain / crypto.
- **License:** from repo.
- **Tool count:** 15.

README badge — if AllMCPs offers one:

    [![AllMCPs listed](https://allmcps.com/badge/xrpldashboard.svg)](https://allmcps.com/servers/xrpldashboard)

Add alongside the glama badge in README's Agent-tier section.

**Reversal path:** revert README badge commit; use AllMCPs submitter
UI to unpublish.

---

## 6. MCP Market — COMMUNITY SUBMISSION

Submit at: `https://mcpmarket.com/submit` (verify URL live).

Form fields (Charlie's browser, ~10 min):
- **Server name:** `xrpldashboard MCP`
- **Description:** same one-liner (≤120 chars).
- **Endpoint URL:** `https://mcp.xrpldashboard.com/mcp?ref=mcpmarket`
- **Documentation:** `https://xrpldashboard.com/connect#connect-in-60-seconds`
- **Repository:** `https://github.com/Enkryptedx/xrpldashboard`
- **Category:** blockchain.
- **Free / paid:** **Free** (nothing else — do not tick any paid tier).
- **Tool count:** 15.

MCP Market may have community moderation queue — listing lands within
a day.

**Reversal path:** submitter account has a delist button; email their
support if not visible.

---

## Watch items (queued, not fired)

- **PulseMCP** — 22K server board, biggest single listing but pause on
  new submissions as of 2026-08-12. **Daily poll:** open
  `https://pulsemcp.com/submit` (or the "Submit MCP Server" link on the
  homepage). Moment the pause lifts, run the same MCP.so-shape submission
  with `?ref=pulse`.
- **OpenAI directory** — research task for a fresh session. Confirm
  current submission shape and whether a paid-tier posture must be
  defined first (OpenAI's directory may score against monetization).
- **Google MCP registry** — research task for a fresh session; format
  and gating unknown as of 2026-08-12.

---

## Post-submission verification (per directory)

For each directory, after the listing renders:
1. Confirm name, description, tool count (15), and URL shown match
   the submission.
2. Confirm the URL rendered contains `?ref=<slug>` (fallback:
   `/connect/<slug>`).
3. Live-fire connect from Cursor or Claude Desktop using the
   directory-rendered URL — one `get_ledger_stats` call should
   result in one row in `walker_health` with
   `walker_name='mcp_session_start'` and message containing the
   expected ref. This is the end-to-end proof the counter works.
4. Screenshot the listing card + walker_health row for the
   expansion memo.

## Measurement contract (October's read)

Per-directory arrival count over N days:

    SELECT
      substring(message from 'ref=([a-z0-9_-]+)') AS ref,
      COUNT(DISTINCT substring(message from 'session=([^ ]+)')) AS sessions
    FROM walker_health
    WHERE walker_name = 'mcp_session_start'
      AND last_success_at > NOW() - INTERVAL '30 days'
    GROUP BY ref
    ORDER BY sessions DESC;

Redirect-time clicks (arrival-at-door, not session-completed):

    SELECT
      substring(message from 'ref=([a-z0-9_-]+)') AS ref,
      COUNT(*) AS clicks
    FROM walker_health
    WHERE walker_name = 'mcp_connect_redirect'
      AND last_success_at > NOW() - INTERVAL '30 days'
    GROUP BY ref
    ORDER BY clicks DESC;

Ratio of `sessions / clicks` per ref is the funnel quality signal —
directories with high click-through but low session-completion may be
routing casual scrapers, not tool-callers. This informs which
directories deserve further investment vs. deprioritize.
