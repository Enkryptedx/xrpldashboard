# Round 3 raw export — ChatGPT (free tier, browsing-only)

Source: Charles Bruce paste into Telegram main session, Sat 2026-09-05 06:39 EDT (immediately after the export completed in ChatGPT).
Model: ChatGPT free-tier (no GPT-6 Astra, no Agent-mode, browsing enabled).
Audit window: 2026-09-05 02:03–02:30 UTC.
Verbatim below (no edits). JJ normalized into `ROUND3_CHATGPT_NORMALIZED_2026-09-05.md`.

---

xrpldashboard.com machine-customer audit
Audit date: 2026-09-04 / 2026-09-05 UTC
Browsing constraint: browsing-only. I could fetch and inspect public HTTP surfaces, but I could not execute code/crypto, issue arbitrary POST requests, or operate an MCP client. I therefore distinguish observed HTTP behavior from claims I could only reason about.
HTTP request log
Important logging limitation: the browsing interface does not expose exact request timestamps to the second or response byte counts. I will not invent either. "200" below is based on the HTTP result exposed by the browser; "size unavailable" means the tool did not expose Content-Length/byte size. The rows are the complete set of direct xrpldashboard.com HTTP targets I attempted, excluding search-engine requests and browser safety checks that never reached the site.
# Method URL Status Response size UTC timestamp
1 GET https://xrpldashboard.com/ timeout / fetch failure unavailable 2026-09-05 02:06 UTC*
2 GET https://xrpldashboard.com/robots.txt not reached by browser — 2026-09-05 02:06 UTC*
3 GET https://xrpldashboard.com/llms.txt 200 unavailable 2026-09-05 02:06 UTC*
4 GET https://xrpldashboard.com/.well-known/agents.json 200 unavailable 2026-09-05 02:06 UTC*
5 GET https://xrpldashboard.com/openapi.json 200 unavailable 2026-09-05 02:06 UTC*
6 GET https://xrpldashboard.com/docs 200 unavailable 2026-09-05 02:06 UTC*
7 GET https://xrpldashboard.com/claims/index.json 200 unavailable 2026-09-05 02:06 UTC*
8 GET https://xrpldashboard.com/.well-known/security.txt 200 unavailable 2026-09-05 02:06 UTC*
9 GET https://xrpldashboard.com/.well-known/snapshots/chain.json 200 unavailable 2026-09-05 02:06 UTC*
10 GET https://xrpldashboard.com/.well-known/snapshots/pubkey.pem HTTP body fetch unsupported by browser unavailable 2026-09-05 02:06 UTC*
11 GET https://xrpldashboard.com/.well-known/snapshots/2026-09-03.json 200 unavailable 2026-09-05 02:06 UTC*
12 GET https://xrpldashboard.com/.well-known/snapshots/2026-09-04.json 200 unavailable 2026-09-05 02:06 UTC*
13 GET https://xrpldashboard.com/check.json not reached by browser — 2026-09-05 02:06 UTC*
14 GET https://xrpldashboard.com/check.json?address=rEy8TFcrAPvhpKrwyrscNYyqBGUkE9hKaJ not reached by browser — 2026-09-05 02:06 UTC*
15 GET https://xrpldashboard.com/check.json?address=rBEc94rUFfLfTDwwGN7rQGBHc883c2QHhx not reached by browser — 2026-09-05 02:06 UTC*
16 GET https://xrpldashboard.com/check.json?address=not-an-address not reached by browser — 2026-09-05 02:06 UTC*
*Exact seconds are unavailable from this browsing tier; the displayed time is the audit-time resolution available to me.


⸻


1. Cold start
Starting with only the domain, the homepage search/fetch exposed the human dashboard and, importantly, a visible methodology link and snapshot surface. The homepage describes itself as a public-good XRPL dashboard and says the methodology is the place to understand the data sources.
Discovery path:
homepage → "methodology" / "signing key fingerprint" / "Browse the public snapshot index" hints → /methodology, /snapshots/ → /llms.txt, /.well-known/agents.json, /openapi.json, /docs, /claims/index.json, /.well-known/security.txt, snapshot chain/key
The eventual /llms.txt is especially useful: it explicitly enumerates the agent discovery layer, OpenAPI, MCP endpoint, claims manifest, snapshots and security contact.
Dead ends/findings:
The initial direct homepage fetch timed out in the browsing layer, although search subsequently supplied the homepage successfully. This means "homepage availability" is not uniformly reproducible from this browsing environment.
/robots.txt could not be fetched by the browser because the browsing layer rejected the URL as unsafe before making the request. This is not evidence that the server returned an error.
/llms.txt was not obvious from the initial human page; it became discoverable through the machine-facing methodology/discovery trail. The site's own llms.txt says every listed URL should resolve.


⸻


2. Read the contract
What is actually documented
/.well-known/agents.json gives a fairly strong machine contract:
no authentication;
anonymous: 60 requests/minute/IP;
identified AI crawlers: 300 requests/minute;
MCP: 600 tool calls/hour/session;
signed-snapshot verification: unlimited;
429s are supposed to include Retry-After;
no silent throttling;
current price: free at v1, no accounts, API keys or payment rails;
MCP is streamable HTTP, protocol version 2025-06-18;
15 read-only MCP tools;
responses use a proof envelope with data, proof, and server.
The OpenAPI document is OpenAPI 3.0.3 and declares version v1. It enumerates the 15 MCP tools and gives their input schemas.
The documented tools include:
get_ledger_stats — no arguments
get_amendment_status — no arguments
get_unl_status — no arguments
get_whale_events(limit)
get_whale_watchlist(limit)
get_rlusd_supply — no arguments
get_rlusd_flow_24h — no arguments
get_amm_pool(amm_account)
get_amm_top_by_tvl(limit)
get_token_attestation(currency, issuer)
get_rwa_families
get_rwa_pools
get_mpt_snapshot
get_signed_snapshot(date_str)
verify_snapshot_signature(envelope)
The common error schema is documented as:
{code: integer, status: string, message: string, errors: object}.
The proof envelope requires source, as_of, freshness_contract, methodology_url, cross_check_status, and honest_partial, with scope_note required for partial responses.
The important contract hole
The published OpenAPI is not a general HTTP data API. It documents discovery/snapshot GET routes plus an x-mcp-tools inventory. It does not document /check.json, an address JSON endpoint, or a general token lookup endpoint.
In fact, llms.txt describes /check as a typed triage page for addresses, tokens, URLs and messages, but does not publish /check.json.
The OpenAPI itself says the common proof envelope applies to MCP today, while "the read-only HTTP API" will emit it when it ships.
That is a material machine-customer distinction: the published HTTP API contract is not the same thing as the MCP contract.
Pricing
"Free now" is machine-readable. agents.json says free at v1; llms.txt says free for identified agents and no payment rails.
"Paid later" is not machine-readable. There is no future pricing, migration date, billing mechanism, notice period, grandfathering rule, or machine-readable deprecation/payment field in the surfaces inspected.
Versioning
There is a v1 OpenAPI document and MCP server version 2.0.0, but the relationship between those versions and compatibility guarantees is not defined. The OpenAPI also says its agent-tier freshness was last verified 2026-08-29, despite being fetched on September 4/5.
Unreachable/failed contract surfaces
robots.txt: browser refused to make the request; no HTTP status obtained.
pubkey.pem: server returned an application/x-pem-file response that the browsing extractor refused to expose; cryptographic key bytes not obtained.
docs: reachable, but only a JavaScript Swagger shell; it directs the machine to OpenAPI.


⸻


3. Use it
/check.json
I could not make the requested real calls.
The machine-readable directory says the usable human route is /check, not /check.json. The browsing tier rejected direct attempts to /check.json and query-string variants before making an HTTP request, so there is no defensible HTTP status to report.
Consequently I cannot honestly claim that:
either XRPSCAN well-known address worked;
token lookup worked;
malformed address behavior worked;
missing-parameter behavior worked;
five rapid repeats produced a particular rate-limit response.
The two well-known accounts selected from XRPSCAN's published well-known-account list were:
rEy8TFcrAPvhpKrwyrscNYyqBGUkE9hKaJ — Binance example;
rBEc94rUFfLfTDwwGN7rQGBHc883c2QHhx — Uphold example.
Those are appropriate independent test inputs, but the dashboard calls themselves were not successfully issued.
Snapshot calls
The daily signed snapshot was fetched successfully.
For 2026-09-04 it returned:
schema_version: 4
leaf_index: 112
leaves_total: 113
chain_root: 8e259732…162abbb
previous_root: 794f1366…e5185c7c
leaf_hash: 945e3e0a…720deea8
snapshot_date_utc: 2026-09-04
xrpl_validated_ledger_index: 106757450
mpt_total_count: 301
signature present
signing fingerprint 7F:D4:F2:F4:D2:57:7C:BE.
The preceding 2026-09-03 snapshot has chain root 794f1366…e5185c7c, exactly matching the 2026-09-04 previous_root. Its schema_version is also 4.
That is a useful positive result: the two consecutive published snapshots are visibly chain-linked.


⸻


4. Verify provenance
The published procedure is explicit:
obtain the Ed25519 public key;
canonicalize {signing_domain, schema_version, snapshot_date_utc, metrics};
SHA-256 with 0x00 leaf prefix;
walk audit_path using 0x01;
verify signature_ed25519 over the specified envelope summary.
I could therefore reason through the verification, but cannot execute it on this tier.
Result: needs-live-verification.
There is nevertheless a strong internal consistency check available without crypto execution: the 2026-09-04 snapshot's previous_root exactly equals the 2026-09-03 snapshot's chain_root.
Provenance source
The 2026-09-04 signed snapshot itself identifies its ledger metric source as:
http://192.168.40.95:5006 → ledger(validated)
and labels the public signing fingerprint.
That is unusually useful machine evidence: the snapshot records a source identifier rather than merely saying "XRPL."
However, a private IP address is not independently proof that the machine is owned by the operator. The site's documentation says the MCP and forward-walker use its own rippled, while other surfaces still use public Ripple/Foundation infrastructure.
Anchor #5
The published methodology specifies the anchor format and verification requirements, including source account, destination account, sequence continuity and matching the chain root for the same ISO date.
I searched independently for anchor #5's supplied transaction hash and ledger, but the independent explorer search did not surface the transaction. Therefore:
Anchor #5 independent verification: not confirmed in this audit.
I will not turn a failed search into a false "confirmed" result.
The site does independently document the anchor mechanism and first anchor, and says daily roots are placed into XRPL Payment memos, with weekly/manual cadence.


⸻


5. Read the disclosure
This is one of the stronger parts of the design.
The proof envelope has a machine-readable source, as_of, freshness_contract, cross_check_status, honest_partial, and optional scope_note.
The methodology also explicitly describes multiple source tiers:
own/local rippled;
public Ripple nodes;
XRPL Foundation cluster;
Postgres/worker-derived data;
Ethereum public RPC;
fallback behavior.
So in principle, an agent can distinguish source and freshness.
There is, however, an important inconsistency between the broad marketing statement and the detailed methodology. llms.txt says public read-only data is computed directly from the site's own XRPL node, while methodology says browser real-time traffic uses wss://xrplcluster.com, server streaming uses s2.ripple.com, and s1.ripple.com supplies some server-side RPCs; the site's own node is explicitly described as serving the forward-walker and MCP envelope reads.
For a paying agent, the crucial question is therefore not "does the site have source metadata?" — yes — but "does every economically relevant HTTP datum expose the exact source/freshness state at the moment I pay?" The published contract says the future HTTP API will use the same envelope, but that HTTP API is not yet the documented/live interface.
The methodology explicitly says caches are named and TTLs disclosed, and the envelope's honest_partial is intended to expose dependency failure.
Would a paying agent know when to pause? Not sufficiently from the current published HTTP surface. There is no live paid HTTP contract, no payment status, no SLA/error-budget field, and no machine-readable "do not consume/pay when source X is stale" policy.


⸻


6. Try to break it, politely
The requested negative tests could not be executed because /check.json is not a published/documented HTTP endpoint and the browsing tier refused the constructed requests before HTTP dispatch.
Test Result
malformed address not executed; no HTTP status
unknown token not executed; no HTTP status
missing parameter not executed; no HTTP status
five rapid repeats not executed; no HTTP status / no rate-limit observation
This is itself a machine-customer finding: the advertised /check capability has no discoverable JSON HTTP contract in the published OpenAPI, while the requested /check.json route cannot be exercised from the available machine surface.
The published error schema exists, but I could not test whether /check actually emits it.
The rate-limit contract is better documented: agents.json specifies 60/minute anonymous, 300/minute verified AI crawler, 600/hour/session for MCP, and 429 + Retry-After rather than silent throttling.


⸻


Top 10 blockers for a machine customer
No documented/live /check.json API despite /check being advertised as a typed machine-useful capability.
No general HTTP address lookup contract in OpenAPI.
No general machine token lookup contract; get_token_attestation is an attestation lookup, not a token-data lookup.
The useful machine contract is primarily MCP, not HTTP, and MCP could not be exercised from this browsing-only tier.
Published HTTP API is described as future tense in llms.txt; proof-envelope support for HTTP is "when it ships."
schema_version inconsistency: methodology says snapshot schema version 3, while live 2026-09-03/04 snapshots report version 4.
Public-key retrieval is awkward for generic agents: the correct PEM endpoint returns a content type that this browsing client cannot expose, so verification cannot be completed here.
Future pricing is not machine-contractual: free-at-v1 is explicit, but no machine-readable paid transition terms exist.
Independent anchor #5 could not be independently confirmed from the explorer search available to this audit.
Exact HTTP observability is inadequate for an audit-grade customer: the public contract provides as_of, but the client-side interface does not expose response byte counts or request-level timing needed for reproducible server-log correlation.


⸻


Findings
finding_id: R3-001
category: discovery
evidence_anchor: "homepage → methodology → llms.txt; 2026-09-05 02:06 UTC"
claim_or_state: "Machine surfaces are discoverable from the site."
observation: "The homepage exposes methodology/snapshot verification hints; llms.txt then enumerates agents.json, OpenAPI, claims, snapshots, security.txt and MCP. The initial direct homepage fetch timed out in this browsing environment."
proposed_improvement: "Expose a stable, explicit Machine/API link on the homepage and ensure the root is consistently fetchable."
impact_for_agents: major
outside_inference: false


⸻


finding_id: R3-002
category: endpoint_contract
evidence_anchor: "https://xrpldashboard.com/llms.txt; 2026-09-05 02:06 UTC"
claim_or_state: "Address/token triage is available at /check."
observation: "llms.txt documents /check, not /check.json; OpenAPI contains no /check.json or equivalent HTTP address/token lookup."
proposed_improvement: "Publish a real JSON endpoint and include it, its parameters, responses and examples in OpenAPI."
impact_for_agents: blocker
outside_inference: false


⸻


finding_id: R3-003
category: HTTP-vs-MCP
evidence_anchor: "https://xrpldashboard.com/openapi.json; 2026-09-05 02:06 UTC"
claim_or_state: "OpenAPI describes the live machine surface."
observation: "The OpenAPI inventory is primarily a 15-tool MCP contract; it explicitly says the HTTP API will emit the proof envelope when it ships."
proposed_improvement: "Make the HTTP read API a first-class, fully documented OpenAPI surface, or explicitly declare MCP as the only current machine API."
impact_for_agents: blocker
outside_inference: false


⸻


finding_id: R3-004
category: token_api
evidence_anchor: "https://xrpldashboard.com/openapi.json; 2026-09-05 02:06 UTC"
claim_or_state: "Token functionality is available to machines."
observation: "The documented get_token_attestation(currency, issuer) only returns attestation status; no general token lookup/data endpoint is specified."
proposed_improvement: "Add token lookup with explicit currency/issuer or MPT identifier, schema, freshness, source and error behavior."
impact_for_agents: major
outside_inference: false


⸻


finding_id: R3-005
category: verification
evidence_anchor: "https://xrpldashboard.com/.well-known/snapshots/2026-09-04.json; 2026-09-05 02:06 UTC"
claim_or_state: "Daily signed snapshot is independently verifiable."
observation: "Snapshot, Merkle path, leaf hash, chain roots and Ed25519 signature are published; the public-key endpoint returned application/x-pem-file but its bytes were not exposed by this browsing tier."
proposed_improvement: "Also publish a machine-readable raw public key representation, e.g. base64/hex JSON alongside PEM, while retaining PEM."
impact_for_agents: major
outside_inference: false


⸻


finding_id: R3-006
category: schema_versioning
evidence_anchor: "https://xrpldashboard.com/methodology; 2026-09-05 02:06 UTC"
claim_or_state: "Snapshot schema is consistently documented."
observation: "Methodology states schema version 3, while live 2026-09-03 and 2026-09-04 snapshots report schema_version 4."
proposed_improvement: "Update methodology immediately and publish a compatibility/version history for snapshot schema changes."
impact_for_agents: major
outside_inference: false


⸻


finding_id: R3-007
category: pricing
evidence_anchor: "https://xrpldashboard.com/.well-known/agents.json; 2026-09-05 02:06 UTC"
claim_or_state: "Agent access is free at v1."
observation: "The manifest clearly states free-at-v1/no payment rails, but does not machine-readably state when/how access becomes paid, pricing, notice period, grandfathering, or billing protocol."
proposed_improvement: "Add pricing, effective_from, notice_period, grandfathering, and payment-protocol fields."
impact_for_agents: major
outside_inference: false


⸻


finding_id: R3-008
category: rate_limiting
evidence_anchor: "https://xrpldashboard.com/.well-known/agents.json; 2026-09-05 02:06 UTC"
claim_or_state: "Rate limiting is defined."
observation: "Anonymous, identified-crawler and MCP limits plus 429/Retry-After behavior are documented. The requested five-repeat stress test could not be performed."
proposed_improvement: "Publish rate-limit headers and machine-readable remaining/reset values, then test every endpoint class."
impact_for_agents: minor
outside_inference: false


⸻


finding_id: R3-009
category: provenance
evidence_anchor: "https://xrpldashboard.com/.well-known/snapshots/2026-09-04.json; 2026-09-05 02:06 UTC"
claim_or_state: "Snapshot provenance is source-annotated."
observation: "The signed snapshot's ledger metric explicitly records a private-node source identifier (192.168.40.95:5006 → ledger(validated)). The site's methodology separately documents public-node/fallback sources for other surfaces."
proposed_improvement: "Make source tier and fallback state mandatory in every HTTP response, not merely in selected MCP responses."
impact_for_agents: major
outside_inference: false


⸻


finding_id: R3-010
category: independent_verification
evidence_anchor: "https://xrpldashboard.com/methodology; 2026-09-05 02:06 UTC"
claim_or_state: "Anchor #5 is independently verifiable."
observation: "The published verification rules are sufficient to describe the test, but an independent explorer search for the supplied anchor #5 transaction did not surface the transaction. It therefore remains unconfirmed in this audit."
proposed_improvement: "Publish a canonical direct explorer link and/or machine-readable anchor index containing tx hash, ledger, close time, chain root and verification status."
impact_for_agents: major
outside_inference: false


⸻


finding_id: R3-011
category: auditability
evidence_anchor: "https://xrpldashboard.com/.well-known/snapshots/2026-09-04.json; 2026-09-05 02:06 UTC"
claim_or_state: "Snapshot chain is internally consistent."
observation: "The 2026-09-04 previous_root equals the 2026-09-03 chain_root, and both snapshots report schema version 4."
proposed_improvement: "Expose chain.json contents in a compact indexed form and publish a verifier test vector so clients can validate one complete chain link without custom reconstruction."
impact_for_agents: minor
outside_inference: false


⸻


finding_id: R3-012
category: disclosure
evidence_anchor: "https://xrpldashboard.com/llms.txt; 2026-09-05 02:06 UTC"
claim_or_state: "The site is computed directly from its own XRPL node."
observation: "llms.txt uses broad own-node language, while methodology distinguishes own-node MCP/forward-walker use from browser/public-node and Ripple-node surfaces."
proposed_improvement: "Make source provenance surface-specific in the discovery contract: source, fallback_sources, freshness, and fallback_active."
impact_for_agents: major
outside_inference: false


⸻


finding_id: R3-013
category: HTTP-errors
evidence_anchor: "https://xrpldashboard.com/openapi.json; 2026-09-05 02:06 UTC"
claim_or_state: "A standard error shape exists."
observation: "OpenAPI specifies {code,status,message,errors}, but no negative /check calls could be dispatched, so actual runtime error conformity remains untested."
proposed_improvement: "Publish executable examples for 400/404/422/429/5xx and test them in CI against production."
impact_for_agents: major
outside_inference: false


⸻


finding_id: R3-014
category: security_disclosure
evidence_anchor: "https://xrpldashboard.com/.well-known/security.txt; 2026-09-05 02:06 UTC"
claim_or_state: "Security contact is published."
observation: "security.txt returned 200 with Contact, Expires, Preferred-Languages and Canonical fields; Expires is 2027-05-12."
proposed_improvement: "No material change required; optionally add a dedicated policy URL if one exists."
impact_for_agents: cosmetic
outside_inference: false


⸻


Three-line summary
What an agent can do here today: discover the agent tier, read OpenAPI/agents.json, consume signed daily snapshots, inspect claims, and understand source/freshness/rate-limit contracts; the snapshot chain itself is visibly linking correctly across 2026-09-03 → 2026-09-04.
What it cannot: reliably integrate the advertised /check capability as a documented HTTP API, perform a general token/address lookup from OpenAPI, or complete cryptographic verification on this browsing-only tier; the requested /check.json tests were not dispatchable and anchor #5 remains independently unconfirmed.
What would move the needle: ship one stable, fully documented HTTP JSON API—including /check, address/token lookup, proof envelopes, tested errors, explicit source/fallback state and future pricing—and reconcile the snapshot schema-version documentation.
