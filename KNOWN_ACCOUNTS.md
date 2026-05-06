# Known accounts — contribution guide

`named_accounts.json` maps XRPL account addresses (`rXXX...`) to human
labels and categories. The whale-watch panel uses it to render
"rNxp4h8apvRis6mJf9Sh8C6iRxfrDWN7AV" as "Bitstamp hot wallet."

This file is hand-curated and **community-contributed via GitHub PR**.
Same model as `token_names.json` (see `TOKEN_NAMES.md`). No runtime
dependency on Bithomp / XRPSCAN tag APIs. We own the curation; the
trade-off is slower growth but every entry has a human-reviewed PR
trail.

## Schema

Each entry is keyed by the account address and has these fields:

| Field           | Description                                                    |
|-----------------|----------------------------------------------------------------|
| `name`          | Display label (e.g. "Bitstamp hot wallet")                     |
| `category`      | `exchange` / `ripple` / `etf_custody` / `treasury` / `validator` / `whale_individual` / `other` |
| `verified_via`  | URL to a first-party source. See policy below.                 |
| `_note` (optional) | Curator note (purpose of the wallet, when it was active, etc.) |

## `verified_via` policy

We never publish a name we can't back to a first-party source. Acceptable
sources, in order of preference:

1. The owning entity's own site or support article naming the address
   (Ripple blog post, Bitstamp's published deposit-address list,
   exchange proof-of-reserves attestation)
2. An official disclosure document (ETF custodian filing, regulatory
   filing, audit report)
3. The entity's verified social account (Twitter / X with verified
   business badge) explicitly identifying the address
4. Reputable third-party publication that names the address — only as
   a last resort, and only if no first-party source is available

If a label is later disputed and we can't re-verify, **remove it**
pending re-verification. Brand survives "we removed a label" much
better than "we got it wrong."

## How to contribute

1. Fork the repo.
2. Add an entry to `named_accounts.json` following the schema.
3. Open a PR with:
   - The address and the proposed label
   - At least one `verified_via` URL pointing to a first-party source
   - A short note in the PR description explaining what this account
     does and why it deserves to be tracked
4. A maintainer reviews the source, validates the address against the
   source, and merges or requests changes.

## How to amend

Same process. Open a PR with the change and a fresh `verified_via`
that backs the new value. PRs that downgrade verification quality
(e.g. swapping a first-party source for a tweet) will be rejected.

## What about ETF custodians?

ETF custodians (BlackRock, Fidelity, Bitwise, etc.) are particularly
high-value to track because their on-chain flows are publicly
attestable. Whenever XRP ETFs launch, custodian addresses are
disclosed in fund prospectuses or regulatory filings — those are
gold-standard `verified_via` sources.

## What about exchange wallets?

Exchanges typically rotate hot wallets and don't always publish
addresses. Acceptable sources:
- Their published proof-of-reserves attestation (most exchanges now
  publish these quarterly)
- Their support docs / API docs that include sample addresses
- Their official announcements about treasury moves

If you're not sure whether a source qualifies, open the PR anyway and
flag it for discussion.

## Why not just import Bithomp / XRPSCAN tags?

Two reasons:
- **Brand independence.** Importing a competitor's curation makes us
  look like a wrapper for their work, even with attribution. Slower
  to grow, much stronger as a standalone product.
- **Trust signal.** Every label in our index is backed by a public
  PR review. That's auditable in a way that "we imported their tag
  list at some point" is not.

Same pattern as mempool.space's mining-pool list — open repo, PR-driven,
manually merged.
