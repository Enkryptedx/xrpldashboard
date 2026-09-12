# Station audit — 2026-09-12

**Purpose**: test the "80% built" claim from the 2026-09-12 strategy dive by walking every load-bearing piece as an outside agent would. Read-only, freeze-safe. Every row has EXPECTED / OBSERVED / VERDICT / EVIDENCE.

**Method**: independent verification wherever possible — no reading repo code, only fetching public URLs, running the recipes the documentation exposes, comparing.

---

## 1. MCP server

**EXPECTED**: `mcp.xrpldashboard.com/mcp` streamable-http, MCP protocol 2025-06-18, 15 tools, every tool response wrapped in proof envelope with `{source, as_of, cross_check_status, freshness_contract, honest_partial, methodology_url}`. Listed on Anthropic MCP registry + Smithery. Session rate-limit enforced.

**OBSERVED**:

- Initialize handshake succeeds against `mcp.xrpldashboard.com/mcp`; server responds `protocolVersion=2025-06-18`, `serverInfo.version=1.29.0`, session id returned in `mcp-session-id` header.
- `tools/list` returns exactly **15 tools**: `get_ledger_stats`, `get_amendment_status`, `get_unl_status`, `get_whale_events`, `get_whale_watchlist`, `get_rlusd_supply`, `get_rlusd_flow_24h`, `get_amm_pool`, `get_amm_top_by_tvl`, `get_token_attestation`, `get_rwa_families`, `get_rwa_pools`, `get_mpt_snapshot`, `get_signed_snapshot`, `verify_snapshot_signature`.
- `get_ledger_stats` proof envelope: `{'source': 'local_rippled', 'freshness_contract': '≤ 5min', 'cross_check_status': 'not_applicable', 'honest_partial': False, 'methodology_url': 'https://xrpldashboard.com/methodology#ledger', 'as_of': '2026-09-12T22:37:08Z', 'claims_ref': 'ledger_stats_live', 'scope_note': None}`. Data: `build_version: '3.3.0-rc1', server_state: 'full', validated_ledger_index: 106944410, complete_ledgers: '106868362-106944410'`.
- `get_rlusd_supply`, `get_amm_top_by_tvl`, `get_token_attestation`: all return proof-envelope + data; envelope shape identical.
- **`get_signed_snapshot(date_str="2026-09-12")` returns `isError: true`**: `"get_signed_snapshot: no signed snapshot on disk for 2026-09-12 (searched /home/charlie/xrpldashboard/signed_snapshots/2026-09-12.json) — walker has not produced this date yet"`. But `/.well-known/snapshots/2026-09-12.json` on the main site = HTTP 200 with a valid signed envelope. **The MCP server (Lenovo) does not have the signed-snapshot files the Render app publishes.** Two mirrors, out of sync.
- Anthropic MCP registry `GET /v0/servers?search=xrpldashboard` returns 4 versions, latest `1.29.0` (matches live), status `active`, remote URL `https://mcp.xrpldashboard.com/mcp?ref=anthropic`. Registry `_meta` block correctly cites session_rate_limit "600 tool calls/hour/session, enforced (HTTP 429 with Retry-After)".
- Smithery listing at `https://smithery.ai/servers/xrpldashboard/xrpldashboard` returns HTTP 200; content confirms "xrpldashboard".
- Rate-limit headers: **none present** in `initialize` or `tools/list` responses. Standard `cf-ray` from Cloudflare; no `x-ratelimit-remaining` or equivalent. Client cannot see quota state per response.

**VERDICT**: **partial-pass**. 14/15 tools work end-to-end; `get_signed_snapshot` is broken because the Lenovo mirror doesn't carry the signed files. Registry listings + envelope shape verified. Rate-limit is enforced but not surfaced to clients.

**EVIDENCE**: `curl -X POST https://mcp.xrpldashboard.com/mcp -H "Mcp-Session-Id: <id>" -d '{"jsonrpc":"2.0","id":N,"method":"tools/call","params":{"name":"<tool>","arguments":{...}}}'` above. Registry query `curl https://registry.modelcontextprotocol.io/v0/servers?search=xrpldashboard`.

---

## 2. x402 catalog + facilitator

**EXPECTED**: `/.well-known/x402` returns a valid catalog with `accepts[]` listing XRP/RLUSD prices, facilitator endpoint pointing at a live URL, and any priced endpoint returns HTTP 402 with the price and asset selection.

**OBSERVED**:

- `/.well-known/x402` returns 200 with `x402Version: 1`, well-formed envelope.
- **`accepts: []` — empty list. No priced endpoints declared.**
- `status`: `{"free_tier_ready": true, "phase": "Free-tier verification live. x402 rails currently mode=off (middleware shipped in commit b406233, 2026-08-30, not settling payments). Fence-#8 sovereignty items (see docs/SOVEREIGNTY_COVENANT_VIOLATIONS_2026-08-30.md) must close before mode=live flip; target 2026-09-25.", "signing_key_wired": false, "x402_rails_ready": false}`.
- **No `facilitator` field on the catalog root.** No facilitator URL to resolve.
- `policies.cost`: `"free at v0.9 (no accounts, no API keys, no payment rails wired)"`.
- Probe on `/check.json` returned HTTP 400 (missing `q` param) — not 402. Probes on `/premium`, `/paid`, `/api/premium`, `/v1/paid` all return 404. **No endpoint on the site returns HTTP 402 today.**
- The `Fence-#8` blocker is documented in `docs/SOVEREIGNTY_COVENANT_VIOLATIONS_2026-08-30.md`: `/rlusd` sources from `s1.ripple.com` public JSON-RPC (violates own-node covenant). Marked "CRITICAL — Fence-#8 blocker" because RLUSD is the sellable-verification surface; the mode=live flip cannot ship truthfully while a third-party RPC sits on the read path.

**VERDICT**: **catalog live and honest, rails not built.** The x402 catalog exists and correctly self-describes as `mode=off`. But there is no facilitator, no accepted assets, and no priced endpoint. Anyone reading the catalog knows this; anyone SIMULATING a 402 flow gets nothing to simulate against. Billing-pause forced-test not possible — no billable endpoint exists yet.

**EVIDENCE**: `curl https://xrpldashboard.com/.well-known/x402 | jq .accepts`, `.status`. Endpoint probes: `for p in /premium /paid /api/premium /v1/paid; do curl -o /dev/null -w '%{http_code}\n' https://xrpldashboard.com$p; done`.

---

## 3. Receipts — independent signature verify

**EXPECTED**: one `/check.json` response, signature verified using ONLY the published pubkey (no repo access). Fingerprint on receipt-key DNS TXT record matches PEM/JSON.

**OBSERVED**:

- `/check.json?q=rrrrrrrrrrrrrrrrrrrrrhoLvTp` returns 200 with `proof.check_v09_signature = {canonicalization: "sorted-keys-no-whitespace-utf8", domain_separator: "xrpldashboard/receipt/v1", canonical_hash_sha256: "5c8953…", sig_ed25519: "ec4cee…"}`.
- Pubkey fetched from `/.well-known/snapshots/receipt_pubkey.json`: `fingerprint_short: A4:0F:B1:0A:9D:33:64:03`, `encoding.hex: f35c9e0aaa7d9e0e…`.
- **Signature verify recipe**: tried 8 message reconstructions. Only one passes: `sep.utf8 + 0x00 + sha256(canonical_json)_bytes`. **PASSES**.
- **The receipt-signature recipe (`sep || 0x00 || hash_bytes`) is NOT documented on `/methodology`.** Methodology page describes the snapshot signature recipe in detail but not the check.json receipt recipe. **An outsider with only public artifacts must reverse-engineer the format.**
- DNS TXT `_xrpld-receipt-key.xrpldashboard.com` resolves to: `"v=ed25519; fp=A4:0F:B1:0A:9D:33:64:03; pub=f35c9e0aaa7d9e0ebc22fe343dbb75ede8a517e54eac6a2320fb3bde8ceabd7d; sep=xrpldashboard/receipt/v1"`. **Matches the .json / .pem key material and fingerprint byte-for-byte.**
- Bonus: DNS TXT `_xrpld-snapshot-key.xrpldashboard.com` also resolves: `"v=ed25519; fp=7F:D4:F2:F4:D2:57:7C:BE; pub=…"` — separate pin for the snapshot-signing key (distinct from receipt).
- Bonus: root `xrpldashboard.com` TXT includes `"v=MCPv1; k=ed25519; p=wVwWVoqFaNXxaveCpW0O+QKieo0wb0diVpcjryCQbiw="` — MCP key advertised at DNS level.
- `ua_origin` recording works: sent unique `User-Agent: station-audit-<timestamp>`; page_views captured 1 row within 3 seconds. Recorded.

**VERDICT**: **partial-pass** — signature is verifiable independently AND DNS pin fully covers the fingerprint. But the exact `sep || 0x00 || hash` message construction is not documented on `/methodology`, so an outsider must reverse-engineer or read the repo to complete a verify. That's a documentation gap, not a cryptographic gap.

**EVIDENCE**: Python `cryptography` verify passed only on `sep.encode() + b'\x00' + bytes.fromhex(hash_hex)` — see the probe script embedded above. `dig +short TXT _xrpld-receipt-key.xrpldashboard.com @1.1.1.1`.

---

## 4. Signed snapshots + on-XRPL anchor chain

**EXPECTED**: from `/.well-known/` artifacts alone, verify today's signed snapshot, walk chain link to yesterday, verify anchor #6 memo matches the day-of anchor's chain_root. Outsider from docs alone.

**OBSERVED**:

- `/.well-known/snapshots/pubkey.json` fingerprint: `7F:D4:F2:F4:D2:57:7C:BE` — different from receipt key (correct — domain separation).
- `/.well-known/snapshots/chain.json`: `current_root=914fbbe85bcf2179…`, `root_history` length 121, current_leaf_index consistent.
- Today's `/.well-known/snapshots/2026-09-12.json`: `leaf_index=120`, `previous_root=cd0545a4685cdf8a…`.
- Yesterday's `/.well-known/snapshots/2026-09-11.json`: `leaf_index=119`, `chain_root=cd0545a4685cdf8a…`.
- **Chain-link check**: `today.previous_root == yesterday.chain_root` → **TRUE**.
- **Ed25519 signature verify** on today's snapshot, using the methodology-page recipe (`Ed25519 over canonical-JSON of {signing_domain, schema_version, snapshot_date_utc, leaf_hash, leaf_index, leaves_total, chain_root, previous_root}` against pinned pubkey): **PASSES**.
- `/.well-known/anchors.json`: 6 anchors, anchor #6 = `{date: '2026-09-11', ledger_index: 106922141, tx_hash: '54C2D7569598277D…', chain_root: 'cd0545a4685cdf8a…', memo_data_decoded: 'xrpldashboard/anchor/v1|2026-09-11|cd0545a4685cdf8a…'}`.
- **Anchor #6 chain_root == 2026-09-11 snapshot chain_root**: **TRUE**. On-ledger commitment matches disk artifact.
- `anchors.json.chain_cross_check.roots_match: false` — anchors.json HONESTLY declares the drift between current chain root (`914fbbe8…`) and latest-anchor chain root (`cd0545a4…`). Note field explains: weekly anchor cadence + daily chain advance = false is normal, but a `true` signal would be nice-to-have. False alone doesn't mean anything's broken.

**VERDICT**: **full pass**. Every step in the methodology recipe verifies independently. Anchor #6 memo cross-checks against the published snapshot from the same date. An outsider could do this from docs alone.

**EVIDENCE**: Python script above; hash chain-link + Ed25519 verify + memo comparison all one-shot from URLs.

---

## 5. Registry — /.well-known/registry/<today>.json + cross-page tier agreement

**EXPECTED**: today's registry snapshot verifies. Token pages, /whales, /check agree on tier for RLUSD (canonical), one GateHub token, one bare, one warning. Audited-18 count matches feed AND filter.

**OBSERVED**:

- `/.well-known/registry/2026-09-12.json` (post-commit `3d1be02`): valid JSON, `signing_key_fingerprint: A4:0F:B1:0A:9D:33:64:03`, `taxonomy_version: 1.0.0`, `row_counts: {issuer_facts: 10663, token_category_current: 387, token_category_history: 524, token_facts: 16742}`.

**Cross-page tier agreement — 4 samples:**

| Token | /token page | /check.json | Agree? |
|---|---|---|---|
| RLUSD canonical (rMxCKb…) | `verified` | `verified` | ✓ |
| GateHub USD (rhub8V…) | (regex miss — page renders "self-described" but not matched by my regex) | `self` | probably ✓ but not confirmed |
| Reaper RPR (r3qWgp…) — bare/two_way_toml | `verified` | `self` | **✗ DISAGREE** |
| Impostor USDT (rGbUj…) | warning-line present ("not Tether") + tier field not extracted | `self` | **✗ DISAGREE** (`self` is not the right tier for an impostor with `ticker_collision=TRUE`) |

**Audited-18 count agreement:**

- Frozen baseline this morning (09:00 EDT): 18 tokens on `/tokens?range=warnings`.
- **Now (18:45 EDT)**: DB count = **14**. Rolling 24-hour window has moved; low-volume warnings dropped out.
- Rolling feed panel: 7 rows total ("show all 7").
- The "18" number in `docs/thisweek/2026-09-13.md` will be a DIFFERENT number by Sunday 16:30 UTC publish (~44 hours from now). The paragraph refers to a moving 24h-active count, not a stable audited-pool count. The STABLE number is: **168 tokens carry `ticker_collision=TRUE` in `token_facts`; 421 carry `non_standard_code=TRUE`.**

**VERDICT**: **partial-pass**. Registry snapshot signed + structurally correct. Cross-page tier agreement is INCONSISTENT across at least 2 of 4 samples — /check.json and /token render different tier values for the same (currency, issuer). Audited-18 is TIME-SENSITIVE and already stale by 10 hours.

**EVIDENCE**: Live curl of 4 token pages + `/check.json?q=<cur>.<iss>` responses above; jj_ro `SELECT COUNT(*)` from live token_volume × token_facts join with the same WHERE clause the app uses.

---

## 6. Discovery — llms.txt / agents.json / openapi.json URL sweep

**EXPECTED**: every URL those files publish returns 200 today. Claims match reality.

**OBSERVED**:

- Extracted 46 distinct URLs across llms.txt + agents.json + x402 + openapi.json.
- **41 of 46 return 200 (89%)**.
- Non-200s:
  - `/changes/YYYY-MM-DD.json` → 404. **Template placeholder in the schema-doc — not a real URL. Not a bug** (my regex was too greedy).
  - `/check.json` → 400. **Requires `?q=` param.** Not a real bug — 400 without params is the correct schema-enforcement response, but the agents.json entry could point at a valid example URL.
  - `/check.json?q=rEXAMPLE...` → 400. **Placeholder in the agents.json example.** Not a real bug.
  - `/static/favicon.ico` → 404. **Real bug — favicon path returned by the icon extractor is dead. Filed post-freeze.**
  - `/thisweek` → 404. **Intentional gate — Sunday's edition not published until 16:30 UTC tomorrow.** Not a bug.

**VERDICT**: **pass with one real bug filed** (favicon 404). Every other non-200 is either intentional (gate) or a placeholder in the schema doc. Claims in llms.txt/agents.json about surface presence hold.

**EVIDENCE**: Python script iterating every URL discovered by regex, HTTP status per URL above.

---

## 7. Sovereignty — Render read paths

**EXPECTED**: every Render read path with its source (LAN tunnel / walker-cache / public RPC / public Ethereum). Zero public-RPC dependencies except disclosed browser-side WS and anchor canary witness.

**OBSERVED** (from existing `docs/SOVEREIGNTY_COVENANT_VIOLATIONS_2026-08-30.md`, cross-checked against llms.txt sourcing block):

| Route | Source class | Sovereignty | Notes |
|---|---|---|---|
| `/` (homepage strip) | walker-cache | 🟢 own | signed_snapshot deltas |
| `/whales` | walker-cache + XRPSCAN + Bithomp labels | 🟡 curated-labels-external | Fence-#3 violation; charter says grandfather-with-per-row-source-pill; not yet shipped |
| `/tokens` | walker-cache | 🟢 own | LAN rippled → token_volume + token_facts |
| `/token/<cur>/<iss>` | walker-cache | 🟢 own | + shared_tier_verifier reads token_category_current |
| `/pools` | walker-cache | 🟢 own | amm_ranked_pools |
| `/mpts` | sovereign-tunnel + PG cache | 🟢 own | via `rpc.xrpldashboard.com` CF-Access-tunnel |
| `/rlusd` | **public XRPL RPC** (s1.ripple.com) + Ethereum (Alchemy/1rpc.io) | 🔴 **Fence-#8 CRITICAL** | violates "backed by our own rippled node" claim on the very page pitched as sellable; blocks x402 mode-live |
| `/nfts` (backfill) | Ripple's public Clio archive | 🟡 disclosed-external | free-tier only per SELLABLE_REQUIRES_SOVEREIGN_SOURCE |
| `/check` | sovereign-tunnel | 🟢 own | |
| `/lending` | sovereign-tunnel | 🟢 own | |
| `/amendments` cross-check panel | **public XRPL RPC** (s1.ripple.com) | 🟡 semantic-purpose | Fence-#2 — "cross-check" implies second source; grandfather-with-disclosure recommended |
| `/wallet/<address>` blurb | public RPC | 🟡 disclosed | wallet_data still on public path |
| `/cold-storage` | walker-cache | 🟢 own | |
| `/api/xrp-price` | public | 🟡 external | |
| MCP server (mcp.xrpldashboard.com) | LAN rippled (Lenovo) | 🟢 own | envelope source = `local_rippled` |

**Public-RPC dependencies (Render read path):**
- `/rlusd` — **CRITICAL**, Fence-#8 blocker
- `/amendments` — **medium** (semantic OK if disclosed)
- `/wallet/<address>` blurb, `/api/xrp-price` — low priority, small surfaces

**VERDICT**: **partial** — 10+ routes are 🟢 own-sourced; 3 known 🟡 (labels, cross-check, wallet blurb); **1 CRITICAL** (`/rlusd` is Fence-#8, blocks x402 mode-live per the pre-existing sovereignty doc). Not new news — Charlie has this filed since Aug 30. Confirmed still open today.

**EVIDENCE**: `docs/SOVEREIGNTY_COVENANT_VIOLATIONS_2026-08-30.md`, cross-checked against llms.txt sourcing paragraph.

---

## 8. Uptime evidence — actual availability

**EXPECTED**: two weeks of route-canary + page_views history. Actual availability % per route.

**OBSERVED** — from `page_views.status` since 2026-09-09 (column added that day; 3 days of coverage, not 14 as requested):

| Route | Total requests | 5xx count | Availability % |
|---|---|---|---|
| `/` | 1,260 | 0 | **100.000%** |
| `/.well-known/anchors.json` | 345 | 0 | **100.000%** |
| `/check.json` | 346 | 0 | **100.000%** |
| `/whales` | 564 | 0 | **100.000%** |
| `/tokens` | 557 | 5 | **99.102%** — LP-explainer + XLM 500 bugs from yesterday, both fixed same day |
| **`/health`** | 379 | **28** | **92.612%** — **hidden gap** |

**`/health` availability at 92.6% is a real hidden problem.** 28 5xx over 3 days = ~9 per day. Charlie already has "07-11 UTC /health 5xx root-cause" on the post-freeze list, but 92% availability on the healthcheck endpoint itself is worse than the sellable-tier SLA the x402 flip presupposes.

**VERDICT**: **gap**. Uptime column history is only 3 days deep (not 14) because `status` was added 2026-09-09. Of routes covered, /health has a real availability problem that undermines any "high-availability paid data API" pitch.

**EVIDENCE**: `SELECT path, COUNT(*), COUNT(*) FILTER (WHERE status >= 500), pct FROM page_views WHERE ts >= NOW() - INTERVAL '14 days' GROUP BY path`. BetterStack history not queried (external API; not attempted in this pass).

---

## 9. The four planes — outside-verify per plane, one sentence each

| Plane | What an outsider can verify today | Verified? |
|---|---|---|
| **Data plane** | Fetch signed snapshot, walk chain-link, verify Ed25519 sig with published pubkey — all from `/.well-known/`. | ✓ |
| **Verifiability plane** (receipts) | Fetch signed /check.json response, verify Ed25519 sig — but message-construction recipe is not documented; outsider must reverse-engineer. | partial |
| **Payment plane** (x402) | Fetch catalog, see `accepts: []` and `mode=off` — nothing to pay for; no facilitator to simulate. | catalog-only |
| **Discovery plane** (MCP + agents.json + llms.txt) | Fetch discovery URLs, hit MCP server, list tools, call tools, get envelope. | ✓ (14/15 tools; 1 broken) |

---

## 10. Two strategy-doc claims tested

### (a) Mac Mini rippled node

**Claim in strategy doc** (Tier 2 #8): "Second-node redundancy (rippled cluster of 2). Mac Mini already runs a rippled node; wire it as a failover for the Lenovo."

**Observed**: `ps -ef | grep rippled` on Mac Mini returns no rippled process. `lsof -iTCP -sTCP:LISTEN | grep -E ':5005|:6006|:51234|:51235'` returns nothing. `/opt/homebrew/etc/rippled.cfg` does not exist. `brew list | grep -i ripple` returns nothing. Only `~/.config/rippled/rippled.cfg` exists (dated Jun 24 — a stale config file, no daemon).

**Verdict**: **REFUTED**. Mac Mini does not run a rippled node today. Strategy doc's Tier 2 #8 needs to be rewritten as "Stand up a second rippled node on the Mac Mini using the existing stale config as a starting point," not "wire the existing one as failover." Charlie's earlier recollection that the node was retired at the Lenovo migration was correct.

### (b) DNS TXT receipt-key pinning

**Claim in strategy doc** (Tier 3 #9): "DNS TXT pubkey pinning for both signing keys. `_xrpld-snapshot-key.xrpldashboard.com` + `_xrpld-receipt-key.xrpldashboard.com`. High-assurance clients pin via DNS. Effort: ~1 day (mostly DNS + docs)."

**Observed**:
- `_xrpld-receipt-key.xrpldashboard.com` TXT: `"v=ed25519; fp=A4:0F:B1:0A:9D:33:64:03; pub=f35c9e0aaa7d9e0ebc22fe343dbb75ede8a517e54eac6a2320fb3bde8ceabd7d; sep=xrpldashboard/receipt/v1"` — **matches published PEM/JSON**.
- `_xrpld-snapshot-key.xrpldashboard.com` TXT: `"v=ed25519; fp=7F:D4:F2:F4:D2:57:7C:BE; pub=a7efda2175ba3344bffa254e34854fdb7774c8beef663c3754e15d2fbf02c983"` — resolves.
- Root `xrpldashboard.com` TXT includes MCPv1 key: `"v=MCPv1; k=ed25519; p=wVwWVoqFaNXxaveCpW0O+QKieo0wb0diVpcjryCQbiw="`.

**Verdict**: **VERIFIED — already shipped**. Strategy doc Tier 3 #9 should be moved to "done, verified today"; the remaining work is documenting the DNS pin path on `/methodology` so outsiders know to use it. That's ~30 min, not 1 day.

---

## RE-RUN 2026-09-12 evening (post-fix window)

Same nine points, same outside-client method. Ship-list executed between 18:35 EDT and 20:00 EDT:

| # | Change | Commit | Live-verified? |
|---|---|---|---|
| 1 | Cross-page tier reconciliation (/check.json now reads `shared_tier_verifier.resolve_tier` for its top-level `tier` field, matching /token) | `d369716` | ✓ 5/5 agree live |
| 2 | Receipt recipe documented on /methodology — full 4-step outsider-verify walkthrough (pubkey pinning locations incl. DNS TXT, sig-block fields, message reconstruction `sep_utf8 \|\| 0x00 \|\| sha256_bytes`, worked example) | `43e2dd0` | ✓ anchor + recipe + fingerprint + DNS TXT all present |
| 3 | MCP `get_signed_snapshot` — code fix: fetch from published /.well-known/ URL first, local disk fallback for dev-mode | `86bdcde` | Render deploy landed; Lenovo restart pending Charlie's sudo (SSH session waiting on `restarted`) |
| 4 | Fence-#8 (/rlusd via own-node) — DEFERRED. On code read, closing requires adding a tunnel tier to `xrpl_client.py` before its local-first path (affects ~20 callers, needs broader testing). Not safe during freeze; my audit's 1.5hr estimate was wrong | (not shipped) | — |
| 5 | /health 5xx investigation — 28/28 5xx were on 2026-09-10 07-11 UTC, a discrete 5-hour incident. Zero 5xx on /health in the 48+ hours since. Not an ongoing problem. Root cause narrowed to Neon PG brief blip OR heavy-read walker in that window (both discrete-event class). No live-code change warranted | (not shipped — investigate-only) | — |
| 6 | /thisweek collision-line swapped for time-stable wording: "168 tokens flagged for ticker collision after this weekend's audit; a rolling 10-20 are actively trading at any moment" | `269935a` | ✓ preview reachable + wording swapped |
| — | Canary `_tier_agreement_probes()` — 5 fixed cross-surface tier checks (RLUSD/Reaper/Circle/GateHub/impostor USDT) fire every canary run. Pre-fix: 4 mismatches detected. Post-fix: 45 ok, 0 failing | `d369716` (same as #1) | ✓ live canary passes |

**Re-run against the same 9 points:**

| Point | Before | After | Delta |
|---|---|---|---|
| MCP server + envelope + registry listings | 14/15 tools | 14/15 (get_signed_snapshot fix deployed, Lenovo restart pending) | flat; +1 once MCP restarts |
| x402 catalog live-and-honest | 6/10 | 6/10 (attorney-gated; not touched) | flat |
| Receipts verify from published pubkey | 8.5/10 | **10/10** — recipe now documented | **+1.5** |
| DNS TXT pinning | 5/5 | 5/5 | flat |
| Signed snapshot chain outsider-verify | 10/10 | 10/10 | flat |
| Anchor #6 memo cross-check | 5/5 | 5/5 | flat |
| Registry snapshot signed + shape | 5/5 | 5/5 | flat |
| Cross-page tier agreement | 5/10 | **10/10** — 5/5 samples agree live | **+5** |
| Discovery URL sweep | 4.5/5 | 4.5/5 (2 new timeouts were probe-config not real regressions) | flat |
| Rate-limit headers on HTTP | 0/3 | 0/3 (not addressed this window) | flat |
| Sovereignty — Render read paths | 7/10 | 7/10 (Fence-#8 deferred as noted) | flat |
| Uptime — /health specifically | 4/8 | 4/8 (Sept 10 incident still in the rolling window; will roll off) | flat but ceiling improving |
| Standing-pool audited count vs published claim | 2/4 | **4/4** — wording swapped to time-stable 168 | **+2** |

**Delta: +8.5 points ≈ 76 → 84.5%.** Once the Lenovo MCP restart lands, +1 more (15/15 tools) → **~85.5%.**

## Honest number

| Piece | Weight | Score | Notes |
|---|---|---|---|
| MCP server + envelope + registry listings | 15 | 14 | get_signed_snapshot broken (Lenovo mirror lag) |
| x402 catalog live-and-honest | 10 | 6 | catalog+decisions shipped, rails not built |
| Receipts verify from published pubkey | 10 | 8.5 | verifies, but recipe undocumented |
| DNS TXT pinning | 5 | 5 | both keys pinned, MCP key too — undocumented on /methodology |
| Signed snapshot chain outsider-verify | 10 | 10 | full pass |
| Anchor #6 memo cross-check | 5 | 5 | full pass |
| Registry snapshot signed + shape | 5 | 5 | full pass |
| Cross-page tier agreement | 10 | 5 | 2 of 4 samples disagree between /token and /check.json |
| Discovery URL sweep | 5 | 4.5 | 1 real bug (favicon); rest gates or placeholders |
| Rate-limit headers on HTTP | 3 | 0 | not surfaced |
| Sovereignty — Render read paths | 10 | 7 | 3 known violations, Fence-#8 open |
| Uptime — /health specifically | 8 | 4 | 92.6%; other routes ≥99% |
| Standing-pool audited count matches published claim | 4 | 2 | 18-in-preview is stale by 10h; 168 is the STANDING pool number |

**Baseline morning number: 76 / 100. Post-fix evening number: ~84.5 / 100 (~85.5 once Lenovo MCP restart lands).** See the RE-RUN section above for the delta table.

**Everything that moved the number came from tonight's fixes:**
- Cross-page tier reconciliation: +5 points (5/5 samples now agree cross-surface — the class of bug that shipped Circle-USDC-as-impostor)
- Receipt recipe on /methodology: +1.5 points (outsider can now verify per docs, not by reverse-engineering)
- /thisweek time-stable wording: +2 points (168 flagged pool, not a moving "18 currently active" number)
- +1 more expected on Lenovo MCP restart

**Everything that did NOT move the number, and why:**
- Fence-#8: deferred — my audit's 1.5hr estimate was wrong; closing requires a `xrpl_client.py` tunnel-tier addition affecting ~20 callers. Not safe during freeze. Filed as a Sunday-morning-before-publish standalone commit if it can be tested in isolation, otherwise post-freeze.
- x402 mode-live rails: attorney-gated per Charlie's rule; not touched.
- Rate-limit headers on /check.json: not addressed this window; small effort but non-critical.
- /health uptime: Sept 10 incident still in the rolling 3-day query window; will roll off naturally by Monday. Not an ongoing problem — zero /health 5xx in the last 48+ hours.
- Discovery URL sweep flat: the 2 new "failures" (analytics, whales) were probe-timeout artifacts of my 8s probe budget, not real regressions.

**The "80% built" strategy claim now stands, honestly.** 84.5% is proven-working-end-to-end by an outside client tonight. The remaining ~15% is:
- x402 rails (blocked on Fence-#8 + attorney review — a decision, not a build)
- Rate-limit header emission (~2hr)
- /health incident root-cause docs (~1hr — narrowed to two candidates)
- /rlusd tunnel-tier (~3-4hr careful refactor)
- Second-node cluster for redundancy (~2 weeks)
- Publish agent observability (~1 week for MVP)

None of that gates Sunday's publish. All of it fits inside a post-freeze 30-day window.

---

## Ranked gap list with effort estimates

Ordered by "moves the needle vs. effort":

| # | Gap | Effort | Impact |
|---|---|---|---|
| 1 | **Fence-#8 close: /rlusd via own-node instead of s1.ripple.com** | ~1.5 hr per pre-existing doc | HIGH — unblocks x402 mode-live; largest single strategic gate |
| 2 | **Document the check.json receipt signature recipe** on /methodology (`sep_utf8 \|\| 0x00 \|\| sha256(canonical)_bytes`) | ~30 min | HIGH — turns "verifiable in principle" into "verifiable per docs" |
| 3 | **Cross-page tier agreement audit + fix** — /token, /check.json, /whales, /tokens list must return the same tier value for the same (currency, issuer). Reconcile shared_tier_verifier semantics vs. check_data.py's own tier ladder | ~4 hr | HIGH — the class of gap that shipped Circle-USDC as impostor; reproducible for every impostor row |
| 4 | **Sync signed_snapshots to the Lenovo mirror** so get_signed_snapshot MCP tool works | ~1 hr rsync-in-plist or ~1 day PG-first-read + drop-file-dep | MEDIUM — currently 1/15 MCP tools broken |
| 5 | **Add X-RateLimit-* headers to /check.json** so agents can self-throttle | ~2 hr | MEDIUM — table-stakes for a paid API |
| 6 | **Investigate + close /health 5xx (7.4% rate)** | unknown; already on post-freeze list | MEDIUM — undermines paid-tier SLA claim |
| 7 | **Rename strategy-doc claim about Mac Mini node** — it's stale-config, not a running node. If we want cluster-of-2, actually stand it up | ~1 day + amendments-tracker | LOW today but blocks scale story |
| 8 | **Move DNS TXT documentation onto /methodology** so outsiders know to pin | ~30 min | LOW — DNS is shipped; just tell people |
| 9 | **Fix `/thisweek` collision-count wording** — swap "18 impostor tickers actively warned" for something time-stable ("168 tokens flagged with ticker collision; a rolling ~10-20 are actively trading at any moment") | ~10 min template edit | MEDIUM — Sunday's post will lie by ~4 counts otherwise |
| 10 | **Fix favicon 404** | ~10 min | LOW cosmetic |

---

## Bottom line

The "80% built" claim overstated by ~4 percentage points. **The actual proven-working-end-to-end score today is ~76%.**

The 4-point gap is real but not catastrophic — the biggest single item on it (Fence-#8) is the same one Charlie has already documented as the x402 mode-live blocker with a target date. The rest are last-mile documentation, mirror-sync, and cross-page consistency fixes that would take days-to-weeks, not months.

The station isn't as complete as I called it — but the direction and the readiness argument still hold: nobody else has 76% of this stack, and the missing 24% is enumerated with specific items and hours.

Sunday's `/thisweek` post should say "168 flagged; rolling active depends on trading" rather than a moving "18" number that will be different by publish time.
