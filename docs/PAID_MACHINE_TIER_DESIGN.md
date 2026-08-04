# xrpldashboard — Paid Machine-Tier
## Decision Memo (NOT a build)

**Date:** 2026-08-03 (last amended 2026-08-04 per fresh-Claude external evaluation with web-searched revenue evidence)
**Status:** DRAFT DECISION MEMO — pressure-test of a strategy sentence, not an approved build. Gated by (1) attorney answers and (2) the sovereignty audit in § 0 of this document.

**Amendment log:**
- 2026-08-03 (`d2fed28`): § 1.A gained signing dimension per Grok evaluation.
- 2026-08-03: § 1.E added (dataset licensing per ChatGPT); § 3.1 added (queryable claims layer as Phase 3 free-tier substrate — both engines converged on this); § 3.2 added (envelope `confidence` enum spec per ChatGPT gap #4); Next-steps #5 (`/audits` public surface backlog) + #6 (overlap-weighting standing protocol) + #7 (envelope-fix confidence-field placement).
- 2026-08-04 (this commit): external-reality calibration folded in per third external voice (fresh Claude with cited web search — see `project_external_ai_evaluation_claude_cold_2026-08-04.md`). § 1.A gained y1 revenue expectation ("low hundreds to low thousands"). § 1.D HOLD hardened with x402 $28K/day XRPL-ecosystem baseline as step-change requirement before Q4-2026 revisit. § 1.E reframed as relationship-driven ($2K-20K/deal manual sale to eval labs), not marketplace-listable. § 1.F added — Cloudflare Pay Per Crawl (opt-in, feasibility read only, respects all five sovereignty rules, zero build cost). New headline framing added below the strategy sentence: **monetizing the trust layer is a 2027+ story, not a 2026 one** — validates current sequencing (build citation substrate first). All market numbers are `[FROM-REVIEW-CITED-SEARCH]` — treat as pointers to verify before load-bearing decisions, not consensus fact.

**Strategy sentence under test:**
> *"Current state free for everyone forever; depth, delivery, and proof-at-scale metered for machines exclusively."*

**Timing framing (added 2026-08-04):**
> *"Monetizing the trust layer is a 2027+ story, not a 2026 one."*
>
> This is not a retreat from the strategy sentence. It is an honest read of the current market: measured MCP monetization sector-wide is <5%, x402 XRPL-ecosystem volume is ~$28K/day, dataset-licensing deals to eval labs price in the $2K-20K/deal manual-sale band. None of those numbers support a paid-tier revenue business in 2026. What they do support is **building the citation substrate now (queryable claims, signed history, envelope discipline) and metering it when the market shape emerges.** This validates the current sequencing — the memo's HOLDs are not stalling, they are aligned with the market's own tempo. Source: `project_external_ai_evaluation_claude_cold_2026-08-04.md` (Part 2 findings, cited web search).

**Hard gates before any paid product ships:**
1. **Attorney gate** — money-transmission classification, sales-tax treatment of digital services, custody posture, refund policy, Coinbase-facilitator ToS flow-down. Verbatim question already on next attorney-call agenda (see `docs/AGENT_TIER_DESIGN.md` § Revisit triggers). Awaiting Kirk (Taft Law) response to 2026-08-03 email; Oberheiden / Cogent / IndyBar remaining in Charlie's lane.
2. **Sovereignty gate** — the audit in § 0. Nothing below moves without a series passing the provenance test.

---

## § 0. The Sovereignty Rule

> **We only sell data we source ourselves and can prove ourselves.**

This is not a preference. It is a boundary that gates every candidate below. The site's entire thesis is *proof-at-source*; monetizing data whose lineage ends at someone else's infrastructure would be selling their name under ours.

### Classifications

Every candidate data series is classified into exactly one of:

- **SOVEREIGN** — computed by our walkers from our own local `rippled` node (post-Batch-B) or from XRPL consensus data we independently validated. Signable in our name without qualification.
- **PUBLIC-INFRA-DEPENDENT** — currently sourced from Ripple's free public nodes (`s1.ripple.com`, `s2.ripple.com`) or the XRP Ledger Foundation cluster (`xrplcluster.com`). **Sellable only after migration to our own node,** and only after a gap audit of any historical data reused post-migration. Name the migration path per series.
- **THIRD-PARTY-DERIVED** — depends on someone else's API, index, curation, or non-XRPL chain infrastructure (Clio archives, Bithomp labels, external Ethereum RPC providers, price feeds, anything not first-order XRPL consensus retrieved via our own node). **NOT sellable, period.** These can inform *free* surfaces with attribution; they never enter a paid product.

The proof test doubles as the boundary test: **if we cannot attach our own envelope with our own signature over data whose lineage ends at our infrastructure, it does not go in the tier.**

### Provenance table (the memo's spine)

Traced 2026-08-03 by reading actual code paths, not env-var lever names (per the twice-proven census lesson). Every row cites the file:line of the actual endpoint call.

| Series | Producing file:line | Endpoint (literal in code) | Class | Path to sovereign |
|---|---|---|---|---|
| **AMM TVL / pool data** | `rank_amms.py:43,445` | `https://s1.ripple.com:51234` (env `XRPL_NODE`, default hardcoded) | PUBLIC-INFRA-DEPENDENT | Batch B: set `XRPL_NODE=<local-rippled>`; migrate to `xrpl_client` cascade |
| **RLUSD supply/net-change (XRPL side)** | `rlusd_live.py:117,330` | `xrpl_client.JsonRpcClient` → local rippled → s1 → s2 cascade | **SOVEREIGN today** (walker fleet on local rippled) | Already there; verify with gap audit on historical rows |
| **RLUSD supply/net-change (Ethereum side)** | `rlusd_live.py:114`, `_DEFAULT_ETH_RPCS` | Public Ethereum RPC list: `ethereum-rpc.publicnode.com`, `eth.llamarpc.com`, `rpc.ankr.com/eth`, `1rpc.io/eth` | **THIRD-PARTY-DERIVED** | Would require operating our own Ethereum node — out of scope for this project. **Not sellable ever under the rule.** |
| **Whale events (≥ 100k XRP)** | `xrpl_stream.py:49` | `wss://s2.ripple.com` (hardcoded, no fallback) | PUBLIC-INFRA-DEPENDENT | Batch B: env-var lever + reconnect-with-backfill + Lenovo WS exposure (pre-decision memo already exists — see § D of the AI cross-verify from earlier today) |
| **Token volumes (`volumes.db`)** | `xrpl_stream.py:49,476` | Same `wss://s2.ripple.com` as whales | PUBLIC-INFRA-DEPENDENT | Same migration as whale events |
| **MPT snapshots** | `mpt_snapshot.py:86,128` → `mpt_data.py:46` | `os.environ.get("XRPL_NODE","https://s1.ripple.com:51234")` | PUBLIC-INFRA-DEPENDENT | Batch B: `XRPL_NODE=<local-rippled>` |
| **NFT activity — activity mode (forward)** | `nft_activity_walker.py:277` | `get_client()` → local rippled → s1 → s2 cascade | **SOVEREIGN today** | Already there; but activity-mode retention is only forward from cursor-seed date |
| **NFT activity — backfill mode (historical)** | `nft_activity_walker.py:65-67,413` | `https://s2-clio.ripple.com:51234/` (env `XRPL_BACKFILL_CLIO`) | **THIRD-PARTY-DERIVED** | Clio is a curated archive service run by Ripple. Would require our own full-history `rippled` (~2M ledger retention, disk + config on Lenovo). Currently parked; see hard-case verdict § 0.4. |
| **Amendment change events** | `amendments_state.py:28,189` | `https://s1.ripple.com:51234` (env `XRPL_NODE`) | PUBLIC-INFRA-DEPENDENT | Batch B: `XRPL_NODE=<local-rippled>` |
| **Signed snapshots** | `signed_snapshot.py:63,317` | Ledger index from s1; pool/MPT/RLUSD payload from PG cache written by upstream walkers (also s1-rooted currently) | **MIXED** — see § 0.5 | Requires per-series decomposition |
| **Cold storage / labeled balances** | `cold_storage.py:79,84` | `get_client()` → local rippled cascade | **SOVEREIGN today** | Verify with gap audit |
| **Escrow / Oracle objects** | `escrow_walker.py:106`, `oracle_walker.py:161` | `get_client()` → local rippled cascade | **SOVEREIGN today** | Verify with gap audit |
| **Network pulse / ledger tip** | `network_pulse.py:25,97-100` | `https://s1.ripple.com:51234` **hardcoded, no env override** | PUBLIC-INFRA-DEPENDENT | Batch B: normalize env-var lever; walker RPC-lever backlog issue |
| **R-alarm events (Layer 2 alarms)** | Alarm state persisted in our own tables | Our own logic over our own detection surfaces | **SOVEREIGN by construction** | Already there |

### § 0.1 — Silent hardcodes surfaced by the trace

Three code paths escaped the walker RPC-lever normalization backlog (`project_walker_rpc_lever_normalization_backlog.md`):

- `network_pulse.py:25` — hardcoded s1, no env override. Outlier for consistency. Adds to Batch B agenda.
- `xrpl_stream.py:49` — hardcoded `wss://s2.ripple.com`, no fallback. Already in Batch B decision memo from the cross-verify earlier today (stream-gap semantics blocker).
- `nft_activity_walker.py:65-67` — Clio hardcode is *deliberate* (comment explains: local rippled's ledger_history is ~10k vs ~2M needed for backfill). Not a bug — an architectural constraint that produces the hard case in § 0.4.

### § 0.2 — Grandfathered-sovereign or tainted-until-recollected?

*The load-bearing philosophical decision of this memo.*

**Argument for grandfathered-sovereign:** XRPL consensus data is XRPL consensus data regardless of retrieval path. If our walker successfully retrieved a validated ledger from s1 and stored it in our tables, we hold consensus data. The endpoint was delivery, not source.

**Argument for tainted-until-recollected:** Charlie's rule is *"we source ourselves."* If the retrieval path during the historical accumulation window was Ripple's public infrastructure, we sourced *from them*, not *ourselves*. Additionally, retrieval reliability during that window is unverified — we have no gap audit proving no silent drops. `load_factor` events, stream disconnects, and 503 pushback all leave holes we may not have logged. Selling data with unproven completeness contradicts the proof-at-source thesis.

**Verdict: tainted-until-recollected, with one honest carve-out.**

A series may be reclassified as **grandfathered-sovereign** only if all three conditions hold:
1. The series was collected via retrieval paths that were *provably reliable* during the collection window (e.g., a per-request receipt table that logs endpoint + response status, or a cross-check walker with independent verification).
2. A gap audit over the historical range detects zero silent drops (or enumerates and documents them).
3. The lineage is decidable per row (i.e., the row stores which endpoint served it, not just the value).

**Practical consequence:** most historical time-series are tainted-until-recollected. Once Batch B lands, walkers should begin **re-recording** the series with per-request lineage stamps. The "sellable history" clock starts at recollection, not at first-collection. This is the honest cost of the rule — accept it.

Exception: **RLUSD XRPL-side, escrow, oracle, cold-storage, NFT-activity-mode-forward** — these already run on `get_client()` cascade (which prefers local rippled). Their historical rows are already collected from a sovereign-preferring path *once Batch A completed* (Lenovo walker plists moved 2026-08-02). Gap audit still required, but the reclassification path is short.

### § 0.3 — Hard case: RLUSD Ethereum-side

The Ethereum-side supply (RLUSD is an ERC-20 as well as an XRPL IOU) is retrieved by hitting public Ethereum RPC gateways: `ethereum-rpc.publicnode.com`, `eth.llamarpc.com`, `rpc.ankr.com/eth`, `1rpc.io/eth` (`rlusd_live.py:114`).

Verdict: **THIRD-PARTY-DERIVED. Not sellable under this rule, ever.**

Path to sovereign: operate our own Ethereum node. Ethereum full node is ~1TB and rising; execution-layer sync alone is a distinct operational discipline. This is out of scope for xrpldashboard. RLUSD Ethereum-side data stays on the *free* side of the wall, with the same attribution it has today.

Consequence for `/rlusd` page and any bulk-supply totals: cross-chain aggregate numbers that fuse XRPL + Ethereum supply cannot ship in a paid product. Either sell the XRPL-only slice, or don't sell the RLUSD family at all.

### § 0.4 — Hard case: NFT backfill via Clio

The NFT backfill walker sources historical NFT activity from `s2-clio.ripple.com`. Clio is Ripple's own public archive service, structurally a curated index of ledger history that our local rippled cannot serve (retention ~10K ledgers vs. ~2M needed to reach the 2026-04-01 floor).

Under the sovereignty rule, this is **THIRD-PARTY-DERIVED** — not because the data isn't XRPL consensus (it is), but because the *retrieval mechanism* is a curated archive controlled by a third party, not a first-order consensus read from our own node.

**Verdict: historical NFT activity (pre-cursor-seed date) is NOT sellable under this rule.**

**Two honest paths:**

1. **Free-forever designation.** NFT historical is a public good; keep it on the free surfaces (/nfts page when it ships, anomaly-scan reports) with an explicit attribution to Clio as the archive path. This is the recommended verdict.
2. **Someday-sovereign path.** Run our own full-history `rippled` node (with `online_delete=false` and enough disk for ~2M ledgers + growth). This is a multi-month, real-$$$ commitment on the Lenovo (or its successor) and is currently outside any active plan. Do not make this a paid-tier gating decision. If the anomaly-scan report reveals commercially valuable patterns and demand telemetry supports it, revisit at that time — not before.

**Standing decision:** NFT historical stays on the free side. Forward-only NFT activity (from cursor-seed date onward) is SOVEREIGN and could enter a paid tier, but the value of forward-only NFT data without the historical baseline is questionable — the anomaly scan needs the full range to work.

### § 0.5 — Hard case: Signed snapshots (mixed sources)

`signed_snapshot.py` orchestrates multiple upstream sources into a signed daily payload. The signature itself is sovereign by construction (our Ed25519 key, our RFC 6962 domain separation, our DNS TXT publication of the fingerprint). But the *payload* is only as sovereign as its upstream data.

Current signed-snapshot payload contains:
- Ledger index (from s1) — PUBLIC-INFRA-DEPENDENT
- Pool TVL from `amm_ranked.json` (via rank_amms → s1) — PUBLIC-INFRA-DEPENDENT
- MPT counts from mpt_snapshot (via mpt_data → s1) — PUBLIC-INFRA-DEPENDENT
- RLUSD state (XRPL side via cascade + Ethereum side via public RPC) — MIXED
- Others per current daily manifest

**Verdict: signed snapshots as a *product surface* stay free.** The proof-at-scale value proposition of § 1.C (bulk verification) is about verifying *our signatures*, which is sovereign by construction — the caller isn't buying the payload data, they're buying signature verification throughput. That is sellable. But the payload itself, sold as a product, is not — because a per-payload provenance breakdown would show mixed lineage today.

Path to fully sovereign signed snapshots: after Batch B, when upstream walkers all run through local rippled, the payload lineage tightens to (a) local-rippled XRPL data (SOVEREIGN) + (b) Ethereum public RPC data (permanently third-party for RLUSD Ethereum-side). At that point the *payload* is fully sovereign for XRPL-only slices — but that's not enough to make signed snapshots a paid product on its own; see § 1.

---

## § 1. The Four Candidates

Each candidate is evaluated on **feasibility × demand × cost**, with the sovereignty table above as the input filter. Verdicts are **kill / keep / hold**, not scores.

### § 1.A — Signed historical time-series API (lead candidate)

**Signing dimension (added 2026-08-03 per Grok evaluation — see `project_external_ai_evaluation_grok_2026-08-04.md`):**

Candidate A is not just "historical time-series API." It is **signed historical series**. Each series produces daily leaves (hourly where cheap), Merkle-chained and signed with the same Ed25519 key that signs the daily snapshot. Sellable-at-volume, while single-leaf spot-checks stay free.

This upgrade converges two independent paths:
- **Sovereignty-side (§ 0 of this memo):** we sell only what we source and prove ourselves. A signed envelope over sovereign data is the operational form of "prove ourselves."
- **Demand-side (external AI evaluation, 2026-08-03):** Grok, invited to deflate, independently identified signed history over the specific series agents care about as what agents would "pay for or preferentially cite." Two paths, one design answer.

**Sovereignty cross-check per series (from § 0 table — signing does NOT launder lineage):**

Sign only sovereign series. Signature over a PUBLIC-INFRA-DEPENDENT or THIRD-PARTY-DERIVED payload is a false claim of sovereignty and violates § 0.2 verdict. Series eligible for signed-history under this rule (post-gap-audit):

| Series family | Class today | Signable today? |
|---|---|---|
| RLUSD XRPL supply / net-change | SOVEREIGN | Yes (gap-audit gated) |
| Escrow, oracle, cold-storage snapshots | SOVEREIGN | Yes (gap-audit gated) |
| NFT forward-only (post-cursor-seed) | SOVEREIGN | Yes (limited standalone value) |
| R-alarm events | SOVEREIGN | Yes |
| Whale volume by tier | PUBLIC-INFRA-DEPENDENT | **Not until Batch B** |
| Top-N AMM TVL | PUBLIC-INFRA-DEPENDENT | **Not until Batch B** |
| MPT holder concentration | PUBLIC-INFRA-DEPENDENT | **Not until Batch B** |
| Amendment support history | PUBLIC-INFRA-DEPENDENT | **Not until Batch B** |
| RLUSD Ethereum-side supply | THIRD-PARTY-DERIVED | **NEVER** — signing over public-RPC data would sell PublicNode's / llamarpc's / Ankr's / 1rpc.io's reliability under our name |
| NFT historical (via Clio) | THIRD-PARTY-DERIVED | **NEVER** — signing over Clio-sourced data would sell Ripple's archive under our name |

**Scope note (honest):**

The signing machinery **exists**. `signed_snapshot.py` (785 lines, verified 2026-08-03) implements Ed25519 signing, RFC 6962 Merkle chaining, canonicalization, key custody, and per-day append. What the candidate-A signed-series build needs is:

1. **Per-series leaf schema** — one canonical JSON shape per series family (RLUSD, escrow, oracle, etc.). ~1-2 days per family after § 0.2 gap audit clears the series.
2. **Per-series chain storage** — either a chain-per-series or a namespaced global chain. Storage cost is trivial (leaves are ~1KB each; a year of daily leaves per series ~365KB); design cost is deciding which shape survives 10 years of retro-verification queries.
3. **Query envelope** — per-response signature over `{query_params_canonical, sha256(rows), row_count, ledger_range_min, ledger_range_max, as_of_ledger_index, leaf_audit_paths}`. Same Ed25519 key; verification uses same published pubkey and DNS TXT fingerprint.
4. **Serving route + auth + rate metering** — parked API-v1 scaffold covers the shape work.

**Honest total estimate:** 2-3 weeks after § 0.2 gap-audit clears one series family + attorney gate. First shipped series family becomes reference implementation; subsequent families ~1 week each.

**No new cryptography.** No new keys. No new DNS TXT record. The existing chain extends to per-series leaves.

**Sovereign-eligible series (per § 0):**

| Series | Class today | Depth measured | Depth sellable today |
|---|---|---|---|
| RLUSD XRPL supply/net-change | SOVEREIGN (via `get_client()`) | ~53 days confirmed (`rlusd_supply_history`) | Pending gap audit |
| Escrow snapshot | SOVEREIGN | Since escrow_walker deploy (~2026-06 mid) | Pending gap audit |
| Oracle snapshot | SOVEREIGN | Since oracle_walker deploy (~2026-06 mid) | Pending gap audit |
| Cold-storage balances | SOVEREIGN | Since cold_storage walker | Pending gap audit |
| Whale events | PUBLIC-INFRA-DEPENDENT | ~93 days back | **Not sellable until Batch B + gap audit** |
| Token volumes | PUBLIC-INFRA-DEPENDENT | ~93 days back | **Not sellable until Batch B + gap audit** |
| AMM/pool TVL | PUBLIC-INFRA-DEPENDENT | Varies per pool | **Not sellable until Batch B + gap audit** |
| MPT snapshots | PUBLIC-INFRA-DEPENDENT | Hourly, since MPT walker deploy | **Not sellable until Batch B + gap audit** |
| RLUSD Ethereum-side | THIRD-PARTY-DERIVED | Any | **Never sellable** |
| NFT historical | THIRD-PARTY-DERIVED (Clio) | ~2M ledgers back to 2026-04-01 (in progress, ETA ~2026-08-16) | **Never sellable** (per § 0.4) |
| NFT forward-only | SOVEREIGN | Since cursor-seed date | Limited standalone value |

**What's missing:**
- **Per-row lineage stamps.** Historical rows don't record which endpoint served them; without that, gap audit is forensic, not decidable. Add a lightweight `source_endpoint` column to each history table on re-collection. Low cost.
- **Gap-audit tooling per series.** One SQL per series, standardized shape (see § 0.2 verdict). Blocking dependency on any historical API launch.
- **Envelope shape.** Sketch: per-response signature over `{query_params_canonical, sha256(rows), row_count, ledger_range_min, ledger_range_max, as_of_ledger_index}`. Verifiable by any caller with our published Ed25519 public key. Signature key can be same as snapshot signing key or a distinct API key — attorney gate applies.

**Build scope (post-Batch-B, post-gap-audit):**
- Route + params + envelope + auth + rate metering + docs. Existing API v1 parked branch (`parked/api-v1-scaffold @ 2b5eb76`) covers the shape work.
- Estimate: 2-3 weeks of focused work after the sovereign-eligibility gates pass.
- Cost: Neon query time (already paid), signature compute (trivial), no new infra.

**Demand evidence standard:**
- `ai_crawler_hits` telemetry (Phase 2 live 2026-08-02) — need distribution readable ~2026-08-16 and post-kill-criterion ~2026-09-01 (see `project_two_instruments_2026_08_ship_of_record.md`).
- Concrete inbound asks: minimum N=3 credible requests (email, GitHub issue, Twitter DM from an account with verifiable production use) before build starts.
- If both signals show demand: candidate A moves from HOLD to KEEP and enters the build queue behind attorney answers.

**Y1 revenue expectation (calibrated 2026-08-04 per fresh-Claude Part 2 web-searched evidence — `[FROM-REVIEW-CITED-SEARCH]`):**

- **Realistic band: low hundreds to low thousands of USD in year one.** Not low tens of thousands.
- **Grounding**: sector-wide MCP monetization rate is <5% of MCP servers running. Per-call MCP pricing at $0.07/call has been offered publicly with zero paying customers on record. The market has not yet agreed on the shape it will pay for.
- **Implication**: candidate A survives on customer-count × ARPU, not on volume. If year-one ships with 3-5 relationship-sold customers at flat-fee tier subscriptions (e.g., $200-1,000/month), that is on-band success — not a signal to accelerate build.
- **Kill trigger**: if candidate A ships and returns 0 paying customers in the first 90 days despite active outreach, HOLD-again, do not iterate the product surface (the market shape is wrong, not the product shape).

This calibration does not change the HOLD verdict. It changes what "success" looks like when candidate A eventually flips HOLD → BUILD. It also validates the memo's overall "trust layer is a 2027+ story" framing at the candidate level.

**Verdict: HOLD.** Highest-value near-term product IF sovereign eligibility clears AND demand signal materializes. Do not build now; do not kill.

### § 1.B — Webhooks / push delivery

**Sovereign-today event streams:**
- **R-alarm events** — SOVEREIGN by construction. Small volume, high signal for infrastructure-alerting use cases.
- **Amendment change events** — PUBLIC-INFRA-DEPENDENT today; Batch B lifts to SOVEREIGN. Very low volume (amendment status changes are rare).
- **Whale threshold events (≥ 100k XRP)** — PUBLIC-INFRA-DEPENDENT today; blocked on the same Batch B migration as candidate A plus the reconnect-with-backfill fix from the AI cross-verify earlier today.

**Delivery infra scope:**
- HTTP POST to subscriber-registered URLs with retry, idempotency keys, dead-letter queue.
- **Signed envelope on every payload** — this is the differentiator. Subscriber verifies each webhook payload against our published Ed25519 key without needing to call back. Cannot be replicated by any competitor that doesn't publish a signed proof chain.
- Estimated 1-2 weeks including retry/DLQ.

**Cost:**
- Compute: trivial.
- Reliability posture (retry, DLQ, sub-second latency SLO) is the real cost. Committed reliability is expensive to operate for a solo builder.

**Demand evidence standard:**
- **Lower demand ceiling than candidate A.** Webhooks are a specialist need (alerting integrations, custom bot infra). Estimate: N=2 concrete asks in 90 days would be a strong signal.

**Verdict: HOLD, behind candidate A.** Ship as follow-on after candidate A validates the paid-tier appetite. Don't build parallel infrastructure.

### § 1.C — Bulk verification (signature verification throughput)

**What's sellable:**
- Verifying *our signatures* over *our snapshots* — SOVEREIGN by construction (our key, our signed data).
- The caller is buying signature verification throughput, not the underlying payload.

**Volume threshold shape:**
- Free below X req/day/IP (X ~= 100, matches existing rate-limit patterns).
- Metered above the threshold. Attorney gate: pricing metadata + refund semantics.

**Attorney dependency:** light. FCRA-adjacent only if callers use bulk verification in credit decisions, which is unlikely for signature verification specifically — flag but don't block.

**Build scope:**
- Auth (bearer token or API key), rate limiter with cost accounting, billing hook (post-attorney).
- Small: ~1 week after auth infra decisions are made.

**Demand evidence:**
- **Currently zero.** `/snapshots/verify` gets 0 hits/day per the last observed logs. There is no meaningful audience for signature verification throughput yet.
- This may change if the site becomes a citation source at scale (agent-tier crawls demonstrating audit chains).

**Verdict: KEEP AS DESIGN, DON'T BUILD.** Cleanest candidate under the sovereignty rule. But shipping before demand exists = building for no one. Ship only if candidate A validates the market and callers ask for bulk verification specifically.

### § 1.D — x402 per-call micropayments on MCP tools

**Status refresh (searched 2026-08-03):**
- x402 Foundation launched by Coinbase + Linux Foundation on **2026-04-02**.
- Adoption: **~100M cumulative transactions on Base in Q1 2026**; later Coinbase cite of ~165M transactions across 480k+ agents; ~$50M in last-30-day transactions.
- **Base (Coinbase L2) is the primary deployment.** Blockchain-agnostic in spec; Solana and Polygon also mentioned. **No XRPL integration found in search results.**
- Stripe shipped x402 support Feb 2026 (USDC on Base). Cloudflare Agents SDK supports x402 for live Base Sepolia transactions.

**Sovereign filter:** only tools whose data source is SOVEREIGN-classified could be metered. Under § 0's verdict, that limits candidate D to:
- RLUSD XRPL-side tools
- Escrow / oracle / cold-storage tools
- R-alarm tools
- (Post-Batch-B) whale events, token volumes, MPT snapshots, amendments, AMM data
- (Never) RLUSD Ethereum, NFT historical

**Heaviest attorney dependency of the four:**
- Money-transmission classification — receiving stablecoin micropayments as a US solo LLC / individual.
- KYC accumulation at $0.10-scale volumes.
- Sales/use tax on digital services delivered via x402.
- Custody of received USDC/stablecoins (self-custody vs. Coinbase-facilitator flow-down).
- **Refund and dispute-resolution posture** for machine-only payments.

**XRPL-native consideration:** the search surfaced no active XRPL integration with x402. If the paid tier ever ships on x402, the most likely deployment is USDC on Base (not RLUSD on XRPL) — which creates a strategic oddity: an XRPL data product paid for on a non-XRPL chain. This is not disqualifying (many XRPL projects accept USDC), but worth naming.

**Demand:** speculative. Coinbase's own numbers show real usage, but the demand shape for *XRPL data via x402* is unmeasured. The `ai_crawler_hits` distribution readable 2026-08-16 will inform this, but at a lower resolution than for candidate A.

**Step-change requirement (added 2026-08-04 — `[FROM-REVIEW-CITED-SEARCH]`):**

- **XRPL-ecosystem x402 volume baseline: ~$28K/day** across the entire XRPL x402 footprint as of the fresh-Claude Part 2 read (Q3 2026).
- **Threshold to flip HOLD → BUILD-EVAL:** the XRPL-ecosystem x402 daily-volume figure must show a **step-change** (e.g., ≥10× to ≥$280K/day, sustained ≥30 days) AND at least one credible XRPL-native x402 integration must ship AND candidate A must be revenue-positive first. All three conditions, not any one.
- **Why this bar is high:** the current $28K/day baseline across the entire XRPL x402 ecosystem is too small to justify the payments/tax/custody attorney work that candidate D uniquely requires. A 2× or 3× move is within noise; a 10× move is a market-shape change.
- **Revisit cadence:** re-read the volume figure at the Q4 2026 decision date (§ 2). If the step-change condition is not met, HOLD extends through Q1 2027 by default.

**Verdict: HOLD, hardest attorney gate of the four.** Do not build. Watch the x402 adoption curve through Q4 2026 for XRPL-side integrations. Revisit no earlier than candidate A shipping and generating revenue AND the step-change condition above.

### § 1.E — Dataset licensing for AI evaluation and training (added 2026-08-03 per ChatGPT evaluation — see `project_external_ai_evaluation_chatgpt_2026-08-04.md`)

**Target market — narrow, on purpose:** NOT generic crypto-data buyers. Specifically:
- Model-evaluation shops building hallucination benchmarks on crypto-domain LLMs (verifiable ground truth for eval corpora).
- Crypto-domain-LLM builders needing citation-anchored training or fine-tuning corpora with per-fact provenance.
- Compliance/audit vendors building AI-assisted forensics workflows that require reproducible receipts.

The customer isn't "someone who wants XRPL data." It's "someone building or evaluating AI systems who needs verifiable ground truth to test against or ground on." This scoping matters — it protects candidate A from being confused with a general data-licensing play.

**Product shape (sketch, not spec):**
- A curated corpus of `(claim, evidence, snapshot_hash, methodology, confidence)` tuples over SOVEREIGN series only. Per-tuple lineage stamps.
- Delivered as versioned dataset releases (parquet + signed manifest) with reproducibility receipts — each release verifiable against the live signed-snapshot chain at the release's ledger cutoff.
- License terms name eval-and-training use explicitly. Redistribution of the corpus is separately gated.

**Sovereignty filter:** identical to candidates A/B/C — only SOVEREIGN series enter the corpus. RLUSD Ethereum-side and NFT historical stay out permanently (per § 0.3, § 0.4). This is not a workaround for the sovereignty rule; it's an application of it to a different customer segment.

**Explicit non-overlap with candidate A:** dataset licensing is *bulk corpus release for training/evaluation*, sold under a data-licensing contract. Candidate A is *live-query signed history* over the same underlying series, sold as API access. Same source data; different products; different contracts; different customer psychology (dataset buyer wants a stable snapshot; API buyer wants freshness).

**Attorney weight — heaviest of the five candidates, distinct from the others.**
- Data licensing contract law (redistribution terms, derivative-work carve-outs, upstream-liability disclaimers).
- IP posture on our own methodology (are our derivation steps a licensable process, or just an unprotectable observation?).
- Warranty language on "verified ground truth" — false-claim exposure if a downstream benchmark exposes an inaccuracy.
- Cross-jurisdictional buyer risk (AI eval shops often HQ outside US).
- Distinct from candidate D's payments/tax gate and candidate A's rate-metering gate. Attorney answers do not transfer; this candidate needs its own set.

**Demand evidence:**
- Zero measured today. No inbound asks in the "AI eval corpus" shape. The ChatGPT evaluation flagged this segment as a plausible market — one external voice, no customer voices yet.
- Signal path: watch AI-benchmark repos (Hugging Face evals, `lm-eval-harness`-style leaderboards) for crypto-domain entries. Watch for direct asks referencing eval-corpora specifically.

**Sales-motion reframe (added 2026-08-04 — `[FROM-REVIEW-CITED-SEARCH]`):**

- **Not marketplace-listable. Relationship-driven, manual sale.**
- **Deal size band: $2K-20K per deal**, closed 1-to-1 with eval-lab / benchmark-builder counterparts. No self-serve tier. No dataset shopping cart.
- **Sales motion:** direct outreach to named eval-lab teams (Hugging Face benchmark maintainers, `lm-eval-harness` contributors, crypto-domain LLM builders with published training-set provenance) with a scoped corpus proposal. Response rate is the demand signal; deals close over weeks, not clicks.
- **Y1 realistic band:** 0-2 deals. This is a HOLD candidate for a reason — the addressable buyer list is small, the deal cycle is long, the attorney gate is heaviest of any candidate. Zero deals in Y1 is on-band.
- **Implication for candidate A separation:** the manual-sale motion further reinforces § 1.E ≠ § 1.A. Candidate A is a subscription API; candidate E is a bespoke corpus release. Different customer teams inside the same company might buy each — that's fine, keeps them separately gated.

**Verdict: HOLD — watch-and-probe.** Do not build. Do not amend candidate A to absorb it. If (a) candidate A ships and (b) at least one credible eval-corpus ask arrives referencing verifiable XRPL ground truth by name, revisit with a scoped attorney consult AND scope the first deal as a manual sale, not a listed product. Until then, this candidate is a memo entry, not a product.

### § 1.F — Cloudflare: two sub-candidates (split enacted 2026-08-04 per `docs/CLOUDFLARE_PPC_RESEARCH_2026-08-04.md`)

The initial fresh-Claude read treated Cloudflare Pay Per Crawl as a single candidate. One session of research (msg 10622) found Cloudflare bifurcated into two products with opposite verdicts for xrpldashboard. Original § 1.F entry preserved in commit `539d4a9`; research doc is the audit trail; this section enacts the split Charlie approved.

#### § 1.F.1 — Pay Per Crawl (402-per-request model, launched July 2025)

**Verdict: CONFLICTS-WITH-FLYWHEEL-SKIP.**

**What it is:** site operator sets a per-crawl price; crawlers send 402 if they don't present payment intent. Cloudflare's Allow/Charge/Block operates at verified-bot level.

**Why we skip:**
1. **Eligibility gate is not zero-cost.** Requires Cloudflare paid plan tier for the 402 feature + a separate private-beta application (`cloudflare.com/paypercrawl-signup/`) for full monetization. Not a free-plan dashboard toggle.
2. **Flywheel conflict is direct.** Our Day-6 identified-crawler tier (`agent_tier_rate_limit.py:73-99`) deliberately allows 15 citation-crawler UAs (OAI-SearchBot, ClaudeBot, PerplexityBot, Google-Extended, etc.) at 300 req/min + audit-URL header pointing at `/coverage`. The point is to feed the citation flywheel before we've read a single week of data (first crawler-harvest read: Friday 2026-08-08). Cloudflare's Allow/Charge/Block granularity is coarser than our citation/training distinction; enabling PPC risks 402-ing the exact crawlers we're deliberately feeding.
3. **Revenue floor doesn't clear plan-upgrade break-even.** Expected low tens to low hundreds of USD/year at current traffic scale. Does not justify upgrading the Cloudflare plan tier.
4. **Cloudflare's own signal.** On July 1, 2026, Cloudflare called the 402 model "a first step" and argued "crawling is a poor proxy for value." Following a product the vendor is de-emphasizing is a poor bet.

**Re-open trigger:** if (a) Pay Per Crawl graduates to free-tier availability AND (b) Friday's crawler-harvest read shows the citation flywheel is producing measurable citations AND (c) Cloudflare's granularity allows allowlisting citation crawlers while charging training-only bots — all three, not any one.

#### § 1.F.2 — Pay Per Citation (Cloudflare pivot announced July 1, 2026)

**Verdict: OPT-IN-LATER-AT-BROAD-AVAILABILITY.**

**What it is:** publishers who opt in are paid when their content appears in AI answers (Ceramic.ai + You.com as initial partners). No crawl-level 402; compensation arrives at the citation event, not the fetch event.

**Why this fits our strategy exactly:**
- Citation = the value axis the flywheel is building toward. Pay Per Citation monetizes success at the same event we're optimizing for.
- Respects all five sovereignty rules: data stays free at the source; Cloudflare / Ceramic / You.com pay from their answer-monetization side; no payment plumbing added on our end.
- Zero conflict with identified-crawler traffic — more citation crawl activity → more citation events → more payments. Flywheel and revenue are additive, not opposed.

**Attorney weight:** light. Publisher opt-in to a Cloudflare-brokered arrangement. No money-transmission exposure on our side. No custody, no USDC.

**Why we don't opt in today:** no publisher enrollment surface exists yet. Cloudflare says "broad availability later in 2026, without a specific date." Not actionable until the enrollment path ships.

**Watch trigger (concrete):** revisit at **2026-11-01** — matches the memo's 60-90d demand-window decision date. If Pay Per Citation has a public publisher opt-in surface, evaluate sovereignty fit and opt in immediately.

**Kill trigger:** 90 days past 2026-12-31 with no publisher opt-in path published → close watch, mark CLOSED.

**Explicit non-overlap with candidates A-E:** Pay Per Citation pays on citation appearance from the Cloudflare / answer-engine side. Candidate A sells signed-series API access. Different payment mechanics, different customers, different infrastructure — they can co-exist.

---

## Summary matrix

| Candidate | Feasibility (post-Batch-B) | Demand today | Attorney weight | Verdict |
|---|---|---|---|---|
| **A. Signed historical time-series API** | Medium (gap audits blocking) | Unmeasured (`ai_crawler_hits` reading 2026-08-16) | Medium | **HOLD — lead** |
| **B. Webhooks / push** | Medium (delivery reliability) | Lower ceiling than A | Medium | **HOLD — behind A** |
| **C. Bulk verification** | High (sovereign by construction) | Zero today | Light | **KEEP AS DESIGN, don't build** |
| **D. x402 micropayments on MCP** | Medium (no XRPL x402 integration observed) | Speculative | Heaviest (payments/tax/custody) | **HOLD — watch curve** |
| **E. Dataset licensing for AI eval/training** | Medium (corpus curation + signed release infra) | Zero measured (one external voice) | **Heaviest (data-licensing contract + IP + warranty)** — distinct set from candidates A-D | **HOLD — watch-and-probe** (relationship-driven, $2K-20K/deal — see § 1.E reframe) |
| **F.1 Cloudflare Pay Per Crawl (402 model)** | Low (paid-plan-gated + private beta application) | Speculative (low tens to low hundreds/year) | Light (Cloudflare owns payments) | **SKIP — conflicts with citation flywheel** |
| **F.2 Cloudflare Pay Per Citation (July 2026)** | Low today (no publisher enrollment path yet) | Aligned with flywheel (pay on citation = our success axis) | Light (publisher opt-in, Cloudflare-brokered) | **WATCH — opt in at broad availability (trigger 2026-11-01)** |

---

---

## § 2. Demand Evidence Framework

**Instruments (both already shipping):**
- `ai_crawler_hits` telemetry — Phase 2 deployed 2026-08-02 (`d048444`). Fields: `ts / ua_class / path / status`. Distribution readable from 2026-08-09 to 2026-08-16; kill-criterion ~2026-09-01 (see `project_two_instruments_2026_08_ship_of_record.md`).
- Agent-tier MCP session logs — MCP surface shipped 2026-08-02 (per `docs/AGENT_TIER_DESIGN.md`). Session count, tool-call count per tool per day, unique client fingerprints (anonymous — no query retention per design doc).
- Inbound direct asks — an email address, GitHub issue thread, and a single line in `llms.txt` inviting expressions of interest (see § 6 WTP probe).
- **External-AI evaluator convergence (qualitative demand signal, added 2026-08-03):** an outside AI evaluator (Grok, invited to deflate) independently identified signed history over specific series as the feature agents would "pay for or preferentially cite" — arriving at candidate A's shape from the demand side while this memo arrived at the same shape from the sovereignty side. Two independent paths landing on the same design is a demand datapoint from the demand-modelling side itself. Not sufficient alone; sits alongside the mechanical thresholds below. See `project_external_ai_evaluation_grok_2026-08-04.md`.

**Decision window:** 60-90 days from agent-tier ship (2026-08-02) → decision date **2026-10-01 to 2026-11-01**. The decision reads from the table below; it does not go to debate.

**Mechanical go-thresholds (per candidate):**

| Candidate | Fetch volume (agent-tier crawl or MCP call) | Distinct agents (30d window) | Direct asks (60-90d) | Session growth (MoM) | Go |
|---|---|---|---|---|---|
| **A. Signed historical time-series API** | ≥ 100 identified-agent hits/day on `/api/history/*` or MCP `get_history_*` tools, sustained 14d | ≥ 20 unique agent fingerprints | ≥ 3 credible inbound (email/GitHub/verifiable prod use) | ≥ 25% MoM for 2 consecutive months | **ANY three of four** |
| **B. Webhooks / push** | Not primary signal (webhooks are pull-registered, not crawl-detected) | ≥ 5 unique candidates via inbound | ≥ 2 credible "we want event streams" asks with named use case | n/a | **BOTH direct-ask thresholds** |
| **C. Bulk verification** | ≥ 50 hits/day on `/snapshots/verify` sustained 30d (currently 0/day) | ≥ 10 unique verifying agents | ≥ 1 concrete integration ask | n/a | **fetch volume OR direct ask** |
| **D. x402 micropayments** | (a) XRPL-native x402 integration observable in wider ecosystem AND (b) candidate A already shipped and revenue-generating | ≥ 3 direct asks specifically referencing x402 payment for xrpldashboard data | n/a | n/a | **all three** |

**No candidate below its threshold moves to build.** If thresholds partial, the memo re-opens; no verbal-approval workaround. The Phase 2 telemetry design was built for this decision — trust the instrument.

**Deliberate omissions from the metric set:**
- Anonymous crawler counts without agent identification (bot fleet noise; already reclassified by `is_bot` column work).
- Total session counts without per-tool breakdown (a single agent making 10k calls to `get_ledger_stats` is not demand for candidate A).
- Twitter mentions / press coverage (visibility, not demand).

---

## § 3. The Free-Tier Covenant

**Public commitment text (marketing-asset shape, quotable on the site):**

> **Everything on xrpldashboard.com today stays free, forever.** Every page, every dataset, every signed snapshot, every agent-tier MCP tool that ships in 2026 — no matter what future paid product exists, none of these become paid. Ever.
>
> **We sell only what we source and prove ourselves.** Our paid products, if we ship them, will always be limited to data we compute on our own node from XRPL consensus, verified and signed with our own key. We will never charge for someone else's work under our name. Data we don't source ourselves — RLUSD's Ethereum-side supply from public gateways, historical NFT activity retrieved from third-party archives — stays free with attribution. Always.
>
> **Any paid tier we build adds capacity for machine consumers who need volume, guaranteed delivery, or bulk cryptographic verification.** It does not remove capacity from the free surface. If a small paid feature would ever pinch the free experience, we don't ship it.

**Enumerated free-forever surfaces (locked as of 2026-08-03):**

*Dashboard pages:*
- `/`, `/whales`, `/tokens`, `/pools`, `/wallet`, `/rlusd`, `/rwa`, `/mpts`, `/cold-storage`, `/amendments`, `/lending`, `/health`, `/coverage`, `/regulation`, `/check`, `/analytics`, `/security`, `/methodology`, `/about`, `/privacy`, `/snapshots/`, `/snapshots/verify`.
- Any `/learn/*` pages (currently one shipped, more parked).

*Machine-consumption surfaces (agent tier, shipped 2026-08-02):*
- `llms.txt`, `agents.json`, `openapi.json`, `/api/*` GET endpoints exposed by agent-tier.
- MCP server (stdio + streamable HTTP) — every tool in the v1 inventory per `docs/AGENT_TIER_DESIGN.md`.
- OpenAPI/Swagger docs.

*Signed proof surfaces:*
- Signed daily snapshots at `/.well-known/snapshots/*.json`, chain.json, pubkey.pem.
- DNS-TXT-published fingerprint at `_xrpld-snapshot-key.xrpldashboard.com`.
- `/snapshots/verify` interactive form for one-off verification.

*Third-party-derived series (free-with-attribution, forever):*
- RLUSD Ethereum-side supply (source: public Ethereum RPC gateways, attributed on `/rlusd` and methodology).
- Historical NFT activity pre-2026-08-16 cursor-catch-up (source: Ripple's Clio archive, attributed on `/nfts` when it ships and on methodology).
- Any future series whose lineage classification is THIRD-PARTY-DERIVED per § 0.

**Cross-check with Glow disclosure (`docs/GLOW_APPLICATION_DRAFT.md`, commit `986c966`):**
- Glow lines 230-237: *"a paid agent-tier is under consideration ... nothing shipped in the retroactive window is paywalled, and no paywalling of shipped work is planned ... the tier structure, if it ever exists, will add capacity for paying agents on top of an unchanged free surface, not remove capacity from the free surface."*
- **This memo's covenant is a strict superset of the Glow disclosure.** Zero daylight. The Glow text stands unchanged; this covenant extends it with the sovereignty rule as an additional public commitment.

### § 3.1 — Free-tier substrate for the paid tier: the queryable claims layer

*Added 2026-08-03. Reframed from backlog nugget → Phase 3 dependency after both Grok and ChatGPT external evaluations independently proposed a queryable claims surface (see `project_external_ai_evaluation_grok_2026-08-04.md`, `project_external_ai_evaluation_chatgpt_2026-08-04.md`).*

**This is free-tier machinery.** It never enters a paid product. It is documented in this memo because candidate A is not shippable without it: absent a discoverability layer, agents cannot learn which claims are currently backed by SOVEREIGN data and therefore currently buyable through candidate A. The paid tier presumes this substrate.

**Design unit — three pieces, one build:**

1. **Stable resolvable claim URIs.** One per claim in `CLAIMS.yaml` (currently 1000+ lines of latent value with no agent-consumable surface). Proposed scheme: `/claims/<namespace>.<domain>.<series>` — e.g., `/claims/xrpl.rlusd.supply`, `/claims/xrpl.escrow.count`, `/claims/xrpl.mpts.holders`. **The URI scheme is permanent once agents cite it** — pick deliberately. Namespace convention decision goes in the build's design pass, not this memo.
2. **Per-claim status JSON.** Returned when the URI is fetched. Traffic-light shape:
   - `green` — claim is currently backed by SOVEREIGN data with a passing gap audit. Currently sellable through candidate A if it ships. Signable.
   - `yellow` — claim is PUBLIC-INFRA-DEPENDENT (pending Batch B migration) OR gap audit incomplete. Free-surface only until upgraded.
   - `red` — claim is THIRD-PARTY-DERIVED (permanently free-only per § 0.3/§ 0.4) OR claim currently failing / paused / retired.
   Status fields include: current classification, last-verified timestamp, methodology URL, snapshot signature reference where applicable.
3. **Page-side atomic citations.** On `/rlusd`, `/whales`, `/mpts`, etc., specific numbers deep-link to the specific claim URI + snapshot hash on the same page render. Replaces page-level footnote citation (Weak: "Source: XRPL dashboard") with fact-level citation (Strong: `Claim ID + Evidence + Snapshot hash`). Cheap UI change; large semantic shift for AI evaluators.

**Three payoffs, one design:**

- **Discoverability substrate for candidate A.** Agents discover which series are `green` before hitting the paid tier — the "what's currently sellable / verifiable" navigation layer.
- **Latent CLAIMS.yaml value exposed.** The manifest already exists (1000+ lines) but is only agent-consumable if you parse YAML at retrieval time and know the file path. URIs + status JSON make it consumable through the same tool-response pattern as everything else agent-tier already ships.
- **Fact-level citation UX.** Addresses ChatGPT weakness #2 (unknown-authority problem) and weakness #3 (MCP outputs must be citation-native) at the same time. Grok independently asked for this in different language (queryable manifest with rate-tiered access).

**Not new capability.** Surfaces existing capability. The atoms are all there — walkers, CLAIMS.yaml, methodology chips, signed snapshots. The build is the queryable navigation layer over the atoms.

**Cost:** low compared to any § 1 candidate. **Attorney weight:** none (free-tier, no payments, no data licensing). **Placement:** Phase 3 free-tier substrate. Ships **before or alongside** candidate A. **Never sold.**

### § 3.2 — The envelope contract: confidence enum discipline

*Added 2026-08-03 per ChatGPT evaluation gap #4 (envelope confidence field).*

The `ProofAnnotationEnvelope` (declared at `app.py:6292-6307`) currently carries `source / as_of / methodology_url / claims_ref / snapshot_signature`. The envelope-contract build (next in the standing build queue) adds two fields already partially wired in `mcp_server.py`:

- `honest_partial: bool` — automatically `True` on any-null result surface, so partial answers are structurally distinguishable from complete ones. Never fabricated union / overlap / cross-chain totals when a sub-source failed.
- `scope_note: str` — required (via existing `ValueError` at `mcp_server.py:155`) whenever `honest_partial=True`. Names precisely which sub-source is missing.

**This memo adds a third field with a strict spec: `confidence` — categorical enum only.**

**Enum values (finalized 2026-08-03):**

- `signature_verified` — snapshot signature valid; datum is snapshot-derived and cryptographically verifiable.
- `cross_checked` — `cross_check_walker` (or equivalent independent verification path) independently agrees with our walker output. Corroborated but not signed.
- `walker_computed` — our walker computed the value; no external corroboration path exists or has run.
- `single_source` — value depends on a single upstream we could not corroborate at query time (e.g., one of two RPC pool responses succeeded; the other failed). Weakest honest label.

**Prohibition (written into the spec):**

> Numeric confidence values (e.g., `"confidence": 0.91`) are prohibited absent a calibration model. We do not currently run calibrated probability models over any of our lineage paths; publishing a numeric confidence would be a false claim of calibration. The enum is the ceiling.

If a future build introduces a genuine calibration model (statistical, not vibes-based) for a specific series, the enum extends — but numeric values only enter through a distinct field name (`calibrated_probability` or similar) with a linked calibration methodology page. The `confidence` field never carries numeric values.

**Placement in build queue:** the `confidence` field ships with the envelope-contract fix — same build as `honest_partial: true on any-null` and `scope_note` population expansion. Envelope-fix is queue-position 1 in the current standing build queue.

---

## § 4. Competitive Landscape + Pricing Sanity

### § 4.1 — What XRPL peers charge (and what they source)

| Competitor | Free tier | Paid tier | Own node? | Proof-annotation? | Signed history? |
|---|---|---|---|---|---|
| **Bithomp** | 10 rpm / 2,000 rpd | Multiple tiers, prices not on public docs | Undisclosed (backend service; runs their own indexer) | No | No |
| **XRPScan** | Free for OSS/academia/students | Scales to paid; prices not on public docs | **Yes — runs a validator on XRPL** per methodology + Crunchbase | No | No |
| **xrpl-utilities.com** | Freemium | 6 paid products, SDK, MCP proxy — per prior competitive teardown (see `docs/AGENT_TIER_DESIGN.md` motivation § ) | **No — 34 KB proxy; no indexer, no walkers visible; 0 GitHub stars** (per prior teardown) | No | No |
| **xrpldashboard (us, today)** | Everything free | (Not shipped) | **Yes — walker fleet on local rippled; browser layer honestly attributed to Foundation cluster** | **Yes** (proof-annotation is the site's thesis) | **Yes** (signed daily snapshot chain w/ DNS fingerprint) |

**The contrast is now product.** None of the three peers offer proof-annotation, none publish a signed history chain, and (except XRPScan's validator) none source their own data at the node layer. This positioning is not a nice-to-have; it is the pitch:

> *We're the only XRPL data provider whose paid product sells only what we source ourselves — and we prove it, per query, with a signature you can verify against our published key.*

### § 4.2 — Broader agent-data-API price benchmarks (2026)

Per web search 2026-08-03:

- **CoinGecko:** Free 100 calls/min, 10K/month cap. Paid entry $35/mo → 300 calls/min, 100K/month. ([source](https://www.coingecko.com/en/api/pricing))
- **CoinStats:** Free 20K credits/month; Entry plan $49/month. ([source](https://coinstats.app/blog/best-crypto-api/))
- **Blockdaemon:** Starter tier up to 65M compute units/month, 100 rps. Growth tier 365M compute units, 200 rps. ([source](https://www.blockdaemon.com/api/pricing))
- **Industry trend:** credit-based pricing is displacing per-call. Endpoints assigned credit weights; heavy queries cost more. Light call ~1-2 credits, deep DeFi query hundreds.

### § 4.3 — First-guess price shape for candidate A (benchmark, not proposal)

If candidate A ever ships:
- **Free tier retained (not reduced):** current agent-tier rate limits stay at their free-forever level.
- **Metered tier benchmark:** $29-49/mo entry, credit-based, mirroring CoinGecko/CoinStats entry-price norms.
- **Volume tier:** custom quote for repeat-use agents; probably $250-500/mo range if benchmarking against Blockdaemon Starter.
- **x402 per-call (candidate D, if ever):** $0.001-0.01 per credit-weighted call, USDC-on-Base at initial launch (no XRPL x402 integration observed 2026-08).

**Explicit non-decisions in this memo:**
- These are benchmarks, not prices. Prices depend on the attorney answer, on demand telemetry, and on Neon/Render cost accounting at the paid-tier launch date.
- No discount tiers, no annual pricing, no enterprise conversation happens in this memo. That work belongs after the go decision.

---

## § 5. Risks (named, not softened)

### § 5.1 — Goodwill cannibalization

The strategy sentence promises *depth, delivery, and proof-at-scale* metered — but *depth* is exactly what advanced free users appreciate about the site today. If the line is drawn wrong, the paid tier eats the community-goodwill differentiator that made the AI-prober review positive (see `project_external_ai_prober_results_2026-08-03.md`).

**Mitigation:** the covenant text in § 3 enumerates free-forever surfaces. Any candidate that would touch an enumerated surface is disqualified without further discussion. New surfaces built explicitly for the paid tier (e.g., a `/api/history/*` route that didn't exist free) do not cross this line.

### § 5.2 — Paid SLA vs. walker reality

`walker_health` data from the last 30 days is the honest baseline for any SLA conversation. Fleet-wide uptime is not the same as candidate-A endpoint uptime, and the API tier's SLA — if we offer one — must reflect the walker actually powering the answer, not the fleet aggregate.

**Baseline check required before publishing any SLA:** for each candidate-A series, compute the underlying walker's 30-day `ok / total` ratio from `walker_health`. If a walker's 30-day availability is below 99.0%, that series does not go into a paid product with a monetary-credit SLA. Better to publish a "no uptime promise v1" and offer refunds on request than to promise 99.9% and pay credits on a fleet that only hits 98.5%.

### § 5.3 — Sovereignty timeline risk

Multiple candidates are gated on migrations that are themselves gated on operational events:

| Series | Gates before it's sellable |
|---|---|
| Whale events, token volumes | Batch B post-soak decision (~2026-08-31) + `xrpl_stream.py` gap-audit + reconnect-with-backfill build (per AI cross-verify 2026-08-03) + Lenovo WS exposure config check |
| AMM/pool, MPT, amendments, network-pulse | Batch B env-var normalization (per `project_walker_rpc_lever_normalization_backlog.md`) |
| NFT historical | Own-full-history-node decision (multi-month, real-$, no plan yet) — realistically **never** on 2026 timeline |
| RLUSD Ethereum-side | Own-Ethereum-node decision (out of scope) — **never** |

**Honest sequencing:** the sellable-at-decision-date (2026-10-01 to 2026-11-01) set is smaller than the sellable-in-principle set. The go/no-go on candidate A should read the smaller set:
- **SOVEREIGN today (sellable if gap audit passes):** RLUSD XRPL-side, escrow, oracle, cold-storage, NFT-forward-only, R-alarm events.
- **Sellable if Batch B lands + gap audit passes:** whale events, token volumes, MPT, amendments, AMM/pool, network-pulse.
- **Never sellable under the rule:** RLUSD Ethereum-side, NFT historical.

If the go decision fires at 2026-10-01 and Batch B has not landed, candidate A ships with a narrower dataset than the strategy vision.

### § 5.4 — Single-operator risk

If Charlie is unavailable (hardware failure, hospital visit for family, extended travel), a paying customer has no fallback. This is a hard problem for a solo-builder paid product.

**v1 posture (honest, publishable):**
- **Status page** at `/status` — pulls `walker_health` and displays current fleet state. Not a promise; a receipt.
- **No SLA credits in v1.** Refund on request only, defined in the paid tier's ToS with a clear response window (e.g., "Refund requests answered within 7 business days").
- **No uptime promises v1.** The methodology page currently says "hosting and infrastructure currently run ~$25/month" — that same honesty extends to "we do not currently offer SLA credits or uptime guarantees on the paid tier; we offer refunds on service issues at operator discretion."
- **Communication commitment:** any paid customer gets a 1-line email address for issues (goes to Charlie's inbox), no ticket system, no promise of hours, but a promise of *reply*.

The point isn't to disclaim responsibility. The point is to **not promise what a solo builder can't deliver.** A quiet, no-SLA v1 that responds honestly is a stronger long-term posture than an aggressive SLA that pays credits on missed uptime we can't guarantee.

---

## § 6. Recommendation (ranked by evidence-of-demand ÷ build-cost)

**Definitions:**
- **Evidence-of-demand (E):** score 0-3 based on current signals. 0 = zero measurable demand. 3 = validated inbound + observable crawler distribution + N≥3 direct asks.
- **Build-cost (C):** score 1-3. 1 = one-week or less. 2 = 2-4 weeks. 3 = 4+ weeks OR blocked on multiple upstream gates.
- **Rank:** E / C. Higher is more compelling.

| Candidate | Evidence-of-demand (E) | Build-cost (C) | E/C | Rank |
|---|---|---|---|---|
| **A. Signed historical time-series API** | 1 (crawler distribution unread yet; agent-tier ships proved appetite for machine-consumption) | 3 (post-Batch-B + gap audits per series + envelope + auth + docs) | 0.33 | **1st** |
| **C. Bulk verification** | 0 (0 hits/day on /snapshots/verify) | 1 (auth + rate limiter + billing hook) | 0.00 | 3rd |
| **B. Webhooks / push** | 0 (no observed asks) | 2 (delivery reliability + DLQ + signed payload) | 0.00 | 2nd (by build-cost) |
| **D. x402 micropayments** | 0 (no XRPL x402 integration observed) | 3 (heaviest attorney gate + client-tooling ecosystem still nascent for XRPL) | 0.00 | 4th |
| **E. Dataset licensing for AI eval/training** | 0 (one external voice, no customer voices) | 3 (corpus curation + reproducibility receipts + signed release infra + data-licensing contract) | 0.00 | 5th |
| **F. Cloudflare Pay Per Crawl** | 0 (feasibility read only) | 0 (zero build; Cloudflare-hosted toggle) | n/a | **Off the rank** — not a build candidate, memo entry only |

**Bottom-line recommendation:**

1. **Do not build anything now.** All four candidates have E < demand-threshold. The 60-90d decision date (2026-10-01 to 2026-11-01) reads from § 2's threshold table.
2. **Ship the cheapest WTP probe (below) this week.** Zero code required; changes intent-signal collection from anecdote to receipt.
3. **When decision date fires:** re-evaluate candidate A first. If its thresholds cleared and gap audit paths look tractable, candidate A moves to the design queue behind attorney answer. Others wait for A to prove the paid market before follow-on builds.
4. **Preserve the covenant text (§ 3) as a public asset regardless of build outcome.** Even if no paid product ever ships, the sovereignty commitment strengthens the free product's positioning against Bithomp/XRPScan/xrpl-utilities.

### § 6.1 — The single cheapest willingness-to-pay probe

**The probe:**

Add **one line** to `llms.txt` and one line to `agents.json`:

```
paid-tier-interest: candidate historical time-series and webhook delivery under consideration for 2026-Q4. If you are an agent operator with a use case, reply to feedback@xrpldashboard.com with intent. Free surface not affected.
```

**Cost:** 15 minutes to add the line. No form, no route, no hosted UI, no database. The `feedback@xrpldashboard.com` alias forwards to Charlie's inbox.

**Signal:** every inbound reply is a named intent-to-purchase with an identifiable agent operator behind it. Volume of replies over 60-90 days is a direct read on demand for candidate A and B.

**Optional +1 layer (if § 6's probe returns >2 asks):** open a public GitHub Issue on `xrpldashboard/xrpldashboard` labeled `paid-tier-interest` with a candidate-list checkbox. Thumbs-up + comments become public WTP receipts.

**Why this is cheaper than a waitlist route:** no code, no state, no infra, no auth, no UI. The signal-per-dollar is the highest available. If nothing arrives in 60 days, that itself is signal — the candidate is HOLD indefinitely.

**Why this survives the covenant:** the probe is transparent about intent (candidate is under consideration, not shipping) and reinforces the free-surface commitment in the same line. It cannot be read as monetization creep.

---

## What this memo settles

1. **The sovereignty rule is now a written boundary,** not a preference. Every future product proposal filters through the provenance table before demand/cost analysis.
2. **RLUSD Ethereum-side and NFT historical are ruled out of any paid tier.** They stay on the free side. Free-forever designation for NFT historical is the recommended posture until an own-full-history-node decision is made independently.
3. **Grandfathered-sovereign vs tainted-until-recollected** is decided: tainted, with a narrow carve-out for series already collected via `get_client()` cascade. Historical time-series products need re-collection with per-row lineage stamps before entering a paid tier.
4. **Candidate A is the lead** and the only serious near-term candidate. Everything else waits.
5. **Nothing ships without both gates cleared** — attorney answers AND sovereignty audit per series.
6. **Demand thresholds are mechanical, not editorial.** § 2 table reads at decision date; no verbal-approval workaround.
7. **The covenant text (§ 3) is a public asset,** publishable independently of any paid-tier build.
8. **The single cheapest WTP probe (§ 6.1) is a 15-minute change** — free to ship regardless of the rest of this memo's fate.
9. **Candidate E (dataset licensing) is a HOLD watch-and-probe entry, distinct from candidate A.** Same source data, different customer segment, different attorney gate. Do not conflate.
10. **§ 3.1's queryable claims layer is Phase 3 free-tier substrate** — never sold, but a shipping dependency for candidate A. Its URI scheme is permanent once agents cite it; scheme decision is a deliberate design pass, not this memo's call.
11. **§ 3.2's `confidence` enum is a spec commitment.** Numeric confidence values are prohibited absent a calibration model. The enum is `signature_verified / cross_checked / walker_computed / single_source`. Extends when calibration models actually exist for a series; not before.
12. **Timing framing is settled: "monetizing the trust layer is a 2027+ story, not a 2026 one"** (added 2026-08-04). This is a market-calibration statement, not a stall. It validates that the memo's HOLDs are aligned with the observed market shape (MCP <5% monetized, x402 XRPL $28K/day, dataset-licensing manual-sale). The 2026 work is the citation substrate (claims layer, envelope discipline, signed history); metering waits for the market shape to emerge.
13. **Cloudflare bifurcates into F.1 (SKIP) + F.2 (WATCH)** (split enacted 2026-08-04 per one-session research, `docs/CLOUDFLARE_PPC_RESEARCH_2026-08-04.md`). F.1 (Pay Per Crawl, 402 model) conflicts with the citation flywheel and requires a paid-plan upgrade — skipped. F.2 (Pay Per Citation, July 2026 Cloudflare pivot) aligns with our strategy exactly — pays on citation appearance, the same event the flywheel builds toward. Not yet actionable (no publisher enrollment path). Watch trigger: 2026-11-01.

## What this memo does NOT settle

- Attorney questions themselves (Kirk email sent 2026-08-03, awaiting response).
- Whether `xrpl_stream.py` migration (Batch B first item, per today's AI cross-verify) is worth accelerating ahead of the post-soak decision — that's a separate memo, not this one.
- Whether the site takes on operating a full-history XRPL node (a decision that would unlock NFT historical as SOVEREIGN, but has independent operational + cost implications outside the paid-tier question).
- Whether an eventual paid tier accepts x402/USDC on Base vs. an XRPL-native path — deferred until candidate A validates the market.

## Next steps (memo-level; standing build queue lives outside this memo)

1. **Update `project_walker_rpc_lever_normalization_backlog.md`** with `network_pulse.py:25` hardcode surfaced by this trace.
2. **When Batch B decision fires (~2026-08-31 post-soak):** cross-reference this memo's per-series migration paths. `xrpl_stream.py` gap-audit + backfill semantics from today's AI cross-verify blocks any migration of that walker.
3. **When `ai_crawler_hits` distribution reads clean (~2026-08-16):** re-evaluate candidate A's demand signal. If N≥3 inbound asks AND the crawler distribution shows agent traffic, candidate A moves HOLD → KEEP.
4. **When Kirk (or another attorney) responds:** capture money-transmission answer, tax treatment, refund posture, custody posture in an attached decision-record memo. Then this memo's verdicts convert from HOLD to BUILD / KILL per candidate.
5. **`/audits` public surface — backlog.** Publish two artifacts:
   - The sovereignty covenant (§ 3 above) reformatted as **enumerated numbered public commitments** on `/covenant` (or an extension of `/about`). Motivation: Grok's audit cited "your rule #3" which existed only in Charlie's prompt-description, not on the site. Future evaluators should extract the numbered rules from the SITE, not from prompts. Publishing them hardens the covenant.
   - An `/audits` receipts page — Grok verdict + verification table, ChatGPT verdict + coverage-critique cross-check, our corrections. Converts the private discipline the prober memories capture into public reputation-layer evidence, per ChatGPT weakness #2. Cost trivial. Attorney gate: none.
6. **Overlap-weighting protocol — filed into quarterly re-run standing method.** When two external engines converge on a critique, it is load-bearing consensus (weight higher). When one raises it and the other does not, it is one-engine signal (weight lower). Read all future external audits by overlap-weight first. Recorded in both `project_external_ai_evaluation_grok_2026-08-04.md` and `project_external_ai_evaluation_chatgpt_2026-08-04.md` under "How to apply."
7. **When envelope-contract fix ships:** land `confidence` enum values (`signature_verified / cross_checked / walker_computed / single_source`) alongside `honest_partial: true on any-null` and `scope_note` population expansion. Per § 3.2 spec; numeric confidence remains prohibited.

---

## Cross-references

- `docs/AGENT_TIER_DESIGN.md` — the *free* half of the split (approved 2026-07-28, build starts Day 1). This memo is the framing for the *paid* half.
- `project_lenovo_repoint_true_mac_dependents_census.md` — Batch A completed 2026-08-02 (4 walker plists moved to Lenovo). Batch B decision (post-soak, ~2026-08-31) is the migration lever for most PUBLIC-INFRA-DEPENDENT series above.
- `project_walker_rpc_lever_normalization_backlog.md` — the RPC-lever-name inconsistency this memo re-surfaced with `network_pulse.py:25`.
- `feedback_causal_claims_need_same_provenance_as_field_values.md` — the discipline that underwrites this memo's verdicts.
- `project_external_ai_prober_results_2026-08-03.md` — the "AI more accurate than our own picture" case that motivated the AI cross-verify of `xrpl_stream.py` migration (Batch B first item).
- `project_external_ai_evaluation_grok_2026-08-04.md` — external audit (invited to deflate). Source of candidate A's signing dimension (§ 1.A amended 2026-08-03) and the CLAIMS-queryable-endpoint proposal (§ 3.1 amended 2026-08-03).
- `project_external_ai_evaluation_chatgpt_2026-08-04.md` — external audit sibling to Grok's (architecturally anchored — ChatGPT did NOT browse the live site). Source of candidate E (§ 1.E), the confidence-field enum spec (§ 3.2), the atomic-citation UX in § 3.1, and the /audits public-surface backlog entry (Next steps #5).
- `project_external_ai_evaluation_claude_cold_2026-08-04.md` — third external audit (fresh Claude, no project memory, actually browsed live surfaces + web-searched revenue evidence). Source of the 2026-08-04 external-reality amendments: § 1.A y1 revenue calibration, § 1.D step-change requirement, § 1.E relationship-driven reframe, § 1.F Cloudflare Pay Per Crawl entry, and the "trust layer is a 2027+ story" timing framing.
- `project_two_instruments_2026_08_ship_of_record.md` — `ai_crawler_hits` telemetry is the quantitative half of the demand signal for candidate A.
- `parked/api-v1-scaffold @ 2b5eb76` — the API shape work already scaffolded; unpark on candidate A promotion.

## Sources (x402 refresh, 2026-08-03)

- [Coinbase & Linux Foundation Debut X402: HTTP-Native Standard](https://cryptonews.com/news/coinbase-linux-foundation-x402-http-payment-standard/)
- [Inside x402: 100M Agentic Payments on Base — Chainalysis](https://www.chainalysis.com/blog/x402-agentic-payments-adoption/)
- [x402 Protocol Explained — Datawallet](https://www.datawallet.com/crypto/x402-protocol-explained)
- [Coinbase-backed AI payments protocol wants to fix micropayment but demand is just not there yet — CoinDesk 2026-03-11](https://www.coindesk.com/markets/2026/03/11/coinbase-backed-ai-payments-protocol-wants-to-fix-micropayment-but-demand-is-just-not-there-yet)
- [Introducing x402: a new standard for internet-native payments — Coinbase](https://www.coinbase.com/developer-platform/discover/launches/x402)
- [Agentic Payments in 2026: The x402 Explainer — RZLT](https://www.rzlt.io/blog/agentic-payments-2026-x402-explainer)
