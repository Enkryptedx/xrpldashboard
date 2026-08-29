# /analytics query batching — design-class card

**Filed:** 2026-08-29 (Hour 3 of six-hour keyboard sprint)
**Verdict:** design-class, not shipped blind. **Landed as card, not code.**

---

## Snack-vs-design triage — WHY this wasn't shipped

The obs-queue entry framed this as "26 queries × ~300ms Render→Neon RTT = ~8s cold render." That maths correctly for a cold hit, but the cold hit is already defended:

- `_analytics_warmer_loop` (app.py:973-1021) keeps the cache perpetually warm — every 30s rebuild if entry expires within 30s.
- `_ANALYTICS_CACHE_TTL_S` = 60s in-process cache, SWR on expiry.
- `Cache-Control: no-store` (path-scoped, app.py:1301) so browsers can't force a mis-served stale.
- Process-restart is the only user-facing moment a cold render is visible; warmer catches it 10s after gunicorn boots (line 989: `time.sleep(10)`).

So the ~8s cold render is a **code-shape** cost, not a live user-facing wound. Shipping a parallelize/batch refactor as a snack would (a) reduce process-restart TTFB by ~5x for no user who noticed, and (b) risk consuming N Neon pool slots concurrently — Neon Free tier caps at 20 connections; Render's gunicorn workers add multiplier. Any parallel refactor MUST first verify pool ceiling, which turns it design-class.

Also: the header comment says "28 queries, 22 heavy _bot_filter_sql, 4 all-time scans" — the 28-count includes internal sub-selects inside `read_*` functions. Actual top-level `read_*` calls in `analytics()`: 15. The batching win depends on whether the internals can share a bound-parameter block.

---

## Options (three shapes, ordered by risk)

### Option A — cache TTL bump (safest, no code change to hot path)

- `_ANALYTICS_CACHE_TTL_S`: 60 → 300 (5min). Warmer stays at 30s cadence so bump has no cold-render impact.
- Removes 4 warmer cycles per 5min = ~80% less compute on the hot path.
- Trade-off: aggregates lag up to 5min vs current 60s. Right-now counts still fresh via `/analytics/live`.
- Effort: 1 LOC.

### Option B — ThreadPoolExecutor parallelization (medium-risk, real speedup)

- Wrap the 15 `read_*` calls in a `concurrent.futures.ThreadPoolExecutor(max_workers=6)` fan-out. Each function opens its own `pg_connect()` so no shared-connection contention.
- Expected speedup: ~5x on cold render (down from 8s to ~1.5s) since Neon RTT ~200-300ms/query and 15 queries can overlap.
- Prerequisite: verify concurrent connection ceiling on Neon (Free-tier = 20; Render pool sizing unknown). Also verify no `read_*` opens > 1 connection per call.
- Effort: ~30 LOC + concurrency test + connection-ceiling probe.

### Option C — CTE batch refactor (highest-risk, biggest win)

- Rewrite the human-side reads (rollups + top_pages + country_breakdown + country_count) as one CTE query with multiple result subsets, then split in Python.
- Expected speedup: ~10x on cold render (single round-trip).
- Prerequisite: schema-level review; brittle to future changes in either function; bot-side + recent still need separate calls.
- Effort: ~150 LOC + regression suite for exact-row-shape equivalence.

---

## Recommendation

**Ship Option A first (1 LOC, no risk).** Measure impact via existing `analytics_cache: hit/miss/gen_ms` logline for 1 week. If cold-miss rate stays acceptable (target: <1% of requests), stop there — the wound is closed.

**Only consider B or C if:**
- Warmer daemon breaks or is intentionally removed.
- Process-restart frequency increases (currently ~1/deploy = ~5x/week).
- A pattern of `gen_ms > 5000ms` shows up in prod logs at a rate that suggests the warmer is losing races.

**Do not ship B or C blind.** Both need connection-pool sizing that's not in the current instrumentation.

---

## Related

- app.py:7166-7339 — analytics route
- app.py:973-1021 — warmer daemon
- app.py:841-870 — cache constants
- docs/OBSERVABILITY_QUEUE_2026-08-27.md — where this item lived
