"""
Live mainnet Credentials tracker for /credentials.

Credentials activated 2025-09-04 (XLS-70). Mainnet usage is sparse, so
a per-request live count via ledger_data is infeasible: Clio walks the
global SHAMap and filters by type, so a full pass takes ~10 minutes and
typically returns single-digit results.

Solution: a background daemon thread runs two scans on independent
cadences, and the route reads from in-memory state. Until the first
cycle completes, the page surfaces the activation badge + diagram +
"initial scan in progress" framing.

  - Cumulative walk: paginated `ledger_data type=credential` against
    s2.ripple.com (Clio). Refreshes every 6h. May not exhaust within the
    per-cycle time budget; the page frames the count as a FLOOR with an
    "exhausted: true/false" indicator.

  - Recent-activity scan: walks back from the current ledger and counts
    CredentialCreate / CredentialAccept / CredentialDelete transactions
    within a fixed per-cycle time budget. Refreshes every 30 min. The
    page labels the window with the ACTUAL earliest close-time covered,
    not a notional "last 24 hours."
"""

import logging
import os
import threading
import time

import httpx

XRPL_FULL = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
XRPL_CLIO = os.environ.get("XRPL_CLIO_NODE", "https://s2.ripple.com:51234")

CUMULATIVE_REFRESH_SECONDS = int(os.environ.get("CREDENTIALS_CUM_REFRESH", str(6 * 3600)))
CUMULATIVE_BUDGET_SECONDS = int(os.environ.get("CREDENTIALS_CUM_BUDGET", str(12 * 60)))
RECENT_REFRESH_SECONDS = int(os.environ.get("CREDENTIALS_RECENT_REFRESH", str(30 * 60)))
RECENT_BUDGET_SECONDS = int(os.environ.get("CREDENTIALS_RECENT_BUDGET", str(3 * 60)))

WALK_PAGE_LIMIT = 2000
EXAMPLE_LIMIT = 10
LSF_ACCEPTED = 0x00010000
XRPL_EPOCH_OFFSET = 946684800

CREDENTIAL_TX_TYPES = ("CredentialCreate", "CredentialAccept", "CredentialDelete")

log = logging.getLogger("credentials_state")

_state_lock = threading.Lock()
_state = {
    "amendment": None,
    "cumulative": None,
    "recent": None,
    "started_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
_thread = None
_thread_lock = threading.Lock()


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _xrpl_close_to_iso(close_time):
    if close_time is None:
        return None
    try:
        return time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(int(close_time) + XRPL_EPOCH_OFFSET),
        )
    except (TypeError, ValueError):
        return None


def _post(url, method, params, timeout=20):
    try:
        r = httpx.post(
            url,
            json={"method": method, "params": [params]},
            timeout=timeout,
        )
        return (r.json() or {}).get("result") or {}
    except Exception as exc:
        log.debug("rpc %s failed: %s", method, exc)
        return None


def _hex_to_utf8_safe(hex_str):
    if not hex_str or not isinstance(hex_str, str):
        return None
    try:
        decoded = bytes.fromhex(hex_str).decode("utf-8")
    except Exception:
        return None
    if any(ord(c) < 0x20 and c not in "\t\n\r" for c in decoded):
        return None
    return decoded


def _clean_credential(e):
    flags = int(e.get("Flags") or 0)
    return {
        "index": e.get("index"),
        "issuer": e.get("Issuer"),
        "subject": e.get("Subject"),
        "credential_type_hex": e.get("CredentialType"),
        "credential_type_label": _hex_to_utf8_safe(e.get("CredentialType")),
        "uri_hex": e.get("URI"),
        "uri_label": _hex_to_utf8_safe(e.get("URI")),
        "flags": flags,
        "accepted": bool(flags & LSF_ACCEPTED),
        "self_issued": e.get("Issuer") == e.get("Subject"),
        "expiration_xrpl": e.get("Expiration"),
        "expiration_iso": _xrpl_close_to_iso(e.get("Expiration")),
    }


def _fetch_amendment_status():
    res = _post(XRPL_FULL, "feature", {})
    if not res:
        return None
    features = res.get("features") or {}
    for h, info in features.items():
        if not isinstance(info, dict):
            continue
        if (info.get("name") or "").lower() == "credentials":
            return {
                "hash": h,
                "name": info.get("name"),
                "enabled": bool(info.get("enabled")),
                "supported": bool(info.get("supported")),
                "fetched_at_iso": _now_iso(),
            }
    return {"hash": None, "name": "Credentials", "enabled": None,
            "supported": None, "fetched_at_iso": _now_iso()}


def _walk_cumulative():
    """One pass of ledger_data type=credential, bounded by time budget."""
    marker = None
    pages = 0
    credentials = []
    deadline = time.time() + CUMULATIVE_BUDGET_SECONDS
    exhausted = False
    while time.time() < deadline:
        params = {
            "type": "credential",
            "ledger_index": "validated",
            "limit": WALK_PAGE_LIMIT,
        }
        if marker:
            params["marker"] = marker
        res = _post(XRPL_CLIO, "ledger_data", params, timeout=30)
        if res is None:
            time.sleep(2)
            continue
        pages += 1
        for e in res.get("state") or []:
            if e.get("LedgerEntryType") == "Credential":
                credentials.append(_clean_credential(e))
        marker = res.get("marker")
        if not marker:
            exhausted = True
            break

    issuers = {c["issuer"] for c in credentials if c.get("issuer")}
    subjects = {c["subject"] for c in credentials if c.get("subject")}
    accepted = sum(1 for c in credentials if c.get("accepted"))
    self_issued = sum(1 for c in credentials if c.get("self_issued"))

    return {
        "count_floor": len(credentials),
        "exhausted": exhausted,
        "pages_scanned": pages,
        "page_limit": WALK_PAGE_LIMIT,
        "issuers_distinct": len(issuers),
        "subjects_distinct": len(subjects),
        "accepted_count": accepted,
        "self_issued_count": self_issued,
        "examples": credentials[:EXAMPLE_LIMIT],
        "fetched_at_iso": _now_iso(),
        "node": XRPL_CLIO,
    }


def _scan_recent():
    """Sweep recent validated ledgers for Credential* txs within time budget."""
    cur = _post(XRPL_CLIO, "ledger_current", {})
    if not cur:
        return None
    head = cur.get("ledger_current_index")
    if not head:
        return None

    counts = {t: 0 for t in CREDENTIAL_TX_TYPES}
    ledgers_scanned = 0
    earliest_close_iso = None
    latest_close_iso = None
    deadline = time.time() + RECENT_BUDGET_SECONDS

    offset = 2
    while time.time() < deadline:
        li = head - offset
        offset += 1
        if li < 0:
            break
        res = _post(
            XRPL_CLIO,
            "ledger",
            {"ledger_index": li, "transactions": True, "expand": True},
            timeout=15,
        )
        if res is None:
            continue
        ledger = res.get("ledger") or {}
        ledgers_scanned += 1
        close = ledger.get("close_time")
        close_iso = _xrpl_close_to_iso(close)
        if close_iso:
            if latest_close_iso is None:
                latest_close_iso = close_iso
            earliest_close_iso = close_iso
        for t in ledger.get("transactions") or []:
            if isinstance(t, dict):
                tt = t.get("TransactionType")
                if tt in counts:
                    counts[tt] += 1

    window_seconds = None
    if earliest_close_iso and latest_close_iso:
        try:
            t1 = time.mktime(time.strptime(latest_close_iso, "%Y-%m-%dT%H:%M:%SZ"))
            t0 = time.mktime(time.strptime(earliest_close_iso, "%Y-%m-%dT%H:%M:%SZ"))
            window_seconds = int(t1 - t0)
        except Exception:
            window_seconds = None

    return {
        "ledgers_scanned": ledgers_scanned,
        "head_ledger": head,
        "creates": counts["CredentialCreate"],
        "accepts": counts["CredentialAccept"],
        "deletes": counts["CredentialDelete"],
        "total": sum(counts.values()),
        "window_earliest_close_iso": earliest_close_iso,
        "window_latest_close_iso": latest_close_iso,
        "window_seconds": window_seconds,
        "fetched_at_iso": _now_iso(),
        "node": XRPL_CLIO,
    }


def _refresh_loop():
    last_cum = 0.0
    last_recent = 0.0
    last_amend = 0.0
    while True:
        try:
            now = time.time()
            if now - last_amend > 6 * 3600 or _state.get("amendment") is None:
                amend = _fetch_amendment_status()
                if amend is not None:
                    with _state_lock:
                        _state["amendment"] = amend
                    last_amend = now

            if now - last_cum > CUMULATIVE_REFRESH_SECONDS or _state.get("cumulative") is None:
                log.info("credentials: starting cumulative walk")
                cum = _walk_cumulative()
                with _state_lock:
                    _state["cumulative"] = cum
                last_cum = time.time()
                log.info(
                    "credentials: cumulative done — floor=%d exhausted=%s pages=%d",
                    cum["count_floor"], cum["exhausted"], cum["pages_scanned"],
                )

            if now - last_recent > RECENT_REFRESH_SECONDS or _state.get("recent") is None:
                log.info("credentials: starting recent-activity scan")
                rec = _scan_recent()
                if rec is not None:
                    with _state_lock:
                        _state["recent"] = rec
                    last_recent = time.time()
                    log.info(
                        "credentials: recent done — scanned=%d total_tx=%d",
                        rec["ledgers_scanned"], rec["total"],
                    )
        except Exception as exc:
            log.exception("credentials refresh loop error: %s", exc)
        time.sleep(60)


def _ensure_thread():
    global _thread
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(
                target=_refresh_loop,
                name="credentials-refresh",
                daemon=True,
            )
            _thread.start()


def get_credentials_state():
    _ensure_thread()
    with _state_lock:
        snapshot = {
            "amendment": dict(_state["amendment"]) if _state.get("amendment") else None,
            "cumulative": dict(_state["cumulative"]) if _state.get("cumulative") else None,
            "recent": dict(_state["recent"]) if _state.get("recent") else None,
            "started_at_iso": _state["started_at_iso"],
            "now_iso": _now_iso(),
        }
    snapshot["initial_scan_complete"] = bool(
        snapshot.get("cumulative") and snapshot.get("amendment")
    )
    return snapshot
