# Option B — Milestone 2 Relay Diff (PR-shaped, for Charlie's review)

**Status: DRAFT for review. NO Lenovo touch. Needs Charlie's explicit go
before any deploy.** This is the diff-in-hand the Milestone-2 rule requires.

Author: JJ · 2026-09-25
Depends on: Milestone 1 (`relay_feed_filters.py`, committed 92b0bc6, 22 tests
green) and the §11 answers written into `OPTION_B_RELAY_PROPOSAL.md` §11.

Scope of this PR: turn the subscribe-only-`ledger` relay into the Option-B
named-feed relay, in **log-only mode for 72h**, wiring in the already-tested
Milestone-1 filters. Pages move to named feeds as soon as feeds are live;
`enforce` mode only changes misuse handling (§11.5).

---

## Files touched (all in the Lenovo checkout at deploy time; shown here for review)

1. `live_stream_relay.py` — the relay (main change)
2. `relay_feed_filters.py` — already exists (Milestone 1); imported as-is
3. `wss_relay_canary.py` — extended per §9
4. `templates/{pools,whales,tokens,wallet}.html` — swap subscribe payloads
   (SEPARATE follow-up commit, after feeds are live + verified; NOT in this PR)

This PR is **relay + canary only**. Browser template swaps land after the
relay is live and the canary is green, so a relay bug can't dark a page.

---

## 1. `live_stream_relay.py` — diff outline

### 1a. New env (all default-safe; log_only is the shipping default)

```python
RELAY_MODE = os.environ.get("LIVE_STREAM_RELAY_MODE", "log_only")   # log_only | enforce
SLOW_CONSUMER_CAP = int(os.environ.get("LIVE_STREAM_RELAY_SLOW_CAP", "256"))
PER_CLIENT_ADDR_CAP = int(os.environ.get("LIVE_STREAM_RELAY_PER_CLIENT_ADDR_CAP", "1"))
AGG_WALLET_ADDR_CAP = int(os.environ.get("LIVE_STREAM_RELAY_AGG_ADDR_CAP", "1000"))
ADDR_CHANGE_PER_MIN = int(os.environ.get("LIVE_STREAM_RELAY_ADDR_CHANGE_PER_MIN", "5"))
FEED_META_REFRESH_S = int(os.environ.get("LIVE_STREAM_RELAY_FEED_META_REFRESH_S", "60"))
```

### 1b. Upstream sub: `ledger` → `transactions` + `ledger`

```diff
 await ws.send(json.dumps({
     "id": "relay_subscribe",
     "command": "subscribe",
-    "streams": ["ledger"],
+    "streams": ["transactions", "ledger"],
 }))
```

And in the read loop, route transactions through the filters:

```diff
 async for raw in ws:
     data = json.loads(raw)  # (existing guard kept)
     if data.get("type") == "ledgerClosed":
         state.last_ledger = data
         state.last_ledger_at = time.time()
-        await _broadcast(state, raw)
+        await _broadcast_feed(state, "ledger", raw)
+        # supply_updates: decorate every ledger close with total_coins (§11.4)
+        await _broadcast_supply_update(state, data)
+    elif data.get("type") == "transaction":
+        feeds = relay_feed_filters.classify(data, state.refs)
+        for feed in feeds:
+            await _broadcast_feed(state, feed, raw)
+        # wallet feed is per-subscriber (address sets), handled separately:
+        await _broadcast_wallet(state, data, raw)
     elif (data.get("type") == "response" ...):  # unchanged
```

### 1c. `is_valid_subscribe` — accept named feeds + validate wallet addresses

```python
FEED_NAMES = {
    "ledger", "amm_transactions", "token_top100_transactions",
    "whale_transactions", "wallet_transactions", "supply_updates",
}

def is_valid_subscribe(data):
    if not isinstance(data, dict) or data.get("command") != "subscribe":
        return False
    # Legacy form: streams:['ledger'] (kept for back-compat during rollout)
    streams = data.get("streams")
    if isinstance(streams, list) and streams:
        return all(s == "ledger" for s in streams)
    # New form: stream:<feed_name>
    feed = data.get("stream")
    if feed not in FEED_NAMES:
        return False
    if feed == "wallet_transactions":
        accts = data.get("accounts")
        if not isinstance(accts, list) or not (1 <= len(accts) <= PER_CLIENT_ADDR_CAP):
            return False
        # §11.1: validate well-formed classic address BEFORE accepting
        if not all(relay_feed_filters.is_valid_classic_address(a) for a in accts):
            return False
    return True
```

### 1d. Per-feed subscriber sets + per-client bounded queue

Replace the single `state.clients` broadcast with per-feed sets and a
per-client `asyncio.Queue(maxsize=SLOW_CONSUMER_CAP)`:

```python
class Client:
    def __init__(self, ws):
        self.ws = ws
        self.queue = asyncio.Queue(maxsize=SLOW_CONSUMER_CAP)
        self.feeds = set()              # feed names subscribed
        self.addresses = set()         # for wallet_transactions
        self.addr_changes = []         # timestamps, for ~5/min cap (§11.1)
        self.slow_drops = 0            # per-client (§11.2 logging)

# state.feeds: dict[str, set[Client]]  (ref-counted; feed populated iff subscribers)
# state.wallet_watch: dict[str, set[Client]]  address -> clients
```

`_broadcast_feed(state, feed, raw)` enqueues to each subscriber's queue,
**drop-oldest** on full (§11.2), bumping `client.slow_drops` and
`state.slow_consumer_drops`. A per-client send-loop task drains the queue with
`asyncio.wait_for(..., SEND_TIMEOUT_S)`; two consecutive timeouts → close 1011.

### 1e. Wallet feed (§3d / §11.1)

```python
async def _broadcast_wallet(state, data, raw):
    if not state.wallet_watch:
        return
    for addr, clients in state.wallet_watch.items():
        if relay_feed_filters.matches_wallet(data, {addr}):
            for c in clients:
                _enqueue_drop_oldest(state, c, raw)
```

Address-change rate cap (§11.1, ~5/min): on a `wallet_transactions`
re-subscribe, prune `client.addr_changes` to the last 60s; if
`len > ADDR_CHANGE_PER_MIN` → in enforce mode close 1008
`reason='addr_change_rate'`; in log_only mode log `SHADOW_TRIP addr_change_rate`
and allow.

### 1f. `supply_updates` (§11.4 — every ledger close)

Relay reads last-known `total_coins` from a tiny module-level ref refreshed by
a 60s asyncio task (reads the supply walker's PG row / cache). On every
`ledgerClosed`, emit a decorated copy:

```python
async def _broadcast_supply_update(state, ledger_data):
    if not state.feeds.get("supply_updates"):
        return
    msg = dict(ledger_data)
    msg["type"] = "supply_update"
    msg["total_coins"] = state.refs.get("total_coins")
    await _broadcast_feed(state, "supply_updates", json.dumps(msg))
```

### 1g. Feed-metadata refresh task (60s)

Refreshes `state.refs`: `pool_accounts` (from `amm_pools_metadata`),
`top100` (from `tokens_top100_24h_volume`), `whale_tier_drops` (env/page
constant), `total_coins` (supply walker). Reads the same PG rows the browser
primed from — no HTTP hop.

### 1h. log_only vs enforce (§11.5)

- **log_only (72h default):** every violation (queue drop, addr cap, addr
  change rate, send timeout) is **logged as `SHADOW_TRIP <kind>`** and the
  action is otherwise allowed (drops still drop-oldest — that's mechanical,
  not policy; the SHADOW line records it). Pages get their feeds NOW.
- **enforce (after Charlie's go):** address-cap / addr-change-rate /
  send-timeout closes are enforced. Client-side stopgaps removed after 48h.

### 1i. `/healthz` new fields

`feeds` (per-feed subscriber counts), `slow_consumer_drops`,
`wallet_addr_count`, `relay_mode`, `total_coins_age_s`.

---

## 2. `wss_relay_canary.py` — extension (§9, with §11.3 answer)

Sequential per-run checks (cadence unchanged 900s; budgets under one cadence):

1. `subscribe streams:['ledger']` — wait ≤10s for one `ledgerClosed` (as today).
2. `subscribe stream:'amm_transactions'` — clean handshake; ≤30s for one tx OR ok.
3. `subscribe stream:'token_top100_transactions'` — same.
4. `subscribe stream:'whale_transactions'` — clean handshake only (silence ok).
5. `subscribe stream:'wallet_transactions', accounts:['rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De']`
   — the **RLUSD issuer** (§11.3, ~240 tx/min); wait ≤30s for one tx. **No
   synthetic account, no self-generated tx** — we passively observe real
   RLUSD-issuer traffic.
6. `subscribe stream:'supply_updates'` — wait ≤30s for one decorated
   `supply_update` carrying `total_coins`.

Any sub-check fail → `findings_count > 0`. `last_run_message` becomes a
per-feed OK/FAIL JSON dict so L1 sees which feed broke.

---

## 3. Deploy window (§8 — for the eventual go, NOT now)

1. `git push` to Lenovo checkout (existing workflow).
2. Set `LIVE_STREAM_RELAY_MODE=log_only` in the systemd env (drop-in override;
   tracked read-only in `~/xrpl_test_private_infra/deploy/lenovo/` per §11.6).
3. `systemctl restart xrpld-live-stream-relay` (~1s upstream blip; the existing
   reconnect loop + browser fallback cover it). NOTE: the live unit has no
   reload handler; restart is the safe path.
4. Verify `/healthz`: `upstream_connected=true`, `relay_mode=log_only`,
   `feeds:{...}` present.
5. Kickstart the extended `wss_relay_canary`; expect exit 0,
   `findings_count=0`.
6. **72h log-only clock starts.** Charlie reviews `SHADOW_TRIP` line patterns
   before the enforce flip.
7. Only after 72h + Charlie's go: `LIVE_STREAM_RELAY_MODE=enforce`, restart.

**Rollback:** revert binary + `systemctl restart`. Browser client-side
stopgaps remain in place, so pages regress cleanly to today's baseline.

---

## 4. What is NOT in this PR

- Browser template subscribe-payload swaps (separate commit after feeds live +
  canary green).
- Removing client-side stopgaps (after 48h stable enforce).
- Copy fixes (about.html:187, methodology.html:239/249, app.py:386) — land
  with the stopgap-removal commit.
- Signed live-stream events (separate proposal, post-stabilization).

---

## 5. Review checklist for Charlie

- [ ] Upstream sub change (`ledger` → `transactions`+`ledger`) acceptable on
      the Lenovo rippled's load? (10–20 tx/s sustained; one sub.)
- [ ] log_only default + 72h clock correct.
- [ ] Canary using RLUSD issuer (no synthetic) as specified.
- [ ] Deploy sequence + rollback acceptable.
- [ ] Give the go for Milestone 2 deploy (or request changes to this diff).

*No code changed on Lenovo. This document is the diff-in-hand for the
Milestone-2 go decision.*
