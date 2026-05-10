"""
Wallet detail data layer — feeds /wallet/<address>.

Pulls live data from the XRP Ledger via JSON-RPC:
  - account_info     → XRP balance, owner-object count, reserve math
  - account_lines    → trustline (held-token) count
  - account_tx       → last ~1000 transactions, paginated

Derives:
  - 30-day daily transaction counts ("pulse" trace)
  - top counterparties by tx count, classified as exchange / amm / peer / issuer
  - layout angles + label positions so the SVG graph in wallet.html can
    be driven entirely from the server-rendered JSON

Address classification uses three sources, in priority order:
  1. named_accounts.json (Ripple escrows + manually-curated entities)
  2. amm_index.json      (every indexed AMM pool account)
  3. fallback            ("peer", with shortened address as label)

Cached per address with a short TTL so repeat hits don't hammer the
public node. Thread-safe — gunicorn runs multi-worker / multi-thread.
"""

import json
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountInfo, AccountLines, AccountTx, AMMInfo, ServerInfo

XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")

HERE = os.path.dirname(os.path.abspath(__file__))
NAMED_ACCOUNTS_PATH = os.path.join(HERE, "named_accounts.json")
AMM_INDEX_PATH = os.path.join(HERE, "amm_index.json")
TOKEN_NAMES_PATH = os.path.join(HERE, "token_names.json")

CACHE_TTL = int(os.environ.get("WALLET_CACHE_TTL", "300"))
MAX_TX_PAGES = 5
TX_PAGE_LIMIT = 200
LOOKBACK_DAYS = 30
TOP_N_COUNTERPARTIES = 8

# Ripple Epoch: 2000-01-01 00:00 UTC. Ledger close times are seconds since
# this epoch, not Unix epoch. Convert with: unix_ts = ledger_time + 946684800.
RIPPLE_EPOCH = 946684800

# XRPL reserve math. Live values come from server_info (validated_ledger);
# these constants are only used as a last-resort fallback if the lookup
# fails. Reserves can change via on-chain amendment, so we never want to
# rely on the hardcoded numbers in steady state.
BASE_RESERVE_XRP = 10.0
OWNER_RESERVE_XRP = 0.2

# Server-info reserve cache: keyed only on the node URL. Reserves only
# change with on-chain amendments (rare), so a 10-minute TTL is safe and
# avoids a server_info round-trip on every wallet render.
_RESERVE_TTL_SECONDS = 600
_reserve_cache_lock = threading.Lock()
_reserve_cache = {}  # node_url -> (fetched_at_unix, (base_xrp, owner_xrp))


def _fetch_server_reserves(client):
    """Pull (base_reserve_xrp, owner_reserve_xrp) from validated server_info.
    Falls back to module constants if the request fails so a transient
    node error never breaks the wallet view."""
    now = time.time()
    with _reserve_cache_lock:
        cached = _reserve_cache.get(XRPL_NODE)
        if cached and now - cached[0] < _RESERVE_TTL_SECONDS:
            return cached[1]
    try:
        info = (client.request(ServerInfo()).result or {}).get("info") or {}
        validated = info.get("validated_ledger") or {}
        base = validated.get("reserve_base_xrp")
        owner = validated.get("reserve_inc_xrp")
        if base is not None and owner is not None:
            pair = (float(base), float(owner))
            with _reserve_cache_lock:
                _reserve_cache[XRPL_NODE] = (now, pair)
            return pair
    except Exception:
        pass
    return (BASE_RESERVE_XRP, OWNER_RESERVE_XRP)

_cache_lock = threading.Lock()
_cache = {}  # (address, lookback_days) -> (fetched_at_unix, data_dict)

# Per-pool metrics cache — shared across wallets, since pool activity is
# wallet-independent. Longer TTL than per-wallet cache because the same hot
# pool will be hit by many wallets.
_POOL_CACHE_TTL = int(os.environ.get("POOL_METRICS_TTL", "600"))
_pool_cache_lock = threading.Lock()
_pool_metrics_cache = {}  # amm_account -> (fetched_at_unix, (volume_xrp, trade_count))


def _load_json_safe(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


_NAMED = _load_json_safe(NAMED_ACCOUNTS_PATH) or {}
_TOKENS_RAW = _load_json_safe(TOKEN_NAMES_PATH) or {}
_TOKEN_BY_KEY = {
    (v.get("currency_hex"), v.get("issuer")): v
    for v in _TOKENS_RAW.values()
    if isinstance(v, dict)
}

# AMM index — used both ways: by account (lookup classifying a counterparty)
# and by (Asset, Asset2) pair (for AMMDeposit/AMMWithdraw txs that don't
# carry the AMM account directly).
_AMM_INDEX = _load_json_safe(AMM_INDEX_PATH) or []
_AMM_BY_ACCOUNT = {}
_AMM_BY_PAIR = {}


def _pair_key(asset, asset2):
    """Canonical (Asset, Asset2) → key for AMM lookup."""
    return (
        asset.get("currency"), asset.get("issuer"),
        asset2.get("currency"), asset2.get("issuer"),
    )


for _entry in _AMM_INDEX:
    if isinstance(_entry, dict) and _entry.get("Account"):
        _AMM_BY_ACCOUNT[_entry["Account"]] = _entry
        _AMM_BY_PAIR[_pair_key(_entry.get("Asset", {}), _entry.get("Asset2", {}))] = _entry["Account"]


def _decode_currency_hex(hex_str):
    if not hex_str or len(hex_str) != 40:
        return hex_str or "?"
    try:
        b = bytes.fromhex(hex_str).rstrip(b"\x00")
    except ValueError:
        return hex_str[:8].upper()
    if not b or not all(32 <= c < 127 for c in b):
        return hex_str[:8].upper()
    try:
        return b.decode("ascii").strip()
    except UnicodeDecodeError:
        return hex_str[:8].upper()


def _short_currency(currency, issuer=None):
    if not currency or currency == "XRP":
        return "XRP"
    if len(currency) == 3:
        return currency
    meta = _TOKEN_BY_KEY.get((currency, issuer)) if issuer else None
    if meta and meta.get("currency_display"):
        return meta["currency_display"]
    return _decode_currency_hex(currency)


def _amm_pair_label(asset, asset2):
    a = _short_currency(asset.get("currency"), asset.get("issuer"))
    b = _short_currency(asset2.get("currency"), asset2.get("issuer"))
    return f"{a}/{b}"


def _classify_address(address):
    """Return (type, display_name) where type ∈ exchange|amm|peer|issuer."""
    if address in _NAMED:
        entry = _NAMED[address]
        category = entry.get("category", "")
        if category in ("exchange", "ripple", "foundation", "service", "issuer_known"):
            ntype = "exchange" if category != "issuer_known" else "issuer"
            return ntype, entry.get("name", address)
        return "peer", entry.get("name", address)
    if address in _AMM_BY_ACCOUNT:
        amm = _AMM_BY_ACCOUNT[address]
        pair = _amm_pair_label(amm.get("Asset", {}), amm.get("Asset2", {}))
        return "amm", f"AMM · {pair}"
    return "peer", None


def _self_info(address):
    """Identity metadata for the focal wallet — name, category, and the
    public attestation URL (e.g. xrp-ledger.toml). Surfaced in the WALLET
    card as a verified badge so users can tell a curated/disclosed entity
    apart from an arbitrary address. Returns all-None for unknown wallets."""
    entry = _NAMED.get(address)
    if not entry:
        return {"name": None, "category": None, "verified_via": None}
    return {
        "name": entry.get("name"),
        "category": entry.get("category"),
        "verified_via": entry.get("verified_via"),
    }


def _amm_self_info(address):
    """If `address` is an AMM pool account, return its pair label
    ("XRP/RLUSD") so the wallet view can re-frame itself as a pool view.
    Returns (False, None) for non-AMM addresses."""
    amm = _AMM_BY_ACCOUNT.get(address)
    if not amm:
        return False, None
    return True, _amm_pair_label(amm.get("Asset", {}), amm.get("Asset2", {}))


def _compute_pool_flow(txs, amm_account):
    """Walk already-fetched account_tx for an AMM and aggregate the signed
    XRP balance change on its AccountRoot across recent swaps.

    Only Payment / OfferCreate transactions count as swaps — AMMDeposit /
    AMMWithdraw / AMMVote etc. also move the pool's XRP balance but they're
    liquidity events, not trades, so they're excluded so flow numbers
    match how other sites count "volume."

    Returns (xrp_in_drops, xrp_out_drops, swap_count, last_swap_unix,
             span_s, swaps, xrp_in_24h_drops, xrp_out_24h_drops,
             swap_count_24h):
      - xrp_in_drops / xrp_out_drops / swap_count: full sample totals.
      - last_swap_unix: ledger time of the most recent swap.
      - span_s: seconds between first and last swap in the sample.
      - swaps: ordered list of {t, dir, size} dicts. Used by the Exact
               reactor mode to replay real swap timing 1:1.
      - xrp_in_24h_drops / xrp_out_24h_drops / swap_count_24h: same as
        above but filtered to the last 86400 seconds of wall-clock. If
        the sample doesn't reach back 24h, these are undercounts.

    No new XRPL calls — reuses the txs the wallet pipeline already fetched.
    """
    xrp_in = 0
    xrp_out = 0
    count = 0
    last_unix = None
    earliest_unix = None
    raw_events = []
    for t in txs:
        inner = _tx_envelope(t)
        if inner.get("TransactionType") not in ("Payment", "OfferCreate"):
            continue
        ts = _ripple_to_unix(inner.get("date"))
        meta = t.get("meta") or t.get("metaData") or {}
        for node in meta.get("AffectedNodes", []) or []:
            mod = node.get("ModifiedNode") or {}
            if mod.get("LedgerEntryType") != "AccountRoot":
                continue
            ff = mod.get("FinalFields") or {}
            if ff.get("Account") != amm_account:
                continue
            pf = mod.get("PreviousFields") or {}
            prev_bal = pf.get("Balance")
            new_bal = ff.get("Balance")
            if not isinstance(prev_bal, str) or not isinstance(new_bal, str):
                continue
            try:
                delta = int(new_bal) - int(prev_bal)
            except ValueError:
                continue
            if delta > 0:
                xrp_in += delta
                raw_events.append((ts, "in", delta))
            elif delta < 0:
                xrp_out += -delta
                raw_events.append((ts, "out", -delta))
            else:
                break
            count += 1
            if ts:
                if last_unix is None or ts > last_unix:
                    last_unix = ts
                if earliest_unix is None or ts < earliest_unix:
                    earliest_unix = ts
            break
    span = (last_unix - earliest_unix) if (last_unix and earliest_unix) else 0
    if earliest_unix is not None:
        swaps = [
            {
                "t": (ts - earliest_unix) if ts else 0,
                "dir": d,
                "size": s,
            }
            for ts, d, s in sorted(raw_events, key=lambda r: r[0] or 0)
        ]
    else:
        swaps = []

    # 24h aggregates — wall-clock window. Undercounted if the sample
    # doesn't reach back that far.
    cutoff = int(time.time()) - 86400
    xrp_in_24h = 0
    xrp_out_24h = 0
    count_24h = 0
    for ts, d, s in raw_events:
        if not ts or ts < cutoff:
            continue
        if d == "in":
            xrp_in_24h += s
        else:
            xrp_out_24h += s
        count_24h += 1

    return (
        xrp_in, xrp_out, count, last_unix, span, swaps,
        xrp_in_24h, xrp_out_24h, count_24h,
    )


def _ripple_to_unix(rt):
    if rt is None:
        return None
    try:
        return int(rt) + RIPPLE_EPOCH
    except (TypeError, ValueError):
        return None


def _short_addr(addr):
    if not addr:
        return None
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 14 else addr


def _age_str(unix_ts):
    if not unix_ts:
        return "—"
    secs = max(0, int(time.time() - unix_ts))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"~{secs // 60}m ago"
    if secs < 86400:
        return f"~{secs // 3600}h ago"
    days = secs // 86400
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def _label_pos(ang):
    """Position a label outside a node at the given angle.
    Returns (label_dy, label_dx, anchor). label_dx may be None (= no offset)."""
    a = ang % 360
    if a > 180:
        a -= 360
    if -100 < a < -80:
        return -22, None, "middle"
    if -80 <= a < -10:
        return -14, 14, "start"
    if -10 <= a < 10:
        return 4, 20, "start"
    if 10 <= a < 80:
        return 20, 14, "start"
    if 80 <= a <= 100:
        return 22, None, "middle"
    if 100 < a <= 170:
        return 20, -14, "end"
    if 170 < a or a <= -170:
        return 4, -20, "end"
    return -14, -14, "end"


def _safe_request(client, request):
    try:
        resp = client.request(request)
        if "error" in resp.result:
            return None
        return resp.result
    except Exception:
        return None


def _fetch_account_info(client, address):
    return _safe_request(client, AccountInfo(account=address, ledger_index="validated"))


def _fetch_account_lines(client, address):
    result = _safe_request(client, AccountLines(account=address, ledger_index="validated"))
    return (result or {}).get("lines", []) if result else []


def _build_holdings(lines):
    """Convert raw account_lines into render-ready holdings.

    Each line: {account: issuer, currency: code, balance: str, ...}
    LP tokens: 40-char hex starting with "03" — these represent AMM pool
    shares and we tag them is_lp=True so the template can split them out.
    """
    out = []
    for ln in lines:
        try:
            bal = float(ln.get("balance") or 0)
        except (TypeError, ValueError):
            bal = 0.0
        # Skip dust + negative-balance trustlines (we owe the issuer).
        if abs(bal) < 1e-8:
            continue
        currency = ln.get("currency") or ""
        issuer = ln.get("account") or ""
        is_lp = len(currency) == 40 and currency.startswith("03")

        meta = _TOKEN_BY_KEY.get((currency, issuer)) or {}
        labeled = bool(meta)
        category = meta.get("category") if meta else None

        if is_lp:
            amm = _AMM_BY_ACCOUNT.get(issuer)
            if amm:
                pair = _amm_pair_label(amm.get("Asset", {}), amm.get("Asset2", {}))
                display = f"LP · {pair}"
            else:
                display = "LP token"
            category = category or "lp"
        elif labeled:
            display = meta.get("currency_display") or currency
        else:
            display = _decode_currency_hex(currency) or currency[:8] + "…"

        out.append({
            "display": display,
            "currency_raw": currency,
            "issuer": issuer,
            "issuer_short": _short_addr(issuer),
            "balance": bal,
            "category": category,
            "labeled": labeled,
            "is_lp": is_lp,
        })
    # Sort: labeled non-LP first, then unlabeled non-LP, then LP — each by balance desc.
    def _sort_key(h):
        if h["is_lp"]:
            tier = 2
        elif h["labeled"]:
            tier = 0
        else:
            tier = 1
        return (tier, -abs(h["balance"]))
    out.sort(key=_sort_key)
    return out


def _amount_xrp(amount):
    """Return XRP value if `amount` is the XRP side of an AMM pool, else None.
    XRPL encodes XRP as a drops string and IOUs as an {currency,issuer,value} object."""
    if isinstance(amount, str):
        try:
            return int(amount) / 1_000_000
        except ValueError:
            return None
    return None


def _amount_iou(amount):
    """Return (currency, issuer, value_float) if amount is an IOU, else None."""
    if isinstance(amount, dict):
        try:
            v = float(amount.get("value") or 0)
        except (TypeError, ValueError):
            v = 0.0
        return (amount.get("currency"), amount.get("issuer"), v)
    return None


def _pool_24h_metrics(client, amm_account):
    """Estimate the pool's 24h XRP throughput by summing |Balance deltas|
    on the AMM AccountRoot across recent Payment txs. The trading_fee skim
    happens per-swap, so volume × fee_pct ≈ pool fees earned in the window.

    Returns: (volume_xrp_24h, trade_count_24h). 0/0 if the pool isn't XRP-paired
    (we can't price token-token volume in XRP without a quote feed).

    Cached per-pool with a 10-minute TTL (shared across wallets) since pool
    activity is wallet-independent and the same hot pool gets hit by many users."""
    now = time.time()
    with _pool_cache_lock:
        cached = _pool_metrics_cache.get(amm_account)
        if cached and (now - cached[0]) < _POOL_CACHE_TTL:
            return cached[1]
    cutoff = int(time.time()) - 86400
    volume_drops = 0
    count = 0
    marker = None
    # Paginate up to 5×400 = 2000 txs; that covers very busy pools while
    # bounding worst-case latency. We break early once we walk past the cutoff.
    done = False
    for _ in range(5):
        if done:
            break
        kwargs = {"account": amm_account, "limit": 400, "forward": False}
        if marker is not None:
            kwargs["marker"] = marker
        result = _safe_request(client, AccountTx(**kwargs))
        if not result:
            break
        txs = result.get("transactions", []) or []
        for t in txs:
            inner = _tx_envelope(t)
            if inner.get("TransactionType") not in ("Payment", "OfferCreate"):
                continue
            unix_ts = _ripple_to_unix(inner.get("date"))
            if unix_ts is None or unix_ts < cutoff:
                # AccountTx returns newest-first; stop scanning past the window.
                done = True
                break
            meta = t.get("meta") or t.get("metaData") or {}
            for node in meta.get("AffectedNodes", []) or []:
                mod = node.get("ModifiedNode") or {}
                if mod.get("LedgerEntryType") != "AccountRoot":
                    continue
                ff = mod.get("FinalFields") or {}
                if ff.get("Account") != amm_account:
                    continue
                pf = mod.get("PreviousFields") or {}
                prev_bal = pf.get("Balance")
                new_bal = ff.get("Balance")
                if not isinstance(prev_bal, str) or not isinstance(new_bal, str):
                    continue
                try:
                    delta = abs(int(new_bal) - int(prev_bal))
                except ValueError:
                    continue
                volume_drops += delta
                count += 1
                break
        marker = result.get("marker")
        if not marker:
            break
    metrics = (volume_drops / 1_000_000, count)
    with _pool_cache_lock:
        _pool_metrics_cache[amm_account] = (now, metrics)
    return metrics


def _enrich_one_lp(h):
    """Worker for the parallel LP enrichment pool. Each call uses its own
    JsonRpcClient since xrpl-py's JsonRpcClient is per-thread safe but we
    want independent connections for parallelism."""
    client = JsonRpcClient(XRPL_NODE)
    amm_account = h["issuer"]
    my_lp = abs(h["balance"])
    result = _safe_request(client, AMMInfo(amm_account=amm_account))
    if not result:
        return None
    amm = result.get("amm") or {}
    amount = amm.get("amount")
    amount2 = amm.get("amount2")
    lp_token = amm.get("lp_token") or {}
    try:
        total_lp = float(lp_token.get("value") or 0)
    except (TypeError, ValueError):
        total_lp = 0.0
    if total_lp <= 0 or my_lp <= 0:
        return None
    share = my_lp / total_lp
    xrp_total = _amount_xrp(amount)
    iou_side = _amount_iou(amount2)
    if xrp_total is None:
        xrp_total = _amount_xrp(amount2)
        iou_side = _amount_iou(amount)
    if xrp_total is None:
        iou_a = _amount_iou(amount)
        iou_b = _amount_iou(amount2)
        if not iou_a or not iou_b:
            return None
        pair = f"{_short_currency(iou_a[0], iou_a[1])}/{_short_currency(iou_b[0], iou_b[1])}"
        paired_token = pair
        my_xrp = None
        total_xrp = None
    else:
        paired_token = _short_currency(iou_side[0], iou_side[1]) if iou_side else "?"
        pair = f"XRP/{paired_token}"
        my_xrp = share * xrp_total
        total_xrp = xrp_total
    try:
        fee_bps = int(amm.get("trading_fee") or 0)
    except (TypeError, ValueError):
        fee_bps = 0
    fee_pct = fee_bps / 1000.0
    if my_xrp is not None:
        pool_24h_vol_xrp, pool_24h_trades = _pool_24h_metrics(client, amm_account)
        pool_24h_fees_xrp = pool_24h_vol_xrp * (fee_pct / 100.0)
        my_24h_fees_xrp = pool_24h_fees_xrp * share
        est_apr_pct = (my_24h_fees_xrp * 365 / my_xrp * 100) if my_xrp > 0 else 0.0
    else:
        pool_24h_vol_xrp = None
        pool_24h_trades = 0
        pool_24h_fees_xrp = None
        my_24h_fees_xrp = None
        est_apr_pct = None
    return {
        "amm_account": amm_account,
        "pair": pair,
        "paired_token": paired_token,
        "fee_pct": fee_pct,
        "my_lp": my_lp,
        "total_lp": total_lp,
        "my_share_pct": share * 100,
        "my_xrp": my_xrp,
        "total_xrp": total_xrp,
        "pool_24h_volume_xrp": pool_24h_vol_xrp,
        "pool_24h_trades": pool_24h_trades,
        "pool_24h_fees_xrp": pool_24h_fees_xrp,
        "my_24h_fees_xrp": my_24h_fees_xrp,
        "est_apr_pct": est_apr_pct,
    }


def _enrich_lp_holdings(holdings_lp):
    """Fan out per-LP enrichment over a small thread pool. Each LP requires
    one AMMInfo call + (for XRP-paired pools) up to 5 paginated AccountTx calls
    to compute 24h volume, so total wall-clock for 4 LPs collapses from ~45s
    sequential to ~12s with workers=4."""
    if not holdings_lp:
        return []
    enriched = []
    max_workers = min(8, max(1, len(holdings_lp)))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for r in ex.map(_enrich_one_lp, holdings_lp):
            if r is not None:
                enriched.append(r)
    enriched.sort(key=lambda x: -(x["my_xrp"] if x["my_xrp"] is not None else -1))
    return enriched


def _fetch_account_tx(client, address, max_pages=MAX_TX_PAGES):
    """Paginated fetch of recent txs. Returns list of tx envelopes."""
    txs = []
    marker = None
    for _ in range(max_pages):
        kwargs = {"account": address, "limit": TX_PAGE_LIMIT, "forward": False}
        if marker is not None:
            kwargs["marker"] = marker
        result = _safe_request(client, AccountTx(**kwargs))
        if not result:
            break
        txs.extend(result.get("transactions", []) or [])
        marker = result.get("marker")
        if not marker:
            break
    return txs


def _tx_envelope(tx):
    """account_tx returns {tx: {...}, meta: {...}, validated: bool}.
    Some node versions use tx_json instead. Return the inner tx dict."""
    return tx.get("tx") or tx.get("tx_json") or {}


def _tx_amount(tx):
    """Return the actual amount that landed for a Payment, preferring the
    delivered_amount from meta (the source of truth post-amendment), then
    falling back to DeliverMax (newer rippled API_VERSION 2+) or Amount
    (older).  Without this, token payments on newer nodes look like None
    because the inner tx dict has DeliverMax instead of Amount.
    """
    meta = tx.get("meta") or tx.get("metaData") or {}
    delivered = meta.get("delivered_amount") if isinstance(meta, dict) else None
    if delivered is not None:
        return delivered
    inner = _tx_envelope(tx)
    return inner.get("Amount") or inner.get("DeliverMax")


def _tx_counterparty(tx, owner_address):
    """Return (counterparty_address, kind) for the tx, or None to skip."""
    inner = _tx_envelope(tx)
    tx_type = inner.get("TransactionType")

    if tx_type == "Payment":
        from_a = inner.get("Account")
        to_a = inner.get("Destination")
        if from_a == owner_address and to_a:
            return (to_a, "payment_out")
        if to_a == owner_address and from_a:
            return (from_a, "payment_in")
        return None

    if tx_type in ("AMMDeposit", "AMMWithdraw", "AMMVote", "AMMBid"):
        a = inner.get("Asset", {}) or {}
        a2 = inner.get("Asset2", {}) or {}
        acct = _AMM_BY_PAIR.get(_pair_key(a, a2))
        if acct:
            return (acct, "amm_op")
        return None

    if tx_type == "TrustSet":
        inner_acct = inner.get("Account")
        limit_amt = inner.get("LimitAmount", {}) or {}
        issuer = limit_amt.get("issuer") if isinstance(limit_amt, dict) else None
        # Two directions:
        #   - we set a trustline to an issuer → counterparty is the issuer
        #   - someone sets a trustline to us (we are the issuer) → counterparty is them
        if inner_acct == owner_address and issuer:
            return (issuer, "trustset")
        if issuer == owner_address and inner_acct:
            return (inner_acct, "trustset_inbound")
        return None

    return None


def _build_pulse(txs, lookback_days):
    """Return list of length lookback_days with daily tx count, oldest first.
    Index lookback_days-1 is today; index 0 is (lookback_days-1) days ago."""
    now = int(time.time())
    cutoff = now - lookback_days * 86400
    buckets = [0] * lookback_days
    for tx in txs:
        inner = _tx_envelope(tx)
        unix_ts = _ripple_to_unix(inner.get("date"))
        if unix_ts is None or unix_ts < cutoff:
            continue
        days_ago = (now - unix_ts) // 86400
        idx = lookback_days - 1 - days_ago
        if 0 <= idx < lookback_days:
            buckets[idx] += 1
    return buckets


def _build_counterparty_graph(txs, owner_address, lookback_days):
    """Aggregate counterparties from txs within the lookback window.
    Returns top-N list of (address, agg_record) sorted by tx_count desc."""
    cutoff = int(time.time()) - lookback_days * 86400
    by_addr = defaultdict(lambda: {
        "tx_count": 0, "first_seen": None, "last_seen": None,
        "volume_in_drops": 0, "volume_out_drops": 0,
        "in_tx_count": 0, "out_tx_count": 0,
        # USD-priced via price_oracle. Sums across XRP + token payments.
        "volume_in_usd": 0.0, "volume_out_usd": 0.0,
        # Track per-token amounts so the detail card can show e.g.
        # "5 RLUSD + 12 USDC" without re-fetching anything.
        "tokens_in": defaultdict(float),  # (cur, iss) -> total
        "tokens_out": defaultdict(float),
    })
    for tx in txs:
        cp = _tx_counterparty(tx, owner_address)
        if cp is None:
            continue
        addr, kind = cp
        if addr == owner_address:
            continue
        inner = _tx_envelope(tx)
        unix_ts = _ripple_to_unix(inner.get("date"))
        if unix_ts is not None and unix_ts < cutoff:
            continue
        rec = by_addr[addr]
        rec["tx_count"] += 1
        if kind == "payment_in":
            rec["in_tx_count"] += 1
        elif kind == "payment_out":
            rec["out_tx_count"] += 1
        if unix_ts:
            if rec["first_seen"] is None or unix_ts < rec["first_seen"]:
                rec["first_seen"] = unix_ts
            if rec["last_seen"] is None or unix_ts > rec["last_seen"]:
                rec["last_seen"] = unix_ts
        amount = _tx_amount(tx)
        if isinstance(amount, str) and amount.isdigit():
            drops = int(amount)
            if kind == "payment_in":
                rec["volume_in_drops"] += drops
            elif kind == "payment_out":
                rec["volume_out_drops"] += drops
        # Price-aware volume: works for both XRP and token amounts.
        # Imported lazily to avoid a hard dependency cycle and so that
        # missing oracle data degrades gracefully (USD just stays 0).
        if kind in ("payment_in", "payment_out"):
            try:
                from price_oracle import value_amount_usd, _amount_token
                usd = value_amount_usd(amount)
                if usd:
                    if kind == "payment_in":
                        rec["volume_in_usd"] += usd
                    else:
                        rec["volume_out_usd"] += usd
                tok = _amount_token(amount)
                if tok:
                    cur, iss, val = tok
                    bucket = rec["tokens_in"] if kind == "payment_in" else rec["tokens_out"]
                    bucket[(cur, iss)] += val
            except Exception:
                pass
    sorted_cps = sorted(by_addr.items(), key=lambda x: -x[1]["tx_count"])
    return sorted_cps[:TOP_N_COUNTERPARTIES]


def _layout_nodes(counterparties, total_recent_txs):
    """Convert top counterparties → render-ready node dicts (matches the
    shape that wallet.html's JS expects in its `nodes` array)."""
    n = len(counterparties)
    if n == 0:
        return []
    now = int(time.time())
    max_count = max(c["tx_count"] for _, c in counterparties) or 1
    nodes = []
    for i, (addr, c) in enumerate(counterparties):
        ang = -90 + (360 * i / n)
        ntype, name = _classify_address(addr)
        if name is None:
            name = "Unknown account"
        weight = round(c["tx_count"] / max_count, 2)
        pct = round(100 * c["tx_count"] / total_recent_txs, 1) if total_recent_txs else 0
        first_seen = c["first_seen"]
        first_seen_days = (now - first_seen) // 86400 if first_seen else None

        label_dy, label_dx, anchor = _label_pos(ang)

        detail = {
            # `address` is the display-shortened form (rXxx…YyY) used inside
            # the small detail-card tile. `addressFull` is the un-truncated
            # XRPL address used as the click-through URL — needed because
            # /wallet/<address> validates the address strictly and the
            # ellipsis form would 404.
            "address": _short_addr(addr),
            "addressFull": addr,
            "txCount": c["tx_count"],
            "lastSeen": _age_str(c["last_seen"]),
        }
        if first_seen_days is not None:
            detail["firstSeen"] = (
                "today" if first_seen_days == 0 else f"{first_seen_days} days ago"
            )
        if c["volume_in_drops"]:
            detail["volumeIn"] = f"{c['volume_in_drops']/1_000_000:,.0f} XRP"
        if c["volume_out_drops"]:
            detail["volumeOut"] = f"{c['volume_out_drops']/1_000_000:,.0f} XRP"
        # USD-priced totals (sums XRP + tokens). These drive the on-graph
        # label when available — falls back to count if pricing failed.
        usd_in = c.get("volume_in_usd") or 0.0
        usd_out = c.get("volume_out_usd") or 0.0
        if usd_in > 0:
            detail["usdIn"] = usd_in
        if usd_out > 0:
            detail["usdOut"] = usd_out
        # Per-token breakdown for the detail card — useful when most flow
        # was in stablecoins or specific tokens.
        token_lines_in = []
        for (cur, iss), val in (c.get("tokens_in") or {}).items():
            sym = _short_currency(cur, iss)
            token_lines_in.append({"sym": sym, "amount": round(val, 4)})
        token_lines_out = []
        for (cur, iss), val in (c.get("tokens_out") or {}).items():
            sym = _short_currency(cur, iss)
            token_lines_out.append({"sym": sym, "amount": round(val, 4)})
        if token_lines_in:
            detail["tokensIn"] = sorted(token_lines_in, key=lambda x: -x["amount"])
        if token_lines_out:
            detail["tokensOut"] = sorted(token_lines_out, key=lambda x: -x["amount"])
        if ntype == "amm":
            detail["pair"] = name.replace("AMM · ", "")
            detail["action"] = "View pool"
        elif ntype == "exchange":
            detail["action"] = f"View {c['tx_count']} txs"
        elif first_seen_days is not None and first_seen_days < 14:
            detail["action"] = "Investigate"
        else:
            detail["action"] = f"View {c['tx_count']} txs"

        in_v = c["volume_in_drops"]
        out_v = c["volume_out_drops"]
        in_n = c["in_tx_count"]
        out_n = c["out_tx_count"]
        if in_v + out_v > 0:
            ratio = in_v / (in_v + out_v)
        elif in_n + out_n > 0:
            ratio = in_n / (in_n + out_n)
        else:
            ratio = None
        if ratio is None:
            direction = "both"
        elif ratio >= 0.65:
            direction = "in"
        elif ratio <= 0.35:
            direction = "out"
        else:
            direction = "both"

        node = {
            "name": name,
            "ang": round(ang, 1),
            "w": weight,
            "pct": pct,
            "type": ntype,
            "labelDy": label_dy,
            "anchor": anchor,
            "detail": detail,
            "direction": direction,
            "usdIn": usd_in if usd_in > 0 else None,
            "usdOut": usd_out if usd_out > 0 else None,
        }
        if label_dx is not None:
            node["labelDx"] = label_dx
        # Flag as recently-first-seen if we saw them <30 days ago. The JS
        # uses this to render the amber badge + pulsing ring.
        if first_seen_days is not None and first_seen_days <= 30:
            node["firstSeenDays"] = first_seen_days
        nodes.append(node)
    return nodes


def fetch_wallet_data(address, lookback_days=LOOKBACK_DAYS):
    client = JsonRpcClient(XRPL_NODE)
    is_amm, amm_pair = _amm_self_info(address)
    info = _fetch_account_info(client, address)
    if info is None:
        return {
            "error": "Account not found on the XRP Ledger.",
            "address": address,
            "address_short": _short_addr(address),
            "self_info": _self_info(address),
            "is_amm": is_amm,
            "amm_pair": amm_pair,
            "balance_xrp": 0.0, "available_xrp": 0.0, "reserved_xrp": 0.0,
            "base_reserve_xrp": BASE_RESERVE_XRP,
            "owner_reserve_xrp": OWNER_RESERVE_XRP,
            "pct_locked": 0.0, "owner_count": 0, "trustline_count": 0,
            "tx_count_30d": 0, "active_days_30d": 0,
            "lookback_days": lookback_days,
            "last_seen": "—", "top_counterparty_label": "—",
            "top_counterparty_addr": None,
            "pulse": [0] * lookback_days, "nodes": [], "tx_sample_size": 0,
            "holdings": [], "holdings_lp": [],
            "amm_positions": [], "amm_total_xrp": 0.0,
            "amm_24h_fees_xrp": 0.0, "amm_blended_apr": 0.0,
            "pool_flow": None,
        }
    account_data = info.get("account_data", {})
    balance_drops = int(account_data.get("Balance", "0"))
    owner_count = int(account_data.get("OwnerCount", 0))
    balance_xrp = balance_drops / 1_000_000
    base_reserve_xrp, owner_reserve_xrp = _fetch_server_reserves(client)
    reserved_xrp = base_reserve_xrp + owner_reserve_xrp * owner_count
    available_xrp = max(0.0, balance_xrp - reserved_xrp)
    pct_locked = (reserved_xrp / balance_xrp) if balance_xrp > 0 else 0.0

    lines = _fetch_account_lines(client, address)
    trustline_count = len(lines)
    holdings = _build_holdings(lines)
    holdings_token = [h for h in holdings if not h["is_lp"]]
    holdings_lp = [h for h in holdings if h["is_lp"]]
    # Skip the per-LP enrichment (AMMInfo + 5 paginated AccountTx per LP)
    # when the focal address is itself an AMM pool — the wallet view
    # renders a "Pool reserves" card instead of the AMM-allocation block,
    # so the enrichment output is unused. Without this short-circuit a
    # pool with N LP-bearing trustlines stalls the page for 5–25s on cold
    # cache because each enrichment is sequentially expensive.
    amm_positions = [] if is_amm else _enrich_lp_holdings(holdings_lp)
    amm_total_xrp = sum(p["my_xrp"] for p in amm_positions if p["my_xrp"] is not None)
    amm_24h_fees_xrp = sum(
        p["my_24h_fees_xrp"] for p in amm_positions if p["my_24h_fees_xrp"] is not None
    )
    amm_blended_apr = (
        (amm_24h_fees_xrp * 365 / amm_total_xrp * 100) if amm_total_xrp > 0 else 0.0
    )

    # AMM pool accounts have very high tx volume (every swap = a tx) and
    # each AccountTx page is ~5s against a public node. Cap to a single
    # page for AMM views — keeps the recent-swappers graph meaningful
    # without paginating thousands of historic swaps. Regular wallets
    # still get the full 5-page lookback for the 30-day pulse.
    txs = _fetch_account_tx(client, address, max_pages=1 if is_amm else MAX_TX_PAGES)
    pulse = _build_pulse(txs, lookback_days)
    counterparties = _build_counterparty_graph(txs, address, lookback_days)
    total_recent_txs = sum(pulse)
    active_days = sum(1 for x in pulse if x > 0)
    nodes = _layout_nodes(counterparties, total_recent_txs)

    last_seen_unix = None
    if txs:
        inner = _tx_envelope(txs[0])
        last_seen_unix = _ripple_to_unix(inner.get("date"))
    last_seen = _age_str(last_seen_unix)

    top_label = "—"
    top_addr_full = None
    if nodes:
        top = nodes[0]
        top_label = f"{top['name']} · {top['pct']}%"
        top_addr_full = (top.get("detail") or {}).get("addressFull")

    # AMM-only: aggregate the recent flow direction so the pool widget
    # can bias its slosh to match what's actually happening on-chain.
    pool_flow = None
    if is_amm:
        (
            xrp_in_drops, xrp_out_drops, swap_count, last_swap_unix,
            span_s, swap_events,
            xrp_in_24h_drops, xrp_out_24h_drops, swap_count_24h,
        ) = _compute_pool_flow(txs, address)
        # Token reserve = the largest non-LP holding (XRP-paired AMMs only
        # hold their two assets, so this is the paired-token side).
        token_holding = holdings_token[0] if holdings_token else None
        token_reserve = abs(token_holding["balance"]) if token_holding else 0.0
        token_label = token_holding["display"] if token_holding else "?"
        # Implied price = token per 1 XRP (constant-product spot price).
        current_price = (token_reserve / balance_xrp) if balance_xrp > 0 else 0.0
        last_swap_age_s = (
            int(time.time() - last_swap_unix) if last_swap_unix else None
        )
        # Convert swap drops → XRP for the JS replay, drop the size field
        # if drops==0 (shouldn't happen, but defensive).
        swaps_xrp = [
            {"t": float(s["t"]), "dir": s["dir"], "size": s["size"] / 1_000_000}
            for s in swap_events
        ]
        pool_flow = {
            "xrp_in": xrp_in_drops / 1_000_000,
            "xrp_out": xrp_out_drops / 1_000_000,
            "swap_count": swap_count,
            "xrp_in_24h": xrp_in_24h_drops / 1_000_000,
            "xrp_out_24h": xrp_out_24h_drops / 1_000_000,
            "swap_count_24h": swap_count_24h,
            "volume_24h_xrp": (xrp_in_24h_drops + xrp_out_24h_drops) / 1_000_000,
            "window_seconds": span_s,
            "current_price": current_price,
            "last_swap_age_s": last_swap_age_s,
            "xrp_reserve": balance_xrp,
            "token_reserve": token_reserve,
            "token_label": token_label,
            "swaps": swaps_xrp,
        }

    return {
        "error": None,
        "address": address,
        "address_short": _short_addr(address),
        "self_info": _self_info(address),
        "is_amm": is_amm,
        "amm_pair": amm_pair,
        "balance_xrp": balance_xrp,
        "available_xrp": available_xrp,
        "reserved_xrp": reserved_xrp,
        "base_reserve_xrp": base_reserve_xrp,
        "owner_reserve_xrp": owner_reserve_xrp,
        "pct_locked": pct_locked,
        "owner_count": owner_count,
        "trustline_count": trustline_count,
        "tx_count_30d": total_recent_txs,
        "active_days_30d": active_days,
        "lookback_days": lookback_days,
        "last_seen": last_seen,
        "top_counterparty_label": top_label,
        "top_counterparty_addr": top_addr_full,
        "pulse": pulse,
        "nodes": nodes,
        "tx_sample_size": len(txs),
        "holdings": holdings_token,
        "holdings_lp": holdings_lp,
        "amm_positions": amm_positions,
        "amm_total_xrp": amm_total_xrp,
        "amm_24h_fees_xrp": amm_24h_fees_xrp,
        "amm_blended_apr": amm_blended_apr,
        "pool_flow": pool_flow,
    }


def fetch_wallet_data_cached(address, lookback_days=LOOKBACK_DAYS, ttl=None):
    ttl = ttl if ttl is not None else CACHE_TTL
    key = (address, lookback_days)
    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and (now - cached[0]) < ttl:
            data = dict(cached[1])
            data["cached_age_seconds"] = round(now - cached[0], 1)
            return data
        fresh = fetch_wallet_data(address, lookback_days)
        _cache[key] = (now, fresh)
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result


if __name__ == "__main__":
    import sys
    addr = sys.argv[1] if len(sys.argv) > 1 else "rwietsevLFg8XSmG3bEZzFein1g8RBqWDZ"
    print(f"fetching {addr}...")
    t0 = time.time()
    data = fetch_wallet_data(addr)
    elapsed = time.time() - t0
    print(f"  fetched in {elapsed:.1f}s")
    if data.get("error"):
        print(f"  ERROR: {data['error']}")
        sys.exit(1)
    print(f"  balance: {data['balance_xrp']:,.2f} XRP")
    print(f"  reserved: {data['reserved_xrp']:.2f} XRP ({data['pct_locked']*100:.2f}% locked)")
    print(f"  trustlines: {data['trustline_count']}")
    print(f"  tx sample: {data['tx_sample_size']}")
    print(f"  30d txs: {data['tx_count_30d']} · active days: {data['active_days_30d']}/{data['lookback_days']}")
    print(f"  last seen: {data['last_seen']}")
    print(f"  pulse: {data['pulse']}")
    print(f"  nodes ({len(data['nodes'])}):")
    for n in data["nodes"]:
        flag = f" [NEW · {n['firstSeenDays']}d]" if "firstSeenDays" in n else ""
        print(f"    {n['type']:9s} {n['name'][:30]:30s} ang={n['ang']:+6.1f} w={n['w']:.2f} pct={n['pct']:5.1f}%{flag}")
