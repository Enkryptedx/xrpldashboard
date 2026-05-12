"""
XLS-66 Lending Protocol + SingleAssetVault amendment status tracker.

Pulls the public `feature` RPC and surfaces the two amendments that gate
the native lending stack:

  - LendingProtocol  (the broker/loan layer)
  - SingleAssetVault (the deposit primitive XLS-66 lending sits on)

The public RPC returns enabled/supported but NOT per-validator vote
tallies — those are admin-only. So this module emits an honest status
("in voting" / "activated") and we link out to xrplf.org's amendment
tracker for the per-validator detail.

Activation is the trigger to switch the /lending page from "awaiting"
mode to live LoanBroker/Vault data mode. Cached for 5 min — amendment
status changes on the order of weeks, not seconds.
"""

import os
import threading
import time

import httpx

XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
CACHE_TTL = int(os.environ.get("LENDING_CACHE_TTL", "300"))

# The two amendments that gate XLS-66 native lending. Both must enable
# before the /lending page can switch to live data.
LENDING_AMENDMENT_HASH = "565B90CA1AB2B9D42208ED10884188C64F9E19083DECB9634AAF06EB03299509"
VAULT_AMENDMENT_HASH = "81BD2619B6B3C8625AC5D0BC01DE17F06C3F0AB95C7C87C93715B87A4FD240D8"

_cache_lock = threading.Lock()
_cache = {"fetched_at": 0.0, "data": None}


def _fetch_features():
    try:
        resp = httpx.post(
            XRPL_NODE,
            json={"method": "feature", "params": [{}]},
            timeout=15.0,
        )
        return (resp.json() or {}).get("result") or {}
    except Exception:
        return None


def fetch_lending_status():
    result = _fetch_features()
    if not result:
        return {
            "ok": False,
            "lending": None,
            "vault": None,
            "ledger_index": None,
            "ready": False,
            "activated": False,
        }
    feats = result.get("features") or {}
    lending = feats.get(LENDING_AMENDMENT_HASH) or {}
    vault = feats.get(VAULT_AMENDMENT_HASH) or {}

    def shape(info, h):
        if not info:
            return {
                "hash": h,
                "name": None,
                "supported": False,
                "enabled": False,
                "status": "unknown",
            }
        enabled = bool(info.get("enabled"))
        supported = bool(info.get("supported"))
        if enabled:
            status = "activated"
        elif supported:
            status = "voting"
        else:
            status = "not_supported"
        return {
            "hash": h,
            "name": info.get("name"),
            "supported": supported,
            "enabled": enabled,
            "status": status,
        }

    lending_s = shape(lending, LENDING_AMENDMENT_HASH)
    vault_s = shape(vault, VAULT_AMENDMENT_HASH)
    activated = lending_s["enabled"] and vault_s["enabled"]

    return {
        "ok": True,
        "lending": lending_s,
        "vault": vault_s,
        "ledger_index": result.get("ledger_index"),
        "ready": True,
        "activated": activated,
    }


def fetch_lending_status_cached(ttl=None):
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < ttl:
            data = dict(_cache["data"])
            data["cached_age_seconds"] = round(now - _cache["fetched_at"], 1)
            return data
        fresh = fetch_lending_status()
        if fresh.get("ok"):
            _cache["fetched_at"] = now
            _cache["data"] = fresh
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result


if __name__ == "__main__":
    d = fetch_lending_status()
    print(f"ok: {d['ok']}, activated: {d['activated']}")
    for k in ("lending", "vault"):
        s = d.get(k) or {}
        print(f"  {k}: name={s.get('name')} status={s.get('status')} hash={s.get('hash','')[:16]}...")
