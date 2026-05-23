# Token names — contribution guide

`token_names.json` maps `(currency, issuer)` pairs to human-readable
display names and categories. The dashboard uses it to render
ledger-level hex codes (e.g. `5852646F67650000...`) as something
humans can read (e.g. `XRdoge`).

This file is hand-curated and **community-contributed via GitHub PR**.
Same model as the named-accounts list (`KNOWN_ACCOUNTS.md`, see
`WHALE_WATCH.md`). No runtime dependency on Bithomp / XRPSCAN /
xpmarket / xrpl.to APIs. We own the curation; the trade-off is that
growth is slower but every entry has a human-reviewed PR trail.

## Schema

Each entry is keyed `currency_hex:issuer` and has these fields:

| Field              | Description                                              |
|--------------------|----------------------------------------------------------|
| `currency_hex`     | 40-char hex (or 3-char ASCII for legacy currencies)      |
| `currency_display` | Human-readable display name shown to users               |
| `issuer`           | Issuer account address (rXXX...)                         |
| `category`         | `stablecoin` / `fiat` / `wrapped_major` / `native_utility` / `memecoin` / `other` |
| `verified_via`     | URL to a first-party source. See policy below.           |
| `_note` (optional) | Curator note (e.g. "lowercase intentional")              |

## `verified_via` policy

We never publish a name we can't back to a first-party source. Acceptable
sources, in order of preference:

1. The issuer's own site, naming the address as theirs (`solo.com`,
   `gatehub.net`, `bitstamp.net` support article)
2. An official disclosure document (proof-of-reserves attestation,
   ETF custodian filing)
3. The issuer's verified social account (Twitter / X with verified
   business badge)
4. A reputable third-party publication that names the issuer
   (CoinDesk, etc.) — only as a last resort

Set `"verified_via": "TODO_curation_pass"` for entries that are
inherited from the legacy hardcoded list and still need a verifiable
source attached. These will get a verification pass before launch.

If a label is later disputed and we can't re-verify, **remove it**
pending re-verification. Brand survives "we removed a label" much
better than "we got it wrong."

## Verified source tiers

Each non-TODO entry has a `source` field naming the verification tier:

| `source`           | Trust origin                                                                |
|--------------------|------------------------------------------------------------------------------|
| `mpt_metadata`     | XLS-89 on-ledger MPT name/ticker fields. Protocol-deterministic.            |
| `toml`             | Issuer's `xrp-ledger.toml` `[[CURRENCIES]]` block; on-chain `Domain` field. |
| `domain_fallback`  | Issuer publishes a TOML with org name but no matching currency block.       |
| `lp_derived`       | LP-token currency derived from a verified AMM asset pair (see below).       |

### `lp_derived` — LP-token labels

LP-token currency codes are protocol-deterministic: rippled computes them
from the AMM's asset pair via SHA512Half. The derivation is the canonical
algorithm in `src/libxrpl/protocol/AMMCore.cpp::ammLPTCurrency`:

1. Canonical order: `minmax(Asset1, Asset2)` over the full Issue
   (currency+account compared byte-wise).
2. Hash: `SHA512Half(currency_lo || currency_hi)` — only the 20-byte
   currency parts are hashed. The issuer is the ordering key, not hash input.
3. LP currency = `0x03` || first 19 bytes of the hash.

> **Spec nuance**: The xrpl.org docs phrase the inputs as "the two assets'
> currency codes **and their issuers**." The rippled source is canonical;
> the issuer participates only in canonical ordering, not in the hash. Trust
> rippled, not the docs page.

We ship `lp_derived` labels only when **both underlying tokens are already
verified** (any non-TODO source above). Labeling an LP transitively endorses
both component tokens, so each side must pass the same gate. The
`derive_lp_currency` helper in `enrich_token_names.py` is available for
on-demand computation of unverified pairs without persistence.

Coverage expands automatically as more underlying tokens are curated: every
verified issuer cascades into the LP labels it appears in, no code change
required.

## How to contribute

1. Fork the repo.
2. Add or amend an entry in `token_names.json` following the schema.
3. Open a PR with:
   - The display name and issuer
   - At least one `verified_via` URL pointing to a first-party source
   - A short note in the PR description explaining why this token
     deserves to be named (it has measurable activity, it's a
     known stablecoin, etc.)
4. A maintainer reviews the source, validates the address against
   the source, and merges or requests changes.

## How to amend

Same process. Open a PR with the change and a fresh `verified_via`
that backs the new value. PRs that downgrade verification quality
(e.g. swapping a first-party source for a tweet) will be rejected.

## Why not just import Bithomp / XRPSCAN tags?

Two reasons:
- **Brand independence.** Importing a competitor's curation makes us
  look like a wrapper for their work, even with attribution. Slower
  to grow, much stronger as a standalone product.
- **Trust signal.** Every label in our index is backed by a public
  PR review. That's auditable in a way that "we imported their tag
  list at some point" is not.

This is the same pattern that mempool.space uses for its
mining-pool list — open repo, PR-driven, manually merged.
