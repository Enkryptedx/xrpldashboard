#!/usr/bin/env python3
"""xrpl_stream_restart_rate_walker.

Every hour, count xrpl_stream restarts on Lenovo over rolling 24h and 7d
windows and write the totals to walker_health. Makes the restart-rate
visible in Postgres instead of buried in xrpl_stream.log — the log
analysis 2026-09-03 found 1,218 watchdog-triggered restarts across 23
days (~53/day) but nobody would have known without a manual grep. This
walker turns that grep into a first-class metric.

Reads from Lenovo via ssh (rippled-node alias). The grep runs remotely;
only the counts come back over the wire. If ssh fails, walker_health
gets ok=False + the error message (so a broken node is visible too).
"""

import os
import subprocess
import sys
import time


HERE = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, HERE)

WALKER_NAME = "xrpl_stream_restart_rate"
WALKER_CADENCE_SECONDS = 3600  # hourly

# grep on Lenovo. Two rolling windows via awk on the [YYYY-MM-DD HH:MM:SS]
# timestamp prefix — no dependency on rotated logs, so a truncated / just-
# rotated log simply reports a smaller count for a few hours.
REMOTE_HOST = os.environ.get("XRPL_STREAM_HOST", "rippled-node")
REMOTE_LOG = os.environ.get(
    "XRPL_STREAM_LOG_PATH",
    "/home/charlie/xrpldashboard/logs/xrpl_stream.log",
)

REMOTE_CMD_TEMPLATE = (
    'python3 -c "'
    "import re,sys,time,datetime\n"
    "p=\\\"{log}\\\"\n"
    "now=time.time()\n"
    "cut24=datetime.datetime.fromtimestamp(now-86400).strftime(\\\"%Y-%m-%d %H:%M:%S\\\")\n"
    "cut7d=datetime.datetime.fromtimestamp(now-7*86400).strftime(\\\"%Y-%m-%d %H:%M:%S\\\")\n"
    "c24=c7=c_wd=c_sr=0\n"
    "with open(p) as f:\n"
    "  for line in f:\n"
    "    if len(line)<21 or line[0]!=\\\"[\\\": continue\n"
    "    ts=line[1:20]\n"
    "    if \\\"xrpl_stream starting\\\" in line:\n"
    "      if ts>=cut7d: c7+=1\n"
    "      if ts>=cut24: c24+=1\n"
    "    elif \\\"watchdog: no msg\\\" in line and ts>=cut24: c_wd+=1\n"
    "    elif \\\"session ended cleanly\\\" in line and ts>=cut24: c_sr+=1\n"
    "print(f\\\"{{c24}} {{c7}} {{c_wd}} {{c_sr}}\\\")"
    '"'
)


def _log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def fetch_counts() -> tuple[int, int, int, int]:
    cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
           REMOTE_HOST, REMOTE_CMD_TEMPLATE.format(log=REMOTE_LOG)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(
            f"ssh failed rc={out.returncode}: "
            f"stderr={out.stderr.strip()[:200]}"
        )
    parts = out.stdout.strip().split()
    if len(parts) != 4:
        raise RuntimeError(f"unexpected stdout shape: {out.stdout!r}")
    return int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])


def main() -> int:
    try:
        import db
    except Exception as e:
        _log(f"ERROR db import failed: {e!r}")
        return 1

    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)

    ok = False
    msg = "init"
    findings = None
    try:
        c24, c7, cwd, csr = fetch_counts()
        # findings_count is a compound liveness signal: process restarts
        # + session reconnects (both are data gaps in events.db that the
        # process-restart count alone missed until 2026-09-04).
        findings = c24 + csr
        rate_per_day_7d = c7 / 7.0
        msg = (f"24h={c24} restarts (watchdog={cwd}) · "
               f"session_reconnects={csr} · "
               f"7d={c7} ({rate_per_day_7d:.1f}/day avg) · "
               f"host={REMOTE_HOST}")
        _log(f"OK: {msg}")
        ok = True
        return 0
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _log(f"ERROR: {msg}")
        return 1
    finally:
        db.write_walker_health_end(
            WALKER_NAME, ok=ok, message=msg, findings_count=findings,
        )


if __name__ == "__main__":
    sys.exit(main())
