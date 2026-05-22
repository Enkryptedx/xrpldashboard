"""Token detail data layer — feeds /token/<currency>/<issuer>.

Pulls everything from local artifacts (no live RPC):
  - volumes.db          → trade activity (24h / 7d / all time + 7d sparkline)
  - token_names.json    → display name + category (when labeled)
  - amm_index.json      → AMM pools containing this token

Cached per (currency, issuer) with a short TTL. Thread-safe.
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import db

HERE = os.path.dirname(os.path.abspath(__file__))
VOLUMES_DB_PATH = os.path.join(HERE, "volumes.db")
TOKEN_NAMES_PATH = os.path.join(HERE, "token_names.json")
AMM_INDEX_PATH = os.path.join(HERE, "amm_index.json")

CACHE_TTL = int(os.environ.get("TOKEN_CACHE_TTL", "120"))
SPARKLINE_HOURS = 24 * 7  # last 7 days, hourly

_cache_lock = threading.Lock()
_cache = {}  # (currency, issuer) -> (fetched_at_unix, data_dict)


def _load_json_safe(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


_TOKENS_RAW = _load_json_safe(TOKEN_NAMES_PATH) or {}
_TOKEN_BY_KEY = {
    (v.get("currency_hex"), v.get("issuer")): v
    for v in _TOKENS_RAW.values()
    if isinstance(v, dict)
}
_AMM_INDEX = _load_json_safe(AMM_INDEX_PATH) or []


def _short_addr(addr):
    if not addr:
        return None
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 14 else addr


def _decode_currency_hex(hex_str):
    if not hex_str or len(hex_str) != 40:
        return None
    try:
        b = bytes.fromhex(hex_str).rstrip(b"\x00")
    except ValueError:
        return None
    if not b or not all(32 <= c < 127 for c in b):
        return None
    try:
        return b.decode("ascii")
    except UnicodeDecodeError:
        return None


def _amm_pools_holding(currency, issuer):
    """Return list of AMM pools whose Asset or Asset2 matches this token."""
    out = []
    for entry in _AMM_INDEX:
        if not isinstance(entry, dict):
            continue
        a1 = entry.get("Asset") or {}
        a2 = entry.get("Asset2") or {}
        m1 = (a1.get("currency") == currency and a1.get("issuer") == issuer) or (
            currency == "XRP" and a1.get("currency") == "XRP"
        )
        m2 = (a2.get("currency") == currency and a2.get("issuer") == issuer) or (
            currency == "XRP" and a2.get("currency") == "XRP"
        )
        if not (m1 or m2):
            continue
        # Trading fee is in 1/100,000 units (e.g. 1000 = 1.0%)
        fee = entry.get("TradingFee") or 0
        fee_pct = fee / 1000.0  # → percent
        # Pair label: render the OTHER asset
        other = a2 if m1 else a1
        other_cur = other.get("currency") or "?"
        other_iss = other.get("issuer")
        other_meta = _TOKEN_BY_KEY.get((other_cur, other_iss)) if other_iss else None
        if other_meta:
            other_display = other_meta.get("currency_display") or other_cur
        elif other_cur == "XRP":
            other_display = "XRP"
        else:
            other_display = (
                _decode_currency_hex(other_cur)
                or (other_cur[:8] + "…" if len(other_cur) > 8 else other_cur)
            )
        out.append({
            "account": entry.get("Account"),
            "account_short": _short_addr(entry.get("Account")),
            "other_display": other_display,
            "other_issuer_short": _short_addr(other_iss) if other_iss else None,
            "fee_pct": fee_pct,
            "lp_balance": (entry.get("LPTokenBalance") or {}).get("value"),
        })
    # Sort by LP balance descending (largest pools first)
    def _lp_key(p):
        try:
            return -float(p["lp_balance"] or 0)
        except (TypeError, ValueError):
            return 0
    out.sort(key=_lp_key)
    return out


def _trade_history(conn, currency, issuer):
    """Return totals + 7d hourly sparkline + first/last bucket."""
    now_hour = int(time.time() // 3600)
    cutoff_24h = now_hour - 24
    cutoff_7d = now_hour - 24 * 7

    row_all = conn.execute(
        "SELECT COALESCE(SUM(trade_count), 0), COALESCE(SUM(volume_xrp), 0), "
        "       COUNT(*), MIN(hour_bucket), MAX(hour_bucket) "
        "FROM token_volume WHERE currency = ? AND issuer = ?",
        (currency, issuer),
    ).fetchone()
    trades_all, volume_all, hours_active, first_bucket, last_bucket = row_all

    row_24 = conn.execute(
        "SELECT COALESCE(SUM(trade_count), 0) FROM token_volume "
        "WHERE currency = ? AND issuer = ? AND hour_bucket >= ?",
        (currency, issuer, cutoff_24h),
    ).fetchone()
    trades_24h = row_24[0] if row_24 else 0

    row_7d = conn.execute(
        "SELECT COALESCE(SUM(trade_count), 0) FROM token_volume "
        "WHERE currency = ? AND issuer = ? AND hour_bucket >= ?",
        (currency, issuer, cutoff_7d),
    ).fetchone()
    trades_7d = row_7d[0] if row_7d else 0

    rows_spark = conn.execute(
        "SELECT hour_bucket, trade_count FROM token_volume "
        "WHERE currency = ? AND issuer = ? AND hour_bucket >= ? "
        "ORDER BY hour_bucket ASC",
        (currency, issuer, cutoff_7d),
    ).fetchall()
    by_hour = {b: c for (b, c) in rows_spark}
    sparkline = [by_hour.get(now_hour - SPARKLINE_HOURS + 1 + i, 0)
                 for i in range(SPARKLINE_HOURS)]

    return {
        "trades_all": int(trades_all or 0),
        "volume_all_xrp": float(volume_all or 0),
        "hours_active": int(hours_active or 0),
        "first_bucket": first_bucket,
        "last_bucket": last_bucket,
        "trades_24h": int(trades_24h or 0),
        "trades_7d": int(trades_7d or 0),
        "sparkline": sparkline,
    }


def _bucket_to_iso(bucket):
    if bucket is None:
        return None
    return datetime.fromtimestamp(
        bucket * 3600, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")


def _bucket_age(bucket):
    """Human age string for the most recent activity bucket."""
    if bucket is None:
        return "—"
    now_hour = int(time.time() // 3600)
    delta = now_hour - bucket
    if delta < 1:
        return "this hour"
    if delta < 24:
        return f"{delta}h ago"
    days = delta // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    return f"{months}mo ago"


def fetch_token_data(currency, issuer):
    """Build the full template payload for one token."""
    # 1. Display name + category
    meta = _TOKEN_BY_KEY.get((currency, issuer)) or {}
    if meta:
        display = meta.get("currency_display") or currency
        category = meta.get("category")
        labeled = True
        source_url = meta.get("source_url")
    else:
        decoded = _decode_currency_hex(currency)
        display = decoded or (currency[:8] + "…" if currency and len(currency) > 8 else (currency or "?"))
        category = None
        labeled = False
        source_url = None

    # 2. Trade history. Prefer Postgres (Render reads here — volumes.db
    # is gitignored, so the SQLite path returns all-zero in prod and the
    # detail page lied about activity that /tokens correctly surfaced).
    # Fall back to the local SQLite file when Postgres isn't configured
    # (dev) or errors out.
    history = {
        "trades_all": 0, "volume_all_xrp": 0.0, "hours_active": 0,
        "first_bucket": None, "last_bucket": None,
        "trades_24h": 0, "trades_7d": 0,
        "sparkline": [0] * SPARKLINE_HOURS,
    }
    history_source = None
    if db.pg_available():
        try:
            history = db.read_token_history(currency, issuer, SPARKLINE_HOURS)
            history_source = "postgres"
        except Exception:
            history_source = None
    if history_source is None and os.path.exists(VOLUMES_DB_PATH):
        try:
            conn = sqlite3.connect(f"file:{VOLUMES_DB_PATH}?mode=ro", uri=True)
            try:
                history = _trade_history(conn, currency, issuer)
                history_source = "sqlite"
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    # 3. AMM pools containing this token
    pools = _amm_pools_holding(currency, issuer)

    return {
        "currency_raw": currency,
        "currency_decoded": _decode_currency_hex(currency),
        "issuer": issuer,
        "issuer_short": _short_addr(issuer),
        "display": display,
        "category": category,
        "labeled": labeled,
        "source_url": source_url,
        "trades_all": history["trades_all"],
        "trades_24h": history["trades_24h"],
        "trades_7d": history["trades_7d"],
        "volume_all_xrp": history["volume_all_xrp"],
        "hours_active": history["hours_active"],
        "first_seen_iso": _bucket_to_iso(history["first_bucket"]),
        "last_seen_iso": _bucket_to_iso(history["last_bucket"]),
        "last_seen_age": _bucket_age(history["last_bucket"]),
        "sparkline": history["sparkline"],
        "sparkline_hours": SPARKLINE_HOURS,
        "pools": pools,
        "pool_count": len(pools),
        "history_source": history_source,
    }


def fetch_token_data_cached(currency, issuer, ttl=None):
    ttl = ttl if ttl is not None else CACHE_TTL
    key = (currency, issuer)
    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and (now - cached[0]) < ttl:
            data = dict(cached[1])
            data["cached_age_seconds"] = round(now - cached[0], 1)
            return data
        fresh = fetch_token_data(currency, issuer)
        _cache[key] = (now, fresh)
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result


if __name__ == "__main__":
    import sys
    cur = sys.argv[1] if len(sys.argv) > 1 else "USD"
    iss = sys.argv[2] if len(sys.argv) > 2 else "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B"
    t0 = time.time()
    data = fetch_token_data(cur, iss)
    print(f"fetched in {time.time() - t0:.2f}s")
    print(f"  display: {data['display']}  category: {data['category']}  labeled: {data['labeled']}")
    print(f"  trades 24h={data['trades_24h']:,}  7d={data['trades_7d']:,}  all={data['trades_all']:,}")
    print(f"  hours_active: {data['hours_active']}  first_seen: {data['first_seen_iso']}  last_seen: {data['last_seen_iso']} ({data['last_seen_age']})")
    print(f"  AMM pools: {data['pool_count']}")
    for p in data["pools"][:5]:
        print(f"    paired with {p['other_display']:14s}  fee {p['fee_pct']:.2f}%  account {p['account_short']}")
