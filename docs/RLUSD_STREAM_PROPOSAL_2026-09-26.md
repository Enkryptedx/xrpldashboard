# /rlusd event log: stream the XRPL side via the relay — Proposal (queued 2026-09-26)

Status: **QUEUED — not built.** Charlie 2026-09-26: "Proposal for later: stream
the RLUSD issuer via the relay's wallet_transactions feed with the walker as
fallback." Minimum honesty fix shipped the same day (event log labeled as a
polled snapshot with its real cadence and last-refresh age; row times as
`data-ts` with client-side relative time; freshness chip anchored on the
server's `fetched_at`).

## Today

- `rlusd_refresher` walker (launchd `StartInterval` 300) calls
  `rlusd_live._refresh_cache_once()`: Ethereum `eth_getLogs` window +
  XRPL `account_tx` on the issuer `rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De`
  (80 txs), merges into one payload with `events[:120]`, `fetched_at`,
  `ttl_seconds`, writes `rlusd_state_cache` in Postgres.
- `/rlusd` SSR reads that row; the page polls `/api/rlusd/state` every 60 s.
  Net: an XRPL-side mint/burn/trade shows up between 0 and ~5 min late.

## Proposal

1. **Subscribe the page to the relay's `wallet_transactions` feed** with
   `accounts=[<RLUSD issuer>]` (the feed already exists on
   `wss.xrpldashboard.com`, Option B M2, proven live against the issuer on
   2026-09-26 by `wss_relay_canary --all-feeds` `wallet_transactions:OK`).
   Reuse the `/wallet` page's subscribe + reconnect + fallback client
   (`live_stream.js` PRIMARY → xrplcluster fallback with the
   `walker_node_fallback` ping), not a new socket stack.
2. **Map relay txs to the event log's shape** client-side with the same
   rules `rlusd_live` uses server-side (Payment from issuer = mint, Payment
   to issuer = burn, OfferCreate with an RLUSD side = trade; amount from the
   RLUSD leg). Keep the classifier in ONE place: extract
   `rlusd_live._classify_xrpl_tx()` and expose it as a tiny JSON rule table
   the client and the walker both read (no duplicated logic to drift).
3. **Walker stays as the fallback and the backfill**: on load the page
   seeds from the snapshot (as now); the stream adds rows in real time;
   every 60 s poll still re-anchors supply and 24 h counters (server-owned).
   If the relay socket is not proven live within the fallback budget, the
   page behaves exactly as today and the freshness label says "snapshot".
4. **Label truthfully per source**: rows that arrived via the stream carry a
   `stream` marker; the heading says "XRPL side live via our node · Ethereum
   side snapshot every 5 min". Ethereum stays polled (we run no Ethereum
   node; disclosed on the page already).
5. **Dedupe by tx hash** (the page already keeps `seenTx`); a streamed tx that
   later appears in the snapshot must not double-render or double-animate.

## Guardrails

- Log-only first (request_path_filter_log_only_first pattern): ship with the
  stream wired but rendering suppressed, log stream-vs-snapshot deltas for
  24 h, then enable.
- Relay is in LOG-ONLY until the Wed 2026-09-30 enforce review; adding a
  new page subscription before that only adds a subscriber, no relay change.
- Counts-only / privacy rules untouched: the issuer is a public account; no
  user addresses are subscribed.
- Tests: classifier rule table round-trips (same result client and server
  on the fixture txs); OPEN + CLOSED for the label per source.

## Size

Half a day: client subscribe + classifier extraction + label + tests. No
walker, schema or Lenovo change.
