#!/usr/bin/env bash
#
# D6 launchd-FDA probe — one-shot diagnostic before DockVault install.
#
# Runs the exact operations the real dockvault_* jobs will need, from
# the launchd GUI-domain context (via a RunAtLoad probe plist). Writes
# a labeled log so we can read the pass/fail signals in one place.
#
# Doctrine: evidence before settings changes. Launchd context ≠ Terminal
# context for TCC/FDA (2026-08-23 revival lesson). Terminal.app grants
# don't help the LaunchAgents that run at 04:00 EDT — the launchd process
# tree has its own responsible-process attribution.
#
# What we test:
#   1. /Volumes root read              (baseline — no special grant needed)
#   2. /Volumes/DockVault read         (FDA or Removable Volumes required)
#   3. /Volumes/DockVault write        (same — the actual sync path)
#   4. rclone lsf b2crypt:...          (rclone binary + config + network)
#   5. python3 + perl availability     (prune loop + preflight helper deps)
#
# Pass condition: every section prints its expected output. Any "Operation
# not permitted" / silent-hang / stderr-only output = FDA denial signal.

set -u
LOG_DIR="/Users/charliebruce/xrpl_test/launchd_logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/probe_fda_$(date -u +%Y%m%dT%H%M%SZ).log"

exec > "$LOG" 2>&1

echo "═══ D6 FDA PROBE ═══"
echo "date_utc:      $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "date_local:    $(date +%Y-%m-%d\ %H:%M:%S\ %Z)"
echo "whoami:        $(whoami)"
echo "uid:           $(id -u)"
echo "shell:         ${SHELL:-<unset>}"
echo "pid:           $$"
echo "ppid:          $PPID"
echo "PATH:          ${PATH}"
echo "context_hint:  $(launchctl print-cache 2>/dev/null | head -1 || echo 'launchctl print-cache unavailable')"
echo

echo "─── §1 /Volumes root read (baseline) ───"
if ls -la /Volumes/ 2>&1; then
  echo "  [ok] /Volumes read succeeded"
else
  echo "  [FAIL] /Volumes read denied — nothing else will work"
fi
echo

echo "─── §2 /sbin/mount (DockVault presence) ───"
if /sbin/mount | grep -i dockvault; then
  echo "  [ok] DockVault visible in mount table"
else
  echo "  [FAIL] DockVault NOT in mount — unlock volume before re-probe"
fi
echo

echo "─── §3 /Volumes/DockVault read ───"
if ls -la /Volumes/DockVault 2>&1 | head -20; then
  echo "  [ok] DockVault root readable"
else
  echo "  [FAIL] DockVault read denied — FDA gate is CLOSED"
fi
echo

echo "─── §4 /Volumes/DockVault write round-trip ───"
TEST_DIR="/Volumes/DockVault/.heartbeat"
TEST_FILE="${TEST_DIR}/probe_fda_$$_$(date +%s).txt"
if mkdir -p "$TEST_DIR" 2>&1 && \
   echo "probe-payload" > "$TEST_FILE" 2>&1 && \
   cat "$TEST_FILE" 2>&1 && \
   rm "$TEST_FILE" 2>&1; then
  echo "  [ok] write/read/delete round-trip succeeded"
else
  rc=$?
  echo "  [FAIL] write round-trip failed rc=${rc} — FDA gate is CLOSED"
fi
echo

echo "─── §5 rclone binary + config + network ───"
if command -v rclone >/dev/null 2>&1; then
  echo "  rclone_path: $(command -v rclone)"
  echo "  rclone_version: $(rclone version 2>&1 | head -1)"
  echo "  rclone_config_show [b2crypt] type only:"
  rclone config show b2crypt 2>&1 | grep -E "^(type|remote) =" || echo "  (no b2crypt in config)"
  echo
  echo "  rclone lsf b2crypt:xrpldashboard-backup-Charlies-Mac-mini/postgres (head -5):"
  if rclone lsf b2crypt:xrpldashboard-backup-Charlies-Mac-mini/postgres --files-only --include "neondb-*.dump" --timeout 30s --contimeout 10s 2>&1 | head -5; then
    echo "  [ok] rclone remote lsf succeeded"
  else
    rc=$?
    echo "  [FAIL] rclone lsf failed rc=${rc} — rclone config path or network problem"
  fi
else
  echo "  [FAIL] rclone not on PATH under launchd — env plist PATH is $PATH"
fi
echo

echo "─── §6 python3 + perl (helper deps) ───"
echo "  python3: $(/usr/bin/python3 --version 2>&1)"
echo "  perl:    $(/usr/bin/perl -e 'print $^V' 2>&1)"
echo

echo "─── §7 launchd_state dir write ───"
STATE_DIR="/Users/charliebruce/xrpl_test/launchd_state"
STATE_TEST="${STATE_DIR}/probe_$$_$(date +%s).txt"
if mkdir -p "$STATE_DIR" 2>&1 && \
   echo "probe" > "$STATE_TEST" 2>&1 && \
   cat "$STATE_TEST" 2>&1 && \
   rm "$STATE_TEST" 2>&1; then
  echo "  [ok] launchd_state write round-trip succeeded (home dir writes OK)"
else
  echo "  [FAIL] launchd_state write failed — home-dir problem, not FDA"
fi
echo

echo "═══ END PROBE ═══"
