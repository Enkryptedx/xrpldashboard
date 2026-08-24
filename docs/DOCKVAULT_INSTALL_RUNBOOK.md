# DockVault A+D+Monitor — Install Runbook

Bundle staged 2026-08-23 per Charlie's Phase 4 ruling.
Charlie executes at keyboard. FDA-verify gate is BLOCKING per D6 doctrine.

---

## Files staged

| Path | Role |
|---|---|
| `launchd/dockvault_preflight.sh` | Shared helper — mount check + active write-test with 10s Perl-alarm timeout |
| `launchd/run_dockvault_mirror.sh` | A — rclone sync ~/xrpl_test + ~/.ssh to DockVault |
| `launchd/run_dockvault_neon_dump.sh` | D — rclone copy latest Neon dump from B2 to DockVault (1a-iii pull-back) |
| `launchd/run_dockvault_monitor.sh` | Monitor — active write-test → BetterStack heartbeat |
| `launchd/com.charliebruce.xrpldashboard.dockvault_mirror.plist` | 04:00 EDT daily |
| `launchd/com.charliebruce.xrpldashboard.dockvault_neon_dump.plist` | 04:15 EDT daily |
| `launchd/com.charliebruce.xrpldashboard.dockvault_monitor.plist` | Hourly + RunAtLoad |

---

## Prereqs (before ANY of the below)

1. DockVault is mounted at `/Volumes/DockVault` (verified 2026-08-23 with keychain-auto-unlock).
2. BetterStack heartbeat monitor exists:
   - Create at https://betterstack.com/uptime → Heartbeats → New heartbeat
   - Name: `xrpldashboard-dockvault-mount-ok`
   - Period: 1 hour, grace: 15 min
   - Copy the heartbeat URL
3. Add heartbeat URL to `~/.config/xrpldashboard/env`:
   ```
   DOCKVAULT_MONITOR_HEARTBEAT_URL=https://uptime.betterstack.com/api/v1/heartbeat/XXXXXX
   ```
4. `rclone` + `python3` available on PATH (already installed for b2_backup / pg_backup).
5. Backblaze remote `b2crypt:` configured (already done — same one pg_backup uses).

---

## Install sequence

### 1. Push staged code + this doc

```bash
cd ~/xrpl_test
git status  # verify only the 8 new files show
git add launchd/dockvault_preflight.sh \
        launchd/run_dockvault_mirror.sh \
        launchd/run_dockvault_neon_dump.sh \
        launchd/run_dockvault_monitor.sh \
        launchd/com.charliebruce.xrpldashboard.dockvault_mirror.plist \
        launchd/com.charliebruce.xrpldashboard.dockvault_neon_dump.plist \
        launchd/com.charliebruce.xrpldashboard.dockvault_monitor.plist \
        docs/DOCKVAULT_INSTALL_RUNBOOK.md \
        docs/DOCK_REVIVAL_2026-08-23.md
git commit -m "dockvault: A+D+monitor bundle staged (1a-iii pull-back, 90d/12mo, active-write monitor)"
git push
```

### 2. Make scripts executable

```bash
chmod +x ~/xrpl_test/launchd/run_dockvault_mirror.sh
chmod +x ~/xrpl_test/launchd/run_dockvault_neon_dump.sh
chmod +x ~/xrpl_test/launchd/run_dockvault_monitor.sh
# preflight helper is sourced not executed — no chmod needed
```

### 3. Copy plists into user LaunchAgents

```bash
cp ~/xrpl_test/launchd/com.charliebruce.xrpldashboard.dockvault_mirror.plist \
   ~/Library/LaunchAgents/
cp ~/xrpl_test/launchd/com.charliebruce.xrpldashboard.dockvault_neon_dump.plist \
   ~/Library/LaunchAgents/
cp ~/xrpl_test/launchd/com.charliebruce.xrpldashboard.dockvault_monitor.plist \
   ~/Library/LaunchAgents/
```

### 4. BLOCKING FDA-verify gate (D6 doctrine)

**Do NOT bootstrap plists until this gate returns green for all three scripts.**

Test each script manually FIRST — while your Terminal already has FDA (granted 2026-08-23 during revival), plists run under launchd which has its OWN TCC context. A shell-level success does NOT prove the launchd context will work.

**Step 4a — Terminal-context sanity (must pass before proceeding):**

```bash
~/xrpl_test/launchd/run_dockvault_monitor.sh
# Expect log tail: "preflight ok — mount + write round-trip succeeded"
tail -10 ~/xrpl_test/launchd_logs/dockvault_monitor.$(date +%Y-%m-%d).log
```

**Step 4b — Launchd-context verify (THE ACTUAL D6 GATE):**

Bootstrap only the MONITOR plist first (not mirror or neon_dump — those run heavy syncs; monitor is cheap and gives the same FDA signal):

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.charliebruce.xrpldashboard.dockvault_monitor.plist
launchctl kickstart -k gui/$(id -u)/com.charliebruce.xrpldashboard.dockvault_monitor
sleep 5
tail -20 ~/xrpl_test/launchd_logs/dockvault_monitor.$(date +%Y-%m-%d).log
```

**Read the tail carefully:**

- ✅ `"preflight ok — mount + write round-trip succeeded"` → LAUNCHD CONTEXT HAS FDA. Proceed to Step 5.
- ❌ `"SKIP: DockVault write-test failed/timed out"` → LAUNCHD CONTEXT LACKS FDA. Grant fix:
  - System Settings → Privacy & Security → Full Disk Access
  - Click **+**, navigate to `/bin/bash`, add it, enable
  - Also add `/usr/bin/python3` if you plan to use scripts that invoke Python (neon_dump does)
  - `launchctl kickstart -k gui/$(id -u)/com.charliebruce.xrpldashboard.dockvault_monitor` again
  - Re-tail the log
  - Iterate until you see the ok message
- ❌ `"SKIP: DockVault not mounted"` → mount problem, not FDA problem. Unlock via Finder or physical replug.

**DO NOT proceed to Step 5 until the launchd-context test returns "preflight ok."**

### 5. Bootstrap the other two plists

Only after Step 4b returned green:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.charliebruce.xrpldashboard.dockvault_mirror.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.charliebruce.xrpldashboard.dockvault_neon_dump.plist
```

Verify all three are loaded:

```bash
launchctl list | grep dockvault
# Expect three entries: dockvault_mirror, dockvault_neon_dump, dockvault_monitor
```

### 6. Kickstart each once manually — verify real content lands

```bash
# Mirror — should rsync xrpl_test + .ssh to DockVault
launchctl kickstart -k gui/$(id -u)/com.charliebruce.xrpldashboard.dockvault_mirror
sleep 30  # rclone sync of ~5-50GB depending on state
tail -30 ~/xrpl_test/launchd_logs/dockvault_mirror.$(date +%Y-%m-%d).log
ls -la /Volumes/DockVault/xrpl_mirror/

# Neon dump — should pull latest B2 dump to DockVault
launchctl kickstart -k gui/$(id -u)/com.charliebruce.xrpldashboard.dockvault_neon_dump
sleep 30  # rclone copy of ~300MB
tail -30 ~/xrpl_test/launchd_logs/dockvault_neon_dump.$(date +%Y-%m-%d).log
ls -la /Volumes/DockVault/neon_dumps/
```

### 7. Verify BetterStack heartbeat is landing

Check the BetterStack dashboard 5 min after Step 4b — should show "Received a heartbeat." Every hourly monitor run pings again. If you don't see the ping, `DOCKVAULT_MONITOR_HEARTBEAT_URL` isn't sourcing correctly — check env file syntax.

### 8. Register the schedules — let them run overnight

If everything above returned green, the plists are already active and scheduled. First real cycles fire tonight or tomorrow at:
- 04:00 EDT — mirror
- 04:15 EDT — neon_dump pull-back
- Every hour on the clock — monitor

### 9. Morning-after verify

```bash
# All three logs should have a start line + end line for the overnight run
for name in dockvault_mirror dockvault_neon_dump dockvault_monitor; do
  echo "=== $name ==="
  tail -5 ~/xrpl_test/launchd_logs/${name}.$(date +%Y-%m-%d).log
done

# Verify DockVault has fresh content
ls -la /Volumes/DockVault/xrpl_mirror/
ls -la /Volumes/DockVault/neon_dumps/

# BetterStack dashboard should show hourly pings, no missed heartbeats
```

---

## Uninstall (if needed)

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.charliebruce.xrpldashboard.dockvault_mirror.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.charliebruce.xrpldashboard.dockvault_neon_dump.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.charliebruce.xrpldashboard.dockvault_monitor.plist
rm ~/Library/LaunchAgents/com.charliebruce.xrpldashboard.dockvault_*.plist
```

Data on DockVault is untouched by uninstall.

---

## Doctrinal reminders (from Charlie's 2026-08-23 ruling)

- **No unwatched writers, ever.** Monitor ships in-band with the bundle, not after.
- **Loud skip, never silent hang.** Every DockVault-touching op has a 10s Perl-alarm timeout.
- **launchd context ≠ Terminal context** for TCC/FDA grants. Step 4b is the blocking gate.
- **Pull-back is a pure downstream reader.** run_pg_backup.sh stays untouched. Zero risk to primary B2 path.
- **Prune retention:** 90 daily + 12 monthly-anchored on DockVault. Deeper than B2's 14+3.
