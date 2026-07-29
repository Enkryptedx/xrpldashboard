# Monitor Audit — July 2026

**Ordered:** 2026-07-29 after three monitor failures in seven days.  
**Scope:** Every piece of code whose job is watching something else.  
**Method:** File census → three-question interrogation → fix registry → watch-chain.

---

## Why This Exists

Three distinct lie-shapes caught by accident in one week:

| Incident | Lie shape | Caught by |
|----------|-----------|-----------|
| is_bot_writer dead 2.7d | False-green: lockdir clean-exit skipped all work, walker_health.ok stayed True | Cross-evidence (canary delta) |
| walker_health SSL drops | False-green: Neon SSL exception swallowed, write dropped silently | Manual check |
| pg_backup_canary false-red 3d | False-red: `--order-by name,desc` returns oldest in rclone v1.74.1, blamed healthy pipeline | B2 dashboard ground-truth |

All three were caught by accident or external cross-check — not by design. This audit codifies the finding class and maps every monitor against it.

---

## Three-Question Test (Standing Rubric)

Every monitor — existing and new — must answer these three questions. New monitors must include answers in a header comment before they ship.

**Q1 — False-green:** If the watched thing dies, does this monitor DEFINITELY go red — or does some crash / skip / exception / early-exit path leave it green? Trace the code paths. Do not reason from intent.

**Q2 — False-red / ground-truth:** Can the monitor's own verdict logic be wrong, independent of the watched thing? Name the one-line check that validates it against independent reality right now.

**Q3 — Who watches it:** If this monitor itself crashes, hangs, or silently stops being scheduled — what notices, and how fast? Silence-is-green is the killer default.

**Verdicts:** CLEAN / SUSPECT(Q#, mechanism) / CONFIRMED-LIAR(describe exactly)

---

## Census — Count

**11 monitors across 6 families** (2026-07-29 census date)

| Family | Count | Members |
|--------|-------|---------|
| Dedicated canary scripts | 3 | pg_backup_canary, pg_restore_test, is_bot_canary |
| Plausibility rules (answer_plausibility_walker) | 4 live / 4 unbuilt | R1, R2, R4, UNDECLARED_WALKER (live); R3, R5, R6, R7 (designed-unbuilt) |
| Cross-check pairs (cross_check_walker) | 6 | CC-1…CC-6 |
| Walker health mechanism | 1 (24 instrumented walkers) | walker_health table + /walker-health + /healthz |
| Stream heartbeat | 1 | xrpl_stream → worker_heartbeat |
| External uptime | **1** | BetterStack → /api/heartbeat-age (real account, real monitor, 6 incidents since Jul 14) |

**Notable absences:** claims_check.sh is push-discipline, not a running monitor. BetterStack heartbeat monitors for supervision_walker/pg_backup_canary/pg_restore_test pending (Charlie creates in dashboard, wiring commit follows).

**Amendment 2026-07-29:** Initial census called BetterStack "phantom / not integrated." WRONG — integration is inbound (BetterStack polls purpose-built endpoint), not outbound (no API keys in code). Census method missed this shape. Account real, monitor real, catching real outages.

---

## Census Table

| ID | Monitor | Watches | Pass/Fail logic | Verdict lands | Cadence | If IT breaks |
|----|---------|---------|-----------------|---------------|---------|--------------|
| M-01 | pg_backup_canary | neondb-*.dump age in B2 | filename timestamp age ≤ 25h | launchd_logs only | Daily 04:00 | Nothing — log file only |
| M-02 | pg_restore_test | Dump restorability + smoke queries | restore + 5 smoke queries ok | launchd_logs only | Weekly Sun 04:00 | Nothing for 7 days |
| M-03 | is_bot_canary | is_bot column vs predicate baseline | trailing-7d + historical-week delta = 0 | walker_health row | Daily 06:00 | Walker row stale; health UI yellow after 48h |
| M-04 | is_bot_writer | page_views.is_bot classification | backfill + trigger cycle completes | walker_health row | Every 5 min | Walker row → consecutive_failures; health UI yellow |
| M-05a | R1 | Metric flat ≥7d when expected to wiggle | alarm in answer_plausibility_alarms | alarm table + /health | Every 10 min | Silently stops if walker stops |
| M-05b | R2 | Zero denominator with large numerator | alarm in answer_plausibility_alarms | alarm table + /health | Every 10 min | Silently stops if walker stops |
| M-05c | R4 | Monotonic supply violated | alarm in answer_plausibility_alarms | alarm table + /health | Every 10 min | Silently stops if walker stops |
| M-05d | UNDECLARED_WALKER | Walker missing from walker_health ≥4×cadence | alarm in answer_plausibility_alarms | alarm table + /health | Every 10 min | **Self-referential: cannot catch its own death** |
| M-06a | CC-1 rlusd_eth | rlusd_eth_supply vs Ethereum RPC | agree/disagree/external_unreachable | cross_check_results + walker_health | Every 10 min | Walker row; disagreements accumulate unalarmed |
| M-06b | CC-2 rlusd_xrpl | rlusd_xrpl_supply vs XRPL RPC | agree/disagree/external_unreachable | cross_check_results + walker_health | Every 10 min | " |
| M-06c | CC-3 xrp_price | Local price vs CoinGecko | agree/disagree/external_unreachable | cross_check_results + walker_health | Every 10 min | " |
| M-06d | CC-4 amendments | Local amendments vs XRPL feature RPC | agree/disagree/external_unreachable | cross_check_results + walker_health | Every 10 min | " |
| M-06e | CC-5 validator_unl | Local UNL vs UNL RPC | agree/disagree/external_unreachable | cross_check_results + walker_health | Every 10 min | " |
| M-06f | CC-6 ledger_vocab | Local vocab vs XRPL definitions | agree/disagree/external_unreachable | cross_check_results + walker_health | Every 10 min | " |
| M-07 | walker_health mechanism | ~24 instrumented walkers' freshness | age vs 2×cadence (yellow) / 4×cadence (red) | /walker-health UI + /healthz overall | Read on every /health request | If PG down: all reads fail together; /healthz may 503 |
| M-08 | xrpl_stream heartbeat | XRPL ledger stream alive (Mac→Render) | pg_hb_age < 900s (remote) or log < 600s (local) | worker_heartbeat → /healthz stream_alive | Every 5 min write; continuous | /healthz degrades to 503 after 900s — IF something polls /healthz |
| M-09 | claims_check.sh | CLAIMS.yaml entries resolve | exit 0 / non-zero on missing target | Stdout only (manual) | Manual pre-push only | Never runs; silent |
| M-10 | BetterStack → /api/heartbeat-age | xrpl_stream heartbeat freshness + PG reachability | 200 if hb_age < 600s AND PG reachable; 503 on any failure | BetterStack incidents dashboard → email/phone alert | 3 min checks (free tier) | BetterStack platform outage — no code fallback |

---

## Interrogation Verdicts

### M-01 · pg_backup_canary
**Q1 False-green?** No — it does go red.  
**Q2 False-red?** YES — CONFIRMED. `rclone lsf --order-by name,desc | head -1` returns *oldest* filename in rclone v1.74.1 (`--order-by` is a transfer-order flag, not an lsf sort). Caused false-red for 3 days (07-27→07-29). **Fixed in this audit**: replaced with `| sort -r | head -1`.  
Ground-truth: `rclone lsf b2crypt:.../postgres --files-only --include "neondb-*.dump" | sort -r | head -1`  
**Q3 Who watches it?** Nothing. Exit code to launchd log only. No walker_health row. No /healthz integration. No external poller.  
**VERDICT: FIXED(Q2-sort-bug) + SUSPECT(Q3-no-watcher)**

### M-02 · pg_restore_test
**Q1 False-green?** No — exceptions exit non-zero; no swallowed failure paths found.  
**Q2 False-red?** No — restore + smoke query logic is sound.  
**Q3 Who watches it?** Nothing for up to 7 days. Log-only. No walker_health write. No heartbeat.  
**VERDICT: CLEAN logic, SUSPECT(Q3-weekly-silence)**

### M-03 · is_bot_canary
**Q1 False-green?** YES. `is_bot_canary.py:165`: `if not db.pg_available(): sys.exit(1)` fires BEFORE `write_walker_health_start()`. Prior walker_health row stays at whatever it was — potentially ok=True — for up to 48h (2×86400s cadence threshold before UI turns yellow).  
Fix: move `write_walker_health_start()` before the pg_available check; on bail write `write_walker_health_end(ok=False, message="pg_unavailable at canary start")`. ~5 lines.  
Ground-truth: `SELECT COUNT(*) FROM page_views WHERE is_bot IS NOT TRUE AND ts >= ? AND ts < ?` vs legacy predicate; delta should be 0.  
**Q3 Who watches it?** walker_health row (liveness). No real-time alert. BetterStack not integrated.  
**VERDICT: SUSPECT(Q1-bail-before-start) + SUSPECT(Q3-no-alert)**

### M-04 · is_bot_writer
**Q1 False-green?** Lockdir skip was the founding failure — shipped fix. Residual: stale-lock recovery writes walker_health.ok=False via `_record_walker_failure.py`, but the wrapper uses `|| true` — if that write fails under PG pressure, is_bot_writer proceeds and can write ok=True in the same cycle, masking the gap. Small residual risk.  
Ground-truth: count `page_views WHERE is_bot IS NULL` — should approach 0 if backfill is advancing.  
**Q3 Who watches it?** is_bot_canary (correctness layer) + walker_health row (liveness). Layered.  
**VERDICT: CLEAN post-fix, minor residual SUSPECT(Q1-stale-lock-write-under-PG-pressure)**

### M-05a–d · answer_plausibility_walker rules
**Q1 False-green?** Walker itself: No — `write_walker_health_start()` fires before try block; exceptions → walker_health.ok=False. PG unavailable → reads return [] → rules process empty data → no false alarm. Correct.  
**Per-rule silent death?** Yes for individual rules: if a rule's internal query raises an exception, it's caught per-rule and the rule silently fires no alarm. The walker stays ok=True. The watched signal dies; no alarm.  
**M-05d self-referential gap:** UNDECLARED_WALKER catches walkers missing from walker_health — but if answer_plausibility_walker stops, UNDECLARED_WALKER stops evaluating. The rule cannot catch its own walker's death.  
Ground-truth: query `answer_plausibility_alarms WHERE created_at > now() - 2h` — R1/R2/R4 alarms should reflect current anomalies.  
**Q3 Who watches it?** walker_health row. The UNDECLARED_WALKER rule is self-referential (see above). No external monitor.  
**VERDICT: CLEAN(Q1 walker), SUSPECT(Q1 per-rule silencing, Q3 self-referential)**

### M-06a–f · cross_check_walker pairs
**Q1 False-green?** No — pair exceptions caught per-pair and written as `status='local_unavailable'`; walker ok=True reflects walker liveness not pair agreement, which is correct design.  
**Q2 False-red?** Pairs could flag false disagreement if external source has flash lag. No persistent disagreement threshold exists to suppress transient.  
Ground-truth: query `cross_check_results WHERE pair_key = ? ORDER BY created_at DESC LIMIT 5` — check status and delta.  
**Q3 Who watches it?** walker_health row (liveness). Persistent disagreements accumulate in cross_check_results but no alarm fires. A pair could disagree for days without anyone noticing.  
**VERDICT: CLEAN(Q1) + SUSPECT(Q3-disagreements-silent)**

### M-07 · walker_health mechanism
**Q1 False-green?** If Neon SSL errors hit during `write_walker_health_end()`, the write drops and the row stays at prior state. The exception-sweep fix addressed explicit except-pass patterns, but the write itself can fail under PG degradation. Affects all 24 rows simultaneously.  
Ground-truth: `SELECT walker_name, last_run_ok, last_run_started, now() - last_run_started AS age FROM walker_health ORDER BY age DESC LIMIT 5`  
**Q3 Who watches it?** /healthz overall flag. walker_health reads are the mechanism — if PG is down, the mechanism itself is blind.  
**VERDICT: SUSPECT(Q1-PG-degradation-drops-all-writes) + SUSPECT(Q3-/healthz-unpolled)**

### M-08 · xrpl_stream heartbeat
**Q1 False-green?** YES. `xrpl_stream.py:1051`: watchdog fires `os._exit(1)` — hard exit, bypasses all finally blocks. Final heartbeat write is skipped. /healthz stays green for up to 900s after watchdog kill. This is a 15-minute false-green window on the most critical single process.  
Fix: replace `os._exit(1)` with a flag + graceful exception that triggers finally blocks, writes final heartbeat with `"idle_kill"` note before dying. ~10 lines. Needs spec before building — xrpl_stream is the highest-risk edit target.  
Ground-truth: `SELECT ts, now() - to_timestamp(ts) AS age FROM worker_heartbeat WHERE worker LIKE 'xrpl_stream%'`  
**Q3 Who watches it?** /healthz stream_alive goes to 503 after 900s. No confirmed external poller. launchd KeepAlive=true on Mac restarts within ~2min. **On Render: no auto-restart on crash; deployment is manual.**  
**VERDICT: SUSPECT(Q1-15min-false-green) + SUSPECT(Q3-Render-no-restart, /healthz-unpolled)**

---

## Watch-Chain Diagram

The chain must terminate outside the Mac. Right now it doesn't.

```
                    EXTERNAL (outside Mac)
                    ┌─────────────────────────────────────────┐
                    │  BetterStack (NOT YET INTEGRATED)       │
                    │  → polls /healthz every 30s             │
                    │  → pages Charlie on 503 or silence       │
                    └──────────────┬──────────────────────────┘
                                   │ (gap: not wired)
                    ┌──────────────▼──────────────────────────┐
                    │  /healthz (Render)                       │
                    │  overall = ok | degraded (503)           │
                    │  → stream_alive (M-08)                   │
                    │  → scan_alive (cadence-based)            │
                    │  → mirror_alive (ranker heartbeat)       │
                    └──────────────┬──────────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────────┐
                    │  /walker-health (Mac)                    │
                    │  walker_health table → 24 rows           │
                    │  freshness thresholds: 2×cad=⚠ 4×cad=🔴│
                    │  Monitors: M-03, M-04, M-05, M-06, M-08 │
                    └──────────────┬──────────────────────────┘
                                   │
              ┌────────────────────┼────────────────────────────┐
              │                    │                            │
 ┌────────────▼──────┐  ┌──────────▼────────┐  ┌──────────────▼─────┐
 │ M-05               │  │ M-03               │  │ M-01 / M-02        │
 │ answer_plausibility│  │ is_bot_canary      │  │ pg_backup_canary   │
 │ walker             │  │                    │  │ pg_restore_test    │
 │ → alarm table      │  │ Q1 gap: pre-start  │  │                    │
 │ → /health alarms   │  │ bail leaves stale  │  │ M-01: FIXED sort   │
 │ Q3: self-referential│  │ green row          │  │ M-01 Q3: no row,   │
 │ (UNDECLARED_WALKER │  │                    │  │ no watcher         │
 │  can't catch own   │  │                    │  │ M-02 Q3: weekly,   │
 │  death)            │  │                    │  │ log only           │
 └────────────────────┘  └────────────────────┘  └────────────────────┘

OUTER RING CHAIN (written explicitly):
  Charlie ← [email/Telegram] ← BetterStack ← /healthz 503
  /healthz degrades when: stream_alive=False OR scan_alive=False OR mirror_alive=False
  stream_alive=False when: pg_hb_age ≥ 900s AND stream_log_age ≥ 600s
  pg_backup_canary / pg_restore_test: NOT in /healthz chain (log-only today)

STATUS: Outer ring is designed but NOT CONNECTED.
        BetterStack → /healthz polling does not exist in code or config.
        Everything above /healthz is only reachable by Charlie manually checking Telegram.
```

---

## Monitor-of-Monitors Spec

**Goal:** One daily check that every monitor *ran* within its expected cadence — ran, not passed. A dead watcher must be loud.

**Minimal design:** `supervision_walker.py`

```
Cadence: daily at 02:00 (before pg_backup_canary 04:00 and is_bot_canary 06:00)
Writes: answer_plausibility_alarms (status='supervision', severity=error)
         walker_health row (own liveness)

For each walker_health row:
  if now() - last_run_started > cadence_seconds * 1.5:
    fire alarm: "{walker_name} missed expected cadence
                 (last_run={last_run_started}, expected every {cadence_seconds}s)"

For pg_backup_canary (no walker_health row today):
  read latest launchd_logs/pg_backup_canary.YYYY-MM-DD.log
  if log date < today: fire alarm "pg_backup_canary has no log for today"

For pg_restore_test (weekly):
  read latest launchd_logs/pg_restore_test.*.log
  if log date > 8 days ago: fire alarm "pg_restore_test missed weekly window"

Who watches supervision_walker?
  → walker_health row (own staleness → UNDECLARED_WALKER rule in M-05d)
  → BUT M-05d is self-referential if answer_plausibility_walker also stops

Breaking the regress:
  BetterStack polls /healthz every 30s. If /healthz 503s (answer_plausibility_walker
  stops → supervision alarms pile up → /healthz overall degrades to 503 → BetterStack
  pages Charlie). This chain works IF BetterStack is wired.

  If supervision_walker itself dies:
    Its walker_health row goes stale → UNDECLARED_WALKER fires (if APW is alive)
    → /healthz alarm → BetterStack pages Charlie.

  If BOTH supervision_walker AND answer_plausibility_walker die simultaneously:
    /healthz overall checks stream_alive + scan_alive + mirror_alive.
    stream_alive = pg_hb_age < 900s (xrpl_stream heartbeat, independent of APW).
    So: xrpl_stream death still reaches BetterStack even with both walkers down.
    Double-walker death without stream death: /healthz stays 200, silence.
    Outer ring: human (Charlie) manually checks /walker-health within SLA.

REGRESS STOP: BetterStack is EXTERNAL (off-Mac). Its polling of /healthz is the
termination point. Every chain above eventually reaches /healthz 503 → BetterStack.
The only undetected failure is: everything above /healthz appears healthy while
actual data is wrong (business logic failure, not liveness failure) — that's
answer_plausibility_walker's job, not supervision_walker's.
```

**Ship gate:** supervision_walker spec is approved before building. BetterStack must be wired to /healthz before supervision_walker ships (otherwise the outer ring termination is a lie).

---

## Fix Registry

### Shipped this audit session
| Fix | File | What | Commit |
|-----|------|------|--------|
| Sort-bug fix | run_pg_backup_canary.sh | `--order-by name,desc` → `\| sort -r \| head -1` | TBD |
| Retention policy | run_pg_backup.sh | Replace 30d rolling with 14 nightly + 3 monthly Python prune | TBD |

### GO-approved, ship next
| Priority | Fix | File | Lines | Risk |
|----------|-----|------|-------|------|
| Q1 | is_bot_canary bail-before-start | is_bot_canary.py | ~5 | Low |
| Q3 | pg_backup_canary walker_health row | run_pg_backup_canary.sh | ~4 | Low |
| Q3 | pg_restore_test walker_health row | pg_restore_test.py (or wrapper) | ~4 | Low |

### Spec needed before building
| Priority | Fix | Reason |
|----------|-----|--------|
| Q1 | xrpl_stream watchdog graceful exit | os._exit(1) change is high-risk; needs spec + careful testing |
| Q3 | BetterStack → /healthz integration | External config + code change; Charlie owns API key |
| Q3 | cross_check_walker disagreement alarm | New rule (R5), new alarm shape, needs design decision |
| Structural | supervision_walker | Spec above; ships after BetterStack wired |

### Standing decisions (no build)
- claims_check.sh: push-discipline only is acceptable. Gap acknowledged. Adding to CI would be the upgrade path if CI ever exists.
- Designed-unbuilt rules (R3, R5–R7): build when their trigger condition is first observed. Not preemptively.

---

## Standing Rubric — New Monitor Checklist

Every new monitor ships with a header comment answering the three questions:

```bash
# Q1 FALSE-GREEN: [describe what failure path could leave this green when watched thing is dead]
# Q2 GROUND-TRUTH: [one-line command to validate verdict against independent reality]
# Q3 WHO-WATCHES: [what detects if this script stops running, and how fast]
```

This rubric applies to: canary scripts, walker rules, heartbeat writers, health endpoints. If the answer to Q3 is "nothing" — that's a known gap, not an acceptable default.

---

*Audit initiated: 2026-07-29 · Founding incidents: is_bot_writer lockdir, walker_health SSL drop, pg_backup_canary sort bug · Next review trigger: any new CONFIRMED-LIAR finding*
