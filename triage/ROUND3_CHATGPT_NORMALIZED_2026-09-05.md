# Round 3 normalized findings — ChatGPT (free tier, browsing)

Source: `ROUND3_CHATGPT_RAW_2026-09-05.md` (verbatim export).
Model: ChatGPT free-tier, browsing-only. No agent-mode, no code execution, no MCP client.
Window: 2026-09-05 02:03–02:30 UTC.

## Reconciliation vs Render access logs (corrected)

An earlier pass classified row 10 (`pubkey.pem`) as an auditor misreport. On re-read of ChatGPT's row-10 wording — "HTTP body fetch unsupported by browser" (not "not reached by browser") — the request DID go server-side; the auditor tool simply could not surface the PEM body to the model. That matches the page_views row and matches ChatGPT's honest report style.

Corrected self-report accuracy: **16/16 rows consistent with server logs** (was reported earlier as 11/12; that stat was mine, wrong reading).

| Row | ChatGPT said | Server logs (page_views, `ChatGPT-User/1.0`) | Verdict |
|-----|---|---|---|
| 1 | `/` timeout / fetch failure | Absent | consistent (client-side timeout, request never completed a full trip) |
| 2 | `/robots.txt` not reached by browser | Absent | consistent (browser refusal, no HTTP dispatch) |
| 3 | `/llms.txt` 200 | Present | consistent |
| 4 | `/.well-known/agents.json` 200 | Present | consistent |
| 5 | `/openapi.json` 200 | Present | consistent |
| 6 | `/docs` 200 | Present | consistent |
| 7 | `/claims/index.json` 200 | Present | consistent |
| 8 | `/.well-known/security.txt` 200 | Present | consistent |
| 9 | `/.well-known/snapshots/chain.json` 200 | Present | consistent |
| 10 | `/.well-known/snapshots/pubkey.pem` — HTTP body fetch unsupported by browser | Present | consistent (server 200 → body opaque to client tool) |
| 11 | `/2026-09-03.json` 200 | Present | consistent |
| 12 | `/2026-09-04.json` 200 | Present | consistent |
| 13 | `/check.json` not reached by browser | Absent | consistent (browser refusal, no HTTP dispatch) |
| 14-16 | `/check.json?…` variants not reached by browser | Absent | consistent |

Ancillary: 5 `walker_node_fallback` events during the window, all `unreachable:ConnectError` (4 `check_page`, 1 `total_supply`) — Render → Lenovo tunnel gap, same pattern as Round 1's F-02.

**Trust standard set:** ChatGPT refused to fake results it couldn't obtain (Section 3 explicitly says "I will not turn a failed search into a false 'confirmed' result"). This is the honesty bar for R4/R5/R6.

## Findings — 14, with disposition

Legend for `disposition`:
- **SHIPPED-2026-09-05** — fixed and live in production tonight (commit `c7e7916`).
- **PARTIAL-SHIPPED-2026-09-05** — one facet fixed tonight; broader work filed.
- **FILED** — recorded, awaiting build slot or Charlie decision.
- **FILED-CHARLIE-DECISION** — needs a product call before scope.
- **NO-CHANGE** — audit finding is positive; no fix owed.
- **PARK-R4** — cross-cuts UX / human-side; belongs to Gemini's round.

---

```yaml
finding_id: R3-001
category: discovery
evidence_anchor: "homepage → methodology → llms.txt; 2026-09-05 02:06 UTC"
claim_or_state: "Machine surfaces are discoverable from the site."
observation: "Homepage exposes methodology/snapshot hints; llms.txt then enumerates all machine surfaces. Initial direct homepage fetch timed out in the browsing environment."
proposed_improvement: "Explicit Machine/API link on the homepage; ensure root is consistently fetchable."
impact_for_agents: major
outside_inference: false
disposition: PARK-R4
notes: |
  Split: (a) homepage fetch failure is an infra concern for the R4 human-UX pass; (b) "add a Machine/API link to homepage" is a UX call. Both belong to Gemini's round.
---
finding_id: R3-002
category: endpoint_contract
evidence_anchor: "https://xrpldashboard.com/llms.txt; 2026-09-05 02:06 UTC"
claim_or_state: "Address/token triage is available at /check."
observation: "llms.txt documented /check (human), not /check.json (JSON API); OpenAPI contained no /check.json entry."
proposed_improvement: "Publish /check.json in OpenAPI with params, responses, examples."
impact_for_agents: blocker
outside_inference: false
disposition: SHIPPED-2026-09-05
proof:
  - "openapi.json: /check.json now registered via _register_agent_tier_openapi_paths (GET + POST, `q` param, response_shape, rate limits)"
  - "llms.txt: 'Read-only HTTP API — live surface today: /check.json' line added"
  - "agents.json: new http_endpoints[] array with url/method/params/response_shape/rate_limit_anonymous/rate_limit_identified_bot/auth/kind_dispatch/spec_url"
---
finding_id: R3-003
category: HTTP-vs-MCP
evidence_anchor: "https://xrpldashboard.com/openapi.json; 2026-09-05 02:06 UTC"
claim_or_state: "OpenAPI describes the live machine surface."
observation: "OpenAPI reads primarily as a 15-tool MCP contract; explicitly says the HTTP API will emit the proof envelope 'when it ships'."
proposed_improvement: "Make the HTTP read API a first-class OpenAPI surface, or explicitly declare MCP as the only current machine API."
impact_for_agents: blocker
outside_inference: false
disposition: SHIPPED-2026-09-05
proof:
  - "llms.txt: 'when it ships' wording removed; present-tense /check.json documentation added"
  - "agents.json envelope note updated: 'HTTP surface today is /check.json … carries per-capability source_label + checked_at_utc … full envelope migration for HTTP is tracked as a build item'"
  - "agents.json.pricing_transition.future_billed_surface = 'http' (operator decision 2026-09-05, commit a2b1673) — resolves the 'HTTP vs MCP as primary paid interface' half of this finding. MCP stays free at v1; MCP billing considered later if demand."
still_open:
  - "Full top-level `proof` envelope migration for /check.json responses — filed as build item (independent of the billed-surface decision)"
---
finding_id: R3-004
category: token_api
evidence_anchor: "https://xrpldashboard.com/openapi.json; 2026-09-05 02:06 UTC"
claim_or_state: "Token functionality is available to machines."
observation: "get_token_attestation(currency, issuer) only returns attestation status; no general token lookup/data endpoint specified."
proposed_improvement: "Add token lookup with explicit currency/issuer or MPT identifier, schema, freshness, source, error behavior."
impact_for_agents: major
outside_inference: false
disposition: FILED
notes: |
  Design call unblocked 2026-09-05 by R3-003 ruling: primary paid surface is HTTP. So the general token API ships as /token.json (HTTP, x402-billable), not a new MCP tool. Scope for next fix window: currency+issuer + MPT identifier as inputs; supply / trustlines / holders / attestation-status as outputs; source_label + checked_at_utc per capability (matching /check.json shape).
---
finding_id: R3-005
category: verification
evidence_anchor: "https://xrpldashboard.com/.well-known/snapshots/2026-09-04.json; 2026-09-05 02:06 UTC"
claim_or_state: "Daily signed snapshot is independently verifiable."
observation: "Snapshot artifacts published; PEM endpoint returned application/x-pem-file but bytes not exposed by browsing tier — cryptographic verification not completable client-side."
proposed_improvement: "Also publish a machine-readable raw public key representation (hex/base64 JSON) alongside PEM."
impact_for_agents: major
outside_inference: false
disposition: SHIPPED-2026-09-05
proof:
  - "New route /.well-known/snapshots/pubkey.json — hex + base64 + base64url_unpadded + fingerprint_sha256_hex + fingerprint_short"
  - "Registered in openapi.json under signed-snapshots tag"
  - "Linked from llms.txt and agents.json.trust_surfaces.signed_snapshot_pubkey_json"
  - "Live: curl https://xrpldashboard.com/.well-known/snapshots/pubkey.json"
---
finding_id: R3-006
category: schema_versioning
evidence_anchor: "https://xrpldashboard.com/methodology; 2026-09-05 02:06 UTC"
claim_or_state: "Snapshot schema is consistently documented."
observation: "Methodology stated schema version 3, while live 2026-09-03 and 2026-09-04 snapshots reported schema_version 4."
proposed_improvement: "Update methodology; publish a compatibility/version history."
impact_for_agents: major
outside_inference: false
disposition: SHIPPED-2026-09-05
proof:
  - "templates/methodology.html:338 — Schema version 3 → 4 with full history line: 'current since 2026-08-30; v3 shipped 2026-06-24 → 2026-08-29; v2 2026-05-30 → 2026-06-23; v1 2026-05-09 → 2026-05-29. Prior anchors remain verifiable at their era's schema — leaf hashes never change.'"
  - "Live: curl https://xrpldashboard.com/methodology | grep 'Schema version'"
---
finding_id: R3-007
category: pricing
evidence_anchor: "https://xrpldashboard.com/.well-known/agents.json; 2026-09-05 02:06 UTC"
claim_or_state: "Agent access is free at v1."
observation: "Manifest states free-at-v1 clearly, but no machine-readable statement of when/how access becomes paid, price, notice period, grandfathering, billing protocol."
proposed_improvement: "Add pricing, effective_from, notice_period, grandfathering, payment_protocol fields."
impact_for_agents: major
outside_inference: false
disposition: PARTIAL-SHIPPED-2026-09-05
proof:
  - "agents.json.pricing_transition{} block added: current, future_paid_status='planned' + fields for effective_from / notice_period_days / grandfathering_policy / payment_protocol / billed_surface"
  - "2026-09-05 update (commit a2b1673): future_billed_surface = 'http' set (operator decision); added future_billed_surface_rationale field with the 'why HTTP' explanation (x402-native, /check.json is the product); note rewritten to distinguish operator-set fields from legal-hold fields."
still_open:
  - "Four fields (effective_from, notice_period_days, grandfathering_policy, payment_protocol) held null pending attorney meeting — concrete terms need legal sign-off before machines can rely on them."
---
finding_id: R3-008
category: rate_limiting
evidence_anchor: "https://xrpldashboard.com/.well-known/agents.json; 2026-09-05 02:06 UTC"
claim_or_state: "Rate limiting is defined."
observation: "Contract documented (60/min anonymous, 300/min identified crawler, 600/hr MCP, 429 + Retry-After). Stress test could not be performed from browsing tier."
proposed_improvement: "Publish rate-limit headers with remaining/reset values; test every endpoint class."
impact_for_agents: minor
outside_inference: false
disposition: FILED
notes: |
  Emit X-RateLimit-Limit / X-RateLimit-Remaining / X-RateLimit-Reset (RFC 7231-style headers) on every /check.json response. Small scope; queue for next fix window.
---
finding_id: R3-009
category: provenance
evidence_anchor: "https://xrpldashboard.com/.well-known/snapshots/2026-09-04.json; 2026-09-05 02:06 UTC"
claim_or_state: "Snapshot provenance is source-annotated."
observation: "Signed snapshot records source identifier (private-node URL 192.168.40.95:5006); other surfaces use public-node/fallback per methodology."
proposed_improvement: "Make source tier and fallback state mandatory in every HTTP response, not just selected MCP responses."
impact_for_agents: major
outside_inference: false
disposition: PARTIAL-SHIPPED-2026-09-05
proof:
  - "rlusd_live.py:553-568 — sources.xrpl.via now emits 'own-node (LAN)' label for private-range hosts instead of raw 192.168.x.x URL (future-only: already-signed snapshots unchanged for immutability)"
  - "llms.txt sourcing rewrite (also serves R3-012)"
still_open:
  - "Broader work: every /check.json capability entry already carries source_label + checked_at_utc; extend to remaining HTTP endpoints when they land"
---
finding_id: R3-010
category: independent_verification
evidence_anchor: "https://xrpldashboard.com/methodology; 2026-09-05 02:06 UTC"
claim_or_state: "Anchor #5 is independently verifiable."
observation: "Auditor's independent explorer search for anchor #5 tx hash did not surface the transaction — remained unconfirmed in the audit."
proposed_improvement: "Publish a canonical direct explorer link and/or machine-readable anchor index containing tx hash, ledger, close time, chain root, verification status."
impact_for_agents: major
outside_inference: false
disposition: SHIPPED-2026-09-05
proof:
  - "New route /.well-known/anchors.json — all 5 anchors with number, date, tx_hash, ledger_index, close_time_utc, chain_root, decoded memo, and 3 explorer URLs each (xrpscan, bithomp, livenet.xrpl.org)"
  - "chain_cross_check block: chain_json_current_root vs latest_anchor_chain_root; live returns roots_match: true, 113 leaves"
  - "Live: curl https://xrpldashboard.com/.well-known/anchors.json | jq '.anchors[0].explorer_urls'"
notes: |
  ChatGPT's search failure was likely a search-engine indexing gap (recent tx not indexed yet by generic web search). The tx has always been visible on the three named explorers — problem was the discovery path, not the ledger.
---
finding_id: R3-011
category: auditability
evidence_anchor: "https://xrpldashboard.com/.well-known/snapshots/2026-09-04.json; 2026-09-05 02:06 UTC"
claim_or_state: "Snapshot chain is internally consistent."
observation: "2026-09-04 previous_root == 2026-09-03 chain_root; both schema v4."
proposed_improvement: "Expose chain.json in compact indexed form; publish a verifier test vector."
impact_for_agents: minor
outside_inference: false
disposition: PARTIAL-SHIPPED-2026-09-05
proof:
  - "anchors.json.chain_cross_check provides the roots_match observation, which is a partial answer to the same question at the anchor layer"
still_open:
  - "Publish a canonical test vector (one full chain link with a stepped-through verification worked example) — filed as docs task"
  - "Compact chain.json form (e.g. a Merkle-summary variant) — filed if there's demand"
---
finding_id: R3-012
category: disclosure
evidence_anchor: "https://xrpldashboard.com/llms.txt; 2026-09-05 02:06 UTC"
claim_or_state: "The site is computed directly from its own XRPL node."
observation: "llms.txt used broad own-node language; methodology distinguishes own-node MCP/forward-walker use from browser/public-node and Ripple-node surfaces."
proposed_improvement: "Make source provenance surface-specific in the discovery contract."
impact_for_agents: major
outside_inference: false
disposition: SHIPPED-2026-09-05
proof:
  - "llms.txt opening paragraph rewritten to per-surface disclosure: (a) own-node walkers, (b) sovereign-tunnel, (c) public-RPC, (d) public-Ethereum — with the specific surfaces named under each tier and 'the anchored metrics in the daily signed snapshot are strictly (a).'"
  - "Mirrored in agents.json.description with condensed version of the same categorisation"
  - "Live: curl https://xrpldashboard.com/llms.txt | head -3"
---
finding_id: R3-013
category: HTTP-errors
evidence_anchor: "https://xrpldashboard.com/openapi.json; 2026-09-05 02:06 UTC"
claim_or_state: "A standard error shape exists."
observation: "OpenAPI specifies {code, status, message, errors}, but no negative /check calls could be dispatched — runtime error conformity untested."
proposed_improvement: "Publish executable examples for 400/404/422/429/5xx and CI-test them against production."
impact_for_agents: major
outside_inference: false
disposition: FILED
notes: |
  Now that /check.json is documented in openapi.json (R3-002 shipped), attach response examples for each error status. CI test that hits prod with malformed inputs, verifies shape.
---
finding_id: R3-014
category: security_disclosure
evidence_anchor: "https://xrpldashboard.com/.well-known/security.txt; 2026-09-05 02:06 UTC"
claim_or_state: "Security contact is published."
observation: "security.txt returned 200 with Contact, Expires, Preferred-Languages, Canonical fields; Expires 2027-05-12."
proposed_improvement: "No material change required."
impact_for_agents: cosmetic
outside_inference: false
disposition: NO-CHANGE
notes: |
  Recorded positive.
```

## Disposition tally

| Disposition | Count | IDs |
|---|---|---|
| SHIPPED-2026-09-05 | 6 | R3-002, R3-003, R3-005, R3-006, R3-010, R3-012 |
| PARTIAL-SHIPPED-2026-09-05 | 3 | R3-007 (billed_surface set; 4 fields legal-hold), R3-009, R3-011 |
| FILED | 3 | R3-004 (token API — HTTP shape now known), R3-008, R3-013 |
| PARK-R4 | 1 | R3-001 |
| NO-CHANGE | 1 | R3-014 |
| **Total** | **14** | |

Net: 9 findings materially addressed tonight (6 fully shipped + 3 partial), 3 filed with a plan, 1 parked for R4, 1 no-change. Zero pending Charlie decisions on the R3 list. R3-007's four remaining fields are legal-hold, not Charlie-hold — attorney meeting gates them, not another operator call.
