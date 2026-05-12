"""
MPToken Issuance registry — walks every MPTokenIssuance ledger object,
decodes the XLS-89 metadata blob, classifies, shapes for /mpts.

Why this is a dedicated module instead of folding into token_data:
trust-line IOUs (`token_data.py`) and MPTs are completely different
ledger primitives. IOUs have no on-ledger metadata; MPTs carry a
JSON metadata blob right on the ledger. The retail story for each
is different ("legacy trust line currency" vs "next-gen token with
asset class + audits + issuer info baked in"), so they get separate
pages.

Activation: MPTokensV1 is already enabled on mainnet. Adoption is
sparse today — this page is partly a "ready for growth" play, same
as /lending was for activation.
"""

import binascii
import json
import os
import threading
import time

import httpx

XRPL_NODE = os.environ.get(
    "XRPL_MPT_NODE",
    os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234"),
)
CACHE_TTL = int(os.environ.get("MPT_DATA_CACHE_TTL", "1800"))  # 30 min
PAGE_LIMIT = 400

HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.environ.get(
    "MPT_SNAPSHOT_PATH",
    os.path.join(HERE, "mpt_snapshot.json"),
)
SNAPSHOT_MAX_AGE = int(os.environ.get("MPT_SNAPSHOT_MAX_AGE", "7200"))  # 2 hr

_cache_lock = threading.Lock()
_cache = {"fetched_at": 0.0, "data": None}


def _rpc(method, params, timeout=30.0):
    try:
        resp = httpx.post(
            XRPL_NODE,
            json={"method": method, "params": [params]},
            timeout=timeout,
        )
        return (resp.json() or {}).get("result") or {}
    except Exception:
        return None


def _decode_metadata(hex_blob):
    """XLS-89: MPTokenMetadata is UTF-8 JSON, hex-encoded on ledger.
    Returns the decoded dict on success, or {} when missing/malformed.
    Never raises — the registry should render even when metadata is junk."""
    if not hex_blob:
        return {}
    try:
        raw = binascii.unhexlify(hex_blob)
    except (binascii.Error, ValueError):
        return {"_raw_decode_error": "non-hex"}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"_raw_decode_error": "non-utf8"}
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        return {"_raw_decode_error": "not-an-object"}
    except json.JSONDecodeError:
        # Some issuers stuff plain text in here; preserve it as a name.
        return {"_raw_text": text[:200]}


# Asset-class normalization. XLS-89 doesn't strictly enforce a vocabulary,
# so issuers write whatever string they want. We map common variants to
# four buckets so the filter chips on /mpts are stable.
_RWA_TOKENS = {"rwa", "real-world-asset", "real_world_asset",
               "tokenized-asset", "tokenized_asset", "fund", "bond",
               "treasury", "real-estate", "real_estate", "commodity",
               "equity", "security", "loan", "credit", "invoice"}
_STABLE_TOKENS = {"stable", "stablecoin", "stable-coin", "stable_coin",
                  "fiat", "fiat-backed", "usd", "eur", "gbp"}
_UTILITY_TOKENS = {"utility", "gov", "governance", "lp", "reward",
                   "loyalty", "points", "access", "membership"}


def _meta_field(metadata, *keys):
    """Read the first non-empty value across a list of candidate keys.
    XLS-89 has both verbose (asset_class) and abbreviated (ac) forms in
    the wild — observed on mainnet today. We accept both."""
    for k in keys:
        v = metadata.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _classify(metadata):
    """Classify into RWA / Stablecoin / Utility / Other. Reads asset_class
    + asset_subclass + ticker + name; first match wins. Best-effort —
    issuers tag their own tokens, and many won't tag at all."""
    ac = str(_meta_field(metadata, "asset_class", "ac") or "").lower().strip()
    asub = str(_meta_field(metadata, "asset_subclass", "as") or "").lower().strip()
    ticker = str(_meta_field(metadata, "ticker", "symbol", "t") or "").lower()
    name = str(_meta_field(metadata, "name", "n") or "").lower()

    haystacks = (ac, asub, ticker, name)
    for h in haystacks:
        if not h:
            continue
        if h in _RWA_TOKENS or any(tok in h for tok in _RWA_TOKENS):
            return "rwa"
        if h in _STABLE_TOKENS or any(tok in h for tok in _STABLE_TOKENS):
            return "stablecoin"
        if h in _UTILITY_TOKENS or any(tok in h for tok in _UTILITY_TOKENS):
            return "utility"
    return "other"


def _amount_to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _shape_issuance(state):
    """Take a raw MPTokenIssuance ledger entry, return a row dict for
    the registry. Lossy by design — we don't pass the full ledger
    entry through to the template; we shape for display."""
    meta_hex = state.get("MPTokenMetadata") or ""
    meta = _decode_metadata(meta_hex)
    classification = _classify(meta)
    issuer = state.get("Issuer")
    seq = state.get("Sequence")
    # The mpt_issuance_id (the value used by mpt_holders RPC) is computed
    # as: hex(Sequence, big-endian 4 bytes) + hex(Issuer's accountID, 20 bytes).
    # The ledger object exposes it as `mpt_issuance_id` already on most
    # nodes — use that if present, else fall back to the ledger index.
    issuance_id = (state.get("mpt_issuance_id")
                   or state.get("MPTokenIssuanceID")
                   or state.get("index"))
    # XLS-89 canonical: `uris` is an array of {u, c, t} objects. Some early
    # issuers used `urls` (dict) before the spec landed — accept both shapes.
    uris_val = _meta_field(meta, "uris", "urls", "us")
    addl = _meta_field(meta, "additional_info", "ai")
    return {
        "issuance_id": issuance_id,
        "issuer": issuer,
        "sequence": seq,
        "name": _meta_field(meta, "name", "n") or meta.get("_raw_text") or None,
        "ticker": _meta_field(meta, "ticker", "symbol", "t"),
        "asset_class_raw": _meta_field(meta, "asset_class", "ac"),
        "asset_subclass": _meta_field(meta, "asset_subclass", "as"),
        "classification": classification,
        "issuer_name": _meta_field(meta, "issuer_name", "in"),
        "icon": _meta_field(meta, "icon", "i"),
        "uris": uris_val if isinstance(uris_val, (dict, list)) else None,
        "additional_info": addl if isinstance(addl, (dict, list)) else None,
        "desc": _meta_field(meta, "desc", "description", "d"),
        "outstanding_amount": _amount_to_int(state.get("OutstandingAmount")),
        "maximum_amount": _amount_to_int(state.get("MaximumAmount")),
        "asset_scale": _amount_to_int(state.get("AssetScale")),
        "transfer_fee": _amount_to_int(state.get("TransferFee")),
        "flags": _amount_to_int(state.get("Flags")),
        "metadata_present": bool(meta and "_raw_decode_error" not in meta),
        "metadata_error": meta.get("_raw_decode_error"),
    }


def _paginate_all(max_pages=2000, time_budget_secs=600):
    """Walk every MPTokenIssuance. Bounded by both page count and wall-clock
    so a malfunctioning node can't make us loop forever. Returns list."""
    t0 = time.time()
    out = []
    marker = None
    pages = 0
    while pages < max_pages and (time.time() - t0) < time_budget_secs:
        params = {
            "type": "mpt_issuance",
            "ledger_index": "validated",
            "limit": PAGE_LIMIT,
        }
        if marker is not None:
            params["marker"] = marker
        result = _rpc("ledger_data", params)
        if not result:
            return None
        for entry in result.get("state") or []:
            out.append(entry)
        pages += 1
        marker = result.get("marker")
        if not marker:
            break
    return out


def fetch_mpt_data(max_pages=2000, time_budget_secs=600):
    raw = _paginate_all(max_pages=max_pages, time_budget_secs=time_budget_secs)
    if raw is None:
        return {
            "ok": False,
            "node": XRPL_NODE,
            "issuances": [],
            "total": 0,
            "by_class": {"rwa": 0, "stablecoin": 0, "utility": 0, "other": 0},
        }
    rows = [_shape_issuance(s) for s in raw]
    rows.sort(key=lambda r: -(r["outstanding_amount"] or 0))

    by_class = {"rwa": 0, "stablecoin": 0, "utility": 0, "other": 0}
    for r in rows:
        by_class[r["classification"]] = by_class.get(r["classification"], 0) + 1

    issuers = {r["issuer"] for r in rows if r.get("issuer")}

    return {
        "ok": True,
        "node": XRPL_NODE,
        "issuances": rows,
        "total": len(rows),
        "unique_issuers": len(issuers),
        "by_class": by_class,
    }


def fetch_mpt_data_cached(ttl=None):
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < ttl:
            data = dict(_cache["data"])
            data["cached_age_seconds"] = round(now - _cache["fetched_at"], 1)
            return data
        fresh = fetch_mpt_data()
        if fresh.get("ok"):
            _cache["fetched_at"] = now
            _cache["data"] = fresh
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result


def load_mpt_snapshot(path=None, max_age=None):
    """Read the JSON written by mpt_snapshot.py. Returns None if missing,
    malformed, or stale beyond max_age."""
    path = path or SNAPSHOT_PATH
    max_age = max_age if max_age is not None else SNAPSHOT_MAX_AGE
    try:
        st = os.stat(path)
    except OSError:
        return None
    age = time.time() - st.st_mtime
    if age > max_age:
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data["snapshot_age_seconds"] = round(age, 1)
    data["from_snapshot"] = True
    return data


if __name__ == "__main__":
    t0 = time.time()
    d = fetch_mpt_data(time_budget_secs=600)
    print(f"node: {d['node']}")
    print(f"fetched in {time.time()-t0:.1f}s")
    print(f"total: {d['total']}  by_class={d['by_class']}")
    for r in d["issuances"][:10]:
        print(f"  {r.get('ticker') or '?':8s} {r.get('name') or '(unnamed)':30s} class={r['classification']}  issuer={r['issuer']}")
