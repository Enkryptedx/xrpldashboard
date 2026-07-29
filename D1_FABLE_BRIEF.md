# D1 — Unnamed-Transactions Investigation
### Paste-ready brief for a fresh Fable 5 chat

---

## How this works

A fresh Fable chat can *reason and design* but it can't query our Postgres, hit XRPL nodes, scan issuer domains, or pull Bithomp / XRPScan APIs. The realistic split:

- **JJ executes the data collection** (Neon queries, xrpl-py `AccountRoot.Domain` decode, TOML fetches, Bithomp + XRPScan cross-ref, 30-day volume math). Writes results to `~/xrpl_test/D1_DATA_RESULTS.md`.
- **Charlie pastes `D1_DATA_RESULTS.md` into the Fable chat below this brief.**
- **Fable synthesizes**: honest-floor number, methodology validation, hero spec design, tooltip copy, category structure, edge-case reasoning.

**JJ deliverable → Charlie → Fable**. Don't skip the middle step; Fable can't see our DB.

---

## Copy-paste starts below this line
## ──────────────────────────────────────────────────────────

You are being asked to complete a design + synthesis pass on a live analytics site. Read this brief in full before answering. Do not begin work until you have digested the constraints section at the end.

### Context (self-contained)

**xrpldashboard.com** is an XRP Ledger analytics site for a general (non-developer) audience. Positioning: comprehensive-but-understandable, avoid raw blockchain jargon. Standing rule: **truth-first** — every claim on the site must be accurate, real-time where possible, never overstated.

The page in question is **`/tokens`**. It ranks every XRPL-issued token by trade count over the selected window (24h / 7d / all-time). Data source: our `token_volume` table (hourly buckets of Payments carrying a token amount + AMMDeposit / AMMWithdraw). One row per `(currency, issuer)` pair.

The top of the page renders a **hero of 5 category bars**: `stablecoin`, `fiat`, `wrapped_major`, `native_utility`, `memecoin`. Below the hero is a ranked table of the top 50 tokens by trade count.

Category is looked up in a curated JSON file, `token_names.json`, containing **210 entries** with a `(currency, issuer)` key. If a row's `(currency, issuer)` isn't in the file, it is labeled `Unlabeled` — no category, display falls back to decoded ASCII of the currency code, or truncated hex if the code isn't ASCII.

**The problem the visitor sees**: most `/tokens` activity is `Unlabeled`. This reads as a legibility failure — as if the site can't tell them what most XRPL traffic is.

**What we already know**:
- Only 27 of our 210 named tokens land in the 5 rendered hero bars. The other 183 sit in `other` (119), `rwa` (38), `native_utility` (15), `lp_token` (15), no-category (10), and other buckets that aren't rendered.
- XRPL's design allows anyone to mint a token with no metadata. Most trade volume genuinely belongs to unlabeled memecoins from unknown issuer wallets. This is real, not a data gap on our side.
- We have not systematically ingested Bithomp / XRPScan public labels or scanned issuer AccountRoot.Domain fields.

### Three deliverables

**(1) Enrichment audit — synthesis**

JJ (the executing agent) will supply raw data pulls attached to this prompt: a table of top-100 unlabeled issuers by 30d trade volume, their AccountRoot.Domain values (decoded from hex where present), TOML-fetch results at those domains, and Bithomp + XRPScan label cross-refs.

Your job: read those results, synthesize how many more tokens we can *honestly* name and from where. Produce a table:

| Source | Newly-nameable count | % of unlabeled volume covered | Notes / gotchas |
|---|---|---|---|
| AccountRoot.Domain (no TOML) | ... | ... | domain set but no attestation chain — is this enough? |
| xrp-ledger.toml (closed chain) | ... | ... | cryptographic attestation |
| Bithomp public labels | ... | ... | licensing/re-export question |
| XRPScan public labels | ... | ... | same |

Flag anything ambiguous. If Bithomp's terms prohibit re-export of labels, say so.

**(2) Honest floor**

From the same supplied data: what percentage of 30-day `/tokens` trade volume is attributable to tokens meeting ALL of:
- Not in `token_names.json`
- Issuer's AccountRoot.Domain is empty OR the value is not a resolving hostname
- No xrp-ledger.toml at any related domain
- Not labeled by Bithomp
- Not labeled by XRPScan

This is the **permanently-unnameable share**. Report it as a plain percentage. This number becomes the label on the hero's honest bar.

If the JJ-supplied data has methodology gaps that would inflate or deflate this number, flag them.

**(3) Redesigned hero spec**

Design the new hero. Constraints:
- Retain the 5 existing labeled category bars (`stablecoin`, `fiat`, `wrapped_major`, `native_utility`, `memecoin`)
- Add 2 more bars for tokens we already have named but currently hide: `rwa` and `lp_token`
- Add one honest bar: **"Unlabeled — most XRPL activity"** with the honest-floor % from (2) and a plain-English tooltip explaining WHY (XRPL's minting is permissionless; no domain, no attestation, no exchange listing = permanently unnameable at scale)
- Optionally show the enrichment ceiling: after ingesting the sources from (1), how many additional bars *could* light up? Grey-out target vs. current-lit.

Deliverable format for (3):
- A layout description (ordering, sizing rules, sort criteria)
- The exact tooltip copy for the honest bar (~2 sentences, plain English, no jargon)
- Any interaction notes (hover behavior, click-through to the token list, etc.)
- One paragraph on why this design tells the truth better than the current 5-bar hero

### Verbatim constraints — do not violate

1. **Never invent a label.** If a token can't be attributed via a source you can cite from the supplied data, it stays Unlabeled.
2. **Every "we can name X% more" claim must include the source and 2–3 sample rows** from the supplied data. No unsourced synthesis.
3. **If Bithomp's public labels are legally restricted from re-export**, note the licensing question in the enrichment table — don't paper over it.
4. **The honest-floor number from (2) is the priority.** If it's 60%, we tell visitors it's 60%. If it's 90%, we tell them 90%. The number is not negotiable to look better.
5. **No build.** Investigation + synthesis + design spec only. Charlie gates the build separately.
6. **Site audience is general/non-developer.** No raw XRPL jargon in visitor-facing copy (no "AccountRoot", "SLE", "amendment", etc.). "Wallet with no attached domain" not "AccountRoot with empty Domain field."

### If the supplied data is missing something

If JJ's data pull doesn't cover something you need for the honest-floor calculation, say so and describe the missing pull. Do not estimate around missing data.

### Output format

Single markdown response with three sections matching (1), (2), (3). Bracket the honest-floor % in bold. End with a one-paragraph "what I'd want next" note if there are follow-up investigations.

## ──────────────────────────────────────────────────────────
## Copy-paste ends above this line

---

## What JJ runs before this brief goes to Fable

Written to `~/xrpl_test/D1_DATA_RESULTS.md`:

1. **Top-100 unlabeled issuers by 30d trade volume** — SQL against `token_volume` filtered against `token_names.json` misses. Column: `currency, issuer, trades_30d, volume_xrp_30d, share_of_unlabeled_volume, share_of_total_volume`.

2. **AccountRoot.Domain decode** for each of those 100 issuers — xrpl-py `account_info` + `binarycodec` decode of the hex-encoded Domain field. Column: `issuer, domain_hex, domain_ascii, resolves_yes_no`. (Codified pattern: `feedback_manifest_domain_binarycodec`.)

3. **TOML sweep** on every resolving domain — HTTP GET `https://{domain}/.well-known/xrp-ledger.toml`, parse `[[ACCOUNTS]]` and `[[CURRENCIES]]` (canonical, not `[[TOKENS]]` — codified in `project_xrpldashboard_verify_toml_currencies_gap`). Column: `issuer, toml_found, toml_declares_this_issuer, attestation_chain_closed`.

4. **Bithomp public label cross-ref** — for the same 100 issuers, check bithomp.com/api/v2/address/{issuer} (or scrape the label field). Column: `issuer, bithomp_label`. Flag if terms prohibit re-export.

5. **XRPScan public label cross-ref** — same, xrpscan.com/api/v1/account/{issuer}. Column: `issuer, xrpscan_label`.

6. **Volume math for honest floor** — sum 30d trade volume where ALL five criteria fail. Divide by total 30d /tokens page trade volume. Report %.

Each pull writes a markdown table + a summary sentence. When done, Charlie pastes `D1_DATA_RESULTS.md` into the Fable chat *below* the brief above.

## Order of operations

1. JJ (this session or a follow-up) runs the six data pulls → `D1_DATA_RESULTS.md`
2. Charlie opens a fresh Fable 5 chat
3. Charlie pastes the section between the copy-paste markers above, then pastes `D1_DATA_RESULTS.md`, then hits send
4. Fable returns the three-section synthesis
5. Charlie gates the hero-build decision from Fable's output
