#!/bin/bash
# Sample rippled (xrpld-main) RSS + swap into a CSV.
#
# Lives in a FILE on purpose. The first version of this inlined the whole body
# into ExecStart= and systemd rejected the unit outright:
#   "Ignoring unknown escape sequences" + "Failed to resolve unit specifiers
#    ... Invalid slot" -> "unit will not be started"
# because `%` is systemd's specifier prefix, so `date -u +%FT%TZ` was read as
# the specifiers %F/%T/%Z. Escaping to %% would have fixed that one symptom,
# but Exec= lines also perform $VAR expansion, so an inline body stays one
# edit away from breaking again. In a script file neither applies.
#
# Optional $1 = label written to the label column (e.g. PRE_RESTART).
set -uo pipefail

LOG=/var/log/rippled-rss.log
LABEL="${1:-sample}"

[ -f "$LOG" ] || echo "ts_utc,label,pid,etimes,rss_kb,vsz_kb,pct_mem,mem_used_kb,swap_used_kb,cgroup_bytes" > "$LOG"

TS="$(date -u +%FT%TZ)"
PID="$(pgrep -x xrpld-main | head -1 || true)"

# pgrep -x xrpld-main is deliberate: the 3.x binary is xrpld and the main
# thread presents as xrpld-main. `pgrep -x rippled` matches NOTHING on this
# host even though the systemd unit is still named rippled.
if [ -z "$PID" ]; then
  echo "$TS,$LABEL,,,,,,,,PROCESS_NOT_FOUND" >> "$LOG"
  exit 0
fi

read -r ET RSS VSZ PM <<< "$(ps -o etimes=,rss=,vsz=,pmem= -p "$PID")"
MU="$(awk '/MemAvailable/{a=$2} /MemTotal/{t=$2} END{print t-a}' /proc/meminfo)"
SU="$(awk '/SwapTotal/{t=$2} /SwapFree/{f=$2} END{print t-f}' /proc/meminfo)"
CG="$(systemctl show rippled -p MemoryCurrent --value)"

echo "$TS,$LABEL,$PID,$ET,$RSS,$VSZ,$PM,$MU,$SU,$CG" >> "$LOG"
