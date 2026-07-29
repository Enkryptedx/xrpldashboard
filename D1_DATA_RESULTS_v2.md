# D1 — Data Results v2 (JJ execution, punch-list closed)

Generated 2026-07-03 on the xrpldashboard prod dataset. Supersedes
`D1_DATA_RESULTS.md`. Closes the six-item punch list from Fable's
synthesis: two data fixes (contradictions), two missing datasets
(per-category trade counts + row-level TOML), two scoping notes
(walker fix + XRPScan permission path). No build; data-only.

Measure = **trade_count** (same as v1). Section 8 scopes the value-weighted
follow-up.

---

## 1. Fix — ARMY "contradiction" resolved

**Not a bug. Currency-code collision.**

There are two XRPL tokens using the 4-char code `ARMY` (hex
`41524D5900000000000000000000000000000000`), minted by different issuers:

| # | currency | issuer | trades 30d | source |
|---:|---|---|---:|---|
| 27 | ARMY | `rGG3wQ4kUzd7Jnmk1n5NWPZjjut62kCBfC` | 29,702 | XRPScan verified label + firstledger TOML |
| 30 | ARMY | `r319FqohpKLwjtcV2mosyC5sy125fDk4uH` | 28,149 | no domain, no TOML, no XRPScan label |

The (currency, issuer) pair is the unique key everywhere in our data —
same 4-char display label, different tokens. The floor calc treated them
independently and correctly: `rGG3wQ…` is nameable (excluded from floor),
`r319Fqoh…` is anonymous (included in floor).

The v1 display bug: both got rendered as just "ARMY" without their
issuer prefix. **Fix in the hero data payload:** whenever the same
currency code appears with multiple issuers in the top table, render
disambiguated (e.g. "ARMY (rGG3…)" and "ARMY (r319…)"). This is a
display-side change only; the underlying counts are correct.

## 2. Fix — 73 vs 75 domain count reconciled

**Both correct, counting different things.**

| basis | count | interpretation |
|---|---:|---|
| **97 unique issuers** in top-100 | **73** with Domain field | *how many wallets have a domain set* |
| **100 (currency, issuer) pairs** in top-100 | **75** with Domain field | *how many token-rows have a domain-backed issuer* |

Difference = 2 issuers each mint 2 currencies in the top-100:
`rKiCet8SdvWxPXnAgYarFUXMh1zCPz432Y` (both with Domain) and
`rfmS3zqrQrka8wVyhXifEeyTwe8AMz2Yhw` (both with Domain,
`axelar.foundation`). Adds 2 pair-rows over the unique-issuer count.

**Standard for v2 tables:** each metric now labels its basis
("per issuer" vs "per pair") in the column header. Hero bar sizing uses
per-pair (each token-row is one entry in the ranked table); per-issuer
is only relevant for the domain-attestation chain.

---

## 3. Missing data — per-category trade counts (labeled cohort)

Full breakdown across all 210 named tokens, ordered by trade share of total.

| category | named pairs | traded pairs 30d | trades 30d | % of TOTAL 30d | % of LABELED 30d | in current hero? |
|---|---:|---:|---:|---:|---:|---|
| **stablecoin** | 4 | 4 | 411,598 | 5.83% | 39.7% | H5 (kept) |
| **native_utility** | 15 | 7 | 364,677 | 5.16% | 35.2% | H5 (kept) |
| **memecoin** | 4 | 4 | 171,483 | 2.43% | 16.6% | H5 (kept) |
| no_category | 10 | 6 | 62,479 | 0.88% | 6.0% | — |
| **wrapped_major** | 3 | 3 | 13,139 | 0.19% | 1.3% | H5 (kept) |
| bridge | 1 | 1 | 11,030 | 0.16% | 1.1% | — (not in Fable's 8) |
| **fiat** | 1 | 1 | 1,187 | 0.02% | 0.1% | H5 (kept) |
| **lp_token** | 15 | 2 | 25 | 0.00% | 0.0% | +2 (Fable added) |
| **rwa** | 38 | 0 | 0 | 0.00% | 0.0% | +2 (Fable added) |

**Findings Fable will need:**

- **`rwa`: 38 named tokens, ZERO 30d trades.** RWA tokens on XRPL are
  currently non-trading (custody / issuance is on-chain, secondary market
  is off-chain / OTC). If Fable's hero shows an `rwa` bar sized by trade
  count, it will render at 0. Options: (a) size by issuance count / TVL
  not trades (data available in a different table); (b) drop `rwa` from
  the bar chart and represent as a separate honest "held but not traded"
  metric; (c) size all 8 bars by named-token *count*, not activity.
- **`lp_token`: 15 named, 2 traded, 25 trades.** Same problem in miniature.
  LP tokens are held not traded — no one Payment-transfers an LP claim.
- **`fiat`: 1 pair (EURØ) driving the whole bucket.** A single-issuer
  bar is a fragile display — the moment EURØ delists we render a zero.
- **`bridge` bucket (Axelar's XRPL gateway) exists in our category set
  but is NOT in Fable's 8-bar hero.** Axelar is 1.1% of labeled trades;
  we can either fold it into `wrapped_major` (semantic match: bridged
  asset from another chain) or add a 9th bar. Flagging so Fable decides.
- **`stablecoin` + `native_utility` + `memecoin` = 91.5% of labeled trades.**
  The remaining 5 categories share the last 8.5%.

## 4. Missing data — row-level TOML mapping for the top 100

Full JSON at `scratch/d1_v2_row_level.json`. Below is the tier breakdown
that Fable's "self-described vs verified" split needs. Each of the 100
pairs falls into exactly one tier under this priority order:
VERIFIED_TOML > XRPSCAN_VERIFIED > SELF_DESCRIBED_TOML > XRPSCAN_UNVERIFIED >
DOMAIN_ONLY > ANONYMOUS.

| tier | pairs | trades 30d | % of top-100 | % of TOTAL 30d | display treatment |
|---|---:|---:|---:|---:|---|
| **VERIFIED_TOML** (canonical `[[ACCOUNTS]]`) | 1 | 7,572 | 0.2% | 0.1% | ✓ Verified badge |
| **XRPSCAN_VERIFIED** (verified=true) | 14 | 208,325 | 4.2% | 2.9% | ✓ Verified badge + XRPScan attribution |
| **SELF_DESCRIBED_TOML** (non-canonical `[[ISSUERS]]`/`[[TOKENS]]`) | 49 | 1,705,672 | 34.3% | 24.1% | ⓘ Self-described |
| **XRPSCAN_UNVERIFIED** (labeled but not verified) | 9 | 1,529,457 | 30.8% | 21.7% | ⓘ Self-described + XRPScan attribution |
| **DOMAIN_ONLY** (Domain field set, TOML missing or silent) | 6 | 73,379 | 1.5% | 1.0% | ⓘ Wallet says its domain is X |
| **ANONYMOUS** (no domain / doesn't resolve / no TOML / no XRPScan) | 21 | 1,447,736 | 29.1% | 20.5% | — (bare currency code) |

### Sample rows for citations (per the "cite 2-3 samples" rule)

**VERIFIED_TOML sample** (canonical xrpl.org spec):
- `USDB` / `rB3y9EPnq1ZrZP3aXgfyfdXQThzdXMrLMc` / trades=7,572 / domain=`tokens.brazacripto.com.br` — the only issuer in the top-100 whose TOML uses the canonical `[[ACCOUNTS]]` section pointing back at the on-chain issuer.

**XRPSCAN_VERIFIED samples** (XRPScan `verified=true`):
- `BITx` / `rBitcoiNXev8VoVxV7pwoQx1sSfonVP9i3` / trades=898,749 (rank #1)
- `ASC` / `r3qWgpz2ry3BhcRJ8JE6rxM8esrfhuKp4R` / trades=236,802 — XRPScan label "Reaper" (mismatch between token code and issuer label; noted for future review)
- `XCORE` / `r3dVizzUAS3U29WKaaSALqkieytA2LCoRe` / (Coreum bridge)

**SELF_DESCRIBED_TOML samples** — non-canonical, custom-hosted (NOT firstledger):
- `ARK` / `rf5Jzzy6oAFBJjLhokha1v8pXVgYYjee3b` / trades=255,112 (rank #3) / domain=`ark.institute` — self-hosted TOML uses `[[ISSUERS]]`/`[[TOKENS]]` shape
- `MAG` / `rXmagwMmnFtVet3uL26Q2iwk287SRvVMJ` / trades=41,155 / domain=`xmagnetic.org` — same, self-hosted

**SELF_DESCRIBED_TOML samples** — firstledger auto-generated (50 of 49 total in this tier):
- `ZERPS` / `rJztCAZEQvKcMSQTpKUZRJJjA11aUTk9Aw` / trades=175,304 / domain=`eqpxobsslocteiamm7vl.toml.firstledger.net`
- `welth` / `rDSEfDcJ5UzQyK3UNmUDGvZT1Z7cmoiFv7` / trades=131,444 / domain=`qomgfkimwfxzkw3i1reg.toml.firstledger.net`
- `FIFA` / `rhonTjRzac7X1xPrMUWdVCFAeXKDsawabb` / trades=120,056 / domain=`ehoih1rghmcotqkqgk6c.toml.firstledger.net`

**ANONYMOUS samples** (no channel available):
- `C9BR` / `rBCf85rmfGBTAQrqyiNv5fxTvJ5A4EQXWg` / trades=517,237 (rank #2) — no Domain field on issuer
- `Xoge` / `rJMtvf5B3GbuFMrqybh5wYVXEH4QE8VyU1` / trades=208,810 — no Domain field
- `PLR` / `rNSYhWLhuHvmURwWbJPBKZMSPsyG5Qek17` / trades=135,229 — Domain set to `https://xrpillars.com/` which is a URL not a hostname (invalid, silently ignored)

### Cross-check on Fable's "biggest anonymous tokens" claim

Confirming: **20.5% of TOTAL 30d trades come from top-100 pairs in the
ANONYMOUS tier**. These are the ones that ship with no name, no domain,
no attestation from any source. The floor holds.

---

## 5. Scoping note — walker fix for `volume_xrp` (value-weighted floor)

**Root cause.** `xrpl_stream.py:token_event_handler` writes `volume_xrp`
as a hard-coded `0.0` placeholder (line 375). It was Phase 0 of
`TOKEN_ECONOMY.md` — count trades first, price them later. The token
amount (`Amount.value`) is not extracted or persisted; the row only
carries `(currency, issuer, hour_bucket, +1 trade)`.

**What needs to change (forward-fill first, cheap):**

1. In `token_event_handler`, extract token `Amount.value` (Payment) and
   `Amount.value` / `Asset2.value` (AMMDeposit / AMMWithdraw).
2. Look up the (currency, issuer) → XRP-per-token last-observed ratio.
   This already exists in `amm_ranked_pools` (the walker that populates
   `/pools`). Cache the ratios in-process, refresh every N minutes.
3. Multiply amount × ratio → `xrp_equivalent`. Pass as `volume_xrp_delta`
   to both `_volumes_conn.execute` (SQLite) and
   `pgbridge.upsert_token_volume` (Postgres).
4. If no AMM pool exists for the pair → write 0 honestly (already the
   default) and separately track "pairs with no XRP-price signal" so
   the /tokens page can footnote them.

**Effort:** ~1–2 hours coding + local test + 24h observation. Small
diff. Non-destructive (adds a column value, doesn't change schema —
column already exists). No downtime.

**Backfill scope.** Harder. Two options:

- **Cheap backfill (recommended if we ship this):** for each existing
  `token_volume` row, apply the *current* AMM ratio at read time and
  compute an estimate. Fast, but backdated trades get today's price
  (not the price at trade time). Acceptable for a 30d aggregate.
- **True backfill:** replay past ledger data to reconstruct amounts
  and pool ratios at each hour. Expensive — needs XRPL historical
  scan going back 30d for millions of transactions. Not worth it for
  a hero-bar number.

**Recommended sequence** (scope only — no build):

1. Ship forward-fill (patch `token_event_handler` + rebuild pool-ratio
   cache) — ~2 hours.
2. Deploy to prod. Walker starts populating `volume_xrp` on new trades
   from deploy time forward.
3. After 30 days of forward-fill data, re-run the honest-floor calc on
   value-weighted numbers.
4. Until step 3, ship /tokens with the count-based number + Fable's
   permanent "counts trades, not dollars" footnote. Value-weighted
   number replaces it silently once available.

**Charlie decision needed only if we want the cheap-backfill estimate
sooner** — that would let us ship the value-weighted floor immediately
using current prices applied to backdated trades. Truth-first cost:
prices drift, especially for memecoins that pumped-then-dumped in the
30d window. The estimate could be off by 2–3x for such rows.

**Recommendation:** ship count-based now, forward-fill in walker, revisit
value-weighted in ~30 days. Don't backfill.

---

## 6. Scoping note — XRPScan written-permission path

**Contact channel:** `support@xrpscan.com` (published in their API pricing
docs). Twitter: `@xrpscan`. No dedicated legal/partnerships email
surfaced; support inbox is the entry point.

**What to ask for (draft):** written permission for xrpldashboard.com
(free, public, non-monetized, XRPL-audience) to systematically ingest
their `accountName` labels from the public v1 API for the purpose of
labeling XRPL-issued tokens on our `/tokens` page, with visible
"Source: XRPScan" attribution on every labeled row and a link back
to the corresponding xrpscan.com account page.

**What XRPScan gains from saying yes:** a backlink from every labeled
row on our site, brand visibility on a free-tier XRPL analytics dest,
no engineering ask (public API is already free 10k req/day).

**What xrpldashboard gives up if they say no:** the 24.6% coverage that
XRPScan-verified + XRPScan-unverified would unlock (2.9% + 21.7%). The
floor stays at 20.5% strict-anonymous OR 24.6% higher if they say no
and we exclude XRPScan entirely.

**Rate limits:** free tier = 10k requests/day. For our top-N ingest
(currently 100, could grow to 1,000) that's plenty. If growth exceeds
that we upgrade — Pro is $ documented in the pricing page.

**Recommendation (surfacing to Charlie, not deciding):**

- Draft an email to `support@xrpscan.com` from `contact@xrpldashboard.com`
  requesting written non-commercial data-reuse permission with the
  attribution and backlink terms above.
- Include a link to xrpldashboard.com so they can see the audience
  and use-case.
- Ask for their preferred attribution string ("Source: XRPScan" is our
  proposal, but they may have a canonical wording).
- **Do not act until Charlie greenlights the outbound.**

If they decline or don't respond in 14 days: exclude XRPScan from
enrichment, ship with the 20.5% strict floor, no XRPScan citations
anywhere on the site.

---

## 7. Buildability confirmation — self-described vs verified display

**Confirmed buildable.** The distinction fits into the existing schema
and templates with no structural change.

- `token_names.json` already carries a `verified_via` field per entry.
  Add a companion `attestation_shape` field: `canonical` | `non_canonical` | `xrpscan` | `domain_only` | null.
- On `/tokens` template: a small badge next to each token name.
  - ✓ green pill "Verified" for `canonical` OR `xrpscan_verified=true`
  - ⓘ grey pill "Self-described" for `non_canonical` OR `xrpscan_unverified`
  - no badge for `domain_only` or unattested; token displays as bare
    currency code with a "(?)" hover
- Hero bars can render two-tone: solid segment = Verified pairs, hatched
  segment = Self-described pairs. This lets one bar honestly show both
  tiers at once.
- Backend surface: extend `token_data.build_ranked_tokens()` (already
  reads `token_names.json`) to attach the tier per row. ~15 LOC.

No new tables, no schema migration. **Buildable as designed.**

---

## 8. Corrected honest floor (v2 reading)

Same measure (trade_count), tier-based decomposition:

| Reading | Top-100 pairs failing all naming channels | Top-100 pair trades | Floor (top-100 only) | Floor (top-100 + assume tail all fail, upper bound) |
|---|---:|---:|---:|---:|
| **STRICT** (only VERIFIED_TOML or XRPSCAN_VERIFIED counts as nameable) | 85 | 4,756,244 | **67.3%** | **82.5%** |
| **PRAGMATIC** (also accept SELF_DESCRIBED_TOML + XRPSCAN_UNVERIFIED + DOMAIN_ONLY) | 21 | 1,447,736 | **20.5%** | **35.6%** |

**Change from v1:** the pragmatic floor came in at 20.5% (v2) vs 21.5%
(v1). Difference = the v2 tier calc treats `DOMAIN_ONLY` as "at least
some signal, exclude from anonymous" whereas v1's failing-all rule
required strict TOML attestation, marking DOMAIN_ONLY as failing. Six
pairs / ~1% of total flipped. Both are defensible; the tier-based
reading is the one Fable's design uses so v2's 20.5% is the number
that goes into the hero.

**Recommended hero label:** "at least **20.5%** — most XRPL activity is
permanently anonymous."

If Charlie picks the STRICT reading: "at least 67.3% — most XRPL
activity is self-described but not verified."

---

## 9. Summary table for Fable

| Question | v2 answer |
|---|---|
| ARMY contradiction | Two different tokens with the same 4-char code. Display bug in v1, floor calc was correct. |
| Domain count 73 vs 75 | Per-issuer (73) vs per-pair (75). Both correct, now labeled explicitly. |
| Category breakdown | Full table in §3. `rwa` and `lp_token` have ~0 trades — Fable's design needs to size them differently or drop. |
| Row-level TOML | Full JSON at `scratch/d1_v2_row_level.json`. Tier breakdown + sample citations in §4. |
| Walker value-fix scope | Forward-fill = ~2h. Backfill = don't. Ship count-based + footnote now. |
| XRPScan permission path | Email `support@xrpscan.com` from `contact@xrpldashboard.com`. Draft ready; awaiting Charlie's greenlight to send. |
| Self-described vs verified buildable | Yes. Existing schema supports it. ~15 LOC template change + one field addition. |
| Honest floor (pragmatic) | **20.5%** of TOTAL 30d trades (top-100 only, safe lower bound). |
| Honest floor (strict) | **67.3%** of TOTAL 30d trades if only verified attestation counts. |

## 10. Charlie's judgment call

**PRAGMATIC 20.5% vs STRICT 67.3%.**

- 20.5%: "at least 1 in 5 trades come from tokens no one has ever
  claimed" — but 24% of trades come from firstledger-auto-generated
  self-descriptions, which some visitors will find weaker than what
  they'd call "named."
- 67.3%: "at least 2 in 3 trades come from tokens without verifiable
  attribution" — a stricter, more honest-feeling number, but it
  demotes real self-descriptions (Coreum, Axelar, ARK, brazacripto)
  to the same tier as anonymous memecoins, which is arguably harsher
  than reality.

Both numbers are equally *defensible*. Charlie picks which honesty
axis the hero optimizes for: minimalist (report only ironclad
attestation gaps) or maximalist (report every wallet without cryptographic
attestation as unverifiable).

## 11. Files

- Top-100 raw: `scratch/d1_unlabeled_top100.json`
- Domain decode: `scratch/d1_domain_decode.json`
- TOML sweep: `scratch/d1_toml_sweep.json`
- Bithomp audit: `scratch/d1_bithomp.json`
- XRPScan aggregate: `scratch/d1_xrpscan.json`
- Category breakdown (§3): `scratch/d1_v2_categories.json`
- Row-level TOML (§4): `scratch/d1_v2_row_level.json`
- Tier breakdown (§4, §8): `scratch/d1_v2_tier_breakdown.json`
- XRPScan flags (per-issuer, no labels stored): `scratch/d1_v2_xrpscan_flags.json`

## 12. Not addressed (deliberate)

- **Bithomp**: still hard-blocked by ToS. Excluded from v2.
- **Backfill of volume_xrp for existing rows**: deliberately not scoped
  in detail — recommend against, ship forward-fill and footnote.
- **No hero build**: per Charlie's directive, v2 closes data gaps only.
  Fable rebuilds the spec on v2, Charlie makes the strict-vs-pragmatic
  call, then we build.
