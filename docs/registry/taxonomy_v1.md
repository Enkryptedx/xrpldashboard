# XRPL Token Registry — Taxonomy v1

**Version:** 1.0.0 (draft, awaiting Charlie's approval before public)
**Published at:** `xrpldashboard.com/registry/taxonomy`
**Changelog:** [CHANGELOG.md](./CHANGELOG.md)
**Governance:** [../REGISTRY_GOVERNANCE.md](../REGISTRY_GOVERNANCE.md)

The taxonomy is the vocabulary the XRPL Token Registry uses to describe what class of thing a token is. It has **12 real categories + 2 review-status values** (14 total). The two review-status values (`not_yet_reviewed` and `reviewed_unlabeled`) are what a token displays when it does not fall into any of the 12 real categories — they are distinct on purpose so the site never overclaims curatorial attention. Two mechanical flags — `ticker_collision` and `non_standard_code` — attach to any row regardless of category and never replace it.

Every entry below is exactly four lines: **definition** (what it means in one sentence, ≤280 chars for citation), **rule** (mechanical or evidence-based test), **evidence source** (which registry layer produces the signal), **boundaries** (adjacent categories a token could belong to instead, and why they were excluded).

---

## `stablecoin_regulated`
- **Definition:** Fiat-pegged token issued by a legally accountable entity with a canonical XRPL issuer address on the curated whitelist.
- **Rule:** Curator whitelist entry in `ticker_canonical_issuers.json` matching (currency, issuer) AND MPT `asset_subclass=stablecoin` OR toml-attested fiat-peg claim.
- **Evidence:** L2a (MPT metadata) or L3 (curator + citation URL).
- **Boundaries:** ≠ `stablecoin_gateway` (which is a named exchange's USD/EUR IOU without regulated-entity status). Impostor USDT/USDC/DAI DO NOT qualify — they land outside the 12 real categories (`not_yet_reviewed` by default, promotable to `reviewed_unlabeled` once a curator confirms) and carry `ticker_collision=true`.

## `stablecoin_gateway`
- **Definition:** USD/EUR IOU from a named XRPL gateway (Bitstamp, GateHub, historical exchanges); custody-backed but not regulated-entity-issued.
- **Rule:** Issuer Domain resolves to a known-gateway registry entry (curator-maintained) AND ticker is a 3-char fiat code.
- **Evidence:** L3 (curator gateway registry, TBD file).
- **Boundaries:** ≠ `stablecoin_regulated` (no regulator accountability). ≠ `wrapped_bridge` (not wrapping an off-chain token).

## `native_utility_chain`
- **Definition:** Utility token for an XRPL-family or XRPL-adjacent chain (Xahau, Coreum, CasinoCoin, Sologenic on-XRPL).
- **Rule:** Curator whitelist keyed on (ticker, issuer) with citation URL matching an established sibling chain's foundation domain.
- **Evidence:** L3 (curator).
- **Boundaries:** ≠ `dex_utility` (which is utility for a DEX front-end, not a chain). ≠ `wrapped_bridge` (native, not wrapped).

## `dex_utility`
- **Definition:** Utility token for an XRPL-native DEX or AMM front-end (Magnetic, Sologenic marketplace, OpulenceX, XPMarket, StayKX).
- **Rule:** Curator whitelist + issuer Domain matches a known XRPL DEX front-end AND that front-end's own token registry references this token as its official utility.
- **Evidence:** L3 (curator with DEX-registry citation).
- **Boundaries:** ≠ `defi_lending` / `defi_yield` (which are protocol-specific). ≠ `gaming` (which is game-currency, not trading-app utility).

## `defi_lending`
- **Definition:** Governance or utility token for an XRPL lending protocol.
- **Rule:** DefiLlama-listed as an XRPL lending protocol OR curator citation to protocol whitepaper; MPT `asset_subclass=credit`/`private_credit` may apply.
- **Evidence:** L3 (curator) or L2a (MPT).
- **Boundaries:** ≠ `defi_yield` (yield-farming vault vs credit/lending). ≠ `rwa` (RWA carries a specific off-chain asset; lending token is protocol-native).

## `defi_yield`
- **Definition:** Yield-farming or vault-share token that represents a claim on protocol-generated yield.
- **Rule:** MPT `asset_subclass=yield` OR curator citation to protocol vault documentation.
- **Evidence:** L2a (MPT) or L3 (curator).
- **Boundaries:** ≠ `lp_token` (auto-issued AMM share, not a manual vault). ≠ `defi_lending` (yield ≠ credit).

## `gaming`
- **Definition:** In-game currency or premium item for a game whose economy is anchored on the XRPL (Zerpmon, XRPillars, Ark Institute).
- **Rule:** Curator whitelist + issuer Domain resolves to a game front-end with public gameplay evidence.
- **Evidence:** L3 (curator with gameplay-page citation).
- **Boundaries:** ≠ `memecoin` (which has no protocol claim; gaming has a game). ≠ `community` (which is fan-token/social/loyalty, not in-game utility).

## `wrapped_bridge`
- **Definition:** Represents an off-chain asset via a named cross-chain bridge account (Axelar XRPL bridge, other future named bridges).
- **Rule:** Issuer address is in the curator-maintained bridge-account whitelist AND currency decodes to a canonical external ticker OR MPT metadata declares wrapped status.
- **Evidence:** L3 (curator bridge whitelist).
- **Boundaries:** Ticker impersonation (BTC from non-Axelar issuer) → outside the 12 real categories (`not_yet_reviewed` by default, `reviewed_unlabeled` after curator review) + `ticker_collision=true`. ≠ `stablecoin_gateway` (which is an XRPL-native custody claim, not a cross-chain wrap).

## `rwa`
- **Definition:** Tokenized real-world asset. Sub-tags may apply: `treasury`, `commodity`, `credit`, `private_credit`, `equity`, `bond`, `real_estate`, `collectible`.
- **Rule:** MPT `asset_subclass` populated with a real-world-asset value (per XLS-89) OR curator citation to an off-chain proof-of-reserves / trust deed / prospectus.
- **Evidence:** L2a (MPT metadata) — 31 of 302 mainnet MPTs populate this today. L3 (curator) for the IOU-side.
- **Boundaries:** ≠ `stablecoin_regulated` (which is fiat-pegged specifically). ≠ `wrapped_bridge` (which is a chain-crossing representation, not a real-world claim).

## `lp_token`
- **Definition:** Automatic-market-maker pool share, issued by the AMM object itself and representing proportional ownership of that pool's reserves.
- **Rule:** Fully mechanical — issuer address IS an AMM account (present in `amm_ranked_pools`) AND currency code has the `0x03` LP-token high-nibble prefix per XLS-30.
- **Evidence:** L1 (mechanical, always derivable).
- **Boundaries:** None — LP tokens are structurally distinct from all other classes. Always renders in a filter-out-able bucket.

## `memecoin`
- **Definition:** Purely social or speculative token with no protocol claim, no wrap, no game, no fiat backing; typically issued for community-branding or joke reasons.
- **Rule:** Machines never infer `memecoin`. A human curator may assign it, and the row carries `tier=curator-inferred` with the curator's reasoning in the citation. That is the whole rule — there is no mechanical fallback and no pattern-match short-circuit, because an early-stage legitimate utility token on day one can be visually indistinguishable from a memecoin.
- **Evidence:** L3 (curator, `tier=curator-inferred`, reasoning in citation).
- **Boundaries:** ≠ `community` (which requires positive evidence of fan-token / social-tipping purpose). ≠ `gaming` (which requires a game). Impostor tickers land `not_yet_reviewed` or `reviewed_unlabeled` + `ticker_collision`, never `memecoin`.

## `community`
- **Definition:** Fan token, tipping token, or "XRP community" branded token that claims a specific community purpose (donations, event access, group identity).
- **Rule:** Curator whitelist with positive evidence of community-purpose declaration (public community-page citation, Discord/Twitter community-lead attestation).
- **Evidence:** L3 (curator).
- **Boundaries:** ≠ `memecoin` (memes have no positive community-purpose claim). ≠ `gaming` (no game). Most tokens hand-inspected end up here get demoted to `reviewed_unlabeled` — this category has a high evidence bar.

## `not_yet_reviewed`
- **Definition:** No curator has ever inspected this token. It has no toml claim, no MPT metadata, no bridge-whitelist entry, and no curator decision. This is the honest default state for every new row — the registry has not yet formed a view on this token.
- **Rule:** Default whenever `token_category_history` has zero curator-source rows for `(currency_hex, issuer)` AND no toml/MPT/mechanical signal applies. Continues to render this way until either the curator makes a decision (moving the row to a real category OR to `reviewed_unlabeled`) OR the issuer self-submits through the L2c form (once shipped).
- **Evidence:** Absence of any curator or automated signal. The row exists in `token_facts` (we know it exists on the ledger) but no downstream statement has been made about what it is.
- **Boundaries:** Distinct from `reviewed_unlabeled` — a curator has NOT looked. Distinct from every real category — no positive evidence supports any of them. Distinct from `pending` (a below-facts-completeness state; `not_yet_reviewed` requires `facts_completeness ≥ 80%`).
- **Coverage-gauge:** Counted separately. A rising `not_yet_reviewed` count is not editorial failure — it reflects registry surface area growing faster than curator attention. The gap between `not_yet_reviewed` and total tokens is the curator queue.

## `reviewed_unlabeled`
- **Definition:** A human curator has inspected this token and confirmed that none of the 12 real categories applies. The registry has looked and has nothing to say about the class of thing this is.
- **Rule:** `token_category_history` has at least one curator-source row for `(currency_hex, issuer)` where `category='unlabeled'` AND `superseded_by IS NULL`. The row also carries the reviewing curator's id and a citation URL explaining what evidence was considered and why no category fit.
- **Evidence:** L3 (curator explicit decision with reasoning).
- **Boundaries:** Distinct from `not_yet_reviewed` — a curator HAS looked, and the empty answer is the answer. A row moves from `reviewed_unlabeled` to a real category only when new evidence arrives (a toml claim published, MPT metadata added, or a curator revisits with new information); the taxonomy version is captured on every history row so re-review under a later vocabulary is auditable.
- **Coverage-gauge:** Counted separately. Every entry represents editorial attention that was spent producing no category — that attention itself is the value.

---

## Mechanical flags (orthogonal to category)

Both flags may attach to a row of any category. They render as prominent badges regardless of category.

### `ticker_collision`
- **Definition:** The token's decoded name matches a well-known off-chain ticker (USDT, USDC, BTC, ETH, RLUSD, LTC, …) but its issuer address is NOT in that ticker's canonical whitelist. This is a statement about issuer provenance, not about the token's intent or the issuer's character.
- **Rule:** Fully mechanical — `token_naming.py` decodes currency to ASCII, cross-references against `ticker_canonical_issuers.json`, sets flag if match + non-canonical.
- **Evidence:** L1 (mechanical).
- **Public wording:** "Ticker collision: this token's currency code decodes to a well-known off-chain ticker but its issuer is not on our list of canonical issuers for that ticker." Never "impostor," "scam," "fake," or a character judgment.

### `non_standard_code`
- **Definition:** The token's 40-character currency code does not decode to a printable ASCII name; the on-chain bytes are non-printable or unpaddable.
- **Rule:** Fully mechanical — `token_naming.decode_currency` returns `kind="junk"`.
- **Evidence:** L1 (mechanical).
- **Public wording:** "Non-standard currency code: the on-chain bytes for this token do not decode to a printable name. The raw hex is shown for reference; there is no reader-friendly ticker to compare against."

---

## Curator authority + successor path

The registry is currently maintained by:
- **Primary curator:** Charlie Bruce
- **Deputy / queue-holder:** JJ (automated queue triage; anything editorial escalated within 48 hours)
- **Editorial successor:** to-be-named human (Charlie designates when named)

### Interim hold-pattern (in effect until a human successor is named)

The successor role has not yet been assigned. Until Charlie names a human editorial successor, the following hold-pattern is in force:

- If Charlie is unreachable for **>48 hours**, JJ holds the queue in **read-only** status. No new curator-verified categorizations land during the hold.
- Every incoming dispute or self-submission during the hold is logged with status `acknowledged, awaiting editorial review` and a timestamp.
- JJ escalates the read-only state to Charlie's known contact channels at the 48-hour mark; if no reply within a further 48 hours, JJ produces a public status page entry disclosing that the registry is in read-only mode with a specific reason ("primary curator temporarily unavailable — no editorial writes are landing pending review").
- Mechanical (L1) rules — LP-token detection, ticker_collision, non_standard_code — continue to compute and render normally during the hold. Those don't require human judgment.
- Rate limits, submission validation, and all other machine-only paths continue to run during the hold. Only the *editorial write path* freezes.
- Once Charlie is reachable again (or a named successor is designated), the hold lifts and the queue's frozen entries are processed in the order they arrived.

This interim pattern is written up here so the registry's failure-mode is public and predictable, not opaque. When a successor is named, this section is rewritten to name them explicitly.

## Version-bump policy

- **PATCH** (1.0.0 → 1.0.1): typo, one-word wording tweak, or a boundary clarification that reclassifies no existing tokens.
- **MINOR** (1.0.0 → 1.1.0): new category added, or a boundary widened/narrowed such that some tokens legitimately move between adjacent categories (changelog documents counts).
- **MAJOR** (1.x → 2.0): category renamed, removed, or the taxonomy shape restructured. Requires a 30-day preview period during which the daily signed snapshot ships BOTH the v1 and v2 payloads.

## What this document IS NOT

- Not legal advice.
- Not a certification of any token's safety, value, or issuer's honesty.
- Not exhaustive of everything on the XRPL — the registry aims for 99%+ coverage-by-30d-volume, not 100%-of-issuers coverage. Both `not_yet_reviewed` and `reviewed_unlabeled` are first-class values — one honestly declares that no curator has looked, the other honestly declares that a curator looked and no category fit.
- Not final. This is v1.0.0 draft. Feedback via [`/contact?purpose=attestation-dispute`](https://xrpldashboard.com/contact?purpose=attestation-dispute) or the coming XLS discussion draft.
