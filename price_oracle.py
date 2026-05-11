"""Self-sourced USD price oracle for XRPL tokens.

Goal: the dashboard owns its pricing layer end-to-end. Every USD figure on
every page is derived from data that lives on the XRP Ledger itself — no
borrowed third-party APIs.

Architecture (test scope):
  XRP/USD anchor   ← median of 2+ XRP/stablecoin AMMs (Ripple RLUSD,
                     GateHub USD, etc.) live-queried via amm_info
  Token/USD        ← (token/XRP from the token's XRP-paired AMM) × XRP/USD
  Cache            ← in-memory, ~60s TTL per key

Surfaces:
  price_oracle.xrp_usd()                 → float USD per 1 XRP
  price_oracle.token_usd(cur, issuer)    → float USD per 1 unit of token
  price_oracle.value_amount_usd(amount)  → USD value of an XRPL Amount
                                           (handles both drops-string and
                                           token-dict forms)

Production path (out of scope for this test): a Mac worker runs the same
queries every N ledgers and writes snapshots to Neon Postgres; Flask reads
from the snapshot table. The interface stays the same.
"""

from __future__ import annotations

import os
import time
import statistics
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Optional, Tuple

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AMMInfo

# Reuse the same node config as the rest of the app.
XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
PRICE_TTL = int(os.environ.get("PRICE_TTL_SECONDS", "60"))
# Hard cap per AMM request so a slow XRPL node can't stall a Flask request.
# JsonRpcClient has no timeout knob, so we run the call in a worker thread.
AMM_REQUEST_TIMEOUT = float(os.environ.get("PRICE_AMM_TIMEOUT_SECONDS", "4"))
# When all anchors fail (or any per-token AMM fetch fails), cache the None
# briefly so the next request doesn't re-spam XRPL — keeps the page snappy
# during a network blip and gives the bad cache time to recover.
NEGATIVE_TTL = int(os.environ.get("PRICE_NEGATIVE_TTL_SECONDS", "30"))

# Background pool used solely to enforce per-request timeouts on AMM fetches.
# Single worker is enough — we serialize fetches inside _xrp_per_unit_via_amm
# anyway, and we never want this to compete with Flask request workers.
_AMM_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="oracle-amm")

# ---------------------------------------------------------------------------
# Anchors: XRP/USD reference pools.
# Each entry is a stablecoin we trust to be ~$1, paired with XRP via an AMM.
# We query each AMM's reserves, derive XRP-per-stablecoin, take the median.
# ---------------------------------------------------------------------------
# RLUSD: Ripple's USD stablecoin (40-char hex form on-chain).
RLUSD_HEX = "524C555344000000000000000000000000000000"
RLUSD_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
# GateHub USD trustline.
USD_GH_ISSUER = "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq"
# Bitstamp USD trustline.
USD_BITSTAMP_ISSUER = "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B"

# Stable anchors used to derive XRP/USD. Format: (currency, issuer, label).
# Each must be paired against XRP via an AMM the ledger knows about.
_USD_ANCHORS = [
    (RLUSD_HEX, RLUSD_ISSUER, "RLUSD"),
    ("USD", USD_GH_ISSUER, "USD.GH"),
    ("USD", USD_BITSTAMP_ISSUER, "USD.Bitstamp"),
]

# Stablecoins to short-circuit (treated as $1 directly without re-deriving
# through the XRP/USD anchor — the anchor already trusts them, so cycling
# through it would be circular).
_STABLE_KEYS = {(c, i) for (c, i, _) in _USD_ANCHORS}
_STABLE_KEYS.add(("USDC", "rcEGREd8NmkKRE8GE424sksyt1tJVFZwu"))  # placeholder
# (Add more stables here as we verify them.)

# ---------------------------------------------------------------------------
# In-memory cache. Test scope only — production reads from a snapshot table.
# ---------------------------------------------------------------------------
_cache: dict = {}  # key -> (fetched_at_unix, value)


def _cache_get(key):
    """Return cached value or None when missing/expired. None values use a
    shorter TTL (NEGATIVE_TTL) so the cache learns quickly when the oracle
    recovers from an outage; positive values use PRICE_TTL."""
    rec = _cache.get(key)
    if rec is None:
        return None
    fetched_at, value = rec
    ttl = NEGATIVE_TTL if value is None else PRICE_TTL
    if time.time() - fetched_at > ttl:
        return None
    return value


def _cache_put(key, value):
    _cache[key] = (time.time(), value)


def _cache_has_fresh(key):
    """True iff `key` has a fresh entry (positive OR negative). Lets callers
    distinguish 'cached miss' from 'never tried' so we don't double-fetch."""
    rec = _cache.get(key)
    if rec is None:
        return False
    fetched_at, value = rec
    ttl = NEGATIVE_TTL if value is None else PRICE_TTL
    return (time.time() - fetched_at) <= ttl


def _client():
    return JsonRpcClient(XRPL_NODE)


def _amount_xrp(amount):
    """Return XRP amount (whole XRP, not drops) if the amount field is a
    drops-string; else None. Mirrors wallet_data._amount_xrp but kept local
    to avoid a circular import."""
    if isinstance(amount, str) and amount.isdigit():
        return int(amount) / 1_000_000.0
    return None


def _amount_token(amount):
    """Return (currency, issuer, value_float) for a token-dict amount, else
    None."""
    if not isinstance(amount, dict):
        return None
    try:
        v = float(amount.get("value") or 0)
    except (TypeError, ValueError):
        return None
    return (amount.get("currency"), amount.get("issuer"), v)


def _amm_fetch_blocking(currency: str, issuer: str):
    """Synchronous AMM fetch. Returned dict is `result` from rippled, or None
    on any error. Runs inside the ThreadPoolExecutor so the caller can apply
    a wall-clock timeout (JsonRpcClient has no native timeout)."""
    try:
        req = AMMInfo(
            asset={"currency": "XRP"},
            asset2={"currency": currency, "issuer": issuer},
        )
        client = _client()
        resp = client.request(req)
        return resp.result
    except Exception:
        return None


def _xrp_per_unit_via_amm(currency: str, issuer: str) -> Optional[float]:
    """Query the XRP/<token> AMM and return how many XRP equal 1 unit of the
    token. Returns None if no XRP-paired AMM exists, the request fails, or
    the request exceeds AMM_REQUEST_TIMEOUT (so a slow XRPL node never stalls
    a Flask request).

    We send amm_info with the asset pair rather than an account, so we don't
    need to know the AMM account ahead of time — the ledger resolves it.
    """
    try:
        future = _AMM_EXECUTOR.submit(_amm_fetch_blocking, currency, issuer)
        result = future.result(timeout=AMM_REQUEST_TIMEOUT)
    except FuturesTimeout:
        return None
    except Exception:
        return None
    if not result or "error" in result:
        return None

    amm = result.get("amm") or {}
    a1, a2 = amm.get("amount"), amm.get("amount2")
    # One side is XRP (drops-string), the other is the token (dict).
    xrp_side = _amount_xrp(a1) if _amount_xrp(a1) is not None else _amount_xrp(a2)
    tok_side = _amount_token(a2) if _amount_token(a2) is not None else _amount_token(a1)
    if xrp_side is None or tok_side is None:
        return None
    _, _, tok_amount = tok_side
    if tok_amount <= 0:
        return None
    # XRP per 1 token = XRP_reserve / token_reserve  (constant-product midpoint)
    return xrp_side / tok_amount


def xrp_usd() -> Optional[float]:
    """USD per 1 XRP, derived from the median of multiple XRP/stablecoin AMMs.
    Returns None if no anchor pool resolves (full outage; should be rare)."""
    # Honor a fresh cache entry even when the cached value is None — that's
    # the negative-cache path that prevents re-spamming XRPL during an outage.
    if _cache_has_fresh(("xrp_usd",)):
        return _cache_get(("xrp_usd",))

    # For each anchor (XRP/STABLE), the AMM tells us how many XRP equal 1
    # stablecoin. If 1 stable ≈ $1, then 1 stable ≈ that-many XRP, so:
    #     XRP/USD = 1 / (XRP_per_stable)
    samples = []
    debug = []
    for cur, iss, label in _USD_ANCHORS:
        xrp_per_stable = _xrp_per_unit_via_amm(cur, iss)
        if xrp_per_stable and xrp_per_stable > 0:
            usd_per_xrp = 1.0 / xrp_per_stable
            samples.append(usd_per_xrp)
            debug.append((label, usd_per_xrp))

    if not samples:
        # Negative-cache the failure so the next page render doesn't refetch
        # all anchors immediately — gives XRPL/network a moment to recover.
        _cache_put(("xrp_usd",), None)
        _cache_put(("xrp_usd_sources",), [])
        return None
    # Median is robust to a single anchor depegging or going thin.
    rate = statistics.median(samples)
    _cache_put(("xrp_usd",), rate)
    _cache_put(("xrp_usd_sources",), debug)
    return rate


def xrp_usd_sources():
    """Returns the list of (label, usd_per_xrp) samples used in the last
    median calculation. Useful for the methodology page and debugging."""
    return _cache_get(("xrp_usd_sources",)) or []


def token_usd(currency: str, issuer: str) -> Optional[float]:
    """USD per 1 unit of token. Strategy:
      1. If token is XRP → return xrp_usd().
      2. If token is a known stablecoin in our anchor set → return ~1.0.
      3. Otherwise: derive token/XRP from the token's XRP-paired AMM, then
         multiply by XRP/USD.
    Returns None if pricing isn't available (no XRP-paired AMM, etc.)."""
    if currency == "XRP" or (currency is None and issuer is None):
        return xrp_usd()
    cache_key = ("token_usd", currency, issuer)
    if _cache_has_fresh(cache_key):
        return _cache_get(cache_key)

    if (currency, issuer) in _STABLE_KEYS:
        _cache_put(cache_key, 1.0)
        return 1.0

    xrp_per_token = _xrp_per_unit_via_amm(currency, issuer)
    if xrp_per_token is None:
        _cache_put(cache_key, None)
        return None
    usd_per_xrp = xrp_usd()
    if usd_per_xrp is None:
        _cache_put(cache_key, None)
        return None
    rate = xrp_per_token * usd_per_xrp
    _cache_put(cache_key, rate)
    return rate


def value_amount_usd(amount) -> Optional[float]:
    """Given an XRPL Payment Amount (either a drops-string for XRP or a
    {currency, issuer, value} dict for tokens), return its USD value or None.

    This is the single function the rest of the app should call when
    converting a tx amount into USD — keeps all the branching here.
    """
    xrp = _amount_xrp(amount)
    if xrp is not None:
        rate = xrp_usd()
        return xrp * rate if rate is not None else None
    tok = _amount_token(amount)
    if tok is not None:
        cur, iss, val = tok
        rate = token_usd(cur, iss)
        return val * rate if rate is not None else None
    return None


def value_amount_xrp(amount) -> Optional[float]:
    """XRP-equivalent value of an XRPL Payment Amount.

    Mirrors value_amount_usd but stays inside the XRP unit so callers can
    compare against XRP-denominated thresholds (e.g. the whale floor) without
    a second USD→XRP conversion.
      - drops-string → drops / 1_000_000
      - token dict   → value * xrp_per_token (via XRP/<token> AMM)
    Returns None if the token has no XRP-paired AMM, or the AMM query fails.
    """
    xrp = _amount_xrp(amount)
    if xrp is not None:
        return xrp
    tok = _amount_token(amount)
    if tok is not None:
        cur, iss, val = tok
        xrp_per_token = _xrp_per_unit_via_amm(cur, iss)
        return val * xrp_per_token if xrp_per_token is not None else None
    return None
