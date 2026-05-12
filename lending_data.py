"""
XLS-66 Loan Broker / Vault / Loan data fetcher.

Pulls LoanBroker, Vault, and Loan ledger objects via `ledger_data` type
filters, then joins them into per-broker rows for the /lending page.

Dormant by default: if the amendments aren't activated on the node we
query, returns {"ok": True, "activated": False, "brokers": []}. The
template's awaiting state renders from that. Once activation flips,
the same code path returns real rows on the next 5-min refresh.

Node selection: XRPL_LENDING_NODE wins, falls back to XRPL_NODE, then
mainnet. Set XRPL_LENDING_NODE=https://s.devnet.rippletest.net:51234 to
exercise the live-data branch today (devnet has both amendments enabled).
"""

import json
import os
import threading
import time

import httpx

from lending_amendment import fetch_lending_status_cached

HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.environ.get(
    "LENDING_SNAPSHOT_PATH",
    os.path.join(HERE, "lending_snapshot.json"),
)
SNAPSHOT_MAX_AGE = int(os.environ.get("LENDING_SNAPSHOT_MAX_AGE", "1800"))  # 30 min

XRPL_NODE = os.environ.get(
    "XRPL_LENDING_NODE",
    os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234"),
)
CACHE_TTL = int(os.environ.get("LENDING_DATA_CACHE_TTL", "300"))
PAGE_LIMIT = 400

_cache_lock = threading.Lock()
_cache = {"fetched_at": 0.0, "data": None}


def _rpc(method, params):
    try:
        resp = httpx.post(
            XRPL_NODE,
            json={"method": method, "params": [params]},
            timeout=20.0,
        )
        return (resp.json() or {}).get("result") or {}
    except Exception:
        return None


def _paginate_objects(obj_type):
    """Walk every ledger object of a given type. Returns list (may be empty)."""
    out = []
    marker = None
    for _ in range(200):  # hard ceiling; brokers/vaults won't approach this
        params = {
            "type": obj_type,
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
        marker = result.get("marker")
        if not marker:
            break
    return out


def _to_xrp_or_iou(asset_field):
    """Vault Asset is either 'XRP' or {currency, issuer}. Normalize to a label."""
    if isinstance(asset_field, str):
        if asset_field.upper() == "XRP":
            return {"is_xrp": True, "currency": "XRP", "issuer": None, "label": "XRP"}
    if isinstance(asset_field, dict):
        cur = asset_field.get("currency")
        iss = asset_field.get("issuer")
        if cur:
            label = cur if not iss else f"{cur} · {iss[:6]}…{iss[-4:]}"
            return {"is_xrp": False, "currency": cur, "issuer": iss, "label": label}
    return {"is_xrp": False, "currency": None, "issuer": None, "label": "?"}


def _amount_to_float(amount):
    """LoanBroker / Loan amounts: drops string if XRP, {value,...} if IOU."""
    if amount is None:
        return 0.0
    if isinstance(amount, str):
        try:
            return int(amount) / 1_000_000  # drops → XRP
        except (TypeError, ValueError):
            return 0.0
    if isinstance(amount, dict):
        try:
            return float(amount.get("value") or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _vault_assets_total(vault):
    """Vault.AssetsTotal: drops string when XRP, {value,...} when IOU."""
    return _amount_to_float(vault.get("AssetsTotal"))


def _is_defaulted(loan, now_ripple_epoch):
    """Loan default heuristic: now > NextPaymentDueDate + GracePeriod.
    Ripple epoch is seconds since 2000-01-01 UTC."""
    try:
        due = int(loan.get("NextPaymentDueDate") or 0)
        grace = int(loan.get("GracePeriod") or 0)
    except (TypeError, ValueError):
        return False
    if due <= 0:
        return False
    return now_ripple_epoch > (due + grace)


def _ripple_epoch_now():
    # XRPL epoch starts 2000-01-01 UTC = unix 946684800
    return int(time.time() - 946684800)


def fetch_lending_data():
    status = fetch_lending_status_cached()
    activated = bool(status.get("activated"))
    if not activated:
        return {
            "ok": True,
            "activated": False,
            "node": XRPL_NODE,
            "brokers": [],
            "broker_count": 0,
            "vault_count": 0,
            "loan_count": 0,
            "total_tvl_xrp_equiv": 0.0,
            "status": status,
        }

    brokers_raw = _paginate_objects("loan_broker")
    vaults_raw = _paginate_objects("vault")
    loans_raw = _paginate_objects("loan")
    if brokers_raw is None or vaults_raw is None or loans_raw is None:
        return {
            "ok": False,
            "activated": True,
            "node": XRPL_NODE,
            "brokers": [],
            "broker_count": 0,
            "vault_count": 0,
            "loan_count": 0,
            "total_tvl_xrp_equiv": 0.0,
            "status": status,
        }

    vaults_by_id = {(v.get("index") or v.get("LedgerIndex")): v for v in vaults_raw}

    now_re = _ripple_epoch_now()
    loans_by_broker = {}
    for loan in loans_raw:
        bid = loan.get("LoanBrokerID")
        if not bid:
            continue
        loans_by_broker.setdefault(bid, []).append(loan)

    rows = []
    for b in brokers_raw:
        bid = b.get("index") or b.get("LedgerIndex")
        vault_id = b.get("VaultID")
        vault = vaults_by_id.get(vault_id) or {}
        asset = _to_xrp_or_iou(vault.get("Asset"))
        tvl = _vault_assets_total(vault) if vault else 0.0
        broker_loans = loans_by_broker.get(bid, [])
        outstanding = sum(_amount_to_float(l.get("PrincipalOutstanding")) for l in broker_loans)
        defaulted = sum(1 for l in broker_loans if _is_defaulted(l, now_re))

        # ManagementFeeRate / InterestRate are basis-point-style integers
        # serialized as decimal strings; XRPL elides zero-valued fields.
        def _bps(v):
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        rows.append({
            "broker_id": bid,
            "broker_account": b.get("Account"),
            "owner": b.get("Owner"),
            "vault_id": vault_id,
            "vault_asset": asset,
            "vault_tvl": tvl,
            "loan_count": len(broker_loans),
            "outstanding_principal": outstanding,
            "defaulted_count": defaulted,
            "management_fee_rate_bps": _bps(b.get("ManagementFeeRate")),
            "cover_available": _amount_to_float(b.get("CoverAvailable")),
            "debt_total": _amount_to_float(b.get("DebtTotal")),
            "share_mpt_id": vault.get("ShareMPTID") if vault else None,
        })

    rows.sort(key=lambda r: -(r["vault_tvl"] or 0))

    xrp_tvl = sum(r["vault_tvl"] for r in rows if r["vault_asset"]["is_xrp"])

    return {
        "ok": True,
        "activated": True,
        "node": XRPL_NODE,
        "brokers": rows,
        "broker_count": len(rows),
        "vault_count": len(vaults_raw),
        "loan_count": len(loans_raw),
        "total_tvl_xrp_equiv": xrp_tvl,  # XRP-leg only; IOU pricing left for later
        "status": status,
    }


def fetch_lending_data_cached(ttl=None):
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < ttl:
            data = dict(_cache["data"])
            data["cached_age_seconds"] = round(now - _cache["fetched_at"], 1)
            return data
        fresh = fetch_lending_data()
        if fresh.get("ok"):
            _cache["fetched_at"] = now
            _cache["data"] = fresh
        result = dict(fresh)
        result["cached_age_seconds"] = 0.0
        return result


def load_lending_snapshot(path=None, max_age=None):
    """Read the JSON written by lending_snapshot.py. Returns None if the
    file is missing, malformed, or stale beyond max_age — the route then
    falls back to the in-process fetcher."""
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
    d = fetch_lending_data()
    print(f"node: {d['node']}")
    print(f"fetched in {time.time()-t0:.1f}s")
    print(f"activated: {d['activated']} ok: {d['ok']}")
    print(f"brokers: {d['broker_count']}  vaults: {d['vault_count']}  loans: {d['loan_count']}")
    print(f"XRP-leg TVL: {d['total_tvl_xrp_equiv']:,.2f} XRP")
    for r in d["brokers"][:5]:
        print(
            f"  broker={r['broker_account']}  asset={r['vault_asset']['label']}  "
            f"tvl={r['vault_tvl']:,.2f}  loans={r['loan_count']}  "
            f"outstanding={r['outstanding_principal']:,.2f}  defaulted={r['defaulted_count']}"
        )
