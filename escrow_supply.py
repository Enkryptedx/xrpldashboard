"""
Live total of XRP locked in EscrowCreate ledger objects owned by Ripple's
20 monthly-release escrow accounts (the same `category=ripple` cohort used
by cold_storage.py, minus the RLUSD issuer).

This complements cold_storage.py: cold_storage sums the *liquid* balance
sitting in those accounts (XRP that has unlocked but not yet been swept),
while this module sums the *locked* EscrowCreate objects they own — i.e.
the multi-year contracted-but-not-yet-released supply.

Hourly cache because the locked total only changes once a month on Ripple's
release schedule. Reads route through xrpl_client.get_client() (local
rippled primary, s1/s2 fallback) so a load-shedding local node cascades
cleanly instead of returning partial data. Thread-safe.
"""

import os
import threading
import time

from xrpl.models.requests import AccountObjects

from cold_storage import _load_named  # reuse named_accounts.json loader
from xrpl_client import get_client

CACHE_TTL = int(os.environ.get("ESCROW_CACHE_TTL", "3600"))

_cache_lock = threading.Lock()
_cache = {"fetched_at": 0.0, "data": None}


def _sum_account_escrows(client, address):
    total = 0.0
    count = 0
    marker = None
    while True:
        req = AccountObjects(
            account=address,
            type="escrow",
            limit=400,
            marker=marker,
            ledger_index="validated",
        )
        try:
            resp = client.request(req)
        except Exception:
            return None
        result = resp.result or {}
        if "error" in result:
            return None
        for obj in result.get("account_objects") or []:
            amt = obj.get("Amount")
            if isinstance(amt, str):
                try:
                    total += int(amt) / 1_000_000
                    count += 1
                except (TypeError, ValueError):
                    pass
        marker = result.get("marker")
        if not marker:
            break
    return {"total_xrp": total, "object_count": count}


def fetch_escrow_locked():
    """Sum EscrowCreate amounts across Ripple's monthly-release accounts."""
    named = _load_named()
    addresses = [
        addr
        for addr, meta in named.items()
        if isinstance(meta, dict)
        and meta.get("category") == "ripple"
        and "RLUSD" not in (meta.get("name") or "")
    ]

    client = get_client("escrow_supply")
    total_xrp = 0.0
    object_count = 0
    fetched_ok = 0
    for addr in addresses:
        r = _sum_account_escrows(client, addr)
        if r is None:
            continue
        fetched_ok += 1
        total_xrp += r["total_xrp"]
        object_count += r["object_count"]

    return {
        "total_xrp": total_xrp,
        "object_count": object_count,
        "accounts_scanned": fetched_ok,
        "accounts_total": len(addresses),
    }


def fetch_escrow_locked_cached(ttl=None):
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < ttl:
            data = dict(_cache["data"])
            data["cached_age_seconds"] = round(now - _cache["fetched_at"], 1)
            return data
        fresh = fetch_escrow_locked()
        if fresh.get("accounts_scanned", 0) > 0:
            _cache["fetched_at"] = now
            _cache["data"] = fresh
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result


if __name__ == "__main__":
    t0 = time.time()
    d = fetch_escrow_locked()
    print(f"fetched in {time.time()-t0:.1f}s")
    print(f"accounts: {d['accounts_scanned']}/{d['accounts_total']}")
    print(f"escrow objects: {d['object_count']}")
    print(f"total locked: {d['total_xrp']:,.0f} XRP")
