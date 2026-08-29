#!/bin/bash
# One-shot reminder to re-verify /regulation content on 2026-09-14 (Senate
# recess return, cloture-vote week of 09-15 on H.R. 3633 CLARITY Act).
#
# Filed 2026-08-17 after the /regulation cloture-motion content miss
# (commit 7928a36) that surfaced from a stale-banner day-9 breach —
# `regulation_status_timeline.risk_note` names 2026-09-14 as a hard trigger.
#
# Guards + channels:
#  - Date self-guard so an accidental early load or a launchd calendar
#    misfire cannot fire on the wrong day
#  - macOS notification (guaranteed to reach Charlie at his daily driver)
#  - Marker file at ~/xrpl_test/REGULATION_HARD_TRIGGER_2026-09-14.txt
#    (survives after notification is dismissed)
#  - Optional Telegram via L1_TELEGRAM_BOT_TOKEN/CHAT_ID if present in env
#    (currently absent on Mac; add to ~/.config/xrpldashboard/env to enable)
#  - Self-unloads the LaunchAgent after successful fire so it never
#    fires a second time
set -uo pipefail

TARGET_DATE="2026-09-14"
LOG="/Users/charliebruce/xrpl_test/launchd_logs/regulation_hard_trigger.log"
MARKER="/Users/charliebruce/xrpl_test/REGULATION_HARD_TRIGGER_2026-09-14.txt"
PLIST_LABEL="com.charliebruce.xrpldashboard.regulation_hard_trigger"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

# --test: fires today for phone-buzz verification. Bypasses date guard,
# skips marker write + LaunchAgent unload (the second would silently kill
# the real Sep-14 fire). Added 2026-08-18 during stability-clock sitting.
TEST_MODE=0
if [[ "${1:-}" == "--test" ]]; then
  TEST_MODE=1
fi

log() { echo "[$(date '+%F %T %Z')] $*" >>"$LOG"; }

today="$(date '+%Y-%m-%d')"
if [[ "$TEST_MODE" != "1" && "$today" != "$TARGET_DATE" ]]; then
  log "guard: today=$today != target=$TARGET_DATE — exit 0 without firing"
  exit 0
fi

if [[ "$TEST_MODE" == "1" ]]; then
  MSG_TITLE="TEST — /regulation Sep-14 reminder path"
  MSG_BODY="Phone-buzz verification $(date '+%F %T %Z'). Real Sep-14 fire path untouched (marker + launchd unload skipped)."
else
  MSG_TITLE="RE-VERIFY /regulation TODAY"
  MSG_BODY="Hard trigger — Senate recess return + cloture-vote week of 09-15 on H.R. 3633 (CLARITY Act). Pull primary sources, update timeline + LAST_VERIFIED_REGULATION, reset staleness clock."
fi

if [[ "$TEST_MODE" != "1" ]]; then
  log "fire: writing marker + macOS notification"
  {
    echo "REGULATION HARD-TRIGGER — $TARGET_DATE 09:00 ET"
    echo
    echo "$MSG_TITLE"
    echo "$MSG_BODY"
    echo
    echo "Fired at: $(date '+%F %T %Z')"
  } >"$MARKER"
else
  log "test-mode: skipped marker write"
fi

/usr/bin/osascript -e "display notification \"$MSG_BODY\" with title \"$MSG_TITLE\" sound name \"Sosumi\"" \
  >>"$LOG" 2>&1 || log "warn: osascript notification failed (rc=$?)"

# Opportunistic Telegram — only sends if creds are set. Reuse L1 pager's
# variables so a future paste-in doesn't need a second convention.
ENV_FILE="$HOME/.config/xrpldashboard/env"
if [[ -r "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
tg_token="${L1_TELEGRAM_BOT_TOKEN:-}"
tg_chat="${L1_TELEGRAM_CHAT_ID:-}"
if [[ -n "$tg_token" && -n "$tg_chat" ]]; then
  full_msg="${MSG_TITLE}\n\n${MSG_BODY}"
  http_code=$(/usr/bin/curl -sS -o /dev/null -w '%{http_code}' -m 15 \
    -d "chat_id=${tg_chat}" \
    --data-urlencode "text=${full_msg}" \
    "https://api.telegram.org/bot${tg_token}/sendMessage" || echo "curl_err")
  log "telegram: http=$http_code"
else
  log "telegram: skipped (L1_TELEGRAM_* not in env — macOS notify + marker still fired)"
fi

# Self-unload so the plist doesn't sit around triggering again next year.
# Skipped under --test so the real Sep-14 fire remains armed.
if [[ "$TEST_MODE" != "1" && -f "$PLIST_PATH" ]]; then
  /bin/launchctl unload "$PLIST_PATH" >>"$LOG" 2>&1 \
    && log "unloaded LaunchAgent $PLIST_LABEL" \
    || log "warn: unload failed (rc=$?); manual unload required"
elif [[ "$TEST_MODE" == "1" ]]; then
  log "test-mode: skipped LaunchAgent unload (real Sep-14 fire still armed)"
fi

log "done"
exit 0
