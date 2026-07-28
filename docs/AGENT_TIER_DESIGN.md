# xrpldashboard — Agent Tier (Free, Read-Only)
## Design Document

**Date:** 2026-07-27 (drafted) · **2026-07-28 (approved by Charlie, build starts Day 1)**
**Status:** APPROVED — five judgment calls resolved to standing recs; one addition folded in at tool #20 (`get_layer2_alarms` payload gains `status` + `resolution_note` fields, per § Judgment calls resolution below)
**Founding framing:** *The first MCP server whose data proves itself. Every response carries its receipts — source tier, freshness stamp, CLAIMS reference, snapshot signature where applicable. Competitors serve heuristic scores; we serve numbers with proof attached.*

---

## Motivation

The agent-payable data economy has arrived (x402, MCP, llms.txt, agents.json). One near-competitor (xrpl-utilities.com) is already positioned in that lane — six paid products, Swagger UI, PyPI SDK, MCP proxy — but their surface is 40-50% façade (empty tiles, "Loading…" stubs, "deterministic heuristic data only" per their own terms) and their engineering is thin (34 KB proxy, no walkers, no indexer visible, 0 stars). Their existence validates the thesis; their emptiness kills the urgency.

xrpldashboard already has the data. What we don't have is a machine-consumption surface. The research verdict (2026-07-27) is that we can add that surface in **5-7 engineering days with zero payment plumbing and zero new legal surface** — a strict subset of the parked API v1 work.

This tier is the *free* half of the "humans + agents free forever, agents paid only when attorney clears and demand proves it out" split. It ships now, occupies the discovery ground, and reveals demand telemetry that gates the paid-tier decision at 60-90 days.

---

## The one thesis

**Every response is proof-annotated.**

Not "trust the score." Not "our AI classified this." **Every JSON payload carries the source, the freshness contract, the CLAIMS reference where one exists, and the methodology URL.** Agents that consume our data can verify it themselves — against ledger truth, against the signed snapshot chain, against the four-layer audit — without a support ticket, without a phone call, without trust.

This is the differentiator. Competitors sell numbers. We serve numbers *and their receipts*, and we do it for free.

---

## Scope

### What this IS

- **Read-only** access to already-public data (nothing new is exposed that isn't already on `/whales`, `/rlusd`, `/rwa`, etc.)
- **Free** for all agents at reasonable volumes (rate limits below)
- **Machine-first surfaces:** MCP server (stdio + streamable HTTP), `llms.txt`, `agents.json`, OpenAPI/Swagger docs
- **Proof-annotated:** every response carries the standard receipts block (§ Proof-annotation spec)
- **Production-grade from day one:** monitored via `walker_health`, alarmed via `answer_plausibility_walker`, subject to the same four-layer audit as the HTML pages

### What this is NOT

- **NOT paid.** No x402, no API keys, no accounts, no billing rails. Four-zeros posture untouched by construction — nothing here receives crypto, nothing here holds fiat, nothing here touches user funds.
- **NOT the parked API v1.** API v1 (`parked/api-v1-scaffold @ 2b5eb76`) contains additional endpoints and — when unparked — payment gating. This tier is a strict subset: manifests + read-only MCP + read-only Swagger docs on the same data.
- **NOT a claim-attestation service.** We surface OUR facts and OUR CLAIMS. We do not adjudicate other parties' claims via API. (`/check` is a separate judgment call — see § Judgment calls.)
- **NOT under any obligation to remain free forever.** This is a phase, not a covenant. If the demand telemetry + attorney answer at 60-90 days supports a paid tier, we can add x402 on additional endpoints without breaking anything shipped here.

### Revisit triggers for the paid-tier decision (Option B follow-on)

Both of the following must clear before the paid tier is even a decision on the table:

1. **Attorney answer:** the legal shape of receiving crypto micropayments as a US-based solo LLC/individual — money-transmission classification, KYC accumulation at $0.10-scale volumes, sales/use tax on digital services, refund policy, Coinbase-facilitator ToS flow-down. Question added to next attorney-call agenda verbatim.
2. **Observed demand at 60-90 days:** tool-call counts per tool per day, unique client fingerprints (anonymous — no query retention), MCP-server session durations. If the surface is quiet, the paid-tier question defers indefinitely. If it's busy, the numbers inform which tools warrant metering first.

---

## Tool inventory (proposed v1: 20 tools)

Every tool below reads from existing production tables. No new walkers, no new data pipelines. Each entry lists **source**, **freshness contract**, and **proof-annotation shape** — the annotation appears in the response envelope, not as an out-of-band claim.

### Ledger primitives

**1. `get_ledger_stats`**
- Source: local `rippled` server_info via `xrpl_stream` writer + `walker_health.ledger_definitions_walker`
- Freshness: ≤ 5s (live ledger close cadence)
- Proof: `{source: "local_rippled", as_of: <ISO>, methodology_url: "/methodology#ledger"}`

**2. `get_amendment_status`**
- Source: `amendments` table (from `feature` RPC on localhost:5005) cross-checked by `cross_check_walker` pair (4)
- Freshness: ≤ 10 min
- Proof: `{source: "local_rippled+cross_check_pair_4", as_of, cross_check_status: "agree"|"disagree", claims_ref: "amendments_active_count", methodology_url}`

**3. `get_unl_status`**
- Source: `unl_snapshots` (from vl.ripple.com) cross-checked by pair (5)
- Freshness: ≤ 1 hour
- Proof: `{source: "vl.ripple.com+cross_check_pair_5", as_of, methodology_url: "/methodology#unl"}`

### Value flows

**4. `get_whale_events`**
- Source: `whales` table (100K+ XRP transactions)
- Freshness: ≤ 5 min
- Proof: `{source: "whales_walker", as_of, freshness_contract: "5min", claims_ref: "whales_events_24h", methodology_url: "/methodology#whales"}`

**5. `get_whale_watchlist`**
- Source: watchlist accounts (100 XRP+ shown on homepage row)
- Freshness: ≤ 5 min
- Proof: same as above with `claims_ref: "whale_watchlist_100xrp"`

**6. `get_rlusd_supply`**
- Source: `rlusd_supply_xrpl` + `rlusd_supply_eth`; XRPL side cross-checked by pair (2), ETH side by pair (1)
- Freshness: ≤ 5 min
- Proof: `{source: "s1.ripple.com+publicnode.com", cross_check_status, as_of_xrpl, as_of_eth, claims_ref: "rlusd_supply_total", methodology_url: "/methodology#rlusd"}`

**7. `get_rlusd_flow_24h`**
- Source: `rlusd_xrpl_net_change_24h`
- Freshness: finalized-window rule per R2 (only reports `snapshot_date < today-UTC`)
- Proof: `{source: "rlusd_refresher_walker", as_of, window_rule: "finalized_only", claims_ref: "rlusd_flow_24h"}`

### AMM / pools

**8. `get_amm_pool`** (query by address or asset pair)
- Source: `amm_index.json` + `amm_tvl_recorder` walker
- Freshness: ≤ 30 min per-pool, ≤ 5 min ranking
- Proof: `{source: "rank_amms_walker+amm_tvl_recorder", as_of, methodology_url: "/methodology#amm"}`

**9. `get_amm_top_by_tvl`** (ranked top-N)
- Source: same
- Freshness: ≤ 5 min
- Proof: `{source, as_of, claims_ref: "amm_top_by_tvl_rank"}`

### Tokens / RWA

**10. `get_token_attestation`** (query by issuer + currency)
- Source: `verify_toml` + `enrich_token_names` walkers
- Freshness: weekly cadence per walker declaration
- Proof: `{source: "verify_toml_walker+enrich_token_names_walker", as_of, verified_via: "toml"|"domain_fallback"|"lp_derived", claims_ref: "token_attestation_status", methodology_url: "/methodology#attestation"}`

**11. `get_rwa_families`**
- Source: RWA family table (Ondo, RLUSD, SocGen FORGE, etc.)
- Freshness: daily
- Proof: `{source: "rwa_families_table", as_of, claims_ref: "rwa_families_count", methodology_url: "/methodology#rwa"}`

**12. `get_rwa_pools`**
- Source: attributed AMM pools table
- Freshness: ≤ 30 min
- Proof: `{source: "rwa_pools_attributed", as_of, claims_ref: "rwa_pools_attributed_count"}`

**13. `get_mpt_snapshot`**
- Source: `mpt_snapshot` + `mpt_holders_refresh` walkers
- Freshness: daily snapshot; holders ≤ 1 hour
- Proof: `{source, as_of_snapshot, as_of_holders, claims_ref: "mpt_active_count"}`

### Institutional

**14. `get_permissioned_domains`**
- Source: `permissioned_domains_walker` — 14-account curated seed
- Freshness: daily
- Proof: `{source: "permissioned_domains_walker", as_of, honest_partial: true, scope_note: "14-account institutional seed", claims_ref: "permissioned_domains_active"}`

**15. `get_credentials`**
- Source: `credentials_walker` — same 14-account seed
- Freshness: daily
- Proof: `{source: "credentials_walker", as_of, honest_partial: true, scope_note: "14-account institutional seed", claims_ref: "credentials_active"}`

### Protocol snapshots

**16. `get_lending_snapshot`**
- Source: `lending_snapshot` walker
- Freshness: daily
- Proof: `{source, as_of, methodology_url: "/methodology#lending"}`

### Regulation

**17. `get_regulation_status`**
- Source: `regulation.html` context (LAST_VERIFIED_REGULATION constant + CLARITY Act tracker state)
- Freshness: manual bump (Charlie updates on regulatory events)
- Proof: `{source: "regulation_page_manual", as_of: LAST_VERIFIED_REGULATION, claims_ref: "clarity_act_status", methodology_url: "/regulation"}`

### The signed snapshot chain (proof primitives)

**18. `get_signed_snapshot`** (retrieve daily snapshot + fingerprint)
- Source: `signed_snapshot` walker + committed SQLite fallback
- Freshness: daily
- Proof: `{source: "signed_snapshot_walker", as_of, ed25519_signature, public_key_fingerprint, methodology_url: "/methodology#signed-snapshot"}`

**19. `verify_snapshot_signature`** (submit a snapshot payload + signature, get back verify result)
- Source: cryptographic verification only, no DB read
- Freshness: n/a (stateless)
- Proof: `{tool: "verify_snapshot_signature", public_key_fingerprint, verify_result: bool}`
- **This is the moat expression.** An agent can fetch a signed snapshot from us today, hold it, and verify against our published key months later without depending on our infrastructure being alive.

### Meta-transparency (the differentiator)

**20. `get_layer2_alarms`**
- Source: `answer_plausibility_alarms` table (append-only)
- Freshness: ≤ 5 min
- Proof: `{source: "answer_plausibility_walker", as_of, methodology_url: "/methodology#layer2"}`
- **Payload fields per alarm** (added 2026-07-28 per Charlie's build-approval addition — the single change from the drafted spec):
  - `status`: enum — `"open" | "resolved" | "expected-behavior"`
  - `resolution_note`: nullable string — MUST be non-null when `status ≠ "open"`; one-line explanation
  - **Founding example / rationale:** an open alarm quoted without context reads as *"their own system flags their data!"* during an innocent event. Yesterday's RLUSD market-lull fire is the founding case (alarm correct, data correct, world quiet). A fired-and-explained alarm must travel WITH its explanation. The tool's promise is not "here are our raw alarms" — it's "here are our alarms **with our own disposition of each**."
- **Schema/backfill work** (rides tool #20's build day, per build order): if `answer_plausibility_alarms` lacks `status` + `resolution_note`, add them in this build — schema migration + walker write-path update + manual backfill of the RLUSD market-lull as `status="expected-behavior"` with its one-line note. The Sunday-audit disposition workflow becomes the standing writer of `resolution_note` going forward — audit agenda gains one line ("triage new alarms → write disposition"). Ships as its own focused commit(s), separate from the MCP tool wiring.
- **This is the four-layer audit made machine-readable.** Agents can query the alarms feed to see what we're currently flagging on our own site — every open alarm, every rule fire, every escalation. Nobody else on the market does this. It's the "nothing stays wrong here quietly" contract, exposed as an API — and every non-open alarm carries its own explanation, so a snapshot of the feed is self-describing.

*(Optional 21st tool considered: `get_walker_health` for full walker-freshness telemetry. Deferred as potentially reads-only-noise for agents; revisit if requested.)*

---

## Proof-annotation spec

Every tool response wraps its payload in a **standard envelope**. The envelope IS the proof.

```json
{
  "data": { /* tool-specific payload */ },
  "proof": {
    "source": "walker_name or table_name or 'cross_check_pair_N'",
    "as_of": "2026-07-27T22:30:00Z",
    "freshness_contract": "≤ 5min | ≤ 30min | daily | finalized_only",
    "claims_ref": "canonical CLAIMS.yaml entry key (or null if not tracked)",
    "methodology_url": "https://xrpldashboard.com/methodology#section",
    "cross_check_status": "agree | disagree | not_applicable",
    "honest_partial": false,
    "scope_note": "optional string when honest_partial=true"
  },
  "server": {
    "name": "xrpldashboard-mcp",
    "version": "1.0.0",
    "public_key_fingerprint": "<Ed25519 fingerprint>",
    "docs": "https://xrpldashboard.com/methodology#for-ai-agents"
  }
}
```

**Field contracts:**

- `source`: string, non-empty. Names the specific walker or table. Never "our system."
- `as_of`: ISO 8601 UTC. Never null. If data has no freshness stamp, the tool must not exist.
- `freshness_contract`: one of the enumerated values matching the walker's declared cadence.
- `claims_ref`: string keyed to CLAIMS.yaml. Null only if the tool response is not itself a page claim (e.g., ledger primitives).
- `methodology_url`: absolute URL. Must resolve to a real anchor on `/methodology`. Broken anchors caught by claims_check.sh extension.
- `cross_check_status`: only populated when a `cross_check_walker` pair covers this data. Values: `"agree"` (last check agreed), `"disagree"` (last check disagreed — the alarm arm), `"not_applicable"` (no pair covers this).
- `honest_partial`: boolean from `walker_scope_declarations.honest_partial` for the source walker. When true, `scope_note` must be populated with the same text as the walker's `declared_scope`.

**Enforcement:** the MCP server has a single response-wrap function that all tools call. No tool bypasses it. Proof envelope is not decoration — it's the response schema.

---

## Manifests

### `llms.txt` (single markdown file at root)

Per llmstxt.org spec: H1 title + blockquote summary + H2-delimited link lists. Content covers all ~40 human-facing pages plus a "For AI agents" H2 pointing at the MCP server + Swagger docs.

**Draft skeleton:**

```markdown
# xrpldashboard.com

> XRPL truth-audit dashboard. Every claim has receipts. Four-layer audit
> exposes machinery honesty, answer plausibility, external cross-checks,
> and a change-safety claims manifest. Free for humans and free for AI
> agents at reasonable volumes.

## Core dashboards
- [Homepage constellation](https://xrpldashboard.com/): Live ledger stats
- [/whales](https://xrpldashboard.com/whales): 100K+ XRP transactions
- [/rlusd](https://xrpldashboard.com/rlusd): RLUSD XRPL + ETH tracker
- ... (all pages)

## For AI agents
- [MCP server (streamable HTTP)](https://xrpldashboard.com/.well-known/mcp)
- [MCP quickstart](https://xrpldashboard.com/methodology#for-ai-agents)
- [OpenAPI spec](https://xrpldashboard.com/openapi.json)
- [agents.json](https://xrpldashboard.com/.well-known/agents.json)

## Trust surfaces
- [/coverage](https://xrpldashboard.com/coverage): Four-layer audit register
- [/methodology](https://xrpldashboard.com/methodology): Data provenance
- [Signed snapshot key](https://xrpldashboard.com/about#snapshot-key)
```

### `agents.json` (Wildcard-AI flavor)

Wildcard's `agents.json` is the most-cited proposal (built atop OpenAPI, defines "flows"). Serve at `/.well-known/agents.json`. Content = OpenAPI reference + top-level flows for common agent tasks (e.g., "verify a token address," "get whale events for last hour," "verify a signed snapshot").

**Google's `ai-catalog.json` (Agentic Resource Discovery)** is a candidate to add later — serve when the Google spec stabilizes. Not v1 blocker.

**IETF `draft-han-ai-manifest-01`** is early-stage; skip until it advances.

### OpenAPI / Swagger

Options: `flask-smorest` (mature, marshmallow-based) or `flask-openapi3` (Pydantic-based). Recommend **flask-smorest** for its wider adoption. Serve Swagger UI at `/docs`, JSON at `/openapi.json`.

The parked `api-v1-scaffold @ 2b5eb76` (~400 lines) can be decorated with flask-smorest schemas as part of this build. Endpoints ship as-is; payment gating stays parked (that's Option B).

**Title-is-contract applies to every tool description.** If the tool description says "returns the last 5 minutes of whale events," the response must actually reflect that. Descriptions are claims, and CLAIMS.yaml gains entries for the manifest content itself.

---

## Rate limiting + abuse posture

**Three-audience rule (standing):**

1. **Humans:** never friction. The MCP server is not for humans; the /docs page has a "you probably want the dashboard" pointer for accidental visitors.
2. **AI crawlers (retrieval/citation):** never blocked, always identified. Common AI-crawler user agents (GPTBot, ClaudeBot, PerplexityBot, Google-Extended, etc.) get a generous rate and a `X-XRPL-Dashboard-Audit-URL` header on every response pointing at `/coverage`.
3. **Scraper fleets:** cost-defended. Same fleet-block logic used for `/whales` extended to the MCP endpoint. Per-IP token bucket; per-fingerprint rate.

**Concrete limits (v1):**

- **Unauthenticated / anonymous:** 60 requests / minute / IP. Standard for free public APIs.
- **Identified AI crawlers (by UA):** 300 requests / minute. Warm citations, cold to scrapers.
- **MCP session (long-lived connection):** 600 tool calls / hour / session. Long-tail scraping deterrent without blocking legitimate agent use.
- **Signed-snapshot verification (`verify_snapshot_signature`):** unlimited (stateless, cryptographic-only).
- **Backoff shape:** 429 with `Retry-After` header, not silent throttling.

**Fleet detection:** same signature as the 2026-07-23 `/whales` fleet-block first-night event. Chrome/142 UA + geographic clustering + burst pattern → soft-block with 429. Adaptation observed → escalate to CF-Challenge (still gated on the `/whales` challenge parking condition).

**Abuse tell-tales to monitor from day one:**

- Repeated 429s from a single fingerprint (fleet)
- Repeated calls to the same `verify_snapshot_signature` with different signatures (fuzz attempt on the crypto)
- Repeated calls to `get_layer2_alarms` with no other tool calls (competitor intelligence gathering — acceptable, not blocked, but instrumented)

---

## Infrastructure

**Placement:** Render, alongside the Flask app in the same service. MCP server exposes at `/mcp` (streamable HTTP) and can be attached to Claude Desktop via a small stdio-adapter script published to the repo.

**Rationale:** the MCP server is a thin wrapper over the same Postgres queries the HTML pages already run. Same Neon connection pool, same walker outputs, same code paths. Separate service adds infra cost, deploy complexity, and a second walker_health surface. Zero rationale to split.

**Cost delta:** **$0/month** at expected volumes. Render service scales with existing plan; Neon read load is a small delta over existing HTML page reads. Only cost scenario is if traffic scales past the current Render tier — which is a good problem, not a design constraint.

**Walker health:** the MCP server itself gets a `walker_health` row (`walker_name = "mcp_server_heartbeat"`, cadence 60s, self-writes a heartbeat via a background thread). Freshness surfaced on `/health` alongside every other walker. If the MCP server dies, `walker_health` goes yellow within 60s → red within 5min → external monitor pages.

**Layer 1 coverage from day one:** MCP server is a production surface. It gets:

- `walker_scope_declarations` row (declared_scope: "read-only MCP tool proxy over existing Neon tables", filter_note describes the 20-tool inventory)
- `walker_health` row (heartbeat)
- Schema-drift-loud handling — any query hitting Undefined* propagates as an MCP error with the same loudness discipline
- `answer_plausibility_walker` awareness — no new rule needed initially, but the MCP server's own uptime is a Layer 1 fact worth alarming on

---

## CLAIMS.yaml + `/methodology` updates

### CLAIMS.yaml additions

Every tool that has a `claims_ref` needs a corresponding CLAIMS.yaml entry (or already has one — most do, since these are the same claims the HTML pages make). Additionally:

- The manifests themselves become claims: `agents.json` content, `llms.txt` content, MCP tool descriptions all go through `claims_check.sh` on every diff.
- New claim key: `mcp_server_tool_inventory` — the 20-tool list is itself a public statement about what we expose.
- New claim key: `agent_tier_free_forever_v1` — the "free at v1" promise. Non-covenant, but a claim, and CLAIMS discipline catches any accidental copy drift.

### `/methodology` update — new section "For AI agents"

New H2 section on `/methodology`. Contents:

- What the MCP server exposes (link to `/openapi.json`)
- The proof-annotation spec (the JSON envelope, with a live example)
- The freshness contracts by tool
- The signed-snapshot verification flow (agents can verify us against our published key)
- The rate-limit shape (transparent)
- The "we don't retain your queries" contract
- The reason we don't currently charge (link to `/regulation` and `/security` for the legal-first posture)

`LAST_VERIFIED_REGULATION` sibling: consider `LAST_VERIFIED_AGENT_TIER_METHODOLOGY` for the "For AI agents" section — bumped when any tool schema changes.

---

## Judgment calls — RESOLVED 2026-07-28

Charlie's word (msg 9542, 2026-07-28 13:16 EDT): **APPROVED, five calls resolved to the standing recommendations below, plus one addition folded in at tool #20 (see § Tool inventory → tool #20 payload fields).**

Rationale for each call preserved below as the founding record; the standing rec is now the ship spec.

**1. `/check` as a tool: EXCLUDE from v1 (my lean).**

`/check` aggregates OFAC + RDAP + crt.sh + attestation signals for scam evaluation. Its HTML page ships with three FCRA disclaimers because the surface is FCRA-adjacent (name-in-loop with potential adverse-action interpretation). Exposing `/check` via MCP amplifies that surface — an agent could programmatically feed `/check` outputs into an automated decision pipeline that IS FCRA-covered (employment, credit, housing, insurance screening).

**Recommendation:** exclude `/check` from v1 tool inventory. Wait for the attorney call. If counsel approves, v1.1 adds `check_address_signals` with the same three FCRA fences baked into the tool description and every response.

**2. Any other tool with legal texture:**

- `get_regulation_status` — pure factual reporting of publicly available bill status. No legal texture. INCLUDE.
- `get_layer2_alarms` — our own alarms about our own data. No third-party naming. INCLUDE.
- `get_token_attestation` — reports our attestation status for a token. Includes third-party names (issuers). Currently on `/tokens` HTML with attestation-dispute-contact links (per prior attestation-dispute shipping). Same rules apply — INCLUDE with a `dispute_contact_url` field in the proof envelope.
- `get_rwa_families` / `get_rwa_pools` — names third-party RWA issuers, same treatment as HTML `/rwa`. INCLUDE with `dispute_contact_url`.

**3. Signed-snapshot verification as a public tool:**

`verify_snapshot_signature` is genuinely novel. No competitor offers this. The moat expression I'd most want Charlie's eyes on before shipping: is exposing a public cryptographic-verification endpoint any different, legally or operationally, from publishing the public key itself (which we already do)? My read: no — it's a convenience wrapper. But flagging.

**4. `agents.json` spec choice:**

Wildcard is the most-cited but not canonical. Google's ai-catalog is likely to consolidate. Ship Wildcard now, add Google when it stabilizes. My lean; Charlie may prefer to wait for spec consolidation and skip Wildcard entirely.

**5. MCP server as its own repo vs. in-tree:**

Competitor's MCP repo is a public GitHub artifact (part of their positioning). We could publish ours similarly (`xrpldashboard-mcp` repo) or keep it in-tree in `xrpl_test`. Publishing separately amplifies the "we have this" signal for grant application material; in-tree is less setup. My lean: in-tree first, extract to public repo if the demand telemetry shows adoption.

---

## Grant note

Per §1 verdict (2026-07-27 research): shipping this tier is **grant-neutral to grant-positive.** It extends the free public-good surface (Glow eligibility factor: "quality" + "impact" + "open access"), it's MIT-compatible with any grant license expectation, and it does not touch the paid-tier disclosure question. The Glow application can cite the MCP server + manifests + signed-snapshot verification as concrete public-good deliverables shipped in the retroactive window.

Recommended framing for the application: *"xrpldashboard exposes its data + audit surface to AI agents on the same free-and-verifiable terms as it does to human readers. Every tool response carries source attribution, freshness stamp, and a link to the methodology page. Signed daily snapshots are cryptographically verifiable against a published Ed25519 key, ensuring auditability that outlives any single hosting provider."*

---

## Build order (5-7 days after Charlie approves this doc)

1. **Day 1 — llms.txt + agents.json + `/methodology` "For AI agents" section.** Static files; zero infra impact. Publish first because they're the discovery layer.
2. **Day 1-2 — MCP server scaffold.** Anthropic Python SDK, streamable-HTTP transport, `walker_health` heartbeat wired, proof-envelope wrap function shipped first.
3. **Day 2-4 — Tool implementations, 3-5 per day.** Ledger primitives first (they exercise the envelope), then value flows, then AMM/pools/tokens, then meta-transparency (`get_layer2_alarms`, `verify_snapshot_signature` last).
4. **Day 5 — OpenAPI decoration.** flask-smorest applied to parked `api-v1-scaffold`; Swagger UI at `/docs`. This is the human-readable machine-facing doc.
5. **Day 5-6 — Rate limiting + fleet-block extension.** Per-IP token bucket, AI-crawler UA whitelist, 429s with Retry-After. Extend existing fleet-block infrastructure.
6. **Day 6-7 — Verification bar.** Each tool tested against ledger truth (same evidence-table discipline as every ship). Manifests validated against their specs. **One end-to-end demo from Claude Desktop captured** — screenshot + tool-call log — for the record and for Glow application material.

---

## Verification bar (ship gate)

Nothing ships without:

- **Proof envelope enforced on every tool.** Random-sample audit: no tool returns a payload without the standard envelope.
- **Every `claims_ref` resolves.** `claims_check.sh` extended to walk MCP tool inventory + CLAIMS.yaml + `/methodology#for-ai-agents`.
- **Every `methodology_url` resolves.** Simple HTTP head-check on all URLs referenced in envelopes.
- **Every walker referenced in `source` has a `walker_scope_declarations` row.** Includes the new `mcp_server_heartbeat` row.
- **Rate limits verified under load.** Simulated fleet traffic + real AI-crawler UA traffic — both behave as declared.
- **One live end-to-end demo captured** from Claude Desktop connecting to the MCP server and calling ≥ 3 tools. Screenshot committed to `docs/agent_tier_ship_evidence/`.
- **Signed-snapshot verification tested end-to-end.** Fetch a signed snapshot via `get_signed_snapshot`, then verify it via `verify_snapshot_signature`. Round-trip must return true.

---

## Demand telemetry (Days 1-90 post-ship)

Records per tool per day, retained for aggregate analysis. **No query retention** (mirroring `/check` privacy contract):

- Tool call count (per tool, per day)
- Unique client fingerprint count (per day, hashed IP + UA)
- MCP session count + median session duration
- 429 rate (per tool, per day) — abuse signal
- Verification tool call rate — separate line-item because it's the moat expression

At Day 60 and Day 90: aggregate summary written to `docs/agent_tier_demand_telemetry_YYYY-MM-DD.md`. If numbers indicate demand, this becomes input to the Option B decision (with attorney answer in hand). If quiet, we've still shipped a strong public-good surface and the paid decision defers further.

---

## What ships after Charlie says go — NOT this document

- No file gets committed to `main` before Charlie reviews this design.
- The `seed_walker_scope_declarations.py` edit for `is_bot_writer` + `is_bot_canary` is staged (Charlie decides commit; it's independent of this build).
- The design doc itself is `docs/AGENT_TIER_DESIGN.md`, in the tree, uncommitted. It rides its own commit when Charlie says go.

**The one thing this document changes if Charlie approves:** the parked `api-v1-scaffold` branch gets an OpenAPI decoration commit as part of the build. The branch stays parked (payment gating unshipped); the OpenAPI work is additive and prep for Option B if it ever arrives.

---

## Fences (load-bearing, restate on any future edit)

1. **No payments in this tier.** Ever. If a future edit proposes adding x402 to this document, reject and refer to Option B design (which does not yet exist as a doc).
2. **Retail dashboard stays free forever.** Any tool response that would gate the same data behind an HTML paywall is disallowed by construction; there is no HTML paywall.
3. **Proof envelope is not optional.** Every tool wraps. A tool that ships without the envelope is a bug, not a shortcut.
4. **The signed-snapshot chain is the auditability floor.** If Neon dies, if Render dies, if the operator disappears — a caller who fetched a signed snapshot last week can still verify it against the published key. This is the actual promise of the tier.
5. **Adversary behavior does not drive edits to this doc.** If a competitor ships something faster, cheaper, or shinier, that changes nothing about what we ship. Our promise is proof-annotated free data. Everything else is noise.

---

**End of design doc. Approved 2026-07-28 by Charlie — build kicks off same day; see commit message for kickoff.**
