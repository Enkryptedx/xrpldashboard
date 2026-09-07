# Round 2 brief — for Claude Fable 5.1 (code-trace + spec/taxonomy)

**Source of findings:** `triage/ROUND1_PERPLEXITY_NORMALIZED_2026-09-05.md`
**Baseline:** commit `292737c` (anchor #5) + Round 1 walkthrough delta noted where relevant
**Round 2 shape:** trace → confirm-or-refute → propose diff. Proposals only, no commits.

## What Round 1 actually verified

23 actionable findings after filtering out:
- 5 `WRONG-ABOUT-SITE` findings (Perplexity read stale/cached content or hallucinated timestamps; F-02, F-04, F-05, F-06, F-20)
- 5 `outside_inference` findings (about our internals — pass through to Round 2)

Plus 2 verified in the /rlusd trace tonight (Sep 4 EDT evening after Round 1):
- **F-03 correction** — my earlier "template renders both strings simultaneously" was a false alarm. Template uses CSS `.ribbon { display: none; }` and only unhides via JS `.show` after 2 consecutive JS poll failures. Both strings in HTML source is intentional (SSR both, JS shows one). Users see only ONE at a time. Not a defect.
- **F-19 correction** — Perplexity's `$896M XRPL / $697M ETH` reading was WRONG. Actual cache right now: XRPL $1.030B, ETH $1.377B, total $2.407B — matches recent Ripple-linked coverage ($1.024B XRPL). Cache freshness: ~320s. Not a bug.

**New real finding surfaced during trace:** `walker_health.rlusd_refresher` shows `ok=False, last_run_completed=None` DESPITE the cache being freshly written every ~5min. Silent-write walker health-row bug — cache writer isn't updating its own health row. Filed as F-38 below.

## Findings to trace (grouped for Fable's work)

### Group (i) — TAXONOMY (highest-value code work)

Perplexity flagged this cluster repeatedly. It requires reading multiple files + drafting one canonical glossary.

**F-18 — "verified" taxonomy fragmented across pages**
- `/tokens` uses `√ verified`, `self-described`, `bare`, `labeled`
- `/about` describes hand-curated names AND cryptographic Domain + `xrp-ledger.toml` two-way attestation
- Homepage has `identity claim`
- Same word ("verified") sometimes means "we have a display-name source" and sometimes means "two-way cryptographic issuer attestation"

Fable's job:
1. Grep all files/templates that use `verified`, `self-described`, `bare`, `labeled`, `named`, `identity claim`, `attested`
2. For each usage, trace what the code actually validates against
3. Propose ONE canonical enum with N levels (JJ's guess: `unlabeled` / `named` / `self-described` / `domain-attested` / `two-way-verified`; Fable may propose a different shape)
4. Draft a `/glossary.json` file referenced from every surface (HTML, docs, OpenAPI, llms.txt, agents manifest, claims, API responses)
5. Propose the source diff to unify — but don't ship

**F-29 — standardize definitions across UI/docs/OpenAPI/llms.txt/agents/claims/API**

Same shape as F-18 but for the wider set: `trade`, `volume`, `supply`, `circulating supply`, `whale`, `verified`, `self-described`, `label`, `claim`, `sanctions hit`. Same job — grep, trace, propose one glossary, propose diff.

**F-17 — "trade" semantics (Payments + AMM deposit/withdrawal ≠ market trades)**

Charlie already asked for wording options in Part 2 of tonight's ruling (I'll send those separately). Fable's job on this: verify the current `/tokens` code definition matches what the page copy says, and if Charlie picks a rename, propose the diff across `tokens.py` + `templates/tokens.html` + `openapi.json` + `agents.json` + `llms.txt`.

### Group (ii) — PROVENANCE (blocker for machines)

**F-23 — per-headline provenance trail**
Every headline number should carry: raw RPC request + response + calculation + ledger + observed_at. Currently only prose disclosure on `/about`.
Fable's job: pick 3-5 highest-traffic data pages (`/amendments`, `/rlusd`, `/tokens`, `/mpts`, `/network`), map each headline number to its producer code path, propose a shared `<citation-anchor>` component that renders per-figure provenance.

**F-15 / F-27 — citation header per data page**
Same as F-23 but at the page level. Format: `Observed at: <ISO> · Validated ledger: <index> · Method: <version> · Source endpoint: <hostname> · Cache age: <s> · Canonical: <URL> · Machine export: <JSON URL>`.
Fable's job: propose the citation header template + list which pages need it + note where it already partially exists (e.g. RLUSD chip).

**F-28 — immutable observation URLs**
Time-versioned URLs (`/rlusd?at=<iso>` or `/rlusd/history/<date>`) that resolve to signed immutable observations. Requires backfill from `rlusd_supply_history` (already exists!) or the signed_snapshots chain.
Fable's job: propose the URL scheme + which data pages should support it + how it interoperates with the anchor chain.

### Group (iii) — DISCOVERABILITY / MACHINE SURFACE HYGIENE

**F-14 — persistent global freshness/status strip**
Nav has liveness chip (Ledger # · age). Extend it to include source node + degraded-mode flag per page.
Fable's job: propose extension to `_liveness_chip.html` + a shared `page_sourcing_context()` helper.

**F-26 — machine surfaces not indexed by search engines**
`llms.txt`, `agents.json`, `openapi.json` are all reachable directly (200 OK all UAs) but don't surface in search. Two-step fix: (a) add to `sitemap.xml`, (b) add `<link rel="alternate" type="application/json">` in HTML head.
Fable's job: check current `sitemap.xml` (is it complete?), propose additions + head-link injection points.

**F-25 — /wallet + /token disallowed in robots.txt**
Design call — Charlie's intent is probably "per-user lookups shouldn't be crawled." But this blocks agents from ever crawling wallet detail pages.
Fable's job: none code-side; note for Charlie decision. If keeping disallow, propose sitemap.xml entry for "top known wallets" only. If removing, propose the robots.txt diff.

### Group (iv) — CONTENT / EDITORIAL / SOVEREIGNTY

**F-30 — weekly signed "XRPL Verified Findings"**
Ship a `/findings/YYYY-MM-DD` report scheme with stable URLs + embeddable widgets + RSS.
Fable's job: propose the route + template + minimal data model. No implementation, just the design shape + how it links to the existing signed_snapshot chain.

**F-36 — editorial standards + correction policy + author attribution**
`/editorial-policy` page.
Fable's job: propose page structure + how it references the existing anchor chain for correction transparency.

**F-37 — snapshot public-key-pinning threat model needs explicit statement**
`/security/threat-model` page listing what the anchor covenant proves vs doesn't.
Fable's job: draft the page copy grounded in `docs/ONLEDGER_ANCHOR_SPEC.md` § "Compromise model."

### Group (v) — WALKER HEALTH BUG (found during /rlusd trace tonight)

**F-38 (new) — walker_health.rlusd_refresher shows ok=False despite fresh cache writes**
```yaml
finding_id: R1-038
category: machine-surface-gap
audience: machine (indirect: this is what `walker_health_summary` anchored metric reads from)
evidence_anchor:
  type: db-query
  value: SELECT walker_name, last_run_completed, last_run_ok FROM walker_health WHERE walker_name='rlusd_refresher'
  fetched_at: 2026-09-05T00:26Z
observation: rlusd_state_cache is being written every ~5min (payload has fresh $1.030B XRPL + $1.377B ETH, sources.via=192.168.40.95:5006 = Mac walker via LAN). But walker_health row for rlusd_refresher shows last_run_ok=False, last_run_completed=None, consecutive_failures=0. Someone is writing the cache without calling write_walker_health_end.
proposed_improvement: trace who calls db.write_rlusd_state_cache() — is it via a walker wrapper that forgets to update walker_health? If Mac walker: add write_walker_health_end(). If Render's rlusd_live.fetch_state() writing on every request: this is expected — the walker_health row for rlusd_refresher can be dropped OR renamed to reflect "on-demand refresher."
impact_for_humans: n/a (invisible to visitors)
impact_for_machines: major (walker_health_summary is an ANCHORED meta-metric; a walker that appears dead in every future snapshot is a false-negative that erodes anchor credibility)
outside_inference: false
```
Fable's job: trace + propose fix.

### Group (vi) — HUMAN UX (not code-shaped, park for R3/R4)

These are worth capturing but not in Fable's remit:
- F-07 flat nav / 15 nouns
- F-08 homepage identity crisis (three products)
- F-09 raw-hex token codes prominent
- F-10 CLARITY Act duplicated
- F-11 terminology density
- F-12 daily "What changed?" (roadmap)
- F-13 no unified cross-site search
- F-31 /amendments should be flagship
- F-32 cut "procurement-ready" institutional framing

These go to Round 3 (Astra machine-surface / interaction) or Round 4 (Gemini multimodal / cross-cutting UX).

### Group (vii) — POSITIVE findings (protect + expand)

- F-21 source disclosure is unusually good
- F-22 "we don't guess" editorial discipline
- F-33 signed-snapshot public key + fingerprint pin
- F-34 wallet inspector no-keys reassurance
- F-35 /learn's progressive-explanation model

Fable's job: note these in the output as "protect" — don't diff them out during other work.

## What Round 2 is NOT doing

- No deploys, no commits, no pushed changes. All output is proposals.
- No re-audit of Perplexity's findings that we already refuted (F-02, F-03, F-04, F-05, F-06, F-19, F-20 — all listed in ROUND1_NORMALIZED with cross-check).
- No new invention. If a finding needs work Fable can't do (needs Charlie decision, needs product design), say so and stop.
- No YAML shape for output — Fable can use its own preferred format; JJ normalizes into the master findings list afterward.

## Handoff to JJ

Fable's output goes to `triage/ROUND2_FABLE_RAW_2026-09-05.md`. JJ (Opus 4-7) will then:
- Read Fable's proposals
- Verify each proposed file:line against the live repo
- Reconcile with any Charlie decisions (F-17 wording, F-25 robots)
- Propose a concrete implementation order for the next Fix window (post-R2)

Round 2 ends when Fable's output + JJ's verification + Charlie's rulings on the taxonomy shape are all in.

## Family caveat (why Fable + JJ trades diversity for depth)

Both Fable 5.1 and JJ (Opus 4-7) are Anthropic-family. This round deliberately optimizes for code depth (Fable's 81.2% SWE-bench Pro + long-context refactoring) at the cost of family-diversity. The judgment: Round 1 (Perplexity) established the outside view and its stale-fetch flakiness confirmed a family with strong direct-fetch and code-execution is what R2 needs. Rounds 3 (GPT-6 Astra) and 4 (Gemini 3.1 Pro) will restore family diversity — Astra via computer-use for machine-surface driving, Gemini via multimodal for what-the-user-actually-sees.
