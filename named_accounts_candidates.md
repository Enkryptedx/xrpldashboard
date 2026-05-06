# Named accounts — seed candidate list

Companion file to `KNOWN_ACCOUNTS.md`. This is the **wishlist** of
account categories worth seeding into `named_accounts.json` once we
have verifiable first-party sources for the specific addresses.

The verified file (`named_accounts.json`) stays empty until each entry
has a real `verified_via` URL. This file lists *what we want to seed
with*, so contributors (or future-us with web access) know where to
focus the curation effort.

Per `KNOWN_ACCOUNTS.md`: every entry that lands in
`named_accounts.json` must come with at least one first-party source
URL. No exceptions, even from this list.

---

## Tier 1 — Highest signal, easiest to verify

These are addresses publicly disclosed by the entity itself. Source
quality is gold; finding the address is the only work.

### Ripple operations & escrow
- ~~**Ripple Escrow wallets** (20 addresses)~~ — **SEEDED** from
  `https://ripple.com/.well-known/xrp-ledger.toml` (Ripple's
  first-party `xrp-ledger.toml` declaration, the XRPL standard
  mechanism for an entity to publish addresses on its own domain).
  All 20 monthly escrow wallets are in `named_accounts.json`.
- ~~**RLUSD Issuer**~~ — also seeded from the same toml. Issuer
  `rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De`, same source URL also linked
  from `token_names.json`.
- **Ripple Operations wallet(s)** — *not yet found in a first-party
  source.* Quarterly XRP Markets Reports give aggregate balances only,
  no addresses. Ripple's xrp-ledger.toml currently lists only escrow
  wallets and the RLUSD issuer.
- **Ripple Treasury** — same gap as above.

Sources checked, nothing useful found:
- ripple.com/insights/explanation-ripples-xrp-escrow (conceptual only)
- ripple.com/insights/ripple-escrows-55-billion-xrp (conceptual only)
- ripple.com/insights/q1-2025-xrp-markets-report (aggregates only)

Future re-check: Ripple may add new sections to its toml over time.
Periodically re-fetch `https://ripple.com/.well-known/xrp-ledger.toml`
and diff against the entries we already have.

### Major exchange hot wallets
Status: **harder than expected.** Almost no major exchange publishes
specific XRPL addresses through a first-party channel. Verified via
web search (May 2026):
- **Bitstamp** — does not publish proof-of-reserves with addresses;
  no `xrp-ledger.toml` at bitstamp.net (404).
- **Binance** — proof-of-reserves page describes the Merkle-tree /
  zk-SNARK methodology but does not list raw blockchain addresses.
- **Gatehub** — gatehub.net serves HTML on the toml path, not a real
  toml file.
- **Uphold** — no `xrp-ledger.toml` (404).

Acceptable sources if any of these change:
- Their published proof-of-reserves attestation including raw addresses
- Their official `xrp-ledger.toml` once published
- Treasury-move announcements on their corporate / verified social

Targets, by likely volume on XRPL:
- **Bitstamp** — long-standing XRPL gateway
- **Binance** — XRPL hot wallet(s)
- **Coinbase** — XRP custody addresses
- **Kraken** — hot wallet(s)
- **OKX** — hot wallet(s)
- **Bitfinex** — hot wallet(s)
- **Bybit** — hot wallet(s)
- **Upbit** / **Bithumb** — Korean exchanges, often heavy XRPL volume

### XRPL Foundation
- **XRPL Foundation** treasury / grants distribution wallet — likely
  disclosed on xrplf.org or their blog.

---

## Tier 2 — Medium signal, requires more careful sourcing

### ETF custodians (high-priority once XRP ETFs launch)
ETF custodian addresses are gold — disclosed in fund prospectuses and
regulatory filings. As of writing (May 2026), check current state.

Targets to watch:
- **BlackRock** XRP ETF custodian (if launched)
- **Fidelity** XRP ETF custodian
- **Bitwise** XRP ETF custodian
- **VanEck** XRP ETF custodian
- **Grayscale** XRPL trust addresses
- **21Shares** XRP ETP custodian

Sources: SEC S-1 filings, fund prospectuses, custodian disclosures.

### Token issuer wallets
The issuer addresses already in `token_names.json` (RLUSD, USDC,
SOLO, CORE, EQ, etc.) — these are issuers, not necessarily wallet
operators. Worth tagging in `named_accounts.json` separately if the
issuer also acts as a treasury wallet for the token.

### Validator operators (notable)
Validators identified by the entity that runs them:
- **Ripple validators** — disclosed
- **XRPL Foundation validators** — disclosed
- **University of Waterloo / MIT / Coil** validators — historically
  disclosed
- **Major exchange validators** (Bitrue, etc.)

Source: validator domains / .well-known/xrp-ledger.toml files (the
XRPL standard for validator disclosure).

---

## Tier 3 — Lower signal, only if a clean source exists

### Notable individuals / OG holders
Almost never first-party disclosed. Mostly third-party speculation.
Avoid unless the individual has explicitly named the address as theirs
on a verified social account.

### Protocol-owned wallets
- **Sologenic / SOLO treasury**
- **Coreum bridge** addresses
- **Equilibrium** treasury

These should have first-party sources on each protocol's documentation.

---

## How this list gets emptied

Each address that finds a verifiable source moves out of this file
and into `named_accounts.json` with its `verified_via` URL filled in.
The PR that adds it should also remove the corresponding bullet here
(or annotate it as "added: rXXX...").

Once a tier is fully covered, delete the section.

If a target turns out to have no first-party source available, leave
it here as a known gap — better visible than silently absent.
