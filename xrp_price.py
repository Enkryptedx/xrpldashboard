"""
XRP spot price — derived from the on-chain XRP/RLUSD AMM pool.

Price = RLUSD reserves / XRP reserves (implied by the AMM constant-product
formula at zero slippage). RLUSD is Ripple's USD stablecoin on XRPL mainnet,
backed 1:1 and audited monthly — the most reliable on-chain USD reference.

Source: xrplcluster.com (same node cluster used for whale streaming).
Cached 20s server-side so concurrent page loads share one RPC call.
"""

import os
import threading
import time

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AMMInfo

import xrpl_client
from sovereign_tunnel_client import SovereignFetcher


XRPL_NODE = os.environ.get("XRPL_RPC", "https://xrplcluster.com")

RLUSD_CURRENCY = "524C555344000000000000000000000000000000"
RLUSD_ISSUER   = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"

PRICE_CACHE_TTL = int(os.environ.get("PRICE_CACHE_TTL_SECONDS", "20"))

_lock  = threading.Lock()
_state = {"data": None, "fetched_at": 0.0}


def fetch_xrp_price() -> dict:
    """Live fetch. Returns dict with price, reserves, or error key.

    2026-09-06: tunnel-first via SovereignFetcher. Previously hardcoded
    to `xrplcluster.com` (public). One RPC per fetch (amm_info); walker
    identity = xrp_price so any cascade attributes correctly in
    walker_node_fallback.
    """
    fetcher = SovereignFetcher(
        public_url=xrpl_client.PUBLIC_NODES[0],
        walker_name="xrp_price",
    )
    result = fetcher.call("amm_info", {
        "asset":  {"currency": "XRP"},
        "asset2": {"currency": RLUSD_CURRENCY, "issuer": RLUSD_ISSUER},
    })
    if result is None:
        return {"error": "amm_info failed (all endpoints)", "sourcing": fetcher.sourcing}

    if "error" in result:
        return {"error": result.get("error", "amm_info error"), "sourcing": fetcher.sourcing}

    amm = result.get("amm")
    if not amm:
        return {"error": "no amm in response", "sourcing": fetcher.sourcing}

    xrp_raw = amm.get("amount")
    rlusd_raw = amm.get("amount2", {})

    if not isinstance(xrp_raw, str):
        return {"error": "unexpected XRP amount format", "sourcing": fetcher.sourcing}

    xrp_amount   = int(xrp_raw) / 1_000_000
    rlusd_amount = float(rlusd_raw.get("value", 0))

    if xrp_amount <= 0:
        return {"error": "zero XRP reserves", "sourcing": fetcher.sourcing}

    price = rlusd_amount / xrp_amount

    return {
        "error": None,
        "price": round(price, 6),
        "xrp_reserves": round(xrp_amount, 2),
        "rlusd_reserves": round(rlusd_amount, 2),
        "source": "XRPL DEX (XRP/RLUSD AMM)",
        "amm_account": amm.get("account"),
        "sourcing": fetcher.sourcing,
    }


def fetch_xrp_price_cached() -> dict:
    """Thread-safe cached wrapper. Same shape as fetch_xrp_price() + cached_age_seconds."""
    now = time.time()
    with _lock:
        cached = _state["data"]
        age    = now - _state["fetched_at"]

        if cached is not None and age < PRICE_CACHE_TTL:
            result = dict(cached)
            result["cached_age_seconds"] = round(age, 1)
            return result

        fresh = fetch_xrp_price()
        if not fresh.get("error"):
            _state["data"]       = fresh
            _state["fetched_at"] = now

        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result
