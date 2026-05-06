"""
XRPL AMM Multi-Pool Scanner
Queries multiple real XRPL AMM pools and displays them in a ranked table.
"""

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AMMInfo, LedgerCurrent
from datetime import datetime, timezone
import json
import os
import sys
import threading
import time


XRPL_NODE = "https://s1.ripple.com:51234"
XRP_USD_PRICE = 1.44

# Curated XRP/token AMM pool list now lives in token_names.json so it can
# be edited (and contributed to via PR) without touching code. See
# TOKEN_NAMES.md for the contribution flow.
TOKEN_NAMES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "token_names.json"
)

# Categories whose tokens we treat as USD-pegged for TVL math. Anything
# outside this set returns usd_peg=None and TVL is shown as "estimated"
# in the UI.
USD_PEGGED_CATEGORIES = {"stablecoin"}

# Server-side cache for scan results. Without this, every visitor triggers a
# fresh round of N XRPL queries — at scale that's expensive (hosting bills,
# rate limits, slow page loads). Cache TTL is the trade-off between data
# freshness and load: at 30s the ledger advances ~8 ledgers, balances
# barely move, and the page feels live. Tunable via env var.
SCAN_CACHE_TTL_SECONDS = int(os.environ.get("SCAN_CACHE_TTL_SECONDS", "30"))
_cache_lock = threading.Lock()
_cache_state = {"data": None, "fetched_at": 0.0}

def _load_known_tokens():
    try:
        with open(TOKEN_NAMES_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: could not load {TOKEN_NAMES_PATH}: {e}", file=sys.stderr)
        return []
    tokens = []
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        category = entry.get("category")
        usd_peg = 1.00 if category in USD_PEGGED_CATEGORIES else None
        tokens.append({
            "name": entry["currency_display"],
            "currency": entry["currency_hex"],
            "issuer": entry["issuer"],
            "usd_peg": usd_peg,
            "category": category,
        })
    return tokens


KNOWN_TOKENS = _load_known_tokens()


def drops_to_xrp(drops):
    return int(drops) / 1_000_000


def format_trading_fee(raw_fee):
    return raw_fee / 1000.0


def fmt_money(n):
    if n >= 1_000_000:
        return f"${n/1_000_000:,.2f}M"
    elif n >= 1_000:
        return f"${n/1_000:,.1f}K"
    else:
        return f"${n:,.2f}"


def fmt_num(n):
    return f"{n:,.2f}"


def right(text, width):
    return str(text).rjust(width)


def left(text, width):
    text = str(text)
    if len(text) > width:
        text = text[:width - 1] + "..."
    return text.ljust(width)


def fetch_pool(client, token_info):
    try:
        request = AMMInfo(
            asset={"currency": "XRP"},
            asset2={
                "currency": token_info["currency"],
                "issuer": token_info["issuer"],
            },
        )
        response = client.request(request)
    except Exception as e:
        return {"error": f"request failed: {e}"}

    if "error" in response.result:
        err = response.result.get("error", "unknown")
        return {"error": err}

    amm = response.result.get("amm")
    if not amm:
        return {"error": "no amm in response"}

    xrp_raw = amm.get("amount")
    if isinstance(xrp_raw, str):
        xrp_amount = drops_to_xrp(xrp_raw)
    elif isinstance(xrp_raw, dict):
        xrp_amount = float(xrp_raw.get("value", 0))
    else:
        return {"error": "unexpected amount format"}

    token_info_response = amm.get("amount2", {})
    token_amount = float(token_info_response.get("value", 0))

    fee_raw = amm.get("trading_fee", 0)
    fee_pct = format_trading_fee(fee_raw)

    xrp_tvl_usd = xrp_amount * XRP_USD_PRICE
    token_tvl_usd = token_amount * token_info["usd_peg"] if token_info["usd_peg"] else None

    if token_tvl_usd is not None:
        total_tvl_usd = xrp_tvl_usd + token_tvl_usd
    else:
        total_tvl_usd = xrp_tvl_usd * 2
        token_tvl_usd = xrp_tvl_usd

    implied_price = token_amount / xrp_amount if xrp_amount > 0 else 0

    return {
        "pair": f"XRP/{token_info['name']}",
        "amm_account": amm.get("account"),
        "xrp_amount": xrp_amount,
        "token_amount": token_amount,
        "fee_pct": fee_pct,
        "fee_raw": fee_raw,
        "xrp_tvl_usd": xrp_tvl_usd,
        "token_tvl_usd": token_tvl_usd,
        "total_tvl_usd": total_tvl_usd,
        "implied_price": implied_price,
        "usd_peg_known": token_info["usd_peg"] is not None,
    }


def scan_all_pools():
    """Run the multi-pool scan and return structured results. No printing."""
    timestamp = datetime.now(timezone.utc)
    client = JsonRpcClient(XRPL_NODE)

    try:
        ledger = client.request(LedgerCurrent()).result["ledger_current_index"]
    except Exception as e:
        return {
            "error": f"Ledger connection failed: {e}",
            "timestamp": timestamp,
            "ledger": None,
            "node": XRPL_NODE,
            "pools": [],
            "missing": [],
            "total_tvl_usd": 0.0,
        }

    results = []
    missing = []
    for token in KNOWN_TOKENS:
        pool = fetch_pool(client, token)
        if pool and "error" not in pool:
            results.append(pool)
        else:
            err = pool.get("error", "unknown") if pool else "no response"
            missing.append({"pair": f"XRP/{token['name']}", "error": err})

    results.sort(key=lambda p: p["total_tvl_usd"], reverse=True)
    total_tvl_all = sum(p["total_tvl_usd"] for p in results)

    return {
        "error": None,
        "timestamp": timestamp,
        "ledger": ledger,
        "node": XRPL_NODE,
        "pools": results,
        "missing": missing,
        "total_tvl_usd": total_tvl_all,
    }


def scan_all_pools_cached(ttl_seconds=None):
    """
    Server-side cached wrapper around scan_all_pools().

    Multiple concurrent requests share one scan result for `ttl_seconds`.
    First request after expiry triggers a fresh scan; everyone else gets
    the cached result. Thread-safe (gunicorn runs multi-worker / multi-thread).

    Returns the same dict shape as scan_all_pools(), with one extra key:
    "cached_age_seconds" — how stale the data is (0 if just refreshed).
    """
    ttl = ttl_seconds if ttl_seconds is not None else SCAN_CACHE_TTL_SECONDS
    now = time.time()

    with _cache_lock:
        cached = _cache_state["data"]
        fetched_at = _cache_state["fetched_at"]
        age = now - fetched_at

        if cached is not None and age < ttl:
            # Return cached result with age annotation.
            result = dict(cached)
            result["cached_age_seconds"] = round(age, 1)
            return result

        # Cache miss or expired — refresh.
        fresh = scan_all_pools()
        _cache_state["data"] = fresh
        _cache_state["fetched_at"] = now
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result


def main():
    print("=" * 92)
    print("XRPL AMM MULTI-POOL SCANNER")
    print("=" * 92)
    print(f"Node:   {XRPL_NODE}")
    print(f"Time:   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Pools:  {len(KNOWN_TOKENS)} curated pool pairs")
    print()

    data = scan_all_pools()

    if data["error"]:
        print(data["error"])
        return 1

    print(f"Ledger: {data['ledger']:,}")
    print()

    results = data["pools"]
    missing = data["missing"]

    print("-" * 92)
    print(
        left("POOL", 20)
        + right("XRP RESERVES", 18)
        + right("TOKEN RESERVES", 20)
        + right("FEE", 8)
        + right("TVL (USD)", 14)
        + right("PRICE", 12)
    )
    print("-" * 92)

    for p in results:
        token_display = fmt_num(p["token_amount"])
        tvl_display = fmt_money(p["total_tvl_usd"])
        if not p["usd_peg_known"]:
            tvl_display += "*"
        price_display = f"{p['implied_price']:,.4f}"
        print(
            left(p["pair"], 20)
            + right(fmt_num(p["xrp_amount"]), 18)
            + right(token_display, 20)
            + right(f"{p['fee_pct']:.3f}%", 8)
            + right(tvl_display, 14)
            + right(price_display, 12)
        )

    print("-" * 92)
    print(f"  Found {len(results)} active pool(s) | Total TVL (approx): {fmt_money(data['total_tvl_usd'])}")
    if any(not p["usd_peg_known"] for p in results):
        print("  * TVL estimated (non-stable token)")
    print()

    if missing:
        print("Pools not found on ledger:")
        for m in missing:
            print(f"  MISSING {m['pair']}: {m['error']}")
        print()

    print("All numbers pulled live from XRP Ledger at time of run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
