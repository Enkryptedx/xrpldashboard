"""
XRPL Wallet Explainer

Plain-language breakdown of what an XRP-Ledger account holds, where it
lives, and roughly what it's doing. Designed to render as a panel on /v2
("paste an address, see what you own — in normal English").

What this module does NOT cover (deferred):
    - USD pricing for tokens (no oracle layer; everything XRP-denominated)
    - NFT holdings
    - Escrow inspection (visible via owner_count, but not itemized)
    - Open offers / DEX orderbook positions
    - Multi-chain (XRPL only — see WHALE_WATCH/IDEAS.md for the parked
      multi-chain "wallet explained" venture)
"""

from datetime import datetime, timezone
import json
import os
import threading
import time

from xrpl.clients import JsonRpcClient
from xrpl.core.addresscodec import is_valid_classic_address
from xrpl.models.requests import (
    AccountInfo,
    AccountLines,
    AccountTx,
    AMMInfo,
    ServerInfo,
)


XRPL_NODE = "https://s1.ripple.com:51234"

_HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_NAMES_PATH = os.path.join(_HERE, "token_names.json")
NAMED_ACCOUNTS_PATH = os.path.join(_HERE, "named_accounts.json")

# Per-wallet cache. Wallet lookups are cheaper than the AMM scan (3-ish
# RPC calls per address vs ~19 for the scanner), but pages refreshing
# every few seconds still hammer s1 if we don't cache. 60s is a fair
# trade between freshness and load.
WALLET_CACHE_TTL_SECONDS = int(os.environ.get("WALLET_CACHE_TTL_SECONDS", "60"))
_cache_lock = threading.Lock()
_cache_state = {}  # address -> {"data": dict, "fetched_at": float}

# Trustline dust threshold. Below this, the line is treated as inactive
# and skipped from the breakdown. Keeps the output focused on what the
# holder actually owns.
TRUSTLINE_DUST_THRESHOLD = 0.0001

# AMM LP tokens have currency codes that are 40-char hex strings starting
# with "03" — the XRPL marker for LP shares. (Per AMM amendment spec.)
LP_TOKEN_PREFIX = "03"


def _load_known_tokens():
    """Map (currency_hex, issuer) → display name for friendly token labels."""
    try:
        with open(TOKEN_NAMES_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        out[(entry["currency_hex"], entry["issuer"])] = entry["currency_display"]
    return out


def _load_named_accounts():
    """Map address → human label (e.g. 'Ripple Escrow #01')."""
    try:
        with open(NAMED_ACCOUNTS_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for addr, entry in data.items():
        if isinstance(entry, dict) and "name" in entry:
            out[addr] = entry["name"]
    return out


KNOWN_TOKENS = _load_known_tokens()
NAMED_ACCOUNTS = _load_named_accounts()


def _hex_to_currency(code):
    """XRPL currency codes are either 3-char ASCII or 40-char hex. Normalize."""
    if not code:
        return code
    if len(code) == 3:
        return code
    # 40-char hex: try to decode trailing zeros off, common pattern.
    try:
        raw = bytes.fromhex(code).rstrip(b"\x00")
        decoded = raw.decode("ascii")
        if decoded.isprintable() and len(decoded) >= 3:
            return decoded
    except (ValueError, UnicodeDecodeError):
        pass
    return code  # opaque hex — caller can show it as-is or truncate


def _drops_to_xrp(drops):
    return int(drops) / 1_000_000


def _fmt_xrp(amount):
    if amount >= 1_000_000:
        return f"{amount/1_000_000:,.2f}M XRP"
    if amount >= 1_000:
        return f"{amount:,.0f} XRP"
    return f"{amount:,.2f} XRP"


def _fmt_token(amount, name):
    if amount >= 1_000_000:
        return f"{amount/1_000_000:,.2f}M {name}"
    if amount >= 1_000:
        return f"{amount:,.0f} {name}"
    if amount >= 1:
        return f"{amount:,.2f} {name}"
    return f"{amount:.4f} {name}"


def _fetch_reserves(client):
    """Pull base + per-item reserve from server_info. ~5 XRP base, 0.2/item now."""
    try:
        info = client.request(ServerInfo()).result.get("info", {})
    except Exception:
        return None, None
    validated = info.get("validated_ledger") or {}
    return (
        validated.get("reserve_base_xrp"),
        validated.get("reserve_inc_xrp"),
    )


def _fetch_account_info(client, address):
    try:
        resp = client.request(AccountInfo(account=address, ledger_index="validated"))
    except Exception as e:
        return {"error": f"account_info failed: {e}"}
    result = resp.result
    if "error" in result:
        # Most common: actNotFound (unfunded address).
        return {"error": result.get("error_message") or result.get("error")}
    acct = result.get("account_data") or {}
    return {
        "balance_xrp": _drops_to_xrp(acct.get("Balance", "0")),
        "owner_count": int(acct.get("OwnerCount", 0)),
        "sequence": int(acct.get("Sequence", 0)),
    }


MAX_TRUSTLINE_PAGES = 3  # cap at 600 lines — exchange/AMM accounts can have thousands


def _fetch_trustlines(client, address):
    """All trustlines + classification (regular token vs AMM LP share)."""
    lines = []
    marker = None
    pages = 0
    while pages < MAX_TRUSTLINE_PAGES:
        try:
            req = AccountLines(account=address, limit=200, marker=marker)
            resp = client.request(req).result
        except Exception as e:
            return {"error": f"account_lines failed: {e}"}
        if "error" in resp:
            return {"error": resp.get("error_message") or resp.get("error")}
        lines.extend(resp.get("lines") or [])
        marker = resp.get("marker")
        pages += 1
        if not marker:
            break

    holdings = []
    lp_positions = []
    for ln in lines:
        balance = float(ln.get("balance", "0"))
        if abs(balance) < TRUSTLINE_DUST_THRESHOLD:
            continue
        currency_hex = ln.get("currency", "")
        issuer = ln.get("account", "")
        is_lp = (
            len(currency_hex) == 40
            and currency_hex.startswith(LP_TOKEN_PREFIX)
        )
        if is_lp:
            lp_positions.append({
                "amm_account": issuer,
                "lp_balance": balance,
                "currency_hex": currency_hex,
            })
        else:
            display = KNOWN_TOKENS.get((currency_hex, issuer)) or _hex_to_currency(currency_hex)
            holdings.append({
                "currency_display": display,
                "currency_hex": currency_hex,
                "issuer": issuer,
                "issuer_label": NAMED_ACCOUNTS.get(issuer),
                "balance": balance,
            })
    return {"holdings": holdings, "lp_positions": lp_positions}


def _fetch_last_activity(client, address):
    """Single most recent transaction. Cheap — limit=1, descending."""
    try:
        resp = client.request(AccountTx(account=address, limit=1)).result
    except Exception:
        return None
    txs = resp.get("transactions") or []
    if not txs:
        return None
    tx = txs[0]
    # close_time_iso is provided on modern rippled/clio responses.
    iso = tx.get("close_time_iso")
    if iso:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return None
    # Fallback: ripple-epoch close_time on the inner tx body.
    body = tx.get("tx") or tx.get("tx_json") or {}
    rt = body.get("date")
    if rt:
        return datetime.fromtimestamp(int(rt) + 946684800, tz=timezone.utc)
    return None


def _resolve_lp_position(client, lp, amm_lookup):
    """
    Given an LP trustline, name the pool and compute the holder's share.
    amm_lookup is a {amm_account: pair_name} map (best-effort) we pre-built
    from the curated pool list. Falls back to a live AMMInfo call.
    """
    pair = amm_lookup.get(lp["amm_account"])
    pool = None
    try:
        resp = client.request(AMMInfo(amm_account=lp["amm_account"])).result
        if "error" not in resp:
            pool = resp.get("amm") or {}
    except Exception:
        pool = None

    fee_pct = None
    pool_xrp = None
    pool_token_amount = None
    pool_token_display = None
    lp_total = None
    share_pct = None

    if pool:
        fee_pct = (pool.get("trading_fee", 0) or 0) / 1000.0
        amount = pool.get("amount")
        amount2 = pool.get("amount2") or {}

        # Identify which side is XRP (drops string) and which is the token (dict).
        for side in (amount, amount2):
            if isinstance(side, str):
                pool_xrp = _drops_to_xrp(side)
            elif isinstance(side, dict) and side.get("currency") not in (None, "XRP"):
                pool_token_amount = float(side.get("value", 0) or 0)
                pool_token_display = (
                    KNOWN_TOKENS.get((side.get("currency"), side.get("issuer")))
                    or _hex_to_currency(side.get("currency", ""))
                )
        lp_token = pool.get("lp_token") or {}
        try:
            lp_total = float(lp_token.get("value", 0))
        except (TypeError, ValueError):
            lp_total = None
        if lp_total and lp_total > 0:
            share_pct = (lp["lp_balance"] / lp_total) * 100.0

        if not pair:
            other = pool_token_display or "?"
            pair = f"XRP/{other}"

    return {
        "amm_account": lp["amm_account"],
        "pair": pair or "Unknown AMM pool",
        "lp_balance": lp["lp_balance"],
        "lp_total": lp_total,
        "share_pct": share_pct,
        "pool_xrp": pool_xrp,
        "pool_token_amount": pool_token_amount,
        "pool_token_display": pool_token_display,
        "fee_pct": fee_pct,
        "user_xrp_in_pool": (
            (share_pct / 100.0) * pool_xrp if (share_pct and pool_xrp) else None
        ),
        "user_token_in_pool": (
            (share_pct / 100.0) * pool_token_amount
            if (share_pct and pool_token_amount) else None
        ),
    }


def _build_amm_lookup():
    """
    Best-effort {amm_account: 'XRP/FOO'} map built from the curated pool list.
    Used to label LP positions without a per-position AMMInfo call. Falls
    back gracefully if the scanner module isn't importable.
    """
    try:
        from amm_scan_pools import scan_all_pools_cached
    except ImportError:
        return {}
    try:
        scan = scan_all_pools_cached()
    except Exception:
        return {}
    out = {}
    for p in scan.get("pools") or []:
        if p.get("amm_account") and p.get("pair"):
            out[p["amm_account"]] = p["pair"]
    return out


# -------- plain-language builders ----------------------------------------------

def _build_summary_line(total_xrp, has_amm, holdings_count):
    parts = [f"This wallet holds {_fmt_xrp(total_xrp)} on the XRP Ledger."]
    if has_amm:
        parts.append("Some of it is in AMM liquidity pools earning trading fees.")
    if holdings_count:
        parts.append(
            f"It also holds {holdings_count} other token"
            + ("s." if holdings_count != 1 else ".")
        )
    return " ".join(parts)


def _build_balance_text(available, reserved, base_reserve, owner_count, inc_reserve):
    out = []
    out.append(
        f"{_fmt_xrp(available)} is free to send or trade — this is the spendable balance."
    )
    if reserved > 0:
        bits = [f"{_fmt_xrp(reserved)} is locked by the network as a reserve."]
        if base_reserve and inc_reserve and owner_count is not None:
            bits.append(
                f"That's {base_reserve} XRP base + {owner_count} owned objects × "
                f"{inc_reserve} XRP each. The reserve keeps the account active and "
                f"can't be spent unless those objects (trustlines, offers, AMM "
                f"positions) are removed."
            )
        out.append(" ".join(bits))
    return out


def _build_amm_text(positions):
    if not positions:
        return []
    out = []
    if len(positions) == 1:
        out.append("This wallet holds 1 AMM liquidity position.")
    else:
        out.append(f"This wallet holds {len(positions)} AMM liquidity positions.")
    for p in positions:
        line = f"In the {p['pair']} pool"
        if p["share_pct"] is not None:
            line += f", it owns {p['share_pct']:.3f}% of the pool"
        if p["user_xrp_in_pool"] is not None:
            line += f" — about {_fmt_xrp(p['user_xrp_in_pool'])}"
            if p["user_token_in_pool"] is not None and p["pool_token_display"]:
                line += f" + {_fmt_token(p['user_token_in_pool'], p['pool_token_display'])}"
        line += "."
        if p["fee_pct"] is not None:
            line += (
                f" The pool charges a {p['fee_pct']:.3f}% trading fee on every swap; "
                f"that fee is paid back to liquidity providers like this wallet."
            )
        out.append(line)
    return out


def _build_holdings_text(holdings):
    if not holdings:
        return []
    out = []
    if len(holdings) == 1:
        out.append("This wallet also holds 1 issued token:")
    else:
        out.append(f"This wallet also holds {len(holdings)} issued tokens:")
    for h in holdings:
        line = f"  • {_fmt_token(h['balance'], h['currency_display'])}"
        if h["issuer_label"]:
            line += f" — issued by {h['issuer_label']}."
        else:
            line += f" — issued by {h['issuer'][:6]}…{h['issuer'][-4:]}."
        out.append(line)
    return out


def _build_activity_text(last_tx_at):
    if not last_tx_at:
        return []
    delta = datetime.now(timezone.utc) - last_tx_at
    days = delta.total_seconds() / 86400
    if days < 1:
        return ["Last on-chain activity was within the past day. This account is active."]
    if days < 7:
        return [f"Last on-chain activity was about {int(days)} day(s) ago."]
    if days < 60:
        return [f"Last on-chain activity was about {int(days)} days ago."]
    return [f"Last on-chain activity was over {int(days/30)} month(s) ago — this wallet is dormant."]


# -------- top-level orchestration ----------------------------------------------

def explain_wallet(address):
    """
    Single entry point. Returns a dict with structured data + plain-English
    text blocks, ready for the /v2 panel renderer.
    """
    timestamp = datetime.now(timezone.utc)

    if not address or not is_valid_classic_address(address):
        return {
            "error": "Not a valid XRPL classic address.",
            "address": address,
            "timestamp": timestamp,
        }

    client = JsonRpcClient(XRPL_NODE)

    info = _fetch_account_info(client, address)
    if "error" in info:
        return {
            "error": info["error"],
            "address": address,
            "timestamp": timestamp,
            "node": XRPL_NODE,
        }

    base_reserve, inc_reserve = _fetch_reserves(client)
    reserved_xrp = 0.0
    if base_reserve is not None and inc_reserve is not None:
        reserved_xrp = float(base_reserve) + (info["owner_count"] * float(inc_reserve))
    available_xrp = max(0.0, info["balance_xrp"] - reserved_xrp)

    lines_result = _fetch_trustlines(client, address)
    if "error" in lines_result:
        return {
            "error": lines_result["error"],
            "address": address,
            "timestamp": timestamp,
            "node": XRPL_NODE,
        }
    holdings = lines_result["holdings"]
    lp_positions_raw = lines_result["lp_positions"]

    amm_lookup = _build_amm_lookup() if lp_positions_raw else {}
    amm_positions = [
        _resolve_lp_position(client, lp, amm_lookup) for lp in lp_positions_raw
    ]

    last_tx_at = _fetch_last_activity(client, address)

    name_label = NAMED_ACCOUNTS.get(address)

    summary = _build_summary_line(
        info["balance_xrp"], bool(amm_positions), len(holdings)
    )
    balance_text = _build_balance_text(
        available_xrp, reserved_xrp, base_reserve, info["owner_count"], inc_reserve
    )
    amm_text = _build_amm_text(amm_positions)
    holdings_text = _build_holdings_text(holdings)
    activity_text = _build_activity_text(last_tx_at)

    return {
        "error": None,
        "address": address,
        "address_label": name_label,
        "timestamp": timestamp,
        "node": XRPL_NODE,
        "balance_xrp": info["balance_xrp"],
        "available_xrp": available_xrp,
        "reserved_xrp": reserved_xrp,
        "owner_count": info["owner_count"],
        "base_reserve_xrp": base_reserve,
        "inc_reserve_xrp": inc_reserve,
        "amm_positions": amm_positions,
        "holdings": holdings,
        "last_tx_at": last_tx_at,
        "summary_text": summary,
        "balance_text": balance_text,
        "amm_text": amm_text,
        "holdings_text": holdings_text,
        "activity_text": activity_text,
    }


def explain_wallet_cached(address, ttl_seconds=None):
    """Per-address cache wrapper. Same shape as explain_wallet + cached_age_seconds."""
    ttl = ttl_seconds if ttl_seconds is not None else WALLET_CACHE_TTL_SECONDS
    now = time.time()

    with _cache_lock:
        entry = _cache_state.get(address)
        if entry:
            age = now - entry["fetched_at"]
            if age < ttl:
                result = dict(entry["data"])
                result["cached_age_seconds"] = round(age, 1)
                return result

        fresh = explain_wallet(address)
        if not fresh.get("error"):
            _cache_state[address] = {"data": fresh, "fetched_at": now}
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result


def main():
    """CLI: python wallet_explainer.py <r-address>"""
    import sys
    if len(sys.argv) < 2:
        print("Usage: python wallet_explainer.py <r-address>")
        return 1
    address = sys.argv[1]
    data = explain_wallet(address)
    if data.get("error"):
        print(f"ERROR: {data['error']}")
        return 1

    print("=" * 72)
    print("XRPL WALLET EXPLAINED")
    print("=" * 72)
    print(f"Address:  {data['address']}")
    if data["address_label"]:
        print(f"Known as: {data['address_label']}")
    print(f"Node:     {data['node']}")
    print(f"Time:     {data['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()
    print(data["summary_text"])
    print()
    for line in data["balance_text"]:
        print(line)
        print()
    for line in data["amm_text"]:
        print(line)
        print()
    for line in data["holdings_text"]:
        print(line)
    if data["holdings_text"]:
        print()
    for line in data["activity_text"]:
        print(line)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
