"""
One-time bootstrap scan of every AMM on the XRP Ledger.

Walks `ledger_data` page by page, filtered to type=AMM, and writes each
discovered AMM to amm_index.json. Saves resume state every page so a
crash, network blip, or Ctrl-C can pick up where it left off.

Expected runtime: 6–12 hours against s2.ripple.com (Ripple's full-history
public node — s1 only retains ~4-7 hours of ledger state, not enough for
a single locked snapshot to survive the scan). The XRPL has tens of
millions of ledger objects and we have to scan all of them to be sure
we've caught every AMM. After the bootstrap completes, an incremental
subscription watcher keeps the index current.

Usage:
    python scan_all_amms.py            # start or resume
    python scan_all_amms.py --reset    # force fresh scan (deletes state)

Outputs (next to this script):
    amm_index.json        — list of all discovered AMMs
    amm_scan_state.json   — resume marker + counters
    amm_scan.log          — progress log (tail -f to watch)
"""

from xrpl.clients import JsonRpcClient
from xrpl.models.requests.ledger_data import LedgerData, LedgerEntryType
from datetime import datetime, timezone
import json
import os
import sys
import time

# s2.ripple.com is Ripple's full-history public node. s1 only retains
# ~32k ledgers (~4-7 hours), which isn't enough for a 10+ hour scan
# locked to a single ledger version. s2 keeps the snapshot alive for
# the duration of the walk.
XRPL_NODE = "https://s2.ripple.com:51234"
PAGE_LIMIT = 500
SAVE_INDEX_EVERY = 50            # pages between full index writes
LOG_EVERY = 25                   # pages between log lines
# Bounded but generous: 100 attempts with backoff capped at 60s tolerates
# ~100 minutes of continuous network/node trouble before giving up.
MAX_RETRIES = 100
BACKOFF_BASE = 2.0               # seconds, exponential
BACKOFF_CAP = 60.0               # seconds, max sleep between retries

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(HERE, "amm_index.json")
STATE_PATH = os.path.join(HERE, "amm_scan_state.json")
LOG_PATH = os.path.join(HERE, "amm_scan.log")


def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log(f"WARN failed to load {path}: {e} — starting fresh")
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2 if path == INDEX_PATH else None)
    os.replace(tmp, path)


class StaleLedgerError(RuntimeError):
    """Raised when the locked snapshot ledger has aged out of the node."""


def request_with_retry(client, request):
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.request(request)
            if "error" in resp.result:
                err = resp.result.get("error")
                # Snapshot expired — caller needs to relock + restart.
                # Don't waste retries on this; surface it immediately.
                if err in ("noCurrent", "lgrNotFound", "lgrNotInDb"):
                    raise StaleLedgerError(err)
                raise RuntimeError(f"node error: {err}")
            return resp
        except StaleLedgerError:
            raise
        except Exception as e:
            last_err = e
            wait = min(BACKOFF_CAP, BACKOFF_BASE ** attempt)
            log(f"  retry {attempt+1}/{MAX_RETRIES} after error: {e} — sleep {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"exhausted retries: {last_err}")


def main():
    if "--reset" in sys.argv:
        for p in (INDEX_PATH, STATE_PATH):
            if os.path.exists(p):
                os.remove(p)
                log(f"removed {p}")

    state = load_json(STATE_PATH, {
        "marker": None,
        "pages": 0,
        "raw_objects_scanned": 0,
        "ledger_index": None,
        "started_at": None,
        "finished_at": None,
    })
    index = load_json(INDEX_PATH, [])

    if state.get("finished_at"):
        log(f"scan already complete: {len(index)} AMMs in {INDEX_PATH}")
        log("delete amm_scan_state.json or pass --reset to rescan")
        return 0

    if state["started_at"] is None:
        state["started_at"] = datetime.now(timezone.utc).isoformat()
        log(f"starting fresh scan against {XRPL_NODE}")
    else:
        log(f"resuming scan: {state['pages']} pages done, {len(index)} AMMs found so far")

    client = JsonRpcClient(XRPL_NODE)

    # Lock the ledger version for the whole scan so pagination stays consistent.
    if state["ledger_index"] is None:
        ledger_resp = request_with_retry(
            client,
            LedgerData(ledger_index="validated", limit=1)
        )
        state["ledger_index"] = ledger_resp.result["ledger_index"]
        save_json(STATE_PATH, state)
        log(f"locked to ledger {state['ledger_index']}")

    t_loop_start = time.time()
    pages_this_run = 0

    while True:
        try:
            resp = request_with_retry(client, LedgerData(
                ledger_index=state["ledger_index"],
                type=LedgerEntryType.AMM,
                limit=PAGE_LIMIT,
                marker=state["marker"],
            ))
        except KeyboardInterrupt:
            log("interrupted — state saved, run again to resume")
            return 130
        except StaleLedgerError as e:
            # Locked snapshot aged out (shouldn't happen on s2 but handle it).
            # Markers are tied to the ledger version, so we have to start over
            # against a fresh snapshot. Reset progress and let the loop relock.
            log(f"snapshot ledger {state['ledger_index']} expired ({e}) — relocking and restarting")
            state["ledger_index"] = None
            state["marker"] = None
            state["pages"] = 0
            state["raw_objects_scanned"] = 0
            index.clear()
            save_json(STATE_PATH, state)
            save_json(INDEX_PATH, index)
            ledger_resp = request_with_retry(
                client, LedgerData(ledger_index="validated", limit=1)
            )
            state["ledger_index"] = ledger_resp.result["ledger_index"]
            save_json(STATE_PATH, state)
            log(f"relocked to ledger {state['ledger_index']}")
            t_loop_start = time.time()
            pages_this_run = 0
            continue
        except Exception as e:
            log(f"fatal: {e} — state saved at page {state['pages']}")
            save_json(STATE_PATH, state)
            return 1

        result = resp.result
        page_amms = result.get("state", []) or []
        for obj in page_amms:
            index.append(obj)

        state["pages"] += 1
        pages_this_run += 1
        # Each page approximately scans PAGE_LIMIT raw objects, regardless of how
        # many matched the type filter.
        state["raw_objects_scanned"] += PAGE_LIMIT
        state["marker"] = result.get("marker")

        save_json(STATE_PATH, state)

        if state["pages"] % LOG_EVERY == 0:
            elapsed = time.time() - t_loop_start
            rate = pages_this_run / elapsed if elapsed > 0 else 0
            log(
                f"pages={state['pages']} amms={len(index)} "
                f"raw_scanned~{state['raw_objects_scanned']:,} "
                f"this_run_rate={rate:.2f} pages/s"
            )

        if state["pages"] % SAVE_INDEX_EVERY == 0:
            save_json(INDEX_PATH, index)

        if state["marker"] is None:
            # Done.
            save_json(INDEX_PATH, index)
            state["finished_at"] = datetime.now(timezone.utc).isoformat()
            save_json(STATE_PATH, state)
            log(
                f"COMPLETE: {len(index)} AMMs across {state['pages']} pages, "
                f"~{state['raw_objects_scanned']:,} raw objects scanned, "
                f"ledger {state['ledger_index']}"
            )
            return 0


if __name__ == "__main__":
    sys.exit(main())
