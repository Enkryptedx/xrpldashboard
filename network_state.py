"""
Live XRPL UNL (Unique Node List) tracker for the /network page.

XRPL validators choose one or more validator lists to trust. The two
canonical published lists are:

  - vl.ripple.com    — Ripple's UNL
  - vl.xrplf.org     — XRPL Foundation's UNL

Each list is a signed manifest with an expiration. If a list's expiration
passes, operators using that list stop accepting it as authoritative
(but the rest of the configured UNL keeps working). The editorial story
this page exists to tell: XRPL maintained consensus from 2026-01-18
through today even though the Foundation UNL has been expired for
months, because nearly every operator carries the Ripple UNL too —
decentralization-through-overlap, not decentralization-through-redundancy.

We fetch both manifests on a 10-min cache, decode the base64 blobs, and
expose: per-list expiration + validator count, the pubkey overlap
between the two, and the XRPLF/Ripple expiry deltas in days. The
template renders the truth-first version: which list is currently fresh,
which has expired, what the overlap is, and a plain-English explainer.
"""

import base64
import datetime
import json
import os
import threading
import time

import httpx

UNL_SOURCES = [
    {
        "key": "ripple",
        "label": "Ripple UNL",
        "url": "https://vl.ripple.com/",
        "operator": "Ripple",
        "operator_url": "https://ripple.com",
    },
    {
        "key": "xrplf",
        "label": "Foundation UNL",
        "url": "https://vl.xrplf.org/",
        "operator": "XRPL Foundation",
        "operator_url": "https://xrplf.org",
    },
]

CACHE_TTL = int(os.environ.get("NETWORK_CACHE_TTL", "600"))

# Ripple-epoch offset: ripple timestamps are seconds since 2000-01-01 UTC.
RIPPLE_EPOCH = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)

_cache_lock = threading.Lock()
_cache = {"fetched_at": 0.0, "data": None}


def _fetch_unl(url):
    """Return (blob_dict, error). Foundation server 403s without a UA."""
    try:
        resp = httpx.get(
            url,
            timeout=10.0,
            headers={"User-Agent": "xrpldashboard/1.0 (+https://xrpldashboard.com)"},
        )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        outer = resp.json()
        blob_b64 = outer.get("blob")
        if not blob_b64:
            return None, "no blob"
        decoded = json.loads(base64.b64decode(blob_b64))
        return decoded, None
    except Exception as exc:
        return None, str(exc)[:120]


def _expiration_iso(ripple_seconds):
    if ripple_seconds is None:
        return None
    dt = RIPPLE_EPOCH + datetime.timedelta(seconds=int(ripple_seconds))
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_remaining(ripple_seconds):
    if ripple_seconds is None:
        return None
    dt = RIPPLE_EPOCH + datetime.timedelta(seconds=int(ripple_seconds))
    now = datetime.datetime.now(datetime.timezone.utc)
    return int((dt - now).total_seconds() // 86400)


def fetch_network_state():
    """Return a fresh combined UNL state dict (no caching here)."""
    lists = []
    pubkey_sets = {}
    for src in UNL_SOURCES:
        blob, err = _fetch_unl(src["url"])
        if blob is None:
            lists.append({
                **src,
                "ok": False,
                "error": err,
            })
            continue
        validators = blob.get("validators") or []
        pubkeys = sorted({
            (v.get("validation_public_key") or "").upper()
            for v in validators
            if v.get("validation_public_key")
        })
        pubkey_sets[src["key"]] = set(pubkeys)
        expiry_seconds = blob.get("expiration")
        days_remaining = _days_remaining(expiry_seconds)
        lists.append({
            **src,
            "ok": True,
            "sequence": blob.get("sequence"),
            "validator_count": len(validators),
            "expiration_iso": _expiration_iso(expiry_seconds),
            "days_remaining": days_remaining,
            "is_expired": days_remaining is not None and days_remaining < 0,
            "days_past_expiry": (
                abs(days_remaining)
                if days_remaining is not None and days_remaining < 0
                else 0
            ),
            "pubkey_count": len(pubkeys),
        })

    overlap = None
    if "ripple" in pubkey_sets and "xrplf" in pubkey_sets:
        r = pubkey_sets["ripple"]
        f = pubkey_sets["xrplf"]
        inter = r & f
        union = r | f
        overlap = {
            "both": len(inter),
            "ripple_only": len(r - f),
            "xrplf_only": len(f - r),
            "union": len(union),
            "jaccard_pct": round(100.0 * len(inter) / len(union), 1) if union else 0.0,
        }

    any_ok = any(l.get("ok") for l in lists)
    return {
        "ok": any_ok,
        "lists": lists,
        "overlap": overlap,
        "fetched_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def fetch_network_state_cached(ttl=None):
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < ttl:
            data = dict(_cache["data"])
            data["cached_age_seconds"] = round(now - _cache["fetched_at"], 1)
            return data
        fresh = fetch_network_state()
        if fresh.get("ok"):
            _cache["fetched_at"] = now
            _cache["data"] = fresh
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result
