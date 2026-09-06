"""
Live XRP total supply from the validated ledger's `total_coins` field.

Drifts down as fees burn — the honest number refutes the round 100B
folk-figure. The homepage supply-distribution box derives escrow / AMM /
circulating shares from this value, so a real total flows through every
percentage on the page.

Hourly cache — `total_coins` moves by a few XRP per day; polling more
often is waste. Falls back to last-good cached value if a fresh ledger
request fails; falls back to XRP_DESIGN_SUPPLY_FALLBACK (100e9) with a
logged warning ONLY when there is no cache yet AND the fresh fetch
fails, so first-boot survives an s1/s2 outage without crashing.

Reads route through xrpl_client.get_client() (local rippled primary,
s1/s2 fallback). Thread-safe.
"""

import logging
import os
import threading
import time

from xrpl.models.requests import Ledger

import xrpl_client
from xrpl_client import get_client
from sovereign_tunnel_client import SovereignFetcher

log = logging.getLogger(__name__)

CACHE_TTL = int(os.environ.get("TOTAL_SUPPLY_CACHE_TTL", "3600"))
XRP_DESIGN_SUPPLY_FALLBACK = 100_000_000_000.0

_cache_lock = threading.Lock()
_cache = {"fetched_at": 0.0, "data": None}


def fetch_total_supply():
    """Tunnel-first ledger request.

    walker_name="total_supply" (2026-09-03) — every fallback carries
    a real caller identity so per-page attribution stays clean.
    2026-09-06: switched from xrpl_client.get_client() (local rippled
    primary via localhost:5005, which is unreachable from Render and
    was producing ~52 walker_node_fallback rows/day) to
    SovereignFetcher (tunnel primary via rpc.xrpldashboard.com with
    CF-Access headers; s1/s2 fallback on real cascade). Fail-open if
    tunnel env vars unset.
    """
    fetcher = SovereignFetcher(
        public_url=xrpl_client.PUBLIC_NODES[0],
        walker_name="total_supply",
    )
    result = fetcher.call("ledger", {"ledger_index": "validated"})
    if result is None:
        log.warning("total_supply: ledger request failed (all endpoints)")
        return None
    if "error" in result:
        log.warning("total_supply: ledger error: %s", result.get("error"))
        return None
    ledger = result.get("ledger") or {}
    tc = ledger.get("total_coins")
    if tc is None:
        log.warning("total_supply: ledger response missing total_coins")
        return None
    try:
        drops = int(tc)
    except (TypeError, ValueError):
        log.warning("total_supply: total_coins not integer-parseable: %r", tc)
        return None
    return {
        "total_xrp": drops / 1_000_000.0,
        "total_drops": drops,
        "ledger_index": ledger.get("ledger_index"),
        "sourcing": fetcher.sourcing,
    }


def fetch_total_supply_cached(ttl=None):
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < ttl:
            data = dict(_cache["data"])
            data["cached_age_seconds"] = round(now - _cache["fetched_at"], 1)
            data["is_fallback"] = False
            return data
        fresh = fetch_total_supply()
        if fresh is not None:
            _cache["fetched_at"] = now
            _cache["data"] = fresh
            result = dict(fresh)
            result["cached_age_seconds"] = 0.0
            result["is_fallback"] = False
            return result
        if _cache["data"] is not None:
            data = dict(_cache["data"])
            data["cached_age_seconds"] = round(now - _cache["fetched_at"], 1)
            data["is_fallback"] = False
            return data
        log.warning(
            "total_supply: no cache and fresh fetch failed; "
            "falling back to design supply %.0f XRP",
            XRP_DESIGN_SUPPLY_FALLBACK,
        )
        return {
            "total_xrp": XRP_DESIGN_SUPPLY_FALLBACK,
            "total_drops": int(XRP_DESIGN_SUPPLY_FALLBACK * 1_000_000),
            "ledger_index": None,
            "cached_age_seconds": None,
            "is_fallback": True,
        }


if __name__ == "__main__":
    t0 = time.time()
    d = fetch_total_supply_cached()
    print(f"fetched in {time.time()-t0:.2f}s")
    print(f"total_xrp    : {d['total_xrp']:,.2f}")
    print(f"total_drops  : {d['total_drops']:,}")
    print(f"ledger_index : {d.get('ledger_index')}")
    print(f"is_fallback  : {d.get('is_fallback')}")
    print(f"cache age    : {d.get('cached_age_seconds')}")
