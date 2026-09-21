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

from xrpl.models.requests import AMMInfo

import db
import shared_tier_verifier  # Part C 2026-09-10: live registry tier resolver
import xrpl_client
from check_data import _capability_signals
from sovereign_tunnel_client import SOURCING_SOVEREIGN, SOURCING_STALE_CACHE
from token_naming import (
    TICKER_CANONICAL_PATH,
    decode_currency,
    resolve_display,
)

HERE = os.path.dirname(os.path.abspath(__file__))
VOLUMES_DB_PATH = os.path.join(HERE, "volumes.db")
TOKEN_NAMES_PATH = os.path.join(HERE, "token_names.json")
AMM_INDEX_PATH = os.path.join(HERE, "amm_index.json")

CACHE_TTL = int(os.environ.get("TOKEN_CACHE_TTL", "120"))
SPARKLINE_HOURS = 24 * 7  # last 7 days, hourly

# Capabilities panel staleness threshold. Walker writes every 30 min; 3
# missed cycles = 90 min = data is stale enough to flip the page sourcing
# to stale-cache and light the banner. Kept as a module constant so the
# walker plist cadence and this threshold move together if either shifts.
CAPABILITIES_STALE_SECONDS = 90 * 60

# LP-balance threshold below which a pool is treated as a non-meaningful
# micro / algorithmic-seed pool (typical pattern: thousands of pools across
# a token with sub-1 LP supply, no real liquidity). Lets the /token page
# distinguish "this token has N real pools" from "this token has N pools
# total, most of them dust" without ever hiding rows. 1000 is empirical
# (BITx pools sit at 0.07–11.18; RLUSD/USDC sit above 10^9).
MEANINGFUL_LP_THRESHOLD = 1000.0

_cache_lock = threading.Lock()
_cache = {}  # (currency, issuer) -> (fetched_at_unix, data_dict)

# ── AMM reserves fetch (2026-09-20 Charlie ruling) ────────────────────
#
# Show reserves + LP shares + a per-row teaching line + a canonical
# comparison block when the token collides with a curated ticker. That's
# the presentation fix for impostor-token pages where the LP-token count
# alone reads impressively (rLUSDtyk's RLUSD/XRP pool renders "8,994,790
# shares" but the pool holds 82.7 XRP of real value). Reserves come from
# live amm_info via xrpl_client._post_rpc against the sovereign tunnel;
# cached per AMM account with a freshness stamp. Fail-open: reserves=None
# → template renders "reserves unavailable" rather than shares alone.
_amm_reserves_cache = {}  # {amm_account: (fetched_at_mono, data_or_None)}
_amm_reserves_lock = threading.Lock()
AMM_RESERVES_TTL = int(os.environ.get("TOKEN_AMM_RESERVES_TTL", "300"))

_canonical_registry_cache = None
_canonical_registry_lock = threading.Lock()


def _load_json_safe(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


_TOKENS_RAW = _load_json_safe(TOKEN_NAMES_PATH) or {}
# TODO_curation_pass entries are gated per TOKEN_NAMES.md ("never publish
# a name we can't back to a first-party source"). The /tokens list route
# already applies this filter in _load_token_names_dict; mirror it here
# so the detail page can't publish labels the list page is suppressing.
_TOKEN_BY_KEY = {
    (v.get("currency_hex"), v.get("issuer")): v
    for v in _TOKENS_RAW.values()
    if isinstance(v, dict)
    and v.get("verified_via") != "TODO_curation_pass"
}


def _load_amm_index():
    """Postgres-first; falls back to amm_index.json only when DATABASE_URL
    is unset (dev / Mac path). On Render, a PG failure raises and the
    module import fails — the service refuses to start rather than serve
    a silently-empty pool list."""
    entries = db.read_amm_index_entries()
    if entries is not None:
        return entries
    return _load_json_safe(AMM_INDEX_PATH) or []


_AMM_INDEX = _load_amm_index()


def _short_addr(addr):
    if not addr:
        return None
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 14 else addr


def _amm_reserves_cached(amm_account):
    """Fetch AMM pool reserves via amm_info against the sovereign tunnel.
    Cached per amm_account for AMM_RESERVES_TTL seconds. Fail-open —
    returns None on any error so the template can render "reserves
    unavailable" without shares alone; per Charlie 2026-09-20:
    never show shares without reserves.

    Return shape:
      {"xrp_drops": int|None, "xrp": float|None, "other_amount": str,
       "other_currency": str, "other_issuer": str|None, "fetched_at_iso":
       str}
    or None on error / non-XRP-paired pool.
    """
    if not amm_account:
        return None
    now_mono = time.monotonic()
    with _amm_reserves_lock:
        entry = _amm_reserves_cache.get(amm_account)
        if entry and (now_mono - entry[0]) < AMM_RESERVES_TTL:
            return entry[1]

    data = None
    try:
        # Use the sovereign-preferring XrplClient (LOCAL_NODE tries first,
        # cascades to PUBLIC_NODES with walker_node_fallback logging on
        # failure). Prior version used raw _post_rpc against LOCAL_NODE
        # only — that returned None whenever the tunnel path silently
        # failed on Render, and the impostor page rendered "reserves
        # unavailable" for every row. The cascade preserves sovereignty
        # telemetry via walker_node_fallback but keeps the impostor
        # comparison visible when the tunnel is degraded. Charlie ruling
        # 2026-09-20 (evening): "the live impostor page shows shares
        # alone in prod" is the failure mode we're closing.
        client = xrpl_client.get_client("token_page_amm_reserves")
        resp = client.request(AMMInfo(amm_account=amm_account))
        result = getattr(resp, "result", None) or {}
        amm = result.get("amm") or {}
        a1 = amm.get("amount")
        a2 = amm.get("amount2")
        # One leg is XRP (str of drops); the other is an IOU dict.
        # Both-IOU pools aren't in the display comparison scope; leave
        # data=None so the row shows "reserves unavailable".
        if isinstance(a1, str):
            xrp_drops = int(a1)
            other = a2 if isinstance(a2, dict) else None
        elif isinstance(a2, str):
            xrp_drops = int(a2)
            other = a1 if isinstance(a1, dict) else None
        else:
            xrp_drops = None
            other = None
        if other:
            data = {
                "xrp_drops": xrp_drops,
                "xrp": xrp_drops / 1_000_000.0 if xrp_drops is not None else None,
                "other_amount": other.get("value"),
                "other_currency": other.get("currency"),
                "other_issuer": other.get("issuer"),
                "fetched_at_iso": (
                    datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
            }
    except Exception:
        data = None

    with _amm_reserves_lock:
        _amm_reserves_cache[amm_account] = (now_mono, data)
    return data


def _load_canonical_registry():
    """Cached load of ticker_canonical_issuers.json for the impostor
    comparison lookup. Returns a dict[ticker → set(issuers)] or {}."""
    global _canonical_registry_cache
    with _canonical_registry_lock:
        if _canonical_registry_cache is not None:
            return _canonical_registry_cache
        try:
            with open(TICKER_CANONICAL_PATH) as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            _canonical_registry_cache = {}
            return _canonical_registry_cache
        reg = {}
        for ticker, entry in raw.items():
            if ticker.startswith("_") or not isinstance(entry, dict):
                continue
            reg[ticker] = {
                "canonical_issuers": set(entry.get("canonical_issuers") or []),
                "brand": entry.get("brand"),
                "issuer_name": entry.get("issuer_name"),
            }
        _canonical_registry_cache = reg
        return reg


def _canonical_issuer_for_currency(currency):
    """If `currency` decodes to a ticker that has exactly one canonical
    XRPL issuer in ticker_canonical_issuers.json, return that issuer.
    Return None for the ambiguous cases (no canonical, multiple, or
    unresolvable). Multi-issuer canonicals (e.g. bridged assets) fall
    through — the /token page renders no comparison for those."""
    d = decode_currency(currency)
    if d.get("kind") not in ("decoded", "short"):
        return None
    ticker = d.get("display")
    if not ticker:
        return None
    entry = _load_canonical_registry().get(ticker) or {}
    issuers = entry.get("canonical_issuers") or set()
    if len(issuers) == 1:
        return next(iter(issuers))
    return None


def _canonical_pool_comparison(currency, impostor_issuer, impostor_pools):
    """Build the impostor-vs-canonical XRP-reserve comparison block.

    Picks the biggest XRP-paired AMM for the canonical issuer, fetches
    live reserves for both sides, returns None if either side can't be
    resolved (canonical isn't a single-issuer ticker, or reserves aren't
    available). The template gates the whole comparison box on this
    dict's presence — no reserves = no comparison = no claim.
    """
    canonical_iss = _canonical_issuer_for_currency(currency)
    if not canonical_iss or canonical_iss == impostor_issuer:
        return None
    # Impostor's own biggest XRP-paired pool (from the enriched pools list).
    impostor_pool = None
    for p in impostor_pools:
        if p.get("other_display") == "XRP" and p.get("reserves"):
            impostor_pool = p
            break
    if not impostor_pool:
        return None
    impostor_xrp = (impostor_pool.get("reserves") or {}).get("xrp")
    if impostor_xrp is None:
        return None
    # Canonical's biggest XRP-paired pool (fresh index lookup).
    canonical_pools = _amm_pools_holding(currency, canonical_iss)
    for cp in canonical_pools:
        if cp.get("other_display") == "XRP":
            r = _amm_reserves_cached(cp["account"])
            if r and r.get("xrp") is not None:
                reg_entry = (_load_canonical_registry().get(
                    decode_currency(currency).get("display") or ""
                ) or {})
                brand = reg_entry.get("brand")
                # canonical_issuer_name is the ORG that issues the token
                # (e.g. "Ripple" for RLUSD, "Circle" for USDC), used in
                # possessive comparison copy ("Ripple's RLUSD/XRP pool
                # holds…"). Falls back to brand so tickers without an
                # explicit issuer_name still render the older shape.
                issuer_name = reg_entry.get("issuer_name") or brand
                return {
                    "canonical_issuer": canonical_iss,
                    "canonical_issuer_short": _short_addr(canonical_iss),
                    "canonical_amm_account": cp["account"],
                    "canonical_amm_account_short": _short_addr(cp["account"]),
                    "canonical_xrp_reserve": r["xrp"],
                    "canonical_brand": brand,
                    "canonical_issuer_name": issuer_name,
                    "impostor_amm_account_short": _short_addr(impostor_pool["account"]),
                    "impostor_xrp_reserve": impostor_xrp,
                    "fetched_at_iso": r["fetched_at_iso"],
                }
    return None


def _decode_currency_hex(hex_str):
    """Delegates to token_naming.decode_currency so decode behaviour stays
    consistent with /whales, /check, /tokens. Returns the ASCII string
    for a clean decode or None (matches historical shape callers expect)."""
    if not hex_str or len(hex_str) != 40:
        return None
    d = decode_currency(hex_str)
    return d["display"] if d["kind"] == "decoded" else None


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
    # hours_active is rendered on the "last 7 days" sparkline card; derive
    # it from the sparkline result so the count matches the window the card
    # displays. Mirrors the PG path in db.read_token_history.
    hours_active_7d = len(by_hour)

    return {
        "trades_all": int(trades_all or 0),
        "volume_all_xrp": float(volume_all or 0),
        "hours_active": hours_active_7d,
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

    # Ticker-collision guard (NEW-1, 2026-09-06). Use the issuer's Domain
    # field from the DB snapshot as the collision hint when available —
    # renders "USDT (usdxrp.net — not Tether)" instead of a short-address
    # form. Domain comes from token_issuer_flags_snapshot (already fetched
    # for the capabilities block below).
    _issuer_domain = None
    _snap = db.read_token_issuer_flags(issuer)
    if _snap and _snap.get("domain_hex"):
        try:
            _d = bytes.fromhex(_snap["domain_hex"]).decode("ascii").strip()
            if _d and all(32 <= ord(c) < 127 for c in _d):
                _issuer_domain = _d
        except (ValueError, UnicodeDecodeError):
            pass
    _resolved = resolve_display(currency, issuer, issuer_domain=_issuer_domain)
    ticker_collision = _resolved["collision"]
    non_standard_code = _resolved["non_standard"]

    # LP-token clarifier: extract the underlying pair from the curated display
    # string ("LP: XRP/RLUSD" -> "XRP/RLUSD") so the template can render a
    # one-line explanation. Reuses the already-resolved pair label rather than
    # re-deriving from asset_pair so naming stays consistent with the title.
    pool_pair_label = None
    if category == "lp_token" and isinstance(display, str) and display.startswith("LP: "):
        pool_pair_label = display[4:]

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
    meaningful_pool_count = 0
    for p in pools:
        try:
            if float(p["lp_balance"] or 0) >= MEANINGFUL_LP_THRESHOLD:
                meaningful_pool_count += 1
        except (TypeError, ValueError):
            continue

    # 3a. Enrich the top 20 pools (what the template renders) with live
    # reserves via amm_info. Charlie 2026-09-20: LP shares alone read
    # impressively but say nothing about pool value; show reserves +
    # shares + a per-row teaching line so the honest read is unavoidable.
    # Fail-open — p["reserves"]=None → template shows "reserves
    # unavailable" for that row.
    for p in pools[:20]:
        try:
            p["reserves"] = _amm_reserves_cached(p["account"])
        except Exception:
            p["reserves"] = None

    # 3b. For ticker-collision (impostor) tokens, resolve the canonical
    # issuer's biggest XRP-paired AMM reserves so the template can render
    # a side-by-side "the impostor's pool holds X XRP; Ripple's holds
    # Y XRP" comparison — the scam explained where it happens. None if
    # canonical isn't a single-issuer ticker or reserves aren't live.
    _canonical_comparison = None
    if ticker_collision:
        try:
            _canonical_comparison = _canonical_pool_comparison(
                currency, issuer, pools[:20]
            )
        except Exception:
            _canonical_comparison = None

    # 3c. Daily activity chart labels + optional canonical activity
    # comparison. Charlie 2026-09-20: `/token` shows a labeled 7-bar
    # daily chart with a headline sentence and a count/volume toggle;
    # flagged-token pages also show the canonical issuer's 7-day trade
    # count so a reader can compare impostor activity against real
    # activity in one glance.
    daily_day_labels = []
    if history.get("daily_labels_hour"):
        for h in history["daily_labels_hour"]:
            end_ts = int(h) * 3600
            try:
                dt_end = datetime.fromtimestamp(end_ts, tz=timezone.utc)
                daily_day_labels.append(dt_end.strftime("%a %m-%d"))
            except (ValueError, OSError):
                daily_day_labels.append("")
    _canonical_activity = None
    if ticker_collision:
        canonical_iss = _canonical_issuer_for_currency(currency)
        if canonical_iss and canonical_iss != issuer:
            try:
                canonical_hist = db.read_token_history(
                    currency, canonical_iss, SPARKLINE_HOURS
                )
                _canonical_activity = {
                    "canonical_issuer_short": _short_addr(canonical_iss),
                    "trades_7d": int(canonical_hist.get("trades_7d") or 0),
                    "volume_7d_xrp": float(canonical_hist.get("volume_7d_xrp") or 0),
                    "daily_trades": canonical_hist.get("daily_trades") or [],
                }
            except Exception:
                _canonical_activity = None

    # 4. XRP price (None when no XRP-paired pool clears the dust floor — the
    # absence IS the signal; template renders "—" so consumers don't backfill
    # with stale data. See token_prices.py for the floor rationale.)
    xrp_price = None
    if db.pg_available():
        try:
            xrp_price = db.read_token_price(currency, issuer)
        except Exception:
            xrp_price = None

    # 5. Ledger-level capability signals for the issuer AccountRoot.
    # 2026-09-06: DB-first via token_issuer_flags_snapshot (populated by
    # token_issuer_flags_walker every 30 min on LAN rippled). Kills the
    # last live-RPC path in this module (~3 walker_node_fallback rows/day
    # for walker_name=token_page). Row age > 90 min OR row absent (new
    # issuer, walker hasn't caught up) → sourcing=stale-cache and banner.
    capabilities = []
    capabilities_sourcing = SOURCING_SOVEREIGN
    snap = _snap  # reuse the read above so we don't hit the DB twice
    if snap is None or (snap.get("age_seconds") or 0) > CAPABILITIES_STALE_SECONDS:
        capabilities_sourcing = SOURCING_STALE_CACHE
    if snap and snap.get("fetch_ok"):
        # Rehydrate the account_data shape _capability_signals reads.
        acct = {
            "Flags": snap.get("flags") or 0,
            "TransferRate": snap.get("transfer_rate"),
            "RegularKey": snap.get("regular_key"),
            "signer_lists": [{}] if snap.get("has_signer_list") else [],
            "Domain": snap.get("domain_hex"),
        }
        capabilities = _capability_signals(acct)

    # 6. Review status (Charlie ruling 2026-09-07). Taxonomy v1 splits
    # 'unlabeled' into two honest states: not_yet_reviewed (default —
    # no curator has looked) vs reviewed_unlabeled (curator confirmed
    # no category applies). Derive from token_category_current: if any
    # curator-source row exists, this row has been reviewed. If the row
    # has no curator-source entry AND no cached category, it renders as
    # not_yet_reviewed.
    reviewed = False
    if db.pg_available():
        try:
            with db.pg_connect() as _conn:
                with _conn.cursor() as _cur:
                    _cur.execute(
                        """
                        SELECT 1 FROM token_category_current
                        WHERE currency_hex = %s AND issuer = %s
                          AND source = 'curator'
                        LIMIT 1
                        """,
                        (currency, issuer),
                    )
                    reviewed = bool(_cur.fetchone())
        except Exception:
            reviewed = False

    if category is None or category == 'unlabeled':
        review_status = 'reviewed_unlabeled' if reviewed else 'not_yet_reviewed'
    else:
        review_status = None  # a real category is assigned; split doesn't apply

    # 2026-09-10 Part C: tier + attestation via shared_tier_verifier
    # (LIVE token_category_current → hero snapshot fallback → 'unknown').
    # Canonical lowercase-hyphen; template calls `tier_display` for
    # Title Case. elevate=False — web-request path never stalls on
    # toml fetch + XRPL RPC; a background walker handles elevation.
    _tier_rec = shared_tier_verifier.resolve_tier(currency, issuer, elevate=False)

    return {
        "currency_raw": currency,
        "currency_decoded": _decode_currency_hex(currency),
        "issuer": issuer,
        "issuer_short": _short_addr(issuer),
        "display": display,
        "category": category,
        "reviewed": reviewed,
        "review_status": review_status,
        "labeled": labeled,
        "source_url": source_url,
        "pool_pair_label": pool_pair_label,
        "trades_all": history["trades_all"],
        "trades_24h": history["trades_24h"],
        "trades_7d": history["trades_7d"],
        "volume_all_xrp": history["volume_all_xrp"],
        "xrp_price": xrp_price,
        "hours_active": history["hours_active"],
        "first_seen_iso": _bucket_to_iso(history["first_bucket"]),
        "last_seen_iso": _bucket_to_iso(history["last_bucket"]),
        "last_seen_age": _bucket_age(history["last_bucket"]),
        "sparkline": history["sparkline"],
        "sparkline_hours": SPARKLINE_HOURS,
        "pools": pools,
        "pool_count": len(pools),
        "meaningful_pool_count": meaningful_pool_count,
        "meaningful_lp_threshold": MEANINGFUL_LP_THRESHOLD,
        # 2026-09-20: impostor-vs-canonical XRP reserve comparison for
        # ticker-collision tokens. None when canonical is unresolvable
        # or reserves are unavailable — template gates on presence.
        "canonical_pool_comparison": _canonical_comparison,
        # 2026-09-20 activity redesign: daily 7-bar chart data + optional
        # canonical activity comparison. Template renders whichever the
        # count/volume toggle selects; empty days remain visible as
        # zero-height labeled bars.
        "daily_trades": history.get("daily_trades") or [0]*7,
        "daily_volume": history.get("daily_volume") or [0.0]*7,
        "daily_day_labels": daily_day_labels or [""]*7,
        "volume_7d_xrp": float(history.get("volume_7d_xrp") or 0),
        "canonical_activity_comparison": _canonical_activity,
        "history_source": history_source,
        "capabilities": capabilities,
        "capabilities_sourcing": capabilities_sourcing,
        "capabilities_age_seconds": (snap or {}).get("age_seconds"),
        "sourcing": capabilities_sourcing,
        # NEW-1 (2026-09-06): ticker-collision + non-standard-code fields.
        # Templates render these under the token name; JSON twins expose
        # them so agents can trust-check the collision boundary in code.
        "ticker_collision": ticker_collision,
        "non_standard_code": non_standard_code,
        "issuer_domain": _issuer_domain,
        # 2026-09-10 Part C: tier from shared_tier_verifier (live registry).
        "tier": _tier_rec.tier,
        "tier_display": shared_tier_verifier.title_case_tier(_tier_rec.tier),
        "tier_source": _tier_rec.source,
        "tier_citation": _tier_rec.citation_url,
        "tier_observed_at": _tier_rec.observed_at,
        # 2026-09-11 NO-GO fix: raw external .toml links removed from
        # the token page (Charlie: "A scam-checking site must never
        # send people to a download"). Fallback tonight = text-only
        # proof line derived from tier + source + observed_at.
        # Post-freeze upgrade will render the two-way match inline
        # with a plain-text view of the cached TOML.
        "tier_proof_line": _format_tier_proof_line(_tier_rec),
    }


def _format_tier_proof_line(rec):
    """Text-only proof/source line for the /token page hero.
    Replaces the pre-freeze raw-toml citation link. Format:
        verified + toml source        → "Proof: two-way toml match — last checked YYYY-MM-DD HH:MM UTC"
        verified + curator authority  → "Proof: curator authority two-way match — last checked ..."
        self-described                → "Self-report only — last checked ..."
        labeled (curator)             → "Curator label — last checked ..."
        anything else                 → "Last checked ..." or "" if no observed_at
    """
    if rec is None:
        return ""
    tier = getattr(rec, "tier", None)
    source = getattr(rec, "source", None) or ""
    obs = getattr(rec, "observed_at", None)
    when = ""
    if obs:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(obs.replace("Z", "+00:00"))
            when = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            when = ""
    when_suffix = f" — last checked {when}" if when else ""
    if tier == "verified":
        if "two_way_toml" in source or "two_way_proof" in source:
            head = ("Proof: two-way toml match" if source.startswith("two_way")
                    else "Proof: curator authority two-way match")
        else:
            head = "Proof: verified"
        return head + when_suffix
    if tier == "self-described":
        return "Self-report only" + when_suffix
    if tier == "labeled":
        return "Curator label" + when_suffix
    if when:
        return "Last checked " + when
    return ""


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
