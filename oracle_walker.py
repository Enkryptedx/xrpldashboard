"""Standalone one-shot walker for the /price-data explainer.

Invoked by com.charliebruce.xrpldashboard.oracle_walker.plist on a
30-min cadence. Each run iterates the tracked oracle cohort
(category=oracle in named_accounts.json — DIA today) via
account_objects(type=oracle) through xrpl_client.get_client (local
rippled primary, s1/s2 fallback), decodes hex-encoded Provider/URI/
AssetClass + hex-encoded AssetPrice values into human-readable pairs,
and replaces the oracles_snapshot table in Postgres.

The /price-data route reads through oracle_snapshot.py (5-min TTL
cache over the Postgres table). It never touches the XRPL node
directly for the page render.

Curated-cohort scope is deliberate. A one-time 2026-07-02 full ledger
walk (25 min, ~10% of state) found 4 non-DIA oracle owners, all
hobby-flavor (NexusESP32 microcontroller feed, threexrp, ripitlabs,
Ctrl Alt). None publish AccountRoot Domain. DIA (rP24Lp7bcUHvEW7T7c8xkxtQKKd9fZyra7,
provider='diadata') is the one production PriceOracle currently
publishing to mainnet. New production providers get added by dropping
them in named_accounts.json with category='oracle' — walker picks
them up next cycle. XRPLWin's directory carries the full raw
inventory (link out from template).

Band Protocol: press-claimed for XRPL but not observed in the walk
sample. Template auto-shows "announced, not observed on-ledger"
until a Band-tagged owner surfaces (TokenEscrow-footnote pattern).
"""

import logging
import sys

from xrpl.models.requests import AccountObjects

import db
from cold_storage import _load_named
from xrpl_client import get_client

# Mirrors launchd/com.charliebruce.xrpldashboard.oracle_walker.plist
# StartInterval. Read by /walker_health for per-row staleness thresholds.
WALKER_CADENCE_SECONDS = 1800

logger = logging.getLogger("oracle_walker")


def _decode_hex(h):
    """XRPL text-blob fields (Provider, URI, AssetClass) round-trip as
    ASCII/UTF-8 hex. Bad hex or bytes that don't decode → None so the
    template can render 'unknown' rather than a garbled string."""
    if not h:
        return None
    try:
        return bytes.fromhex(h).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _price_to_float(price_hex, scale):
    """XLS-47 encodes AssetPrice as a hex integer with a companion Scale
    field indicating decimal places: real_value = int(hex,16) / 10**Scale.
    Either field may be absent for uninitialized/placeholder pairs
    (observed in DIA's second/stale oracle). Returns None in that case
    so the template can flag the pair as pending."""
    if price_hex is None or scale is None:
        return None
    try:
        raw = int(price_hex, 16)
    except (TypeError, ValueError):
        return None
    try:
        s = int(scale)
    except (TypeError, ValueError):
        return None
    return raw / (10 ** s)


def _tracked_cohort():
    """Return [(addr, meta)] for category='oracle' in named_accounts.
    v1 = DIA only. New production providers added by editing the file."""
    named = _load_named()
    return [
        (addr, meta)
        for addr, meta in named.items()
        if isinstance(meta, dict) and meta.get("category") == "oracle"
    ]


def _scan_account(client, address):
    """Paginate account_objects(type=oracle) for one account. Returns list
    of raw Oracle objects, or None on RPC error (caller soft-skips the
    account and increments err count for walker_health)."""
    out, marker = [], None
    while True:
        req = AccountObjects(
            account=address,
            type="oracle",
            limit=200,
            marker=marker,
            ledger_index="validated",
        )
        try:
            resp = client.request(req)
        except Exception as e:
            logger.warning("account_objects failed account=%s err=%s", address, e)
            return None
        result = resp.result or {}
        if result.get("error"):
            logger.warning("account_objects rpc-error account=%s err=%s",
                           address, result.get("error"))
            return None
        out.extend(result.get("account_objects") or [])
        marker = result.get("marker")
        if not marker:
            break
    return out


def _build_row(obj, addr, meta):
    """Shape one raw Oracle ledger object into a db.replace_oracles_snapshot
    row. All hex fields decoded here so the reader/template don't touch
    hex; every price pair has its float pre-computed."""
    ledger_hash = obj.get("index")
    if not ledger_hash:
        return None
    provider = _decode_hex(obj.get("Provider"))
    uri = _decode_hex(obj.get("URI"))
    asset_class = _decode_hex(obj.get("AssetClass"))

    pairs_raw = obj.get("PriceDataSeries") or []
    pairs = []
    for wrapper in pairs_raw:
        pd = wrapper.get("PriceData") or {}
        pairs.append({
            "base": pd.get("BaseAsset"),
            "quote": pd.get("QuoteAsset"),
            "price_raw_hex": pd.get("AssetPrice"),
            "scale": pd.get("Scale"),
            "price_float": _price_to_float(pd.get("AssetPrice"), pd.get("Scale")),
        })

    return {
        "ledger_index_hash": ledger_hash,
        "owner": addr,
        "owner_name": meta.get("name"),
        "document_id": obj.get("OracleDocumentID"),
        "provider": provider,
        "uri": uri,
        "asset_class": asset_class,
        "last_update_time": obj.get("LastUpdateTime"),
        "price_data_json": pairs,
        "pair_count": len(pairs),
        "previous_txn_id": obj.get("PreviousTxnID"),
        "previous_txn_lgr_seq": obj.get("PreviousTxnLgrSeq"),
    }


def collect_snapshot():
    """Iterate the tracked cohort, produce (rows, snapshot_ledger_index,
    stats). Rows are shaped for db.replace_oracles_snapshot."""
    cohort = _tracked_cohort()
    client = get_client("oracle_walker")

    rows = []
    ok = err = 0
    snap_ledger = None
    for addr, meta in cohort:
        objs = _scan_account(client, addr)
        if objs is None:
            err += 1
            continue
        ok += 1
        for obj in objs:
            row = _build_row(obj, addr, meta)
            if row is None:
                logger.warning("oracle object missing 'index' account=%s", addr)
                continue
            rows.append(row)
    return rows, snap_ledger, {"ok": ok, "err": err, "cohort_size": len(cohort)}


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start("oracle_walker",
                                 cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = None
    try:
        rows, snap_ledger, stats = collect_snapshot()
        if stats["cohort_size"] == 0:
            message = "empty_cohort — no category=oracle in named_accounts"
            logger.error(message)
        elif stats["ok"] == 0:
            # Every account failed — don't clobber the previous snapshot
            # with an empty one. Same soft-fail as escrow_walker.
            message = (f"all_accounts_failed "
                       f"cohort_size={stats['cohort_size']}")
            logger.error("no accounts responded; leaving previous snapshot")
        else:
            wrote = db.replace_oracles_snapshot(rows, snap_ledger)
            if wrote:
                ok = True
                message = (f"rows={len(rows)} "
                           f"accounts_ok={stats['ok']}/{stats['cohort_size']} "
                           f"accounts_err={stats['err']}")
                logger.info(message)
            else:
                message = "pg_write_failed"
                logger.error("replace_oracles_snapshot returned False")
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end("oracle_walker", ok=ok, message=message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
