"""Live RLUSD treasury feed — Ethereum + XRP Ledger.

Both chains are queried directly from public nodes — no third-party
gateway, no API key, no secret to leak. This matches the "self-sourced"
framing the rest of the dashboard already uses for XRP pricing and
ledger state.

Ethereum side: public JSON-RPC (defaults to https://1rpc.io/eth).
  * totalSupply()    via eth_call            → circulating supply
  * Transfer events  via eth_getLogs         → recent mints/burns
    A Transfer with the zero address as `from` is a mint; with the
    zero address as `to` is a burn.

XRPL side: Ripple's public JSON-RPC (s1.ripple.com).
  * gateway_balances  → issuer obligations (= circulating supply)
  * account_tx        → recent Payment txs to/from the issuer
    Issuer as sender = mint (issuance out). Issuer as destination =
    burn (redemption back in).

The two are merged behind a single TTL-cached entry point so the API
route stays cheap regardless of how often clients poll.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
from typing import Any
from urllib import request as urlrequest
from urllib.error import URLError

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountTx, GatewayBalances

# macOS Python installs ship without a populated system cert store, so
# urllib's default SSLContext can't verify HTTPS. certifi ships with the
# Mozilla CA bundle and is already a transitive dependency via xrpl-py →
# httpx → certifi, so we can rely on it without adding a new requirement.
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    _SSL_CONTEXT = ssl.create_default_context()


# --- Constants ---------------------------------------------------------------

ETH_CONTRACT = "0x8292bb45bf1ee4d140127049757c2e0ff06317ed"
ETH_DECIMALS = 18
# `totalSupply()` selector — first 4 bytes of keccak256("totalSupply()").
ETH_TOTAL_SUPPLY_SELECTOR = "0x18160ddd"
# ERC-20 Transfer(address,address,uint256) event topic.
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
ZERO_TOPIC = "0x" + "0" * 64

XRPL_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
XRPL_CURRENCY_HEX = "524C555344000000000000000000000000000000"
# XRPL serializes a 3-char ticker like "USD" literally, but RLUSD is 5
# chars so its on-wire form is the 40-char hex above. Some serializers
# still echo "RLUSD" back as a convenience, so we accept either.
XRPL_CURRENCY_NAMES = {"RLUSD", XRPL_CURRENCY_HEX}

# XRPL timestamps are seconds since 2000-01-01 UTC. Add this to align
# with the standard Unix epoch.
XRPL_EPOCH_OFFSET = 946_684_800

# Ethereum log lookback. Public RPCs cap eth_getLogs windows (Cloudflare
# allows ~1024 blocks); ~1000 blocks ≈ 3.3 hours at 12s/block.
ETH_LOOKBACK_BLOCKS = 1000
ETH_MAX_EVENTS = 80
# Approximate average block time, used to back-fill timestamps from
# block numbers (logs don't carry timestamps over public RPC, and we
# don't want to fan out N+1 eth_getBlockByNumber calls).
ETH_SECONDS_PER_BLOCK = 12

XRPL_TX_LIMIT = 80
XRPL_MAX_EVENTS = 80

CACHE_TTL = int(os.environ.get("RLUSD_CACHE_TTL_SECONDS", "60"))
ETH_RPC = os.environ.get("ETH_RPC", "https://1rpc.io/eth")
XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")

HTTP_TIMEOUT = 9.0
USER_AGENT = "xrpldashboard/1.0 (+https://xrpldashboard.com)"


# --- Cache -------------------------------------------------------------------

_lock = threading.Lock()
_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}


# --- Ethereum (public JSON-RPC) ---------------------------------------------

def _eth_rpc(method: str, params: list) -> Any:
    """POST a JSON-RPC request to the public Ethereum node. Returns the
    `result` field; raises RuntimeError if the node returns a JSON-RPC
    error envelope. Uses stdlib urllib to avoid an extra dependency."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode("utf-8")
    req = urlrequest.Request(
        ETH_RPC,
        data=payload,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CONTEXT) as resp:
            body = resp.read()
    except URLError as e:
        raise RuntimeError(f"eth_rpc {method}: {type(e).__name__}: {e}")
    data = json.loads(body.decode("utf-8"))
    if "error" in data:
        err = data["error"]
        msg = err.get("message", err) if isinstance(err, dict) else err
        raise RuntimeError(f"eth_rpc {method}: {msg}")
    return data.get("result")


def _decode_eth_amount(data_hex: str) -> float:
    if not data_hex:
        return 0.0
    raw = int(data_hex, 16)
    return raw / (10 ** ETH_DECIMALS)


def fetch_eth() -> dict:
    """Returns {supply: float|None, events: [...], error: str|None}."""
    out: dict[str, Any] = {"supply": None, "events": [], "error": None}

    # Total supply via eth_call → totalSupply().
    try:
        result = _eth_rpc("eth_call", [
            {"to": ETH_CONTRACT, "data": ETH_TOTAL_SUPPLY_SELECTOR},
            "latest",
        ])
        if isinstance(result, str) and result.startswith("0x"):
            raw = int(result, 16)
            out["supply"] = raw / (10 ** ETH_DECIMALS)
    except Exception as e:
        out["error"] = f"eth_supply: {type(e).__name__}: {e}"

    # Recent Transfer events (filtered to mint/burn by zero-address topic).
    try:
        latest_hex = _eth_rpc("eth_blockNumber", [])
        latest = int(latest_hex, 16)
        from_block = max(0, latest - ETH_LOOKBACK_BLOCKS)
        now_unix = int(time.time())

        logs = _eth_rpc("eth_getLogs", [{
            "address": ETH_CONTRACT,
            "topics": [TRANSFER_TOPIC],
            "fromBlock": hex(from_block),
            "toBlock": "latest",
        }])

        if isinstance(logs, list):
            for log in logs[-ETH_MAX_EVENTS:]:
                topics = log.get("topics", [])
                if len(topics) < 3:
                    continue
                from_t = topics[1].lower()
                to_t = topics[2].lower()
                amount = _decode_eth_amount(log.get("data", "0x0"))
                tx_hash = log.get("transactionHash", "")
                blk_hex = log.get("blockNumber", "0x0")
                blk = int(blk_hex, 16) if isinstance(blk_hex, str) else int(blk_hex)
                # Approximate timestamp from latest block + average block time.
                ts = now_unix - max(0, latest - blk) * ETH_SECONDS_PER_BLOCK
                if from_t == ZERO_TOPIC:
                    out["events"].append({
                        "type": "mint", "chain": "eth",
                        "amount": amount, "tx": tx_hash, "ts": ts,
                    })
                elif to_t == ZERO_TOPIC:
                    out["events"].append({
                        "type": "burn", "chain": "eth",
                        "amount": amount, "tx": tx_hash, "ts": ts,
                    })
    except Exception as e:
        prev = out["error"]
        out["error"] = (
            f"{prev} | eth_events: {type(e).__name__}: {e}"
            if prev else f"eth_events: {type(e).__name__}: {e}"
        )

    return out


# --- XRPL --------------------------------------------------------------------

def _xrpl_currency_match(cur: str | None) -> bool:
    return cur in XRPL_CURRENCY_NAMES if cur else False


def fetch_xrpl() -> dict:
    """Returns {supply: float|None, events: [...], error: str|None}."""
    out: dict[str, Any] = {"supply": None, "events": [], "error": None}
    client = JsonRpcClient(XRPL_NODE)

    # gateway_balances → total RLUSD obligations (= circulating supply).
    try:
        resp = client.request(GatewayBalances(
            account=XRPL_ISSUER, ledger_index="validated"
        ))
        obligations = (resp.result or {}).get("obligations", {})
        for cur, amt in obligations.items():
            if _xrpl_currency_match(cur):
                try:
                    out["supply"] = float(amt)
                except (TypeError, ValueError):
                    pass
                break
    except Exception as e:
        out["error"] = f"xrpl_supply: {type(e).__name__}: {e}"

    # account_tx on the issuer → recent mint/burn payments.
    try:
        resp = client.request(AccountTx(
            account=XRPL_ISSUER,
            ledger_index_min=-1,
            ledger_index_max=-1,
            limit=XRPL_TX_LIMIT,
            binary=False,
        ))
        for entry in (resp.result or {}).get("transactions", []):
            tx = entry.get("tx") or entry.get("tx_json") or {}
            meta = entry.get("meta") or {}
            if tx.get("TransactionType") != "Payment":
                continue
            delivered = meta.get("delivered_amount")
            if not isinstance(delivered, dict):
                src = tx.get("Amount")
                delivered = src if isinstance(src, dict) else None
            if not isinstance(delivered, dict):
                continue
            if not _xrpl_currency_match(delivered.get("currency")):
                continue
            if delivered.get("issuer") != XRPL_ISSUER:
                continue
            try:
                amount = float(delivered.get("value", "0"))
            except (TypeError, ValueError):
                continue
            sender = tx.get("Account", "")
            destination = tx.get("Destination", "")
            if sender == XRPL_ISSUER and destination != XRPL_ISSUER:
                ev_type = "mint"
            elif destination == XRPL_ISSUER and sender != XRPL_ISSUER:
                ev_type = "burn"
            else:
                continue
            ts = int(tx.get("date", 0)) + XRPL_EPOCH_OFFSET
            out["events"].append({
                "type": ev_type, "chain": "xrpl",
                "amount": amount,
                "tx": tx.get("hash", ""),
                "ts": ts,
            })
            if len(out["events"]) >= XRPL_MAX_EVENTS:
                break
    except Exception as e:
        prev = out["error"]
        out["error"] = (
            f"{prev} | xrpl_events: {type(e).__name__}: {e}"
            if prev else f"xrpl_events: {type(e).__name__}: {e}"
        )

    return out


# --- Combined cached entry point --------------------------------------------

def fetch_state(force: bool = False) -> dict:
    """TTL-cached combined Ethereum + XRPL state.

    Returns the cached payload if it's still fresh. Otherwise refreshes
    both chains and replaces the cache. Force=True bypasses the freshness
    check (useful for smoke tests)."""
    now = time.time()
    with _lock:
        cached = _cache["data"]
        age = now - _cache["fetched_at"]
        fresh_enough = cached is not None and age < CACHE_TTL
        if fresh_enough and not force:
            return cached

    eth = fetch_eth()
    xrpl = fetch_xrpl()

    # Combine + sort events newest-first across both chains.
    events = sorted(
        list(eth.get("events", [])) + list(xrpl.get("events", [])),
        key=lambda e: e.get("ts", 0),
        reverse=True,
    )

    data = {
        "eth": {"supply": eth["supply"], "error": eth["error"]},
        "xrpl": {"supply": xrpl["supply"], "error": xrpl["error"]},
        "events": events[:120],
        "fetched_at": int(now),
        "ttl_seconds": CACHE_TTL,
        "sources": {
            "eth": {
                "contract": ETH_CONTRACT,
                "explorer": f"https://etherscan.io/token/{ETH_CONTRACT}",
                "via": ETH_RPC,
            },
            "xrpl": {
                "issuer": XRPL_ISSUER,
                "explorer": f"https://xrpscan.com/account/{XRPL_ISSUER}",
                "via": XRPL_NODE,
            },
        },
    }
    with _lock:
        _cache["data"] = data
        _cache["fetched_at"] = now
    return data
