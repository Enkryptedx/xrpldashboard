"""
Live XRPL UNL (Unique Node List) tracker for the /network page.

XRPL validators choose one or more validator lists to trust. The two
canonical published lists are:

  - vl.ripple.com    — Ripple's UNL
  - unl.xrpl.foundation     — XRPL Foundation's UNL

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

from xrpl.core import addresscodec

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
        "url": "https://unl.xrpl.foundation/",
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


def _diff_entry(validator):
    return {
        "pubkey": validator.get("pubkey"),
        "domain": validator.get("domain"),
    }


def _sort_diff_entries(entries):
    # Domain-bearing rows first (alphabetical), null-domain rows last
    # (sorted by pubkey). Stable across runs so the template renders
    # the same order every time.
    return sorted(
        entries,
        key=lambda e: (e["domain"] is None, e["domain"] or "", e["pubkey"]),
    )


def compute_unl_diff(source):
    """Return the most recent diff between two snapshots of `source`,
    or None if fewer than two snapshots exist.

    Identity is pubkey-keyed: a validator that re-publishes its manifest
    with a different Domain stays the same validator. Metadata-only
    changes are intentionally invisible here — only true add/drop events
    surface. If we ever want to surface "domain changed," it's an
    additive enhancement, not a reshape of this contract.

    Shape:
      {
        "from_date": "YYYY-MM-DD",
        "to_date":   "YYYY-MM-DD",
        "added":   [{"pubkey": "...", "domain": "..."|None}, ...],
        "dropped": [{"pubkey": "...", "domain": "..."|None}, ...],
      }
    """
    import db
    rows = db.read_recent_unl_snapshots(source, limit=2)
    if len(rows) < 2:
        return None
    newer, older = rows[0], rows[1]
    new_validators = (newer["payload"] or {}).get("validators") or []
    old_validators = (older["payload"] or {}).get("validators") or []
    new_by_pubkey = {v["pubkey"]: v for v in new_validators if v.get("pubkey")}
    old_by_pubkey = {v["pubkey"]: v for v in old_validators if v.get("pubkey")}
    added_keys = set(new_by_pubkey) - set(old_by_pubkey)
    dropped_keys = set(old_by_pubkey) - set(new_by_pubkey)
    return {
        "from_date": str(older["snapshot_date"]),
        "to_date": str(newer["snapshot_date"]),
        "added": _sort_diff_entries(_diff_entry(new_by_pubkey[k]) for k in added_keys),
        "dropped": _sort_diff_entries(_diff_entry(old_by_pubkey[k]) for k in dropped_keys),
    }


def format_validator_for_display(entry):
    """Decorate a diff entry with base58 + truncated pubkey for the
    template. Base58 is the canonical display form across XRPL tooling
    (xrpscan, xrpl.org, validator manifests); we store hex for binary
    compactness and convert at render. Returns a new dict; input is
    not mutated. On encode failure (malformed hex), pubkey_base58 is
    None and the truncated form falls back to the raw hex string —
    truth-first: never fabricate a base58 pubkey from broken input."""
    out = dict(entry)
    pubkey_hex = entry.get("pubkey") or ""
    try:
        b58 = addresscodec.encode_node_public_key(bytes.fromhex(pubkey_hex))
        out["pubkey_base58"] = b58
        out["pubkey_truncated"] = (
            f"{b58[:8]}…{b58[-4:]}" if len(b58) > 12 else b58
        )
    except (ValueError, Exception):
        out["pubkey_base58"] = None
        out["pubkey_truncated"] = (
            f"{pubkey_hex[:8]}…{pubkey_hex[-4:]}"
            if len(pubkey_hex) > 12
            else pubkey_hex
        )
    return out


def build_unl_diff_view(source):
    """Template-friendly diff view for one UNL source. Returns one of:

      {"state": "no_data"}
        — zero snapshots in DB (first deploy, PG hiccup).

      {"state": "cold_start", "tracking_started": "YYYY-MM-DD"}
        — exactly one snapshot recorded; diff requires two.

      {"state": "diffed",
       "from_date": "...", "to_date": "...",
       "added":   [{...}, ...],
       "dropped": [{...}, ...],
       "entries": [{"change_type": "added"|"dropped", ...}, ...],
       "total_changes": N}
        — two or more snapshots; metadata-only changes (same pubkey,
          different domain) intentionally do not surface here.

    Two distinct empty-states ("cold_start" vs. "diffed" with
    total_changes==0) so the template can give honest, distinct copy
    for "we haven't seen enough yet" vs. "we've watched and nothing
    happened."
    """
    import db
    diff = compute_unl_diff(source)
    if diff is not None:
        added = [format_validator_for_display(e) for e in diff["added"]]
        dropped = [format_validator_for_display(e) for e in diff["dropped"]]
        entries = (
            [{"change_type": "added", **e} for e in added]
            + [{"change_type": "dropped", **e} for e in dropped]
        )
        return {
            "state": "diffed",
            "from_date": diff["from_date"],
            "to_date": diff["to_date"],
            "added": added,
            "dropped": dropped,
            "entries": entries,
            "total_changes": len(entries),
        }
    rows = db.read_recent_unl_snapshots(source, limit=1)
    if not rows:
        return {"state": "no_data"}
    return {
        "state": "cold_start",
        "tracking_started": str(rows[0]["snapshot_date"]),
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
