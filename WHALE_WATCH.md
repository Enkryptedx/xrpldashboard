# Whale Watch — architecture sketch

Step 4 of the roadmap. The differentiated panel: what no other XRPL
analytics product does well. Surfaces the "what did the big money do
today" view that turns the dashboard from a data table into a story.

## Locked-in decisions

These are settled. The rest of the doc reflects them.

1. **Display style: pure data, no editorial inference.** We show what
   happened, never what it means. Aligns with "trust > features."
   Editorial layer remains a future option, not a launch feature.
2. **Threshold philosophy: conservative.** Start at ~500k XRP for
   transfers, ~5M XRP balance minimum for trustline-from-large-holder
   events. Easy to loosen later; trust-damaging to tighten.
3. **Named accounts: hand-curated core + community PR submissions.**
   Seed ~50 accounts from public sources we own (Ripple's published
   wallets, exchange disclosures). After that, a `KNOWN_ACCOUNTS.md`
   in the repo invites PR submissions with verification sources. Every
   label has a human-reviewed PR trail. No dependency on Bithomp /
   XRPSCAN tag APIs.

## What "whale activity" actually means here

Three event categories, ranked by signal strength:

1. **Large XRP transfers.** Payment transactions where delivered amount
   crosses a threshold. Signal: cash flow between major holders /
   exchanges / custody.
2. **Notable account activity.** Any transaction from or to an account
   on our named-watchlist (Ripple wallets, exchange hot wallets, ETF
   custodians, known whale individuals). Signal: who did what.
3. **Trustline changes from large holders.** A TrustSet from an account
   above a XRP threshold is an early indicator of upcoming token
   interest — the whale is preparing to receive a token. Undersurfaced
   on every other XRPL dashboard.

Lower priority, add later if useful:
- AMM deposits/withdrawals above threshold (single-LP flexing)
- Large OfferCreate orders (DEX whale activity)
- Escrow events (especially Ripple's monthly billion)

## Components

```
+-------------------+   +-----------------+   +------------------+
| xrpl_stream.py    |-->| whale_filter.py |-->| events.db        |
| (ws subscribe to  |   | (threshold +    |   | (sqlite, 30d     |
|  all txns)        |   | watchlist match)|   | rolling)         |
+-------------------+   +-----------------+   +------------------+
                                                       |
                                                       v
                                              +------------------+
                                              | named_accounts   |
                                              | .json (curated   |
                                              | + bithomp tags)  |
                                              +------------------+
                                                       |
                                                       v
                                              +------------------+
                                              | /whales template |
                                              | + homepage card  |
                                              +------------------+
```

### `xrpl_stream.py` — single websocket, multiple handlers

One long-lived process, websocket connection to s2.ripple.com (the
public WebSocket node). Subscribe to the `transactions` stream.
Dispatch each incoming transaction to registered handlers:

- `whale_handler(tx)` — checks thresholds + watchlist, writes events
- `amm_create_handler(tx)` — adds new AMMs to the pool index (Step 2 incremental)
- `escrow_handler(tx)` — future: escrow release events

Reusing one stream for two features is critical: we don't want to
maintain two WebSocket connections, each with its own reconnect logic.

### `whale_filter.py` — the rules engine

Pure function that takes a transaction and returns either an Event
record or None. Defaults reflect the conservative-threshold lock:

**Two-tier whale threshold:**

- **Capture** (`xrpl_stream.py`): `WHALE_XRP_THRESHOLD_DROPS`, default 50K XRP.
  Wide capture into `events.db` — keeps enough history for retrospective
  filtering at higher thresholds. Overrideable via env
  `WHALE_XRP_THRESHOLD_XRP`.
- **Display** (`app.py`): `WHALE_XRP_THRESHOLD = 100K XRP`.
  Editorial display floor on `/whales` and homepage feed. Surfaces the
  top tier of captured events.

The display floor must be ≥ the capture floor (display can only show
what was captured). Display can be raised in the future without
losing historical data.

- `WHALE_USD_THRESHOLD` (env, default 750_000 USD-equivalent)
- `TRUSTSET_MIN_BALANCE_XRP` (env, default 5_000_000)
- `WATCHLIST_ALWAYS_INCLUDE` (any tx involving a watchlisted account
  passes regardless of amount)

### `events.db` — sqlite, 30d rolling

```
CREATE TABLE events (
  tx_hash       TEXT PRIMARY KEY,
  ledger_index  INTEGER NOT NULL,
  ts            INTEGER NOT NULL,        -- unix epoch seconds
  type          TEXT NOT NULL,           -- 'large_xfer'|'tagged'|'trustset'
  from_addr     TEXT,
  to_addr       TEXT,
  amount_drops  INTEGER,
  currency      TEXT,
  issuer        TEXT,
  raw_json      TEXT NOT NULL
);
CREATE INDEX events_ts_idx ON events(ts);
CREATE INDEX events_type_idx ON events(type);
```

Cron / scheduled task drops events older than 30 days.

Why sqlite, not JSON: events accumulate fast, "give me the last 24h
sorted by amount" is a SQL query, and we'd need queryable storage
anyway for Step 2.5 historical snapshots — might as well start the
habit here.

### `named_accounts.json` + `KNOWN_ACCOUNTS.md` — owned curation

The labels file lives in the repo, hand-curated, MIT-licensed, and
open to community submissions via GitHub PR. No runtime dependency on
Bithomp or XRPSCAN tag APIs.

```json
{
  "rNxp4h8apvRis6mJf9Sh8C6iRxfrDWN7AV": {
    "name": "Bitstamp hot wallet",
    "category": "exchange",
    "verified_via": "bitstamp_disclosure_2024_proof_of_reserves"
  },
  "rs8ZPbYqgecRcDzQpJYAMhSxSi5htsjnza": {
    "name": "Ripple Operations",
    "category": "ripple",
    "verified_via": "ripple_blog_post_url"
  }
}
```

**Bootstrap (~50 accounts) — sources we own:**
- Ripple's published wallet list (operations, escrow, treasury — all
  publicly documented on ripple.com / blog posts)
- Exchange disclosures (proof-of-reserves attestations, official
  support articles linking addresses)
- ETF custodian disclosures (BlackRock, Fidelity, Bitwise — published
  on-chain addresses once XRP ETFs launch)

**Ongoing curation — community PRs:**
- `KNOWN_ACCOUNTS.md` in the repo explains the contribution flow:
  submit a PR to `named_accounts.json` adding the address, label,
  category, and at least one verifiable source URL.
- Maintainer reviews each PR. Source must be checkable, not
  community hearsay.
- Pattern lifted from mempool.space's mining-pools list: open,
  auditable, every label has a human-reviewed PR trail.

**Naming policy:**
- Never publish a name we can't verify against at least one
  first-party source.
- "Unknown" is fine — better than wrong attribution.
- If a label is later disputed, remove it pending re-verification.
  Brand survives "we removed a label" much better than "we got it
  wrong."

## What gets shown where

### Homepage panel (top 5, headline only)
```
Whale activity — last 24h
+----------------------------------------------------+
| 2h ago · Ripple Ops → Bitstamp · 12.5M XRP         |
| 4h ago · Unknown → Unknown · 5.2M XRP              |
| 6h ago · BlackRock custody → Coinbase · 3.1M XRP   |
| 9h ago · Trustline · Unknown → new RLUSD trustline |
| 14h ago · Bitstamp hot → Unknown · 8.0M XRP        |
+----------------------------------------------------+
[ see all flagged events → /whales ]
```

Each row is pure data: who (named or "Unknown"), to whom (named or
"Unknown"), how much, when. No commentary. The reader interprets.

### `/whales` section page (full feed, filters)
- Last 24h / 7d / 30d toggle
- Filter by event type
- Filter by category (exchange / ripple / etf / unknown)
- Each event: pure data row, raw amount, txn hash linking to
  xrpscan, both account addresses linking to /account/<addr>
- Daily summary at top is **factual aggregates only**: "$X moved
  between tracked accounts in 24h, N trustline changes from large
  holders, biggest single transfer was Y" — no inference about what
  those numbers mean.

## Editorial inference — deferred, not at launch

Locked: launch with **pure data**, no inference layer.

The temptation is to render `Payment 12_500_000_000 drops from rs8Z...
to rNxp4h...` as "Ripple Operations sent 12.5M XRP to Bitstamp —
likely a customer withdrawal preparation." That's powerful, and it's
also where misattribution risk lives. Calling it "withdrawal
preparation" is an inference; if we're wrong, we've published an
incorrect read of the market.

The dashboard's voice is in the **selection** of what to show, not
the interpretation. Choosing thresholds, picking categories, designing
the layout — that's editorial enough at launch.

If we add inference later, it would be:
- Opt-in, behind a toggle ("show interpretation")
- Always hedged ("likely", "consistent with", never "is")
- Documented per-pattern somewhere visible to the user

## Build phases

### Phase 1: silent collection (no UI)
- Build `xrpl_stream.py` and `whale_filter.py`
- Run for a week, just collect events into `events.db`
- Tune the conservative threshold floor against real volume
- Build the bootstrap `named_accounts.json` (start with ~50 entries)
  + `KNOWN_ACCOUNTS.md` contribution guide

### Phase 2: pure-data feed page
- `/whales` shows the event list with filters (24h/7d/30d, type,
  category)
- Pure data display — no inference layer
- Get feedback from testers

### Phase 3: homepage card + polish
- Add the homepage card showing top 5
- Daily factual aggregates summary at top of `/whales`
- Polish, mobile layout, copy pass

### Phase 4: recurring-pattern recognition
- Identify recurring large addresses (heuristic: same address
  appears in N+ flagged events)
- For unnamed recurring whales: surface as anonymous addresses with
  observed behavior summaries ("this address has appeared in 23
  flagged transfers in the last 30 days")
- Track behavior over time. This is where the long-tail value lives.

### Phase 5 (deferred, optional): editorial inference toggle
Only if user feedback in Phases 2-3 shows demand. Ships with the
hedged-language and per-pattern documentation policy above.

## Risks

- **Wrong attribution.** Mislabeling an account as "Bitstamp" when
  it's actually a customer wallet is a brand-killer. Mitigation:
  conservative naming policy, link out for verification, never claim
  inferred intent as fact.
- **Stream disconnects.** WebSocket connections drop. Mitigation:
  reconnect with backoff, use ledger_index to detect gaps and
  backfill via account_tx for the gap period.
- **Threshold gaming.** If thresholds are public, whales can split
  transactions to stay under. Mitigation: tag any account that
  appears suspicious in our database whether or not the individual
  txn passes threshold.
- **Public node rate limits.** s2.ripple.com may throttle. Mitigation:
  if it becomes a problem, run our own rippled (real cost; defer
  until we hit the limit).

## Still-open question

**Trustline changes — dedicated section or fold into the unified
event feed?** Defer until Phase 1 collection runs for a week. If
trustline events drown out transfer events in volume, separate them.
If they're sparse, fold them in.

Everything else is locked. See "Locked-in decisions" at the top.
