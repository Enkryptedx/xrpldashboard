"""
Live mainnet Credentials tracker for /credentials.

Credentials activated 2025-09-04 (XLS-70). Mainnet usage is sparse, so
a per-request live count via ledger_data is infeasible: Clio walks the
global SHAMap and filters by type, so a full pass takes ~10 minutes and
typically returns single-digit results.

Solution: a single-shot walker (credentials_walker.py) runs on launchd
every 30 min and writes a snapshot to Postgres. The /credentials route
reads from Postgres only. This module exposes `run_once()` for the
walker and `get_credentials_state()` for the route.

Each walker pass does three things:
  - Amendment status refresh via the `feature` RPC.
  - Cumulative walk via `account_objects type=credential` for every
    account in the persisted seed set, auto-expanding the seed set with
    each newly-discovered issuer / subject. Bounded by a time budget;
    the page frames the count as a FLOOR with an "exhausted: true/false"
    indicator.
  - Recent-activity scan: walks back from the current ledger and counts
    CredentialCreate / CredentialAccept / CredentialDelete transactions
    within a fixed per-cycle time budget. The page labels the window
    with the ACTUAL earliest close-time covered, not a notional "last
    24 hours."
"""

import logging
import os
import threading
import time

import httpx

import db

XRPL_FULL = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
XRPL_CLIO = os.environ.get("XRPL_CLIO_NODE", "https://s2.ripple.com:51234")

CUMULATIVE_BUDGET_SECONDS = int(os.environ.get("CREDENTIALS_CUM_BUDGET", str(5 * 60)))
RECENT_BUDGET_SECONDS = int(os.environ.get("CREDENTIALS_RECENT_BUDGET", str(3 * 60)))

WALK_PAGE_LIMIT = 2000
EXAMPLE_LIMIT = 10
LSF_ACCEPTED = 0x00010000
XRPL_EPOCH_OFFSET = 946684800

CREDENTIAL_TX_TYPES = ("CredentialCreate", "CredentialAccept", "CredentialDelete")

# Seed set for the account_objects-based walker. Each account is queried for
# its Credential ledger objects (`account_objects type=credential` returns
# both credentials issued BY and TO the account). New issuer/subject addresses
# discovered in each cycle are merged into the seed set and persisted, so the
# walker's coverage grows monotonically across runs. The supplemental SHAMap
# walk (kept for new-issuer discovery, run rarely) is what bootstraps brand-new
# issuers we don't yet know about.
SEED_ACCOUNTS_BOOTSTRAP = [
    "rLGRav6ziCMTpQGeobWXz9gAs8kBstr2jU",
    "rKT9K5764jvejpmPQfeKoVb6vuG8LTqenV",
    "rKX81Nyg4LiBJwxNibKnwvwLNoDTx5iQXV",
    "rUhvWdQZKAyzVJPa3TiX9fULQswc5iinMn",
    "rm9oxnf2QRUckx5YDVYHMCN1stbu1ngjW",
    "rLwjrhYnjVCDHKdTD2zSNUEy2k3WzcgdV8",
    "rwLtR6jCNAMddG6udvXiCV3XUm4XNtGK2N",
    "raBopEaxzWRWsjqzXu2Ykur1YndYEFbgWG",
    "r4C88NG3qbu9z9eqRvCaXZXbGGoVFaLzTi",
    "r9JoiCWSrNcebGfRoc8PP95SUQxXDAo4hZ",
    "rNGz3rwHA9g7ABEzR7FgWNjuoEBY8ruANx",
    "r97MGUDC6v7szXXu1JurAWQarueCi4isEv",
    "rGKMDUdmquUCvoXxhcJSttnVnr2NYc9fYi",
    "rJi1NFyZia2KfY472aQAUiDCr5Ssz5pXqk",
]
ACCOUNT_OBJECTS_PAGE_LIMIT = 400

log = logging.getLogger("credentials_state")

_state_lock = threading.Lock()
_state = {
    "amendment": None,
    "cumulative": None,
    "recent": None,
    "started_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}


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


def _account_objects_credentials(account, ledger_index="validated"):
    """Return all Credential ledger objects for an account, paginated."""
    objs = []
    marker = None
    while True:
        params = {
            "account": account,
            "type": "credential",
            "ledger_index": ledger_index,
            "limit": ACCOUNT_OBJECTS_PAGE_LIMIT,
        }
        if marker:
            params["marker"] = marker
        res = _post(XRPL_CLIO, "account_objects", params, timeout=15)
        if res is None:
            return objs
        for o in res.get("account_objects") or []:
            if o.get("LedgerEntryType") == "Credential":
                objs.append(o)
        marker = res.get("marker")
        if not marker:
            break
    return objs


def _walk_via_account_objects(seed_accounts):
    """Discover credentials by walking `account_objects type=credential` for
    every known account, auto-expanding the seed set with each new issuer or
    subject encountered. Converges when no new accounts are discovered in a
    pass — typically 2-3 iterations.

    Unlike the SHAMap walk, this is exhaustive PER ACCOUNT (Clio returns every
    credential involving that account) and bounded by the size of the seed set,
    not by the size of global state. The trade-off: it can only find credentials
    whose issuer OR subject is already in the seed set. New issuers are picked
    up by the supplemental `_walk_cumulative` discovery scan.
    """
    started_at = time.time()
    started_at_iso = _now_iso()
    deadline = started_at + CUMULATIVE_BUDGET_SECONDS

    seed_set = set(seed_accounts or []) | set(SEED_ACCOUNTS_BOOTSTRAP)
    queried = set()
    credentials_by_index = {}
    accounts_queried = 0
    ledger_index = None

    while time.time() < deadline:
        to_query = seed_set - queried
        if not to_query:
            break
        for acc in sorted(to_query):
            if time.time() >= deadline:
                break
            objs = _account_objects_credentials(acc)
            queried.add(acc)
            accounts_queried += 1
            for o in objs:
                idx = o.get("index")
                if not idx:
                    continue
                if idx not in credentials_by_index:
                    credentials_by_index[idx] = _clean_credential(o)
                iss = o.get("Issuer")
                sub = o.get("Subject")
                if iss:
                    seed_set.add(iss)
                if sub:
                    seed_set.add(sub)

    exhausted = (seed_set == queried)
    credentials = list(credentials_by_index.values())
    issuers = {c["issuer"] for c in credentials if c.get("issuer")}
    subjects = {c["subject"] for c in credentials if c.get("subject")}
    accepted = sum(1 for c in credentials if c.get("accepted"))
    self_issued = sum(1 for c in credentials if c.get("self_issued"))

    return {
        "count": len(credentials),
        "count_floor": len(credentials),
        "exhausted": exhausted,
        "method": "account_objects_seed",
        "accounts_queried": accounts_queried,
        "seed_set_size": len(seed_set),
        "seed_accounts": sorted(seed_set),
        "issuers_distinct": len(issuers),
        "subjects_distinct": len(subjects),
        "accepted_count": accepted,
        "self_issued_count": self_issued,
        "examples": credentials[:EXAMPLE_LIMIT],
        "started_at_iso": started_at_iso,
        "fetched_at_iso": _now_iso(),
        "duration_seconds": int(time.time() - started_at),
        "node": XRPL_CLIO,
        "ledger_index": ledger_index,
    }


def _walk_cumulative():
    """One pass of ledger_data type=credential, bounded by time budget."""
    marker = None
    pages = 0
    credentials = []
    started_at = time.time()
    started_at_iso = _now_iso()
    deadline = started_at + CUMULATIVE_BUDGET_SECONDS
    exhausted = False
    # Pin to the first response's ledger_index so the marker walk stays
    # consistent across pagination. ledger_index="validated" re-resolves
    # to a new ledger on every request, which can desync the marker
    # mid-walk and cause us to skip whole regions of state.
    pinned_ledger_index = None
    while time.time() < deadline:
        params = {
            "type": "credential",
            "ledger_index": pinned_ledger_index or "validated",
            "limit": WALK_PAGE_LIMIT,
        }
        if marker:
            params["marker"] = marker
        res = _post(XRPL_CLIO, "ledger_data", params, timeout=30)
        if res is None:
            time.sleep(2)
            continue
        if pinned_ledger_index is None:
            pinned_ledger_index = res.get("ledger_index") or res.get("ledger_hash")
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
        "ledger_index": pinned_ledger_index,
        "issuers_distinct": len(issuers),
        "subjects_distinct": len(subjects),
        "accepted_count": accepted,
        "self_issued_count": self_issued,
        "examples": credentials[:EXAMPLE_LIMIT],
        "started_at_iso": started_at_iso,
        "fetched_at_iso": _now_iso(),
        "duration_seconds": int(time.time() - started_at),
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
    started_at = time.time()
    started_at_iso = _now_iso()
    deadline = started_at + RECENT_BUDGET_SECONDS

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
        "started_at_iso": started_at_iso,
        "fetched_at_iso": _now_iso(),
        "duration_seconds": int(time.time() - started_at),
        "node": XRPL_CLIO,
    }


def _persist_snapshot():
    """Write the in-memory state to PG so every gunicorn worker reads
    the same view. No-op when DATABASE_URL isn't configured.

    Reads PG first and only overrides keys where this worker actually has
    a value. This prevents a fresh worker (whose `_state["cumulative"]`
    might still be None because the walk lock is held elsewhere) from
    clobbering a good snapshot another worker just wrote.

    Also refuses to overwrite a cumulative snapshot whose seed set was
    larger than this walk's. A bootstrap-only walk (seed_set_size near
    len(SEED_ACCOUNTS_BOOTSTRAP)) running against an expanded existing
    snapshot would otherwise erase the discovered seed accounts and
    collapse the visible count. Genuine deletes are unaffected because
    a real walk inherits the expanded seed set via prior_seeds."""
    try:
        existing = db.read_credentials_snapshot() or {}
    except Exception:
        existing = {}
    with _state_lock:
        local_amend = dict(_state["amendment"]) if _state.get("amendment") else None
        local_cum = dict(_state["cumulative"]) if _state.get("cumulative") else None
        local_rec = dict(_state["recent"]) if _state.get("recent") else None

    existing_cum = existing.get("cumulative") or {}
    existing_seed_size = int(existing_cum.get("seed_set_size") or 0)
    if local_cum and existing_seed_size > len(SEED_ACCOUNTS_BOOTSTRAP):
        local_seed_size = int(local_cum.get("seed_set_size") or 0)
        if local_seed_size < existing_seed_size:
            log.warning(
                "credentials: refusing to persist regressed walk "
                "(local seed_size=%d count=%s vs existing seed_size=%d count=%s); "
                "keeping existing cumulative snapshot",
                local_seed_size, local_cum.get("count_floor"),
                existing_seed_size, existing_cum.get("count_floor"),
            )
            local_cum = None

    payload = {
        "amendment": local_amend or existing.get("amendment"),
        "cumulative": local_cum or existing.get("cumulative"),
        "recent": local_rec or existing.get("recent"),
        "written_at": int(time.time()),
    }
    db.write_credentials_snapshot(payload)


def run_once():
    """Single-pass walker entrypoint. Invoked by credentials_walker.py
    under launchd every 30 minutes. Forces all three steps (amendment,
    cumulative, recent) and persists the resulting snapshot to Postgres."""
    log.info("credentials walker: starting one-shot pass")

    amend = _fetch_amendment_status()
    if amend is not None:
        with _state_lock:
            _state["amendment"] = amend

    prior_seeds = []
    try:
        pg_snap = db.read_credentials_snapshot() or {}
        prior_seeds = (pg_snap.get("cumulative") or {}).get("seed_accounts") or []
    except Exception:
        prior_seeds = []
    cum = _walk_via_account_objects(prior_seeds)
    with _state_lock:
        _state["cumulative"] = cum
    log.info(
        "credentials walker: cumulative — count=%d exhausted=%s accounts_queried=%d seed_size=%d",
        cum["count"], cum["exhausted"], cum["accounts_queried"], cum["seed_set_size"],
    )

    rec = _scan_recent()
    if rec is not None:
        with _state_lock:
            _state["recent"] = rec
        log.info(
            "credentials walker: recent — scanned=%d total_tx=%d",
            rec["ledgers_scanned"], rec["total"],
        )

    _persist_snapshot()
    log.info("credentials walker: persisted snapshot")


def get_credentials_state():
    """Return the latest snapshot. Reads only from Postgres — the walker
    writes there on its launchd cadence and every gunicorn worker reads
    the same view. Falls back to an empty snapshot if PG is unavailable
    or hasn't been written yet."""
    pg_snap = db.read_credentials_snapshot()
    if pg_snap and (pg_snap.get("amendment") or pg_snap.get("cumulative")):
        pg_snap["now_iso"] = _now_iso()
        pg_snap["initial_scan_complete"] = bool(
            pg_snap.get("cumulative") and pg_snap.get("amendment")
        )
        pg_snap["source"] = "postgres"
        return pg_snap

    return {
        "amendment": None,
        "cumulative": None,
        "recent": None,
        "started_at_iso": _state["started_at_iso"],
        "now_iso": _now_iso(),
        "source": "empty",
        "initial_scan_complete": False,
    }
