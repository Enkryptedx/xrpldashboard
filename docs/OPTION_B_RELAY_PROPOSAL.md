# OPTION_B_RELAY_PROPOSAL.md

**Report-only draft.** No code changes, no Lenovo touch until Charlie signs.

Author: JJ · Date: 2026-09-24 (afternoon slot before 21:00 ET leaf)
Ruling references:
- Charlie 2026-09-23 evening: chose Option B (relay subscribes once upstream to
  `transactions`, exposes named feeds with server-side filtering + per-subscriber
  ref-count; filters must be a straight port of the current browser-side logic).
- Charlie 2026-09-24 afternoon: proposal doc must cover named feeds, filters
  including `metadata.AffectedNodes`, one upstream sub, per-feed fanout with
  ref-count, slow-consumer queue cap, upstream reconnect, deploy window note,
  log-only 24h, canary changes, `/wallet` section with a recommendation,
  and a config-diff outline.

---

## 1. Problem statement (why Option B exists)

Today's relay (`~/xrpl_test/live_stream_relay.py` on Lenovo, exposed as
`wss://wss.xrpldashboard.com`) accepts **only** `streams:['ledger']` and closes
every `streams:['transactions']` subscribe with `close code=1008 reason='subscribe_only'`.

Four pages need a **transactions** live feed:
- `/pools` — pool events (Payment involving an AMM account; AMMDeposit/AMMWithdraw)
- `/whales` — Payment above threshold XRP delivered
- `/tokens` — issued-currency Payment on the top-100 by 24h volume list
- `/wallet` — any tx touching a visitor-supplied XRPL account

Current stopgap (commits `2f3e9a6`, `6a5c153` for /pools + /whales): browser
tries relay → 3× 1008 → falls back to `wss://xrplcluster.com`. `/tokens` has no
stopgap yet; `/wallet` never tried the relay (`templates/wallet.html:2660`:
`var WS_URL = 'wss://xrplcluster.com'`). Sovereignty regressed on all four.

Option A (naïve): let the relay pass through `streams:['transactions']` and
have every browser get every tx. Rejected: firehose to every page (10-20 tx/s
today; higher on volatile days); redundant per-client filtering (browser CPU +
bandwidth waste); no accounting; abuse-friendly.

**Option B**: relay opens **one** upstream `streams:['transactions','ledger']`
sub to `ws://127.0.0.1:6006` (local rippled), splits it into **named feeds**
server-side, per-subscriber ref-counted, per-connection bounded queue.

---

## 2. Named feeds (one server-side definition per public number)

Every browser-side filter port must produce the SAME set of events the page
currently accepts. Any drift breaks the "one definition per public number" rule.

| Feed name | Upstream data | Filter (server-side) | Refresh |
|---|---|---|---|
| `ledger` | `type=ledgerClosed` | none (whole ledger stream) | every closed ledger |
| `amm_transactions` | `type=transaction` | Payment/AMMDeposit/AMMWithdraw AND either `Account`/`Destination` OR any `meta.AffectedNodes[*].{ModifiedNode,CreatedNode,DeletedNode}.FinalFields.Account` matches an AMM account in the tracked set (see §3a) | live |
| `token_top100_transactions` | `type=transaction` | Payment with `DeliverMax.currency` or `Amount.currency` in the top-100 currency set (see §3b) | live |
| `whale_transactions` | `type=transaction` | Payment where derived XRP delta from `meta.AffectedNodes` on the destination's `AccountRoot` ≥ `WHALE_XRP_THRESHOLD` (see §3c) | live |
| `wallet_transactions` | `type=transaction` | any tx where any `meta.AffectedNodes[*].*` `FinalFields.Account` matches an address in this SUBSCRIBER's address-set OR `Account`/`Destination` matches (see §3d) | live |
| `supply_updates` (NEW, from daily 09-23 line 718) | `type=ledgerClosed` | none; relay decorates with `total_coins` (from supply walker) | every closed ledger, ≤60s |

`supply_updates` is a decorated `ledgerClosed` — no extra upstream cost; it
reads the last-known `total_coins` + `escrow_total` from a small per-second
walker (step 3 of today's afternoon plan). Ties directly to §5 supply block.

---

## 3. Filter details — straight port of current browser code

Every filter below is a byte-for-byte port of what the page's JS currently
computes on each incoming tx. Same PG rows, same field paths in the tx.

### 3a. `amm_transactions` filter (from `templates/pools.html:1053-1180`)

Source of truth for "which accounts are AMM pools":
- **PG cache**: `SELECT amm_account FROM amm_pools_metadata` (already served by
  `/api/live/amm_accounts.json` for the browser to prime its set — the relay
  reads the same rows directly, no HTTP hop).
- Refresh on the relay side: re-query every 60s (matches the browser's own
  re-fetch cadence). No hot-reload triggers on `rank_amms.py` writes.

Match rule (verbatim from `pools.html:1149-1178`):
1. If `tx.TransactionType` in `{Payment, AMMDeposit, AMMWithdraw}` AND
   (`tx.Account` in `amm_accounts` OR `tx.Destination` in `amm_accounts`)
   → include.
2. Else if `Array.isArray(meta.AffectedNodes)` — for each node, check
   `{ModifiedNode,CreatedNode,DeletedNode}.LedgerEntryType == 'AccountRoot'` and
   `FinalFields.Account` in `amm_accounts` → include.
3. Else → exclude.

Directionless pulse only (per pools.html:844 comment: "Swap = directionless
pulse only. Without parsing AffectedNodes"). Relay must emit both matches with
the FULL raw tx envelope so the browser can render its own animation state
without a second fetch.

### 3b. `token_top100_transactions` filter (from `templates/tokens.html:2354-2397`)

Source of truth: `SELECT currency, issuer FROM tokens_top100_24h_volume` —
same view the /tokens table renders from. Refreshed by `enrich_token_names`
+ `token_icon_walker`; relay re-reads every 60s.

Match rule:
1. `tx.TransactionType == 'Payment'`.
2. `tx.DeliverMax` (or legacy `tx.Amount`) is an object (issued currency, not
   XRP drops as a string).
3. That object's `{currency, issuer}` tuple is in the top-100 set.

### 3c. `whale_transactions` filter (from `templates/whales.html:955-1050`)

`WHALE_XRP_THRESHOLD` = the same value already jinja-injected into the page
(`whale_xrp_threshold`, currently visible in the browser's meta_description).

Match rule (verbatim from whales.html handler):
1. `tx.TransactionType == 'Payment'`.
2. Compute XRP delta on `AccountRoot` for `tx.Destination` from
   `meta.AffectedNodes` (`FinalFields.Balance - PreviousFields.Balance`, in
   drops → XRP).
3. If `xrp_delta >= WHALE_XRP_THRESHOLD` → include.

Named-account decoration (which the /whales page currently does client-side
via `/api/accounts/known` fetches) can stay client-side to keep the relay
stateless-per-tx; the raw tx envelope has enough for the browser to look up
the name.

### 3d. `wallet_transactions` filter (proposed; see §7 for the decision)

Per-subscriber address set (typically size 1: the address in the URL path).
Subscribe message shape:
```json
{"id":"wallet-sub","command":"subscribe","stream":"wallet_transactions","accounts":["rXXXX..."]}
```
Cap: **≤ 1 address per subscriber** (matches the current `/wallet/<addr>`
page's single-account scope; anything larger looks like a probe).
Aggregate cap: **≤ 1000 distinct addresses under watch across all connected
clients**; excess subscribes get `close code=1008 reason='address_cap'`.

Match rule per tx: `tx.Account` or `tx.Destination` in the address set OR any
`meta.AffectedNodes[*].{ModifiedNode,CreatedNode,DeletedNode}.FinalFields.Account`
in the address set. (Same set of possible matches xrplcluster.com's per-account
sub gives today.)

---

## 4. One upstream sub — architecture

```
                    ┌─────────────────────────────────────────────┐
                    │  live_stream_relay (Lenovo, systemd)        │
                    │                                             │
  ws://127.0.0.1:   │  UpstreamLoop (single)                      │
       6006 rippled ├─►streams:['transactions','ledger']──────────┤
                    │            │                                │
                    │            ▼                                │
                    │  Router: classify each tx into 0..N feeds   │
                    │  (per §3 filter table; O(k) where k is the  │
                    │   number of matching feeds — small).        │
                    │            │                                │
                    │            ▼                                │
                    │  Per-feed subscriber set (ref-counted):     │
                    │  amm_transactions:    {ws_a, ws_c}          │
                    │  token_top100_...:    {ws_b, ws_c}          │
                    │  whale_transactions:  {ws_d}                │
                    │  wallet_transactions: {ws_e→{r1}, ws_f→{r2}}│
                    │  ledger:              {ws_a, ws_b, ...}     │
                    │  supply_updates:      {ws_g}                │
                    │            │                                │
                    │            ▼                                │
                    │  Per-client bounded asyncio.Queue           │
                    │  (max 256 messages; oldest-dropped on cap;  │
                    │   slow_consumer_drops counter++)            │
                    │            │                                │
                    │            ▼                                │
                    │  Send-loop task per client, timeout 1.0s    │
                    │  → close 1011 on repeated timeout           │
                    └─────────────────────────────────────────────┘
```

- **One upstream sub** — combined `transactions + ledger` streams.
  Removes today's split (relay currently only subs to `ledger`).
- **Router** — pure Python dict lookups on per-feed subscriber sets. No
  regex, no JSON re-parse (raw `data` is already a dict by the time it hits
  the router).
- **Ref-count** — feed is populated only if `len(subscribers) > 0`; when the
  last subscriber drops, the feed's set becomes empty but the subscription
  data structures remain (constant memory). Zero upstream cost for unused
  feeds today (we don't re-subscribe upstream per feed — filtering is client-
  side to the relay).
- **Fair fanout** — per-client `asyncio.Queue` decouples fast senders from
  slow receivers. A slow client cannot block the relay's read of upstream
  messages.

---

## 5. Slow-consumer queue cap + upstream reconnect

### Slow-consumer policy (per-client)
- Bounded `asyncio.Queue(maxsize=SLOW_CONSUMER_CAP)`, default `256`.
- Enqueue is non-blocking; on full-queue: drop the OLDEST message, bump
  `state.slow_consumer_drops`. Alternative "disconnect on 3 consecutive
  drops" is available as a plist-configurable knob; default is drop-oldest
  because a bursty ledger close should not disconnect an otherwise-healthy
  browser.
- Send-loop uses `asyncio.wait_for(client.send(msg), timeout=SEND_TIMEOUT_S)`
  (existing pattern). Two consecutive send timeouts → `close code=1011
  reason='send_timeout'`.
- Counters exposed on `/healthz`: `slow_consumer_drops`, `disconnects_send_timeout`.

### Upstream reconnect (already implemented in `upstream_loop`)
- Exponential backoff base 2.0s → cap 60.0s; reset on successful connect.
- On disconnect: mark all feeds as "upstream_down" via a synthetic message
  to every subscriber `{"type":"relay_status","upstream_connected":false}`,
  so browsers can render a spinner without waiting for their own reconnect
  logic to kick in.
- On reconnect: re-subscribe `['transactions','ledger']` and emit
  `{"type":"relay_status","upstream_connected":true}`.
- ONE new element: after an upstream reconnect, `supply_updates` feed
  needs a fresh `total_coins` read from PG before decorating the next
  `ledgerClosed` (see §7 supply-block integration).

---

## 6. Log-only 24h, then enforce (rollout gate)

**Phase 1 (log-only, 24h from deploy)**
- Relay accepts every proposed subscribe (existing `subscribe:['ledger']`
  PLUS the six new named feeds).
- Slow-consumer + address-cap violations are logged as `SHADOW_TRIP` lines
  in `~/xrpl_test/launchd_logs/live_stream_relay.out.log` (or systemd
  journal on Lenovo — TBD; see §10 deploy window). No enforcement.
- Metrics (queue depth p99, `slow_consumer_drops`, `disconnects_send_timeout`,
  aggregate distinct addresses under `wallet_transactions`) exposed on
  `/healthz`. Poll from the Mac via existing wss_relay_canary.
- Canary hooks: extended per §9 to send one subscribe per named feed and
  verify at least one event arrives within 30s (except `whale_transactions`
  which may legitimately be silent).
- Guard: **request_path_filter_log_only_first rule (Tier 0)** — every new
  filter ships log-only for 24h first. Applies here.

**Phase 2 (enforce, after Charlie's go)**
- Flip `RELAY_MODE=enforce` in the systemd env.
- Address-cap enforced; queue cap enforced; send-timeout disconnect enforced.
- Client-side stopgaps (`2f3e9a6`, `6a5c153`) can be REMOVED after 48h of
  stable enforce.
- Copy fixes (about.html:187, methodology.html:239/249, app.py:386 leak)
  can land in the same commit that removes the stopgaps.

---

## 7. `/wallet` section — recommendation

**Two options in play**:

**W-A (per-address `account` upstream sub, per subscriber)**
- Relay opens a NEW upstream `subscribe:{accounts:[rX]}` for every distinct
  address a subscriber requests. Ref-counted: one upstream sub per address
  regardless of how many browsers watch it.
- Pros: matches xrplcluster's per-account behavior exactly. Zero filter cost
  on the firehose.
- Cons: N upstream subs on rippled (own node handles it fine; but abuse
  potential — an attacker with many tabs can force many upstream subs).
  More complex teardown logic. Extra rippled request path.

**W-B (single-firehose filter, no extra upstream sub) — RECOMMENDED**
- Relay uses the SAME upstream `transactions` stream that already feeds
  amm/token/whale/wallet, filters per-subscriber by address set (see §3d).
- Pros: **zero additional upstream cost** — the transactions firehose is
  already open. Filter cost per tx: O(k) where k is the total distinct
  addresses under watch across all clients (dict lookup on a set). At the
  10-20 tx/s XRPL sustained rate this is ~microseconds per tx.
  Address-cap (`AGGREGATE_WALLET_ADDR_CAP=1000`, `PER_CLIENT_ADDR_CAP=1`)
  bounds attack surface: 1000 distinct addresses × 1 lookup per tx =
  20,000 lookups/s worst case, trivial.
- Cons: relay does per-tx filtering (not "free" like an upstream account sub),
  but the cost is bounded by the caps above.

**W-C (stay on public cluster, label honestly) — fallback if W-B fails review**
- `templates/wallet.html:2660` stays hardcoded to `wss://xrplcluster.com`.
- Update `about.html:187` + `methodology.html:249` to say "/wallet is on
  the public xrplcluster.com by design — per-address subscription pattern
  we don't yet handle sovereignly." Concedes /wallet sovereignty.
- Pros: no relay work for /wallet; no address-cap policy to defend.
- Cons: sovereignty claim now has an explicit hole; four-page equal-treatment
  argument breaks.

**JJ recommends W-B**. It preserves the "primary sovereignty for all four
surfaces" claim uniformly, is a natural extension of the same filter table,
adds no upstream cost, and the caps make abuse economically pointless.
W-A is over-built for a one-address page; W-C is a copy-first fallback if
the abuse concern outweighs the sovereignty benefit — but that's a policy
call for Charlie, not a design constraint.

---

## 8. Deploy window note

**No Lenovo touch today** (Charlie ruling 2026-09-24). Ship-target: after
Charlie signs this doc, in the next window that opens.

Deploy sequence (proposed, for the eventual window):
1. `git push` to Lenovo checkout (per its existing workflow).
2. `systemctl reload xrpld-live-stream-relay` OR `restart` if the reload
   handler isn't yet added (the current unit likely doesn't handle SIGHUP;
   the safe path is `restart`, which is a ~1s upstream disconnect that
   the existing reconnect loop handles).
3. Verify `/healthz` returns `upstream_connected=true` and the new fields
   `slow_consumer_drops:0`, `feeds:{...}`.
4. Kickstart-prove: `wss_relay_canary` runs the extended handshakes
   (per §9). Exit 0 expected; walker_health row `wss_relay_canary` has
   `findings_count=0`.
5. Log-only 24h clock starts; Charlie sees the shadow log line pattern
   before the flip.

**Rollback**: revert to prior binary + `systemctl restart`. Client-side
stopgaps still in place, so browsers regress cleanly to the today-baseline.

---

## 9. Canary changes (`~/xrpl_test/wss_relay_canary.py`)

Current canary: opens `wss.xrpldashboard.com`, sends `subscribe:['ledger']`,
waits ≤10s for one `ledgerClosed`, stamps `_last_ok`, writes walker_health.
Cadence 900s.

Extensions for Option B:
- Add a per-feed subscribe verification, sequentially per canary run:
  1. `subscribe:['ledger']` — as today.
  2. `subscribe stream:'amm_transactions'` — wait ≤30s for one tx OR clean
     handshake response.
  3. `subscribe stream:'token_top100_transactions'` — same.
  4. `subscribe stream:'whale_transactions'` — clean handshake response only
     (silence within 30s is fine; whales don't happen every 30s).
  5. `subscribe stream:'wallet_transactions', accounts:['rXRP_LOOKUP_KEY']`
     — a synthetic canary address that we tag on Lenovo with a heartbeat
     tx every 10 minutes (see §11 open questions). Wait ≤11min for one tx.
  6. `subscribe stream:'supply_updates'` — wait ≤30s for one decorated
     `ledgerClosed` with a `total_coins` field.
- Failure of ANY sub-check fails the canary (findings_count > 0).
- `walker_health.last_run_message` becomes a JSON dict per-feed OK/FAIL so
  L1 can see which feed is broken without reading logs.
- Cadence unchanged (900s); wait budgets stay under one cadence.

---

## 10. Config diff outline

`~/xrpl_test/live_stream_relay.py`:
- `is_valid_subscribe(data)` — accept `command:'subscribe'` with either
  `streams:['ledger']` (unchanged) OR `stream:<feed_name>` where
  `feed_name` in the whitelist AMM_TXS_FEED_NAME_SET.
- Add `class Feed`: name, subscribers (set of Client), optional per-
  subscriber filter payload (address set for `wallet_transactions`).
- Add `class Client`: websocket ref, per-client bounded queue, address
  set, current feeds subscribed, `slow_consumer_drops`, `disconnects` etc.
- `upstream_loop` — subscribe `streams:['transactions','ledger']` instead
  of just `['ledger']`. Route each `type=transaction` through `Router.classify`.
- `Router.classify(tx) -> list[str]` — returns feed names this tx matches.
  Reads amm-account set, top100 set, whale threshold from module-level
  refs refreshed on a 60s asyncio task.
- `_broadcast(state, raw)` replaced by `_broadcast_to_feed(feed_name, raw)`.
- `_healthz_line` — new fields `feeds`, `slow_consumer_drops`,
  `wallet_addr_count`.
- New env vars (defaults in code):
  - `SLOW_CONSUMER_CAP` (default 256)
  - `RELAY_MODE` (default `log_only` for phase 1, `enforce` for phase 2)
  - `PER_CLIENT_ADDR_CAP` (default 1)
  - `AGGREGATE_WALLET_ADDR_CAP` (default 1000)
  - `FEED_METADATA_REFRESH_SECONDS` (default 60)

`~/xrpl_test_private_infra/launchd/…live_stream_relay…`: N/A (unit lives on
Lenovo, not tracked in this repo yet — see follow-up in
`archive/README.md`).

`~/xrpl_test/wss_relay_canary.py`: as §9.

`templates/pools.html`, `whales.html`, `tokens.html`, `wallet.html`:
- Swap browser-side subscribe payload to
  `{"command":"subscribe","stream":"amm_transactions"}` (etc.).
- Remove the browser-side filter loops that Option B has now server-sided.
  Kept as a shadow-check for the log-only 24h if easy.
- `/wallet` adds `accounts:[address]` to the subscribe payload.
- Copy accuracy edits (about.html:187, methodology.html:239/249, app.py:386
  docstring) land in the same PR.

---

## 11. Open questions for Charlie — ANSWERED (Charlie 2026-09-25 09:51 ET)

The doc is the source of truth; these are the binding decisions.

1. **`/wallet` — W-B or W-C?** → **W-B.** PLUS two hardening requirements:
   - Relay **validates the address as a well-formed XRPL classic address**
     (base58 `r...`, checksum-valid) BEFORE accepting the `wallet_transactions`
     sub. Malformed → reject the sub (`close 1008 reason='bad_address'`),
     do not add to any watch set.
   - **Cap address changes per socket to ~5/min.** A client that re-subscribes
     with new addresses faster than 5 changes/minute gets throttled
     (`close 1008 reason='addr_change_rate'` on exceed). This bounds
     churn-based probing on top of the existing PER_CLIENT_ADDR_CAP=1 and
     AGGREGATE_WALLET_ADDR_CAP=1000.
2. **Slow-consumer policy** → **drop-oldest** (default). Log per-client drop
   counts during the log-only phase. Propose disconnect-on-N-drops ONLY if the
   log-only data shows one client clustering the drops; otherwise stay
   drop-oldest. (So `SLOW_CONSUMER_POLICY` ships `drop_oldest`; a disconnect
   flip is a later, data-justified proposal, not shipped now.)
3. **Canary `wallet_transactions` target** → **No synthetic account, no new key.**
   Canary subscribes `wallet_transactions accounts=['rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De']`
   — the **RLUSD issuer account** (~240 tx/min, so a live event arrives well
   within the canary wait budget). **Zero self-generated transactions** — we
   never emit a heartbeat tx; we passively observe real RLUSD-issuer traffic.
   (Supersedes the §9.5 synthetic-address proposal.)
4. **`supply_updates` decoration cadence** → **Every ledger close.** (Matches
   the daily 09-23 line-718 "per ledger" phrasing; the `total_coins` read is
   cheap enough per close.)
5. **Log-only phase length** → **72h** log-only before enforce. Also: the doc
   states that **pages move to the named feeds as soon as the feeds are live**
   — the enforce flip only changes MISUSE handling (address-cap / queue-cap /
   send-timeout enforcement), NOT whether pages use the sovereign feeds. So
   sovereignty is restored at feed-live, not at enforce.
6. **Lenovo unit-file tracking** → **Track now, read-only, no Lenovo edits.**
   Copy the Lenovo `.service`/`.timer` files read-only into
   `~/xrpl_test_private_infra`, secret-scan by filename, commit, and extend
   the drift canary to cover them. **No edits to anything on Lenovo.**

## 11a. Milestone plan (Charlie 2026-09-25 09:51 ET)

- **Milestone 1 (this session, no Lenovo touch):** the §3a–§3d filters built
  as standalone functions + **unit-tested locally as byte-for-byte ports of
  the current browser filters**, against captured real tx envelopes. Report
  test results. This is the milestone item 9's build is gated behind.
- **Milestone 2 (separate go required):** relay wiring ON LENOVO — needs a
  separate Charlie go WITH THE DIFF IN HAND. Not started until then.

---

## 12. Non-goals

- Rewriting rippled's stream subscription semantics. All filtering is at
  the relay; rippled sees exactly the same upstream sub whether we have 1
  or 1000 browsers.
- Signed live-stream events. That's a separate proposal (§10 signed
  streaming lands after Option B stabilizes).
- Removing xrplcluster.com from the wallet page in the same PR. The
  copy-fix path and the code-fix path can decouple; do the code first, edit
  copy after enforce clock elapses.

---

*End of report. Awaiting Charlie's sign-off + answers to §11 before any
code change or Lenovo touch.*
