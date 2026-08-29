# Phase 2 — Memory-Aware Cache · Design Pack

**Status:** DRAFT (disk-first, in-progress). Sections marked with `[TODO]` are scaffolded but not yet written.
**Author:** JJ 🦞
**Date started:** 2026-08-21
**Reviewer:** Charlie
**Deadline (Charlie's ruling):** approved-and-shipped by Day 4 / Sunday 2026-08-24 restores the strict bar on the 7-day stability clock.
**Tier assumption:** Render Standard, 2GB RAM, workers 3 × threads 8 (per current `render.yaml`).

---

## 1. Purpose (one paragraph)

Homepage TTFB is 2–9s on uncached renders because three expensive computations run inline on `/`: the AMM snapshot (top 25 pools with TVL rollup), the whale events feed, and the top-tokens list. Phase 1 (shipped 2026-08-15 `3227997` after revert of `21849ce`) bought a 20→60s pulse TTL bump plus deferred vibes scripts — **zero cache change** — because the earlier attempt at a naive homepage SWR cache blew up in ~3 min with N cold-miss workers each rendering the full page in parallel, spiking RSS past 512MB on the Starter tier and triggering OOM kills. Phase 2 does the actual cache work correctly: **per-key TTL cache with a hard memory budget, LRU eviction, and — most importantly — a single-flight guard so cold-miss deploy cutovers can never again spawn N parallel renders of the same key.** Target: homepage TTFB ≤500ms cached, ≤5s cold-miss single-flight; no RSS regression on the Standard tier.

## 2. Non-goals (explicit — do NOT scope-creep)

- **Not** a distributed cache. Per-process (per-worker) only. No Redis, no cross-worker sync. Accept 3× compute on cold cache — one per worker.
- **Not** a fix for `/whales` or `/nfts` slowness — those already have HTML-body cache. This pack is homepage + shared primitives only.
- **Not** live-mode / SSE — that requires `--worker-class gevent` and is a separate pack.
- **Not** a metrics dashboard build-out. In-scope: minimal structured logging. Out-of-scope: `/admin/cache-stats` UI.
- **Not** a rewrite of `network_pulse.py` or the AMM snapshot generators — cache wraps them, doesn't refactor them.

## 3. Constraints (drawn from prior incident + current tier)

- **RSS budget per worker:** Standard tier gives 2GB total. Steady state on current code is ~240-350MB per worker (3 workers = ~750MB-1.05GB baseline). Peak allowance per worker: **1.2GB** (leaves ~400MB total tier headroom for Postgres pool + gunicorn overhead + spikes).
- **Cache budget per worker:** **200MB hard cap** (600MB fleet-wide). Anything larger risks pressure under concurrent traffic bursts.
- **Rollback contract:** two feature flags — `CACHE_ENABLED` (disables cache path, falls back to inline compute) and `SF_GUARD_ENABLED` (disables single-flight lock, allows thundering herd). Flip via env, no code push required. Deploy Phase 2 with `CACHE_ENABLED=false` first, prove guard alone is stable, then flip cache on.
- **Deploy cutover behavior — LESSON FROM 2026-08-15:** the OOM occurred because a fresh deploy = empty cache + immediate traffic burst → N parallel renders per worker. The single-flight guard MUST be armed even when the cache itself is off, or the fix regresses on every subsequent deploy.
- **Freshness contracts must not weaken current behavior.** Pulse block already at 60s. Any per-surface TTL must be ≤ current implicit "computed every request" plus SWR staleness bound.

## 4. Architecture

### 4.1 The primitive

Single class: `MemoryAwareTTLCache` in `caching/memory_aware_cache.py` (new module). Public API:

```python
class MemoryAwareTTLCache:
    def __init__(self, max_bytes: int, default_ttl_seconds: float,
                 name: str = "unnamed"):
        """max_bytes = hard cap. default_ttl_seconds = fallback TTL when caller doesn't specify. name = used in log lines + metrics."""

    def get_or_compute(self,
                       key: str,
                       computer: Callable[[], Any],
                       ttl_seconds: float | None = None,
                       stale_while_revalidate_seconds: float = 0,
                       size_hint_bytes: int | None = None) -> Any:
        """Cache lookup + compute-on-miss + single-flight + SWR.

        - If key present and not expired: return cached value.
        - If key present but stale AND within SWR window: return stale + trigger
          background refresh (best-effort, ignore failures).
        - If key absent or beyond SWR: acquire per-key lock; first thread computes,
          others wait. Result is cached with size accounting. LRU eviction runs
          if new entry would exceed max_bytes.
        - size_hint_bytes: caller-supplied size (used when auto-sizing is unreliable,
          e.g. large HTML strings — use len(value.encode('utf-8')))."""

    def invalidate(self, key: str) -> bool:
        """Force-drop an entry. Returns True if present."""

    def stats(self) -> dict:
        """Returns {hits, misses, evictions, sf_collisions, current_bytes,
        max_bytes_touched, entries}. Cheap to call."""
```

### 4.2 Single-flight guard (the load-bearing invariant)

- Per-key `threading.Lock` stored in a `dict[str, threading.Lock]` guarded by an outer `threading.Lock` (double-checked locking pattern).
- First thread to arrive on a cold key: acquires lock, computes, writes to cache, releases lock.
- Subsequent threads: block on the same lock; once released, read from cache (fresh entry now present).
- **Timeout on the per-key lock: 30 seconds.** If compute hangs beyond this, waiters get a `TimeoutError` and the caller can decide (homepage: render fallback / return 503; internal helpers: raise).
- **This guard is armed independent of `CACHE_ENABLED`.** With cache off, the compute still happens N times per worker on a cold key (because no cache write) — but the guard prevents the *same* worker from spawning N parallel computes for the same key.

### 4.3 Memory accounting

- On `set`: compute size. Strategy:
  - `size_hint_bytes` if provided → use verbatim (caller knows best for HTML/JSON strings).
  - Else: `sys.getsizeof(value)` for scalars/small tuples; recursive walk with visited-set for containers up to depth 3; fallback to `len(repr(value))` for opaque objects.
- Running total `_current_bytes` maintained on every `set` and eviction.
- **Eviction:** when a new entry would push over `max_bytes`, evict LRU entries until the new entry fits. If a single entry exceeds `max_bytes`, refuse to cache it (log warning, still return the value to the caller).

### 4.4 SWR (stale-while-revalidate)

- On stale-but-within-SWR hit: return stale value immediately; spawn a `threading.Thread(target=computer, daemon=True)` to refresh in background.
- Background refresh acquires the per-key lock — so a stampede of "stale hits" during the SWR window all trigger *one* background refresh, not N.
- Background refresh failures: swallow silently (already-stale value is still being served; log warning; alarm only if refresh fails 3× in a row for the same key).

## 5. Per-surface TTLs and SWR windows

`[TODO — first-pass table below, need Charlie's ruling on the SWR bounds]`

| Surface | Fresh TTL | SWR window | Cache key | Notes |
|---------|-----------|------------|-----------|-------|
| pulse block | 60s | 60s | `pulse:v1` | Matches Phase 1 shipped 20→60s bump |
| AMM snapshot (top 25) | 300s | 300s | `amm_snapshot:top25:v1` | Rankings change slowly; cheap-stale |
| whale events feed | 30s | 60s | `whale_events:v1` | Freshness matters more |
| top tokens (10) | 300s | 300s | `top_tokens:top10:v1` | Cheap-stale like AMM |
| homepage full HTML | 60s | 240s | `home_html:v1:<lang>` | Per-language variant |

**Open Q for Charlie:** the 240s SWR on homepage HTML is aggressive; drop to 120s if you want fresher first-byte. Tighter SWR = more compute; looser = staler content served to some users. My default = 240 (favors speed, since staleness is bounded by the 60s fresh window under any real load).

## 6. Rollout plan (staged, revertible at every step)

**Design principle:** every step is a code-path advancement that can be reverted via dashboard env-flip in <2 min without a git push. Only Step 0 requires a code push; Steps 1→2→3 are env-only transitions.

### 6.1 Env-var contract added to `render.yaml`

Two new keys in the `envVars` block (values default to safe-off; dashboard edit flips them live without a redeploy):

```yaml
      # Phase 2 cache: master enable. false = cache path disabled entirely,
      # falls back to inline compute (current behavior). Env-flip via
      # dashboard for zero-code-push rollback.
      - key: CACHE_ENABLED
        value: "false"

      # Phase 2 single-flight guard: prevents thundering herd on cold-miss
      # keys (per-worker per-key lock, 30s timeout). Armed independent of
      # CACHE_ENABLED so cold-deploy cutovers can't spawn N parallel
      # renders even when cache itself is off. Only flip false as a
      # last-resort rollback — this re-exposes the 2026-08-15 OOM class.
      - key: SF_GUARD_ENABLED
        value: "true"
```

The `render.yaml` values are the FLOOR (Render blueprint sync overwrites dashboard on push per `render.yaml:10-13` note). Dashboard edits are ephemeral — they hold until the next `git push`, at which point Render re-syncs from yaml. **Once Step 2 is proven green for 24h, the yaml `CACHE_ENABLED` value flips to `"true"` in a followup commit** so the setting is durable.

### 6.2 Pre-flight (Step 0 — must pass before ANY deploy)

Unit tests in `tests/test_memory_aware_cache.py`:

1. **Single-flight collision test.** Spawn 20 threads, all calling `get_or_compute("k", slow_computer)` where `slow_computer` sleeps 500ms + increments a counter. Assert: counter == 1 (compute happened exactly once), all 20 threads returned the same value, elapsed wall time < 700ms (not 20 × 500ms).
2. **LRU eviction test.** `max_bytes=1000`. Insert 10 entries of 200 bytes each. Assert: cache holds the 5 most-recently-touched, older entries evicted, `stats()['evictions'] == 5`.
3. **Oversized-entry test.** Try to cache a 2000-byte value with `max_bytes=1000`. Assert: value is RETURNED to caller, NOT cached (`stats()['entries'] == 0`), warning logged.
4. **SWR refresh test.** Insert entry with `ttl=1, swr=5`. Sleep 1.5s. `get_or_compute` returns stale value immediately; assert background refresh thread completed within 2s; assert next call returns fresh value.
5. **Compute-exception test.** `computer` raises `RuntimeError`. Assert: exception propagates to caller, entry NOT cached, next call retries compute (does not serve cached exception).
6. **Memory-accounting drift test.** Insert 100 entries of known JSON size; call `stats()['current_bytes']`; assert within ±10% of true sum (recursive walker is approximation, not exact).
7. **Guard-only smoke test.** With cache-write mocked to no-op, verify guard still serializes computes (single-flight semantic works standalone).

Unit tests must run in <5s combined. Ship as its own commit before the wiring PR — proves the primitive works in isolation.

### 6.3 Step 1 — deploy code + guard armed, cache OFF (T+0 to T+15 min)

**Preconditions:**
- Step 0 tests all green in CI
- Homepage p50 baseline captured from Render Metrics tab (record the number in the deploy notes for later comparison)
- Charlie present at keyboard, Render dashboard open on Events + Metrics tabs

**Action:**
```bash
git push origin main
# Render auto-deploy fires (autoDeploy: true, verified today via anchor #3 push flow)
```

**Watch (Render Events tab):**
- Build succeeds (Step 0 tests are part of build via pytest hook — TBD wire-in)
- Deploy transitions "In progress" → "Live" (~3-5 min)
- First 3 min of "Live": Metrics tab RSS per worker

**GREEN criteria for Step 1:**
- RSS steady state within ±30MB of pre-deploy baseline per worker
- Homepage still renders (curl `xrpldashboard.onrender.com/healthz` returns 200 within 2s from a good vantage — remembering the LAN blindness caveat from today's DNS wound)
- Zero exceptions in gunicorn logs mentioning `MemoryAwareTTLCache` or `single-flight`
- No BetterStack pager fires in 15 min post-Live

**RED criteria — rollback immediately:**
- Any exception traceback mentioning the new module → git revert commit `<phase2-wiring-hash>`; force push if needed with your explicit approval
- Any OOM notification from Render → same

### 6.4 Step 2 — enable cache via env-flip (T+? when Step 1 is 15min-green)

**Action (Render dashboard, ~30 sec):**
1. Render dashboard → xrpldashboard service → Environment tab
2. Find `CACHE_ENABLED`, edit value from `"false"` to `"true"`
3. Save changes → Render triggers auto-restart (rolling, ~2 min for all 3 workers)

**Watch (30-min observation window):**
- **RSS per worker** on Metrics tab — must not exceed 1.2GB per worker at any point
- **Homepage p50 TTFB** — target <500ms cached; first 5 min will show cold-miss slowness as cache warms
- **`CACHE_STAT` log lines** in gunicorn stdout (via Render logs) — hit rate should climb from 0% toward >80% within 5 min of production traffic

**GREEN criteria for Step 2:**
- RSS stays under 1.2GB per worker for full 30 min
- Homepage p50 TTFB drops under 500ms within 5 min of cache warming
- Single-flight collisions logged during first 60 seconds (proves guard is engaging) then tapers as cache warms
- Zero OOM notifications
- No BetterStack pager fires

**RED criteria — rollback immediately (dashboard env-flip, no git needed):**
- RSS > 1.4GB sustained >5 min on any worker → `CACHE_ENABLED=false`
- Any OOM notification → same
- p95 TTFB > baseline + 500ms sustained >5 min → same
- BetterStack pager fire → same, plus investigate before re-attempting
- Any HTTP 500 spike in logs → same

**Rollback command (dashboard):** Environment tab → `CACHE_ENABLED` → `"false"` → Save. Auto-restart clears cache and returns to Step 1 state within ~2 min. If dashboard unreachable, fallback: `git revert <phase2-wiring-hash> && git push`.

### 6.5 Step 3 — 24h hardening + yaml lock-in

After 24h of continuous Step 2 GREEN:
- Commit `render.yaml` change: `CACHE_ENABLED` value `"false"` → `"true"` — durable enablement
- Optional: add per-surface SWR to whale events and AMM snapshot (independent enables via new env keys `SWR_WHALE_EVENTS`, `SWR_AMM_SNAPSHOT`, both defaulting off)
- Write runbook entry in `docs/RUNBOOK_PHASE2_CACHE.md`: rollback playbook, RSS watch cadence, expected hit-rate curve

### 6.6 What Charlie has to do vs. what JJ does

**JJ (with your sign-off on this pack + each PR):**
- Writes the primitive + tests (PR #1)
- Wires the primitive into homepage render path + adds env-var reads (PR #2)
- Adds env-var declarations to render.yaml (part of PR #2)

**Charlie (manually):**
- Merges each PR (git push runs on your local — auto-deploy fires)
- Watches Render Events + Metrics tabs during each rollout step
- Executes the dashboard env-flip for Step 2 (CACHE_ENABLED=true)
- Commits the render.yaml value flip for Step 3 (durable enablement)
- Executes rollback env-flip if any RED criterion trips

## 7. Metrics + logging

### 7.1 Structured log shape

Every cache event emits one JSON line to gunicorn stdout, prefixed `CACHE_STAT` for grep:

```
CACHE_STAT {"cache":"home_html","ev":"hit","key_prefix":"home_html:v1:en","ms":0.4,"size_b":24816,"cur_b":142857,"max_b":209715200}
CACHE_STAT {"cache":"amm_snapshot","ev":"miss","key_prefix":"amm_snapshot:top25:v1","ms":842.7,"size_b":9820,"cur_b":152677,"max_b":209715200}
CACHE_STAT {"cache":"home_html","ev":"sf_wait","key_prefix":"home_html:v1:en","ms":812.3,"waited_for_key":true}
CACHE_STAT {"cache":"home_html","ev":"evict","key_prefix":"home_html:v1:de","size_b":24501,"cur_b":118356,"max_b":209715200}
CACHE_STAT {"cache":"home_html","ev":"refuse_oversized","key_prefix":"home_html:v1:XX","attempted_size_b":250000000,"max_b":209715200}
CACHE_STAT {"cache":"home_html","ev":"refresh_ok","key_prefix":"home_html:v1:en","ms":734.1,"size_b":24816}
CACHE_STAT {"cache":"home_html","ev":"refresh_fail","key_prefix":"home_html:v1:en","err":"TimeoutError","streak":1}
```

Event vocabulary (7 values, kept small on purpose):
- `hit` — value in cache and fresh
- `miss` — value not in cache, computed now
- `sf_wait` — waited on per-key lock for another thread's compute
- `sf_timeout` — waited past 30s timeout, raised TimeoutError to caller
- `evict` — LRU eviction to make room
- `refuse_oversized` — single entry exceeds `max_bytes`, not cached
- `refresh_ok` / `refresh_fail` — SWR background refresh outcome

**Key handling:** never log the full key (may contain user-specific tokens in future). Log `key_prefix` = first 40 chars.

### 7.2 Log destination decision

**Ship first:** gunicorn stdout (Render captures automatically, grep-able via Render logs UI or `render logs` CLI). Zero setup cost.

**Phase 2b (post-24h GREEN):** optional periodic writer to `walker_health` table for SQL-queryable history:

```python
# Every 60s, one row per cache instance summarizing the prior minute:
INSERT INTO walker_health (walker_name, status, message, checked_at)
VALUES ('cache_home_html', 'ok', '{"hits":847,"misses":12,"evictions":0,...}', NOW())
```

Decision boundary: if debugging a live incident requires cross-referencing cache behavior against user-facing symptoms, we want SQL; otherwise stdout grep is enough. **Defer this until we actually need it** — YAGNI.

### 7.3 Alarm thresholds (deliberately deferred)

**No hard alarms in Phase 2 initial ship.** Rationale: we don't yet know normal-looking distributions of these events. Setting a threshold prematurely = false alarms, threshold-blindness, or missed real signals.

After 24-48h of production log observation, candidates to codify:
- `sf_timeout` events (currently zero expected; any non-zero warrants investigation)
- `refresh_fail` streak ≥3 for same key (persistent computer failure)
- `refuse_oversized` events (spec violation — something is generating unexpectedly large cache values)
- `cur_b > 0.95 * max_b` sustained >10 min (approaching cap; may need budget bump)

Threshold PRs come after observation, not with the initial ship.

### 7.4 Sampling

**Initial ship:** log 100% of events. Volume estimate: at 24 concurrent slots × 60s/pulse × 3 workers × ~10 cache surfaces = ~30 log lines/sec sustained. Small, tolerable, and diagnostic if anything goes wrong.

**Post-24h optimization:** if log volume becomes a problem (unlikely on gunicorn stdout + Render's log ingestion), drop `hit` events to 1% sampling. Keep `miss`, `sf_*`, `evict`, `refuse_oversized`, `refresh_fail` at 100% — those are the diagnostic signals.

## 8. Failure modes + guardrails

Enumerated defensively — each mode named + mitigation designed BEFORE ship (not after incident).

### 8.1 Compute raises exception on miss
**Mode:** `computer()` raises (e.g., DB timeout, XRPL RPC failure).
**Behavior:** exception propagates to caller. Entry is NOT cached (do not poison the cache with a failed compute). Per-key lock is released. Next thread arriving retries the compute.
**Guardrail:** wrap `computer()` in `try/finally` where the `finally` releases the lock; catch the exception outside the cache-write path so no partial state persists.
**Non-mitigation (deliberately):** no retry, no fallback value, no exception caching. Caller decides how to handle failure.

### 8.2 Guard deadlock
**Mode:** thread A holds per-key lock for key K, tries to `get_or_compute(K)` recursively (e.g., cached view calls into itself).
**Behavior:** would deadlock on the second acquire.
**Guardrail:** use `threading.RLock()` (reentrant), not `threading.Lock`. Same thread can re-acquire safely. Cost: slight overhead per acquire; benefit: eliminates the entire recursive-computer footgun class.
**Additional guardrail:** doc string on `get_or_compute` warns against recursive use anyway (recursion produces N compute levels each with own lock cost).

### 8.3 Size accounting drift
**Mode:** recursive walker under-counts deeply nested containers (stops at depth 3); `current_bytes` reads lower than true heap use over time.
**Behavior:** cache may hold more data than `max_bytes` reports.
**Guardrail:** for known-large values (HTML strings, JSON blobs), require `size_hint_bytes` from caller. Fail loudly (log warning) if caller stores a value >10KB without a hint.
**Recovery:** `stats()` triggers a periodic full-recount every N calls (N=1000) — corrects drift silently. Do NOT recount on every `set` (too expensive).

### 8.4 RSS pressure independent of cache size
**Mode:** cache reports 150MB used, but Python GC + Postgres pool + module state pushes actual worker RSS to 1.5GB.
**Behavior:** OOM risk despite cache being within its internal budget.
**Guardrail:** **cache max_bytes is NOT the safety mechanism.** The external Render RSS metric IS. Rollout Step 2's GREEN criterion is 1.2GB per worker on Render's meter, not on our internal accounting. If external RSS climbs while internal accounting looks fine, the rollback trigger still fires.
**Design implication:** don't set `max_bytes` optimistically ("we have 800MB headroom, cache 600MB"). Set conservatively (200MB) so external pressure has room to breathe.

### 8.5 SWR refresh storm
**Mode:** during high traffic, many keys go stale near-simultaneously; each spawns a background refresh thread; N × M refreshes hammer downstream (Postgres, XRPL RPC).
**Behavior:** downstream saturation, refresh_fail cascade, potential external service anger.
**Guardrail per-key:** already handled — per-key lock means only ONE refresh per key at a time regardless of how many stale hits arrive.
**Guardrail cross-key (design decision):** initial ship has NO global refresh semaphore. Rationale: 10 cache surfaces × 3 workers × 1 refresh each = 30 concurrent refreshes worst case, well within Postgres pool (16-20 conns per worker) and XRPL RPC tolerance. If we observe refresh_fail spikes correlated with high traffic, add `threading.BoundedSemaphore(8)` around the background refresh spawn.

### 8.6 Cache-poisoning via mutable value
**Mode:** caller retrieves a cached list/dict and mutates it in place. Next reader sees mutated value.
**Behavior:** cache correctness broken subtly. Very hard to debug.
**Guardrail:** doc contract — cached values are IMMUTABLE from caller's perspective. Callers who need to mutate MUST `copy.deepcopy` first.
**Additional guardrail (optional, deferred):** in debug mode only (FLASK_DEBUG=1), wrap returned values in a read-only proxy that raises on mutation. Not in prod (perf cost).

### 8.7 Stampede on module load / worker boot
**Mode:** worker starts, cache is empty, first N concurrent requests all miss on the same keys, single-flight serializes them all sequentially → cold-start p99 spike.
**Behavior:** each cold miss adds ~1s to the sequential chain; 20 concurrent same-key requests = 20s tail latency for the last one.
**Guardrail:** single-flight is per-key, so *different* keys computed in parallel across threads (parallelism preserved for the common case). Same-key stampede is the correct behavior (only one compute per unique work).
**Optional mitigation (deferred):** background prewarming task on module import — but this risks OOM at boot on all 3 workers simultaneously. Do NOT ship at initial rollout.

## 9. What Phase 3 might build on this

Nothing in this pack precludes any of the following, but none of it is in-scope for the initial ship.

### 9.1 Live-mode / SSE (whales stream, pulse stream)
- Requires `gunicorn --worker-class gevent` (currently sync). Sync workers hold a slot open for the SSE lifetime → connection starvation within 24 slots.
- Cache implication: SSE streams read from the same primitive; `MemoryAwareTTLCache` is thread-safe today but has never been exercised under long-lived greenlet contention. Add a `test_greenlet_safety.py` gate when the SSE pack lands.
- Prior finding (2026-08-15 memo): Postgres `LISTEN/NOTIFY` is the zero-node-impact source for whale events. Cache should sit *between* the LISTEN handler and the SSE emitter, not on top.

### 9.2 Cross-worker cache sharing (Redis)
- Current cost of per-worker cache: 3× cold compute per key (once per worker) on deploy. Acceptable on Standard tier.
- Cost of adding Redis: Render Redis add-on (~$10/mo starter), operational surface expands, serialization overhead per get/set (~1-3ms per op), new failure mode ("Redis down").
- **Decision default:** do NOT add Redis until we observe the 3× cold-compute cost as an actual pain point (RSS spike on deploy, OOM near-miss, or user-visible latency during rollout). It has never been demonstrated as a real problem.
- If we do add it later: keep `MemoryAwareTTLCache` as L1 (per-worker, microseconds), Redis as L2 (cross-worker, milliseconds). Never make Redis the only cache — network partition scenarios would trigger the exact 2026-08-15 OOM pattern.

### 9.3 Observability dashboard (`/admin/cache-stats`)
- Prereq: Phase 2b log→`walker_health` writer shipped (Section 7.2).
- Shape: single Flask route (admin-auth-gated) reads last 60 rows per cache instance, renders sparkline of hit-rate / RSS / evictions.
- Value: converts grep-through-Render-logs into 30-second visual triage.
- Slot: bundle with the general observability sweep, not this pack.

### 9.4 Cache invalidation on write (currently: TTL only)
- Today's whale-events cache is TTL-invalidated (max 30s stale). If a whale tx lands during the TTL window, users see the old feed for up to 30s.
- Phase 3 candidate: `Postgres LISTEN/NOTIFY` on `amm_pool_events` → cache.invalidate("whale_events:v1"). Turns 30s freshness ceiling into ~100ms.
- Not in Phase 2 because: the TTL contract is honest and 30s of staleness is inside our published freshness bar for the whales feed.

### 9.5 Per-endpoint compression (gzip/brotli of cached HTML)
- Homepage HTML cache would benefit: ~24KB gzipped vs ~120KB raw. Net cache-size reduction 5×.
- Trade-off: CPU per get (decompress) vs bandwidth per response (already gzipped by CF).
- Not in Phase 2 because: cache budget (200MB) is nowhere near saturated at current homepage HTML size × language variants.

## 10. Rulings requested from Charlie

Everything below is a decision point that needs your ack before I open PRs. Ordered by blocking-ness (top = blocks first commit, bottom = can decide later).

### 10.1 [BLOCKS PR #1] Per-worker cache budget: 200MB
Section 3 constraint. Alternatives:
- **128MB** — safer under RSS pressure, forces earlier LRU eviction, may reduce hit rate on homepage HTML with many language variants.
- **200MB (JJ default)** — leaves ~400MB tier-wide headroom for spikes; comfortable given 240-350MB steady state × 3 workers.
- **300MB** — more headroom for future surfaces; tighter safety margin (peak worker could reach 1.5GB).

My recommendation: **200MB**. Rationale: 2026-08-15 OOM class was N cold-miss parallel *renders*, not steady-state cache growth. 200MB gives headroom for LRU to breathe without approaching the tier ceiling.

### 10.2 [BLOCKS PR #1] Ship order: primitive+tests as own PR vs bundled with wiring
- **Split (JJ default):** PR #1 = primitive + Section 6.2 unit tests, zero prod behavior change. PR #2 = wiring + env-var reads + render.yaml changes. Merges independently; #1 rollback = trivial (no callers), #2 rollback = env-flip.
- **Bundle:** single PR. Fewer merge cycles, but rollback surface is larger.

My recommendation: **split**. Rationale: PR #1 has zero runtime risk (unused module), gets the primitive on prod for internal testing before wiring goes live.

### 10.3 [BLOCKS PR #2] Homepage SWR window: 240s
Section 5 table. Trade-off:
- **240s (JJ default)** — favors first-byte speed; stale-served window bounded by 60s TTL + 240s SWR = 5 min worst case; background refresh keeps a live worker feeding the cache.
- **120s** — fresher content, doubles the background refresh rate.
- **60s (no SWR)** — matches current pulse TTL discipline exactly; every 60+s request is a cold-miss (single-flight-guarded).

My recommendation: **240s**. Rationale: homepage HTML changes slowly (content editorially updated on your cadence, not per-request). 5 min worst-case staleness ≪ your editorial cycle.

### 10.4 [BLOCKS PR #2] Which surfaces get cached in initial ship
Section 5 lists five surfaces. Alternatives:
- **All five (JJ default)** — matches homepage compute inventory in Section 1.
- **Homepage HTML only** — smallest blast radius; other surfaces still recompute per-request.
- **Homepage HTML + AMM snapshot** — covers the two heaviest computes; whale events + top tokens stay direct.

My recommendation: **all five**. Rationale: the primitive is one class; wiring 5 call-sites vs 1 is trivial marginal cost, and the single-flight guard benefit compounds across surfaces.

### 10.5 [DEFERRABLE] Log destination for initial ship: gunicorn stdout
Section 7.2. Alternatives:
- **stdout (JJ default)** — zero setup, grep-via-Render-logs works.
- **walker_health table now** — SQL-queryable from day 1; adds Phase 2b work up-front.

My recommendation: **stdout**. Rationale: don't build the SQL surface until we've read the log stream for 24-48h and know what queries we'd actually run.

### 10.6 [DEFERRABLE] Fold live-fetch amendments (pack b) cache decisions into this architecture
- **Fold** — one primitive powers both; live-fetch amendments becomes another `get_or_compute` call with its own TTL/SWR.
- **Separate** — live-fetch amendments gets its own domain-specific cache (e.g., in `amendments_watcher.py`).

My recommendation: **fold**. Rationale: `MemoryAwareTTLCache` is intentionally general-purpose. Adding a live-fetch key with `ttl=600, swr=600` is a one-liner in the live-fetch pack, no new primitive.

### 10.7 [DEFERRABLE] Rollback tolerance for Step 2
Section 6.4 GREEN/RED criteria assume `p95 TTFB > baseline + 500ms` as a rollback trigger. Alternatives:
- **+500ms (JJ default)** — leaves room for cold-warmup jitter; catches real regressions.
- **+250ms** — tighter, more sensitive to any degradation.
- **+1000ms** — very lax, only catches severe regressions.

My recommendation: **+500ms**. Rationale: baseline uncached is 2-9s; +500ms is well inside noise on that band, but +500ms over the *cached* baseline (~500ms target) would be a real signal.

---

**End of design pack v1.** Sections 1-10 all fully written. Awaiting Charlie rulings on 10.1-10.4 to unblock PR #1 and PR #2 wiring. 10.5-10.7 can be decided during rollout without holding up ship. Post-ruling next step: PR #1 (`caching/memory_aware_cache.py` + `tests/test_memory_aware_cache.py`), estimated ~2 hours of code + test writing after your ack lands.
