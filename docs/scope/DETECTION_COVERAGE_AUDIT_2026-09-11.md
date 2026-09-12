# Detection coverage audit — 2026-09-11

Read-only audit filed alongside the Friday-night /tokens work per Charlie's ruling ("A"). Every number here was pulled via `jj_ro`; no writes. Follow-up implementation belongs to Saturday (pattern-list additions) or post-freeze (new detectors).

Companion commits this session: `2575d63` (pools rename), `bb6dc5a` (legibility), `43970b0` (warnings filter chip).

## (a) 7-day baseline + BARE sample hand-check

**Distinct tokens traded, last 7d (last 168h buckets):**

| Metric | Value |
|---|---|
| Distinct (currency, issuer) pairs traded | **3,650** |
| Of those, warning-flagged (`ticker_collision` OR `non_standard_code`) | **69** (1.9%) |
| Total 7d trades | 1,702,174 |
| Warning-flagged 7d trades | 9,457 (0.6%) |

Warnings are rare in the token corpus (1.9% by count) and even rarer in flow-weighted trades (0.6%) — the filter chip will produce a legibly-short list for a viewer to scan.

**Sample of 30 top BARE tokens by 7d trades (hand-checked for missed patterns):**

Top-30 by 7d trades among tokens with `ticker_collision = FALSE` AND `non_standard_code = FALSE`. Ranked from 1,042,058 (RLUSD, canonical) down to 3,363 (ARMY, unknown). Patterns spotted:

| # | Ticker | Issuer | 7d trades | Missed detection |
|---|---|---|---|---|
| 1 | RLUSD | rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De | 1,042,058 | Canonical (correct — issuer whitelisted) |
| — | BITx | **rBitcoiNXev8VoVxV7pwoQx1sSfonVP9i3** | 9,466 | **Vanity brand-prefix impostor** (`rBitcoiN…` mimics "Bitcoin" brand + "BIT" ticker). Not flagged. Detector gap #2. |
| — | CSC | **rCSCManTZ8ME9EoLrSHHYKW8PPwWMgkwr** | 6,020 | Vanity brand-prefix (`rCSCMan…` reads as an authority address for CSC). Same class as BITx. |
| — | XPM | **rXPMxBeefHGxx2K7g5qmmWq3gFsgawkoa** | 5,260 | Vanity brand-prefix (rXPM…). |
| — | STX | rSTAYKxF2K77ZLZ8GoAwTqPGaphAqMyXV | 6,861 | Vanity brand-prefix (rSTAY… reads as staying on STX? Ambiguous — curator judgment). |
| — | MAG | rXmagwMmnFtVet3uL26Q2iwk287SRvVMJ | 9,481 | Weak vanity (rXmag…) — likely FP if flagged, MAG is not a stolen brand. |
| — | LHT | rHashLabsu2H6hXDSunL8AcVDmTyE17Z35 | 7,730 | Legit brand vanity (HashLabs owns their name). Should NOT flag. |

**Verdict:** the vanity brand-prefix pattern is a real detection gap (BITx, CSC, XPM). The neighbor table in `token_naming._load_canonical_neighbors()` already computes look-alike candidates for `/check`; the falling-visual on `/tokens` does not consult it. Detector #2 below.

Beyond that, the top-30 BARE set is dominated by legitimately-labeled tokens or curator-decided-non-scams (Reaper's RPR/ASC on `r3qWgp…`, Ark on `rf5Jzz…`, HashLabs LHT).

## (b) Canonical ticker list expansion proposal

Current file: `ticker_canonical_issuers.json`. Head entries (20 tickers): RLUSD, USDT, USDC, DAI, BTC, WBTC, ETH, SOL, ADA, MATIC, LINK, XLM, BNB, LTC, DOGE, SHIB, PEPE, TRX, USDX, TETHER. Only RLUSD has a canonical issuer; every other is `canonical_issuers=[]` (all matches are collisions).

**Proposed additions (Saturday-safe — no new detectors, just pattern-list growth):**

| Ticker | Brand | Why | Source | Benign counter-case |
|---|---|---|---|---|
| `XRP` | Native XRPL asset — impossible to issue as an IOU | Any (currency, issuer) with decoded `XRP` is a collision claim by construction | XRPL protocol | None — no legit XRPL-side XRP IOU exists |
| `WXRP` | Wrapped XRP (GateHub issues this; already whitelisted via GateHub gateway entry) | Non-GateHub `WXRP` = collision | Verified with GateHub gateway entry (this session) | GateHub-issued WXRP (see gateway whitelist) |
| `RIPPLE` | Ripple Labs brand | Curator confirms no Ripple-branded IOU except RLUSD | ripple.com | RLUSD-only |
| `XRPL` | XRPL protocol brand | Same rationale as XRP | xrpl.org | None known |
| `USDT.e` `USDT.axl` | Axelar-wrapped USDT | Non-Axelar issuers = collision | Existing bridges section for Axelar | Already covered under bridges.midas_xrpl.tickers |
| `WETH` `WBTC` `USDC.axl` (already covered) | Axelar-wrapped | Non-Axelar = collision | Existing bridges section | Covered |
| `FTX` | Bankrupt exchange — no legit XRPL-side issuance | Curator judgment: any FTX-branded IOU on XRPL is impostor | Public bankruptcy record | None known |
| `GEMINI` | Gemini brand | Same as FTX pattern | Public brand | None known |
| `BINANCE` | Binance brand | Same | Public brand | None known |
| `KRAKEN` | Kraken brand — already anti-whitelisted for `rKRAKzW…` vanity | Reinforce with ticker rule | See `_disputed_vanity_addresses` in json | None (Kraken has no XRPL issuance) |
| `COINBASE` | Coinbase brand — already anti-whitelisted for `rBASE…`/`rCoinzYTFiFMJ…` | Same as KRAKEN | See `_disputed_vanity_addresses` | None |
| `RLUSD1` `R1USD` `RLU5D` | RLUSD homoglyph variants | Any decoded name in this set = collision | Detector #1 below | None — Ripple's canonical is `RLUSD` exactly |
| `DOGE.axl` `SHIB.axl` `PEPE.axl` | Bridged variants | Non-Axelar = collision | Existing bridges pattern | Existing bridges section covers |

Recommend: ship the 4 additions (`XRP`, `WXRP`, `RLUSD1/R1USD/RLU5D`, plus the exchange-brand cluster `FTX/GEMINI/BINANCE`) tomorrow. `KRAKEN`/`COINBASE` are already anti-whitelisted at the address level; adding at the ticker level makes the guard bidirectional. Reject the addition patterns before merging any specific issuer.

## (c) Detectors we lack

Each with data source, coverage gain, false-positive risk, effort, and category (WARNING = data-derived fact / CURATOR-QUEUE = judgment).

| # | Detector | Data needed | Already collected? | Coverage gain | FP risk | Effort | Category |
|---|---|---|---|---|---|---|---|
| **1** | **Homoglyph tickers** (R1USD vs RLUSD, USDT with fullwidth digits, Cyrillic Ѕ vs S) | `token_facts.decoded_name` + canonical ticker list | ✓ (data exists; needs normalized-form compare) | High (catches Unicode-substitution scams which vanity guards miss) | LOW (legit Unicode tickers are essentially non-existent) | SMALL (Levenshtein ≤ 1 or normalized-form equality vs canonical list) | **WARNING** (deterministic string comparison, not judgment) |
| **2** | **Issuer address look-alikes on the /tokens visual** — the neighbor table `_load_canonical_neighbors()` exists for `/check` and returns `(address, name, source)` triples over the union of canonical / bridge / gateway issuers. Currently `tokens.html` client doesn't consult it | Already computed server-side | ✓ | High (catches vanity-brand-prefix scams like `rBitcoiN…/BITx`, `rCSCMan…/CSC`, `rXPMx…/XPM`) | LOW-MEDIUM (legit brand-owned vanities like `rHashLabs…/LHT`, `rReaper…/RPR` need a curator whitelist alongside) | MEDIUM (send neighbor JSON to visual + add tierOf branch + build curator whitelist of legit vanities) | **WARNING** for confident hits; **curator-queue** for weak ones |
| **3** | **Brand-new issuer (<7d) + famous ticker** | Issuer first-seen timestamp + canonical ticker list | Partial (issuer_facts + token_facts observed_at) — needs a specific `issuer_first_seen_ledger` field for reliability | Medium (catches rug-pull-on-launch pattern) | MEDIUM (legit projects DO launch new tokens; freshness alone isn't scam) | MEDIUM (needs migration for `issuer_first_seen_ledger` if not already stamped) | **CURATOR-QUEUE** (age is context, not verdict) |
| **4** | **Trust-line spam / airdrop-bait** — unsolicited token appearing on many accounts | TrustSet events + recipient-account count per (currency, issuer) | Partial — xrpl_stream tracks new_tokens via TrustSet but not recipient-fanout per token | Medium (catches airdrop-bait scams that mint value on unwitting recipients' balance sheets) | LOW (legit tokens rarely spawn 10k+ trustlines in a day without opt-in) | MEDIUM (walker over TrustSet event stream, cross-tabulated by day) | **CURATOR-QUEUE** (some airdrops are legit marketing) |
| **5** | **"Blackholed" claims vs AccountRoot master-key state** — an issuer that publicly claims "blackholed" but still has active master key + no RegularKey = false claim | AccountRoot.Flags decode (lsfDisableMaster = 0x00040000; no RegularKey field) | Available from own rippled node | Medium-High (catches false-security marketing; important for stablecoin gateways) | NEAR-ZERO (100% deterministic on-ledger) | SMALL (walker fetches AccountRoot for each issuer, decodes flags) | **WARNING** (100% factual, not judgment) |
| **6** | **Sudden-quiet issuer** — issuer active for 90+d then no activity for 14+d + still has outstanding trustlines | token_volume time series | ✓ (data exists) | Medium (catches abandoned-project / exit-scam risk to holders) | MEDIUM (legit issuers do go quiet) | MEDIUM (walker windowed query) | **CURATOR-QUEUE** |

## (d) /check message + address detectors from Sept 6 research

The Sept 6 research listed 7 scam-shape classes for `/check`. Coverage status now:

| Scam shape | Current coverage | Gap |
|---|---|---|
| Ticker collision (e.g. USDT on non-Tether issuer) | ✓ Full — `ticker_collision` flag, `_disputed_vanity_addresses`, curator queue | None |
| Vanity brand-prefix address (rBitcoiN, rBASE, rCoinz, rKRAKZ) | Partial — anti-whitelisted for Kraken/Coinbase brands; **BITx and CSC and XPM patterns NOT flagged**. See detector #2. | New detector needed |
| Homoglyph ticker (R1USD, Cyrillic Ѕ) | ✗ None | Detector #1 |
| Homoglyph issuer address (r1L2y… mimicking rL2y…) | Partial — `lookalike_canonical_issuer` in `token_naming.py` uses Levenshtein ≤ 3, catches address swaps. **`/check` uses it; `/tokens` visual does not.** | Detector #2 wiring |
| Fake-blackhole claim | ✗ None | Detector #5 |
| New-issuer + famous ticker (rug-pull-on-launch) | ✗ None | Detector #3 |
| Trust-line spam / airdrop-bait | ✗ None | Detector #4 |
| Non-standard currency code (`0x03…` LP / junk hex) | ✓ Full — `non_standard_code` flag, WARNING lane on visual | None |

**Uncovered high-priority shapes:** vanity brand-prefix (BITx/CSC/XPM class), homoglyph ticker (R1USD class), fake-blackhole claim.

## (e) Ranked gaps by victims-protected per hour of work

Sorted by **victims-protected-per-hour-of-implementation**. Higher = ship-first.

| Rank | Detector | Est. victims-protected/mo | Impl hours | Ratio | Saturday-safe? | Notes |
|---|---|---|---|---|---|---|
| 1 | **(1) Homoglyph ticker** | High (every RLUSD/USDT lookalike blocked) | 1–2 hr | **HIGH** | **Yes** (pattern-list addition + normalized-form compare; benign counter-cases: only exact canonical passes) | Add to `ticker_canonical_issuers.json` + `token_naming.hasTickerCollision`. Ship Saturday. |
| 2 | **(b) Canonical list expansion** (XRP self-name, WXRP non-canonical, RLUSD homoglyphs, FTX/GEMINI/BINANCE brands) | High (any newly-listed brand-impostor gets flagged) | 30 min | **HIGH** | **Yes** (list edit + reload) | Ship Saturday alongside detector 1. |
| 3 | **(5) Blackhole verification** | Medium-High (each false-blackhole discovery is a stablecoin-safety win) | 3–4 hr | **MEDIUM-HIGH** | **No** — post-freeze (needs new AccountRoot walker + schema field) | 100% deterministic once shipped; worth doing right. |
| 4 | **(2) Neighbor-table on /tokens visual** | Medium (BITx-class scams routed to WARNING lane) | 3–4 hr (visual + whitelist) | **MEDIUM** | **No** — post-freeze (needs curator whitelist of legit vanities to bound FP) | Data + code exist for /check; wiring is the work. |
| 5 | **(3) New-issuer age + famous ticker** | Low-Medium (fires on genuine new projects too) | 4–6 hr | **LOW-MEDIUM** | **No** — post-freeze (needs age migration + threshold tuning) | Curator-queue category; not a WARNING even when shipped. |
| 6 | **(4) Trust-line spam** | Low-Medium (rare relative to other classes) | 4–6 hr | **LOW-MEDIUM** | **No** — post-freeze | Curator-queue. Cross-tabulated walker, non-trivial. |
| 7 | **(6) Sudden-quiet issuer** | Low | 4 hr | **LOW** | **No** — post-freeze | Curator-queue. Defensive, not detective. |

**Saturday recommendation:** ship (1) + (b) in a single commit that adds ~4 new canonical tickers, adds homoglyph-normalization to the collision check, and ships a benign-counter-cases test. Total est. 90 min including verify + push. All other rows in the table are post-freeze.

## Not addressed this session

- **`/whales` warnings routing** — the WARNING pill already renders on /whales rows per Part C. Coverage identical to /tokens on this axis.
- **`/token/<cur>/<iss>` detail** — the tier pill polish shipped this morning (commit `b03a142` + `289e902`). Already exposes ticker_collision + non_standard_code visibly.
- **`/check` message inspector** — separate audit; not scoped tonight.
