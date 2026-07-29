"""
Fast probe: for every unique account in the census's 20-example token-escrow
list, pull all their escrow objects, filter to non-XRP, and record
PreviousTxnLgrSeq (creation ledger for unmodified Escrow objects — Escrow
objects aren't modified between EscrowCreate and EscrowFinish/Cancel).

Then look up ledger close_time to convert index -> date.

Output: mini-histogram of the sample's creation dates.
"""

import json
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

RIPPLED = "http://127.0.0.1:5005"
# Public full-history node for ledger close_time / tx lookups on ledgers
# older than local rippled's ~10k retention window.
RIPPLED_HISTORY = "https://s2.ripple.com:51234/"
CENSUS_FILE = Path(__file__).parent / "census_escrow_phase1c_2026-07-11.json"
# XRPL ledger epoch: 2000-01-01T00:00:00Z (946684800 unix)
LEDGER_EPOCH = 946684800


def rpc(method, params, endpoint=RIPPLED):
    payload = json.dumps({"method": method, "params": [params]}).encode()
    req = urllib.request.Request(
        endpoint, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def account_escrows(account):
    out, marker = [], None
    while True:
        params = {
            "account": account,
            "type": "escrow",
            "limit": 400,
            "ledger_index": "validated",
        }
        if marker:
            params["marker"] = marker
        d = rpc("account_objects", params)
        r = d.get("result", {})
        out.extend(r.get("account_objects") or [])
        marker = r.get("marker")
        if not marker:
            break
    return out


def ledger_close_time(ledger_index, cache):
    if ledger_index in cache:
        return cache[ledger_index]
    # Try local first (fast), fall back to public history node
    for endpoint in (RIPPLED, RIPPLED_HISTORY):
        try:
            d = rpc(
                "ledger",
                {"ledger_index": int(ledger_index), "transactions": False,
                 "expand": False},
                endpoint=endpoint,
            )
            r = d.get("result", {})
            if r.get("error"):
                continue
            close = (r.get("ledger") or {}).get("close_time")
            if close is not None:
                cache[ledger_index] = close
                return close
        except Exception:
            continue
    cache[ledger_index] = None
    return None


def main():
    report = json.loads(CENSUS_FILE.read_text())
    examples = report["token_escrow_examples"]
    print(f"Loaded {len(examples)} token-escrow examples from census.")

    unique_accounts = sorted({e["account"] for e in examples})
    print(f"Unique accounts in sample: {len(unique_accounts)}")

    all_token_escrows = []  # list of dicts
    per_account = {}
    for i, acct in enumerate(unique_accounts, 1):
        objs = account_escrows(acct)
        token_objs = [o for o in objs if not isinstance(o.get("Amount"), str)]
        per_account[acct] = {"total_escrows": len(objs), "token_escrows": len(token_objs)}
        for o in token_objs:
            all_token_escrows.append({
                "account": acct,
                "amount": o.get("Amount"),
                "destination": o.get("Destination"),
                "prev_txn_lgr_seq": o.get("PreviousTxnLgrSeq"),
                "prev_txn_id": o.get("PreviousTxnID"),
            })
        print(f"  [{i}/{len(unique_accounts)}] {acct}: "
              f"{len(objs)} escrows total, {len(token_objs)} token")
        time.sleep(0.1)

    print(f"\nTotal token escrows discovered from sample accounts: "
          f"{len(all_token_escrows)}")

    # Look up close_time for each unique creation ledger
    close_time_cache = {}
    seqs = sorted({e["prev_txn_lgr_seq"] for e in all_token_escrows
                   if e["prev_txn_lgr_seq"]})
    print(f"Looking up close_time for {len(seqs)} unique ledgers...")
    for s in seqs:
        ledger_close_time(s, close_time_cache)
        time.sleep(0.05)

    # Attach dates
    for e in all_token_escrows:
        seq = e["prev_txn_lgr_seq"]
        ct = close_time_cache.get(seq)
        if ct is not None:
            unix = ct + LEDGER_EPOCH
            e["created_at"] = datetime.fromtimestamp(unix, tz=timezone.utc).isoformat()
            e["created_month"] = e["created_at"][:7]
        else:
            e["created_at"] = None
            e["created_month"] = "unknown"

    # Histogram by month
    hist = Counter(e["created_month"] for e in all_token_escrows)

    # Bucket by amount subtype
    subtype = defaultdict(int)
    for e in all_token_escrows:
        amt = e["amount"] or {}
        if isinstance(amt, dict):
            if "mpt_issuance_id" in amt:
                subtype["MPT"] += 1
            else:
                subtype["IOU"] += 1

    out = {
        "probe_time": datetime.now(timezone.utc).isoformat(),
        "source_census": str(CENSUS_FILE.name),
        "sample_accounts_probed": len(unique_accounts),
        "token_escrows_discovered": len(all_token_escrows),
        "subtype_split": dict(subtype),
        "creation_month_histogram": dict(sorted(hist.items())),
        "per_account": per_account,
        "objects": all_token_escrows,
    }
    out_path = Path(__file__).parent / "token_escrow_creation_probe.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\n=== PROBE COMPLETE ===")
    print(f"Sample accounts probed: {len(unique_accounts)}")
    print(f"Token escrows discovered: {len(all_token_escrows)}")
    print(f"Subtype split: {dict(subtype)}")
    print(f"Creation month histogram:")
    for month, cnt in sorted(hist.items()):
        print(f"  {month}: {cnt}")
    print(f"Written to {out_path.name}")


if __name__ == "__main__":
    main()
