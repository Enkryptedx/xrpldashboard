# XRPL Token Registry — Taxonomy v1

**Version:** 1.0.0 (draft, awaiting Charlie's approval before public)
**Published at:** `xrpldashboard.com/registry/taxonomy`
**Changelog:** [CHANGELOG.md](./CHANGELOG.md)
**Governance:** [../REGISTRY_GOVERNANCE.md](../REGISTRY_GOVERNANCE.md)

The taxonomy is the vocabulary the XRPL Token Registry uses to describe what class of thing a token is. It has **12 real categories + `unlabeled`** (13 total). Two mechanical flags — `ticker_collision` and `non_standard_code` — attach to any row regardless of category and never replace it.

Every entry below is exactly four lines: **definition** (what it means in one sentence, ≤280 chars for citation), **rule** (mechanical or evidence-based test), **evidence source** (which registry layer produces the signal), **boundaries** (adjacent categories a token could belong to instead, and why they were excluded).

---

## `stablecoin_regulated`
- **Definition:** Fiat-pegged token issued by a legally accountable entity with a canonical XRPL issuer address on the curated whitelist.
- **Rule:** Curator whitelist entry in `ticker_canonical_issuers.json` matching (currency, issuer) AND MPT `asset_subclass=stablecoin` OR toml-attested fiat-peg claim.
- **Evidence:** L2a (MPT metadata) or L3 (curator + citation URL).
- **Boundaries:** ≠ `stablecoin_gateway` (which is a named exchange's USD/EUR IOU without regulated-entity status). Impostor USDT/USDC/DAI DO NOT qualify — they land `unlabeled` with `ticker_collision=true`.

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
- **Boundaries:** Ticker impersonation (BTC from non-Axelar issuer) → `unlabeled` + `ticker_collision=true`. ≠ `stablecoin_gateway` (which is an XRPL-native custody claim, not a cross-chain wrap).

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
- **Rule:** Curator only. Never inferred from patterns (a legit early-stage utility can look identical to a memecoin on day one). May be curator-inferred when the issuer address, currency name, and lack of any Domain / toml jointly rule out other categories.
- **Evidence:** L3 (curator, citation `curator_inferred_from_activity` acceptable).
- **Boundaries:** ≠ `community` (which requires positive evidence of fan-token / social-tipping purpose). ≠ `gaming` (which requires a game). Impostor tickers land `unlabeled` + `ticker_collision`, not `memecoin`.

## `community`
- **Definition:** Fan token, tipping token, or "XRP community" branded token that claims a specific community purpose (donations, event access, group identity).
- **Rule:** Curator whitelist with positive evidence of community-purpose declaration (public community-page citation, Discord/Twitter community-lead attestation).
- **Evidence:** L3 (curator).
- **Boundaries:** ≠ `memecoin` (memes have no positive community-purpose claim). ≠ `gaming` (no game). Most tokens hand-inspected end up here get demoted to `unlabeled` — this category has a high evidence bar.

## `unlabeled`
- **Definition:** No category has been positively assigned. This IS the answer for a token, not the absence of one — the registry has looked and confirms it has nothing to say.
- **Rule:** Default when no other rule matches AND `facts_completeness ≥ 80%`. Below 80% completeness the row renders `pending`, not `unlabeled`.
- **Evidence:** Absence of positive evidence across all layers.
- **Boundaries:** All other categories. A row moves from `unlabeled` to a specific category only via curator action OR issuer self-submission through the L2c form (once shipped).

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
- **Editorial successor:** to-be-named human (Charlie to designate)

If Charlie is unreachable for >48 hours, JJ holds the queue in read-only status — no new curator-verified categorizations land, disputes are logged with "acknowledged, awaiting editorial review" status, and the successor is paged.

## Version-bump policy

- **PATCH** (1.0.0 → 1.0.1): typo, one-word wording tweak, or a boundary clarification that reclassifies no existing tokens.
- **MINOR** (1.0.0 → 1.1.0): new category added, or a boundary widened/narrowed such that some tokens legitimately move between adjacent categories (changelog documents counts).
- **MAJOR** (1.x → 2.0): category renamed, removed, or the taxonomy shape restructured. Requires a 30-day preview period during which the daily signed snapshot ships BOTH the v1 and v2 payloads.

## What this document IS NOT

- Not legal advice.
- Not a certification of any token's safety, value, or issuer's honesty.
- Not exhaustive of everything on the XRPL — the registry aims for 99%+ coverage-by-30d-volume, not 100%-of-issuers coverage. The `unlabeled` category is a first-class value.
- Not final. This is v1.0.0 draft. Feedback via [`/contact?purpose=attestation-dispute`](https://xrpldashboard.com/contact?purpose=attestation-dispute) or the coming XLS discussion draft.
