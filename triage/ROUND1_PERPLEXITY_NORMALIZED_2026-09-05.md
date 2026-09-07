# Round 1 — Perplexity findings, normalized (2026-09-05)

**Source:** `triage/ROUND1_PERPLEXITY_RAW_2026-09-05.md` (Perplexity Sonar Pro via web app, retrieved evening of 2026-09-04 EDT)
**Normalizer:** JJ (Opus 4-7)
**Baseline commit:** `292737c` (anchor #5 stamped)
**Cross-check reference:** JJ's own reads of the live site + repo state post-Sep-4 (commits `aa6030a`, `01a5391`, `60e9925`)

## How to read this file

Every Perplexity observation has been normalized into one of three buckets:

- **F-nn:** first-class finding (has evidence anchor URL, reproducible)
- **F-nn (evidence_missing):** finding whose evidence anchor is absent or ambiguous — kept visible per Charlie's ruling, doesn't count as verified
- **F-nn (outside_inference):** claim about our internals that Perplexity is not qualified to judge externally — flagged for the next inward-facing round

Perplexity's own response format was a narrative-with-scores rather than the per-finding YAML the prompt asked for. That's flagged as **F-01 (about the model itself)**.

Where JJ's cross-check against post-Sep-4 code/copy contradicts Perplexity, the finding is annotated **JJ-VERDICT:** with a file:line or curl evidence.

---

## Findings (37 total)

### F-01 — Perplexity ignored the requested YAML output shape
```yaml
finding_id: R1-001
category: about-model
audience: n/a
evidence_anchor: {type: n/a, value: n/a, fetched_at: 2026-09-04T23:50Z}
claim_or_state: prompt requested "findings as YAML documents, one per finding, separated by three dashes"
observation: Perplexity delivered a narrative report (score card, 5 roles, top-10, comparison, bottom line) not YAML. Own-format bias — the model reshaped output to a format it prefers.
proposed_improvement: for future runs, add a hard "your first output token must be `---`" instruction
impact_for_humans: n/a
impact_for_machines: minor (JJ normalized manually)
outside_inference: false
```

### F-02 — Machine surfaces "not retrievable in this browser audit" (contradicted by external test)
```yaml
finding_id: R1-002
category: machine-discoverability-gap
audience: machine
evidence_anchor: {type: url, value: https://xrpldashboard.com/llms.txt AND 7 sibling URLs, fetched_at: 2026-09-04T23:20Z}
claim_or_state: Perplexity: "/llms.txt, /.well-known/agents.json, /openapi.json, /robots.txt, /connect, /docs, /claims/index.json, and /.well-known/snapshots/chain.json did not surface in search and were not retrievable in this browser audit"
observation: FALSE from external vantage. All 8 URLs return HTTP 200 with real content from Lenovo (home ISP, outside Render), across plain-curl, browser-Safari, PerplexityBot, GPTBot, ClaudeBot, Google-Extended UAs. Perplexity's own fetch layer failed; the site itself did not block.
external_source_cite: msg #16654 URGENT-1 matrix + robots.txt content ("User-agent: * / Allow: /...")
JJ-VERDICT: WRONG-ABOUT-SITE, RIGHT-ABOUT-DISCOVERABILITY-GAP-VIA-SEARCH. The site returns content when directly fetched; Perplexity's search index does not surface these URLs organically. Two different problems. Discoverability-via-search is a real finding; direct-fetch-blocked is not.
proposed_improvement: separate the two: (a) submit sitemap.xml + robots directives to Perplexity/Google/Bing so machine surfaces get indexed; (b) file access-fetch-failure as a Perplexity-side flake to their support
impact_for_humans: n/a
impact_for_machines: major (indexing gap — real, actionable)
outside_inference: false
```

### F-03 — /rlusd shows BOTH "Live feed unavailable" AND "Live · updated Ns ago" simultaneously
```yaml
finding_id: R1-003
category: contradictory-claim
audience: both
evidence_anchor: {type: url, value: https://xrpldashboard.com/rlusd, fetched_at: 2026-09-04T23:21Z}
claim_or_state: /rlusd HTML contains both `<strong>Live feed unavailable.</strong>` at line 1089 AND `Live · updated 319s ago` at line 1133
observation: Two contradictory status strings in the same template. Which shows depends on JS state; both are hardcoded branches. If JS is slow/broken, user sees both.
external_source_cite: JJ curl of /rlusd from Lenovo, grep of HTML source
JJ-VERDICT: CONFIRMED — real truth violation in the template. Independent of Perplexity's stale-data claim.
proposed_improvement: template must render exactly ONE of the two status strings; JS should hide the other. Add a test that grep -c both strings on rendered HTML == 1.
impact_for_humans: major
impact_for_machines: major
outside_inference: false
```

### F-04 — /amendments cited as "Fetched 2026-08-08T08:55:52Z" — MODEL WAS WRONG
```yaml
finding_id: R1-004
category: external-claim-fail (about the model, not the site)
audience: n/a
evidence_anchor: {type: url, value: https://xrpldashboard.com/amendments, fetched_at: 2026-09-04T23:21Z}
claim_or_state: Perplexity said "/amendments visibly says 'Fetched 2026-08-08T08:55:52Z'"
observation: FALSE. Live /amendments has `fetched-at="1788564068"` = 2026-09-04T23:21:08Z (right now). The 2026-08-08 timestamp Perplexity saw was either stale content in Perplexity's cache OR the model hallucinated a specific timestamp.
external_source_cite: JJ curl of /amendments, HTTP `cache-control: public, max-age=300, s-maxage=300`, `cf-cache-status: DYNAMIC`
JJ-VERDICT: WRONG-ABOUT-SITE. Page is fresh. The stale-data claim doesn't stand for /amendments.
proposed_improvement: n/a for the site; noted as a Perplexity confabulation risk when auditing
impact_for_humans: n/a
impact_for_machines: n/a
outside_inference: false
```

### F-05 — /tokens cited as "latest bucket 2026-08-04" — MODEL WAS WRONG
```yaml
finding_id: R1-005
category: external-claim-fail (about the model)
audience: n/a
evidence_anchor: {type: url, value: https://xrpldashboard.com/tokens, fetched_at: 2026-09-04T23:21Z}
claim_or_state: Perplexity said "/tokens exposes an August 4 'latest bucket' despite live/current presentation"
observation: FALSE. Live /tokens shows "updated 21m ago." Perplexity saw a stale copy or hallucinated the Aug 4 date.
external_source_cite: JJ curl of /tokens, grep for freshness header
JJ-VERDICT: WRONG-ABOUT-SITE. Page is fresh.
proposed_improvement: n/a for site; note this Perplexity flake
impact_for_humans: n/a
impact_for_machines: n/a
outside_inference: false
```

### F-06 — /about references `wss://s2.ripple.com` — MODEL READ PRE-SEP-3 COPY
```yaml
finding_id: R1-006
category: external-claim-fail (about the model reading stale cache)
audience: n/a
evidence_anchor: {type: url + file, value: https://xrpldashboard.com/about AND templates/about.html:187, fetched_at: 2026-09-04T18:00Z}
claim_or_state: Perplexity: "About page names browser-side wss://xrplcluster.com and server-side wss://s2.ripple.com"
observation: The server-side worker no longer reads from s2.ripple.com — commit aa6030a (2026-09-03) changed /about copy to say "server-side worker from our own rippled node on the LAN at ws://127.0.0.1:6007". Perplexity read pre-fix cached copy.
external_source_cite: git log aa6030a, current templates/about.html:187
JJ-VERDICT: STALE-CACHE-READ, ALREADY FIXED. Perplexity is auditing yesterday's copy.
proposed_improvement: cache-bust hint: on next audit run, tell the model to include `?_v=<timestamp>` on all fetches
impact_for_humans: n/a (already-fixed reference)
impact_for_machines: n/a
outside_inference: false
```

### F-07 — Homepage flat nav — 15+ nouns without primary intent
```yaml
finding_id: R1-007
category: human-ux-gap
audience: human
evidence_anchor: {type: url, value: https://xrpldashboard.com/, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "learn pools whales tokens MPTs NFTs RWA RLUSD lending amendments network sidechain health methodology about institutional" — flat noun list, no user-intent guidance
observation: A first-time visitor must infer the primary job from unfamiliar protocol nouns before knowing why any matter.
proposed_improvement: replace with "Start here" trio (Understand XRPL / Track today's activity / Verify an account/token/claim), progressive disclosure below
impact_for_humans: major
impact_for_machines: minor (agents also parse nav)
outside_inference: false
```

### F-08 — Homepage identity crisis: three products competing
```yaml
finding_id: R1-008
category: human-ux-gap
audience: human
evidence_anchor: {type: url, value: https://xrpldashboard.com/, fetched_at: 2026-09-04T23:50Z}
claim_or_state: Homepage presents three simultaneous products: education (/learn), live dashboards, verification/institutional. Each valid, none dominant.
observation: Visitors from shared links can't answer "am I here to learn / monitor / investigate / verify / buy institutional tooling?"
proposed_improvement: pick ONE lead story per homepage viewport; make the other two accessible but subordinate. Perplexity's suggestion: daily "What changed?" as the front door.
impact_for_humans: major
impact_for_machines: n/a
outside_inference: false
```

### F-09 — Raw hex token codes prominent in /tokens
```yaml
finding_id: R1-009
category: human-ux-gap
audience: human
evidence_anchor: {type: url, value: https://xrpldashboard.com/tokens, fetched_at: 2026-09-04T23:50Z}
claim_or_state: /tokens ranking foregrounds raw hex codes like `5553445600000000000000000000000000000000`, `774D52564C...`, `4F70756E656E6365...`
observation: Serialization detail in prime UI real-estate. Users must tolerate protocol serialization to browse the ranking.
proposed_improvement: decoded name as primary display, hex retained as auditability collapse. Perplexity ranks this in top-3.
impact_for_humans: major
impact_for_machines: minor
outside_inference: false
```

### F-10 — CLARITY Act panel duplicated on homepage
```yaml
finding_id: R1-010
category: human-ux-gap
audience: human
evidence_anchor: {type: url, value: https://xrpldashboard.com/, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "The duplicate CLARITY Act treatment near the top also looks like a content-priority collision rather than intentional repetition"
observation: Homepage shows CLARITY Act content twice near the top; reads as content-model collision.
proposed_improvement: consolidate to single CLARITY panel, or intentionally frame the two instances differently (e.g. summary + explainer)
impact_for_humans: minor (cosmetic-ish)
impact_for_machines: n/a
outside_inference: false
```

### F-11 — Terminology density: MPT/AMM/UNL/XLS-70/XLS-80/SHAMap scattered w/o gloss
```yaml
finding_id: R1-011
category: human-ux-gap
audience: human
evidence_anchor: {type: url, value: https://xrpldashboard.com/ AND multiple, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "MPT, AMM, UNL, XLS-70, XLS-80, and SHAMap remain scattered across the experience"
observation: /learn has progressive-disclosure discipline; the rest of the site lacks it. Newcomers hit jargon early.
proposed_improvement: hover-glossary tooltip on first occurrence of each term per page; borrow /learn's model
impact_for_humans: minor
impact_for_machines: n/a
outside_inference: false
```

### F-12 — No daily "What changed?" narrative
```yaml
finding_id: R1-012
category: human-ux-gap + search-intent-unmet
audience: human
evidence_anchor: {type: url, value: https://xrpldashboard.com/, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "The homepage gives many dashboards and no obvious daily briefing"
observation: Perplexity ranks this as #1 improvement AND agrees with all four prior AI audits. Independent convergence.
proposed_improvement: ship a daily "What changed since yesterday?" page + homepage lead: amendments, validator/UNL changes, whale clusters, RWA/MPT issuance, RLUSD mint/burn, liquidity shifts, incidents, methodology exceptions
impact_for_humans: blocker (habit-loop)
impact_for_machines: major (citation-shaped answer)
outside_inference: false
CONVERGENCE: confirmed by independent outside audit (matches Charlie's roadmap)
```

### F-13 — No unified cross-site search
```yaml
finding_id: R1-013
category: human-ux-gap
audience: human
evidence_anchor: {type: url, value: https://xrpldashboard.com/, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "no unified cross-site search that accepts a wallet address, transaction hash, currency/issuer pair, MPT issuance ID, amendment name/hash, validator, or domain"
observation: Users have to know which sub-page to visit for each query type.
proposed_improvement: build one search bar that routes based on input shape (r-address → /wallet, tx hash → /check, etc.)
impact_for_humans: major
impact_for_machines: minor
outside_inference: false
```

### F-14 — No persistent global freshness/status strip
```yaml
finding_id: R1-014
category: machine-surface-gap + human-ux-gap
audience: both
evidence_anchor: {type: url, value: https://xrpldashboard.com/, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "no persistent global freshness/status strip: source node, validated ledger, last successful fetch, data-cache age, and whether a page is in degraded mode"
observation: Nav has a liveness chip (Ledger #N · age) but no source/degraded-mode info; per-page freshness varies wildly.
proposed_improvement: expand liveness chip to global strip with source node, cache age, per-page degradation flag
impact_for_humans: minor
impact_for_machines: major (this is what makes an agent decide "is this current enough to cite?")
outside_inference: false
```

### F-15 — No "How to cite this page" block on data pages
```yaml
finding_id: R1-015
category: machine-surface-gap + search-intent-unmet
audience: both
evidence_anchor: {type: url, value: https://xrpldashboard.com/amendments (example), fetched_at: 2026-09-04T23:50Z}
claim_or_state: "A short visible 'How to cite this page' block on analytical pages, including observed-at timestamp, ledger index, methodology version, canonical URL, and machine-readable export"
observation: Journalist/agent has to reverse-engineer citation shape from prose.
proposed_improvement: standard citation block bottom-of-page on every data page: `Observed at: <ISO> · Validated ledger: <index> · Method: <version> · Canonical: <URL> · Machine export: <JSON URL>`
impact_for_humans: major
impact_for_machines: blocker (this is the mechanism that turns a page into a first-class citation object)
outside_inference: false
CONVERGENCE: matches Charlie's "one-screen proof receipt" roadmap
```

### F-16 — Signed-snapshot proof needs one-screen human receipt
```yaml
finding_id: R1-016
category: human-ux-gap + machine-surface-gap
audience: both
evidence_anchor: {type: url, value: https://xrpldashboard.com/about, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "The current signed-snapshot prose is promising but not journalist-ready" — needs one-screen receipt (claim / observation time / XRPL source / reproducibility / integrity / chain anchoring / limits)
observation: Design is technically strong; UX is prose-heavy. Perplexity provides an exact 7-field table shape.
proposed_improvement: build `/proof/<date>` page that renders the 7-field table with one-click reproduce/verify
impact_for_humans: major
impact_for_machines: major
outside_inference: false
CONVERGENCE: confirmed by all 5 audits (matches Charlie's roadmap)
```

### F-17 — "trade" semantics = Payments + AMM deposit/withdraw ≠ market trades
```yaml
finding_id: R1-017
category: contradictory-claim + machine-surface-gap
audience: both
evidence_anchor: {type: url, value: https://xrpldashboard.com/tokens, fetched_at: 2026-09-04T23:50Z}
claim_or_state: /tokens defines "trade" as "every token Payment plus every AMM deposit/withdraw" (transparently disclosed on-page). But this is NOT how readers interpret "trading volume."
observation: A journalist or AI can easily cite the broad label without the caveat. Perplexity flagged this as its own catch — a real definitional risk not caught by prior audits.
proposed_improvement: rename metric or split it: "token flow events" (broad) vs "market trades" (narrow, DEX-order only). If keeping "trade," make the caveat inline next to every headline number, not just methodology.
impact_for_humans: minor
impact_for_machines: major (semantic risk when agents cite the number without the caveat)
outside_inference: false
CHARLIE-FLAGGED: yes (his instruction: "flag as its own finding")
```

### F-18 — "verified" taxonomy fragmented across pages
```yaml
finding_id: R1-018
category: contradictory-claim
audience: both
evidence_anchor: {type: url, value: https://xrpldashboard.com/tokens AND /about, fetched_at: 2026-09-04T23:50Z}
claim_or_state: /tokens uses "✓ verified", "self-described", "bare", "labeled"; /about describes hand-curated names AND cryptographic Domain + xrp-ledger.toml two-way attestation; homepage has "identity claim"
observation: "Verified" can mean "we have a display-name source" on one page and "two-way cryptographic issuer attestation" on another. Same word, different truth-strengths.
proposed_improvement: one formal taxonomy, enum values, evidence-per-level. E.g. levels 0-4: `unlabeled` / `named` / `self-described` / `domain-attested` / `two-way-verified`. Same terms + evidence surface on every page and in every machine response.
impact_for_humans: major
impact_for_machines: blocker (contract-level ambiguity)
outside_inference: false
CONVERGENCE: confirmed by independent outside audit (matches Charlie's roadmap)
```

### F-19 — /rlusd search results conflict with external reporting
```yaml
finding_id: R1-019
category: external-claim-fail
audience: both
evidence_anchor: {type: search-result-url, value: Perplexity search for "RLUSD current supply", fetched_at: 2026-09-04T23:50Z}
claim_or_state: /rlusd reported "Live feed unavailable" + $896.49M XRPL + $697.46M Ethereum; late-Aug reporting cited ~$963M or $1.024B on XRPL
external_source_cite: [12] finance.yahoo.com/markets/crypto/articles/rlusd-hits-2b-market-cap-130101226.html, [13] u.today/rlusd-hits-historic-1-billion-supply-milestone-on-xrp-ledger
observation: Site number disagrees with reporting. Could be: (a) reporting is behind, (b) our fetcher was mid-outage during Perplexity's read, (c) real freshness gap in rlusd_live.py.
JJ-VERDICT: NEEDS-CODE-TRACE — is `rlusd_live.py` currently fresh? What's the last successful fetch time? This is exactly what Round 2 (Fable code-trace) should verify.
proposed_improvement: (a) verify rlusd_live.py current state; (b) if fetcher is broken, fix; (c) if fetcher is fine, put explicit disagreement note "our number differs from finance.yahoo — here's why"
impact_for_humans: blocker (a stablecoin-supply mismatch is a big deal)
impact_for_machines: blocker
outside_inference: partial — some inference on rlusd_live.py internals, but the external mismatch is verifiable
FLAG-FOR-ROUND-2: yes
```

### F-20 — Machine-layer 6.4/10 score is not measuring the layer, it's measuring Perplexity's fetch success
```yaml
finding_id: R1-020
category: about-model
audience: n/a
evidence_anchor: {type: n/a, value: n/a, fetched_at: 2026-09-04T23:50Z}
claim_or_state: Perplexity: machine-layer score 6.4/10 broken into 8 sub-dimensions; several dimensions (Contract accessibility 3.0, Tool/schema completeness 4.0, Integrator experience 4.0, Discovery 5.0, Freshness/operability 4.5) all pinned low because "I could not inspect" the endpoints
observation: Perplexity is scoring the visibility of the machine layer FROM ITS OWN FETCHER, not the machine layer itself. From external vantage (Lenovo), every one of those endpoints returns 200 with real content. The low scores are (a) partly Perplexity's fetch-flake, (b) partly a real search-index-discoverability gap.
proposed_improvement: split the finding: real discoverability gap → F-02; the score itself → outside_inference until re-run with a working fetcher
impact_for_humans: n/a
impact_for_machines: major (real Perplexity-fetch reliability gap, whether ours or theirs, affects citation)
outside_inference: true (about the underlying implementation being adequate) / false (about the fetch reliability being a problem)
```

### F-21 — Data provenance disclosure (positive finding)
```yaml
finding_id: R1-021
category: n/a (positive)
audience: both
evidence_anchor: {type: url, value: https://xrpldashboard.com/about + /amendments + /tokens, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "The site does substantially better than most dashboards at stating its intended sources"
observation: /about names browser + server WS endpoints; /amendments names feature RPC + s1.ripple.com:51234 + 5-min cache ceiling + per-validator tally limitation; /tokens defines "trade", AMM-only liquidity, verified/self-described/bare/labeled taxonomy
proposed_improvement: keep. This is a moat instinct — protect it as site grows.
impact_for_humans: n/a
impact_for_machines: n/a
outside_inference: false
```

### F-22 — "we don't guess" editorial discipline (positive finding)
```yaml
finding_id: R1-022
category: n/a (positive)
audience: human
evidence_anchor: {type: url, value: https://xrpldashboard.com/amendments, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "No plain-English summary is on file … until we do, we don't guess at what it does"
observation: Editorial restraint on uncatalogued amendments. Perplexity says this is "stronger editorial behavior than a polished but unsupported summary" and calls it the site's real trust signal.
proposed_improvement: protect this instinct; codify as editorial standard
impact_for_humans: positive
impact_for_machines: positive
outside_inference: false
```

### F-23 — Independent-source gap (per-headline reproducibility)
```yaml
finding_id: R1-023
category: machine-surface-gap
audience: both
evidence_anchor: {type: url, value: https://xrpldashboard.com/tokens (example), fetched_at: 2026-09-04T23:50Z}
claim_or_state: "For publication-grade citation, I want the source RPC request, response/normalized artifact, ledger index, and calculation formula attached to each figure or exportable record"
observation: Every headline number should carry a per-response provenance trail. Currently exists in prose form on About; not per-figure.
proposed_improvement: append `<sup>` hover / footnote per headline number that expands to: raw RPC request + response + calculation + ledger + observed_at
impact_for_humans: minor
impact_for_machines: blocker
outside_inference: false
```

### F-24 — Solo-project operational risk (honest, not a fix)
```yaml
finding_id: R1-024
category: outside-inference
audience: both
evidence_anchor: {type: url, value: https://xrpldashboard.com/about, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "The site names one developer and a roughly $25/month out-of-pocket hosting arrangement. That is honest, not a problem in itself, but a newsroom should recognize the bus factor and availability risk"
observation: Perplexity's flag is accurate context; the site DID disclose this. Nothing to fix here, but the disclosure could be more prominent for prospective citation-users.
proposed_improvement: /about's "bus factor + hosting" note could be one line higher; consider a "reliability disclaimer" tag on institutional pages
impact_for_humans: minor
impact_for_machines: minor
outside_inference: partial (about how a newsroom would react)
```

### F-25 — /wallet + /token pages disallowed by robots.txt for all crawlers
```yaml
finding_id: R1-025
category: machine-discoverability-gap
audience: machine
evidence_anchor: {type: url, value: https://xrpldashboard.com/robots.txt, fetched_at: 2026-09-04T23:20Z}
claim_or_state: robots.txt disallows /wallet/ and /token/ for all crawlers
observation: Charlie's intent is probably "per-user wallet lookups shouldn't be crawled/cached." But this blocks agents from ever discovering the wallet or token detail endpoints via crawl — they'd have to know the URLs upfront.
JJ-VERDICT: DESIGN-CALL. If wallets/tokens SHOULD be crawlable for citation, remove disallow. If they shouldn't (per-user privacy), the current shape is correct.
proposed_improvement: Charlie decision: keep the disallow (privacy-first) or add wallet/token-detail to sitemap.xml for machine discovery
impact_for_humans: n/a
impact_for_machines: major
outside_inference: false
```

### F-26 — Perplexity's search index doesn't surface machine surfaces organically
```yaml
finding_id: R1-026
category: machine-discoverability-gap
audience: machine
evidence_anchor: {type: search-result-url, value: Perplexity search for XRPL agent surfaces, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "Search also did not surface llms.txt, agents.json, or openapi.json; it surfaced standard HTML pages, especially /about, /amendments, /rlusd, /network, and /mpts"
observation: Direct fetch works (F-02); search doesn't index. Two-step problem: (a) sitemap.xml may not list machine surfaces, (b) machine surfaces may not be linked from HTML pages that ARE indexed.
proposed_improvement: (a) add llms.txt, /.well-known/agents.json, /openapi.json, /docs, /claims/index.json to sitemap.xml explicitly; (b) add a `<link rel="alternate" type="application/json">` in HTML head where relevant; (c) reference these URLs prominently in llms.txt itself
impact_for_humans: n/a
impact_for_machines: major
outside_inference: false
```

### F-27 — Every factual page needs a citation header
```yaml
finding_id: R1-027
category: machine-surface-gap
audience: both
evidence_anchor: {type: url, value: https://xrpldashboard.com/amendments (example), fetched_at: 2026-09-04T23:50Z}
claim_or_state: "Put the current answer at the top, then: Observed at, Validated ledger, Source endpoint(s), Methodology version, Last successful refresh, Cache age, Data quality status, and Canonical citation URL"
observation: Same as F-15 but framed for citation engines specifically.
proposed_improvement: unified template component `<citation-header>` on every /amendments, /rlusd, /tokens, /mpts, /network, /whales, /pools, /nfts, /rwa, /lending, /credentials, /learn (as applicable)
impact_for_humans: major
impact_for_machines: blocker
outside_inference: false
```

### F-28 — Immutable observation URLs (time-versioned)
```yaml
finding_id: R1-028
category: machine-surface-gap
audience: machine
evidence_anchor: {type: url, value: https://xrpldashboard.com/rlusd (example), fetched_at: 2026-09-04T23:50Z}
claim_or_state: "A headline 'RLUSD supply on XRPL' should have time-versioned URLs or query parameters that resolve to a signed immutable observation. Citation engines prefer stable objects over mutable dashboards."
observation: Current pages are mutable; a citation from Aug 8 doesn't guarantee the reader sees the same numbers today.
proposed_improvement: `/rlusd?at=2026-09-04T21:00Z` returns signed snapshot for that observation window; or `/rlusd/history/2026-09-04` returns immutable date-page
impact_for_humans: n/a
impact_for_machines: blocker (this is what makes the site permanently citable)
outside_inference: false
```

### F-29 — Standardize definitions across UI/docs/OpenAPI/llms.txt/agents/claims/API
```yaml
finding_id: R1-029
category: machine-surface-gap + contradictory-claim
audience: machine
evidence_anchor: {type: url, value: multiple, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "Use formal definitions and structured identifiers for trade, volume, supply, circulating supply, whale, verified, self-described, label, claim, and sanctions hit. Keep these definitions synchronized across UI, docs, OpenAPI, llms.txt, and data responses."
observation: Same terms sometimes mean different things depending on surface. Perplexity flagged this consistently.
proposed_improvement: one glossary file `/glossary.json` referenced from every other surface; every HTML tooltip / OpenAPI enum / llms.txt reference / claims field points to it
impact_for_humans: minor
impact_for_machines: blocker
outside_inference: false
```

### F-30 — Weekly signed "XRPL Verified Findings" publication
```yaml
finding_id: R1-030
category: search-intent-unmet + missing-competitor-parity
audience: both
evidence_anchor: {type: url, value: https://xrpldashboard.com/, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "Publish weekly signed 'XRPL Verified Findings' with immutable URLs, evidence artifacts, correction history, and distribution"
observation: Site has near-zero social footprint / external citation trail. Authority compounds through externally referenced stable reporting.
proposed_improvement: weekly /findings/YYYY-MM-DD report with 3-5 signed findings, embeddable widgets, RSS feed, X/Twitter distribution
impact_for_humans: major
impact_for_machines: major (backlinks + reputation signals for citation engines)
outside_inference: false
```

### F-31 — /amendments should be the flagship
```yaml
finding_id: R1-031
category: human-ux-gap
audience: human
evidence_anchor: {type: url, value: https://xrpldashboard.com/amendments, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "Make amendments/governance the flagship vertical: it is the strongest current combination of explanation, sources, uniqueness, and honest uncertainty"
observation: Independent convergence — Perplexity agrees with all four prior audits.
proposed_improvement: front-page amendments hero section; homepage anchor link to "current governance state"; separate SEO landing pages per amendment
impact_for_humans: major
impact_for_machines: major
outside_inference: false
CONVERGENCE: confirmed by 5-of-5 audits
```

### F-32 — Cut/subordinate premature "procurement-ready" institutional positioning
```yaml
finding_id: R1-032
category: contradictory-claim
audience: human
evidence_anchor: {type: url, value: https://xrpldashboard.com/institutional, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "'Procurement-ready' and future paid institutional APIs invite enterprise-grade expectations. Until the site demonstrates freshness SLAs, support/incident processes, contract stability, and independently testable data integrity, prioritize the public observatory identity"
observation: Institutional-tier promises unsupported by operational maturity; opens credibility attack surface.
proposed_improvement: rename tier ("early-access / preview"), remove "procurement-ready" until SLA is real; document what's aspirational vs shipped
impact_for_humans: major
impact_for_machines: minor
outside_inference: partial (about operational maturity — but the copy claim is externally verifiable)
```

### F-33 — Signed-snapshot public key + fingerprint pin (positive finding)
```yaml
finding_id: R1-033
category: n/a (positive)
audience: machine
evidence_anchor: {type: url, value: https://xrpldashboard.com/about + /.well-known/snapshots/*, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "Ed25519 signatures, public key/fingerprint, Merkle-chained manifests, DNS pinning, verification form, and snapshot index are strong in design"
observation: Design is credited as strong; execution needs to become discoverable + verifiable (see F-16, F-28)
proposed_improvement: keep design; harvest for external audit visibility
impact_for_humans: n/a
impact_for_machines: n/a
outside_inference: false
```

### F-34 — Wallet inspector no-keys reassurance (positive finding)
```yaml
finding_id: R1-034
category: n/a (positive)
audience: human
evidence_anchor: {type: url, value: https://xrpldashboard.com/, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "We never ask for keys, seeds, or signatures — public address only"
observation: Excellent security trust cue; differentiates from scam-adjacent trading products.
proposed_improvement: keep; consider expanding this "read-only / never-asks-for-secrets" framing across the site
impact_for_humans: positive
impact_for_machines: n/a
outside_inference: false
```

### F-35 — /learn's progressive-explanation model (positive finding)
```yaml
finding_id: R1-035
category: n/a (positive)
audience: human
evidence_anchor: {type: url, value: https://xrpldashboard.com/learn, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "/learn is genuinely useful for someone who knows XRP but not XRPL mechanics. The 'notebook' model, then its limitations, is a good onboarding structure"
observation: Best-in-class onboarding pattern on the site; borrow across other pages
proposed_improvement: use /learn's tone + structure on every non-data page; keep /learn central in nav
impact_for_humans: positive
impact_for_machines: n/a
outside_inference: false
```

### F-36 — Editorial standards + correction policy + author attribution
```yaml
finding_id: R1-036
category: search-intent-unmet
audience: both
evidence_anchor: {type: url, value: https://xrpldashboard.com/about, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "About already names Charlie Bruce. Add editorial standards, correction policy, publication/update timestamps per author, contact for factual challenges, and a changelog of amended figures"
observation: Author is disclosed; but no correction policy or amended-figures changelog exists.
proposed_improvement: /editorial-policy page with standards, correction history, contact-for-challenges
impact_for_humans: major
impact_for_machines: major
outside_inference: false
```

### F-37 — Snapshot public-key-pinning threat model needs explicit statement
```yaml
finding_id: R1-037
category: machine-surface-gap
audience: machine
evidence_anchor: {type: url, value: https://xrpldashboard.com/about, fetched_at: 2026-09-04T23:50Z}
claim_or_state: "Public-key pinning proves a relationship between a key and signed artifacts if key-discovery surfaces are independently secured. It does not by itself prove underlying metrics are objectively correct, complete, or free of upstream query/calculation errors. The documentation should say that plainly."
observation: Design does the strong thing but claims-limits aren't clearly documented.
proposed_improvement: /security/threat-model page listing what the anchor covenant DOES prove (signing key controls chain_root output) and what it DOES NOT prove (source-node correctness beyond XRPL consensus; no independent-observer redundancy)
impact_for_humans: minor
impact_for_machines: major
outside_inference: false
```

---

## Ranked TOP-10 (human impact)

| Rank | Finding ID | Category | Impact | One-line |
|---|---|---|---|---|
| 1 | F-03 | contradictory-claim | major | /rlusd shows both "Live feed unavailable" AND "Live · updated Ns ago" on same page — real template defect |
| 2 | F-19 | external-claim-fail | blocker | /rlusd number disagrees with external RLUSD supply reporting — needs code trace of rlusd_live.py |
| 3 | F-12 | search-intent-unmet | blocker | no daily "What changed?" narrative — biggest habit-loop gap, 5/5 audit convergence |
| 4 | F-08 | human-ux-gap | major | homepage identity crisis: three products competing for opening hierarchy |
| 5 | F-09 | human-ux-gap | major | raw hex token codes prominent in /tokens ranking |
| 6 | F-07 | human-ux-gap | major | 15+ noun-flat nav; no user-intent guidance for newcomers |
| 7 | F-15 | machine-surface-gap | major | no "How to cite this page" block on data pages |
| 8 | F-16 | human-ux-gap | major | signed-snapshot proof needs one-screen human receipt |
| 9 | F-31 | human-ux-gap | major | /amendments should be flagship — currently one tile among many |
| 10 | F-13 | human-ux-gap | major | no unified cross-site search bar |

## Ranked TOP-10 (machine impact)

| Rank | Finding ID | Category | Impact | One-line |
|---|---|---|---|---|
| 1 | F-15 | machine-surface-gap | blocker | citation-header on every data page (observed_at + ledger + method + canonical URL) |
| 2 | F-27 | machine-surface-gap | blocker | same as F-15 but framed for citation engines (dedup below) |
| 3 | F-28 | machine-surface-gap | blocker | immutable/time-versioned observation URLs — `/rlusd?at=<iso>` or `/rlusd/history/<date>` |
| 4 | F-18 | contradictory-claim | blocker | "verified" taxonomy fragmented across pages — one enum, one glossary |
| 5 | F-29 | machine-surface-gap | blocker | standardize definitions across UI/docs/OpenAPI/llms.txt/agents/claims |
| 6 | F-23 | machine-surface-gap | blocker | per-headline provenance trail (RPC request + response + calc + ledger + observed_at) |
| 7 | F-19 | external-claim-fail | blocker | /rlusd number vs external — code-trace pending Round 2 |
| 8 | F-14 | machine-surface-gap | major | persistent global freshness/status strip |
| 9 | F-26 | machine-discoverability-gap | major | machine surfaces not indexed by search engines (sitemap.xml + link-rel headers) |
| 10 | F-17 | contradictory-claim | major | "trade" semantics — rename or per-headline caveat |

Note: F-15 and F-27 are the same finding phrased for two audiences; treat as one implementation item.

---

## Convergence — confirmed by independent outside audit

The following Perplexity findings independently corroborate items ALREADY on Charlie's roadmap:

| Finding | Status |
|---|---|
| F-12 daily "What changed?" feed | on roadmap |
| F-16 one-screen proof receipt | on roadmap |
| F-09 raw-hex token naming → canonical asset identity | on roadmap |
| F-18 verified/self-described/labeled taxonomy | on roadmap |

Each of these is now attested by 5-of-5 external audits (Claude, ChatGPT, Grok, Gemini, Perplexity). Convergence complete on these four.

---

## Cross-check: where Perplexity was WRONG about the site

| Perplexity claim | Reality (JJ verified) | Explanation |
|---|---|---|
| Machine surfaces "not retrievable" (F-02) | All 8 URLs return 200 with real content across all UAs | Perplexity's fetch layer failed OR search index doesn't surface them. NOT a site block. |
| /amendments "Fetched 2026-08-08T08:55:52Z" (F-04) | Live /amendments shows current timestamp (2026-09-04T23:21Z) | Perplexity read stale content or hallucinated the date |
| /tokens "latest bucket Aug 4" (F-05) | Live /tokens shows "updated 21m ago" | Same as above |
| /about names s2.ripple.com (F-06) | Fixed in commit aa6030a (2026-09-03) — now names ws://127.0.0.1:6007 | Perplexity read pre-fix cached copy |
| Machine-layer score 6.4/10 (F-20) | The score is based on the failed-fetches, not the actual layer | Score is `outside_inference` under this shape — needs re-run against a working fetcher |

All 5 are the same underlying phenomenon: Perplexity's fetch and/or search-index reads stale/incomplete data. The site is genuinely fresher than Perplexity thinks. These are Perplexity-side observability issues, but they surface a real machine-discoverability gap (F-26) worth fixing regardless.

---

## Round 2 read (one paragraph)

**Round 2's job should be: verify Perplexity's flagged internal claims against actual code + fill the code-trace gap for the /rlusd number mismatch.** The strongest hits from Perplexity are (a) a real template defect at /rlusd showing "Live feed unavailable" AND "Live · updated 319s ago" simultaneously (F-03), (b) a specific external-claim mismatch for RLUSD supply (F-19) that Perplexity can't resolve because it lacks code access, and (c) a cluster of "verified taxonomy" / "trade semantics" / "per-headline provenance" findings that require reading `tokens.py`, `rlusd_live.py`, `credentials_state.py`, and the /learn glossary to normalize into one authoritative shape. This is a pure code-path job with editorial-writing weight — trace claims → files → lines, decide the canonical taxonomy, propose the diff. **Claude Fable 5.1 fits.** Its SWE-bench Pro 81.2% and its purposive strengths on code-refactor + spec-doctrine review are exactly the shape of the work. Round 2's bespoke prompt should hand Fable the F-nn shortlist as CONTEXT (verified findings only, not the wrong ones) and ask it to (i) trace each claim to code, (ii) confirm-or-refute Perplexity's finding with file:line evidence, (iii) propose the exact source diff, and (iv) draft the one canonical glossary that resolves F-18 + F-29.

---

## Files this Round 1 produced

- `triage/ROUND1_PERPLEXITY_RAW_2026-09-05.md` — verbatim raw (condensed + full original from PDF)
- `triage/ROUND1_PERPLEXITY_NORMALIZED_2026-09-05.md` — this file (37 findings + top-10s + convergence + Round 2 read)
