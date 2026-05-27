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
import sys
import threading
import time

import httpx

XRPL_NODE = os.environ.get(
    "XRPL_MPT_NODE",
    os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234"),
)
CACHE_TTL = int(os.environ.get("MPT_DATA_CACHE_TTL", "1800"))  # 30 min
PAGE_LIMIT = 400

# Walker retry policy. The full SHAMap walk takes ~5 hours and ~74k pages;
# a single transient hiccup anywhere in the walk used to abort with ok=False
# and silently no-op the PG write — 11 of 13 walks failed silently for
# ~2 weeks before being caught. Per-page retries handle one-off blips;
# the cumulative budget keeps a heavy load-shedding day from compounding
# into a 15+ hour walk that never finishes.
RPC_RETRY_BACKOFFS_SECS = (1, 2, 4, 8)  # 4 attempts = 15s max per page
WALK_RETRY_BUDGET_SECS = int(os.environ.get("MPT_WALK_RETRY_BUDGET_SECS", "1800"))  # 30 min

HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.environ.get(
    "MPT_SNAPSHOT_PATH",
    os.path.join(HERE, "mpt_snapshot.json"),
)
SNAPSHOT_MAX_AGE = int(os.environ.get("MPT_SNAPSHOT_MAX_AGE", "93600"))  # 26 hr (daily walk + slack)

_cache_lock = threading.Lock()
_cache = {"fetched_at": 0.0, "data": None}


def _rpc(method, params, timeout=30.0):
    """Single-shot RPC. Returns the result dict (possibly with an "error"
    key), or None on exception / malformed JSON. Empty result body and
    error responses are returned verbatim — the caller decides what is
    transient. _rpc_retry wraps this for the walk path."""
    try:
        resp = httpx.post(
            XRPL_NODE,
            json={"method": method, "params": [params]},
            timeout=timeout,
        )
        body = resp.json() or {}
        result = body.get("result")
        return result if isinstance(result, dict) else {}
    except Exception:
        return None


# Transient = retry. Empty/None result body is the load-shedding signature
# we caught s1 returning. slowDown / noNetwork are explicit "back off"
# signals. Any other error (e.g. invalidParams) is a real bug — don't retry.
_TRANSIENT_RPC_ERRORS = {"slowDown", "noNetwork", "noCurrent", "tooBusy"}


def _is_transient_rpc(result):
    if result is None:
        return True
    if not result:  # empty dict {} — observed silent-load-shed shape
        return True
    return result.get("error") in _TRANSIENT_RPC_ERRORS


def _rpc_retry(method, params, retry_tracker, page_n=None, timeout=30.0):
    """Wrap _rpc with exponential backoff. Returns:
      - dict (success — possibly with non-transient error key the caller handles)
      - None when retries exhausted
      - "budget_exhausted" sentinel when the per-walk cumulative budget hits
        before all attempts complete; caller treats this as a partial-result
        exit (don't keep retrying forever on a heavy load-shedding day)
    retry_tracker is a mutable dict {"seconds": float} updated in place."""
    last_reason = "?"
    for attempt_idx, backoff in enumerate(RPC_RETRY_BACKOFFS_SECS):
        result = _rpc(method, params, timeout=timeout)
        if not _is_transient_rpc(result):
            return result
        # Identify what we're retrying for the log line.
        if result is None:
            last_reason = "exception_or_timeout"
        elif not result:
            last_reason = "empty_result_body"
        else:
            last_reason = result.get("error") or "unknown_error"
        is_last_attempt = attempt_idx == len(RPC_RETRY_BACKOFFS_SECS) - 1
        if is_last_attempt:
            sys.stderr.write(
                f"[mpt_walk_retry] page={page_n} attempt={attempt_idx + 1} "
                f"reason={last_reason} exhausted\n"
            )
            sys.stderr.flush()
            return None
        # Budget gate: don't sleep into oblivion when s1 is in heavy
        # load-shedding mode and 5%+ of pages fail.
        if retry_tracker["seconds"] + backoff > WALK_RETRY_BUDGET_SECS:
            sys.stderr.write(
                f"[mpt_walk_retry] page={page_n} attempt={attempt_idx + 1} "
                f"reason={last_reason} budget_exhausted "
                f"(cumulative={round(retry_tracker['seconds'], 1)}s)\n"
            )
            sys.stderr.flush()
            return "budget_exhausted"
        sys.stderr.write(
            f"[mpt_walk_retry] page={page_n} attempt={attempt_idx + 1} "
            f"reason={last_reason} sleeping={backoff}s\n"
        )
        sys.stderr.flush()
        time.sleep(backoff)
        retry_tracker["seconds"] += backoff
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


def _paginate_all(max_pages=10_000_000, time_budget_secs=86400):
    """Walk every MPTokenIssuance — and we mean every. ledger_data with a
    type filter still paginates through the FULL ledger object set, so a
    real mainnet walk processes tens of millions of entries and takes
    hours. A 600s/2000-page budget (the old defaults) sampled only ~5-10%
    of the ledger and gave us a floor, not the truth — fine for an MVP,
    misleading for a page that says "every MPT". Defaults now allow the
    walk to actually complete; caps stay only to guard against a wedged
    node that returns markers forever.

    Partial-result contract: if a transient RPC failure exhausts retries
    after we've already collected ≥1 page, we return what we have with
    `truncated=True` + an `abort_reason` string. Only pages==0 returns
    None (no salvage). This unblocks the "got 200 of 210 MPTs before
    transient hiccup" case from the historical silent-skip pattern."""
    t0 = time.time()
    out = []
    marker = None
    pages = 0
    walk_complete = False
    abort_reason = None
    retry_tracker = {"seconds": 0.0}
    while pages < max_pages and (time.time() - t0) < time_budget_secs:
        params = {
            "type": "mpt_issuance",
            "ledger_index": "validated",
            "limit": PAGE_LIMIT,
        }
        if marker is not None:
            params["marker"] = marker
        result = _rpc_retry("ledger_data", params, retry_tracker, page_n=pages)
        if result == "budget_exhausted":
            abort_reason = "retry_budget_exhausted"
            sys.stderr.write(
                f"[mpt_walk] retry budget exhausted after {pages} pages, "
                f"{round(retry_tracker['seconds'], 1)}s total retry time, "
                f"exiting with partial results\n"
            )
            sys.stderr.flush()
            break
        if not result:
            abort_reason = "rpc_failed_after_retries"
            sys.stderr.write(
                f"[mpt_walk] RPC failed after retries at page {pages}, "
                f"exiting with partial results\n"
            )
            sys.stderr.flush()
            break
        for entry in result.get("state") or []:
            out.append(entry)
        pages += 1
        marker = result.get("marker")
        if not marker:
            walk_complete = True
            break
    else:
        # while-else: loop exited via the while condition, not via break.
        # We hit max_pages or time_budget_secs without naturally exhausting
        # the marker chain.
        abort_reason = "page_or_time_budget_exhausted"

    if not walk_complete and pages == 0:
        # Nothing to salvage — fresh-start failure. Caller will treat as
        # full failure (ok=False) so the snapshot isn't overwritten with
        # an empty payload that masks the last-good PG row.
        return None

    return {
        "entries": out,
        "pages": pages,
        "truncated": not walk_complete,
        "elapsed_seconds": round(time.time() - t0, 1),
        "abort_reason": abort_reason,
        "cumulative_retry_seconds": round(retry_tracker["seconds"], 1),
    }


def fetch_one_issuance(issuance_id):
    """Fetch the current ledger state for a single MPTokenIssuance via
    `ledger_entry`. Cheap (one RPC, ~100-200ms) compared to a full ledger
    walk — used by the hourly holders-refresh job to pick up the current
    OutstandingAmount without re-walking the ledger.

    Returns a shaped row dict (same shape as fetch_mpt_data's issuances),
    or None on RPC failure / missing object. Callers MUST treat None as
    "skip this cycle, retry next hour" — never substitute yesterday's
    cached outstanding_amount, or every hourly mpt_supply_history row
    silently lies."""
    if not issuance_id:
        return None
    result = _rpc("ledger_entry", {
        "mpt_issuance": issuance_id,
        "ledger_index": "validated",
    }, timeout=15.0)
    if not result or "error" in (result or {}):
        return None
    node = result.get("node") or result.get("mpt_issuance")
    if not node:
        return None
    # ledger_entry doesn't always echo mpt_issuance_id on the node payload;
    # stamp the requested id so _shape_issuance produces a stable row.
    node.setdefault("mpt_issuance_id", issuance_id)
    try:
        return _shape_issuance(node)
    except Exception:
        return None


def fetch_mpt_data(max_pages=10_000_000, time_budget_secs=86400):
    raw = _paginate_all(max_pages=max_pages, time_budget_secs=time_budget_secs)
    if raw is None:
        # Fresh-start failure — zero pages walked, nothing to salvage.
        return {
            "ok": False,
            "node": XRPL_NODE,
            "issuances": [],
            "total": 0,
            "by_class": {"rwa": 0, "stablecoin": 0, "utility": 0, "other": 0},
            "walk_complete": False,
            "walk_partial": False,
            "walk_abort_reason": "fresh_start_rpc_failure",
            "walk_cumulative_retry_seconds": 0.0,
        }
    entries = raw["entries"]
    rows = [_shape_issuance(s) for s in entries]
    rows.sort(key=lambda r: -(r["outstanding_amount"] or 0))

    by_class = {"rwa": 0, "stablecoin": 0, "utility": 0, "other": 0}
    for r in rows:
        by_class[r["classification"]] = by_class.get(r["classification"], 0) + 1

    issuers = {r["issuer"] for r in rows if r.get("issuer")}

    walk_complete = not raw.get("truncated")
    total = len(rows)
    # ok=True when the walk completed naturally OR we got at least one
    # entry on a partial walk (worth writing — beats yesterday's snapshot
    # for the entries we did see). ok=False only when total==0 AND we
    # didn't complete — that's an empty-payload that would clobber last-good.
    ok = walk_complete or total > 0

    return {
        "ok": ok,
        "node": XRPL_NODE,
        "issuances": rows,
        "total": total,
        "unique_issuers": len(issuers),
        "by_class": by_class,
        "walk_complete": walk_complete,
        "walk_partial": bool(raw.get("truncated")),
        "walk_pages": raw.get("pages"),
        "walk_seconds": raw.get("elapsed_seconds"),
        "walk_abort_reason": raw.get("abort_reason"),
        "walk_cumulative_retry_seconds": raw.get("cumulative_retry_seconds", 0.0),
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
    d = fetch_mpt_data()
    print(f"node: {d['node']}")
    print(f"fetched in {time.time()-t0:.1f}s  walked={d.get('walk_pages')} pages  complete={d.get('walk_complete')}")
    print(f"total: {d['total']}  by_class={d['by_class']}")
    for r in d["issuances"][:20]:
        print(f"  {r.get('ticker') or '?':8s} {r.get('name') or '(unnamed)':30s} class={r['classification']}  issuer={r['issuer']}")
