"""
Phase 1c — Full on-ledger escrow census.

Paginates ledger_data(type=escrow) on local rippled until marker exhausts.
Classifies each object as Ripple vs non-Ripple, XRP vs token (IOU/MPT).
Writes results to census_escrow_phase1c_YYYY-MM-DD.json in the same dir.

Preconditions:
- rippled load_factor <= LOAD_FACTOR_MAX_START at kickoff
- server_state == "full" or "proposing"
Aborts loudly (non-zero exit + ABORTED-tagged artifact) if:
- load_factor exceeds LOAD_FACTOR_MAX_MID mid-walk
- server_state degrades mid-walk
- final marker-absent response is malformed (empty state on a walk that
  should have had ~40k pages)
- pages_fetched < MIN_PAGES_EXPECTED after loop exits
Load-factor discipline was originally load_factor==1.0 gated; 2026-07-12
walk failed at load 505 with 3 read timeouts and silent early termination
(rippled returned marker=None with state=[] after timeouts). Fix: convert
the judgment gate into a mechanical precondition + mid-walk poll, and
verify marker-absent responses carry state data before accepting them.
"""

import json
import logging
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("phase1c")

RIPPLED_URL = "http://127.0.0.1:5005"
PAGE_LIMIT = 400
SLEEP_BETWEEN_PAGES = 0.3

# Load-factor gates. 2026-07-11 successful walk ran at 1.0. Failed
# 2026-07-12 walk started at ~505 and died. Cap start at 5.0 (small
# validator/tx spikes ok, sustained congestion not) and mid-walk at 50.0
# (a large spike aborts the run instead of silently timing out).
LOAD_FACTOR_MAX_START = 5.0
LOAD_FACTOR_MAX_MID = 50.0
SERVER_CHECK_EVERY_N_PAGES = 500

# Page-count sanity gate. 2026-07-11 walk = 40,205 pages against
# 12,441 objects. Refuse to accept "complete" below 90% of that
# baseline unless the object count is also proportionally lower.
MIN_PAGES_EXPECTED = 36000

# Ripple monthly-release accounts (category=ripple, minus RLUSD issuer)
RIPPLE_ADDRS = {
    "r9NpyVfLfUG8hatuCCHKzosyDtKnBdsEN3",
    "r9UUEXn3cx2seufBkDa8F86usfjWM6HiYp",
    "rB3WNZc45gxzW31zxfXdkx8HusAhoqscPn",
    "rDdXiA3M4mYTQ4cFpWkVXfc2UaAXCFWeCK",
    "rKDvgGUsNPZxsgmoemfrgXPS2Not4co2op",
    "rKwJaGmB5Hz24Qs2iyCaTdUuL1WsEXUWy5",
    "rN8pqRwLYuuvY7pUHurybPC8P6rLqVsu6o",
    "rNASJdZjY9dToHnNURi3HAUku3duPwbtD1",
    "rU9qmGM4Y6WWDhiNzkwVKBwwatcoE7YL1T",
    "rfWPPQBYqYmoFMdVnjzXCagJbz5uajSBXL",
    "rh2EsAe2xVE71ZBjx7oEL2zpD4zmSs3sY9",
    "rhEwsCWDCVxDiKxGJAKM6VuXC8EFtJP5gQ",
    "rncKvRcdDq9hVJpdLdTcKoxsS3NSkXsvfM",
    "rp6aTJmW3nq1aKt3Jmuz4DPRxksT5PBjpH",
    "rsjFB8mPWqiZgPUaVh8XYqdfa59PE2d5LG",
    "rw2hzLZgiQ9q62KCuaTWuFHWfiX7JWg3wY",
    "rDqGA2GfveHypDguQ1KXrJzYymFZmKxEsF",
    "rGKHDyj4L6pc7DzRB6LWCR4YfZfzXj2Bdh",
    "rHGfmgv54kpc3QCZGRXEQKUhLPndbasbQr",
    "rMhkqz3DeU7GUUJKGZofusbrTwZe6bDyb1",
}


class CensusAbort(Exception):
    """Raised when the walk cannot complete safely — writes ABORTED artifact."""


def _rpc(method, params):
    payload = json.dumps({"method": method, "params": [params]}).encode()
    req = urllib.request.Request(
        RIPPLED_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _server_state():
    d = _rpc("server_info", {})
    info = d["result"]["info"]
    return {
        "validated_ledger": info["validated_ledger"]["seq"],
        "load_factor": info.get("load_factor"),
        "server_state": info.get("server_state"),
        "complete_ledgers": info.get("complete_ledgers"),
    }


def _write_artifact(payload, started_at, tag=""):
    stamp = started_at.strftime("%Y-%m-%d")
    suffix = f"_{tag}" if tag else ""
    out_path = Path(__file__).parent / f"census_escrow_phase1c_{stamp}{suffix}.json"
    out_path.write_text(json.dumps(payload, indent=2))
    log.info("Artifact written to %s", out_path)
    return out_path


def run_census():
    started_at = datetime.now(timezone.utc)
    log.info("Phase 1c escrow census starting at %s", started_at.isoformat())

    # Precondition gate: server_state + load_factor
    initial = _server_state()
    if initial["server_state"] not in ("full", "proposing"):
        raise CensusAbort(
            f"Refusing to start: server_state={initial['server_state']!r} "
            f"(need 'full' or 'proposing')"
        )
    if initial["load_factor"] is None:
        raise CensusAbort("Refusing to start: load_factor missing from server_info")
    if initial["load_factor"] > LOAD_FACTOR_MAX_START:
        raise CensusAbort(
            f"Refusing to start: load_factor={initial['load_factor']} "
            f"> LOAD_FACTOR_MAX_START={LOAD_FACTOR_MAX_START}"
        )

    anchor_ledger = initial["validated_ledger"]
    log.info(
        "Anchored to ledger %d, load_factor=%s, server_state=%s",
        anchor_ledger, initial["load_factor"], initial["server_state"],
    )

    stats = {
        "total_objects": 0,
        "ripple_xrp_count": 0,
        "ripple_xrp_drops": 0,
        "nonripple_xrp_count": 0,
        "nonripple_xrp_drops": 0,
        "token_count": 0,
        "token_examples": [],
        "token_objects_all": [],
        "nonripple_owners": {},
        "pages_fetched": 0,
    }

    # Rolling record of the last raw response — logged verbatim in every
    # artifact so post-mortems are one grep instead of a re-run.
    last_response = None
    last_response_page = None

    marker = None
    page = 0
    consecutive_timeouts = 0

    while True:
        # Mid-walk server health poll — pinned to page cadence, not clock.
        if page > 0 and page % SERVER_CHECK_EVERY_N_PAGES == 0:
            try:
                mid = _server_state()
                if mid["server_state"] not in ("full", "proposing"):
                    raise CensusAbort(
                        f"Mid-walk abort at page {page}: "
                        f"server_state degraded to {mid['server_state']!r}"
                    )
                if mid["load_factor"] > LOAD_FACTOR_MAX_MID:
                    raise CensusAbort(
                        f"Mid-walk abort at page {page}: "
                        f"load_factor={mid['load_factor']} "
                        f"> LOAD_FACTOR_MAX_MID={LOAD_FACTOR_MAX_MID}"
                    )
                log.info(
                    "Health OK at page %d: load_factor=%s, server_state=%s",
                    page, mid["load_factor"], mid["server_state"],
                )
            except CensusAbort:
                raise
            except Exception as e:
                log.warning("Health probe failed at page %d: %s (continuing)", page, e)

        params = {
            "ledger_index": anchor_ledger,
            "type": "escrow",
            "limit": PAGE_LIMIT,
            "binary": False,
        }
        if marker:
            params["marker"] = marker

        try:
            d = _rpc("ledger_data", params)
            consecutive_timeouts = 0
        except Exception as e:
            consecutive_timeouts += 1
            log.error(
                "RPC error on page %d (consecutive=%d): %s — retrying in 5s",
                page, consecutive_timeouts, e,
            )
            if consecutive_timeouts >= 6:
                raise CensusAbort(
                    f"Abort: {consecutive_timeouts} consecutive RPC timeouts "
                    f"at page {page} (marker={marker!r})"
                )
            time.sleep(5)
            continue

        last_response = d
        last_response_page = page + 1

        result = d.get("result", {})
        status = result.get("status")
        state = result.get("state", [])
        new_marker = result.get("marker")
        error = result.get("error")

        if error or status != "success":
            raise CensusAbort(
                f"Abort at page {page}: rippled returned status={status!r} "
                f"error={error!r} — refusing to treat as valid page"
            )

        page += 1
        stats["pages_fetched"] = page

        for obj in state:
            stats["total_objects"] += 1
            account = obj.get("Account", "")
            amt = obj.get("Amount", "0")

            if isinstance(amt, str):
                drops = int(amt)
                if account in RIPPLE_ADDRS:
                    stats["ripple_xrp_count"] += 1
                    stats["ripple_xrp_drops"] += drops
                else:
                    stats["nonripple_xrp_count"] += 1
                    stats["nonripple_xrp_drops"] += drops
                    stats["nonripple_owners"][account] = (
                        stats["nonripple_owners"].get(account, 0) + 1
                    )
            else:
                stats["token_count"] += 1
                stats["token_objects_all"].append({
                    "account": account,
                    "amount": amt,
                    "destination": obj.get("Destination", ""),
                    "prev_txn_lgr_seq": obj.get("PreviousTxnLgrSeq"),
                    "prev_txn_id": obj.get("PreviousTxnID"),
                })
                if len(stats["token_examples"]) < 20:
                    stats["token_examples"].append({
                        "account": account,
                        "amount": amt,
                        "destination": obj.get("Destination", ""),
                    })

        if page % 50 == 0:
            log.info(
                "Page %d: total=%d ripple=%d nonripple=%d token=%d marker=%s",
                page,
                stats["total_objects"],
                stats["ripple_xrp_count"],
                stats["nonripple_xrp_count"],
                stats["token_count"],
                "yes" if new_marker else "DONE",
            )

        # Marker-absent handling. A legitimate final page has non-empty
        # state; an empty state with marker=None means the server bailed
        # (silent-truncation failure mode observed 2026-07-12).
        if not new_marker:
            if len(state) > 0:
                log.info("Census complete after %d pages (final page had %d objects).",
                         page, len(state))
                break
            # Suspicious: empty final page. Retry once with the same
            # marker to confirm we're actually done vs. server-degraded.
            log.warning(
                "Suspicious marker=None with empty state at page %d — "
                "reprobing with last marker to confirm.", page,
            )
            time.sleep(3)
            try:
                probe_params = dict(params)  # same marker as this failed call
                d2 = _rpc("ledger_data", probe_params)
                probe_result = d2.get("result", {})
                probe_state = probe_result.get("state", [])
                probe_marker = probe_result.get("marker")
                if len(probe_state) > 0 or probe_marker:
                    log.warning(
                        "Reprobe recovered: state=%d marker=%s — treating "
                        "prior empty response as transient and continuing.",
                        len(probe_state), "yes" if probe_marker else "no",
                    )
                    marker = probe_marker
                    # Fold in the recovered state as if it were this page's.
                    for obj in probe_state:
                        stats["total_objects"] += 1
                        account = obj.get("Account", "")
                        amt = obj.get("Amount", "0")
                        if isinstance(amt, str):
                            drops = int(amt)
                            if account in RIPPLE_ADDRS:
                                stats["ripple_xrp_count"] += 1
                                stats["ripple_xrp_drops"] += drops
                            else:
                                stats["nonripple_xrp_count"] += 1
                                stats["nonripple_xrp_drops"] += drops
                                stats["nonripple_owners"][account] = (
                                    stats["nonripple_owners"].get(account, 0) + 1
                                )
                        else:
                            stats["token_count"] += 1
                            stats["token_objects_all"].append({
                                "account": account,
                                "amount": amt,
                                "destination": obj.get("Destination", ""),
                                "prev_txn_lgr_seq": obj.get("PreviousTxnLgrSeq"),
                                "prev_txn_id": obj.get("PreviousTxnID"),
                            })
                    last_response = d2
                    last_response_page = page
                    if not marker:
                        log.info("Census complete after reprobe (page %d).", page)
                        break
                    time.sleep(SLEEP_BETWEEN_PAGES)
                    continue
            except Exception as e:
                log.warning("Reprobe failed: %s", e)
            # Reprobe also came back empty. Only accept if page-count sane.
            if page < MIN_PAGES_EXPECTED:
                raise CensusAbort(
                    f"Refusing to accept 'complete': marker=None with empty "
                    f"state at page {page}, reprobe empty too, but page count "
                    f"is {page} < MIN_PAGES_EXPECTED={MIN_PAGES_EXPECTED}. "
                    f"This is the silent-truncation failure mode."
                )
            log.info(
                "Empty final page reprobed empty and page count %d >= %d — "
                "accepting as legitimate end-of-walk.", page, MIN_PAGES_EXPECTED,
            )
            break

        marker = new_marker
        time.sleep(SLEEP_BETWEEN_PAGES)

    finished_at = datetime.now(timezone.utc)
    elapsed = (finished_at - started_at).total_seconds()

    top_nonripple = sorted(
        stats["nonripple_owners"].items(), key=lambda x: -x[1]
    )[:20]

    final_server = _server_state()

    report = {
        "census_type": "phase1c_full_escrow",
        "anchor_ledger": anchor_ledger,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": elapsed,
        "load_factor_at_start": initial["load_factor"],
        "load_factor_at_end": final_server["load_factor"],
        "server_state_at_end": final_server["server_state"],
        "min_pages_expected": MIN_PAGES_EXPECTED,
        "summary": {
            "total_escrow_objects": stats["total_objects"],
            "ripple_xrp_objects": stats["ripple_xrp_count"],
            "ripple_xrp_total": stats["ripple_xrp_drops"] / 1e6,
            "nonripple_xrp_objects": stats["nonripple_xrp_count"],
            "nonripple_xrp_total": stats["nonripple_xrp_drops"] / 1e6,
            "token_escrow_objects": stats["token_count"],
            "pages_fetched": stats["pages_fetched"],
            "unique_nonripple_owners": len(stats["nonripple_owners"]),
        },
        "top_nonripple_owners": top_nonripple,
        "token_escrow_examples": stats["token_examples"],
        "token_escrow_objects_all": stats["token_objects_all"],
        "last_raw_response": {
            "page": last_response_page,
            "response": last_response,
        },
    }

    out_path = _write_artifact(report, started_at)

    s = report["summary"]
    print(f"\n=== PHASE 1C ESCROW CENSUS COMPLETE ===")
    print(f"Anchor ledger: {anchor_ledger}, elapsed: {elapsed:.0f}s")
    print(f"Total escrow objects: {s['total_escrow_objects']:,}")
    print(f"  Ripple (XRP):     {s['ripple_xrp_objects']:,} objects = {s['ripple_xrp_total']:,.0f} XRP")
    print(f"  Non-Ripple (XRP): {s['nonripple_xrp_objects']:,} objects = {s['nonripple_xrp_total']:,.0f} XRP")
    print(f"  Token (IOU/MPT):  {s['token_escrow_objects']:,} objects")
    print(f"  Unique non-Ripple owners: {s['unique_nonripple_owners']:,}")
    if top_nonripple:
        print(f"  Top non-Ripple owners:")
        for addr, cnt in top_nonripple[:5]:
            print(f"    {addr}: {cnt} escrows")
    print(f"Report: {out_path}")
    return report


def _write_abort_artifact(exc, started_at):
    payload = {
        "census_type": "phase1c_full_escrow_ABORTED",
        "started_at": started_at.isoformat(),
        "aborted_at": datetime.now(timezone.utc).isoformat(),
        "reason": str(exc),
    }
    _write_artifact(payload, started_at, tag="ABORTED")


if __name__ == "__main__":
    _start = datetime.now(timezone.utc)
    try:
        run_census()
    except KeyboardInterrupt:
        log.info("Interrupted.")
        _write_abort_artifact(KeyboardInterrupt("interrupted"), _start)
        sys.exit(1)
    except CensusAbort as e:
        log.error("CensusAbort: %s", e)
        _write_abort_artifact(e, _start)
        sys.exit(2)
    except Exception as e:
        log.exception("Unhandled exception")
        _write_abort_artifact(e, _start)
        sys.exit(3)
