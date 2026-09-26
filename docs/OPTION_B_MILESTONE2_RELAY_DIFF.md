# Option B — Milestone 2 Relay Diff (PR-shaped, for Charlie's review)

**Status: IMPLEMENTED on main 2026-09-26 (Sat AM ET) — relay + canary code
landed per this diff; NOT yet deployed to the Lenovo. Deploy still needs
Charlie's live terminal-time go (§6).** Implementation notes vs. this outline:
`total_coins` is read from the upstream node (`ledger` command on the same
loopback socket), not PG; pool/top-100 refs come from `amm_ranked_pools` /
`token_volume` (the tables that exist — `amm_pools_metadata` and
`tokens_top100_24h_volume` named below do not) when `DATABASE_URL` is set,
else `LIVE_STREAM_RELAY_REFS_FILE`, else empty with `refs_source=none` in
healthz. The live unit's `IPAddressAllow=127.0.0.1` sandbox blocks Neon, so
PG-backed refs need the env file + an IPAddress drop-in at deploy time (§6 B.7).

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

- [x] Upstream sub change (`ledger` → `transactions`+`ledger`) acceptable on
      the Lenovo rippled's load? (10–20 tx/s sustained; one sub.) — GO (Charlie 2026-09-25)
- [x] log_only default + 72h clock correct. — GO
- [x] Canary using RLUSD issuer (no synthetic) as specified. — GO
- [x] Deploy sequence + rollback acceptable. — GO
- [x] **Milestone 2 GO** (Charlie 2026-09-25): deploy **Saturday morning ET**,
      **log-only**. Final "go" comes from Charlie at the terminal at deploy time;
      JJ executes over SSH, NOTHING before that. **Enforce flip not before
      Wednesday** — Tuesday is Batch activation; NOTHING changes on the relay
      Tuesday.

---

## 6. EXACT DEPLOY CHECKLIST (Saturday AM ET, log-only)

**Preconditions (do not start until ALL true):**
- It is Saturday morning ET, low traffic, clear of the 21:00 ET leaf.
- Charlie has given the live "go" at the terminal for THIS session.
- CI on `main` is green (the relay code + filters merged; last green run
  recorded before deploy).

All commands run on the Mac over `ssh rippled-node` unless marked LOCAL.
Host alias `rippled-node`; Lenovo checkout `/home/charlie/xrpldashboard`;
unit `xrpld-live-stream-relay`; relay port 6011, healthz 6012 (per the
tracked mirror deploy/lenovo/xrpld-live-stream-relay.service).

### A. PRE-CHECKS (read-only; abort if any fails)

1. **Relay canary green NOW (baseline):**
   ```
   ssh rippled-node "cd /home/charlie/xrpldashboard && ./.venv/bin/python wss_relay_canary.py --once"
   ```
   Expect exit 0. Also confirm walker_health `wss_relay_canary` findings_count=0
   (LOCAL: read via jj_query). If red, ABORT — don't deploy onto a broken baseline.

2. **Healthz baseline (upstream connected, current mode):**
   ```
   ssh rippled-node "curl -s http://127.0.0.1:6012/healthz"
   ```
   Record: `upstream_connected=true`, `last_ledger_index`, `clients`.

3. **Backup the two files that change (timestamped, on Lenovo):**
   ```
   ssh rippled-node "cd /home/charlie/xrpldashboard && cp live_stream_relay.py live_stream_relay.py.bak-$(date +%Y%m%d-%H%M%S)"
   ssh rippled-node "sudo cp /etc/systemd/system/xrpld-live-stream-relay.service /etc/systemd/system/xrpld-live-stream-relay.service.bak-$(date +%Y%m%d-%H%M%S)"
   ```
   (The unit file only changes if we add the RELAY_MODE env via a drop-in; if
   using a systemd drop-in instead of editing the unit, back up the drop-in dir.)

4. **Record current ledger index (reconnect proof baseline):**
   ```
   ssh rippled-node "curl -s http://127.0.0.1:6012/healthz" | grep -o '"last_ledger_index":[0-9]*'
   ```
   Note the value + wall-clock time.

5. **Record current git HEAD on Lenovo (rollback ref):**
   ```
   ssh rippled-node "cd /home/charlie/xrpldashboard && git rev-parse HEAD"
   ```

### B. DEPLOY

6. **Pull the merged relay code:**
   ```
   ssh rippled-node "cd /home/charlie/xrpldashboard && git fetch origin && git checkout main && git pull --ff-only"
   ```
   Confirm HEAD now includes the Option-B relay commit.

7. **Set log-only mode** (systemd drop-in, preferred — leaves the unit file clean):
   ```
   ssh rippled-node "sudo mkdir -p /etc/systemd/system/xrpld-live-stream-relay.service.d && printf '[Service]\nEnvironment=LIVE_STREAM_RELAY_MODE=log_only\n' | sudo tee /etc/systemd/system/xrpld-live-stream-relay.service.d/mode.conf && sudo systemctl daemon-reload"
   ```

8. **Restart the relay** (~1s upstream blip; reconnect loop + browser fallback cover it):
   ```
   ssh rippled-node "sudo systemctl restart xrpld-live-stream-relay"
   ```
   Note the restart wall-clock time (for the reconnect-within-seconds proof).

### C. POST-CHECKS (all must pass; else ROLLBACK per §D)

9. **Ledger stream reconnects within seconds:**
   ```
   ssh rippled-node "sleep 5; curl -s http://127.0.0.1:6012/healthz"
   ```
   Expect `upstream_connected=true` and `last_ledger_index` >= the §A.4 baseline
   (advanced or equal) within ~5–10s of restart. Record the gap.

10. **Healthz shows the new shape:** `relay_mode=log_only`, a `feeds` object
    present, `slow_consumer_drops=0`.

11. **All 6 feeds subscribe-success via canary:**
    ```
    ssh rippled-node "cd /home/charlie/xrpldashboard && ./.venv/bin/python wss_relay_canary.py --once --all-feeds"
    ```
    Expect per-feed OK for: ledger, amm_transactions, token_top100_transactions,
    whale_transactions (clean handshake; silence OK), wallet_transactions
    (accounts=[RLUSD issuer rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De], ≤30s for a tx),
    supply_updates (≤30s for a decorated ledgerClosed w/ total_coins). Exit 0.

12. **SHADOW_TRIP logging is live (log-only working):** trigger one benign
    violation (e.g. a wallet sub with 2 addresses > PER_CLIENT_ADDR_CAP) and
    confirm a `SHADOW_TRIP` line appears in the journal, request NOT rejected:
    ```
    ssh rippled-node "journalctl -u xrpld-live-stream-relay --since '2 min ago' | grep SHADOW_TRIP | tail"
    ```
    (If no natural trip in the window, this proves the log path exists; enforce
    is off so nothing closes.)

13. **No client-fallback flips on / and /amendments** (the sovereignty proof):
    LOCAL — watch the walker-node-fallback telemetry for browser_wss flips on
    those two routes for ~5 min post-deploy:
    ```
    (LOCAL) jj_query: SELECT * FROM walker_node_fallback WHERE source='browser_wss' AND created_at > now() - interval '10 min';
    ```
    Also load `/` and `/amendments` in a real browser, confirm the live pill
    stays on our node (no fallback to xrplcluster). Expect ZERO new browser_wss
    fallback rows for those routes.

### D. ROLLBACK (if any post-check fails)

14. Revert code + mode, restart:
    ```
    ssh rippled-node "cd /home/charlie/xrpldashboard && git checkout <§A.5 HEAD> && sudo rm -f /etc/systemd/system/xrpld-live-stream-relay.service.d/mode.conf && sudo systemctl daemon-reload && sudo systemctl restart xrpld-live-stream-relay"
    ```
    (Or restore the `.bak-*` files from §A.3.) Browsers were on client-side
    fallback throughout, so they regress cleanly to today's baseline. Confirm
    healthz `upstream_connected=true` and report the failing check.

### E. AFTER A CLEAN DEPLOY

15. **72h log-only clock starts** at the restart time (§B.8). Report the clock
    start + all post-check results to Charlie.
16. **Enforce flip: NOT before Wednesday** (Tuesday = Batch activation; relay
    untouched Tuesday). The flip is a separate action needing its own Charlie
    go after reviewing 72h of SHADOW_TRIP lines.
17. Update `deploy/lenovo/xrpld-live-stream-relay.service` mirror in
    `~/xrpl_test_private_infra` if the unit/drop-in changed (drift canary
    parity), read-only, committed.

*No code changed on Lenovo until Charlie's live terminal-time "go" Saturday AM.
This document is the diff + deploy checklist for that execution.*
