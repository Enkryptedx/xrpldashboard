"""Standalone one-shot walker for /permissioned-domains (XLS-80) Phase 1.

XLS-80 PermissionedDomains enabled on mainnet 2026-02-04. On-chain
adoption is essentially zero today. This walker stands up the data
layer so a future Phase 2 ships a UI against an accumulating daily
history (count rises 0→1→N as institutions adopt the primitive).

Discovery shape mirrors credentials_walker exactly. The initial design
called for a full `ledger_data type=PermissionedDomain` walk per pass,
but pre-code investigation found that the type filter is applied AFTER
SHAMap pagination — a "zero matches" first page does NOT mean "zero
PDs on mainnet," it means "no PDs in the first slice of the SHAMap."
Full coverage requires walking the entire ~25M-entry state, ~10 min
per pass even at N=0. We use bounded `account_objects type=permissioned_domain`
over an institutional seed instead — same approach credentials uses.

The seed is "starting institutional surface" — accounts most likely
to be early PermissionedDomain creators (credential issuers, KYC/
accredited-investor gating use case). Auto-expands as PD owners are
discovered. The count is honestly a FLOOR anchored to the known seed
plus discoveries, not a full ledger census.

Outputs per pass:
  - One row per discovered PD into permissioned_domains
    (PRIMARY KEY (snapshot_date, domain_id) — same-day re-run is no-op)
  - One row per pass into permissioned_domain_walker_runs
    (UPSERT on snapshot_date — same-day re-run updates duration_ms etc.)

Invoked by com.charliebruce.xrpldashboard.permissioned_domains_walker.plist
on a daily cadence.
"""

import datetime
import logging
import os
import sys
import time

import httpx

import db


XRPL_FULL = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")

TIME_BUDGET_SECONDS = int(
    os.environ.get("PERMISSIONED_DOMAINS_BUDGET", str(3 * 60))
)
ACCOUNT_OBJECTS_PAGE_LIMIT = 400
MAX_SEED_EXPANSION_ITERATIONS = 5  # cap auto-expansion to prevent runaway

# Bootstrap seed: reuses the credentials_walker institutional list (XLS-70
# adopters). KYC / accredited-investor gating is the same use case the
# PermissionedDomain primitive serves, so adoption surfaces are likely to
# overlap. Auto-expand picks up domain owners outside this list when they
# appear. See credentials_state.SEED_ACCOUNTS_BOOTSTRAP.
#
# Backlog: periodic audit of this list — see project memory
# `project_xrpldashboard_backlog_walker_seed_audit.md`. Stale entries
# are harmless (they just discover zero), but the list is shared with
# credentials_walker so an audit covers both.
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

log = logging.getLogger("permissioned_domains_walker")


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _post(url, method, params, timeout=15):
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


def _account_objects_permissioned_domains(account):
    """Return all PermissionedDomain ledger objects + the close_time of the
    ledger the response was taken from. Paginated via marker. Returns
    ([], None) on RPC failure (treated as "no objects for this account this
    pass"; next walker run retries)."""
    objs = []
    marker = None
    close_time = None
    while True:
        params = {
            "account": account,
            "type": "permissioned_domain",
            "ledger_index": "validated",
            "limit": ACCOUNT_OBJECTS_PAGE_LIMIT,
        }
        if marker:
            params["marker"] = marker
        res = _post(XRPL_FULL, "account_objects", params, timeout=15)
        if res is None:
            return objs, close_time
        for o in res.get("account_objects") or []:
            if o.get("LedgerEntryType") == "PermissionedDomain":
                objs.append(o)
        if close_time is None:
            # Per data-timestamp-over-bookkeeping rule, prefer the ledger
            # close_time over now(). account_objects doesn't include it
            # directly, but a follow-up `ledger` call on the same index
            # would; cheaper to just call it once at end of walk. For
            # Phase 1 we fall back to fetched_at if not available.
            close_time = res.get("ledger_index")
        marker = res.get("marker")
        if not marker:
            break
    return objs, close_time


def _fetch_ledger_close_time(ledger_index):
    """Return Unix epoch close_time for a given validated ledger_index.
    Used to populate permissioned_domains.ledger_close_time per the
    data-timestamp-over-bookkeeping rule. Returns None on RPC failure;
    walker will fall back to fetched_at epoch."""
    if not ledger_index:
        return None
    res = _post(
        XRPL_FULL,
        "ledger",
        {"ledger_index": int(ledger_index), "transactions": False, "expand": False},
        timeout=10,
    )
    if not res:
        return None
    led = res.get("ledger") or {}
    close = led.get("close_time")
    if close is None:
        return None
    # XRPL epoch (2000-01-01) → Unix epoch
    return int(close) + 946684800


def _clean_domain(obj, ledger_close_time_unix, fallback_unix):
    """Shape a PermissionedDomain ledger object into a row dict for
    persistence. AcceptedCredentials is stored as JSONB; cred_count is
    pre-computed for cheap rank-by-restrictiveness queries."""
    accepted = obj.get("AcceptedCredentials") or []
    # Normalize the array: spec uses {Credential: {Issuer, CredentialType}}
    # wrapper, but some implementations may inline. Persist as-is for
    # forensic fidelity; consumer code can pick either shape.
    return {
        "domain_id": obj.get("index"),
        "owner_account": obj.get("Owner"),
        "sequence": int(obj.get("Sequence") or 0),
        "accepted_credentials": accepted,
        "cred_count": len(accepted),
        "previous_txn_id": obj.get("PreviousTxnID"),
        "ledger_close_time": int(ledger_close_time_unix or fallback_unix),
    }


def run_once():
    """Single-pass walker entrypoint. Invoked by launchd daily.

    1. Walk account_objects type=permissioned_domain over the bootstrap seed
    2. Auto-expand seed with any discovered domain owners (capped iterations)
    3. Persist discovered domains into permissioned_domains
    4. Persist pass metadata (count, duration, exhaustion) into
       permissioned_domain_walker_runs
    """
    started_at = time.time()
    started_at_iso = _now_iso()
    deadline = started_at + TIME_BUDGET_SECONDS

    log.info(
        "permissioned_domains walker: starting pass (budget=%ds, seed=%d)",
        TIME_BUDGET_SECONDS, len(SEED_ACCOUNTS_BOOTSTRAP),
    )

    seed_set = set(SEED_ACCOUNTS_BOOTSTRAP)
    queried = set()
    domains_by_id = {}
    domain_ledger_index = None  # latest ledger_index any RPC saw

    for iteration in range(MAX_SEED_EXPANSION_ITERATIONS):
        to_query = seed_set - queried
        if not to_query:
            break
        for acc in sorted(to_query):
            if time.time() >= deadline:
                break
            objs, ledger_idx = _account_objects_permissioned_domains(acc)
            queried.add(acc)
            if ledger_idx is not None:
                domain_ledger_index = ledger_idx
            for o in objs:
                idx = o.get("index")
                if not idx:
                    continue
                if idx not in domains_by_id:
                    domains_by_id[idx] = o
                owner = o.get("Owner")
                if owner:
                    seed_set.add(owner)
        if time.time() >= deadline:
            break

    exhausted = (seed_set == queried)
    fallback_unix = int(started_at)
    close_time_unix = _fetch_ledger_close_time(domain_ledger_index)

    rows = [
        _clean_domain(o, close_time_unix, fallback_unix)
        for o in domains_by_id.values()
    ]

    snapshot_date = datetime.date.fromtimestamp(started_at)
    duration_ms = int((time.time() - started_at) * 1000)

    metadata = {
        "fetched_at_iso": started_at_iso,
        "seed_set_size": len(seed_set),
        "accounts_queried": len(queried),
        "domains_found": len(rows),
        "exhausted": exhausted,
        "walker_duration_ms": duration_ms,
        "notes": None,
    }

    db.write_permissioned_domains(rows, started_at_iso, snapshot_date)
    db.write_permissioned_domain_walker_run(metadata, snapshot_date)

    log.info(
        "permissioned_domains walker: done — domains_found=%d "
        "accounts_queried=%d seed_size=%d exhausted=%s duration_ms=%d",
        len(rows), len(queried), len(seed_set), exhausted, duration_ms,
    )
    return metadata


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start("permissioned_domains_walker")
    ok = False
    message = None
    try:
        meta = run_once()
        # Phase 1 state expects 0 domains for weeks — completion alone is
        # the ok signal, not domain count. permissioned_domain_walker_runs
        # owns the per-pass audit row (UPSERT on snapshot_date); this row
        # is independent and tracks the launchd-level pass health.
        ok = True
        message = (
            f"domains_found={meta.get('domains_found')} "
            f"accounts_queried={meta.get('accounts_queried')} "
            f"seed_size={meta.get('seed_set_size')} "
            f"exhausted={meta.get('exhausted')} "
            f"duration_ms={meta.get('walker_duration_ms')}"
        )
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end(
            "permissioned_domains_walker", ok=ok, message=message
        )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
