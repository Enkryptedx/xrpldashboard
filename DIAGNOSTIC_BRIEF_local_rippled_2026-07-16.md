# DIAGNOSTIC BRIEF — Local rippled Node
**Written:** 2026-07-16 12:55 UTC (08:55 EDT)
**Node:** Mac mini (Mac16,10 / Apple M4 / 10 cores / 16 GB), rippled-3.1.3, PID 1339, launchd-managed
**Verdict:** SEVERELY DEGRADED — 23 min behind consensus, `ledger_current` returns `noCurrent`, effectively off-network for reads.

---

## 🚨 Surfacing per constraint: DB is broken beyond config

Your brief said: *"If you find something genuinely broken beyond config (disk failing, DB corruption), stop and surface immediately — that changes the conversation."*

**Finding: NuDB store has persistent missing state tree nodes.**
- **2,596** `SHAMapStore:ERR Missing node while copying ledger before rotate: Missing Node: State Tree: hash …` events in `debug.log`
- First: **2026-05-27 03:12 UTC** — Latest: **2026-07-14 21:37 UTC**
- Distinct hashes across the window (not one stuck ledger — genuine intermittent NuDB holes)
- This is on-disk corruption, not a config issue. **Config changes alone cannot recover.**

That changes the conversation as promised. The rest of this brief is written on that basis: config candidates are for the *post-resync* node, not to paper over the corruption.

---

## Phase 1 — Current state (live snapshot, 12:50 UTC)

**server_info:**
- state = `full` (misleading — node is not caught up)
- load_factor = 1 currently (transient — spikes recur; see Thread 1)
- **validated_ledger age = 1,376 s** (~23 min behind consensus)
- peers = 10
- **complete_ledgers = ~60 discontiguous fragments** (a healthy node reports one contiguous range — this is the outward visual of the failure)
- `ledger_current` → `{"error":"noCurrent","error_message":"Current ledger is unavailable."}` — node cannot answer the most basic RPC

**Hardware (Mac16,10 / M4):**
- 10 cores (4P + 6E), 16 GB unified memory
- Load avg 4.09 / 4.01 / 4.05
- Memory very tight: 81 MB free, ~8.5 GB in compressor, ~3.6 GB anonymous, ~1.6 GB wired
- Disk 294 GB free of 460 GB (not headroom-limited)
- rippled PID 1339: 64.1% CPU, 3.23 GB RSS

**rippled DB (`~/rippled-data/db/`):**
- `nudb` = **33 GB** (should be ~10–20 GB with `online_delete=10000` under normal rotation)
- `transaction.db` = 6.3 GB
- `ledger.db` = 15 MB

**Config (`~/.config/rippled/rippled.cfg`):**
- `node_size` = medium
- `ledger_history` = 10000
- `fetch_depth` = 10000
- `online_delete` = 10000
- `advisory_delete` = 0
- `node_db` type = NuDB
- `peers_max` = unset (defaults to 21 outbound)

**Log growth (management concern, not causal):**
- `debug.log` = 7.5 GB, `launchd-err.log` = 6.6 GB, no rotation since 2026-05-26

---

## Phase 2 — Symptom threads, root-caused with evidence

### Thread 1 — Sustained load_factor spikes
**Root cause:** Job queue backpressure. Node fights to stay in sync while SHAMapStore can't rotate.
**Shared root cause with:** Threads 2, 3 outward tell (all downstream of NuDB corruption).
**Evidence:**
- 179 `LoadManager:WRN Server stalled` events in last 6h; **22 in the last 10 min alone**
- Escalation pattern: `stalled 10s → 20s → 30s → 40s → 50s → 70s → recover → repeat` (multiple cycles/hr)
- Job wait offenders: ChkUntrust 91,813 ms, checkPropose 41,419 ms, ProcessLData 22,935 ms
- 295,043 `Validations:WRN Need validated ledger …` — core validation starved
- 26 `MISMATCH` + 25 `changeSpotPriceQuality failed` in last 2h → AMM consensus divergence (downstream of the stall, not a separate cause)

### Thread 2 — Ledger gap 105559592
**Root cause:** A slow-processing ledger on 2026-07-13 forced a 5-min consensus gap; backfill discovered a missing SHAMap node; this *seeded a new wave* in the existing corruption pattern (which had been running since May 27).
**Evidence (verbatim from debug.log):**
- `2026-07-13 01:27:42 TxQ:WRN Ledger 105559592 has 35 transactions. Ledgers are processing slowly. Expected transactions is currently 32 and multiplier is 128000`
- `2026-07-13 01:32:36 LedgerMaster:WRN Gap in validated ledger stream 105559592 - 105559693`
- `2026-07-13 08:28:38 LedgerMaster:WRN SQL DB ledger sequence 105559592 mismatches node store`
- `2026-07-13 08:28:38 NodeFamily:ERR Missing node in 105559592`

### Thread 3 — Reboot recovery
**Verdict:** launchd worked. The corruption survived because it's on-disk.
**Evidence:**
- `last reboot`: Tue Jul 14 20:11 EDT
- `Application:NFO Process starting: rippled-3.1.3` at 2026-07-15 00:13:01 UTC (2 min post-boot, clean start)
- No launchd loop-restart pattern
- Immediately post-restart: `SHAMapStore:WRN Waiting 5s for node to stabilize. state: full. age 198s` and climbing — corruption present from first tick

### Thread 4 — AMM 13,909 → 295 pool collapse
**Verdict:** Walker-side, NOT node corruption. Fully recovered.
**Separate root cause from Threads 1–3.**
**Evidence:**
- Latest `amm_ranked_pools` snapshot (2026-07-16 12:41 UTC): **28,582 rows**
- `rank_amms.py --reset` completed successfully
- The "295" observed earlier was a stale mid-run count (matches the `stale mid-run snapshot trap` memory)

---

## Phase 3 — Diagnosis + gated fix proposals with verification

### Shared vs separate root causes

**One root cause explains Threads 1 + 2 + the reboot-can't-help finding of Thread 3:** NuDB missing-node corruption → SHAMapStore can't rotate → node never stabilizes → consensus lag → job backpressure → load_factor spikes → walker RPC timeouts → downstream MISMATCH / changeSpotPriceQuality errors.

**Separate:** Thread 4 (AMM pool count) was a walker-side snapshot artifact, unrelated. Fully recovered.

**Contributing stressor, not a cause:** Walker read load competes for the same 16 GB / 10 cores during census firings. Not the source of the corruption (corruption predates any walker pattern change and appears in periods with no walker activity), but it *does* deepen the recovery hole. Your constraint stands — walkers keep running. But see the capacity answer below.

**Consequence chain (evidence-linked):**
1. NuDB has missing state tree nodes (2,596 SHAMapStore:ERR events, May 27 – Jul 14)
2. `SHAMapStore` rotation fails → `online_delete` effectively suspended for ~7 weeks
3. Old data never deleted → `nudb` = 33 GB vs 10–20 GB target
4. Node can't hold a stable window long enough to catch consensus (`Waiting 5s for node to stabilize. age NNNs` climbing to 1,304 s+)
5. Consensus lag → jobs pile up (ProcessLData wait 22 s+, ChkUntrust wait 91 s+)
6. LoadManager reports `Server stalled` cycles (179 in 6h)
7. Walker RPCs (`ledger_data`, `account_objects`) time out → census aborts (4 of 5 attempts in last 5 days)
8. `complete_ledgers` fragments into ~60 discontiguous ranges; `ledger_current` returns `noCurrent`

---

### Fix proposals — gated, verifiable, reversible

Order: **A → B → C**. A is prep, B is the real fix, C is post-fix tuning.

#### Fix A — Log rotation (config-only, no restart)
- **What changes:** Add a newsyslog stanza at `/etc/newsyslog.d/rippled.conf` rotating `debug.log` and `launchd-err.log` at 500 MB, keep 7.
- **Why (addresses what, exactly):** Prevents the 7 GB log growth pattern from repeating post-resync. Not causal to the corruption — pure hygiene so we don't ship a new problem.
- **Risk:** None. newsyslog is default macOS log manager, well-worn.
- **Rollback:** `rm /etc/newsyslog.d/rippled.conf`.
- **Verification (measurable after-state):** In 48 h, `ls -la ~/rippled-data/logs/` shows `debug.log.0.gz` present and `debug.log` under 500 MB.

#### Fix B — Rebuild NuDB from fresh sync (destructive, needs your gate)
- **What changes:** Take rippled down, move corrupted `nudb` directory aside, restart. rippled re-syncs current state + `ledger_history` window from the 10 peers.
- **Why (addresses root cause, not symptom):** This is the standard rippled recovery for persistent missing-node errors (rippled docs specifically). Config tweaks cannot backfill missing state tree nodes; only a resync from peers can. Every downstream symptom (rotation failure, DB bloat, consensus lag, load_factor spikes, walker timeouts, fragmented `complete_ledgers`) resolves once NuDB is intact again.
- **Sequence:**
  1. `launchctl bootout gui/$UID/<rippled label>` (identify exact label from `launchctl list | grep rippled`)
  2. `mv ~/rippled-data/db/nudb ~/rippled-data/db/nudb.old.20260716`
  3. `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/<rippled plist>`
  4. Monitor `~/rippled-data/logs/debug.log`: expect `NetworkOPs` state transitions `disconnected → connected → syncing → tracking → full`
- **Downtime:** 4–8 h to reach `full`, based on typical NuDB cold-start from 10 peers. The node is *already* effectively off-network (23 min behind, `noCurrent` on RPC), so real cost is smaller than it sounds.
- **Walkers during resync:** Your constraint says keep them running unless they're the cause. They aren't the cause. Recommendation: **leave them running, accept transient errors**. The load-gated ones (census_watcher) will self-pause via `load_factor` check; the always-on ones (nft_activity, rank_amms) will retry until the node is answering.
- **Risk:**
  - Peer starvation: 10 peers is on the low end; if resync stalls, add explicit `[ips_fixed]` bootstrap entries to `rippled.cfg`.
  - Disk: 33 GB → temporarily up to 66 GB during resync (old + new coexisting). We have 294 GB free — fine.
- **Rollback:**
  1. `launchctl bootout gui/$UID/<rippled label>`
  2. `rm -rf ~/rippled-data/db/nudb`
  3. `mv ~/rippled-data/db/nudb.old.20260716 ~/rippled-data/db/nudb`
  4. `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/<rippled plist>`
  5. Node returns to the current broken state; no additional loss.
- **Verification (measurable after-state) — all four must hold:**
  1. `server_info.validated_ledger.age < 10` sustained over a 30-min window
  2. `complete_ledgers` is **one** contiguous range (not fragmented)
  3. Zero `SHAMapStore:ERR Missing node while copying` in first 24h of new logs
  4. `nudb` size stabilizes under 20 GB after 24h (proves rotation is running)

#### Fix C — Post-resync config tuning (config-only, needs restart, ~1 min)
Apply *after* B verifies clean. Not before — no point tuning a broken node.

**C1: `node_size` medium → large**
- **What changes:** Bump to `large` in `rippled.cfg`. Docs bracket: medium=4 GB RAM target, large=8 GB, huge=32 GB.
- **Why:** M4 / 16 GB box has headroom for `large` (leaves 8 GB for macOS + walkers). `huge` would OOM on this box — do not attempt.
- **Risk:** Marginal — increases cache sizes; if memory pressure re-appears, revert.
- **Rollback:** Change back to `medium`, restart.
- **Verification:** `activity monitor` shows rippled RSS around 5–7 GB (up from 3 GB), free memory still >500 MB after 24 h under walker load.

**C2: `online_delete` — disk math**
- Ledger close time ≈ 3.5 s (XRPL standard).
- Retention math (fresh nudb, ~1.5–2 MB per ledger post-clean):
  - 10,000 ledgers = **9.7 h** retention, **15–20 GB** nudb
  - 30,000 ledgers = **29 h** retention, **45–60 GB** nudb
  - 50,000 ledgers = **48.6 h** retention, **75–100 GB** nudb
  - 100,000 ledgers = **97 h** retention, **150–200 GB** nudb
- We have 294 GB free.
- **Recommendation:** Keep at **10,000** for now. Rationale: bigger retention windows mean less frequent sweeps but larger per-sweep copy jobs — that's the exact SHAMapStore operation that failed here. Frequent-and-small is safer until we know the M4 handles a sweep cleanly. Revisit after 30 days of clean rotation.
- **Risk of raising:** Larger sweep = larger window during which a slow-ledger could trigger the same "Missing node while copying" pattern.

**C3: Peer limits**
- Currently: `peers_max` unset (default 21 outbound). Observed: 10 peers.
- **Recommendation:** Leave alone. 10 is on the low end but not the bottleneck; consensus signal is arriving, we're just failing to process it. Revisit only if resync in Fix B stalls.

**C4: relational DB settings**
- `transaction.db` = 6.3 GB (SQLite). Consumed by RPC-served historical tx lookups. Not implicated in current symptoms — leave alone.

---

### Capacity question — is the Mac mini + this config fundamentally sufficient?

**Honest answer: sufficient for rippled alone, *marginal* for rippled + all walkers under census-scale reads.** Numbers:

| Component | RAM budget | Observed |
|---|---|---|
| rippled node_size=medium | 4 GB (docs) | 3.2 GB RSS |
| rippled node_size=large (proposed) | 8 GB (docs) | est. 5–7 GB |
| macOS baseline | 3 GB | ~3 GB (wired+kernel) |
| Walker Python processes (nft_activity, rank_amms, census when firing) | 1–2 GB steady, spikes 3–4 GB during census | matches observed compressor pressure |
| **Total (large + walkers)** | **13–17 GB** | **16 GB physical** — no safety margin |

Right now, at `node_size=medium`, we're already seeing 8.5 GB in compressor and 81 MB free. That is the operational signature of memory pressure, and it's happening *before* the node is even doing useful work (it's off-consensus).

**What that means:**
- **Post-fix, single-box operation is viable but tight.** Fix B + Fix C1 leaves ~2–3 GB headroom, which will be consumed by census firings.
- **The safer operational posture is "walkers respect the node."** `census_watcher` already does this via the `load_factor <= 5.0` gate. **Recommendation: apply the same pattern to `rank_amms.py` and `nft_activity_walker.py`.** That's a walker-side change, not hardware.
- **The "budget decision" trigger.** If, after Fix B + C1 + walker-side load gates, we see either (a) sustained `Server stalled` events return, or (b) memory-pressure OOM kills, then one box is genuinely too much. At that point the decision is:
  - **Option X:** dedicated node-only Mac mini M4 (~$599) → walkers stay on this box, hit new box's rippled over LAN. Two boxes, each has 16 GB to itself.
  - **Option Y:** move rippled to a Linux VPS with 32 GB (Hetzner / OVH €40/mo), use `node_size=huge`, walkers stay local hitting VPS rippled over WAN.
- **Not now — first prove Fix B + C1 aren't enough.** Ordering the hardware before running the fix would be over-fitting to the symptom.

---

### Done-state — monthly re-runnable checks

Save these as `~/xrpl_test/scripts/node_health_check.sh` (I have not written it yet — will do only on your gate). Green = all eight hold; any red = investigate before the next month passes. Checks 1–5 are the primary "is the node healthy" panel; checks 6–7 are the pressure canaries added 2026-07-16 (Charlie's rider); check 8 is quarterly reboot resilience.

**Primary — node health:**
1. **Consensus lag:** `curl -s -X POST -H "Content-Type: application/json" -d '{"method":"server_info","params":[{}]}' http://127.0.0.1:5005 | jq -r '.result.info.validated_ledger.age'` → **< 10** (currently 1,376 → RED)
2. **Ledger contiguity:** same call, count comma-splits of `complete_ledgers` → **= 1** (currently 70 → RED)
3. **RPC responsiveness:** `curl … ledger_current` returns a `ledger_current_index` field → **present, not `noCurrent`** (currently `noCurrent` → RED)
4. **Rotation is running (primary canary — corruption):** `grep -c "Missing node while copying" ~/rippled-data/logs/debug.log` from a 30-day window → **= 0** (last 30 days: 2,596 → RED)
5. **DB size sane:** `du -sh ~/rippled-data/db/nudb` → **< 25 GB** at `online_delete=10000` (currently 33 GB → RED)

**Pressure canaries — added 2026-07-16 (Charlie's rider — "the box has a history of freezing under pressure"):**
6. **LoadMonitor stalls (secondary canary — box pressure):** `grep -c "LoadMonitor:WRN Job:.* run: [0-9]\{4,\}ms" ~/rippled-data/logs/debug.log` from last 7 days → **< 100/week** under normal walker load. If this climbs while check 4 stays green, the box is *heading toward* another corruption event, not yet arrived. **Triggers second-box decision.**
7. **Log growth rate (tertiary canary — free signal):** `du -sm ~/rippled-data/logs/debug.log` divided by days-since-last-rotation → **< 100 MB/day**. A healthy node barely writes to its own log; growth spike = degradation returning. (Fix A newsyslog rotation will make this straightforward to compute.)

**Quarterly:**
8. **Reboot resilience:** `sudo shutdown -r now`, wait 10 min, re-run checks 1–7 → **all green** without manual intervention.

---

## What I have NOT changed
Per your brief: **no config or process touched.** Awaiting your gate on Fix A, then Fix B, then Fix C (in that order — A is prep, C only after B verifies).

Application questions for you:
1. Gate **Fix A** (log rotation)? Trivial and independent of the rest.
2. Gate **Fix B** (NuDB resync)? Preferred window (now vs overnight EDT)?
3. Gate **Fix C** (config tuning) contingent on B's verification passing?
4. Willing to have me draft the walker-side `load_factor` gate for `rank_amms.py` and `nft_activity_walker.py` (matches the `census_watcher` pattern), as a follow-up after B — or is that a separate conversation?
