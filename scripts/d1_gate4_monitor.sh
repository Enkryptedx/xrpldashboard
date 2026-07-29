#!/bin/bash
# Gate 4 observation monitor for the walker value-fix kickstart.
# Writes progress to /tmp/d1_gate4_progress.log and a final summary
# to /tmp/d1_gate4_summary.json.

set -u

# Load T0
source /tmp/d1_gate4_t0.env
LOG=/tmp/d1_gate4_progress.log
SUM=/tmp/d1_gate4_summary.json
: > "$LOG"

# Load DATABASE_URL from the same env file the walker runners use.
if [[ -f "$HOME/.config/xrpldashboard/env" ]]; then
    source "$HOME/.config/xrpldashboard/env"
fi

PSQL=/opt/homebrew/bin/psql
[[ -x "$PSQL" ]] || PSQL=/usr/local/bin/psql
[[ -x "$PSQL" ]] || PSQL=$(command -v psql)

log() { echo "[$(date +%H:%M:%S)] $*" >> "$LOG"; }

log "T0=$T0_EDT ($T0_ISO), monitor pid=$$"

# --- T+270s: Render /health curl ---
sleep 270
HEALTH_CODE=$(curl -s -o /tmp/d1_gate4_health.json -w "%{http_code}" \
    "https://xrpldashboard.com/health?_=$(date +%s)")
log "T+270s /health HTTP $HEALTH_CODE"

# --- Poll token_volume every 30s until T+15min ---
END_EPOCH=$((T0_EPOCH + 900))
FIRST_NONZERO_TS=""
FIRST_NONZERO_ROW=""
POLL_COUNT=0
LAST_COUNT=0
LAST_SUM=0

while (( $(date +%s) < END_EPOCH )); do
    POLL_COUNT=$((POLL_COUNT+1))
    # count of rows written since T0 with volume_xrp > 0
    # token_volume uses hour_bucket (int hours since epoch); the CURRENT
    # hour_bucket is int(T0_EPOCH/3600). Query the current + next hour
    # bucket in case we cross the hour boundary.
    HB_NOW=$(( T0_EPOCH / 3600 ))
    HB_NEXT=$(( HB_NOW + 1 ))
    RESULT=$("$PSQL" "$DATABASE_URL" -Atc "SELECT COUNT(*), COALESCE(SUM(volume_xrp),0) FROM token_volume WHERE hour_bucket IN ($HB_NOW, $HB_NEXT) AND volume_xrp > 0" 2>&1)
    if [[ "$RESULT" == *"|"* ]]; then
        CUR_COUNT=$(echo "$RESULT" | cut -d'|' -f1)
        CUR_SUM=$(echo "$RESULT" | cut -d'|' -f2)
        LAST_COUNT=$CUR_COUNT
        LAST_SUM=$CUR_SUM
        if [[ -z "$FIRST_NONZERO_TS" ]] && (( CUR_COUNT > 0 )); then
            FIRST_NONZERO_TS=$(date +%H:%M:%S)
            FIRST_NONZERO_ROW=$("$PSQL" "$DATABASE_URL" -Atc "SELECT currency || '|' || issuer || '|' || volume_xrp || '|' || trade_count FROM token_volume WHERE hour_bucket IN ($HB_NOW, $HB_NEXT) AND volume_xrp > 0 ORDER BY volume_xrp DESC LIMIT 1" 2>&1)
            log "FIRST non-zero volume_xrp at $FIRST_NONZERO_TS: $FIRST_NONZERO_ROW"
        fi
        log "poll $POLL_COUNT: count=$CUR_COUNT sum_xrp=$CUR_SUM"
    else
        log "poll $POLL_COUNT: psql error: $RESULT"
    fi
    sleep 30
done

# --- Final walker_health check ---
WH_ROW=$("$PSQL" "$DATABASE_URL" -Atc "SELECT last_run_started, last_success_at, last_run_ok, consecutive_failures, last_run_message FROM walker_health WHERE walker_name='token_price_ratio_cache'" 2>&1)
log "walker_health token_price_ratio_cache: $WH_ROW"

# Also top-5 non-zero rows for the summary
TOP5=$("$PSQL" "$DATABASE_URL" -Atc "SELECT currency || ' / ' || substring(issuer, 1, 20) || ' / v=' || round(volume_xrp::numeric, 4) || ' / c=' || trade_count FROM token_volume WHERE hour_bucket IN ($(( T0_EPOCH / 3600 )), $(( T0_EPOCH / 3600 + 1 ))) AND volume_xrp > 0 ORDER BY volume_xrp DESC LIMIT 5" 2>&1)
log "top5 non-zero:"
echo "$TOP5" | while IFS= read -r line; do log "  $line"; done

cat > "$SUM" <<EOF
{
  "t0_iso": "$T0_ISO",
  "t0_edt": "$T0_EDT",
  "t0_epoch": $T0_EPOCH,
  "health_http_code": "$HEALTH_CODE",
  "first_nonzero_ts_local": "$FIRST_NONZERO_TS",
  "first_nonzero_row": "$FIRST_NONZERO_ROW",
  "final_nonzero_count": $LAST_COUNT,
  "final_sum_xrp": $LAST_SUM,
  "poll_count": $POLL_COUNT,
  "walker_health_row": "$WH_ROW"
}
EOF

log "DONE — summary written to $SUM"
