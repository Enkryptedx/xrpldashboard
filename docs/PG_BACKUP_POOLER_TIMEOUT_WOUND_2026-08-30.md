# pg_backup pooler statement_timeout wound — 2026-08-30

**Filed:** 2026-08-30 17:35 EDT (Sunday triage close-out after full backup verified green).
**Bound:** Diagnosis + fix verification + sibling scope tracking. Primary fix already shipped (def145d) and validated end-to-end. Sibling wound (B) and adjacent wound (C) are recorded here with in-file queue anchors so next-steps travel with the wound.
**Sibling files:**
- Commit `def145d` — pg_backup: direct endpoint + statement_timeout=0 for dumps (the fix)
- Commit `42f2668` — restore_test bucket-prefix drift fix (PRIOR pg_restore_test wound, CLOSED)
- `docs/WALKER_WOUNDS_2026-08-22.md` (wound-file convention)
- `docs/FOUR_AM_WINDOW_DIAGNOSIS_2026-08-22.md` §3.4 (prior restore_test SoT history)
- `~/.claude/projects/-Users-charliebruce--openclaw-workspace/memory/feedback_writer_reader_shared_source_of_truth.md`

---

## Wound A — pg_backup killed by pooler-injected 25s statement_timeout ceiling (CLOSED)

### Evidence

Sat 2026-08-29 22:00 EDT nightly dump — `~/xrpl_test/launchd_logs/pg_backup.2026-08-29.log`:

```
pg_dump: error: Dumping the contents of table "nft_activity" failed:
  PQgetCopyData() failed: server closed the connection unexpectedly
pg_dump: error: Dumping the contents of table "events" failed:
  PQgetResult() failed: ERROR: canceling statement due to statement timeout
```

Two tables aborted mid-COPY. rc=1. No B2 upload. Sun 2026-08-30 morning BetterStack `pg_backup_canary` missed-heartbeat fired (~08:00 EDT).

### Class

**Pooler-invisible policy override.** Neon PgBouncer's `-pooler` endpoint enforces a server-side `statement_timeout=25s` at connection setup AND rejects the client's startup `options=` parameter — so a client-side `PGOPTIONS='-c statement_timeout=0'` never reaches the backend. Pooled connections silently inherit the pooler's timeout policy, even when the client thinks it opted out. Full-table dumps of `nft_activity` (largest) and `events` (second-largest) crossed the 25s budget.

### Autopsy chain

1. **Sat 22:00 EDT** — dump ran through `postgresql://…-pooler…` (from `~/.config/xrpldashboard/env`'s `DATABASE_URL`). PGOPTIONS override swallowed by pooler. Two big tables hit 25s ceiling → COPY aborted → dump rc=1 → no B2 upload.
2. **Sun ~08:00 EDT** — `pg_backup_canary` detected staleness of latest B2 dump (>25h), paged BetterStack.
3. **Sun triage — smoking-gun probe.** `pg_stat_activity` on the live backend during a retry showed `wait_event=ClientWrite`. Server was blocked writing bytes to the client, not stuck on the backend. **Downstream throttle, not backend contention.**
4. **Sun probe — bare pg_dump throughput.** Ran naked pg_dump → local file, no rclone in the pipe. Rate ~1.2-1.6 MB/s sustained. pg_dump CPU ~25%. Not CPU-bound — network-bound. **Neon→Mac SSL throughput is the natural ceiling.**
5. **Root cause chain confirmed** — pooler injects 25s statement_timeout ceiling AND rejects client override. Big tables at ~1.5 MB/s take longer than 25s. Pooler cancels the query mid-COPY.
6. **Fix — `def145d`.** `launchd/run_pg_backup.sh` derives `DUMP_URL` by stripping `-pooler.` from `DATABASE_URL`, connecting to the direct endpoint that honors PGOPTIONS. Prefixes `pg_dump` with `PGOPTIONS='-c statement_timeout=0'`. `DATABASE_URL` itself untouched — every other caller keeps pooler + 25s protection.
7. **Verification.** Full backup 2026-08-30 14:48:51 → 15:51:41 EDT. Log tail: `ok size=5998211932 bytes duration=3766s` = 6.0 GB in 62.8 min = ~1.6 MB/s (matches probe). Prune ok (kept 16, deleted 1). rc=0. `rclone lsl` confirms file on B2 with exact byte-match. `pg_backup_canary` re-run rc=0, `betterstack ping ok`, BetterStack auto-resolved on 20:49:00Z ping. Post-mortem comment posted on the resolved incident.

### Symptom to user

Nightly dump silently under-covers largest tables. If B2 restore ever needed, `nft_activity` and `events` would be missing (COPY aborts partway per pg_dump -Fc semantics). Silent because the pooler kill masquerades as a client disconnect rather than a policy rejection.

### Fix + runtime

- **Fix:** `def145d` (single-file change, `launchd/run_pg_backup.sh`).
- **Runtime shipped:** 2026-08-30, verified 15:51 EDT.
- **Expected duration going forward:** ~60-65 min per nightly dump at current Neon→Mac throughput (~1.6 MB/s for ~6 GB compressed custom-format).
- **Next scheduled fire:** 22:00 EDT tonight (same launchd plist, unchanged).

**Status:** CLOSED.

---

## Wound B — Sibling: canary DB writer connection failing on the pooler (OPEN)

### Evidence

`bash ~/xrpl_test/launchd/run_pg_backup_canary.sh` — 2026-08-30 20:48:57Z output:

```
[db] writer_connect_failed: OperationalError: connection failed:
  connection to server at "18.216.137.125", port 5432 failed:
  ERROR: unsupported startup parameter in options: statement_timeout.
  Please use unpooled connection or remove this parameter from the startup package.
```

Fires twice per canary run.

### Class

**Suspected under-scoped fix, mechanism UNVERIFIED.** `def145d` addressed pg_dump specifically. Prior commit `fdfd851` added `SET statement_timeout='25s'` to db.py at the session level after connect — **poolers usually tolerate session-level SETs**, so the pooler error message here (`unsupported startup parameter in options: statement_timeout`) does NOT cleanly match `fdfd851`'s mechanism. Something else is putting `statement_timeout` into the startup packet — could be a driver default, a different code path, or an earlier connection helper. **Do not name the cause until reproduction confirms it.**

### Symptom to user

`walker_health` row for `pg_backup_canary` doesn't advance. This is a dashboard/telemetry surface. The canary's pager path (BetterStack HTTP ping) works fine — canary rc=0 based on B2 freshness verdict, ping lands, incident auto-resolves. Freshness monitoring is not affected.

### Queue anchor — Monday design session (OPEN)

**Step 1 (before naming any cause):** Reproduce the exact `writer_connect_failed` error in isolation — bare psycopg2/psycopg3 connect to the pooler URL from the canary's Python process. Capture the connection args. Match against actual startup packet (`log_min_messages=debug1` or wireshark).

**Step 2:** Once mechanism confirmed, either (a) strip `statement_timeout` from the startup packet on the pooler-routed path, or (b) route canary DB writes to the direct endpoint like the backup script, or (c) something else the reproduction reveals.

**Step 3:** Full audit — enumerate every db.py caller and its connection topology (pooler vs direct). Any other caller silently broken? (Web app? Other walkers? tools/*.py?)

**Do not skip Step 1.** Wound A's lesson (iii) applies to Wound B — autopsy before kill, reproduce before name.

**Status:** OPEN. Not blocking (freshness + pager work); Monday session slot.

---

## Wound C — Adjacent: pg_restore_test BetterStack ping failing on SSL cert-verify (OPEN)

### Evidence

`~/xrpl_test/launchd_logs/pg_restore_test.out.log` tail — last two runs (2026-08-23 and 2026-08-30 07:00 EDT):

```
[restore_test] duration: 727.0s
[restore_test] result: PASS
[restore_test] betterstack ping failed: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]
  certificate verify failed: unable to get local issuer certificate (_ssl.c:1081)>
  (restore_test still PASS)
```

**Drill itself is HEALTHY AND PROVEN:**
- Today's scheduled Sun 07:00 EDT run PASSED (2026-08-30 11:00:03 UTC start, 727.0s, 5.3 GB restored, 5/5 smoke checks OK — token_prices count/age, events count, unl_snapshots count/age).
- Fired ON TIME amid this morning's pg_backup chaos (independent of pg_backup outage — restores from B2, restore target unaffected).
- Previous run (2026-08-23) also PASSED with same signature.

### Class

**BetterStack heartbeat-delivery plumbing wound, NOT a DR coverage wound.** DR drill runs green; the ping-back to BetterStack fails on Python SSL cert-verify (`unable to get local issuer certificate`). BetterStack therefore never sees the "restore succeeded" heartbeat and eventually flags the monitor as missed. Incident visible in BetterStack Incidents list as "6 days ongoing" is a **stale visibility artifact of the broken ping**, NOT evidence of restore failure.

**Do NOT confuse with the PRIOR pg_restore_test wound** (bucket-prefix drift `PG_BACKUP_*` vs `BACKUP_*`, closed in `42f2668`). That was a real DR-dark wound; it is fully resolved. Documenting explicitly here because the outline draft misdiagnosed this wound as SoT drift — do not resurrect.

### Symptom to user

BetterStack shows `pg_restore_test` incident ongoing while the actual DR drill is passing weekly. Alert noise, false-negative visibility, obscures real regressions if they happen.

### Queue anchor (OPEN)

**Fix path (a):** Repair the Python SSL cert store used by the restore_test wrapper. Options:
- `pip install --upgrade certifi` in the venv that runs restore_test + point Python at it.
- Or use `SSL_CERT_FILE` env pointing at `/etc/ssl/cert.pem` (macOS system) or `certifi.where()`.
- Or swap `urllib` for `requests` (which uses certifi bundle by default).

**Fix path (b) — already resolved today:** Verify today's scheduled Sun 07:00 run fired amid the backup chaos — **CONFIRMED via log, PASS, 727.0s, no interference.**

**Status:** OPEN. Not blocking (drill genuinely passing); own triage session owed. Low urgency — DR posture is proven, only the ping-back is broken.

---

## Durable lessons

### (i) `wait_event=ClientWrite` = look downstream

When `pg_stat_activity` shows a query with `wait_event=ClientWrite`, the server is blocked writing bytes to the client. The bottleneck is network/client-side, not the backend. Do NOT diagnose "query is slow"; diagnose "downstream pipe is slow" — network path, client-side buffering, or (as here) a client throughput ceiling that's now visible because something upstream (statement_timeout removal) stopped hiding it.

### (ii) Pooled connections inherit pooler policy invisibly

Neon PgBouncer's `-pooler` endpoint enforces `statement_timeout=25s` at connection setup AND rejects the client's startup `options=` parameter. A client's `PGOPTIONS='-c statement_timeout=0'` is silently swallowed when connecting through the pooler. Session-level `SET statement_timeout=0` after connect *may* work (pooler lets session SETs through); startup-time `options=` does NOT.

Rule: **when using a pooler, verify which client-side connection parameters actually reach the backend.** Any fix that changes startup-packet parameters via `options=` while pointed at a pooler will fail with `unsupported startup parameter in options`. Fix requires either direct endpoint OR post-connect session-level SET.

### (iii) Autopsy before kill

Sun morning the long-running pg_dump at 40+ min looked wedged. Correct move was the `ps` CPU check that revealed pg_dump at 30% CPU + rclone at 8% CPU — processes actively working, just slow. Rule: **before killing a long-running process, take vitals** (CPU %, network state, DB session state). A slow-but-healthy pipeline looks identical to a wedged pipeline under `ps -o etime` alone.

Corollary applied to Wound B in this same session: **before naming a cause, reproduce the failure.** Sibling of `feedback_verify_script_architecture_before_verdict.md`.

### (iv) fix_scope_enumerate_callers — enumerate callers when changing shared connection helpers

When changing db.py connection helpers, driver defaults, or connection-string handling: enumerate every caller and verify each caller's connection topology (pooler vs direct) still works. Ship the audit as part of the fix, not as a follow-up. The under-scoped fix silently leaves siblings broken (Wound B here is the standing exhibit until reproduced).

Sibling of `memory/feedback_verify_claimed_side_effect_before_banking_as_bonus.md` — same discipline extended from side-effect claims to caller-scope claims.
