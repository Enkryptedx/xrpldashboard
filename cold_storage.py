"""
Cold-storage tracker — currently scoped to the Ripple monthly-release
escrows declared in xrp-ledger.toml. These 21 accounts hold the majority
of Ripple's contractually-locked XRP and are the canonical "cold storage"
cohort on XRPL: balances only change on the published release schedule.

Future scope (not yet wired): exchange cold wallets, foundation reserves.
The page framing makes the current scope explicit.

Live data via account_info through xrpl_client.get_client() — local
rippled primary, s1/s2 fallback. Cached 5 min so reloads don't hammer
the fallback path when the local node is refusing under load.
Thread-safe.
"""

import json
import os
import threading
import time

from xrpl.models.requests import AccountInfo

from xrpl_client import get_client

HERE = os.path.dirname(os.path.abspath(__file__))
NAMED_ACCOUNTS_PATH = os.path.join(HERE, "named_accounts.json")

CACHE_TTL = int(os.environ.get("COLD_CACHE_TTL", "300"))

_cache_lock = threading.Lock()
_cache = {"fetched_at": 0.0, "data": None}


def _load_named():
    try:
        with open(NAMED_ACCOUNTS_PATH) as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _safe_request(client, request):
    try:
        resp = client.request(request)
        if "error" in resp.result:
            return None
        return resp.result
    except Exception:
        return None


def _fetch_balance(client, address):
    result = _safe_request(client, AccountInfo(account=address, ledger_index="validated"))
    if not result:
        return None
    account_data = result.get("account_data", {}) or {}
    try:
        drops = int(account_data.get("Balance", "0"))
    except (TypeError, ValueError):
        drops = 0
    return {
        "balance_xrp": drops / 1_000_000,
        "sequence": account_data.get("Sequence"),
        "owner_count": account_data.get("OwnerCount", 0),
    }


def fetch_cold_storage():
    """Pull live balances for every category=ripple address in named_accounts.
    Returns a dict shaped for direct rendering by the template."""
    named = _load_named()
    addresses = [
        (addr, meta)
        for addr, meta in named.items()
        if isinstance(meta, dict) and meta.get("category") == "ripple"
    ]
    addresses.sort(key=lambda x: x[1].get("name") or x[0])

    client = get_client("cold_storage")
    rows = []
    total_xrp = 0.0
    fetched_ok = 0
    for addr, meta in addresses:
        bal = _fetch_balance(client, addr)
        if bal is None:
            rows.append({
                "address": addr,
                "address_short": f"{addr[:6]}…{addr[-4:]}",
                "name": meta.get("name") or addr,
                "balance_xrp": None,
                "error": True,
                "verified_via": meta.get("verified_via"),
            })
            continue
        fetched_ok += 1
        total_xrp += bal["balance_xrp"]
        rows.append({
            "address": addr,
            "address_short": f"{addr[:6]}…{addr[-4:]}",
            "name": meta.get("name") or addr,
            "balance_xrp": bal["balance_xrp"],
            "sequence": bal["sequence"],
            "error": False,
            "verified_via": meta.get("verified_via"),
        })

    rows.sort(key=lambda r: -(r["balance_xrp"] or -1))
    largest = rows[0] if rows and rows[0]["balance_xrp"] else None
    return {
        "rows": rows,
        "total_xrp": total_xrp,
        "tracked_count": len(rows),
        "escrow_account_count": sum(
            1 for _, meta in addresses
            if "RLUSD" not in (meta.get("name") or "")
        ),
        "fetched_ok": fetched_ok,
        "largest_name": largest["name"] if largest else None,
        "largest_xrp": largest["balance_xrp"] if largest else 0.0,
        "scope_note": (
            "Currently tracking the Ripple monthly-release escrows declared in "
            "ripple.com/.well-known/xrp-ledger.toml. Exchange cold wallets and "
            "foundation reserves coming later."
        ),
    }


def fetch_cold_storage_cached(ttl=None):
    """Cache successful fetches for `ttl` seconds. Never cache an
    all-errored result — a transient network hiccup at startup would
    otherwise pin a useless "0 XRP locked" view for the full TTL window
    while the next request silently retries."""
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < ttl:
            data = dict(_cache["data"])
            data["cached_age_seconds"] = round(now - _cache["fetched_at"], 1)
            return data
        fresh = fetch_cold_storage()
        if fresh.get("fetched_ok", 0) > 0:
            _cache["fetched_at"] = now
            _cache["data"] = fresh
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result


if __name__ == "__main__":
    t0 = time.time()
    d = fetch_cold_storage()
    print(f"fetched in {time.time()-t0:.1f}s")
    print(f"tracked: {d['tracked_count']} (ok: {d['fetched_ok']})")
    print(f"total locked: {d['total_xrp']:,.0f} XRP")
    print(f"largest: {d['largest_name']} = {d['largest_xrp']:,.0f} XRP")
    for r in d["rows"][:5]:
        bal = f"{r['balance_xrp']:,.0f} XRP" if r["balance_xrp"] is not None else "ERR"
        print(f"  {r['name']:30s} {bal:>22s}  seq={r.get('sequence', '?')}")
