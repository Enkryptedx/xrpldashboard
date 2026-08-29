# PR #1 STAGED — Phase 2 primitive + tests (phone-review card)

**Status:** committed locally on Mac tree, **NOT pushed**. Awaits Charlie's morning keyboard pass.
**Commit:** `919f416` on `main`, ahead of `origin/main` by 1.
**Rollout position:** PR #1 of 2 (split ship per ruling 10.2). Zero prod behavior change on merge — no callers wired.
**Push gate:** Charlie's hand, morning of 2026-08-22. Auto-deploy is live → a push IS a deploy.

---

## What's in it

Three new files, 718 insertions, zero modifications to existing files:

```
 caching/__init__.py              |  13 ++
 caching/memory_aware_cache.py    | 432 +++++++++++++++++++++++++++++++++++++++
 tests/test_memory_aware_cache.py | 273 +++++++++++++++++++++++++
```

### `caching/memory_aware_cache.py` — the primitive

`MemoryAwareTTLCache` class. Public surface (stable):

- `__init__(max_bytes, default_ttl_seconds, name)` — one instance per surface
- `get_or_compute(key, computer, ttl_seconds=None, stale_while_revalidate_seconds=0, size_hint_bytes=None)` — the workhorse
- `invalidate(key)` — force-drop (for future write-through invalidation)
- `clear()` — test teardown only
- `stats()` — cheap snapshot; triggers full byte-recount every 1000 calls

Internals:
- **Single-flight guard:** per-key `threading.RLock` in a `dict`, guarded by an outer `_map_lock` (double-checked locking). RLock chosen over Lock to defuse the recursive-computer deadlock class (Section 8.2). 30s per-key acquire timeout matches gunicorn `--timeout 30` in render.yaml.
- **LRU:** `OrderedDict` — `move_to_end` on read/write, `popitem(last=False)` evicts. O(1) all paths.
- **Memory accounting:** `size_hint_bytes` if caller supplies; else `sys.getsizeof` + recursive walker depth 3. Values >10KB without a hint log a warning. Values > `max_bytes` are RETURNED to caller but NOT cached (`refuse_oversized` event).
- **SWR:** stale-but-within-SWR hit spawns daemon thread; refresh acquires per-key lock with `blocking=False` so stampeded stale hits collapse to ONE background refresh.
- **No exception poisoning:** `computer()` failure propagates to caller, entry unchanged, next thread retries.

Structured log line on every event, prefix `CACHE_STAT`:
```
CACHE_STAT {"cache":"home_html","ev":"hit","key_prefix":"home_html:v1:en","cur_b":142857,"max_b":209715200,...}
```
7-event vocabulary: `hit / miss / sf_wait / sf_timeout / evict / refuse_oversized / refresh_ok / refresh_fail`.

### `tests/test_memory_aware_cache.py` — the 7 gates

| # | Gate | What it proves |
|---|------|----------------|
| 1 | Single-flight collision | 20 threads on same cold key → `compute_count == 1`, wall time < 2s (not 10s) |
| 2 | LRU eviction | 10 × 200B entries into 1000B cache → 5 survivors, 5 evictions |
| 3 | Oversized refuse | 5000B value into 1000B cache → returned to caller, NOT cached |
| 4 | SWR refresh | ttl=1s swr=5s: t=1.2s call returns stale + spawns refresh; next call returns fresh |
| 5 | Exception non-poison | RuntimeError propagates; entry not stored; next call retries; recovers cleanly |
| 6 | Accounting drift | 100 × 5000B entries → `current_bytes` within ±10% of expected |
| 7 | Guard-only smoke | With `_store` monkeypatched to no-op: 10 threads → 10 compute (no cache) but SERIALIZED (per-key lock still engages) — proves guard works when CACHE_ENABLED=false |

**Result:** `7 passed in 6.14s` on `venv_py311` (Python 3.11.16, pytest 9.1.1).

The 6.14s slightly exceeds the <5s soft target in the design pack; overage comes from Gate 4's mandatory 1.2s sleep past TTL + Gate 7's ~3s serialized wait chain — both inherent to what the tests exercise (time-based semantics + serialization proof). Acceptable.

---

## What's NOT in this PR (deliberately, per split-ship ruling)

- No wiring into `app.py` or any request handler
- No changes to `render.yaml` (no `CACHE_ENABLED` / `SF_GUARD_ENABLED` env keys yet)
- No changes to `network_pulse.py` or any snapshot generator
- No callers of `MemoryAwareTTLCache` anywhere in the tree

**Blast radius on push:** zero prod behavior change. The module ships to prod unused. Import test in existing test suite may pick up the new module — if any imports break at collection time (unlikely, no side effects at import), rollback = `git revert 919f416`.

---

## Morning keyboard-pass checklist (for Charlie)

1. **Read the diff.** `cd ~/xrpldashboard && git show 919f416` (three files, all new — no modification review needed).
2. **Re-run tests locally.** `./venv_py311/bin/pytest tests/test_memory_aware_cache.py -v` — sanity that they still pass on your keyboard, not just mine.
3. **Push if satisfied.** `git push origin main` — Render auto-deploy fires. Watch Events tab for build success + Live transition (~3-5 min).
4. **Watch first 15 min post-Live:**
   - RSS per worker within ±30MB of pre-deploy baseline (nothing new is running, but confirm nothing regressed at import)
   - `/healthz` returns 200 from a good vantage (remember DNS wound — use `xrpldashboard.onrender.com` direct if in doubt)
   - Zero exceptions in gunicorn logs
   - No BetterStack pager fires

If all four hold: PR #1 GREEN. PR #2 (wiring) is next in the queue and awaits Section 10.5-10.7 rulings + your say-so to open.

---

## Rollback contract (PR #1 alone)

Because nothing is wired, the "rollback" for PR #1 is trivial:

```bash
git revert 919f416 && git push
```

No env-flip needed — there is no env-flip yet (that ships with PR #2). This is the safest possible ship shape.

---

## Where this fits in tonight's queue

- ✅ Rulings 10.1-10.4 received
- ✅ PR #1 staged (this card)
- ✅ Taft agenda + evidence packet landed (`docs/TAFT_AGENDA_2026-08-27.md`, `docs/TAFT_EVIDENCE_PACKET_MANIFEST.md`)
- ✅ Live-fetch amendments design landed (`docs/LIVE_FETCH_AMENDMENTS_DESIGN.md`)
- ✅ OAuth + credential inventory design landed (`docs/OAUTH_CREDENTIAL_INVENTORY_DESIGN.md`)
- ✅ Legibility-sweep /pools entry landed (`docs/LEGIBILITY_SWEEP_POOLS_2026-08-21.md` — refiled; see Correction 1 in `SATURDAY_QUEUE_2026-08-22.md`)
- ⏭️ Rolls to Saturday: real POOLS-COMETS pack (live-mode graphic upgrade — Phase A/B/C), anchor schema v4 filed version, legibility 13 (entries 2-13), contact-filter sketch, revival block (DNS + dock + session continuity + autonomy investigation)

---

## FULL MORNING AGENDA (Charlie's sitting, 2026-08-22)

**Ordered card. Do items in sequence — later steps assume earlier ones landed.**

### 1. Day 3 09:00 EDT one-liner (first, ritual)
7-day stability clock, Day 3 of 7 (restarted 2026-08-20). Bar: ≤2.5s median / ≤5s p95 / no cold-miss >5s cached. Reset classes: sovereignty_loss / prod 5xx on TRUST_CRITICAL / snapshot verify fail / non-outage ingest gap >24h.

### 2. PR #1 push + deploy watch (steps 1-4 above in "Morning keyboard-pass checklist")
- Read diff → local pytest → `git push origin main` → 15-min post-Live watch
- If all four watch conditions hold → PR #1 GREEN

### 3. Anchor ceremony Steps 4+5 (pipeline debut)
Commands already served in prior session. Pipeline debut. Commit + push after Steps 4+5 complete. Reference: `docs/ONLEDGER_ANCHOR_SPEC.md`, `docs/anchor_history.md`.

### 4. Section 10 deferred rulings (memory cache design)
Rulings 10.5-10.7 in `docs/PHASE2_MEMORY_AWARE_CACHE_DESIGN.md` §10. Ruling here unblocks PR #2 (wiring).

### 5. Live-fetch amendments rulings
See `docs/LIVE_FETCH_AMENDMENTS_DESIGN.md` §10 (rulings 10.1-10.6).

### 6. OAuth inventory deferred rulings
See `docs/OAUTH_CREDENTIAL_INVENTORY_DESIGN.md` §7 rulings 8.5-8.7 (WHOIS scope, TLS scope, dashboard surface).

### 7. Taft evidence-packet fill-in items
See `docs/TAFT_EVIDENCE_PACKET_MANIFEST.md` Tab 4 contract inventory — items only Charlie can fill (contracts, LLC docs, prior counsel correspondence).

### 8. Load Saturday queue
Read `docs/SATURDAY_QUEUE_2026-08-22.md` before drafting anything new. Two Friday-night corrections are captured there — anchor schema v4 and real /pools pack (live-mode graphic upgrade) are the two next design packs.

---

## Closing note

Six units landed to disk Friday overnight. 78% of the queue with every deadline-critical item done. Two assignment-blur drifts logged in `SATURDAY_QUEUE_2026-08-22.md` §Corrections as session-continuity evidence for Saturday's autonomy investigation.
