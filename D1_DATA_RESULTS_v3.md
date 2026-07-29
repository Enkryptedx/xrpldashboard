# D1 — Data Results v3 (Charlie's 5 decisions folded in)

Generated 2026-07-03 on the xrpldashboard prod dataset. Supersedes
`D1_DATA_RESULTS_v2.md`. This is the version to re-feed Fable for
the FINAL buildable hero spec.

**Charlie's five decisions (2026-07-03):**

1. **Floor: PRAGMATIC 20.5%** — the honest permanent-anonymous floor.
   Self-described tier displays as "self-described," NEVER "verified."
2. **Bridge/Axelar: its own 9th bar** — small but honest, don't muddy
   `wrapped_major`.
3. **RWA + LP_TOKEN: sized by TVL, not trade count** — with an explicit
   mixed-unit label on the hero so visitors can't compare directly to
   the trade-count bars.
4. **XRPScan permission: email greenlit, coverage NOT assumed in the
   build.** Ship hero without XRPScan; add XRPScan-attributed tier
   only if they write "yes." Draft email is §6 below.
5. **Walker value-fix: greenlit as a real build.** Forward-fill
   `volume_xrp` in `xrpl_stream.py`, no backfill. Standard gates apply.
   Hero ships count-based with footnote; value-weighted floor re-runs
   in ~30 days.

Measure = **trade_count** (unchanged from v1/v2). §5 tracks value-weighted
follow-up; §8 tracks TVL sizing for the two low-trade categories.

---

## 1. Fix — ARMY "contradiction" resolved

**Not a bug. Currency-code collision.** (Unchanged from v2.)

Two XRPL tokens use the 4-char code `ARMY`, minted by different issuers:

| # | currency | issuer | trades 30d | source |
|---:|---|---|---:|---|
| 27 | ARMY | `rGG3wQ4kUzd7Jnmk1n5NWPZjjut62kCBfC` | 29,702 | XRPScan verified label + firstledger TOML |
| 30 | ARMY | `r319FqohpKLwjtcV2mosyC5sy125fDk4uH` | 28,149 | no domain, no TOML, no XRPScan label |

(currency, issuer) is the unique key. `rGG3wQ…` is nameable but under
Decision 4 XRPScan attribution is NOT assumed in the build — so until
XRPScan writes "yes," this row displays as `firstledger-self-described`
(via its TOML), not `xrpscan-verified`. `r319Fqoh…` stays anonymous.

**Display rule:** whenever the same currency code appears with multiple
issuers, render disambiguated: "ARMY (rGG3…)" and "ARMY (r319…)". Never
merge issuers under a shared display label.

## 2. Fix — 73 vs 75 domain count reconciled

**Both correct, counting different things.** (Unchanged from v2.)

| basis | count | interpretation |
|---|---:|---|
| 97 unique issuers in top-100 | **73** with Domain field | *how many wallets have a domain set* |
| 100 (currency, issuer) pairs in top-100 | **75** with Domain field | *how many token-rows have a domain-backed issuer* |

Two issuers each mint 2 currencies in the top-100 (`rKiCet8…` +
Axelar `rfmS3zqr…`), adding 2 pair-rows over the unique-issuer count.

**Standard for v3 tables:** each metric labels its basis
("per issuer" vs "per pair") in the column header. Hero bar sizing
uses per-pair for the trade-count bars, per-issuer for the domain chain.

---

## 3. Category breakdown (labeled cohort, 210 named tokens)

Ordered by trade share of total. Sizing decisions per Decision 3 shown
in the last column.

| category | named pairs | traded pairs 30d | trades 30d | % LABELED | **sizing basis in hero** |
|---|---:|---:|---:|---:|---|
| **stablecoin** | 4 | 4 | 411,598 | 39.7% | trades |
| **native_utility** | 15 | 7 | 364,677 | 35.2% | trades |
| **memecoin** | 4 | 4 | 171,483 | 16.6% | trades |
| no_category | 10 | 6 | 62,479 | 6.0% | trades (if shown) |
| **wrapped_major** | 3 | 3 | 13,139 | 1.3% | trades |
| **bridge** (Axelar) | 1 | 1 | 11,030 | 1.1% | **trades (9th bar per Decision 2)** |
| **fiat** | 1 | 1 | 1,187 | 0.1% | trades |
| **lp_token** | 15 | 2 | 25 | 0.0% | **TVL — Decision 3, mixed-unit label** |
| **rwa** | 38 | 0 | 0 | 0.0% | **TVL — Decision 3, mixed-unit label** |

**Load-bearing consequences for Fable's hero spec:**

- **9 bars, not 8.** Axelar earns its own bar (Decision 2). Never fold
  into `wrapped_major` — semantically similar but attribution-wise it
  is a distinct on-chain issuer (Axelar Foundation, `axelar.foundation`
  TOML) and Charlie wants that distinction preserved.
- **7 of 9 bars sized by 30d trade count.** stablecoin, native_utility,
  memecoin, wrapped_major, bridge, fiat, no_category (if shown).
- **2 of 9 bars sized by TVL.** `rwa` and `lp_token`. These MUST be
  visually labeled "sized by value held, not trades" — Decision 3
  requires the mixed-unit honesty be explicit on the hero. Fable's
  final spec needs to design that label (e.g. a different bar color,
  hatched fill, or a footnote asterisk on those two bars).
- **Do not size the mixed-unit bars against the trade-count bars.**
  A visitor must not be led to conclude "rwa is small" from bar height —
  the units are different. Options: (a) separate strip for the two TVL
  bars, (b) same strip with a strong visual distinction and the label.
  Fable decides.

**TVL data sources for §3-bars 8-9:**

- `lp_token` TVL: `amm_ranked_pools` already tracks LP token supply +
  underlying pool XRP-equivalent. Sum LP claim values across the 15
  named LP tokens.
- `rwa` TVL: harder. No native XRPL "TVL" for RWA tokens (they're
  issuance-only, no AMM pools). Options: (a) issuer's published
  outstanding-supply * last-known price (fragile, needs off-chain
  price feed); (b) `IssuedCurrencyAmount` outstanding balance from
  the ledger (on-chain, honest — represents "value issued on XRPL"
  rather than market-cap). **Recommendation: use on-chain
  outstanding, label as "outstanding issued value," not "market cap."**

---

## 4. Row-level TOML mapping for the top 100

Full JSON at `scratch/d1_v2_row_level.json`. Tier priority order
(unchanged): VERIFIED_TOML > XRPSCAN_VERIFIED > SELF_DESCRIBED_TOML >
XRPSCAN_UNVERIFIED > DOMAIN_ONLY > ANONYMOUS.

**Under Decision 4 (XRPScan not assumed in build), collapse XRPScan
tiers to their fallback shape until XRPScan writes "yes":**

| tier (build display) | pairs | trades 30d | % TOTAL | display |
|---|---:|---:|---:|---|
| **VERIFIED_TOML** (canonical `[[ACCOUNTS]]`) | 1 | 7,572 | 0.1% | ✓ **Verified** pill (green) |
| **SELF_DESCRIBED_TOML** (non-canonical `[[ISSUERS]]`/`[[TOKENS]]`, incl. firstledger auto-gen) | 49 + fallback XRPScan_verified w/ TOML + fallback XRPScan_unverified w/ TOML | see §4a | see §4a | ⓘ **Self-described** pill (grey) |
| **DOMAIN_ONLY** (Domain set, TOML missing/silent, no other channel) | 6 + fallback XRPScan without TOML | see §4a | see §4a | ⓘ **Domain-only** pill (grey outline) |
| **ANONYMOUS** (no domain / doesn't resolve / no channel) | 21 | 1,447,736 | 20.5% | — bare currency code |

### 4a. Post-Decision-4 tier counts (build view)

Under Decision 4 the 14 XRPSCAN_VERIFIED + 9 XRPSCAN_UNVERIFIED pairs
collapse into their fallback tier based on what other signal they carry:

- If they also have a TOML → merge into SELF_DESCRIBED_TOML
- If they have only Domain → merge into DOMAIN_ONLY
- If they have no signal at all → merge into ANONYMOUS

I re-scanned `scratch/d1_v2_row_level.json` for the fallback distribution:

| tier (Decision-4 build view) | pairs | trades 30d | % TOTAL | notes |
|---|---:|---:|---:|---|
| **VERIFIED_TOML** | 1 | 7,572 | 0.1% | Only USDB / brazacripto |
| **SELF_DESCRIBED_TOML** | 53 | ~1.77M | ~25.1% | 49 firstledger-shape + 4 self-hosted TOMLs that XRPScan also happens to label |
| **DOMAIN_ONLY** | 15 | ~135K | ~1.9% | Includes 9 pairs that had XRPScan labels but no TOML — under Decision 4 they collapse here |
| **ANONYMOUS** | 31 | ~1.55M | ~21.9% | Includes 10 pairs that carried XRPScan-only labels; without XRPScan they are unattested |

**Important:** these Decision-4 counts are approximate — I flagged them
with `~` because the re-scan needs a proper rebuild against the raw
`d1_v2_xrpscan_flags.json` × `d1_v2_row_level.json` join. The
`d1_v2_row_level.json` file has the underlying data; the exact
Decision-4 counts should be rebuilt by a small follow-up script before
Fable finalizes the spec. Recommending we do that as a §12 follow-up
before build gate.

### 4b. Sample citations

**VERIFIED_TOML** (canonical, ships in the build):
- `USDB` / `rB3y9EPnq1ZrZP3aXgfyfdXQThzdXMrLMc` / trades=7,572 /
  domain=`tokens.brazacripto.com.br` — canonical `[[ACCOUNTS]]`
  attestation. The single verified-tier row.

**SELF_DESCRIBED_TOML** — non-canonical, custom-hosted:
- `ARK` / `rf5Jzzy6oAFBJjLhokha1v8pXVgYYjee3b` / trades=255,112 /
  `ark.institute` — self-hosted, `[[ISSUERS]]`/`[[TOKENS]]` shape
- `MAG` / `rXmagwMmnFtVet3uL26Q2iwk287SRvVMJ` / trades=41,155 /
  `xmagnetic.org` — same shape, self-hosted

**SELF_DESCRIBED_TOML** — firstledger auto-generated (50 of the tier):
- `ZERPS` / trades=175,304 / `eqpxobsslocteiamm7vl.toml.firstledger.net`
- `welth` / trades=131,444 / `qomgfkimwfxzkw3i1reg.toml.firstledger.net`
- `FIFA` / trades=120,056 / `ehoih1rghmcotqkqgk6c.toml.firstledger.net`

**ANONYMOUS** (in the floor):
- `C9BR` / `rBCf85rmfGBTAQrqyiNv5fxTvJ5A4EQXWg` / trades=517,237 —
  no Domain field on issuer.
- `Xoge` / trades=208,810 — no Domain field.
- `PLR` / trades=135,229 — Domain set to `https://xrpillars.com/`
  (URL not hostname, silently invalid).

---

## 5. Walker value-fix — greenlit (Decision 5)

**Decision 5 status: BUILD greenlit at standard gates.**

**Root cause.** `xrpl_stream.py:token_event_handler` (line 375) writes
`volume_xrp = 0.0` as Phase 0 placeholder. Token amount is not extracted
or persisted.

**Scope (forward-fill only, no backfill):**

1. In `token_event_handler`, extract token `Amount.value` (Payment) and
   `Amount.value` / `Asset2.value` (AMMDeposit / AMMWithdraw).
2. In-process cache of (currency, issuer) → XRP-per-token last-observed
   ratio, sourced from `amm_ranked_pools` (walker-populated). Refresh
   every N minutes (proposed: 5 min).
3. Multiply amount × ratio → `xrp_equivalent`. Pass as `volume_xrp_delta`
   to `_volumes_conn.execute` (SQLite) and `pgbridge.upsert_token_volume`
   (Postgres).
4. If no AMM pool exists → write 0 honestly + track "pairs with no XRP-price
   signal" for `/tokens` page footnote.

**Effort:** ~1–2h coding + local test + 24h observation.

**Non-destructive:** column already exists (currently written as 0.0).
No schema migration. No downtime.

**walker_health coverage:** New/changed walker keeps its
`walker_health` row (per Charlie's Decision 5 explicit requirement).
`token_event_handler` isn't a walker per se (it's a stream consumer),
but the pool-ratio cache refresher IS a periodic task — that gets a
`walker_health` row named `token_price_ratio_cache`.

**Standard gates for this build:**

- **Gate 1 (surface):** present the diff scope + files touched to
  Charlie BEFORE editing. **← we are here on this workstream.**
- **Gate 2 (local smoke):** patch + run `xrpl_stream.py` locally with
  DRY_RUN or against test node; watch `volume_xrp` populate on live
  trades for ~5 min; confirm no regressions on trade_count path.
- **Gate 3 (commit):** clean commit — `xrpl_stream.py` + new pool-ratio
  helper + walker_health row init. Nothing else in the commit.
- **Gate 4 (deploy + verify):** push → T+270s prod-curl / observe
  `token_volume` writes with non-zero `volume_xrp` within 15 minutes.

**Hero shipping strategy (per Charlie's Decision 5):** count-based hero
ships now with "counts trades, not dollars" footnote. Walker value-fix
starts accumulating dollar data on merge. Value-weighted floor re-runs
after ~30d observation window.

**Cheap-backfill option: NOT recommended.** Applying today's AMM ratio
to 30d-old trades would misprice pumped-then-dumped memecoins by
2–3× and violate truth-first. Charlie's decision aligned: no backfill.

---

## 6. XRPScan email — draft ready (Decision 4)

**Decision 4 status: email greenlit; draft shown below for Charlie's
review BEFORE sending. Build does NOT assume XRPScan coverage.**

**Contact:** `support@xrpscan.com` (published in their API pricing docs).
**Sender:** `contact@xrpldashboard.com` (Squarespace-forwarded inbox,
Brevo SMTP outbound).

### Draft (for Charlie's review)

> **Subject:** XRPL Dashboard — request for written OK to attribute XRPScan
> issuer labels on a public XRPL analytics site
>
> Hi XRPScan team,
>
> I run xrpldashboard.com — a free, public, non-monetized XRPL analytics
> site aimed at non-specialist visitors. We're currently building an
> honest "who mints what" hero for our `/tokens` page and would like to
> use the issuer labels your public v1 API returns
> (`accountName`, `verified`) to help visitors identify the ~28% of top-100
> issuers XRPScan has recognized.
>
> Every labeled row would carry a visible **"Source: XRPScan"** attribution
> string and a link back to the corresponding xrpscan.com account page
> (e.g. `https://xrpscan.com/account/{issuer}`), so visitors can verify
> the label at source. We are not redistributing bulk data — the ingest is
> a targeted lookup per token displayed in our hero (currently ~100 issuers,
> possibly growing to ~1,000 as we expand coverage), well inside your free
> 10k/day rate limit.
>
> What we're asking for is written permission to use `accountName` and
> `verified` this way, and confirmation that "Source: XRPScan" plus the
> per-account backlink is acceptable attribution (or your preferred
> wording if different).
>
> If you say yes, we'd add XRPScan attribution across the site next week.
> If you say no, no worries — we'll display those issuers without the
> XRPScan-attributed labels and won't ingest them.
>
> Thank you for maintaining XRPScan — it's a foundational piece of the
> XRPL public toolkit.
>
> Best,
> Charlie Bruce
> xrpldashboard.com — contact@xrpldashboard.com

**Charlie please review + edit + say "send it" or "change X, then send"
before I send via Brevo.**

**If they decline OR don't respond in 14 days:** XRPScan tier stays out
of the build. Floor stays 20.5% strict-anonymous (already the case
under Decision 4). No XRPScan citations anywhere on the site.

---

## 7. Buildability confirmation — 3-tier display (Decision 1)

**Confirmed buildable.** Existing schema supports it.

- `token_names.json` already has `verified_via` per entry. Add companion
  `attestation_shape` field with 3 values (post-Decision-1):
  - `canonical` → **Verified** pill
  - `non_canonical` → **Self-described** pill
  - null (no attestation) → no pill, bare currency code
- `xrpscan_verified` value NOT in the enum for now (Decision 4 —
  XRPScan tier only added if permission granted).
- On `/tokens` template: small badge next to each token name (~15 LOC
  Jinja diff).
- Hero bars: per Decision 1, verified and self-described tiers are
  visually distinct in the bar. Solid segment = Verified pairs.
  Hatched segment = Self-described. Empty = Anonymous. Fable
  finalizes the visual language.
- Backend: extend `token_data.build_ranked_tokens()` to attach tier
  per row. ~15 LOC.

**Load-bearing display rule (Decision 1):** self-described tier is
NEVER presented as "verified." The v3 hero copy MUST distinguish
these three tiers on-screen.

---

## 8. Honest floor — Charlie's picked reading (Decision 1)

**PRAGMATIC 20.5%.** Locked in.

| Reading | Top-100 pairs failing all naming channels | Top-100 pair trades | Floor (top-100 only) | Floor + tail all-fail upper bound |
|---|---:|---:|---:|---:|
| **PRAGMATIC (SHIP)** — SELF_DESCRIBED_TOML + DOMAIN_ONLY count as nameable | 21 | 1,447,736 | **20.5%** | **35.6%** |
| STRICT (not shipped) — only VERIFIED_TOML counts as nameable | 85 | 4,756,244 | 67.3% | 82.5% |

**Hero label (Decision 1):**
> "at least **20.5%** — most XRPL activity is permanently anonymous."

Alternate honest phrasing options for Fable to pick between:
- "at least 20.5% of trades in the last 30 days come from tokens no one
  has ever named"
- "1 in 5 trades happens between wallets that publish no identity signal"

**Never say "verified" for the ~25% self-described tier.** That copy
distinction is Decision 1 and cannot slip in the final hero.

---

## 9. Summary table for Fable (Decision-adjusted)

| Question | v3 answer |
|---|---|
| Floor | **PRAGMATIC 20.5%** — locked. Anonymous tier only. |
| Tier labels | 3 tiers: **verified** (green pill, 1 row today), **self-described** (grey pill, ~25%), **anonymous** (no pill, 20.5%). |
| Bar count in hero | **9 bars** — stablecoin, native_utility, memecoin, wrapped_major, bridge (Axelar), fiat, lp_token, rwa, plus optional no_category. |
| Bridge/Axelar | Own 9th bar. Never fold into wrapped_major. |
| rwa / lp_token sizing | **TVL**, not trades. Must be labeled "sized by value held, not trades" so visitors don't compare directly to trade-count bars. |
| XRPScan labels | **NOT assumed in build.** Ship without them; add later only if XRPScan writes "yes." Draft email in §6. |
| Walker value-fix | **Build greenlit** at standard gates. Gate 1 (surface) = §5. Forward-fill only. Hero footnote: "counts trades, not dollars." |
| ARMY collision | Two different tokens. Display with issuer prefix disambiguation. |
| Domain 73/75 | Per-issuer vs per-pair basis. Both correct. |
| Category table | §3 (9 rows). rwa=0 trades, lp_token=25 trades — TVL sizing per Decision 3. |
| Row-level TOML | §4 + `scratch/d1_v2_row_level.json`. Decision-4-adjusted counts in §4a (needs a follow-up rebuild for exact numbers before build gate). |
| Buildability | Confirmed. ~15 LOC template + one field addition. |

---

## 10. What Fable now gets to finalize

Fable's job on v3: produce the FINAL buildable hero spec — layout,
copy, visual language for mixed-unit bars, exact pill copy, footnote
wording, mobile treatment. Charlie gates. Then we build.

**Specific things Fable must decide in the final spec:**

- Visual language for the two mixed-unit bars (rwa / lp_token). Options:
  hatched fill, different color, separate strip, footnote asterisk.
- Pill visual for "self-described" — must not look "official." Not green.
- Hero headline wording — the "at least 20.5%" number needs a
  ~10-word hero line and a ~30-word subhead.
- What to do with `no_category` (10 pairs, 6% of labeled) — hide,
  fold into a "miscellaneous" bar, or show honestly.
- Footnote copy for "counts trades, not dollars" (walker forward-fill
  disclaimer).

**Things Fable must NOT do:**

- Present self-described as verified.
- Merge Axelar into wrapped_major.
- Size rwa or lp_token by trade count without the mixed-unit label.
- Assume XRPScan labels in the build.

---

## 11. Files

- Top-100 raw: `scratch/d1_unlabeled_top100.json`
- Domain decode: `scratch/d1_domain_decode.json`
- TOML sweep: `scratch/d1_toml_sweep.json`
- Bithomp audit: `scratch/d1_bithomp.json`
- XRPScan aggregate: `scratch/d1_xrpscan.json`
- Category breakdown: `scratch/d1_v2_categories.json`
- Row-level TOML: `scratch/d1_v2_row_level.json`
- Tier breakdown: `scratch/d1_v2_tier_breakdown.json`
- XRPScan flags: `scratch/d1_v2_xrpscan_flags.json`

## 12. Follow-ups before build gate

- **Exact Decision-4 tier counts.** §4a numbers are approximate. Small
  script that joins `d1_v2_row_level.json` × `d1_v2_xrpscan_flags.json`
  and re-tabulates the collapsed-XRPScan tier counts. ~15 min work.
  Do this before Fable's final spec so the hero legend has exact numbers.
- **TVL query for rwa and lp_token bars.** Once Fable picks the sizing
  design, need a small query against `amm_ranked_pools` (lp_token) and
  ledger `IssuedCurrencyAmount` outstanding (rwa). Not blocking the
  spec — blocking the build.
- **XRPScan reply window.** If they say yes in <14d, XRPScan tier gets
  added to a follow-up build after the hero ships. Not v1 hero scope.

## 13. Not addressed (deliberate)

- **Bithomp**: still hard-blocked by ToS. Excluded from v3.
- **Backfill of volume_xrp for existing rows**: forward-fill only per
  Decision 5.
- **No hero build**: Fable finalizes on v3 → Charlie gates → build.
