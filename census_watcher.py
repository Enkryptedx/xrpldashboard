"""census_watcher.py — condition-triggered launcher for census_escrow_phase1c.

Polls rippled server_info at POLL_INTERVAL cadence. Fires the census walker
when load_factor <= LOAD_THRESHOLD AND server_state in HEALTHY_STATES for
CONDITION_HOLD_POLLS consecutive polls. If DEADLINE_UTC arrives first, writes
a WAITED-OUT artifact and exits non-zero — never silent.

Why this exists:
- 2026-07-12 morning census walk failed at 30% coverage due to rippled load
  spike + silent marker-null truncation. Root cause: online_delete=10000
  triggering SHAMapStore sweep jobs (77s / 119s / 283s observed) that stall
  reads. Fix: fire the walker only when the node is measurably calm.
- Replaces the 02:30 EDT wallclock kickoff. Single firing path.
- Registers itself as a walker on line one — both walker_scope_declarations
  and walker_health — so it can't trip Monday's /coverage flip runbook
  undeclared-walker check. First walker whose scope is another walker.

Follow-ups (not tonight):
- Always-on load_factor recorder (Fable's cheap-permanent-fix idea). Watch
  observations are logged per-poll to launchd_logs/census_watcher_observations
  jsonl so tonight's correlation question has data; continuous 24/7 recording
  needs a separate small always-on component.
"""

import json
import logging
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db

WALKER_NAME = "census_watcher"
POLL_INTERVAL = 60
LOAD_THRESHOLD = 5.0
HEALTHY_STATES = ("full", "proposing")
CONDITION_HOLD_POLLS = 10  # 10 * 60s = 10 min of measured calm

# Hard deadline: if the condition never holds by then, write WAITED-OUT
# artifact and exit non-zero. Bumped 2026-07-15 for T2-synthesis re-kickoff.
DEADLINE_UTC = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)

RIPPLED_URL = "http://127.0.0.1:5005"
CENSUS_WALKER = "/Users/charliebruce/xrpl_test/census_escrow_phase1c.py"
LOG_DIR = Path("/Users/charliebruce/xrpl_test/launchd_logs")
_STAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
OBS_LOG = LOG_DIR / f"census_watcher_observations_{_STAMP}.jsonl"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("census_watcher")


def _rpc_server_info():
    payload = json.dumps({"method": "server_info", "params": [{}]}).encode()
    req = urllib.request.Request(
        RIPPLED_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["result"]["info"]


def _declare_scope():
    declared_scope = (
        f"meta-walker: launches census_escrow_phase1c when load_factor <= "
        f"{LOAD_THRESHOLD} AND server_state in {HEALTHY_STATES} for "
        f"{CONDITION_HOLD_POLLS * POLL_INTERVAL}s (deadline-bounded)"
    )
    filter_note = (
        "First walker whose scope is another walker. Owns no on-ledger data — "
        "reads rippled server_info only to gate the census's launch. "
        "Deadline-bounded: if the load condition never holds by "
        f"{DEADLINE_UTC.isoformat()}, writes a WAITED-OUT artifact and exits "
        "non-zero instead of hanging silently. Poll observations logged per-run "
        "to launchd_logs/census_watcher_observations_*.jsonl."
    )
    ok = db.upsert_walker_scope_declaration(
        WALKER_NAME, declared_scope, filter_note, False,
    )
    log.info("scope-declaration upsert ok=%s", ok)


def _write_waited_out(started, poll_count, last_reason):
    stamp = started.strftime("%Y-%m-%d")
    out = Path("/Users/charliebruce/xrpl_test") / f"census_watcher_WAITEDOUT_{stamp}.json"
    payload = {
        "type": "census_watcher_WAITEDOUT",
        "walker_name": WALKER_NAME,
        "started_at": started.isoformat(),
        "gave_up_at": datetime.now(timezone.utc).isoformat(),
        "deadline_utc": DEADLINE_UTC.isoformat(),
        "polls_taken": poll_count,
        "load_threshold": LOAD_THRESHOLD,
        "healthy_states": list(HEALTHY_STATES),
        "condition_hold_polls_required": CONDITION_HOLD_POLLS,
        "last_reason_condition_didnt_hold": last_reason,
        "observations_log": str(OBS_LOG),
    }
    out.write_text(json.dumps(payload, indent=2))
    log.error("WAITED-OUT artifact written to %s", out)
    return out


def main():
    started = datetime.now(timezone.utc)
    log.info(
        "census_watcher starting at %s (deadline %s)",
        started.isoformat(), DEADLINE_UTC.isoformat(),
    )
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    _declare_scope()
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=POLL_INTERVAL)

    consecutive_good = 0
    poll_count = 0
    last_reason = "just started, no observations yet"

    with OBS_LOG.open("a") as obs_f:
        while True:
            now = datetime.now(timezone.utc)

            if now >= DEADLINE_UTC:
                _write_waited_out(started, poll_count, last_reason)
                db.write_walker_health_end(
                    WALKER_NAME, False,
                    f"deadline reached after {poll_count} polls: {last_reason}",
                )
                return 2

            try:
                si = _rpc_server_info()
                err_msg = None
            except Exception as e:
                si = None
                err_msg = f"server_info exception: {e}"
                log.warning("Poll %d: %s", poll_count, err_msg)

            if si is None:
                consecutive_good = 0
                last_reason = err_msg
                obs = {
                    "ts": now.isoformat(),
                    "poll": poll_count,
                    "error": err_msg,
                }
                load = None
                state = None
                validated = None
            else:
                load = si.get("load_factor")
                state = si.get("server_state")
                validated = si.get("validated_ledger", {}).get("seq")
                complete_ledgers = si.get("complete_ledgers")

                healthy = state in HEALTHY_STATES
                calm = load is not None and load <= LOAD_THRESHOLD

                if healthy and calm:
                    consecutive_good += 1
                    last_reason = None
                else:
                    if consecutive_good > 0:
                        log.info(
                            "Streak reset at poll %d (load=%s state=%s)",
                            poll_count, load, state,
                        )
                    consecutive_good = 0
                    last_reason = (
                        f"load_factor={load} state={state} "
                        f"(need load<={LOAD_THRESHOLD} state in {HEALTHY_STATES})"
                    )
                obs = {
                    "ts": now.isoformat(),
                    "poll": poll_count,
                    "load_factor": load,
                    "server_state": state,
                    "validated_ledger": validated,
                    "complete_ledgers": complete_ledgers,
                    "streak": consecutive_good,
                    "healthy": healthy,
                    "calm": calm,
                }

            obs_f.write(json.dumps(obs) + "\n")
            obs_f.flush()

            db.write_heartbeat(WALKER_NAME, last_ledger=validated, extra={
                "started_at": started.isoformat(),
                "polls": poll_count,
                "load_factor": load,
                "server_state": state,
                "condition_good_streak": consecutive_good,
                "last_reason": last_reason,
            })

            if poll_count % 5 == 0 or consecutive_good >= 5:
                log.info(
                    "Poll %d: load=%s state=%s streak=%d/%d",
                    poll_count, load, state, consecutive_good, CONDITION_HOLD_POLLS,
                )

            if consecutive_good >= CONDITION_HOLD_POLLS:
                log.info(
                    "Condition held %d polls (%ds calm) — firing census walker.",
                    consecutive_good, consecutive_good * POLL_INTERVAL,
                )
                db.write_walker_health_end(
                    WALKER_NAME, True,
                    f"fired census after {poll_count} polls with "
                    f"{consecutive_good}-good streak",
                )
                rc = subprocess.run(
                    ["/usr/bin/python3", CENSUS_WALKER],
                    cwd=str(Path(CENSUS_WALKER).parent),
                ).returncode
                log.info("Census walker exited with %d", rc)
                return rc

            poll_count += 1
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.info("Interrupted.")
        db.write_walker_health_end(WALKER_NAME, False, "interrupted")
        sys.exit(130)
    except Exception as e:
        log.exception("Unhandled")
        db.write_walker_health_end(WALKER_NAME, False, f"unhandled: {e}")
        sys.exit(3)
