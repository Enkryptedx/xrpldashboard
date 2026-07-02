"""Cached read layer over the oracles_snapshot Postgres table.

The walker (oracle_walker.py) writes to Postgres on a 30-min launchd
cadence. This module reads from Postgres and caches for 5 min so
reloads don't hammer PG when /price-data traffic bunches. Thread-safe.

Never touches the XRPL node — that's the walker's job. If PG is empty
or unreachable, returns a valid empty-shape dict so the template can
render a "no data yet" state instead of 500'ing.

Decoding / conversion happens in the walker; this layer only:
  - groups per owner (one card per r-address)
  - reshapes for the template (currency-code display, staleness
    bucket for the freshness pill)
  - detects the walker_note flag surfaced when an oracle we don't
    curate appears in the data (never in v1, but wired for future).
"""

import os
import threading
import time
from datetime import datetime, timezone

import db

# XLS-47 uses Unix seconds for LastUpdateTime (not ripple-time). No offset.
CACHE_TTL = int(os.environ.get("ORACLE_SNAPSHOT_CACHE_TTL", "300"))

# Freshness pill thresholds (seconds since LastUpdateTime).
FRESHNESS_FRESH_MAX = 15 * 60      # < 15 min → green
FRESHNESS_OK_MAX = 60 * 60         # < 1 h    → yellow
FRESHNESS_STALE_MAX = 24 * 60 * 60  # < 24 h   → orange (>= red)

# Band Protocol was press-claimed for XRPL but not observed in the
# 2026-07-02 partial full-walk sample or in any named-accounts entry.
# When the walker never surfaces a Band-tagged owner, the template
# shows the "announced, not observed" footnote. If Band ever ships
# on-ledger and gets added to named_accounts with category='oracle',
# this flag flips automatically without a code change here.
BAND_PROVIDER_TAGS = frozenset({"band", "bandprotocol", "band_protocol"})

_cache_lock = threading.Lock()
_cache = {"fetched_at": 0.0, "data": None}


def _short_addr(a):
    if not a or len(a) < 12:
        return a or ""
    return f"{a[:6]}…{a[-4:]}"


def _display_currency(code):
    """XRPL currency codes are 3 ASCII chars OR 40-char hex (custom). Hex
    codes decode to a rendered label if possible; otherwise show 8+…+4
    truncated hex. Falls back to the raw code on any error."""
    if not code:
        return "?"
    if len(code) == 3:
        return code
    if len(code) == 40:
        # Try to decode a "standard" 40-char currency code (issuer-defined
        # with trailing zero bytes for shorter names).
        try:
            b = bytes.fromhex(code)
            stripped = b.rstrip(b"\x00")
            if stripped and all(0x20 <= ch < 0x7f for ch in stripped):
                return stripped.decode("ascii")
        except (ValueError, UnicodeDecodeError):
            pass
        return f"{code[:6]}…{code[-4:]}"
    return code


def _freshness_bucket(last_update_time):
    """Return ('fresh'|'ok'|'stale'|'cold'|'unknown', seconds_since_update)
    for the pill. Bucketing keeps the template branchless."""
    if last_update_time is None:
        return "unknown", None
    try:
        age = int(time.time()) - int(last_update_time)
    except (TypeError, ValueError):
        return "unknown", None
    if age < 0:
        return "unknown", age
    if age <= FRESHNESS_FRESH_MAX:
        return "fresh", age
    if age <= FRESHNESS_OK_MAX:
        return "ok", age
    if age <= FRESHNESS_STALE_MAX:
        return "stale", age
    return "cold", age


def _last_update_iso(ts):
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _decorate_oracle(row):
    """Shape one oracles_snapshot row for the template."""
    pairs = []
    for p in row.get("price_data_json") or []:
        pairs.append({
            "base": p.get("base"),
            "quote": p.get("quote"),
            "base_display": _display_currency(p.get("base")),
            "quote_display": _display_currency(p.get("quote")),
            "price_float": p.get("price_float"),
            "scale": p.get("scale"),
            "price_raw_hex": p.get("price_raw_hex"),
        })
    bucket, age = _freshness_bucket(row.get("last_update_time"))
    return {
        **row,
        "owner_short": _short_addr(row.get("owner")),
        "pairs": pairs,
        "freshness_bucket": bucket,
        "seconds_since_update": age,
        "last_update_iso": _last_update_iso(row.get("last_update_time")),
    }


def _build_page_data():
    snap = db.read_oracles_snapshot()
    raw_rows = snap.get("rows") or []
    oracles = [_decorate_oracle(r) for r in raw_rows]

    # Group per-owner for the "one card per provider" render. Sort each
    # owner's oracle list by last_update_time DESC so the freshest leads.
    by_owner = {}
    for o in oracles:
        by_owner.setdefault(o["owner"], {
            "owner": o["owner"],
            "owner_name": o.get("owner_name"),
            "owner_short": o["owner_short"],
            "oracles": [],
        })["oracles"].append(o)
    owners = list(by_owner.values())
    for grp in owners:
        grp["oracles"].sort(
            key=lambda o: (o.get("last_update_time") or 0),
            reverse=True,
        )
        # Card-level freshness = the freshest oracle's bucket (users care
        # about "does this provider have a live price right now?").
        grp["freshness_bucket"] = grp["oracles"][0]["freshness_bucket"] if grp["oracles"] else "unknown"

    owners.sort(key=lambda g: (g.get("owner_name") or "").lower())

    providers_seen = {(o.get("provider") or "").lower().replace(" ", "") for o in oracles}
    band_observed = bool(providers_seen & BAND_PROVIDER_TAGS)

    return {
        "owners": owners,
        "oracle_count": len(oracles),
        "owner_count": len(owners),
        "band_observed": band_observed,
        "snapshot_fetched_at": snap.get("fetched_at"),
        "snapshot_age_seconds": snap.get("snapshot_age_seconds"),
        "snapshot_ledger_index": snap.get("snapshot_ledger_index"),
    }


def fetch_oracle_snapshot_cached(ttl=None):
    """Thread-safe cached read. Returns a page-shaped dict even when PG
    is empty (oracle_count == 0)."""
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < ttl:
            data = dict(_cache["data"])
            data["cache_age_seconds"] = round(now - _cache["fetched_at"], 1)
            return data
        fresh = _build_page_data()
        _cache["fetched_at"] = now
        _cache["data"] = fresh
        result = dict(fresh)
        result["cache_age_seconds"] = 0.0
        return result


if __name__ == "__main__":
    d = fetch_oracle_snapshot_cached(ttl=0)
    print(f"owners: {d['owner_count']}  oracles: {d['oracle_count']}")
    print(f"band_observed: {d['band_observed']}")
    print(f"snapshot_age_seconds: {d['snapshot_age_seconds']}")
    for g in d["owners"]:
        print(f"  {g['owner_name']}  ({g['owner_short']})  freshness={g['freshness_bucket']}")
        for o in g["oracles"]:
            print(f"    doc={o.get('document_id')} provider={o.get('provider')!r} "
                  f"pairs={o.get('pair_count')} bucket={o.get('freshness_bucket')} "
                  f"age={o.get('seconds_since_update')}s")
            for p in o["pairs"][:3]:
                print(f"      {p['base_display']}/{p['quote_display']} "
                      f"price={p['price_float']}")
