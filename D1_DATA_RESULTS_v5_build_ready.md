# D1 — v5 build-ready spec (hero is now locked; Fable can build)

Supersedes the two open forks in `D1_DATA_RESULTS_v4_addendum.md`.
Nothing else changes: v3 stays, v4 stays; this file records Charlie's
final two calls plus the LP concentration hover requirement.

Decided 2026-07-03 evening EDT after Fable returned the Zone A/B spec.

---

## 1. Floor — 20.5% (locked, unchanged)

**PRAGMATIC 20.5%** is the hero number. Sources: v3 §8, v4 §1. Not open.

---

## 2. Axelar bar sizing — BROAD (0.798% of total 30d trades)

**All 9 Axelar-issuer currencies count as "Axelar bridge tokens" for the
hero.** 56,412 trades = 0.798% of total 30d trades.

- Currencies included (issuer `rfmS3zqrQrka8wVyhXifEeyTwe8AMz2Yhw`):
  mXRP, SOIL, USDC.axl, mBTC, WETH, WBTC, SHX, mTBILL, USDf
- Rationale (from v4 §2): "Axelar bridge tokens" is how a visitor reads
  it; NARROW makes the bar a hairline; 7 of the 9 being unlabeled in
  `token_names.json` is a metadata coverage gap, not a semantic
  exclusion signal.

### Downstream metadata patch (non-blocking on hero build)

Backfill category entries in `token_names.json` for the 7 currently-
unlabeled Axelar tokens: `mXRP`, `mBTC`, `mTBILL`, `WETH`, `WBTC`,
`USDC.axl`, `USDf`. Category = `bridge`. SHX should be double-checked
before adding — the 3-char code doesn't guarantee Axelar provenance.

This backfill improves the audit trail but does NOT change the hero
number — the BROAD reading is per-issuer, not per-category.

---

## 3. Zone-B shape — HYBRID (1 LP bar; RWA as caption + chip)

**Zone A (trade counts):** 7 or 8 bars —
`stablecoin`, `native_utility`, `memecoin`, `wrapped_major`,
`bridge` (0.798% per §2), `fiat`, `no_category` (optional). Same
visual rules as Fable's spec.

**Zone B (walled dollar-labelled zone):** 1 bar —
`LP_TOKEN = $5,802,013 USD`. Sized by pool value held, not trades.
Mixed-unit label required (per Fable's original constraint).

**RWA: NOT a bar.** Caption strip under the Zone A / Zone B hero:
> "38 real-world-asset tokens issued on XRPL — held on-chain, not
> actively traded, no on-chain market anchor available."

Optional `/tokens` filter chip for "RWA" — same population as the 38
caption number.

### 3a. LP bar concentration hover — REQUIRED

**Non-negotiable.** The LP bar is 81.3% one pool (XRP/RLUSD, $4.72M of
$5.80M). A visitor reading the bar as broad LP diversity would be
misled. On hover / tap, show the honest concentration breakdown:

| pool pair       | tvl_usd       | share of LP bar |
|-----------------|---------------|-----------------|
| XRP/RLUSD       | $4,718,731.71 | 81.3%           |
| XRP/USDC        | $638,031.97   | 11.0%           |
| XRP/BTC.Bitstamp| $288,356.08   | 5.0%            |
| XRP/SOLO        | $117,379.49   | 2.0%            |
| XRP/SOIL        | $24,035.26    | 0.4%            |
| XRP/ZRP         | $15,381.51    | 0.3%            |
| XRP/mTBILL      | $95.63        | 0.002%          |
| USD.Bitstamp/USDC | $1.16       | 0.00%           |

Same honest-concentration discipline as everywhere else on the site
(e.g. escrow concentration on `/cold-storage`, whale distribution on
`/whales`).

Full row source: `scratch/d1_v4_missing_three.json`.

---

## 4. Numbers Fable can bake into the hero right now

| slot                       | value                                     |
|----------------------------|-------------------------------------------|
| floor                      | 20.5% (pragmatic, v3 §8)                  |
| Zone A trade-count bars    | 7 or 8 per v3 §3 category table           |
| Zone A: bridge share       | **0.798%** (56,412 / total 30d)           |
| Zone B: LP_TOKEN bar       | **$5,802,013 USD**                        |
| Zone B: LP top-1 hover     | XRP/RLUSD = $4,718,731.71 (81.3%)         |
| RWA caption strip          | 38 tokens, 0 priced, "no market anchor"   |
| RWA optional filter chip   | `/tokens?category=rwa`                    |

All values sourced from `scratch/d1_v4_missing_three.json` +
v3 §3 category totals.

---

## 5. Open items NOT blocking build

- `token_names.json` bridge-category backfill for 7 Axelar tokens (§2)
- Walker value-fix re-anchor (30-day clock restarts at re-kickstart, not
  the 2026-07-03 15:33:55 EDT original — see `scratch/outbound_log/
  2026-07-03_walker_value_fix_kickstart.md` + Gate 2b smoke report)

---

## 6. Handshake

Fable now has:
- v3 (5 decisions + tier-based population)
- v4 (three missing values + Zone-B fork options)
- v5 (this file — final calls: BROAD, HYBRID, RLUSD-concentration hover)

Hero build unblocked on Charlie's "go".
