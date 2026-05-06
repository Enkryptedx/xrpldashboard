# Token Economy — architecture sketch

Step 3 of the roadmap. The "what's happening with XRPL tokens" panel —
top tokens by volume, gainers/losers, new launches today, stablecoin
circulation. Sits between the AMM table (Step 2, what we have) and
whale watch (Step 4).

## Locked-in decisions

These follow the same principles already locked for AMM scan and
whale watch.

1. **Data source: pure XRPL node, no aggregator.** Same rule as live
   AMM data and whale events. Token catalog, prices, and volumes all
   derived from raw ledger queries + our own subscription stream.
   No XPM API, no xrpl.to API, no Bithomp API for live data.
2. **Pricing model: AMM-derived implied price.** For tokens with an
   XRP/token AMM in our index, price = XRP_amount / token_amount ×
   XRP_USD_price. For tokens with no AMM, no price (display TVL by
   trustline holdings only, or omit). Keeps us honest — we only
   price what the ledger lets us price.
3. **Display style: pure data, no editorial inference.** Same as
   whale watch. We show "Token X up 23%, $400k 24h volume." We do
   not say "Token X is having a moment." Reader interprets.
4. **Scam-risk indicator: deferred.** Real fraud-detection requires
   either heuristics that risk false positives (dangerous) or
   community attestation. Skip at launch; ship in a later phase
   when the methodology is solid.

## What "token economy" actually means here

Headline metrics for the homepage card:

- **Top 10 tokens by 24h volume**, with price + 24h change
- **Biggest 24h gainer / loser** (price change, must have ≥ $X liquidity)
- **New tokens launched today** (first appearance on ledger)
- **Total RLUSD in circulation** (Ripple's own stablecoin, easy to
  track because it's a single issuer)
- **Total stablecoin TVL on XRPL** (sum across RLUSD, USDC, USD.*)

Section page `/tokens` breaks each of those out, with filters and a
fuller per-token list.

Per-token detail page `/token/<currency>:<issuer>`:
- Issuer info (account, account flags, age on ledger)
- Total supply (sum of trustline holdings)
- Top 10 holders
- All AMMs paired with the token
- 24h volume + price chart (once historical infra exists)

## Components

```
+--------------------+   +-------------------+   +-----------------+
| token_catalog.py   |-->| token_index.json  |   | xrpl_stream     |
| (one-time scan +   |   | (currency:issuer  |<--| (subscribe;     |
|  incremental from  |   | catalog with      |   | OfferCreate +   |
|  stream)           |   | first-seen, ...)  |   | AMM swap fills) |
+--------------------+   +-------------------+   +-----------------+
                                                           |
                                                           v
                                                  +-----------------+
                                                  | volumes.db      |
                                                  | (sqlite,        |
                                                  | hourly buckets, |
                                                  | 30d retention)  |
                                                  +-----------------+
                                                           |
                                                           v
                                                  +-----------------+
                                                  | token_prices.py |
                                                  | (derive from    |
                                                  | AMM index)      |
                                                  +-----------------+
                                                           |
                                                           v
                                                  +-----------------+
                                                  | /tokens page +  |
                                                  | homepage card   |
                                                  +-----------------+
```

### `token_catalog.py` — discovery

Same pattern as `scan_all_amms.py`. Walk `ledger_data` filtered to
`RippleState` (trustlines). Each trustline references a (currency,
issuer) pair — that's how we discover every token on the ledger.

```
{
  "5852646F6765000000000000000000000000000000:rLqUC2eCPo...": {
    "currency_hex": "5852646F6765000000000000000000000000000000",
    "currency_display": "XRdoge",
    "issuer": "rLqUC2eCPohYvJCEBJ77eCCqVL2uEiczjA",
    "first_seen_ledger": 89342110,
    "first_seen_ts": "2025-04-12T10:23:18Z",
    "trustline_count": 14823,
    "amm_pools": ["rPHGdrECf9GNUsJSb1dCpUUzpuduuPRAXs"]
  }
}
```

Initial scan: another multi-hour walk like the AMM bootstrap. (Could
piggyback on the same scan if we filter ledger objects to multiple
types — investigate whether `ledger_data` accepts a type list.)

Incremental: subscription stream catches `TrustSet` (new trustline
to an unseen currency:issuer = new token detected) and
`AMMCreate` (new pool referencing a token).

### `xrpl_stream` — same connection as whale watch

The token economy handler subscribes to:
- `OfferCreate` → fill events (DEX trades) for token volume
- `AMMDeposit` / `AMMWithdraw` / Payment-via-AMM (AMM swap fills) for
  AMM-side volume
- `TrustSet` for new-token detection

One websocket, multiple handlers. Already established in WHALE_WATCH.md.

### `volumes.db` — sqlite, hourly buckets, 30d

```
CREATE TABLE token_volume (
  currency      TEXT NOT NULL,
  issuer        TEXT NOT NULL,
  hour_bucket   INTEGER NOT NULL,   -- unix epoch hour
  volume_xrp    REAL NOT NULL,      -- sum of XRP-equivalent volume
  trade_count   INTEGER NOT NULL,
  PRIMARY KEY (currency, issuer, hour_bucket)
);

CREATE TABLE token_price_snap (
  currency      TEXT NOT NULL,
  issuer        TEXT NOT NULL,
  ts            INTEGER NOT NULL,
  price_xrp     REAL NOT NULL,      -- 1 token = N XRP
  source_amm    TEXT NOT NULL,      -- which AMM we derived from
  PRIMARY KEY (currency, issuer, ts)
);
```

Hourly bucket is the right granularity for "24h volume" — sum 24
buckets, you have it. Price snaps every N minutes give us 24h-ago
comparisons for gainer/loser calculations.

Storage stays small: ~2k tokens × 24h × 12 price snaps/h × ~50 bytes
≈ 30 MB / day. Sqlite handles this trivially.

### `token_prices.py` — pricing rules

```python
def price_token_xrp(currency, issuer, amm_index):
    """Returns (price_in_xrp, source_amm_account) or (None, None)."""
    pool = amm_index.find_pool(asset_xrp=True,
                               asset2=(currency, issuer))
    if not pool:
        return (None, None)
    if pool.token_amount <= 0 or pool.xrp_amount <= 0:
        return (None, None)
    return (pool.xrp_amount / pool.token_amount, pool.account)
```

Tokens without an XRP/token AMM: no price. Display "—" in the price
column. Honest about what we can and can't compute.

Token/token AMM-only tokens (rare): defer. Could route through a
common token (RLUSD) but adds complexity — skip at launch.

## What gets shown where

### Homepage card
```
Token economy — last 24h
+----------------------------------------------------+
| TOKEN          PRICE       24H        VOLUME       |
| RLUSD          $1.00       0.0%       $1.2M        |
| SOLO           0.51 XRP    +4.2%      $890K        |
| CSC            0.04 XRP    -1.8%      $620K        |
| XRdoge         0.00012 XRP +12.4%     $410K        |
| ...                                                |
| 3 new tokens launched today · view all → /tokens  |
+----------------------------------------------------+
```

### `/tokens` section page
- Top tokens by 24h volume (full list, paginated)
- Filter: stablecoins / wrapped / native / memecoins / all
- Filter: only tokens with AMM liquidity ≥ $X
- "Today's new tokens" sub-section
- "Stablecoin tracker" sub-section (RLUSD, USDC.*, USD.*)
- Daily summary (factual): "N tokens with > $10k 24h volume,
  total token volume $X, M new tokens detected today"

### Per-token detail page `/token/<currency>:<issuer>`
- Title: display name + currency hex + issuer address
- Issuer card: account flags, age, total trustlines
- Liquidity card: list of AMM pools containing this token, per-pool TVL
- Holders card: top 10 holding accounts
- Volume / price card: chart once historical infra exists; until then
  current snapshot only
- Link out: XRPSCAN account page, Bithomp issuer page

## Build phases

### Phase 0: data plumbing
- Extend `xrpl_stream` (the same websocket handler from whale watch)
  to dispatch token-volume + new-token events
- Build `volumes.db` schema
- Run silent for a week, accumulate volume data

### Phase 1: token catalog
- Build `token_catalog.py` discovery scanner. Could either:
  - (a) Run a second multi-hour `ledger_data` scan filtered to
    `RippleState`, OR
  - (b) Derive initial catalog from AMM index (only catches tokens
    with an AMM, but those are the interesting ones anyway)
  - Recommended: start with (b) for speed-to-launch, add (a) later
    for completeness
- Persist as `token_index.json`
- Incremental updates from stream

### Phase 2: pricing + per-token TVL
- Implement `token_prices.py` rules
- Compute total TVL per token from trustline holdings (requires
  per-issuer trustline scan, doable via `account_lines` on the issuer)
- Display: top tokens by liquidity, sorted

### Phase 3: 24h volume + gainers/losers
- Requires Phase 0 to have accumulated ≥ 24h of data
- Compute 24h volume from `volumes.db`
- Compute 24h price change from `token_price_snap`
- Display: gainers / losers cards on homepage

### Phase 4: section page + detail pages
- Build `/tokens` with filters
- Build `/token/<id>` detail pages
- New tokens today sub-section

### Phase 5 (deferred): scam-risk indicator
- Heuristic candidates: issuer freeze events, single-holder
  concentration, age + trustline-count mismatch, no-AMM-but-many-
  trustlines pattern (often a pre-launch pump setup)
- Methodology must be documented and falsifiable before we ship it.
  False-positive on a legit token = legal risk + brand risk

## Risks

- **Token name spoofing.** Issuers can pick any currency code. RLUSD
  is "524C555344" — but anyone can issue currency code "RLUSD" with
  a different issuer. Mitigation: always show issuer alongside
  display name; never let display name stand alone. Curated mapping
  for the obvious-known cases (real RLUSD, real USDC).
- **Volume gaming.** Wash-trading inflates volume. Mitigation: at
  launch, just show what we measured (honest). Later phase: add
  "filtered volume" excluding obvious wash patterns.
- **Pricing distortion from thin AMMs.** A pool with $50 in it gives
  a "price" that's basically meaningless. Mitigation: don't price
  tokens whose source AMM has < $5k TVL. Show price column blank
  with a footnote.
- **Issuer flag changes.** An issuer can enable freeze, clawback,
  or default_ripple after listing. Mitigation: re-check issuer
  flags on a daily cycle; surface changes prominently.

## Resolved decisions (from initial review)

5. **Active tokens only at launch, add all later.** Catalog discovery
   is comprehensive (every token on the ledger), but the displayed
   list is filtered to tokens with measurable activity (24h volume
   > 0, or AMM TVL > $X). A future "show all" toggle exposes the
   long-tail catalog for users who want it.
6. **Currency hex → display name via community PR.** Same model as
   named accounts: a curated `token_names.json` in the repo,
   contributions via PR with verifiable source. Ledger gives the
   hex; we ship the human display name.
7. **Price denomination: XRP primary, USD secondary.** Display
   format: `0.51 XRP ($0.73)`. XRP is the native, on-chain truth;
   USD is the familiar approximation (depends on a hardcoded
   XRP→USD rate so will be slightly stale). Future v2: user toggle
   for USD-primary, plus live XRP→USD price feed.
