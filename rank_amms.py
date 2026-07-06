"""
Rank every AMM in amm_index.json by TVL.

For each indexed AMM (account ID + asset pair from the bootstrap scan),
hit AMMInfo on the XRP Ledger to get current reserves and trading fee,
compute TVL in USD, and write the ranked list to amm_ranked.json.

XRP-paired pools (77% of the index) get a precise TVL using XRP_USD_PRICE
plus any token-side USD peg from token_names.json. Token-token pools
(23%) are tagged tvl_status="non_xrp_pair" — TVL is best-effort if we
know both sides' pegs, otherwise None and they sort to the bottom.

Resumable: progress saved every SAVE_EVERY pools so a network blip or
Ctrl-C picks up where it left off. Same retry/backoff pattern as
scan_all_amms.py.

Usage:
    python rank_amms.py                # rank all 9,559
    python rank_amms.py --limit 100    # smoke test against first 100
    python rank_amms.py --rps 8        # bump pacing to 8 req/sec
    python rank_amms.py --reset        # discard prior progress, start fresh

Outputs (next to this script):
    amm_ranked.json        — list of ranked AMMs (sorted by TVL desc)
    amm_rank_state.json    — resume cursor + counters
    amm_rank.log           — progress log (tail -f to watch)
"""

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AMMInfo
from datetime import datetime, timezone
import json
import os
import sys
import time

import db

# Mirrors ~/Library/LaunchAgents/com.charliebruce.xrpldashboard.rank_amms.plist
# StartInterval. Read by /walker_health for per-row staleness thresholds.
WALKER_CADENCE_SECONDS = 3600

XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
XRP_USD_PRICE = 1.44   # mirrors amm_scan_pools.py — single source of truth later
DEFAULT_RPS = 5.0      # safe under public-node throttling
MAX_RETRIES = 6
BACKOFF_BASE = 2.0
SAVE_EVERY = 100       # pools between full ranked-file writes
LOG_EVERY = 25

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH   = os.path.join(HERE, "amm_index.json")
INCREMENTAL_PATH = os.path.join(HERE, "amm_index_incremental.json")
RANKED_PATH  = os.path.join(HERE, "amm_ranked.json")
STATE_PATH   = os.path.join(HERE, "amm_rank_state.json")
LOG_PATH     = os.path.join(HERE, "amm_rank.log")
TOKEN_NAMES_PATH = os.path.join(HERE, "token_names.json")

USD_PEGGED_CATEGORIES = {"stablecoin", "fiat"}


def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log(f"WARN failed to load {path}: {e} — starting fresh")
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=None)
    os.replace(tmp, path)


def load_token_pegs():
    """Return dict[(currency_hex, issuer)] -> {peg_usd, display, category}."""
    pegs = {}
    raw = load_json(TOKEN_NAMES_PATH, {})
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        key = (entry.get("currency_hex"), entry.get("issuer"))
        cat = entry.get("category")
        peg = 1.00 if cat in USD_PEGGED_CATEGORIES else None
        pegs[key] = {
            "peg_usd": peg,
            "display": entry.get("currency_display"),
            "category": cat,
        }
    return pegs


def load_verified_brands():
    """Return dict[currency_hex] -> set of TOML-attested issuers.

    A currency_hex is a 'protected brand' if any token_names.json entry
    for it has verified_via pointing to an xrp-ledger.toml (or an
    xrpscan@ provenance tier). The display label decoded from the hex
    (e.g. 'RLUSD') is only trustworthy when the issuer is on the
    per-brand allowlist; any other issuer rendering that hex on /pools
    is currency-code spoofing the brand.

    Null-canonical brands: an entry with empty issuer (composite key
    '<hex>:') registers the brand with an empty allowlist, so every
    XRPL issuer of that currency code fails membership and flags. Used
    for tokens whose canonical issuance is off-XRPL (e.g. ONDO is
    Ethereum-only)."""
    brands = {}
    raw = load_json(TOKEN_NAMES_PATH, {})
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        ver = entry.get("verified_via") or ""
        if not any(tier in ver for tier in ("xrp-ledger.toml", "xrpscan@")):
            continue
        try:
            cur_hex, iss = key.split(":", 1)
        except ValueError:
            continue
        cur_hex = cur_hex.strip()
        iss = iss.strip()
        if not cur_hex:
            continue
        brands.setdefault(cur_hex, set())
        if iss:
            brands[cur_hex].add(iss)
    return brands


def decode_currency(currency):
    """XRPL token currency is either 3-char ASCII or 40-char hex.
    Hex codes carry an embedded ASCII name (or junk). Return a display
    string suitable for ranking UI."""
    if not currency or len(currency) <= 4:
        return currency or "?"
    if len(currency) != 40:
        return currency
    try:
        raw = bytes.fromhex(currency).rstrip(b"\x00")
        text = raw.decode("ascii").strip()
        if text and all(32 <= ord(c) < 127 for c in text):
            return text
    except (ValueError, UnicodeDecodeError):
        pass
    return currency[:8].upper()  # short hex if undecodable


def normalize_pair(asset, asset2, pegs, verified_brands=None):
    """Return (display_pair, asset_meta_a, asset_meta_b, kind).
    kind ∈ {'xrp_first', 'xrp_second', 'non_xrp'}.

    When verified_brands is provided, sides whose currency_hex matches a
    TOML-attested brand but whose issuer is not on the allowlist get
    unverified_brand=True so downstream UI can flag the spoof."""
    if verified_brands is None:
        verified_brands = {}

    def meta(side):
        cur = side.get("currency", "?")
        issuer = side.get("issuer")
        if cur == "XRP":
            return {"display": "XRP", "currency": "XRP", "issuer": None,
                    "peg_usd": XRP_USD_PRICE, "category": "native",
                    "unverified_brand": False}
        peg_entry = pegs.get((cur, issuer))
        if peg_entry:
            return {"display": peg_entry["display"], "currency": cur,
                    "issuer": issuer, "peg_usd": peg_entry["peg_usd"],
                    "category": peg_entry["category"],
                    "unverified_brand": False}
        unverified = (cur in verified_brands
                      and issuer not in verified_brands[cur])
        return {"display": decode_currency(cur), "currency": cur,
                "issuer": issuer, "peg_usd": None, "category": None,
                "unverified_brand": unverified}

    a = meta(asset)
    b = meta(asset2)
    if a["currency"] == "XRP":
        kind = "xrp_first"
    elif b["currency"] == "XRP":
        kind = "xrp_second"
    else:
        kind = "non_xrp"
    pair = f"{a['display']}/{b['display']}"
    return pair, a, b, kind


def amount_to_float(amt):
    """AMMInfo amount is either a drops-string (XRP) or {value,currency,issuer}."""
    if isinstance(amt, str):
        return int(amt) / 1_000_000.0  # drops -> XRP
    if isinstance(amt, dict):
        try:
            return float(amt.get("value", 0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def request_with_retry(client, request):
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.request(request)
            if "error" in resp.result:
                err = resp.result.get("error")
                if err == "actNotFound":
                    return None  # AMM was deleted; skip cleanly
                raise RuntimeError(f"node error: {err}")
            return resp
        except Exception as e:
            last_err = e
            wait = BACKOFF_BASE ** attempt
            time.sleep(wait)
    raise RuntimeError(f"exhausted retries: {last_err}")


def rank_one(client, amm_entry, pegs, verified_brands=None):
    """Return a ranked-pool dict, or None if the AMM couldn't be priced."""
    asset  = amm_entry.get("Asset", {})
    asset2 = amm_entry.get("Asset2", {})
    pair, meta_a, meta_b, kind = normalize_pair(
        asset, asset2, pegs, verified_brands
    )

    try:
        resp = request_with_retry(client, AMMInfo(asset=asset, asset2=asset2))
    except Exception as e:
        return {"pair": pair, "amm_account": amm_entry.get("Account"),
                "tvl_status": "error", "error": str(e)[:120],
                "kind": kind, "asset_a": meta_a, "asset_b": meta_b}

    if resp is None:
        return None  # actNotFound — pool gone, skip

    amm = resp.result.get("amm") or {}
    amt_a = amount_to_float(amm.get("amount"))
    amt_b = amount_to_float(amm.get("amount2"))
    fee_pct = amm.get("trading_fee", 0) / 1000.0
    account = amm.get("account") or amm_entry.get("Account")
    lp_token_raw = (amm.get("lp_token") or {}).get("value")
    try:
        lp_token_value = float(lp_token_raw) if lp_token_raw is not None else None
    except (TypeError, ValueError):
        lp_token_value = None

    # Compute TVL.
    a_usd = amt_a * meta_a["peg_usd"] if meta_a["peg_usd"] is not None else None
    b_usd = amt_b * meta_b["peg_usd"] if meta_b["peg_usd"] is not None else None

    tvl_status = "exact"
    if a_usd is not None and b_usd is not None:
        tvl_usd = a_usd + b_usd
    elif kind in ("xrp_first", "xrp_second"):
        # AMM curves are balanced 50/50 by value at equilibrium, so 2x XRP-side
        # is a fair best-effort TVL when the token has no peg.
        xrp_usd = a_usd if meta_a["currency"] == "XRP" else b_usd
        tvl_usd = (xrp_usd or 0) * 2
        tvl_status = "estimated"
    else:
        tvl_usd = None
        tvl_status = "non_xrp_pair"

    return {
        "pair": pair,
        "amm_account": account,
        "fee_pct": fee_pct,
        "fee_raw": amm.get("trading_fee", 0),
        "amount_a": amt_a, "amount_b": amt_b,
        "asset_a": meta_a, "asset_b": meta_b,
        "tvl_usd": tvl_usd,
        "tvl_status": tvl_status,
        "kind": kind,
        "lp_token_value": lp_token_value,
    }


def _amount_to_asset_spec(amt):
    """Convert an AMMCreate amount field into an AMMInfo Asset spec.
    String drops -> {"currency": "XRP"}; dict -> {"currency", "issuer"}."""
    if isinstance(amt, str):
        return {"currency": "XRP"}
    if isinstance(amt, dict) and amt.get("currency"):
        spec = {"currency": amt["currency"]}
        if amt.get("issuer"):
            spec["issuer"] = amt["issuer"]
        return spec
    return None


def _pair_key(asset, asset2):
    """Order-independent dedup key for an AMM pair."""
    def side(a):
        return (a.get("currency"), a.get("issuer"))
    return tuple(sorted([side(asset), side(asset2)]))


def merge_incremental(index):
    """Append new AMMs from amm_index_incremental.json (live AMMCreate
    captures by xrpl_stream.py) into the bootstrap index, deduped by pair."""
    incremental = load_json(INCREMENTAL_PATH, [])
    if not incremental:
        return index, 0
    seen = {_pair_key(e.get("Asset", {}), e.get("Asset2", {})) for e in index}
    added = 0
    for entry in incremental:
        a = _amount_to_asset_spec(entry.get("asset"))
        b = _amount_to_asset_spec(entry.get("asset2"))
        if not a or not b:
            continue
        key = _pair_key(a, b)
        if key in seen:
            continue
        seen.add(key)
        index.append({
            "Account": None,
            "Asset": a,
            "Asset2": b,
            "_source": "incremental",
            "_tx_hash": entry.get("tx_hash"),
        })
        added += 1
    return index, added


# Module-level counters so consecutive failures across SAVE_EVERY checkpoints
# within a single run can be detected and logged loudly. Reset by a success.
# Persistent cross-run state lives in the heartbeat row in Postgres — when
# THAT can't be written, the absence of a fresh heartbeat IS the prod signal.
_mirror_consec_failures = 0


def _mirror_to_postgres(ranked, state, indexed_count):
    """Push the current ranked snapshot + meta to Postgres so the prod
    Flask web (Render) sees the same data the Mac just wrote to disk.

    Silent no-op when DATABASE_URL isn't set; never raises (prod outage on
    Neon must not block local ranking from making progress).

    Tracks consecutive failures: a streak of MIRROR_FAILURE_THRESHOLD (default
    10) trips a loud log line so the operator scanning launchd logs sees it
    without grepping. Prod-side detection rides on heartbeat-row age via
    /health — when mirror writes fail, the heartbeat doesn't update, and
    /health flips to degraded after MIRROR_DEGRADED_AGE_SEC."""
    global _mirror_consec_failures
    threshold = int(os.environ.get("MIRROR_FAILURE_THRESHOLD", "10"))
    try:
        db.replace_amm_ranked_pools(ranked)
        db.write_heartbeat(
            "amm_ranker",
            txns_seen=len(ranked),
            extra={
                "indexed_count": indexed_count,
                "started_at": state.get("started_at"),
                "finished_at": state.get("finished_at"),
                "cursor": state.get("cursor"),
                "errors": state.get("errors"),
                "skipped": state.get("skipped"),
            },
        )
        if _mirror_consec_failures:
            log(f"  postgres mirror recovered after {_mirror_consec_failures} consecutive failure(s)")
        _mirror_consec_failures = 0
    except Exception as e:
        _mirror_consec_failures += 1
        if _mirror_consec_failures >= threshold:
            log(f"  MIRROR_HEALTH degraded — {_mirror_consec_failures} consecutive failures (threshold {threshold}); last error: {e}")
        else:
            log(f"  postgres mirror failed (non-fatal, streak={_mirror_consec_failures}): {e}")


def parse_args():
    args = {"limit": None, "rps": DEFAULT_RPS, "reset": False}
    a = sys.argv[1:]
    while a:
        flag = a.pop(0)
        if flag == "--limit":
            args["limit"] = int(a.pop(0))
        elif flag == "--rps":
            args["rps"] = float(a.pop(0))
        elif flag == "--reset":
            args["reset"] = True
        else:
            log(f"unknown flag: {flag}")
            sys.exit(2)
    return args


def main():
    args = parse_args()
    if args["reset"]:
        for p in (RANKED_PATH, STATE_PATH):
            if os.path.exists(p):
                os.remove(p); log(f"removed {p}")

    pegs = load_token_pegs()
    log(f"loaded {len(pegs)} token pegs from {os.path.basename(TOKEN_NAMES_PATH)}")
    verified_brands = load_verified_brands()
    log(f"loaded {len(verified_brands)} TOML-attested brand(s) "
        f"({sum(len(s) for s in verified_brands.values())} issuer(s))")

    index = load_json(INDEX_PATH, None)
    if index is None:
        log(f"FATAL: {INDEX_PATH} not found — run scan_all_amms.py first")
        return 1
    bootstrap_count = len(index)
    index, added = merge_incremental(index)
    if added:
        log(f"merged {added} live-captured AMM(s) from {os.path.basename(INCREMENTAL_PATH)} "
            f"(bootstrap={bootstrap_count}, total={len(index)})")
    if args["limit"]:
        index = index[: args["limit"]]
        log(f"--limit {args['limit']} active · ranking first {len(index)} AMMs")

    state = load_json(STATE_PATH, {
        "cursor": 0, "started_at": None, "finished_at": None,
        "errors": 0, "skipped": 0,
    })
    ranked = load_json(RANKED_PATH, [])

    if state.get("finished_at") and state.get("cursor", 0) >= len(index):
        log(f"already finished: {len(ranked)} ranked pools in {RANKED_PATH}")
        log("pass --reset to re-rank")
        return 0

    if state["started_at"] is None:
        state["started_at"] = datetime.now(timezone.utc).isoformat()
        log(f"start: {len(index)} AMMs · pacing={args['rps']} rps · node={XRPL_NODE}")
    else:
        log(f"resume: {state['cursor']}/{len(index)} done · {len(ranked)} ranked so far")

    client = JsonRpcClient(XRPL_NODE)
    delay = 1.0 / max(args["rps"], 0.1)
    t_run_start = time.time()
    n_run_start = state["cursor"]

    while state["cursor"] < len(index):
        i = state["cursor"]
        entry = index[i]
        t0 = time.time()
        try:
            row = rank_one(client, entry, pegs, verified_brands)
        except KeyboardInterrupt:
            log("interrupted — saving progress")
            save_json(RANKED_PATH, ranked)
            save_json(STATE_PATH, state)
            return 130
        except Exception as e:
            state["errors"] += 1
            log(f"  unexpected error on {entry.get('Account')}: {e}")
            row = None

        if row is not None:
            ranked.append(row)
        else:
            state["skipped"] += 1

        state["cursor"] += 1

        if state["cursor"] % LOG_EVERY == 0 or state["cursor"] == len(index):
            elapsed = time.time() - t_run_start
            done_run = state["cursor"] - n_run_start
            rate = done_run / elapsed if elapsed > 0 else 0
            eta = (len(index) - state["cursor"]) / rate if rate > 0 else 0
            log(f"progress {state['cursor']}/{len(index)} · "
                f"{rate:.1f}/s · eta {eta/60:.1f}m · "
                f"err={state['errors']} skip={state['skipped']}")

        if state["cursor"] % SAVE_EVERY == 0 or state["cursor"] == len(index):
            save_json(RANKED_PATH, ranked)
            save_json(STATE_PATH, state)
            _mirror_to_postgres(ranked, state, indexed_count=len(index))

        slept = time.time() - t0
        if slept < delay:
            time.sleep(delay - slept)

    # Final sort + finalize
    def sort_key(r):
        # Exact > estimated > non_xrp_pair > error; within group, TVL desc.
        order = {"exact": 0, "estimated": 1, "non_xrp_pair": 2, "error": 3}.get(
            r.get("tvl_status"), 4)
        return (order, -(r.get("tvl_usd") or 0))
    ranked.sort(key=sort_key)

    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    save_json(RANKED_PATH, ranked)
    save_json(STATE_PATH, state)
    _mirror_to_postgres(ranked, state, indexed_count=len(index))
    log(f"done: ranked={len(ranked)} · errors={state['errors']} skipped={state['skipped']}")

    # Derive XRP-equivalent token prices from the just-ranked in-memory
    # pool list and upsert to Postgres. Piggybacks here rather than
    # running on a separate launchd plist so the price snapshot is
    # always paired with the pool snapshot it was derived from — no
    # schedule drift, no "which one ran more recently" question.
    # Failures are isolated: a token_prices error never blocks the
    # ranker exit code.
    try:
        import token_prices
        written, dust, other = token_prices.run_once(
            pools=ranked,
            snapshot_ts=int(time.time()),
        )
        log(f"token_prices: written={written} dust_skipped={dust} "
            f"other_skipped={other}")
    except Exception as e:
        log(f"token_prices: derivation failed (non-fatal): {e}")

    return 0


if __name__ == "__main__":
    # Resumable walker: each launchd invocation either completes the full
    # pass or advances the cursor + saves state. main() returning 0 covers
    # both "completed this invocation" and the early-return "already
    # finished" no-op (line 430-433) — both are forward progress from the
    # walker_health reader's perspective. Cursor advance shows in message
    # so partial vs. full-pass is visible without flagging as failure.
    db.write_walker_health_start("rank_amms", cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = None
    rc = 1
    try:
        rc = main()
        ok = (rc == 0)
        try:
            st = load_json(STATE_PATH, {})
            message = (
                f"rc={rc} cursor={st.get('cursor')} "
                f"errors={st.get('errors')} skipped={st.get('skipped')} "
                f"finished_at={st.get('finished_at')}"
            )
        except Exception:
            message = f"rc={rc}"
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end("rank_amms", ok=ok, message=message)
    sys.exit(rc)
