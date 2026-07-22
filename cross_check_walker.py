"""Layer 3 (External Legitimacy) cross-check walker.

Truth Audit Phase 3 per docs/TRUTH_AUDIT_DESIGN.md (706bc0a §Layer 3).
Compares the values the site computes and serves against independent public
sources. A disagreement is an INVESTIGATION TRIGGER, never auto-correction
— "we might be the right one" is a real outcome (e.g. our local supply
figure is from a validated ledger and the external is stale).

Pairs evaluated
───────────────────────────────────────────────────────────────────
rlusd_eth_supply_vs_external     penny_exact  local cache vs eth.llamarpc.com
rlusd_xrpl_supply_vs_xrplcluster penny_exact  local cache vs xrplcluster.com
xrp_price_vs_coingecko           band ±2%     on-chain AMM vs CoinGecko
amendments_local_vs_mainnet      count_exact  localhost:5005 feature vs Amendments ledger
validator_unl_live_vs_stored     count_exact  live vl.ripple.com vs stored snapshot
ledger_vocab_local_vs_public     set_equal    local server_defs vs s1.ripple.com
───────────────────────────────────────────────────────────────────

Results land in cross_check_results (append-only). Alarm surface = rows
WHERE status = 'disagree'. walker_health.ok = False only on internal
exception or PG unreachability. External failures write
status='external_unreachable' and continue.

Cadence: 10 minutes (same CONTINUOUS bar as L2). Vocabulary and amendments
are stable so evaluating them at 10-minute cadence is cheap redundancy that
catches node-upgrade events within a cycle.
"""

from __future__ import annotations

import base64
import json
import logging
import sys
from typing import Any

import httpx

import db

WALKER_NAME = "cross_check_walker"
WALKER_CADENCE_SECONDS = 600  # 10 min

# ETH constants — match rlusd_live.py so we're comparing same token.
ETH_CONTRACT = "0x8292bb45bf1ee4d140127049757c2e0ff06317ed"
ETH_DECIMALS = 18
ETH_TOTAL_SUPPLY_SELECTOR = "0x18160ddd"

# XRPL RLUSD issuer — same as rlusd_live.XRPL_ISSUER.
XRPL_RLUSD_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
XRPL_CURRENCY_HEX = "524C555344000000000000000000000000000000"

# External endpoints — deliberately different from the primaries rlusd_live
# uses, so we're not just confirming the same node/RPC agrees with itself.
# Multiple fallbacks in case one is down; first that responds is used.
# ETH cross-check source list. The free public RPC landscape tightened in
# mid-2026 (ankr now requires API key; llamarpc flaky; cloudflare rejects
# eth_call for unknown contracts). Etherscan tokensupply is the most reliable
# keyless independent option but is rate-limited to 1 req/5s. If all fail
# the pair records external_unreachable and the audit notes the limitation.
# ETH cross-check source list — ordered by independence from our primary.
# rlusd_live uses publicnode.com first; we use everything else first.
# Mid-2026 free-RPC landscape: ankr requires key, llamarpc flaky,
# cloudflare rejects eth_call for some contracts, etherscan v1 deprecated.
# publicnode.com is listed LAST as a same-source fallback — still checks
# parse/format correctness but notes reduced independence.
ETH_XCHECK_RPCS = [
    "https://cloudflare-eth.com",
    "https://1rpc.io/eth",
    "https://eth.llamarpc.com",
    "https://ethereum-rpc.publicnode.com",  # same-source fallback
]
XRPL_XCHECK_NODE = "https://xrplcluster.com"          # independent from s1.ripple.com
XRPL_LOCAL_NODE = "http://localhost:5005"             # our node — used for vocab/amend checks
XRPL_PUBLIC_NODE = "https://s1.ripple.com:51234"      # mainnet reference (amendments ledger)
COINGECKO_PRICE_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=ripple&vs_currencies=usd&precision=6"
)
VL_RIPPLE_URL = "https://vl.ripple.com"
HTTP_TIMEOUT = 12

# Band tolerance: ±2% for XRP price.
XRP_PRICE_BAND_FRACTION = 0.02

# Penny-exact tolerance: $1 accounts for in-flight mints/burns between
# the local cache read and the external fetch, and for decimal rounding
# in either representation.
SUPPLY_PENNY_TOLERANCE = 1.0

logger = logging.getLogger(WALKER_NAME)


# ── HTTP helpers ─────────────────────────────────────────────────────────────
# Use httpx throughout — it bundles certifi so macOS Python cert issues don't
# affect cross-checks the way urllib.request does.

_HEADERS = {"User-Agent": "xrpldashboard/1.0 cross_check_walker"}


def _http_get(url: str, timeout: int = HTTP_TIMEOUT) -> Any:
    """GET url, return parsed JSON or raise."""
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url, headers=_HEADERS)
        resp.raise_for_status()
        return resp.json()


def _xrpl_post(node: str, method: str, params: dict,
                timeout: int = HTTP_TIMEOUT) -> dict | None:
    """POST a JSON-RPC call to an XRPL node; return result dict or None."""
    body = {"method": method, "params": [params]}
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(node, json=body, headers=_HEADERS)
        resp.raise_for_status()
        raw = resp.json()
    result = raw.get("result") or {}
    if result.get("status") == "error":
        logger.warning("XRPL RPC error %s: %s", method, result.get("error"))
        return None
    return result


def _eth_call(rpc: str, to: str, data: str,
              timeout: int = HTTP_TIMEOUT) -> str | None:
    """eth_call returning the hex result string, or None on error."""
    body = {
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(rpc, json=body, headers=_HEADERS)
        resp.raise_for_status()
        raw = resp.json()
    if "error" in raw:
        logger.warning("eth_call error: %s", raw["error"])
        return None
    return raw.get("result")


# ── Result writer ─────────────────────────────────────────────────────────────

def _record(pair_key: str, check_type: str, external_source: str,
            local_value: str | None, external_value: str | None,
            status: str, delta: float | None = None,
            tolerance: float | None = None, note: str | None = None) -> None:
    db.write_cross_check_result(
        pair_key=pair_key,
        check_type=check_type,
        local_value=local_value,
        external_value=external_value,
        external_source=external_source,
        tolerance=tolerance,
        delta=delta,
        status=status,
        note=note,
    )
    level = logging.WARNING if status == "disagree" else logging.INFO
    logger.log(level,
               "cross_check pair=%s status=%s local=%s ext=%s delta=%s note=%s",
               pair_key, status, local_value, external_value, delta, note)


# ── Pair 1: RLUSD-ETH supply ─────────────────────────────────────────────────

def _check_rlusd_eth_supply() -> None:
    PAIR = "rlusd_eth_supply_vs_external"

    payload, _ = db.read_rlusd_state_cache()
    if not payload:
        _record(PAIR, "penny_exact", ETH_XCHECK_RPCS[0], None, None,
                "local_unavailable", note="rlusd_state_cache empty")
        return
    eth = payload.get("eth") or {}
    if eth.get("error"):
        _record(PAIR, "penny_exact", ETH_XCHECK_RPCS[0], None, None,
                "local_unavailable",
                note=f"local eth sub-dict has error: {eth['error']}")
        return
    local_supply = float(eth.get("supply") or 0)

    # Try each endpoint in order; first success wins.
    hex_result = None
    used_rpc = None
    last_exc: Exception | None = None
    for rpc in ETH_XCHECK_RPCS:
        try:
            hex_result = _eth_call(rpc, ETH_CONTRACT, ETH_TOTAL_SUPPLY_SELECTOR)
            if hex_result is not None:
                used_rpc = rpc
                break
        except Exception as exc:
            last_exc = exc
            logger.info("eth_call %s failed: %s", rpc, exc)
            continue

    if hex_result is None:
        SRC = "all: " + ", ".join(ETH_XCHECK_RPCS)
        note = (
            f"all ETH RPCs failed; last: {type(last_exc).__name__}: {last_exc}. "
            "Structural: mid-2026 free public ETH RPCs require API keys or are "
            "unavailable. ETH supply cross-check blocked until an independent "
            "keyless source is available or an API key is added to env."
        )
        _record(PAIR, "penny_exact", SRC,
                str(round(local_supply, 2)), None,
                "external_unreachable", note=note)
        return

    try:
        raw_int = int(hex_result, 16)
        ext_supply = raw_int / (10 ** ETH_DECIMALS)
    except Exception as exc:
        _record(PAIR, "penny_exact", used_rpc,
                str(round(local_supply, 2)), None,
                "external_unreachable",
                note=f"hex decode failed: {type(exc).__name__}: {exc}")
        return

    delta = abs(local_supply - ext_supply)
    status = "agree" if delta <= SUPPLY_PENNY_TOLERANCE else "disagree"
    _record(PAIR, "penny_exact", used_rpc,
            str(round(local_supply, 2)), str(round(ext_supply, 2)),
            status, delta=round(delta, 4),
            tolerance=SUPPLY_PENNY_TOLERANCE,
            note=(f"delta=${delta:.4f}" if status == "disagree"
                  else f"delta=${delta:.4f} within ${SUPPLY_PENNY_TOLERANCE} tolerance"))


# ── Pair 2: RLUSD-XRPL supply ────────────────────────────────────────────────

def _check_rlusd_xrpl_supply() -> None:
    PAIR = "rlusd_xrpl_supply_vs_xrplcluster"
    SRC = XRPL_XCHECK_NODE

    payload, _ = db.read_rlusd_state_cache()
    if not payload:
        _record(PAIR, "penny_exact", SRC, None, None, "local_unavailable",
                note="rlusd_state_cache empty")
        return
    xrpl = payload.get("xrpl") or {}
    if xrpl.get("error"):
        _record(PAIR, "penny_exact", SRC, None, None, "local_unavailable",
                note=f"local xrpl sub-dict has error: {xrpl['error']}")
        return
    local_supply = float(xrpl.get("supply") or 0)

    try:
        result = _xrpl_post(SRC, "gateway_balances", {
            "account": XRPL_RLUSD_ISSUER,
            "ledger_index": "validated",
        })
        if result is None:
            raise ValueError("gateway_balances returned None/error")
        obligations = result.get("obligations") or {}
        ext_supply = 0.0
        for key, val in obligations.items():
            if key.upper() in ("RLUSD", XRPL_CURRENCY_HEX):
                ext_supply += float(val)
        if ext_supply == 0.0 and not obligations:
            raise ValueError("empty obligations — issuer may not exist on this node")
    except Exception as exc:
        _record(PAIR, "penny_exact", SRC,
                str(round(local_supply, 2)), None,
                "external_unreachable", note=f"{type(exc).__name__}: {exc}")
        return

    delta = abs(local_supply - ext_supply)
    status = "agree" if delta <= SUPPLY_PENNY_TOLERANCE else "disagree"
    _record(PAIR, "penny_exact", SRC,
            str(round(local_supply, 2)), str(round(ext_supply, 2)),
            status, delta=round(delta, 4),
            tolerance=SUPPLY_PENNY_TOLERANCE,
            note=(f"delta=${delta:.4f}" if status == "disagree"
                  else f"delta=${delta:.4f} within ${SUPPLY_PENNY_TOLERANCE} tolerance"))


# ── Pair 3: XRP price band ────────────────────────────────────────────────────

def _check_xrp_price() -> None:
    PAIR = "xrp_price_vs_coingecko"
    SRC = "coingecko.com/api/v3"

    # Local: on-chain AMM-implied price (same formula the site serves).
    import xrp_price as _xp
    local_result = _xp.fetch_xrp_price()
    if local_result.get("error"):
        _record(PAIR, "band", SRC, None, None, "local_unavailable",
                note=f"local AMM fetch error: {local_result['error']}")
        return
    local_price = float(local_result["price"])

    try:
        data = _http_get(COINGECKO_PRICE_URL)
        ext_price = float(data["ripple"]["usd"])
    except Exception as exc:
        _record(PAIR, "band", SRC,
                str(round(local_price, 6)), None,
                "external_unreachable", note=f"{type(exc).__name__}: {exc}")
        return

    delta = abs(local_price - ext_price)
    band = ext_price * XRP_PRICE_BAND_FRACTION
    status = "agree" if delta <= band else "disagree"
    _record(PAIR, "band", SRC,
            str(round(local_price, 6)), str(round(ext_price, 6)),
            status, delta=round(delta, 6),
            tolerance=round(band, 6),
            note=(
                f"local={local_price:.4f} ext={ext_price:.4f} "
                f"band=±{XRP_PRICE_BAND_FRACTION*100:.0f}% (${band:.4f})"
            ))


# ── Pair 4: Amendments local vs mainnet ──────────────────────────────────────

def _check_amendments() -> None:
    PAIR = "amendments_local_vs_mainnet"
    SRC = f"{XRPL_PUBLIC_NODE} Amendments ledger object"

    # Local: our rippled's feature RPC (what this build knows).
    local_result = None
    try:
        local_result = _xrpl_post(XRPL_LOCAL_NODE, "feature", {})
    except Exception as exc:
        local_result = None
        logger.info("local rippled unreachable for amendments check: %s", exc)

    # Mainnet: Amendments ledger object (canonical enabled-on-chain set).
    # Amendments ledger entry hash is constant across all XRPL networks.
    AMENDMENTS_INDEX = (
        "7DB0788C020F02780A673DC74757F23823FA3014C1866E72CC4CD8B226CD6EF4"
    )
    try:
        mainnet_result = _xrpl_post(XRPL_PUBLIC_NODE, "ledger_entry", {
            "index": AMENDMENTS_INDEX,
            "ledger_index": "validated",
        })
        if mainnet_result is None:
            raise ValueError("ledger_entry returned None")
        enabled_on_chain = set(
            (mainnet_result.get("node") or {}).get("Amendments") or []
        )
    except Exception as exc:
        _record(PAIR, "count_exact", SRC,
                str(len((local_result or {}).get("features", {}))),
                None, "external_unreachable",
                note=f"mainnet Amendments ledger fetch failed: {type(exc).__name__}: {exc}")
        return

    if local_result is None:
        # Can't compare without local — record live mainnet count as baseline.
        _record(PAIR, "count_exact", SRC,
                None, str(len(enabled_on_chain)), "local_unavailable",
                note=f"localhost:{XRPL_LOCAL_NODE.split(':')[-1]} unreachable; "
                     f"mainnet enabled_count={len(enabled_on_chain)}")
        return

    local_features = local_result.get("features") or {}
    local_enabled = {
        h for h, info in local_features.items()
        if isinstance(info, dict) and info.get("enabled")
    }

    # Amendments on-chain but absent from our local feature set = our node
    # doesn't know about them. This is the expected state while local
    # rippled predates a recently activated amendment.
    unrecognized = enabled_on_chain - local_enabled
    extra_local = local_enabled - enabled_on_chain  # enabled locally but not mainnet: unexpected

    if unrecognized or extra_local:
        note_parts = []
        if unrecognized:
            note_parts.append(
                f"on-chain but not in local features ({len(unrecognized)}): "
                + ", ".join(sorted(unrecognized)[:5])
                + ("…" if len(unrecognized) > 5 else "")
            )
        if extra_local:
            note_parts.append(
                f"in local features but not on-chain ({len(extra_local)}): "
                + ", ".join(sorted(extra_local)[:5])
            )
        delta = len(unrecognized) + len(extra_local)
        _record(PAIR, "count_exact", SRC,
                str(len(local_enabled)), str(len(enabled_on_chain)),
                "disagree", delta=float(delta),
                note="; ".join(note_parts))
    else:
        _record(PAIR, "count_exact", SRC,
                str(len(local_enabled)), str(len(enabled_on_chain)),
                "agree", delta=0.0,
                note=f"both={len(local_enabled)} enabled amendments")


# ── Pair 5: Validator UNL live vs stored ─────────────────────────────────────

def _check_validator_unl() -> None:
    PAIR = "validator_unl_live_vs_stored"
    SRC = VL_RIPPLE_URL

    # External: live fetch of vl.ripple.com.
    try:
        raw = _http_get(VL_RIPPLE_URL)
        # The signed list blob is base64url-encoded. Decode to get the JSON list.
        blob_b64 = raw.get("blob", "")
        # Standard b64decode after fixing URL-safe padding.
        blob_bytes = base64.urlsafe_b64decode(blob_b64 + "==")
        blob = json.loads(blob_bytes)
        live_validators = blob.get("validators") or []
        live_count = len(live_validators)
    except Exception as exc:
        _record(PAIR, "count_exact", SRC, None, None, "external_unreachable",
                note=f"{type(exc).__name__}: {exc}")
        return

    # Local: most recent stored snapshot.
    stored = db.read_recent_unl_snapshots("vl.ripple.com", limit=1)
    if not stored:
        _record(PAIR, "count_exact", SRC,
                None, str(live_count), "local_unavailable",
                note=(f"no stored unl_snapshots rows yet; "
                      f"live vl.ripple.com count={live_count} (baseline noted)"))
        return

    snap = stored[0]
    stored_payload = snap.get("payload") or {}
    stored_validators = stored_payload.get("validators") or []
    stored_count = len(stored_validators)
    stored_date = snap.get("snapshot_date", "?")

    delta = abs(live_count - stored_count)
    status = "agree" if delta == 0 else "disagree"
    _record(PAIR, "count_exact", SRC,
            f"{stored_count} (snapshot {stored_date})", str(live_count),
            status, delta=float(delta),
            note=(
                f"live={live_count} stored={stored_count} "
                f"(from {stored_date}); "
                + ("counts match" if status == "agree"
                   else f"delta={delta} — validator rotation or list update")
            ))


# ── Pair 6: Ledger vocabulary local vs public ─────────────────────────────────

def _check_ledger_vocab() -> None:
    PAIR = "ledger_vocab_local_vs_public"
    SRC = XRPL_PUBLIC_NODE

    # Local: from DB (populated by ledger_definitions_walker at ~6h cadence).
    local_defs = db.read_ledger_definitions()
    if local_defs is None:
        _record(PAIR, "set_equal", SRC, None, None, "local_unavailable",
                note="ledger_definitions table empty — "
                     "ledger_definitions_walker may not have run yet")
        return

    local_entry_types = set((local_defs.get("entry_types") or {}).keys())
    local_tx_types = set((local_defs.get("tx_types") or {}).keys())
    local_build = local_defs.get("build_version") or "unknown"

    # External: public s1.ripple.com server_definitions.
    try:
        result = _xrpl_post(XRPL_PUBLIC_NODE, "server_definitions", {})
        if result is None:
            raise ValueError("server_definitions returned None/error")
        raw_entry = result.get("LEDGER_ENTRY_TYPES") or {}
        raw_tx = result.get("TRANSACTION_TYPES") or {}

        # server_definitions returns {name: int_code}; strip sentinel -1.
        ext_entry_types = {
            k for k, v in raw_entry.items()
            if isinstance(v, int) and v != -1
        }
        ext_tx_types = {
            k for k, v in raw_tx.items()
            if isinstance(v, int) and v != -1
        }
    except Exception as exc:
        _record(PAIR, "set_equal", SRC,
                f"entry={len(local_entry_types)},tx={len(local_tx_types)}",
                None, "external_unreachable",
                note=f"{type(exc).__name__}: {exc}")
        return

    # Set diffs — what the public node knows that our local build doesn't.
    entry_missing = ext_entry_types - local_entry_types   # in public, not local
    tx_missing = ext_tx_types - local_tx_types
    entry_extra = local_entry_types - ext_entry_types     # in local, not public
    tx_extra = local_tx_types - ext_tx_types

    if not entry_missing and not tx_missing and not entry_extra and not tx_extra:
        _record(PAIR, "set_equal", SRC,
                f"entry={len(local_entry_types)},tx={len(local_tx_types)}",
                f"entry={len(ext_entry_types)},tx={len(ext_tx_types)}",
                "agree", delta=0.0,
                note=f"local build={local_build} matches public node vocabulary")
        return

    # Disagreement: names the vocabularies — every missing entry is a
    # TokenEscrow-class incident waiting to cause a silent omission.
    note_parts = []
    if entry_missing:
        note_parts.append(
            f"entry_types in public not in local ({len(entry_missing)}): "
            + ", ".join(sorted(entry_missing))
        )
    if tx_missing:
        note_parts.append(
            f"tx_types in public not in local ({len(tx_missing)}): "
            + ", ".join(sorted(tx_missing))
        )
    if entry_extra:
        note_parts.append(
            f"entry_types in local not in public ({len(entry_extra)}): "
            + ", ".join(sorted(entry_extra))
        )
    if tx_extra:
        note_parts.append(
            f"tx_types in local not in public ({len(tx_extra)}): "
            + ", ".join(sorted(tx_extra))
        )
    total_delta = (len(entry_missing) + len(tx_missing)
                   + len(entry_extra) + len(tx_extra))
    _record(PAIR, "set_equal", SRC,
            f"entry={len(local_entry_types)},tx={len(local_tx_types)}"
            f" (build={local_build})",
            f"entry={len(ext_entry_types)},tx={len(ext_tx_types)}",
            "disagree", delta=float(total_delta),
            note="; ".join(note_parts))


# ── Main ──────────────────────────────────────────────────────────────────────

_PAIRS = [
    ("rlusd_eth_supply", _check_rlusd_eth_supply),
    ("rlusd_xrpl_supply", _check_rlusd_xrpl_supply),
    ("xrp_price", _check_xrp_price),
    ("amendments", _check_amendments),
    ("validator_unl", _check_validator_unl),
    ("ledger_vocab", _check_ledger_vocab),
]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = None
    try:
        results = {}
        for pair_label, fn in _PAIRS:
            try:
                fn()
                results[pair_label] = "ok"
            except Exception as exc:
                results[pair_label] = f"exception: {type(exc).__name__}: {exc}"
                logger.exception("pair %s raised unexpectedly", pair_label)
        summary = ", ".join(f"{k}={v}" for k, v in results.items())
        message = f"pairs={len(_PAIRS)} {summary}"
        ok = True
        logger.info(message)
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end(WALKER_NAME, ok=ok, message=message)
    sys.exit(0)


if __name__ == "__main__":
    main()
