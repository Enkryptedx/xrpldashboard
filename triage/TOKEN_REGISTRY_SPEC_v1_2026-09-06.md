# The XRPL Token Registry — build spec v1

**Authored:** 2026-09-06 · JJ · design only, no code
**Prompted by:** Charlie's direction — "If the ledger doesn't know what a token is and nobody's tool does either, that's exactly what we have to build. The dashboard should cover the entire ledger." Claude drafted a 5-layer starting spec; this is the real spec, agreeing / disagreeing point by point.

## Prior-art the spec draws from

- **Etherscan address labels**: crowd-sourced submission + staff moderation; contract source-code verification unlocks a "Verified" checkmark that other tooling downstream trusts. Labels are one-way editable (Etherscan can override); no public revision history.
- **CoinGecko listings**: gated intake (audit + team disclosure + volume threshold + ≥2 exchanges); category assigned by staff after listing; thousands of categories accreted over years.
- **Wikidata statements**: every claim is a property-value pair carrying an explicit `reference` (URL, publication). Full revision history public. `rank` (preferred/normal/deprecated) lets conflicting claims coexist without deletion.
- **PyPI verified URLs** (fetched 2026-09-06): PyPI splits project URLs into `verified` and `unverified`. A URL is verified when the project's `pyproject.toml` references it AND the repo's git provenance proves the package was built there (SLSA-style Trusted Publisher). Green checkmark on the verified subset. This is the exact pattern our two-way toml proof extends.
- **npm namespace claim**: first-writer-wins on the name, but 2FA + deprecation-not-deletion (a bad package can be marked deprecated but URLs keep resolving so downstream doesn't 404). Provenance attestations (SLSA-3+) sign the build so downloaded artifact bytes tie back to a git commit + build system.
- **XRPL-native precedents**: `xrp-ledger.toml` with two-way proof is already a documented convention. XRPL Foundation's "Token Self-Assessments" (linked from xrpl.to) are a hand-graded questionnaire scored 0-3. MPT XLS-89 metadata is on-chain and machine-readable.

## Central design principle (missing from Claude's spec)

**Storage schema is a DAG of provenance, not a bag of labels.**
Every fact, claim, and category assignment is stored as a `(subject, predicate, value, source, timestamp, prior_version_ref)` tuple. Nothing is ever overwritten; conflicting claims coexist and the tier ladder disambiguates them at render time. This is Wikidata's insight applied to XRPL tokens. Without it, you can't answer "who said this token was gaming, when, and citing what?" — which is precisely the question `/check` exists to answer.

Everything below inherits from this shape.

---

## LAYER 1 — FACTS (mechanical)

**Claude's spec (paraphrased):** per (currency, issuer), capture decoded name, Domain, toml two-way proof result, AccountRoot flags, issuer age, trust-line count, holder count, AMM/LP detection, first-party 30d/90d volume, MPT metadata, impostor detection, non-standard-code detection. Every fact carries source + as_of.

**AGREE, with three amendments.**

1. **Rename "source + as_of" to "provenance envelope" and formalize it.** Each fact row = `{value, observed_by, observed_at, ledger_index}`. `observed_by` is the walker or module that captured it (e.g. `token_issuer_flags_walker`, `xrpl_stream`, `manual_backfill_2026-09-06`). `ledger_index` pins the fact to the ledger state at capture — critical because a token's holder-count on ledger 106,800,000 is not the same fact as its holder-count on 106,900,000. Without `ledger_index`, the "as_of" is temporal drift instead of on-chain state.

2. **Split Layer 1 into two sub-tables — L1a (per-issuer facts) and L1b (per-(currency, issuer)-pair facts).** Confusing these was one root cause of tonight's `label_lookup` bug: we had per-issuer Domain + per-pair display + per-pair category all sharing one shape. Explicit split:
   - **L1a `issuer_facts`**: AccountRoot Flags, Domain, TransferRate, RegularKey, has_signer_list, blackholed, first_ledger_seen (issuer age), trust-line count aggregate.
   - **L1b `token_facts`**: decoded name, raw currency code, kind (short/decoded/junk), MPT metadata (asset_class/asset_subclass), AMM-pool memberships, 30d/90d volume, impostor-collision flag, LP-token flag.

3. **Add a `facts_completeness` column per row: 0-100% based on which facts were successfully captured this cycle.** A row where AccountRoot fetch failed but AMM detection succeeded is 60% complete; another row where everything landed is 100%. This is what the coverage-gauge computes against, not "does the row exist."

**Disagreement:** Claude's "this layer alone gives every token a verification lane in the visual" is right in principle but under-specified. The verification lane depends on WHICH facts landed, not whether the row exists — a row with `facts_completeness=30%` isn't provably in any lane yet. Explicit rule: a token can be shown in the `bare` lane only when `L1.facts_completeness >= 80%` AND all mechanical checks (impostor, LP, junk-hex) have run. Otherwise render `pending`.

## LAYER 2 — CLAIMS (self-described)

**Claude's spec:** toml `[[TOKENS]]` name/desc/weblinks, Domain, MPT metadata + **an ISSUER SELF-SUBMISSION path**: issuer submits category + description, accepted only if two-way toml proof passes. Accepted claims render as `self-described` immediately, no curator needed. This is how the registry scales past one human.

**PARTIALLY AGREE — three important corrections.**

### 2.1 Two-way toml proof is necessary but not sufficient

The two-way proof (Domain → toml on that domain that names the issuer address) proves **ownership of the issuer account by whoever controls the DNS/webserver at that Domain**. It does NOT prove the CLAIM about the token is true. An issuer can:
- Own their Domain (proof passes) AND publish `category = "stablecoin"` for a token that is nothing of the kind.
- Own a Domain that's a spoof of a legit brand's name.
- Sell their issuer key to a scammer who inherits the toml automatically.

**So:** the two-way proof is a floor, not a ceiling. Self-submitted claims land at tier `self-described`, never `verified`. Only a curator's citation + review promotes to `verified`. This is Wikidata's "reference required for statements" rule applied here.

### 2.2 Three sub-layers of "self-described," not one

Claude collapses toml, MPT metadata, and self-submission into one L2. That erases the important distinction: a claim in MPT metadata is **on-chain** (immutable at ledger-time, can't be silently rewritten to attack a stale reader); a toml claim is **off-chain** (can be rewritten by whoever controls the domain at read-time); a self-submitted-via-form claim is **in-our-database** (whatever provenance we require). These fail differently and need to render differently.

- **L2a on-chain self-declaration**: MPT `asset_class`/`asset_subclass`, AccountRoot `Domain` field. Immutable at their ledger_index. Highest self-declaration trust.
- **L2b off-chain toml declaration**: `[[TOKENS]]` section in xrp-ledger.toml, gated by two-way proof passing at read-time. Mutable but proved.
- **L2c form self-submission**: issuer POSTs a category + description to our form. Requires two-way toml proof (they've already published a toml, we've already fetched it, we cross-check the submission address against toml `[[ISSUERS]]`).

The badge on `/check` and `/token` shows WHICH sub-layer the self-description came from. `self-described (via MPT metadata)` is different from `self-described (via issuer form)`.

### 2.3 Reserved-name (impostor) rule overrides self-submission

A self-submitted claim for a currency code that matches the impostor list (USDT/USDC/BTC/RLUSD/etc.) but whose issuer is NOT in `canonical_issuers.json`: the mechanical impostor tier ALWAYS wins. Their self-description gets accepted into L2 storage but the badge renders `impostor: USDT · mechanical` regardless of their claimed category. Charlie's `verify_before_verdict` rule applied here.

## LAYER 3 — CURATION (human, cited, append-only)

**Claude's spec:** category + citation + curator + timestamp + prior value; never overwritten (history table); `/admin/token-review` queue prioritized by volume, holder count, new-and-hot, impostor flags, issuer self-submissions.

**AGREE strongly.** Only two additions.

### 3.1 Multi-curator provenance

Charlie is one person today. When a second curator lands (whether human or a future JJ+2), the schema must distinguish:
- `curator_id`: which curator made this call
- `curator_authority_at_time`: what were their permissions at the time (Charlie is `owner`; a delegated curator might be `contributor`)
- `citation_url`: the primary source
- `citation_secondary`: additional corroborating sources when applicable
- `superseded_by`: null unless a later revision supersedes this one; then points to it

Wikidata does this and it's why they can survive 20 years of accreted edits without becoming a mess. Etherscan does NOT do this well (labels are edited in-place) and their credibility took hits when Etherscan staff quietly changed labels without notice.

### 3.2 Queue prioritization is a Charlie-facing product, not an internal detail

The queue defines what Charlie sees when he sits down for 20 minutes. Get this wrong and the registry stalls. Better prioritization signal:

1. **Impact score** = `log10(30d_volume + 1) * log10(distinct_holders + 1) * impostor_flag_multiplier(1.0-5.0) * self_submission_multiplier(1.0-3.0)`.
2. **Freshness bonus** for issuers < 30 days old with rising trades (surface new memecoins/rugs before they scale).
3. **Dispute weight** — the existing `/contact?purpose=attestation-dispute` route already accepts reader challenges; those jump the queue.
4. **Random 5% "long tail sampler"** — force the queue to occasionally surface a low-volume token so curation coverage isn't a rich-get-richer trap.

Queue is rendered with one-click "verify as [category from L2 self-submission]" / "reject with reason" / "re-categorize" actions. Curator time-to-decision should target <30s per row for the fast-path decisions.

## LAYER 4 — INFERENCE (assist only, never public)

**Claude's spec:** heuristic scoring PROPOSES a category to the queue with reasoning; curator accepts or rejects; heuristic output never renders on a public badge.

**AGREE completely.** This is the load-bearing rule. Every other honesty rule (`flattering_false_facts`, `no_fake_data_connections`, `no_50_50_editorial_machine_split`) depends on it holding.

**One addition — inference log is public.** The heuristic's reasoning ("issuer address starts rBEARGUA + no toml + name is Rick&Morty reference → memecoin, confidence 0.85") should be visible on the `/token/<cur>/<iss>` detail page as `"Machine assessment (not verified):"` even when a curator hasn't reviewed it yet. Two reasons:
1. Transparency — readers see what our heuristic thinks, weighted honestly against "no curator has ruled here yet."
2. It surfaces where the heuristic is systematically wrong so we can improve it — invisible failures never get fixed.

The rule is: heuristic never renders as a colored/verified badge. It CAN render as gray italic explanatory text below the honest "unlabeled" badge.

## LAYER 5 — PUBLIC SURFACES

**Claude's spec:** `/tokens` searchable directory; `/token/<c>/<i>` shows every fact + claim + citation + history; `/check` uses the same rows; JSON twins; signed anchored daily snapshot; coverage gauge public.

**AGREE, three amendments.**

### 5.1 Signed snapshot uses the RECEIPT key, not the anchor key

Per the receipt-signing design note from earlier today, `/check` receipts are signed with a separate Ed25519 receipt key. Registry snapshots should be signed with the SAME receipt key (or another dedicated key under the same discipline — never with the snapshot/anchor key that gates chain-of-custody claims about historical ledger state). Domain separator: `"xrpldashboard/registry/v1"`. Signing service refuses any payload that doesn't match the frozen registry-snapshot schema.

### 5.2 Coverage gauge — publish THREE numbers, not one

Claude proposes "% of 30d volume with a curated category / % of tokens with any claim / % unlabeled." That's the right shape but the middle number is dangerous — a spam-submitted claim counts the same as a real one. Publish these three, each with what it means labeled next to it:

- **Coverage-by-volume (headline):** `% of last-30d trade volume from tokens with a curator-verified category`. This is the metric that matters — moves as the top-volume tail gets curated.
- **Coverage-by-count:** `% of active-issuer tokens (30d trades > 0) with any structured category signal (curator OR MPT OR toml)`. Different denominator, different message.
- **Attestation health:** `# of tokens with a curator-verified category + citation URL`. Not a percentage — an absolute count, because it's the load-bearing artifact and Charlie's editorial output.

Under each number: a link to the underlying query so a skeptic can reproduce the count.

### 5.3 Machine surfaces need registry-version pinning

`/tokens.json` and `/token.json` must carry a `registry_version` field per response. An agent that made a decision based on `registry_version = 42` and later wants to explain that decision needs to look up what taxonomy + coverage was live at `v42`. Wikidata does this via revision IDs. Without it, our machine responses aren't cite-able. This is a natural fit with the daily signed snapshot: `v42` = the snapshot signed on 2026-09-14.

### 5.4 Public downloadable dataset (free) + real-time bulk API (paid)

Agree with Claude's "public good + paid-tier bulk access" split. Refinement:
- Free forever: nightly SQL dump + Parquet + one JSON of the full registry, downloadable at `/dataset/registry/latest.zip`. Includes signed snapshot header.
- Paid tier: real-time streaming subscription (webhook or SSE) that fires on every registry-row change. Rate-limited hourly-snapshot access. Bulk category-mass-lookup calls. Not the DATA — the FRESHNESS + THROUGHPUT.

Being clear: the fact that a scammer spun up a new impostor token 5 minutes ago is the paid signal. The fact that RLUSD is a stablecoin curated at ripple.com is free forever.

---

## Missing from Claude's spec (net-new additions)

### M1 — Deprecation, not deletion

A token that stops trading for 90+ days isn't removed from the registry — it's marked `dormant`. Historical curation and volume stays accurate; the token can revive without losing history. Prevents the npm-left-pad problem (removing a package breaks downstream). Renders on `/token/<cur>/<iss>` as `Dormant since <date> — last trade <bucket>`.

### M2 — Bot/spam guard on the self-submission path

Two-way toml proof isn't a bot filter — a bot can publish a toml. Additional gates on the L2c form:
- Rate limit: 5 submissions per issuer per week.
- Verification email or Telegram DM to Charlie for the FIRST submission from any (issuer, domain) pair, then auto-accept for subsequent (queued for review, not blocked).
- Duplicate-submission dedup: submitting the same claim in ≤7 days = noop.
- Anti-hijack: if the toml at the domain changes materially, all pending submissions from that domain get flagged for curator review.

### M3 — Rename `unlabeled` explicitly; retire `other`

Charlie's rule from earlier: `unlabeled` is a confession, not a category. Every place `other` currently appears in the codebase (`token_names.json`, `label_lookup`, `LANE_ORDER`, template strings) migrates to `unlabeled`. `other` becomes reserved for a category the taxonomy explicitly names (e.g. `defi_other`, `rwa_other` where the parent class is known but the subclass isn't) — not a catch-all.

### M4 — Registry-anchored dispute path

Reuse the existing `/contact?purpose=attestation-dispute&ref=token:<cur>.<iss>` flow. When a dispute lands:
- Auto-open a curator queue entry with dispute weight = highest priority
- Every curation history row can be marked "disputed" without losing the prior decision
- Public /token page shows a small "1 dispute pending" line linked to the transparency page (not the dispute details — those may contain PII)

### M5 — Multi-signature verification path (future)

Some tokens (Bitstamp, GateHub, RLUSD) have multi-sig issuer accounts. When our L1 `has_signer_list = true`, allow a self-submission that requires ANY signer key to sign the submission payload (not just the master key). This closes a subtle gap: an issuer that disables master and only signs via SignerList can currently prove Domain ownership via TOML but has no way to sign a self-submission because their master is dead.

### M6 — API rate-limit tiers reflect verification tier, not identity

Free-tier callers get FULL registry data but slowed proportional to how "expensive" the query is. Bulk-category-scan of 10k rows = 1 request per minute. Single-token lookup = 60 per minute. Impostor-collision batch check = 20 per minute (used by wallet apps to warn users). No paywall on the correctness signal.

### M7 — The Charlie-facing metrics dashboard

Curator health is a first-class product. `/admin/registry-health` shows:
- Impact-weighted uncovered volume (what's Charlie NOT getting to)
- Time-to-decision distribution per queue category (how fast are we moving)
- Self-submission acceptance rate (are issuers submitting quality claims)
- Dispute-to-verification ratio (how often are we wrong)
- Snapshot-signing streak (uninterrupted daily-signed days)

This dashboard is what tells Charlie when to hire a second curator, when the queue is drowning, when the self-submission funnel is broken.

---

## Open decisions (Charlie's call)

1. **Do we accept L2c form submissions today, or gate behind the receipt-key infrastructure?**
   Argument for now: two-way toml proof is sufficient for `self-described` tier; snapshots and receipts can wait.
   Argument for later: rendering "self-submitted" needs a stable machine-verifiable signature so downstream can trust the claim survived our storage. Ship after receipt-key generation.

2. **How much of the historical curation lives in JSON vs Postgres?**
   Claude implicitly picked Postgres. Argument for keeping `token_names.json` as the human-editable source-of-truth mirror and having Postgres be the append-only projection: git-history of curation stays legible and portable. Argument for Postgres primary: multi-curator + concurrent-write + queue integration is Postgres-native.
   My recommendation: **Postgres primary + nightly JSON export to git for portability + reproducibility.**

3. **Does the coverage-gauge become a public dashboard or a leaderboard?**
   Leaderboard framing ("we're at 14.0% and climbing" with a chart) creates goodhart-pressure to inflate the number by over-curating unimportant tokens. Dashboard framing ("here are the top-N uncurated by volume") stays honest but is less shareable.
   My recommendation: dashboard for the honest signal + a single number in the site footer that reads `Registry coverage: 14.0% of 30d volume · updated <ts>`. No chart, no growth-hype.

4. **Do we build our own token-self-assessment questionnaire (XRPL Foundation-style), or federate to theirs?**
   Federation reduces our work but ties our credibility to theirs. Own questionnaire is more work but keeps the trust surface ours.
   My recommendation: **federate to theirs for now** (they've done the work; their questionnaire URLs are already in xrpl.to's data as `assessment` field). Ingest as `third-party: xrplf-assessment` tier. Revisit if their maintenance falters.

5. **Is the registry a project, a public-good publication, or a business asset?**
   All three at once is possible (Wikidata is all three). If explicitly business-asset, the paid-tier + attribution rules need to be baked in from day 1. If public-good, we set up the export + license (CC-BY 4.0?) explicitly so downstream can rely on it.
   My recommendation: **publicly-licensed public good with attribution requirement (CC-BY 4.0); paid tier for freshness + throughput only.** This is npm/PyPI's model and it's why they became infrastructure.

---

## Ship order (my recommendation)

1. **Repackage taxonomy + rename `other` → `unlabeled`** (30 min editorial, no schema change)
2. **Split `token_facts` and `issuer_facts` in Postgres** (~4h, migration + walker rewrite)
3. **Provenance envelope on every fact + claim row** (~4h, schema + write helpers)
4. **`/admin/token-review` queue with priority scoring** (~1d, no self-submission yet)
5. **L2c form self-submission with two-way toml proof gate** (~2d, after key generation)
6. **Signed daily snapshot with dedicated registry key** (~1d after key gen)
7. **Public coverage-by-volume gauge** (~2h once schema is stable)
8. **Public downloadable dataset** (~1d)
9. **Paid-tier freshness/throughput API** (~1-2 weeks incl. billing wiring)

Steps 1-3 are prerequisites for everything else. Step 4 unlocks Charlie's daily curator loop. Step 5 is the scale lever — nothing else matters as much if issuers won't self-submit.
