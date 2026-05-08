"""
Daily snapshot — captures point-in-time state we can never back-fill cleanly.

What gets recorded each run:
  - Every account in named_accounts.json: XRP balance, sequence, owner count
  - Top-N AMM pools (default 200) by current TVL: reserves on both sides, LP balance
  - Snapshot metadata: UTC timestamp, validated ledger index, schema version,
    counts and skip reasons for any failures

Output: historical_snapshots/YYYY-MM-DD.json (one file per day, deterministic name —
re-runs on the same day overwrite, so a launchd retry after a transient failure
just produces a fresh snapshot for that day rather than duplicate files).

Why JSON files (not Postgres yet): cheap, inspectable, trivially migrate-able
once the post-launch storage decision is made. Disk cost is negligible
(~50KB per day uncompressed).

Run modes:
  python3 daily_snapshot.py              # full snapshot, write to disk
  python3 daily_snapshot.py --dry-run    # do everything, print summary, write nothing
  python3 daily_snapshot.py --top 50     # cap AMM pools at 50 (default 200)
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountInfo, AMMInfo, Ledger

XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
HERE = os.path.dirname(os.path.abspath(__file__))
NAMED_ACCOUNTS_PATH = os.path.join(HERE, "named_accounts.json")
AMM_RANKED_PATH = os.path.join(HERE, "amm_ranked.json")
SNAPSHOT_DIR = os.path.join(HERE, "historical_snapshots")
SCHEMA_VERSION = 1
DEFAULT_TOP_AMMS = 200


def _safe_request(client, request):
    try:
        resp = client.request(request)
        if "error" in resp.result:
            return None
        return resp.result
    except Exception:
        return None


def _validated_ledger_index(client):
    result = _safe_request(client, Ledger(ledger_index="validated"))
    if not result:
        return None
    return result.get("ledger_index") or (result.get("ledger") or {}).get("ledger_index")


def snapshot_accounts(client):
    """Balance + sequence + owner_count for every entry in named_accounts.json."""
    with open(NAMED_ACCOUNTS_PATH) as f:
        named = json.load(f) or {}

    rows = []
    for addr, meta in named.items():
        if not isinstance(meta, dict):
            continue
        result = _safe_request(client, AccountInfo(account=addr, ledger_index="validated"))
        if not result:
            rows.append({"address": addr, "name": meta.get("name"), "category": meta.get("category"), "error": "rpc_failed"})
            continue
        ad = result.get("account_data") or {}
        try:
            drops = int(ad.get("Balance", "0"))
        except (TypeError, ValueError):
            drops = 0
        rows.append({
            "address": addr,
            "name": meta.get("name"),
            "category": meta.get("category"),
            "balance_xrp": drops / 1_000_000,
            "balance_drops": drops,
            "sequence": ad.get("Sequence"),
            "owner_count": ad.get("OwnerCount", 0),
        })
    return rows


def _amm_amount_repr(amount):
    """AMMInfo.amount is either a drops-string (XRP) or {value, currency, issuer}."""
    if isinstance(amount, str):
        try:
            return {"currency": "XRP", "value": int(amount) / 1_000_000}
        except (TypeError, ValueError):
            return {"currency": "XRP", "value": 0}
    if isinstance(amount, dict):
        try:
            value = float(amount.get("value", 0))
        except (TypeError, ValueError):
            value = 0.0
        return {
            "currency": amount.get("currency"),
            "issuer": amount.get("issuer"),
            "value": value,
        }
    return None


def snapshot_amm_pools(client, top_n):
    """Top-N pools (by TVL from amm_ranked.json) — current reserves and LP balance."""
    with open(AMM_RANKED_PATH) as f:
        ranked = json.load(f) or []

    sortable = [p for p in ranked if isinstance(p, dict) and p.get("amm_account")]
    sortable.sort(key=lambda p: -(p.get("tvl_usd") or 0))
    pools = sortable[:top_n]

    rows = []
    for p in pools:
        result = _safe_request(client, AMMInfo(amm_account=p["amm_account"]))
        if not result:
            rows.append({"amm_account": p["amm_account"], "pair": p.get("pair"), "error": "rpc_failed"})
            continue
        amm = result.get("amm") or {}
        rows.append({
            "amm_account": p["amm_account"],
            "pair": p.get("pair"),
            "tvl_usd_at_rank": p.get("tvl_usd"),
            "amount_a": _amm_amount_repr(amm.get("amount")),
            "amount_b": _amm_amount_repr(amm.get("amount2")),
            "lp_token": _amm_amount_repr(amm.get("lp_token")),
            "trading_fee": amm.get("trading_fee"),
        })
    return rows


def build_snapshot(top_amms):
    client = JsonRpcClient(XRPL_NODE)
    started_at = time.time()
    ledger_index = _validated_ledger_index(client)

    accounts = snapshot_accounts(client)
    pools = snapshot_amm_pools(client, top_amms)

    elapsed = round(time.time() - started_at, 2)
    now_utc = dt.datetime.now(dt.UTC)
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_date_utc": now_utc.strftime("%Y-%m-%d"),
        "snapshot_taken_utc": now_utc.replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
        "ledger_index": ledger_index,
        "node": XRPL_NODE,
        "elapsed_seconds": elapsed,
        "accounts": accounts,
        "amm_pools_top_n": top_amms,
        "amm_pools": pools,
    }


def write_snapshot(snap):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"{snap['snapshot_date_utc']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def summarize(snap):
    accts = snap["accounts"]
    pools = snap["amm_pools"]
    acct_ok = sum(1 for a in accts if "error" not in a)
    pool_ok = sum(1 for p in pools if "error" not in p)
    total_xrp = sum((a.get("balance_xrp") or 0) for a in accts if "error" not in a)
    return (
        f"ledger={snap.get('ledger_index')} "
        f"accounts={acct_ok}/{len(accts)} "
        f"pools={pool_ok}/{len(pools)} "
        f"watchlist_xrp_total={total_xrp:,.0f} "
        f"elapsed={snap['elapsed_seconds']}s"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Do everything but don't write the file")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_AMMS, help=f"Top-N AMM pools by TVL (default {DEFAULT_TOP_AMMS})")
    args = parser.parse_args()

    snap = build_snapshot(top_amms=args.top)
    print(summarize(snap))

    if args.dry_run:
        print("(dry-run: nothing written)")
        return 0

    path = write_snapshot(snap)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
