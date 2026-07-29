# D1 — v4 addendum (three missing values for Fable's final spec)

Extends `D1_DATA_RESULTS_v3.md`. Adds the three values Fable flagged
plus a Zone-B recommendation and confirms the 20.5% floor is locked.

Data pulled 2026-07-03 evening; source `scratch/d1_v4_missing_three.json`.

---

## 1. Floor reconciliation — 20.5% CONFIRMED

**PRAGMATIC 20.5% is the locked hero number.** No drift from v3 §8.

- v1 pull-6 = 21.5% (failing-all-channels reading, treated DOMAIN_ONLY as failing)
- v2 §8 = 20.5% (tier-based reading, excluded DOMAIN_ONLY from anonymous)
- v3 §8 = 20.5% (Charlie's Decision 1: pragmatic)
- Fable's spec = 20.5% (confirmed)

**Hero copy locked at 20.5%** — 21.5% is a superseded value, do not use.

---

## 2. Bridge (Axelar) 30d trade share — **0.798% of TOTAL**

**Headline: 56,412 trades across 9 currencies = 0.798% of total 30d trades.**

Broken down per currency (all minted by `rfmS3zqrQrka8wVyhXifEeyTwe8AMz2Yhw`):

| # | currency | trades 30d | category in token_names.json |
|---:|---|---:|---|
| 1 | mXRP | 15,249 | *not in token_names.json (unlabeled)* |
| 2 | SOIL | 11,036 | **bridge** |
| 3 | USDC.axl | 8,347 | *not in token_names.json (unlabeled)* |
| 4 | mBTC | 7,007 | *not in token_names.json (unlabeled)* |
| 5 | WETH | 4,251 | *not in token_names.json (unlabeled)* |
| 6 | WBTC | 4,230 | *not in token_names.json (unlabeled)* |
| 7 | SHX* | 3,310 | *not in token_names.json (unlabeled)* |
| 8 | mTBILL | 2,673 | *no category (in token_names.json but null category)* |
| 9 | USDf | 309 | *not in token_names.json (unlabeled)* |

*SHX row uses raw 3-char code, not hex-40.

### Load-bearing sub-question for Fable

The v3 §3 category table sized `bridge` at **11,030 trades / 0.156% of TOTAL** — that was SOIL alone (the only currency currently categorized `bridge` in `token_names.json`).

Charlie's Decision 2 says "9th bar = Axelar bridge tokens." Two honest readings:

- **NARROW (bridge-category only):** 1 currency (SOIL), 11,036 trades, **0.156% of total**. Matches v3 §3.
- **BROAD (whole-Axelar-issuer):** 9 currencies (all bridge-plumbing per Axelar's protocol design), 56,412 trades, **0.798% of total**. What a normie reads "Axelar bridge tokens" to mean.

**Recommended for hero: BROAD (0.798%).** Reasons:
1. All 9 currencies flow through Axelar's cross-chain bridge — they ARE Axelar-bridge tokens semantically.
2. NARROW makes the bar disappear (0.156% renders as a hairline).
3. The "unlabeled" categorization for 7 of the 9 is a token_names.json coverage gap, not a semantic one — those tokens are genuinely bridged assets.
4. Adopting BROAD adds a small task to backfill 7 category entries in `token_names.json` (`mXRP`, `mBTC`, `mTBILL`, `WETH`, `WBTC`, `USDC.axl`, `USDf`, `SHX` — this last one may not actually be Axelar, worth double-checking) but that's a metadata patch, not a structural change.

Alternate: keep NARROW for the hero AND add a "bridge coverage patch" TODO for the 7 unlabeled Axelar-issued tokens. Then re-run the number on the next Fable pass.

**Charlie decides NARROW vs BROAD.** Data is documented for either.

---

## 3. RWA TVL — **0 computable** ($0 honest floor)

**Headline: 38 named RWA tokens, 0 priced. No honest USD anchor.**

The `token_prices` reader returned **0 priced entries** for the 38 RWA (currency, issuer) pairs. Every RWA token in `token_names.json` lacks an XRP-paired AMM pool above the 2500 XRP dust gate — so `token_prices.py` deliberately did not publish a price for them (per its "absence IS the signal" rule).

Available proxies (all with caveats):

| proxy | what it gives | honest? |
|---|---|---|
| `token_prices` cache | $0 across all 38 | Yes (absence = "no market anchor on-chain") |
| `gateway_balances` per issuer | outstanding token count, no USD | Half — reveals issuance scale but nothing about VALUE |
| Issuer's off-chain publication | claimed USD value from prospectus | No — self-reported, unverifiable, out of scope |
| CoinGecko/similar | market cap for a few listed RWAs (e.g. Ondo USDY) | Partial — <5 of 38 have any off-chain listing at all; and none of the Ondo/Tokeniza type entries are in this 38-pair cohort |

**Best honest number available: $0 computable TVL.** Any non-zero number would come from self-reported issuer claims, violating truth-first.

**Recommendation: represent RWA as "38 tokens issued on XRPL, on-chain custody only, no market anchor available" — no numeric bar.** Alternative: report the outstanding token *count* per issuer as an "issuance activity" signal, but that's a different metric than TVL and Fable's spec doesn't need it for the hero.

---

## 4. LP_token TVL — **$5,802,013 USD** (real, meaningful)

**Headline: 8 of 15 LP tokens are priced, sum = ~$5.8M USD.** Concentrated in the top pool.

| pool pair | tvl_usd | tvl_status |
|---|---:|---|
| XRP/RLUSD | $4,718,731.71 | exact |
| XRP/USDC | $638,031.97 | exact |
| XRP/BTC.Bitstamp | $288,356.08 | estimated |
| XRP/SOLO | $117,379.49 | estimated |
| XRP/SOIL | $24,035.26 | estimated |
| XRP/ZRP | $15,381.51 | estimated |
| XRP/mTBILL | $95.63 | estimated |
| USD.Bitstamp/USDC | $1.16 | exact |
| USD.Bitstamp/SOLO | $0.00 | non_xrp_pair (unpriceable in current pipeline) |
| **Sum** | **$5,802,013** | mix of exact + estimated |

- **15 named LP tokens** in `token_names.json`
- **9 matched** to current `amm_ranked_pools` (issuer = AMM account lookup)
- **6 unmatched** — the AMM accounts for those LP tokens don't appear in the current `amm_ranked_pools` snapshot (either stale `token_names.json` entries or removed/inactive pools). Excluded from TVL sum honestly.

**Concentration risk:** XRP/RLUSD alone is $4.72M = **81.3% of the LP TVL bar**. If Fable renders LP as one bar, that bar is essentially "XRPL's RLUSD pool." Worth noting in the design.

**Confidence:** `tvl_status` = `exact` for the top two pools (which are $4.72M + $638k = $5.36M = 92% of the bar). Rest are `estimated` from off-XRP-pair reference prices. All within our normal AMM-page confidence.

---

## 5. Zone-B fork recommendation

Fable's fork: **if both TVLs are meaningful → keep Zone B (2 walled bars); if negligible → drop → make them table-filter chips.**

**Reality is a split:**

- LP_TOKEN: **$5.8M** — meaningful, real signal, one bar dominant
- RWA: **$0 computable** — no honest bar possible

**Recommended: HYBRID — Zone B stays but as a 1-bar zone.**

- **Zone A (7 or 8 bars):** trade-count bars — stablecoin, native_utility, memecoin, wrapped_major, bridge (0.156% NARROW or 0.798% BROAD per §2), fiat, no_category (optional). Same visual rules as Fable's spec.
- **Zone B (1 bar):** LP_TOKEN = ~$5.8M USD, walled off, "sized by pool value held, not trades" per Fable's original mixed-unit rule.
- **RWA: not a bar.** Instead: a caption strip under the Zone A / Zone B hero, e.g. "38 real-world-asset tokens issued on XRPL — held on-chain, not actively traded, no market anchor available." Table-filter chip on the /tokens table also works for the filter-chip fork Fable proposed.

**Why hybrid over pure drop:**
- LP TVL is $5.8M of real value. Dropping it removes signal a data hero should surface. LP tokens ARE the AMM economy on XRPL — muting them entirely misrepresents the surface.
- Zone B with one meaningful bar still works visually (walled zone, dollar-labeled, clear "different measure" framing).

**Why hybrid over Fable's original 2-bar Zone B:**
- Faking an RWA bar at $0 (or worse, fabricating a value) breaks truth-first.
- A 1-bar Zone B is honest; a 2-bar zone with one bar at zero visually reads as "RWA is failing" which isn't accurate — RWAs aren't failing, they just don't trade and don't publish on-chain valuations. The chip/caption framing communicates that more honestly.

**Alternative if Charlie prefers Fable's pure drop (Zone-A-only hero):** ship 7 or 8 Zone A bars, RWA and LP_TOKEN both become filter chips on the /tokens table. This is cleaner visually but loses the $5.8M signal in the hero. Documented as an option.

**Charlie's call.** Numbers are locked either way.

---

## 6. Files

- Full JSON: `scratch/d1_v4_missing_three.json`
- Pull script: `scripts/d1_v4_missing_three.py`

---

## 7. Handshake for Fable

Fable finalizes on v3 + this v4 addendum. Two calls Charlie owes before build:

1. **Axelar bar sizing:** NARROW (0.156%, SOIL only) or BROAD (0.798%, whole issuer)?
2. **Zone-B shape:** 1-bar hybrid (LP stays, RWA becomes chip) OR pure drop (both become chips, Zone-A-only hero)?

Once Charlie answers those two, the hero is build-ready and we open the build gates.

---

## 8. NOT changed

- Floor: 20.5% pragmatic, locked.
- 3-tier display: verified / self-described / anonymous (Decision 1) — unchanged.
- XRPScan: still NOT assumed in build (Decision 4) — unchanged.
- Walker value-fix: shipped Gate 3 + kickstarted at 15:33:55 EDT (see `scratch/outbound_log/2026-07-03_walker_value_fix_kickstart.md`). Gate 4 observation window closes ~15:48:55 EDT.
