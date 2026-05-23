"""
Live XRPL amendments tracker for the /amendments page.

Pulls two public RPCs:
  - `feature`         — every amendment the responding node knows about,
                         with enabled/supported flags
  - `ledger_entry`    — the Amendments ledger object, which lists the
                         currently-enabled amendments AND any amendments
                         in the 14-day Majorities activation window

Combines them into a single state document the page can render straight.
The interesting cases are: in-flight (supported but not yet enabled), and
the rare "in Majorities but the responding node does not recognize the
hash" case — meaning validators on a newer build are voting on an
amendment definition that current released rippled binaries don't carry.

TTL=300s; amendment voting state changes slowly enough that 5-min cache
freshness is invisible and reduces s1.ripple.com load 5x vs 60s.
"""

import os
import threading
import time

import httpx

XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
CACHE_TTL = int(os.environ.get("AMENDMENTS_CACHE_TTL", "300"))

# Canonical ledger index for the Amendments singleton ledger object.
# This is a fixed, documented index — same on every XRPL network.
AMENDMENTS_LEDGER_INDEX = (
    "7DB0788C020F02780A673DC74757F23823FA3014C1866E72CC4CD8B226CD6EF4"
)

# XRPL epoch = 2000-01-01 00:00:00 UTC. unix = xrpl + this.
XRPL_EPOCH_OFFSET = 946684800

# Validators that have a majority for 14 consecutive days activate the
# amendment at the first close after the window elapses.
ACTIVATION_WINDOW_SECONDS = 14 * 24 * 3600

# Amendments the responding node still reports as enabled=False but
# which have been superseded by a later amendment that bundled their
# effects (and IS enabled). The XRPL node `feature` API doesn't expose
# an `obsolete` flag, so we maintain this list manually against the
# canonical registry.
#
# Source of truth: https://xrpl.org/resources/known-amendments
# When adding entries, verify the "Obsolete" status there before shipping.
OBSOLETE_AMENDMENTS = {
    "NonFungibleTokensV1",   # superseded by NonFungibleTokensV1_1
    "fixNFTokenDirV1",        # effects bundled into NonFungibleTokensV1_1
    "fixNFTokenNegOffer",     # effects bundled into NonFungibleTokensV1_1
}

# Hashes we can name from off-ledger sources even when the responding
# node doesn't carry the definition (e.g. amendments merged to rippled
# `develop` but not yet in a released binary). Each entry must cite a
# verifiable source the reader can re-check.
KNOWN_UNRECOGNIZED_HASHES = {
    "303ACB16CF8DBD3B5C34F131A9D19A7DE01AE05F480A8A682B869D1B4AAC8CFC": {
        "name": "fixCleanup3_1_3",
        "source_label": "rippled PR #7128 (merged 2026-05-13)",
        "source_url": "https://github.com/XRPLF/rippled/pull/7128",
    },
}

_cache_lock = threading.Lock()
_cache = {"fetched_at": 0.0, "data": None}


def _post(method, params):
    try:
        resp = httpx.post(
            XRPL_NODE,
            json={"method": method, "params": [params]},
            timeout=15.0,
        )
        return (resp.json() or {}).get("result") or {}
    except Exception:
        return None


def _xrpl_close_to_iso(close_time):
    if close_time is None:
        return None
    try:
        return time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(int(close_time) + XRPL_EPOCH_OFFSET),
        )
    except (TypeError, ValueError):
        return None


def fetch_amendments_state():
    """Return a fresh combined state dict. No caching here; see the
    `_cached` wrapper for memoization."""
    feat_result = _post("feature", {})
    ledger_result = _post(
        "ledger_entry",
        {"index": AMENDMENTS_LEDGER_INDEX, "ledger_index": "validated"},
    )
    if not feat_result or not ledger_result:
        return {"ok": False}

    features = feat_result.get("features") or {}
    node = (ledger_result.get("node") or {})
    majorities_raw = node.get("Majorities") or []
    enabled_hashes = set(node.get("Amendments") or [])

    enabled = []
    in_flight = []
    superseded = []
    for h, info in features.items():
        if not isinstance(info, dict):
            continue
        name = info.get("name")
        if info.get("enabled"):
            enabled.append({"hash": h, "name": name})
        elif name in OBSOLETE_AMENDMENTS:
            superseded.append({"hash": h, "name": name})
        elif info.get("supported"):
            in_flight.append({"hash": h, "name": name})

    enabled.sort(key=lambda x: (x["name"] or "").lower())
    in_flight.sort(key=lambda x: (x["name"] or "").lower())
    superseded.sort(key=lambda x: (x["name"] or "").lower())

    majorities = []
    for entry in majorities_raw:
        m = (entry or {}).get("Majority") or {}
        h = m.get("Amendment")
        close = m.get("CloseTime")
        if not h:
            continue
        feat = features.get(h) or {}
        name = feat.get("name")
        recognized = bool(feat)
        known_meta = KNOWN_UNRECOGNIZED_HASHES.get(h) if not recognized else None
        majority_iso = _xrpl_close_to_iso(close)
        activation_iso = _xrpl_close_to_iso(
            close + ACTIVATION_WINDOW_SECONDS if close is not None else None
        )
        majorities.append({
            "hash": h,
            "name": name,
            "recognized": recognized,
            "known_meta": known_meta,
            "majority_close_time_xrpl": close,
            "majority_reached_iso": majority_iso,
            "activation_eta_iso": activation_iso,
        })

    return {
        "ok": True,
        "ledger_index": ledger_result.get("ledger_index")
            or feat_result.get("ledger_index"),
        "enabled_count": len(enabled),
        "in_flight": in_flight,
        "in_flight_count": len(in_flight),
        "superseded": superseded,
        "superseded_count": len(superseded),
        "majorities": majorities,
        "fetched_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def fetch_amendments_state_cached(ttl=None):
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < ttl:
            data = dict(_cache["data"])
            data["cached_age_seconds"] = round(now - _cache["fetched_at"], 1)
            return data
        fresh = fetch_amendments_state()
        if fresh.get("ok"):
            _cache["fetched_at"] = now
            _cache["data"] = fresh
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result
