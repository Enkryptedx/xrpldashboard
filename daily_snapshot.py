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
from xrpl.models.requests import AccountInfo, AccountLines, AMMInfo, Ledger

XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
HERE = os.path.dirname(os.path.abspath(__file__))
NAMED_ACCOUNTS_PATH = os.path.join(HERE, "named_accounts.json")
AMM_RANKED_PATH = os.path.join(HERE, "amm_ranked.json")
SNAPSHOT_DIR = os.path.join(HERE, "historical_snapshots")
SCHEMA_VERSION = 3
WALKER_CADENCE_SECONDS = 86400  # run_daily_snapshot.sh called daily via launchd
DEFAULT_TOP_AMMS = 200
DEFAULT_TOP_MPTS = 200
MPT_SNAPSHOT_MAX_AGE_SECONDS = 86400


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


def _fetch_trust_lines(client, addr):
    """Return non-zero trust lines for `addr` as a list of dicts, or [] if
    account_lines fails or the account holds no IOUs. Zero balances are
    filtered as noise — an open trust line at 0 carries no signal worth
    persisting. LP tokens (40-char hex currency starting with '03') ARE
    kept; they're real holdings, just labeled by AMM pool hash."""
    result = _safe_request(client, AccountLines(account=addr, ledger_index="validated"))
    if not result:
        return []
    lines = result.get("lines") or []
    kept = []
    for ln in lines:
        try:
            balance = float(ln.get("balance", "0"))
        except (TypeError, ValueError):
            balance = 0.0
        if balance == 0:
            continue
        kept.append({
            "currency": ln.get("currency"),
            "issuer": ln.get("account"),  # counterparty issuer per account_lines spec
            "balance": ln.get("balance"),
            "limit": ln.get("limit"),
            "no_ripple": ln.get("no_ripple"),
        })
    return kept


def snapshot_accounts(client):
    """Balance + sequence + owner_count + non-zero trust lines for every
    entry in named_accounts.json."""
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
        trust_lines = _fetch_trust_lines(client, addr)
        rows.append({
            "address": addr,
            "name": meta.get("name"),
            "category": meta.get("category"),
            "balance_xrp": drops / 1_000_000,
            "balance_drops": drops,
            "sequence": ad.get("Sequence"),
            "owner_count": ad.get("OwnerCount", 0),
            "trust_lines": trust_lines,
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


def snapshot_mpts(top_n, snapshot_ts):
    """Project the hourly mpt_snapshot.json into a daily-archive subset.

    Composition over duplication: mpt_snapshot.py walks the ledger every
    hour, so re-walking here would just spend rippled bandwidth for data
    we already have on disk. We read the existing file, pick the top-N by
    outstanding supply, and pin each row with snapshot_ts so a single row
    pulled from the daily file still carries provenance.

    Returns (rows, meta_dict). Meta surfaces source freshness and any
    upstream failure so the archive records the reason for an empty
    MPT list instead of silently writing zero."""
    try:
        from mpt_data import load_mpt_snapshot
    except Exception as e:
        return [], {"error": f"mpt_data import failed: {type(e).__name__}"}

    payload = load_mpt_snapshot(max_age=MPT_SNAPSHOT_MAX_AGE_SECONDS)
    if not payload:
        return [], {"error": "mpt_snapshot_unavailable_or_stale"}

    rows_in = payload.get("issuances") or []
    rows_in = sorted(rows_in, key=lambda r: -(r.get("outstanding_amount") or 0))[:top_n]

    rows_out = []
    for r in rows_in:
        rows_out.append({
            "issuance_id": r.get("issuance_id"),
            "issuer": r.get("issuer"),
            "name": r.get("name"),
            "ticker": r.get("ticker"),
            "asset_class": r.get("classification"),
            "outstanding_amount": r.get("outstanding_amount"),
            "asset_scale": r.get("asset_scale"),
            "holders": r.get("holders"),
            "flags": r.get("flags"),
            "snapshot_ts": snapshot_ts,
        })

    return rows_out, {
        "source_age_seconds": payload.get("snapshot_age_seconds"),
        "source_total": payload.get("total"),
    }


def build_snapshot(top_amms, top_mpts):
    client = JsonRpcClient(XRPL_NODE)
    started_at = time.time()
    ledger_index = _validated_ledger_index(client)

    accounts = snapshot_accounts(client)
    pools = snapshot_amm_pools(client, top_amms)
    mpts, mpts_meta = snapshot_mpts(top_mpts, snapshot_ts=int(started_at))

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
        "mpts_top_n": top_mpts,
        "mpts": mpts,
        "mpts_meta": mpts_meta,
    }


def write_snapshot(snap):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"{snap['snapshot_date_utc']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def _rollup_meta_from_disk():
    """Compute the same meta dict /institutional reads from disk, so we can
    mirror it into Postgres after a successful local write. Keys must match
    _historical_snapshot_meta_from_disk() in app.py — the template reads
    those names directly. Returns None if nothing readable on disk."""
    try:
        files = sorted(f for f in os.listdir(SNAPSHOT_DIR) if f.endswith(".json"))
    except (FileNotFoundError, OSError):
        return None
    if not files:
        return None
    latest_path = os.path.join(SNAPSHOT_DIR, files[-1])
    try:
        with open(latest_path) as f:
            latest = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "first_date": files[0].replace(".json", ""),
        "days_collected": len(files),
        "accounts_tracked": len(latest.get("accounts") or []),
        "pools_tracked": len(latest.get("amm_pools") or []),
        "mpts_tracked": len(latest.get("mpts") or []),
    }


def mirror_meta_to_postgres():
    """Best-effort dual-write of the snapshot-strip rollup. Failures are
    silent — the local file is still on disk, and db.write_snapshot_meta()
    already logs and recycles the connection on its end."""
    try:
        import db as pgbridge
    except Exception:
        return None
    meta = _rollup_meta_from_disk()
    if not meta:
        return None
    pgbridge.write_snapshot_meta(meta)
    return meta


def mirror_accounts_to_postgres(snap):
    """Best-effort dual-write of per-account rows into Postgres so Render
    can query per-account XRP + trust-line history without the Mac's
    filesystem. Failures are silent on db.py's side; the JSON file is
    still on disk as the source of truth. Returns the row count written
    (best-effort estimate) or None when PG isn't available."""
    try:
        import db as pgbridge
    except Exception:
        return None
    rows = snap.get("accounts") or []
    if not rows:
        return 0
    pgbridge.write_account_snapshots(snap["snapshot_date_utc"], rows)
    return len(rows)


def summarize(snap):
    accts = snap["accounts"]
    pools = snap["amm_pools"]
    mpts = snap.get("mpts") or []
    acct_ok = sum(1 for a in accts if "error" not in a)
    pool_ok = sum(1 for p in pools if "error" not in p)
    total_xrp = sum((a.get("balance_xrp") or 0) for a in accts if "error" not in a)
    accts_with_tl = sum(1 for a in accts if a.get("trust_lines"))
    mpts_meta = snap.get("mpts_meta") or {}
    mpt_part = f"mpts={len(mpts)}"
    if mpts_meta.get("error"):
        mpt_part += f" (mpts_error={mpts_meta['error']})"
    return (
        f"ledger={snap.get('ledger_index')} "
        f"accounts={acct_ok}/{len(accts)} "
        f"with_trust_lines={accts_with_tl} "
        f"pools={pool_ok}/{len(pools)} "
        f"{mpt_part} "
        f"watchlist_xrp_total={total_xrp:,.0f} "
        f"elapsed={snap['elapsed_seconds']}s"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Do everything but don't write the file")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_AMMS, help=f"Top-N AMM pools by TVL (default {DEFAULT_TOP_AMMS})")
    parser.add_argument("--top-mpts", type=int, default=DEFAULT_TOP_MPTS, help=f"Top-N MPTs by outstanding supply (default {DEFAULT_TOP_MPTS})")
    args = parser.parse_args()

    _wh_db = None
    if not args.dry_run:
        try:
            import db as _wh_db_mod
            _wh_db = _wh_db_mod
            _wh_db.write_walker_health_start("daily_snapshot", cadence_seconds=WALKER_CADENCE_SECONDS)
        except Exception:
            _wh_db = None

    _ok = False
    _msg = "init"
    try:
        snap = build_snapshot(top_amms=args.top, top_mpts=args.top_mpts)
        print(summarize(snap))

        if args.dry_run:
            print("(dry-run: nothing written)")
            return 0

        path = write_snapshot(snap)
        print(f"wrote {path}")

        # Order: mirror pushes happen BEFORE _ok=True so a mirror raise
        # falls into the outer except with _ok still False. db.write_snapshot_meta
        # became fail-loud 2026-09-06 (raises via _log_err_and_raise); the
        # mirror path is not "best-effort" anymore — a silent-mirror-fail
        # means Render serves stale rollup data under a green walker_health
        # row (same shape as the is_bot canary incident, but on the
        # snapshot-strip rollup).
        meta = mirror_meta_to_postgres()
        if meta:
            print(f"mirrored meta to pg: days={meta['days_collected']} accounts={meta['accounts_tracked']} pools={meta['pools_tracked']} mpts={meta['mpts_tracked']}")

        accts_mirrored = mirror_accounts_to_postgres(snap)
        if accts_mirrored is not None:
            print(f"mirrored accounts to pg: rows={accts_mirrored}")

        _ok = True
        _msg = (f"wrote {os.path.basename(path)} "
                f"accounts={len(snap.get('accounts', []))} "
                f"pools={len(snap.get('amm_pools', []))} "
                f"mpts={len(snap.get('mpts') or [])} "
                f"elapsed={snap.get('elapsed_seconds')}s")
        return 0
    except Exception as e:
        _msg = f"{type(e).__name__}: {e}"
        raise
    finally:
        if _wh_db is not None:
            _wh_db.write_walker_health_end("daily_snapshot", ok=_ok, message=_msg)


if __name__ == "__main__":
    sys.exit(main())
