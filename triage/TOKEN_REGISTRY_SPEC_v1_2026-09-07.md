# The XRPL Token Registry — build spec v1, Layer 6 + ecosystem move + scale plan

**Authored:** 2026-09-06 23:45 UTC · JJ · design only, no code
**Extends:** [TOKEN_REGISTRY_SPEC_v1_2026-09-06.md](./TOKEN_REGISTRY_SPEC_v1_2026-09-06.md) (Layers 1-5, provenance-DAG principle, ship order steps 1-9)
**Prior-art fetched live 2026-09-06:** [XRPL Standards README](https://raw.githubusercontent.com/XRPLF/XRPL-Standards/master/README.md) (XLS purpose/scope), [xrp-ledger.toml docs](https://xrpl.org/docs/references/xrp-ledger-toml) (RFC 2119 keywords, HTTPS+/.well-known/ convention, Domain two-way proof). CoinGecko/Etherscan API endpoints returned 403/404 to direct fetch; my model of those systems draws from prior knowledge.

---

## LAYER 6 — GOVERNANCE

The registry is only as useful as its promise of accuracy. Governance is what keeps the promise durable when scale exposes the seams. Six artifacts, published together, that let a reader (or a court, or an integrator) audit any decision the registry made:

### 6.1 Taxonomy v1 definitions doc

**Filename:** `docs/registry/taxonomy_v1.md` — one page, publicly reachable at `xrpldashboard.com/registry/taxonomy` and mirrored in the repo. Every category from the 12-slot vocabulary gets exactly four lines:

- **NAME + one-line definition** (what it means in plain English)
- **RULE** (the mechanical or evidence-based test that assigns it — see §3a of the 09-06 spec)
- **EVIDENCE SOURCE** (which layer produces the signal — L1 mechanical / L2a on-chain / L2b toml / L2c form / L3 curator)
- **BOUNDARIES** (what this category IS NOT — the adjacent categories a token could belong to instead, and why they were excluded)

The boundaries section is load-bearing. Reader question "why is X in memecoin and not community?" gets answered by the boundary text, not by asking Charlie. Wikidata's `Q-item talk pages` failed because boundaries drifted; the taxonomy doc has to freeze them per version.

Format is copy-pastable to a tweet (each category ≤280 chars for the definition) so integrators can cite a specific line when they disagree.

### 6.2 Changelog

**Filename:** `docs/registry/CHANGELOG.md` — SemVer for the taxonomy itself. Entries look like:

```
## taxonomy 1.2.0 — 2026-10-15
### Added
- `defi_yield` category (rule + boundaries below)
### Changed
- `memecoin` boundary widened to cover political/season memes
  (previous: "no protocol claim"; new: "no protocol claim AND
   >=5 trustlines"; motivation: ambient noise tokens with 1-2
   trustlines are more accurately "unlabeled")
### Deprecated
- (none)
### Removed
- (none this cycle; removals require a MAJOR bump per §6.5)
```

**Why SemVer matters:** downstream (agents, integrators, the paid-tier stream) needs to know when a rule changed vs when a data row changed. Wikidata's `revision id` per statement + our `registry_version` per snapshot = the same idea; the changelog is the human-facing narrative that explains each version bump.

### 6.3 Dispute / appeal path

Reuses the existing `/contact?purpose=attestation-dispute&ref=token:<cur>.<iss>` flow. Governance additions:

1. **Disputant identity is optional but recorded.** A pseudonymous dispute is valid; an authenticated dispute (Xaman signature over the dispute payload from the ISSUER account) jumps to the top of the queue because it's a first-party challenge.
2. **Curator has 7 calendar days to acknowledge.** Beyond 7 days, the token's public `/token` page shows an amber "dispute pending — unreviewed" line so the reader knows we owe an answer. Silence is not confirmation.
3. **Every dispute resolution writes to the curation history table**, even if the outcome is "no change." A dispute that produced no change is itself a data point about the taxonomy's stability.
4. **Appeal path:** if the disputant disagrees with the curator's resolution, one appeal is available — reviewed by a different curator (once we have two) OR by an external reviewer named on the taxonomy doc. Appeals close in 14 days. No infinite loops.
5. **Bad-faith disputes** (Sybil, script, or clearly-not-in-good-faith): rate-limited per issuer-key AND per originating IP-hash, with a floor of "always accept at least the first dispute per calendar month per (issuer, token)." Never a total block — the appeal path stays open.

### 6.4 Attorney items — the wording rules

Three lines Charlie's counsel needs to sign off on before Layer 5 goes public:

**(a) The impostor label.** Public rendering describes a MECHANICAL FACT ONLY, never a character judgment:

> "USDT · Ticker collision: this token's currency code decodes to a well-known off-chain ticker (USDT / Tether), but its issuer address is not on our list of canonical Tether-authorized issuers. This is a statement about issuer provenance, not about the token's intent or the issuer's character."

Never: "this token is a scam" or "this token is fake USDT." Never: "the issuer is malicious." The badge is a description of what we mechanically observed, not a judgment. The `impostor` category name is fine internally but the RENDERED text always uses "ticker collision" or "ticker impersonation" (a description of the collision, not the actor).

**(b) The self-submission terms of service.** When an issuer POSTs to L2c, they see a one-screen agreement:

> "By submitting, you represent: (1) you control the issuer account named; (2) the claims you submit are accurate to your knowledge and belief; (3) you accept that xrpldashboard renders your submission at the `self-described` tier unless and until an independent curator promotes it to `verified`; (4) you accept the dispute + appeal process described at xrpldashboard.com/registry/disputes; (5) xrpldashboard reserves the right to remove submissions that violate the impersonation, illegality, or spam terms in this ToS. Your submission is public. Your issuer address, timestamp, and submitted category are permanently recorded in an append-only history."

Public, permanent, and honest about the tier limitation. The two-way toml proof isn't legal consent — the ToS click-through is.

**(c) The dataset license + attribution.** Publishing the free downloadable dataset (per Spec §5.4) means we authorize downstream use. Recommend **CC-BY 4.0** with attribution string `"Registry data from xrpldashboard.com (CC-BY 4.0); accessed <ISO date>"`. Explicit disclaimer: the dataset is a public-good publication, not a certification of any token's status; downstream tools relying on it for financial decisions do so at their own risk.

### 6.5 Version bump policy

- **PATCH** (1.2.0 → 1.2.1): typo, one-word wording tweak, or a boundary clarification that doesn't reclassify any existing token.
- **MINOR** (1.2.0 → 1.3.0): new category added, or a boundary widened/narrowed such that some tokens legitimately move between adjacent categories (documented in changelog with counts).
- **MAJOR** (1.x → 2.0): category renamed, removed, or the taxonomy shape restructured (e.g. splitting `defi` into `defi_lending` + `defi_yield` + `defi_perps`). Requires a 30-day preview period during which downstream can migrate; the daily signed snapshot ships BOTH v1.x and v2.0 during preview.

### 6.6 Anti-capture rules

Governance's least-fun job. Three rules that keep the registry from being captured by any single interest:

1. **No exchange, no market-maker, no issuer may be given elevated curation authority.** Contributor curators (when we have them) publish a conflict-of-interest disclosure kept next to their signed commits.
2. **No paid category.** The verified badge cannot be sold. Ever. This is npm/PyPI's rule and it's why they didn't go the way of some directory services.
3. **Delisting requires appeal path exhaustion.** A "remove from public" request from an issuer requires the full appeal cycle, not a curator's unilateral call. Prevents any single actor from erasing history.

---

## THE ECOSYSTEM MOVE — draft XLS proposal

If the ledger doesn't know what a token is because no standard tells issuers where to declare a category, **write the standard**. Modeled on the XRPL Standards format (fetched today from the XLS README) and referenced against how XLS-#558 (NFT issuer registry) framed a similar off-chain-declaration problem.

### XLS-YYYd (draft, number TBD by XRPL Standards WG) — Token Category Field in `xrp-ledger.toml`

**Type:** Draft — Standard
**Layer:** Ecosystem convention (off-chain, off-protocol)
**Requires:** [xrp-ledger.toml](https://xrpl.org/docs/references/xrp-ledger-toml) domain-verification convention
**Extends:** the existing `[[TOKENS]]` section
**Author:** xrpldashboard (proposal only; adoption by XRPLF working-group required for standard status)

#### Abstract

A machine-readable `category` field for the existing `[[TOKENS]]` section of an issuer's `xrp-ledger.toml`. Issuers declare what class of token they are running; wallets, explorers, and analytics tools consume the same vocabulary rather than each vendor rolling their own. Bootstrapped from the XRP Ledger token registry taxonomy v1 (12 categories, defined and versioned at a public URL).

#### Rationale

- The XRPL has ~10.6k active IOU issuers and ~300 MPTs. Every downstream tool (xrpl.to, xrpscan, Bithomp, Sologenic, xrpldashboard) currently invents its own category vocabulary or foregoes categorization entirely (established today via live catalog inspection). Readers see contradictory labels for the same token across tools; scammers exploit the ambiguity.
- MPT XLS-89 metadata proved that machine-readable category fields work when the standard exists: 31/302 mainnet MPTs populate `asset_subclass` today. The `[[TOKENS]]` section already carries issuer-authored metadata but has no category convention. This proposal fills that gap.
- Issuers WILL adopt because it costs them one line in a file they're already publishing, and it's the fastest path to being correctly categorized across every downstream tool that adopts the same convention.

#### Specification

Add ONE optional string field to each `[[TOKENS]]` entry in `xrp-ledger.toml`:

```toml
[[TOKENS]]
issuer     = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
currency   = "524C555344000000000000000000000000000000"
name       = "Ripple USD"
desc       = "Ripple's fiat-backed USD stablecoin on the XRP Ledger"
category   = "stablecoin_regulated"       # <-- NEW
category_taxonomy = "xrpldashboard/v1"    # <-- NEW (identifies vocabulary)
```

**Field: `category`** — REQUIRED when `category_taxonomy` is set. String value MUST be drawn from the vocabulary identified by `category_taxonomy`. If the value is not in the vocabulary, consumers MUST treat the category as `unlabeled` (silent fallback — never a hard error).

**Field: `category_taxonomy`** — REQUIRED when `category` is set. String identifying the taxonomy vocabulary in `<publisher>/<version>` form. Publisher is a stable identifier (a domain or a shorthand agreed by XRPLF); version is SemVer.

**Default v1 vocabulary** (12 slots, defined at `xrpldashboard.com/registry/taxonomy` — the RFC will point at a URL, not enumerate inline, so vocabulary evolution doesn't require an RFC bump):
`stablecoin_regulated`, `stablecoin_gateway`, `native_utility_chain`, `dex_utility`, `defi_lending`, `defi_yield`, `gaming`, `wrapped_bridge`, `rwa`, `lp_token`, `memecoin`, `unlabeled`.

**Verification.** A consumer that sees a `category` field MUST first verify domain ownership per the existing xrp-ledger.toml two-way proof (issuer's `Domain` field matches the domain serving the toml, AND the toml's `[[ISSUERS]]` section includes the issuer address). If either check fails, the `category` field is present-but-untrusted; consumers MUST NOT show it as issuer-attested.

**Fallback.** If an issuer omits `category`, consumers infer via whatever local method they use today. This standard is additive; existing tomls remain valid.

#### Security considerations

- **Self-declared, not verified.** A passing two-way proof asserts issuer-account ownership; it does NOT assert the category claim is accurate. Consumers SHOULD render self-declared categories with a `self-described` tier badge, distinct from third-party or curator verification.
- **Impersonation category.** Consumers SHOULD maintain a canonical-issuer registry for well-known off-chain tickers (USDT, USDC, BTC, etc.); a `category = "stablecoin_regulated"` claim from a non-canonical issuer MUST NOT override the impersonation warning at render.
- **Vocabulary evolution.** A consumer that encounters `category_taxonomy = "xrpldashboard/v2"` after adopting `v1` MUST silently fall back to `unlabeled` rather than misinterpret an unknown vocabulary term.

#### Deployment path

1. **Draft published** as XLS-YYYd on the XRPL Standards repo (7 days review window per XLSF convention).
2. **Reference implementation** — xrpldashboard's `token_issuer_flags_walker` (already fetches toml + parses `[[TOKENS]]`) extended to read the new field. Ship as first-mover consumer.
3. **Ecosystem-adopters recruit** — xrpl.to, xrpscan, Bithomp, Sologenic pinged with the draft. Adoption by 2+ of them elevates the proposal to `Standard`.
4. **Issuer-onboarding writeup** — one blog post, one Twitter thread, and one PR to XRPL Foundation's ecosystem docs showing the one-line addition. Bootstrap via the top ~50 curated issuers (RLUSD, USDC exchange gateways, Xahau, Sologenic, Coreum): if they add the line, others follow.

**The bet.** If 20 of the top 100 issuers add the line in the first 30 days, self-declaration becomes the dominant signal by month 3 and our curator work shifts from "labeling everything" to "cross-checking self-declarations." That's the ecosystem move — publishing the standard is how a public-good dashboard makes the whole ledger legible, not just its own pages.

---

## SCALE PLAN — concrete curator-hour math

Baseline data (from this session's first-party queries):

- **10,663 IOU issuers** with any activity in `token_volume`.
- **302 MPTs**; 31 have machine-readable subclass; the rest inherit our derived `classification`.
- **Coverage-by-volume today (post-tonight's A+B):** 14.0% of last-2h live cohort pairs are labeled.
- **Coverage-by-count today:** ~0.5% of 10,663 issuers have a curated category.
- **Curator time per top-N decision:** 30-90 sec if the top-N are pre-queued with impact score + machine-assessment gray text (per Spec §3.2). Assume median 60 sec.

### Curator-hour → coverage-by-volume math

Trade volume follows a heavy-tailed distribution: RLUSD alone was 2.16M trades in the last 30d — 40% of the whole IOU volume. The top-50 cover ~85% of 30d volume. The top-200 cover ~95%. Everything beyond ~1000 is <1% cumulative.

Assuming 60-sec median per decision + curator's own read time on citations:

| curator-hour bucket | tokens curated | rank range | cumulative volume covered |
|---|---|---|---|
| **1 curator-hour** | 50-60 decisions | top-60 | ~85% |
| **5 curator-hours** | 250-300 | top-300 | ~96% |
| **20 curator-hours** | 1,000-1,200 | top-1,200 | ~99% |
| **100 curator-hours** | 5,000 | top-5,000 | ~99.7% |

**Order-of-work priority (by volume covered per minute):**
1. RLUSD + the top-10 by volume (already done tonight; single hour buys ~70% coverage-by-volume)
2. Top 50 by 30d volume (tonight got here; ~85% coverage-by-volume)
3. Top 100 by 30d volume (~90%)
4. Top 300 by 30d volume (~96%)
5. First 200 self-submissions accepted (assume they cluster among top-1000 by volume): +2-4% marginal, but ~200 tokens deep into the count-based coverage.
6. Dispute queue drain (irregular, low volume but high signal)
7. Long-tail sampler (5% random, never converges but keeps coverage honest)

### 1 week / 1 month / 3 months projections

Assumes one curator (Charlie) at 20 min/day = 2.3 hours/week. Multiplied by realistic self-submission uptake once the L2c form ships. MPT metadata is a one-time free lift (31 immediately usable subclasses).

**End of Week 1:**
- Registry live with L1 facts (100% mechanical) + L2a on-chain claims (MPT metadata + toml presence)
- Coverage-by-volume: **~85%** (curator top-50 already done)
- Coverage-by-count: **~1%** (100 of 10.6k)
- Attestation health: **~50 tokens** carry a citation URL
- L2c form: not yet shipped; self-submission = 0
- Dispute queue: 0

**End of Month 1:**
- L2c form shipped mid-month; first ~20 issuer self-submissions land + get promoted after curator confirmation
- Charlie's 20 min/day × 30 = 10 curator-hours over the month → curated top-300 (~96% coverage-by-volume)
- MPT subclass ingestion adds 31 verified categories
- Coverage-by-volume: **~96-97%**
- Coverage-by-count: **~3%** (300 tokens, plus 31 MPT, plus 20 self-submitted = ~350 of 10.6k)
- Attestation health: **~350 tokens**
- **Publish first daily signed snapshot** — coverage gauge goes public

**End of Month 3:**
- XLS draft submitted to XRPL Standards; 2-3 adopters commit
- Self-submission uptake accelerates: assume 5-10 per week → ~100 over the quarter, most from the top-1000 tail
- Charlie adds ~30 hours of curator time (assumes discipline holds) → curated top-1200 (~99% by volume)
- MPT population grows to ~500 (RWA momentum); 100+ subclass-declared
- Coverage-by-volume: **~99%**
- Coverage-by-count: **~13%** (1,200 curated + 500 MPT + 100 self-submitted = ~1,400 of 10.6k)
- Attestation health: **~1,400 tokens with citation URLs**
- Free downloadable dataset live; ~5-10 external tools reference it
- The XLS proposal is close to being adopted (or has been superseded by the working group's own version — either way, standard exists)

### What stays "unlabeled" forever (and why that's fine)

**The 87% of 10,663 issuers we NEVER curate.** These are tokens with:
- <100 lifetime trades AND no self-submission
- Issuer never set a Domain AND doesn't publish a toml
- Currency name is either random hex or a single-word meme that carries no external evidence

**Why leaving them `unlabeled` is honest, not a failure:**

1. **Layer 1 (facts) is 100% covered regardless.** Every one of these tokens has a mechanical row: decoded name, issuer age, trust-line count, AMM membership, impostor check, non-standard-code check. The reader who lands on `/token/<obscure>/<issuer>` gets a full Layer-1 fact sheet, an honest "unlabeled — no category on file" note, and the machine-assessment gray line if the heuristic has an opinion.
2. **The visual (verification-tier lanes) still places them correctly.** They pulse in the `bare` lane — which is a fact, not a judgment. Reader sees "these tokens haven't earned attestation" without us pretending we know what they are.
3. **The dispute path is open forever.** If a reader knows a specific obscure token is actually a real gaming coin, they submit a dispute with evidence and it gets curated. The registry scales in the direction of active reader interest, not in the direction of exhaustive coverage of dead memecoins.
4. **`unlabeled` != `unknown`.** We know what's `unlabeled`: it's a token whose issuer has published nothing and whose activity is below the threshold worth manually investigating. That state IS the signal.

**The registry's promise is not "we know every token" — it's "the facts are 100% covered, the top by volume is 99% curated, and every uncurated token carries an honest confession + a path to correction."** That's a defensible promise. "We categorized every one of 10,663 issuers" is not.

---

## Where I disagree with THIS spec (Charlie's Layer 6 + ecosystem move ask)

1. **The XLS proposal should be filed under XRPLF's name, not ours, once it's ready.** Publishing it as `xrpldashboard/v1` in the taxonomy field is fine, but the proposal document itself has higher adoption odds if it enters the XLS repo as an XRPLF working-group draft. We author it, we champion it, we hand it off. Precedent: the XRPL Foundation Token Self-Assessment questionnaire is XRPLF-branded, not vendor-branded. Vendor-branded standards die in the community — see the history of exchange-specific listing standards on other chains.

2. **Layer 6's dispute-authentication path should require Xaman signature-over-payload from day 1, not "optional."** Anonymous disputes are welcome for reader-side complaints, but a first-party dispute from the ISSUER account carries structurally different weight — and the mechanical way to prove "I am the issuer" is a Xaman sign-payload flow. Making it optional means we'll build a review triage for pseudonymous disputes first and never build the Xaman path. Ship the Xaman path in the initial dispute cycle.

3. **The impostor wording rule (§6.4a) should extend to the badge NAME, not just the description text.** Rename the internal category from `impostor` to `ticker_collision`. "Impostor" carries character-judgment vibes even in the source code where lawyers might discover it during discovery. `ticker_collision` is factual all the way down. This propagates back into the taxonomy vocabulary I proposed on 09-06 — worth the rename now before it ships publicly.

## What I'd add (net-new)

- **Provenance-DAG storage principle** (already in the 09-06 spec) should be restated here as the prerequisite for Layer 6 to work. Without immutable history, changelogs and disputes can't be audit-honest.
- **Curator-authority rotation policy** in §6.6 — Charlie's curator authority should have a nominated successor named in the taxonomy doc (not in code). If Charlie steps back, someone knows they're it. Bus factor > 1 becomes governance-level, not "we'll figure it out."
- **A signed monthly "state of the registry" report** — one-page markdown, published to `/registry/state`, signed with the receipt key. Contains the three coverage numbers, disputes-per-month, self-submission uptake, notable curation calls, taxonomy version. Wikimedia does this quarterly; it turns the registry from a service into an institution.

## Build order (extended, effort per layer)

Numbered continues from Spec 09-06:

| step | scope | effort | prerequisites |
|---|---|---|---|
| 1 | Repackage taxonomy + `other` → `unlabeled` + rename `impostor` → `ticker_collision` | 45 min editorial | none |
| 2 | Split `token_facts` / `issuer_facts` in Postgres | ~4h | 1 |
| 3 | Provenance envelope on every row + curation history table (multi-curator schema) | ~6h | 2 |
| 4 | `/admin/token-review` queue with impact-score priority + one-click actions | ~1d | 3 |
| 5 | Taxonomy v1 definitions doc + changelog + dispute path docs (Layer 6.1-6.3) | ~1d | none (can run in parallel) |
| 6 | Attorney review of Layer 6.4 wording + ToS + license (Charlie's counsel) | out-of-band | none |
| 7 | XLS-YYYd draft + submit to XRPL Standards repo | ~2h to write + review cycle | 5 |
| 8 | L2c form self-submission with two-way toml proof + Xaman signature + rate limits | ~3d | 3, 6, 15 |
| 9 | Signed daily snapshot with dedicated registry key | ~1d after key gen | 15 |
| 10 | Public coverage-by-volume gauge (headline number + three-metric detail page) | ~4h | 3 |
| 11 | Public downloadable dataset (`/dataset/registry/latest.zip`) | ~1d | 3 |
| 12 | Paid-tier freshness/throughput API + billing wiring | ~1-2 weeks | 8, 9 |
| 13 | Monthly "state of the registry" signed report (Layer 6 continuous) | ~2h per month, recurring | 9 |
| 14 | Curator-authority rotation policy document (Layer 6.6 add) | ~1h | none |
| 15 | Receipt keypair generation (Charlie's keyboard, per Sept-6 walkthrough) | out-of-band | none |

**Critical path to coverage-gauge-public:** steps 1 → 2 → 3 → 10 (single week if focused).
**Critical path to L2c self-submissions accepting:** 1 → 2 → 3 → 4 → 6 → 15 → 8 (~2 weeks assuming attorney turnaround).
**Critical path to XLS proposal filed:** 5 → 7 (~1.5 days).

Steps 5, 6, 7, 14 can run in parallel to the Postgres schema work. The XLS proposal (step 7) has zero code dependencies and could be filed within 48 hours if you want the ecosystem-move flag planted before the Sept-15 CLARITY window.

---

## Summary — what Charlie is deciding

Six choices define the shape of the next 90 days:

1. **XLS-YYYd — file this week (JJ writes it) OR sit on it?** Filing it plants an ecosystem-standard flag with roughly zero downside; sitting on it means we ship the same standard privately as `xrpldashboard/v1` and hope adopters follow anyway. My rec: file it, under XRPLF's name if they'll take it, under ours if not.
2. **`impostor` → `ticker_collision` rename now, or after attorney review?** Cheap now, expensive later. My rec: rename now.
3. **Receipt keypair walkthrough — this week or next?** Gate on Steps 8, 9, 12. My rec: schedule for Tuesday evening on Charlie's keyboard.
4. **CC-BY 4.0 license commitment.** Bakes in "public good" framing forever. My rec: commit.
5. **XRPL Foundation federation vs own token-assessment questionnaire.** My earlier rec: federate. Restating for the record.
6. **Successor curator nomination.** Bus factor >1 as a governance-level statement. My rec: name a successor even if the successor is "hold pattern maintained by JJ + escalate real editorial calls to a designated human within 48h until Charlie returns" — some named path always beats "hope it doesn't happen."
