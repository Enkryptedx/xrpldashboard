#!/bin/bash
# rippled NuDB rebuild — Fix B from DIAGNOSTIC_BRIEF_local_rippled_2026-07-16.md
#
# Usage:
#   ./rippled_nudb_rebuild.sh          # DRY RUN — prints plan, touches nothing
#   ./rippled_nudb_rebuild.sh GO       # EXECUTES rebuild (destructive, gated)
#   ./rippled_nudb_rebuild.sh WATCH    # watch-only: 4-check + tripwire loop
#
# What it does (in GO mode):
#   1. Capture baseline: server_info, debug.log inode, disk-free, SHAMapStore
#      missing-node count (frozen at rebuild-start so delta is meaningful).
#   2. launchctl bootout gui/$UID/com.charliebruce.rippled
#   3. mv ~/rippled-data/db/nudb ~/rippled-data/db/nudb.old.<YYYYMMDD-HHMM>
#   4. launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.charliebruce.rippled.plist
#   5. Post-restart: log new debug.log inode + disk-free delta (proves the
#      7 GB ghost stderr inode from the pre-newsyslog era released).
#   6. Enter WATCH mode with tripwires.
#
# WATCH-mode tripwires (per rider 2 — flag, don't abort):
#   T1: SHAMapStore missing-node ever > 0 on fresh nudb → hardware-canary
#       signal fired; write ALERT + macOS notification; keep watching (data
#       for the hardware decision, not an abort).
#   T2: No sync progress after 2 h (validated_age not decreasing AND state
#       still < tracking) → surface via ALERT + macOS notification so 3 a.m.
#       weirdness produces an alert, not a breakfast discovery.
#
# Success — all 4 must hold (verdict rendered in the run log):
#   V1: validated_ledger.age < 10 sustained 30 min
#   V2: complete_ledgers = ONE contiguous range
#   V3: Zero SHAMapStore missing-node in first 24h post-rebuild
#   V4: nudb stabilizes < 20 GB after 24h

set -euo pipefail

LABEL="com.charliebruce.rippled"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DB_DIR="$HOME/rippled-data/db"
LOG_DIR="$HOME/rippled-data/logs"
DEBUG_LOG="$LOG_DIR/debug.log"
STAMP="$(date +%Y%m%d-%H%M)"
NUDB="$DB_DIR/nudb"
NUDB_ASIDE="$DB_DIR/nudb.old.$STAMP"
RUN_DIR="$HOME/xrpl_test/ops/rebuild_runs"
RUN_LOG="$RUN_DIR/rebuild_${STAMP}.log"
ALERT_FILE="$RUN_DIR/ALERT_${STAMP}.txt"
STATE_FILE="$RUN_DIR/state_${STAMP}.json"
mkdir -p "$RUN_DIR"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$RUN_LOG"; }

server_info_json() {
  curl -s -X POST http://localhost:5005 -H "Content-Type: application/json" \
       -d '{"method":"server_info"}' 2>/dev/null || echo '{}'
}

# Prints five whitespace-separated fields: state validated_age load peers frag_count
server_info_fields() {
  server_info_json | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    i = d.get("result", {}).get("info", {}) or {}
    v = i.get("validated_ledger", {}) or {}
    cl = i.get("complete_ledgers", "") or ""
    frags = (cl.count(",") + 1) if cl and cl != "empty" else 0
    print(i.get("server_state","?"), v.get("age","?"), i.get("load_factor","?"), i.get("peers","?"), frags)
except Exception:
    print("UNREACHABLE ? ? ? ?")
'
}

summarize_server_info() {
  server_info_json | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    i = d.get("result", {}).get("info", {}) or {}
    v = i.get("validated_ledger", {}) or {}
    cl = i.get("complete_ledgers", "") or ""
    frags = (cl.count(",") + 1) if cl and cl != "empty" else 0
    print("  state=" + str(i.get("server_state")))
    print("  validated_age=" + str(v.get("age")) + "s")
    print("  load_factor=" + str(i.get("load_factor")))
    print("  peers=" + str(i.get("peers")))
    print("  complete_ledgers_fragments=" + str(frags))
    print("  complete_ledgers_first80=" + str(cl)[:80])
except Exception as e:
    print("  server_info UNREACHABLE (" + str(e) + ")")
'
}

# Count SHAMapStore missing-node errors in the current debug.log inode.
# Grep-by-path resolves the inode fresh each call, so post-rotation the count
# is always against the live file.
count_missing_nodes() {
  if [ ! -f "$DEBUG_LOG" ]; then echo 0; return; fi
  grep -c 'SHAMapStore.*Missing node while copying' "$DEBUG_LOG" 2>/dev/null || echo 0
}

capture_log_inode() {
  if [ -f "$DEBUG_LOG" ]; then
    stat -f '%i' "$DEBUG_LOG" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

capture_disk_free_bytes() {
  df -k "$HOME" | awk 'NR==2 {print $4 * 1024}'
}

alert() {
  local tag="$1"; shift
  local msg="$*"
  {
    echo "=== ALERT: $tag @ $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    echo "$msg"
    echo ""
  } | tee -a "$ALERT_FILE" | tee -a "$RUN_LOG" >&2
  # macOS notification — Charlie sees it if the Mac is awake.
  osascript -e "display notification \"$tag: $msg\" with title \"rippled rebuild\" sound name \"Basso\"" 2>/dev/null || true
}

baseline() {
  local start_missing start_inode start_free
  start_missing="$(count_missing_nodes)"
  start_inode="$(capture_log_inode)"
  start_free="$(capture_disk_free_bytes)"
  # Persist for WATCH delta math (may run in a separate invocation)
  printf '{"start_missing":%s,"start_inode":%s,"start_free":%s,"stamp":"%s"}\n' \
    "$start_missing" "$start_inode" "$start_free" "$STAMP" > "$STATE_FILE"

  log "=== BASELINE @ $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  log "-- server_info (pre-rebuild) --"
  summarize_server_info | tee -a "$RUN_LOG"
  log "-- debug.log --"
  log "  path=$DEBUG_LOG"
  log "  inode=$start_inode  size=$(stat -f '%z' "$DEBUG_LOG" 2>/dev/null || echo 0)"
  log "  SHAMapStore_missing_lifetime=$start_missing (frozen; WATCH shows delta since rebuild)"
  log "-- rippled fd inventory (looking for ghost stderr inode) --"
  lsof -p "$(pgrep -f rippled | head -1)" 2>/dev/null \
    | grep -E "\.log|launchd-err" | tee -a "$RUN_LOG" || log "  (no rippled PID or no log fds)"
  log "-- disk free --"
  df -h ~ | tee -a "$RUN_LOG"
  log "  disk_free_bytes=$start_free"
  log "-- db dir --"
  ls -la "$DB_DIR" | tee -a "$RUN_LOG"
  log "-- nudb size --"
  du -sh "$NUDB" 2>/dev/null | tee -a "$RUN_LOG"
  log "-- load avg --"
  uptime | tee -a "$RUN_LOG"
  log "=== END BASELINE ==="
}

post_restart_report() {
  local new_inode new_free start_free
  new_inode="$(capture_log_inode)"
  new_free="$(capture_disk_free_bytes)"
  start_free="$(python3 -c 'import json,sys; print(json.load(open("'"$STATE_FILE"'"))["start_free"])')"
  local delta_gb
  delta_gb="$(python3 -c "print(round((${new_free} - ${start_free}) / 1024**3, 2))")"
  log "=== POST-RESTART REPORT ==="
  log "  debug.log new inode=$new_inode (baseline inode was $(python3 -c 'import json; print(json.load(open("'"$STATE_FILE"'"))["start_inode"])'))"
  log "  disk_free delta since baseline: ${delta_gb} GiB"
  log "  ghost stderr inode from pre-newsyslog era should have released on bootout."
  log "  new rippled PID: $(pgrep -f rippled | head -1)"
}

do_rebuild() {
  local UID_NUM
  UID_NUM="$(id -u)"

  if [ ! -f "$PLIST" ]; then log "FATAL: plist not found at $PLIST"; exit 2; fi
  if [ ! -d "$NUDB" ]; then log "FATAL: nudb dir not found at $NUDB"; exit 2; fi

  baseline

  log ""
  log "=== STEP 1: bootout $LABEL ==="
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>&1 | tee -a "$RUN_LOG" || true

  # Poll for rippled shutdown up to 60s, checking every 2s. NuDB flush on
  # a 41G+ database can take 10-15s of quiet writes after SIGTERM before
  # the process exits. Bumped from a fixed 13s ceiling after 2026-07-18
  # dry-fire: a normal ~15s flush was clipped, script fired FATAL, guard
  # held (nudb untouched) but the 13s was the wrong number for this DB size.
  #
  # `pgrep -x rippled` matches ONLY the rippled binary by exact process
  # image name — critical because the launch-scheduler bash job that fires
  # this script at 21:00 has "rippled" in its cmdline (script name) and
  # `pgrep -f rippled` would false-match it forever, guaranteeing FATAL.
  local shutdown_start shutdown_deadline shutdown_duration
  shutdown_start="$(date +%s)"
  shutdown_deadline=$((shutdown_start + 60))
  while pgrep -x rippled >/dev/null; do
    if [ "$(date +%s)" -ge "$shutdown_deadline" ]; then
      log "FATAL: rippled did not stop within 60s; aborting before mv"
      exit 3
    fi
    sleep 2
  done
  shutdown_duration=$(( $(date +%s) - shutdown_start ))
  log "  rippled stopped after ${shutdown_duration}s."

  log ""
  log "=== STEP 2: mv nudb aside ==="
  log "  $NUDB  ->  $NUDB_ASIDE"
  mv "$NUDB" "$NUDB_ASIDE"
  log "  moved. old nudb size:"
  du -sh "$NUDB_ASIDE" | tee -a "$RUN_LOG"

  log ""
  log "=== STEP 3: bootstrap $LABEL ==="
  launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>&1 | tee -a "$RUN_LOG"
  sleep 5
  pgrep -x rippled >/dev/null || { log "FATAL: rippled did not start after bootstrap"; exit 4; }

  post_restart_report

  log ""
  log "Rollback if needed:"
  log "  launchctl bootout gui/$UID_NUM/$LABEL"
  log "  rm -rf $NUDB"
  log "  mv $NUDB_ASIDE $NUDB"
  log "  launchctl bootstrap gui/$UID_NUM $PLIST"
  log ""
  log "=== STEP 4: entering WATCH mode with tripwires ==="
  do_watch
}

do_watch() {
  local start_missing start_epoch last_progress_epoch last_val_age tripped_t1 tripped_t2
  if [ ! -f "$STATE_FILE" ]; then
    # WATCH invoked standalone; treat "now" as baseline.
    start_missing="$(count_missing_nodes)"
    start_epoch="$(date +%s)"
    printf '{"start_missing":%s,"start_inode":%s,"start_free":%s,"stamp":"%s","standalone":1}\n' \
      "$start_missing" "$(capture_log_inode)" "$(capture_disk_free_bytes)" "$STAMP" > "$STATE_FILE"
  else
    start_missing="$(python3 -c 'import json; print(json.load(open("'"$STATE_FILE"'"))["start_missing"])')"
    start_epoch="$(date +%s)"
  fi
  last_progress_epoch="$start_epoch"
  last_val_age=""
  tripped_t1=0
  tripped_t2=0

  log "=== WATCH — check every 60s; Ctrl-C exits. Tripwires: T1 missing-node>0, T2 no-progress>2h ==="
  log "  (In another pane): tail -F $DEBUG_LOG | grep NetworkOPs"
  log ""

  local i=0
  while true; do
    i=$((i+1))
    log "--- check #$i @ $(date -u +%H:%M:%SZ) ---"
    summarize_server_info | tee -a "$RUN_LOG"

    local nudb_size fields state val_age load peers frags cur_missing delta_missing
    nudb_size="$(du -sh "$NUDB" 2>/dev/null | cut -f1 || echo '?')"
    fields="$(server_info_fields)"
    state="$(echo "$fields" | awk '{print $1}')"
    val_age="$(echo "$fields" | awk '{print $2}')"
    load="$(echo "$fields" | awk '{print $3}')"
    peers="$(echo "$fields" | awk '{print $4}')"
    frags="$(echo "$fields" | awk '{print $5}')"
    cur_missing="$(count_missing_nodes)"
    delta_missing=$((cur_missing - start_missing))

    log "  nudb_size=$nudb_size"
    log "  SHAMapStore_missing_since_rebuild=$delta_missing (lifetime=$cur_missing, baseline=$start_missing)"

    # T1: any missing-node on fresh DB → hardware-canary signal. Flag, don't abort.
    if [ "$delta_missing" -gt 0 ] && [ "$tripped_t1" -eq 0 ]; then
      alert "T1_MISSING_NODE_ON_FRESH_NUDB" \
        "delta=$delta_missing since rebuild. Hardware-canary fired — this is the signal the brief predicted would justify the hardware conversation. Continuing to watch per rider 2 (flag, don't abort)."
      tripped_t1=1
    fi

    # Progress tracking: validated_age decreasing counts as progress; also any state advance.
    local now_epoch
    now_epoch="$(date +%s)"
    if [ "$val_age" != "?" ] && [ -n "$last_val_age" ] && [ "$last_val_age" != "?" ]; then
      # Numeric compare — treat val_age as integer seconds.
      if [ "$val_age" -lt "$last_val_age" ] 2>/dev/null; then
        last_progress_epoch="$now_epoch"
      fi
    fi
    if [ "$state" = "tracking" ] || [ "$state" = "full" ]; then
      last_progress_epoch="$now_epoch"
    fi
    last_val_age="$val_age"

    local stall_seconds=$((now_epoch - last_progress_epoch))
    # T2: no progress after 2h AND state still below tracking.
    if [ "$stall_seconds" -gt 7200 ] && [ "$state" != "tracking" ] && [ "$state" != "full" ] && [ "$tripped_t2" -eq 0 ]; then
      alert "T2_NO_SYNC_PROGRESS_2H" \
        "state=$state val_age=$val_age peers=$peers frags=$frags — stalled ${stall_seconds}s with no forward motion. Investigate peer connectivity or bootstrap ips."
      tripped_t2=1
    fi

    # Verdict check — write once when all four look green, but keep watching.
    if [ "$state" = "full" ] && [ "$val_age" != "?" ] && [ "$val_age" -lt 10 ] 2>/dev/null && [ "$frags" -eq 1 ] && [ "$delta_missing" -eq 0 ]; then
      log "  VERDICT-CANDIDATE: V1(age<10)✓ V2(1frag)✓ V3(0missing)✓ — awaiting 24h + nudb<20GB for full green."
    fi

    log ""
    sleep 60
  done
}

case "${1:-}" in
  GO)
    log "MODE: GO — will execute rebuild in 5 seconds. Ctrl-C to abort."
    sleep 5
    do_rebuild
    ;;
  WATCH)
    do_watch
    ;;
  ""|--dry-run|DRY)
    echo "=== DRY RUN — no changes will be made ==="
    echo ""
    echo "Plan:"
    echo "  1. baseline capture (inode, disk-free, missing-node count frozen)"
    echo "  2. launchctl bootout gui/$(id -u)/$LABEL"
    echo "  3. mv $NUDB -> $NUDB_ASIDE"
    echo "  4. launchctl bootstrap gui/$(id -u) $PLIST"
    echo "  5. post-restart report (new inode + disk delta = ghost release proof)"
    echo "  6. WATCH loop with tripwires T1 (missing-node on fresh) + T2 (no-progress-2h)"
    echo ""
    echo "Current state (baseline preview):"
    summarize_server_info
    echo "  SHAMapStore_missing_lifetime=$(count_missing_nodes)"
    echo "  debug.log inode=$(capture_log_inode)  disk_free=$(df -h ~ | awk 'NR==2 {print $4}')"
    echo ""
    echo "To execute:  $0 GO"
    echo "Watch only:  $0 WATCH"
    ;;
  *)
    echo "Usage: $0 [GO|WATCH|DRY]"; exit 1;;
esac
