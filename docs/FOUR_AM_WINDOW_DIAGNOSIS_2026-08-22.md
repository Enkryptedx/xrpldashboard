# The 4 AM Window — Series Diagnosis (2026-08-22)

**Filed:** 2026-08-22 06:45 EDT (Saturday morning, in-turn)
**Author:** JJ 🦞
**Bound:** Diagnosis only per Charlie's ruling. No fixes, no builds, no config changes today. Fix design goes to Saturday revival block item #4.
**Sibling files:** `docs/SATURDAY_QUEUE_2026-08-22.md`

---

## 1. The observation

BetterStack has fired an incident nearly every morning in the same ~3:30-4:55 EDT band. Series named by Charlie:
- Aug 17 — 33-flap loop (~4 AM window)
- Aug 19 — flap-storm at 04:07 / 04:15 / 04:24 EDT
- Aug 20 — heartbeat blips (his memory: 04:07 + two more; my log data cannot independently confirm those specific times — Aug 20 was the storm/FileVault event, see §4)
- Aug 22 today — endpoint 503 at ~03:55 EDT

Web-tier fixes (connection shield, `connect_timeout=15`, `pg_connect` sweep, gunicorn `--timeout 30`) stopped the Neon-cold-start FLAP class but something is still firing in this clock window, now surfacing through different surfaces.

## 2. The schedule overlay — what fires in 02:00-06:00 ET

Enumerated from every `~/Library/LaunchAgents/com.charliebruce.xrpldashboard.*.plist`:

| Time (ET) | Job | Load class |
|-----------|-----|------------|
| 02:07 | daily_snapshot | writes to Postgres |
| 02:23 | unl_snapshot | small write |
| 03:00 | b2_backup | rclone sync of `~/xrpl_test` to B2 (30s duration, no DB touch) |
| **03:30** | **pg_backup** | **`pg_dump` of full Neon DB — 5.04 GB, ~82 min sustained heavy READ** |
| 04:00 | pg_restore_test | pull latest dump, restore to local Postgres, run smoke queries (~9 min when it succeeds; not firing daily currently — see §3.4) |
| 06:00 | is_bot_canary | small read |
| 06:00 | pg_backup_canary | small read |

Nothing in the 03:30-04:55 window besides pg_backup carries meaningful DB load.

## 3. Evidence

### 3.1 pg_backup timing — verified from launchd logs

Precise durations by wrapper-logged UTC timestamps (`launchd_logs/pg_backup.YYYY-MM-DD.log`):

| Date | Start (UTC) | End (UTC) | Duration | Window EDT |
|------|-------------|-----------|----------|------------|
| Aug 15 | 07:30 | 08:08 | 38 min | 03:30 → 04:08 |
| Aug 16 | 07:30 | 08:20 | 50 min | 03:30 → 04:20 |
| Aug 17 | 07:30 | **08:56** | **86 min** | 03:30 → 04:56 |
| Aug 18 | 07:30 | 08:23 | 53 min | 03:30 → 04:23 |
| Aug 19 | 07:30 | **08:55** | **85 min** | 03:30 → 04:55 |
| Aug 20 | (skipped — FileVault loginwindow gap per prior memo, manual kick at 13:18) | — | — | not running that morning |
| Aug 21 | 07:30 | 08:32 | 62 min | 03:30 → 04:32 |
| Aug 22 | 07:30 | **08:52** | **82 min** | 03:30 → 04:52 |

Today's dump was 5,042,548,305 bytes (5.04 GB) — grows with `amm_pool_events`, `ledger_stream_txns`, and history-writing walkers.

### 3.2 Incident-vs-backup overlap

| Date | Incident time | pg_backup running? | Position in dump window |
|------|---------------|--------------------|-----|
| Aug 17 | ~04 AM ET (33-flap) | YES (86 min day) | mid-dump |
| Aug 19 | 04:07 / 04:15 / 04:24 EDT | YES (85 min day) | mid-dump, all three fires inside the window |
| Aug 20 | Charlie's memory: 04:07 EDT + more (I can't independently confirm times) | **NO — pg_backup skipped due to FileVault gap** | — |
| Aug 22 | ~03:55 EDT (email received time) | YES (82 min day) | 25 min in |

**Every incident in the series that I can time-stamp precisely from disk falls INSIDE the pg_backup window. Aug 20 is the exception — pg_backup did not run that morning, and the recovery pattern that day was different (post-login walker resumption storm, not a coldstart flap).**

### 3.3 walker_health corroboration

Because `walker_health` is a singleton (PK on `walker_name`), it only holds the MOST RECENT failure per walker. But even that fragmentary view is aligned:

| Walker | Last failure (UTC) | EDT | Position vs pg_backup |
|--------|--------------------|-----|-----------------------|
| oracle_walker | 2026-08-18 07:30:11 | 03:30 | **pg_backup start moment (07:30 UTC)** |
| is_bot_writer | 2026-08-16 08:03:20 | 04:03 | mid-dump (33 min in) |
| is_bot_canary | 2026-08-20 12:11:08 | 08:11 | Aug 20 = post-login recovery burst, different class |
| pg_backup_canary | 2026-08-11 10:00:07 | 06:00 | canary's own scheduled time |
| unl_snapshot | 2026-08-09 06:23:04 | 02:23 | unl_snapshot's own scheduled time |
| nft_activity_activity | 2026-08-13 02:43:19 | 22:43 (prev day) | not in-window |

Two of these (oracle_walker 03:30 exact, is_bot_writer 04:03 mid-dump) are unambiguous pg_backup-window failures on their own walker cycles. This is fragmentary because we only see one row per walker, but the direction of evidence is one-way.

### 3.4 pg_restore_test — likely NOT contributing

- Plist says `H4:M00` daily
- `.out.log` shows only ONE recent successful run (2026-08-16 08:00:02 UTC)
- `.err.log` shows `FAIL: could not list dumps: No neondb-*.dump files found in b2crypt:xrpldashboard-backup-Charlies-Mac-mini/postgres` and `...backup-Mac/postgres`
- Bucket prefix mismatch (looks in `Charlies-Mac-mini` and `Mac`, but pg_backup writes to `Charlies-Mac-mini` currently)
- Behavior: fires at 04:00, immediately fails on "no dumps found," exits fast — no meaningful load on Neon
- Not a series contributor. **But a separate wound to file:** restore-test bucket-prefix drift = disaster-recovery drill silently broken since Aug 16.

### 3.5 Aug 20 is a distinct class

Per prior memory (`project_storm_power_event_2026-08-20.md`): Mac rebooted after 01:36 power event, sat at loginwindow behind FileVault until 08:10 EDT login. `StartCalendarInterval` LaunchAgents for pg_backup (03:30) and pg_backup_canary (06:00) silently skipped. BetterStack fires that morning happened AT 08:00-08:01 EDT — pg_backup missing-heartbeat and is_bot coincidental threshold-cross — NOT in the 03:30-04:55 window. Charlie's memory of "04:07 + two more" for Aug 20 may be conflating with Aug 19 (same clock band, adjacent date). Aug 20 is a separate mechanism — force-majeure exception with its own annotation.

## 4. Verdict

**Mechanism (named with evidence):**

`pg_backup` starts at 03:30 EDT and holds an 82-minute `pg_dump` session against Neon, sustaining heavy sequential reads across every table. This does two things to Neon under load:

1. **Compute autoscaling.** Neon's serverless architecture spins compute UP under sustained load and DOWN when idle. Autoscaling transitions can transiently reset connections that are opened during the transition window.
2. **Connection-slot pressure.** `pg_dump` opens its own connection(s). Combined with the Render web-tier's per-request fresh connections (heartbeat-age probe, walkers, `idx_conn` sweeps), Neon's connection budget on the current plan tier gets tight, and marginal connections fail with transient errors.

The Render web tier hitting `/api/heartbeat-age` during the dump window is the most exposed surface: it opens a fresh connection every probe, so a Neon flap during the ~5min BetterStack probe interval catches ONE failure → 503 → BetterStack fires. On non-backup days the same probe never sees a fresh-connection flap, because Neon is idle-warm.

**What ties together and what doesn't:**
- Aug 17 (33-flap), Aug 19 (04:07/04:15/04:24), Aug 22 (03:55) → **same class**, all inside pg_backup window, all Neon-connection flaps
- Aug 20 → **separate class**, power event + FileVault gap, pg_backup did not run
- Aug 18 03:30 oracle_walker failure → **same class**, evidence in walker_health
- Aug 16 04:03 is_bot_writer failure → **same class**, evidence in walker_health

**Ranked suspects (if the above verdict is wrong):**

1. **pg_backup on Neon** (PRIME — evidence-convicted above)
2. Neon-side maintenance window in the 07:30-09:00 UTC band (secondary — would need Neon dashboard confirmation Charlie can pull; if such a window exists it would be an amplifier not the trigger, since it would fire every day including Aug 20)
3. macOS system-level cron at ~03:15 (weak — no evidence in launchd logs; standard daily maintenance runs at 03:15 but doesn't touch DB)
4. pg_restore_test (ruled out §3.4)

## 5. Fix-design directions (SATURDAY REVIVAL BLOCK — do NOT touch today)

Filed for Saturday item #4. NOT for today. Options in rough order of impact/cost:

- **A. Reschedule pg_backup off-peak** — move to 05:30 or 06:30 EDT, out of the pre-morning BetterStack-hot window. Cheapest fix. Ships as plist edit only. But the load doesn't disappear, it just moves. And it needs to finish before Charlie's usual morning check.
- **B. Throttle pg_dump** — use `--jobs=1 --no-synchronized-snapshots` (already default for non-directory) or add `--exclude-table` for the biggest churn tables (would lose forensic value). Reduces Neon read pressure at the cost of longer duration.
- **C. Connection isolation** — separate Neon compute (Neon supports read replicas / branches). Point pg_dump at a read-replica branch, keep production compute clean. Correct fix, higher cost — requires Neon plan check.
- **D. Move pg_backup off the Mac** — run from Lenovo (or the dock, once online). Same Neon-pressure issue but decouples from Mac-local FileVault/power problems. Doesn't fix the pg_dump→Neon load.
- **E. Restore-test bucket-prefix repair** — separate from window fix, but same file cluster: `pg_restore_test` has been silently failing since Aug 16 (§3.4). Restore drill is dark. Ship a bucket-prefix correction as its own tiny PR when the window class is being addressed.

Charlie rules the shape Saturday. My gut without ruling: **A (reschedule) as the interim to buy quiet mornings + E (restore-test repair) alongside**, then **C (Neon branch) as the durable fix if plan allows**.

## 6. Owed-work registry

- **Restore-test bucket-prefix drift** — pg_restore_test silently broken since 2026-08-16, DR drill dark. NEW wound, file separately.
- **walker_health history table** — current singleton design makes series diagnosis fragmentary. Consider append-only history for `walker_health` failures. Post-cert.
- **BetterStack incident export** — no local mirror of BetterStack fires with timestamps means I had to reconstruct the series from Charlie's memory + walker_health rows. A weekly export/sync would make future series diagnoses evidence-driven from turn 1.

End of diagnosis.
