# Walker wounds — 2026-08-22

**Filed:** 2026-08-22 08:45 EDT (Saturday morning triage during morning card walkthrough)
**Bound:** Diagnosis + linkage only per Charlie's (a)+exception ruling. No fixes today unless Sunday-morning resync makes it a today problem.
**Sibling files:** `docs/SATURDAY_QUEUE_2026-08-22.md`, `docs/FOUR_AM_WINDOW_DIAGNOSIS_2026-08-22.md`

---

## Wound A — `is_bot_writer` deadlocking on Neon (NEW, TOMORROW-LINKED)

### Evidence

`~/xrpl_test/launchd_logs/is_bot_writer.out.log` (tail, this morning):

```
[db] ensure_is_bot_schema_failed: QueryCanceled: canceling statement due to statement timeout
[db] refresh_bot_hash_tables_failed: QueryCanceled: canceling statement due to statement timeout
[db] refresh_bot_hash_tables_failed: DeadlockDetected: deadlock detected
DETAIL:  Process 15611 waits for AccessExclusiveLock on relation 3307504 of database 16391;
         blocked by process 15586.
Process 15586 waits for AccessExclusiveLock on relation 3307504 of database 16391;
         blocked by process 15611.
[db] refresh_bot_hash_tables_failed: DeadlockDetected: deadlock detected
DETAIL:  Process 31883 waits for AccessExclusiveLock on relation 3307504 of database 16391;
         blocked by process 31880.
Process 31880 waits for AccessExclusiveLock on relation 3307504 of database 16391;
         blocked by process 31883.
```

### Class

**Concurrent self-deadlock.** Two `is_bot_writer` processes running simultaneously, each trying to grab AccessExclusiveLock on the same relation (`3307504` — needs oid→name mapping to confirm, but the pattern is unambiguous: same relation, both sides blocked on each other). Cycle repeated twice (processes 15611↔15586, then 31883↔31880) — not a one-off.

Sibling error class also present: `statement_timeout` (25s) canceling `ensure_is_bot_schema` and `refresh_bot_hash_tables` — either the DDL/refresh queries genuinely need more than 25s under lock contention, or they're piling up behind the deadlock cycle.

### Symptom to user

`is_bot` classifications stop being written / refreshed. Existing rows remain (the LANDMINE-safe writer stores `TRUE | NULL` only per `feedback_is_bot_reader_convention.md`, so absence-of-refresh doesn't corrupt reads). New bot signals go unclassified until the writer clears. Not user-visible on page loads; visible in the analytics reports if bots stop being flagged.

### **LINKAGE — Sunday-morning resync (TOMORROW 06:00 EDT)**

Pre-approved `BOT_CLASSIFIER_VERSION 3→4` full-table rewrite fires 2026-08-23 06:00 EDT (Sunday morning). That resync targets **the same table family** currently deadlocking. Question the Saturday triage MUST answer before 06:00 Sunday:

- **Is the resync safe to fire into a deadlocking writer?** — likely NO. A full-table rewrite requires an even stronger lock and would either (a) deadlock in the same cycle, (b) succeed by killing the running writer mid-transaction, or (c) fire cleanly if the writer happens to be idle at that moment (unpredictable).
- **What's the pre-req?** — resolve the deadlock class first. Options: single-process guard (only one is_bot_writer at a time), or serialize via advisory lock, or reschedule writer to a cadence that doesn't overlap the resync window.
- **If we can't fix by tonight:** postpone the 06:00 Sunday resync until Monday or later. A `BOT_CLASSIFIER_VERSION` bump that dies mid-rewrite is worse than a version-lag on a table.

**Bound decision: today's fix list gets a "deadlock investigation" slot before end of Saturday** if we want the resync to fire clean tomorrow. Deferrable if Charlie's willing to defer the resync too.

### Suspects (ranked, do NOT act today per Charlie's bound)

1. **Two concurrent instances** of `is_bot_writer` — plist has `StartInterval` short enough that runs overlap, OR a manual invocation ran while the scheduled one was in flight. Check plist cadence + look for a `RunAtLoad + KeepAlive` combination that could spawn twin instances.
2. **Long-running query holding lock** — `refresh_bot_hash_tables` may be doing full-table scans that exceed `statement_timeout=25s` under Neon compute load (esp. during the 03:30 pg_backup window — see `FOUR_AM_WINDOW_DIAGNOSIS_2026-08-22.md`).
3. **DDL churn** — `ensure_is_bot_schema` is a startup-idempotent DDL; if it's grabbing an exclusive lock on every walker cycle and colliding with refresh_bot_hash_tables in the same process pool, that's the collision point.

Fix design for Saturday afternoon: single-flight guard on the walker (advisory lock or pidfile), OR increase `statement_timeout` for this walker only, OR audit whether `ensure_is_bot_schema` needs to run every cycle.

---

## Wound B — `oracle_walker` log-location oddity (NON-CRITICAL, filed for hygiene)

### Evidence

`~/xrpl_test/launchd_logs/oracle_walker.out.log` — last-mtime **2026-08-19 13:02 EDT** (3 days stale). Final content is a historical error from before fix `321d738` (Neon-pooler-rejects-`options=`-in-startup-packet).

`~/xrpl_test/launchd_logs/oracle_walker.err.log` — actively writing, latest entry **2026-08-22 08:42:46 EDT** (2 min before this filing):

```
2026-08-22 08:42:46,467 INFO httpx HTTP Request: POST http://192.168.40.95:5005 "HTTP/1.1 200 OK"
2026-08-22 08:42:46,494 INFO httpx HTTP Request: POST http://192.168.40.95:5005 "HTTP/1.1 200 OK"
2026-08-22 08:42:46,916 INFO oracle_walker rows=2 accounts_ok=1/1 accounts_err=0
```

`launchctl list | grep oracle` → `- 0 com.charliebruce.xrpldashboard.oracle_walker` (loaded, last exit 0).

### Verdict

**ALIVE, healthy.** Feeds `/price-data` via `oracles_snapshot` (5-min TTL cache). DIA feed writing cleanly every 30 min. No stale data served.

### The oddity

Successful runs' `INFO`-level logs land in `.err.log` (stderr), not `.out.log` (stdout). Log config in `oracle_walker.py` sends its Python `logging` output to stderr, so any monitoring pattern that only tails `.out.log` for liveness will falsely conclude the walker is dead after ~30 min of silence. Since I made exactly that mistake in the pre-triage sweep, this pattern will bite the next diagnostic sweep too if not documented.

### Suggested fix (post-cert)

Two options, order of preference:
1. **Reconfigure the walker's logger to stdout** — one-line Python change; `.out.log` becomes the source of truth again; matches convention.
2. **Add a `.err.log` age-check to any walker-health sweep script** — defensive but leaves the log-stream oddity in place.

Option 1 is right. Cheap, no runtime risk, restores the naming contract.

---

## Not-a-wound: verified during triage

- `xrpl_stream` — heartbeat 2026-08-22 12:35:53 UTC, ledger 106467293, 201M+ txns_seen. GREEN.
- `daily_snapshot` — wrote `~/xrpl_test/historical_snapshots/2026-08-22.json` at 02:08 EDT this morning. GREEN. Matches chain.json `current_root` `c7f375928018e47a107ed29f4d0d7bebe47fa253d01181fbc26b1992d48f69d0`.
- Anchor #3 deploy (`f3a30e6`) — 6/6 `/healthz` probes clean, no BetterStack fires, guard-dormant grep clean (zero callers of `MemoryAwareTTLCache`).

## Owed-work registry

- **Wound A triage MUST run today** if we want Sunday-06:00 resync to fire clean tomorrow.
- **Wound B fix** goes to post-cert hygiene queue; not deadline-critical.
- **walker_health DB-side read** — attempt via `venv_py311` failed on `ModuleNotFoundError: psycopg2`; needs a working venv or `psql` invocation for a proper DB-side confirmation across all walkers (not just log-tail evidence). Small task, worth doing during the 4-AM-window fix design in the revival block.

End of filing.
