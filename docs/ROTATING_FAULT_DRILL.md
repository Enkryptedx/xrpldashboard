# Rotating fault drill — monthly schedule

**Standing filing.** Established 2026-08-16 following the 43-day
walker sovereignty loss (which no drill had tested because no drill
existed).

**Purpose**: one small, deliberate failure injection per month,
measured against how long it takes for a monitor to page. Same shape
as fire drills — expensive to run, but the alternative is discovering
the alarm is broken during a real fire.

**Cadence**: first Tuesday of each month, ~09:00 ET (aligns with
weekly anchor cadence for cross-witness).

**Discipline**: only ONE drill per month. Log the detection time to
`docs/fault_drills_log.md` (create when first drill runs). If
detection time exceeds monitor's advertised window, that's a
regression to file.

---

## Next drill (Tuesday 2026-09-01)

**Kill the local rippled cold. Time to L1 detection.**

The 43-day-replay drill. Explicitly test the exact class of failure
that ran undetected 2026-07-04 → 2026-08-16.

Steps:
1. Note the wall-clock time.
2. On Lenovo: `sudo systemctl stop rippled` (or on Mac if we're
   testing Mac-side).
3. Wait.
4. Note when L1 pager fires either `walker_stale` or
   `sovereignty_loss` (Mac-hosted walkers should trigger
   sovereignty_loss within ~4-6 hours per the new check).
5. On restart: `sudo systemctl start rippled`. Wait for
   `walker_health` to green.
6. Log to fault_drills_log.md: drill name, start ts, first-alert ts,
   restore ts, expected vs actual detection window, notes.

**Expected**: <6h detection via sovereignty_loss check (introduced
2026-08-16, this is its first live-fire test).

**Rollback if drill goes sideways**: `sudo systemctl start rippled`
restores everything. Walkers auto-resume next tick.

---

## Twelve-month rotation

| Month | Drill | Rationale |
|---|---|---|
| 2026-09 | Kill Mac's local rippled cold | 43-day-replay — sovereignty_loss check first live-fire |
| 2026-10 | Rotate Ed25519 snapshot signing key | Untested rotation drill per SIGNING_KEY_LIFECYCLE.md; verify old-key attestation + on-ledger anchor lands |
| 2026-11 | Restore from B2 backup to a scratch host | Backup-restore drill; verify DB restore integrity — untested since inception per 08-12 audit |
| 2026-12 | Pull the WARP VPN mid-run of a walker cycle | Simulate the exact 2026-08 failure mode from the other direction |
| 2027-01 | Corrupt one row in `signed_snapshots` | L2 signature check should page next nightly run |
| 2027-02 | Kill `mcp_server` for 5 minutes | BetterStack 60s heartbeat should page within ~2min |
| 2027-03 | Fill Mac disk to 95% (temp files) | Once resource_watchmen gauge exists, verify page fires at 80% |
| 2027-04 | Pull DNS record for xrpldashboard.com for 60s | Cert/domain gauge (once shipped) should catch |
| 2027-05 | Force `walker_health.consecutive_failures` to 5 via manual UPDATE | Verify L1 pager `walker_failing` check trips at ≥3 |
| 2027-06 | Withhold anchor for one Friday (skip the ceremony) | Anchor canary `anchor_stale` should page within 24h |
| 2027-07 | Fake a `root_mismatch` between on-chain and live-site chain.json | Anchor canary's forgery tripwire — highest-stakes test |
| 2027-08 | Kill Lenovo's power for ~15min | 2026-08-14 power-outage replay; verify BIOS auto-restart + walker resume behavior |

After 2027-08 the rotation loops back to 2026-09's kill-rippled drill;
by then we should have a year of fault-drill history to compare.

---

## Drills we're NOT running

- **Kill Neon**: too invasive; Neon-side outage already tested us
  involuntarily on 2026-05 (writer_connect_failed logs preserved).
- **Kill Cloudflare tunnel**: 8-30-second detection is not worth the
  10min site-down window on a live production host. Test on scratch
  when we build one.
- **Kill signing key** (i.e. delete it): irreversible without going
  through full key rotation. Do this DURING the 2026-10 rotation
  drill, not as a separate test.

---

## Meta-drill: "when did we last test this?"

At each drill, ALSO note: which monitor has NOT been exercised in the
past 6 months? If any monitor's last positive exercise is >6mo old,
promote it to the next drill slot ahead of the schedule above.

---

*Filed 2026-08-16. Owner: whoever schedules the first Tuesday.*
