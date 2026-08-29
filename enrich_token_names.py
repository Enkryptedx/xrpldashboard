#!/usr/bin/env python3
"""enrich_token_names.py — populate token_names.json from first-party sources.

Part A (MPT metadata): reads mpt_snapshot.json, writes every named issuance
  with source="mpt_metadata". XLS-89 on-ledger data — highest trust tier.

Part B (IOU TOML): iterates unlabeled IOU token issuers from token_volume,
  checks on-chain Domain field, fetches xrp-ledger.toml, matches against
  [[CURRENCIES]] blocks by (code, issuer). Writes source="toml" on full
  match, source="domain_fallback" when domain/TOML found but no matching
  currency block (issuer org name only).

Part C (LP-token derivation): reads amm_index.json, computes the protocol-
  deterministic LP token currency code for each AMM pool, writes entries
  with source="lp_derived". Same trust tier as mpt_metadata — derived from
  on-ledger AMM state via the canonical algorithm.

Does NOT overwrite existing entries — safe to re-run without data loss.

Usage:
    python enrich_token_names.py            # full run, writes token_names.json
    python enrich_token_names.py --dry-run  # report only, no writes
    python enrich_token_names.py --part-a   # MPT only
    python enrich_token_names.py --part-b   # IOU TOML only
    python enrich_token_names.py --part-c   # LP-token derivation only
    python enrich_token_names.py --limit 50 # cap IOU issuers checked (default 500)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

from verify_toml_accounts import (
    _is_safe_domain,
    fetch_domain_field,
    fetch_toml,
    log,
)
from xrpl.clients import JsonRpcClient
from xrpl.core.addresscodec import decode_classic_address

HERE = os.path.abspath(os.path.dirname(__file__))
TOKEN_NAMES_PATH = os.path.join(HERE, "token_names.json")
MPT_SNAPSHOT_PATH = os.path.join(HERE, "mpt_snapshot.json")
AMM_INDEX_PATH = os.path.join(HERE, "amm_index.json")

XRPL_RPC = os.environ.get("XRPL_RPC", "https://s1.ripple.com:51234")
RPC_PAUSE = 0.12   # seconds between account_info calls
TOML_PAUSE = 0.15  # seconds between TOML fetches
WALKER_CADENCE_SECONDS = 604800  # StartInterval in com.xrpldashboard.enrich_token_names.plist (weekly)


# ─── I/O helpers ─────────────────────────────────────────────────────────────

def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _save(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def _insert(names: dict, key: str, entry: dict) -> bool:
    """Insert only — never overwrite an existing entry."""
    if key in names:
        return False
    names[key] = entry
    return True


# ─── Part A: MPT metadata ─────────────────────────────────────────────────────

def _mpt_category(r: dict) -> str:
    ac = (r.get("asset_class_raw") or "").lower()
    cl = (r.get("classification") or "").lower()
    if "stable" in ac or "stable" in cl:
        return "stablecoin"
    if "rwa" in ac:
        return "rwa"
    if "defi" in ac or "utility" in ac:
        return "native_utility"
    if "meme" in cl:
        return "memecoin"
    return "other"


def enrich_mpt(names: dict, dry_run: bool) -> int:
    snap = _load(MPT_SNAPSHOT_PATH)
    issuances = snap.get("issuances") or []
    if not issuances:
        log("Part A: mpt_snapshot.json has no issuances — skipping")
        return 0

    added = 0
    for r in issuances:
        name   = (r.get("name") or "").strip()
        ticker = (r.get("ticker") or "").strip()
        if not name and not ticker:
            continue
        issuance_id = r.get("issuance_id") or ""
        issuer      = r.get("issuer") or ""
        if not issuance_id or not issuer:
            continue
        if (r.get("classification") or "").lower() == "test":
            continue

        key = f"{issuance_id}:{issuer}"
        entry: dict = {
            "currency_hex":     issuance_id,
            "currency_display": ticker or name[:12],
            "issuer":           issuer,
            "source":           "mpt_metadata",
            "category":         _mpt_category(r),
        }
        if name:
            entry["name"] = name
        if r.get("desc"):
            entry["desc"] = r["desc"]
        if r.get("issuer_name"):
            entry["issuer_name"] = r["issuer_name"]

        if not dry_run and _insert(names, key, entry):
            added += 1
            log(f"  [mpt] {issuance_id[:16]}… {ticker or name[:24]}")
        elif dry_run and key not in names:
            added += 1
            log(f"  [mpt/dry] {issuance_id[:16]}… {ticker or name[:24]}")

    return added


# ─── Part B: IOU TOML enrichment ─────────────────────────────────────────────

def _normalize_code(code: str) -> str:
    """Return uppercase hex form of currency code for comparison.

    XRPL currency codes are either 3-char ASCII (stored as-is, e.g. "USD")
    or 40-char hex (non-standard tokens). TOML [[CURRENCIES]] code fields
    follow the same convention. Normalise both sides to 40-char hex so the
    match is unambiguous regardless of which form each file used."""
    code = code.strip().upper()
    if len(code) == 3:
        # Pad to 40-char hex (per XRPL spec: 3 bytes + 17 zero bytes)
        return code.encode("ascii").hex().upper().ljust(40, "0")
    return code


def _find_currency_block(toml_data: dict, currency_hex: str, issuer: str) -> dict | None:
    """Return the [[CURRENCIES]] block whose (code, issuer) matches, or None.

    Both code and currency_hex are normalised to 40-char hex before comparing
    so "USD" and "5553440000..." match each other. The issuer must be an exact
    string match — this is the key correctness gate Charlie flagged."""
    currencies = toml_data.get("CURRENCIES")
    if not isinstance(currencies, list):
        return None

    our_code = _normalize_code(currency_hex)

    for block in currencies:
        if not isinstance(block, dict):
            continue
        code_raw  = str(block.get("code") or "").strip()
        toml_iss  = str(block.get("issuer") or "").strip()
        if not code_raw or toml_iss != issuer:
            continue
        if _normalize_code(code_raw) == our_code:
            return block
    return None


def _org_name(toml_data: dict) -> str:
    """Extract org name tolerating both [ORGANIZATION] (dict) and
    [[ORGANIZATION]] (array of tables) TOML conventions."""
    org = toml_data.get("ORGANIZATION") or {}
    if isinstance(org, list):
        org = org[0] if org else {}
    if not isinstance(org, dict):
        return ""
    return (org.get("name") or "").strip()


def _make_toml_entry(
    currency_hex: str,
    issuer: str,
    block: dict,
    toml_data: dict,
    toml_url: str,
) -> dict:
    name    = (block.get("name") or "").strip()
    symbol  = (block.get("symbol") or block.get("code") or "").strip()
    desc    = (block.get("desc") or block.get("description") or "").strip()
    org     = _org_name(toml_data)
    display = symbol or name[:12] or currency_hex[:8]

    entry: dict = {
        "currency_hex":     currency_hex,
        "currency_display": display,
        "issuer":           issuer,
        "source":           "toml",
        "verified_via":     toml_url,
    }
    if name:
        entry["name"] = name
    if desc:
        entry["desc"] = desc
    if org:
        entry["issuer_name"] = org
    return entry


def _make_domain_fallback_entry(
    currency_hex: str,
    issuer: str,
    org_name: str,
    domain: str,
    toml_url: str,
) -> dict:
    # No [[CURRENCIES]] match — we know the issuer's org but not the token name.
    # Display the raw code if it's 3-char ASCII, otherwise leave hex.
    display = currency_hex if len(currency_hex) == 3 else ""
    entry: dict = {
        "currency_hex": currency_hex,
        "issuer":       issuer,
        "source":       "domain_fallback",
        "issuer_name":  org_name,
        "domain":       domain,
        "verified_via": toml_url,
    }
    if display:
        entry["currency_display"] = display
    return entry


VOLUMES_DB_PATH = os.path.join(HERE, "volumes.db")


def _read_token_volume_local(limit: int) -> list:
    """SQLite fallback when Postgres is unavailable."""
    import sqlite3
    if not os.path.exists(VOLUMES_DB_PATH):
        return []
    conn = sqlite3.connect(f"file:{VOLUMES_DB_PATH}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT currency, issuer, SUM(trade_count), COUNT(*) "
            "FROM token_volume GROUP BY currency, issuer "
            "ORDER BY SUM(trade_count) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def enrich_iou_toml(names: dict, dry_run: bool, limit: int) -> int:
    rows = None
    try:
        import db
        rows = db.read_token_volume_aggregates(hours_back=None, limit=limit)
    except Exception:
        pass
    if rows is None:
        rows = _read_token_volume_local(limit)
    if not rows:
        log("Part B: no token_volume rows available — skipping")
        return 0

    # Build (currency_hex, issuer) pairs that are NOT already in token_names.
    unlabeled: list[tuple[str, str]] = []
    for currency_hex, issuer, _trades, _hours in rows:
        if not currency_hex or not issuer:
            continue
        key = f"{currency_hex}:{issuer}"
        if key not in names:
            unlabeled.append((currency_hex, issuer))

    log(f"Part B: {len(unlabeled)} unlabeled (currency, issuer) pairs from top-{limit}")

    # Group by issuer to minimise RPC calls: one account_info per issuer.
    issuer_map: dict[str, list[str]] = {}
    for currency_hex, issuer in unlabeled:
        issuer_map.setdefault(issuer, []).append(currency_hex)

    client = JsonRpcClient(XRPL_RPC)
    domain_cache: dict[str, str | None] = {}
    toml_cache: dict[str, tuple[str, dict | None, str | None]] = {}
    added = 0

    for issuer, currencies in issuer_map.items():
        # 1) Fetch on-chain Domain field (one RPC call per issuer)
        if issuer not in domain_cache:
            domain_cache[issuer] = fetch_domain_field(client, issuer)
            time.sleep(RPC_PAUSE)
        domain = domain_cache[issuer]
        if not domain:
            continue

        # 2) Fetch TOML (cached per domain)
        if domain not in toml_cache:
            toml_cache[domain] = fetch_toml(domain)
            time.sleep(TOML_PAUSE)
        toml_url, toml_data, err = toml_cache[domain]
        if err or not isinstance(toml_data, dict):
            if err:
                log(f"  [iou] {issuer[:8]}… domain={domain} toml_err={err}")
            continue

        # 3) For each currency this issuer issued, look for matching [[CURRENCIES]]
        for currency_hex in currencies:
            key = f"{currency_hex}:{issuer}"
            block = _find_currency_block(toml_data, currency_hex, issuer)

            if block:
                entry = _make_toml_entry(currency_hex, issuer, block, toml_data, toml_url)
                if not dry_run:
                    if _insert(names, key, entry):
                        added += 1
                        log(f"  [toml] {currency_hex[:8]}…:{issuer[:8]}… "
                            f"→ {entry['currency_display']} via {domain}")
                else:
                    added += 1
                    log(f"  [toml/dry] {currency_hex[:8]}…:{issuer[:8]}… "
                        f"→ {entry.get('currency_display','?')} via {domain}")
            else:
                # Domain + TOML found, no currency match — org name fallback
                org = _org_name(toml_data)
                if org:
                    entry = _make_domain_fallback_entry(
                        currency_hex, issuer, org, domain, toml_url
                    )
                    if not dry_run:
                        if _insert(names, key, entry):
                            added += 1
                            log(f"  [domain_fallback] {currency_hex[:8]}…:"
                                f"{issuer[:8]}… issuer={org}")
                    else:
                        added += 1
                        log(f"  [domain_fallback/dry] {currency_hex[:8]}…:"
                            f"{issuer[:8]}… issuer={org}")

    return added


# ─── Part C: LP-token derivation ─────────────────────────────────────────────

# Reference: rippled src/libxrpl/protocol/AMMCore.cpp::ammLPTCurrency
# Note: xrpl.org docs say "currency codes and their issuers" but the actual
# algorithm hashes only the 20-byte currency parts; the issuer is the
# ordering key (full Issue = currency+account compared byte-wise), not hash
# input. Spec text on xrpl.org is slightly inaccurate; rippled is canonical.

LP_REFERENCE = "rippled src/libxrpl/protocol/AMMCore.cpp::ammLPTCurrency"


def _currency_to_bytes(cur: str) -> bytes:
    """3-char ASCII -> 20-byte standard form (chars at positions 12-14).
       40-char hex -> raw 20 bytes. 'XRP' -> 20 zero bytes."""
    if cur == "XRP":
        return b"\x00" * 20
    if len(cur) == 3:
        out = bytearray(20)
        out[12:15] = cur.encode("ascii")
        return bytes(out)
    if len(cur) == 40:
        return bytes.fromhex(cur)
    raise ValueError(f"bad currency code: {cur!r}")


def _issue_bytes(asset: dict) -> bytes:
    """40-byte canonical Issue: currency(20) || account(20). XRP zero-pads both."""
    cur = _currency_to_bytes(asset["currency"])
    iss = asset.get("issuer")
    if asset["currency"] == "XRP" or not iss:
        acct = b"\x00" * 20
    else:
        acct = decode_classic_address(iss)
    return cur + acct


def derive_lp_currency(asset1: dict, asset2: dict) -> str:
    """Compute the 40-char hex LP-token currency code for an AMM pool.

    See LP_REFERENCE above for spec source."""
    a, b = _issue_bytes(asset1), _issue_bytes(asset2)
    lo, hi = (a, b) if a < b else (b, a)
    # Hash only the 20-byte currency portions (positions 0-19 of each Issue)
    h = hashlib.sha512(lo[:20] + hi[:20]).digest()[:32]  # sha512Half
    return ("03" + h[:19].hex()).upper()


def _decode_currency_label(cur: str) -> str:
    """Render an XRPL currency code as a short human-presentable label.

    - 'XRP' passes through.
    - 3-char ASCII passes through.
    - 40-char hex: try ASCII decode of the leading bytes (strip trailing zeros)
      if printable, else fall back to first 6 hex chars + '…'."""
    if cur == "XRP" or len(cur) == 3:
        return cur
    if len(cur) == 40:
        try:
            raw = bytes.fromhex(cur).rstrip(b"\x00")
            if raw and all(32 <= b < 127 for b in raw):
                return raw.decode("ascii")
        except Exception:
            pass
        return cur[:6] + "…"
    return cur


def _shippable_side(asset: dict, names: dict) -> tuple[bool, str]:
    """Strict truth-first gate: return (shippable, display) for one LP side.

    XRP always shippable. Non-XRP must have a curated token_names.json entry
    whose verified_via is NOT 'TODO_curation_pass' — labeling an LP token
    implicitly endorses both component tokens, so we only ship LP labels when
    each side has already passed the same verification gate."""
    cur = asset.get("currency", "")
    iss = asset.get("issuer", "")
    if cur == "XRP":
        return True, "XRP"
    existing = names.get(f"{cur}:{iss}")
    if not isinstance(existing, dict):
        return False, ""
    if existing.get("verified_via") == "TODO_curation_pass":
        return False, ""
    disp = (existing.get("currency_display") or "").strip()
    return (bool(disp), disp or _decode_currency_label(cur))


def enrich_lp_tokens(names: dict, dry_run: bool) -> int:
    """Derive LP-token labels ONLY for pools where BOTH sides are
    already-verified, shippable entries in token_names.json.

    Strict-mode rationale: labeling an LP token transitively endorses both
    component tokens. If either side is unverified or TODO-gated, the LP
    label inherits that uncertainty and should not ship. derive_lp_currency
    remains a public helper for on-demand computation of unverified pairs
    without persistence."""
    entries = _load(AMM_INDEX_PATH)
    if not isinstance(entries, list) or not entries:
        log("Part C: amm_index.json missing or empty — skipping")
        return 0

    added = 0
    skipped_unverified = 0
    skipped_existing = 0
    skipped_malformed = 0

    for pool in entries:
        if not isinstance(pool, dict):
            skipped_malformed += 1
            continue
        a1 = pool.get("Asset")
        a2 = pool.get("Asset2")
        amm_acct = pool.get("Account")
        lp_balance = pool.get("LPTokenBalance") or {}
        on_chain_lp_cur = (lp_balance.get("currency") or "").upper()

        if not isinstance(a1, dict) or not isinstance(a2, dict) or not amm_acct:
            skipped_malformed += 1
            continue

        ok1, side1 = _shippable_side(a1, names)
        ok2, side2 = _shippable_side(a2, names)
        if not (ok1 and ok2):
            skipped_unverified += 1
            continue

        try:
            lp_cur = derive_lp_currency(a1, a2)
        except Exception as e:
            log(f"  [lp] derive failed for {amm_acct}: {e}")
            skipped_malformed += 1
            continue

        # Sanity gate: if amm_index recorded the on-chain LP currency and it
        # disagrees with our derivation, refuse to write — surfaces protocol
        # changes or upstream-data drift instead of silently shipping bad codes.
        if on_chain_lp_cur and on_chain_lp_cur != lp_cur:
            log(f"  [lp] WARN derivation mismatch {amm_acct}: "
                f"computed={lp_cur} on_chain={on_chain_lp_cur} — skipped")
            skipped_malformed += 1
            continue

        key = f"{lp_cur}:{amm_acct}"
        if key in names:
            skipped_existing += 1
            continue

        display = f"LP: {side1}/{side2}"

        entry = {
            "currency_hex":     lp_cur,
            "currency_display": display,
            "issuer":           amm_acct,
            "source":           "lp_derived",
            "verified_via":     "lp_derived",
            "category":         "lp_token",
            "asset_pair":       [
                {"currency": a1.get("currency"), "issuer": a1.get("issuer")},
                {"currency": a2.get("currency"), "issuer": a2.get("issuer")},
            ],
            "reference":        LP_REFERENCE,
        }

        if dry_run:
            added += 1
            log(f"  [lp/dry] {lp_cur[:10]}…:{amm_acct[:8]}… → {display}")
        else:
            if _insert(names, key, entry):
                added += 1
                log(f"  [lp] {lp_cur[:10]}…:{amm_acct[:8]}… → {display}")

    log(f"Part C: scanned {len(entries)} pools — "
        f"+{added} shippable, {skipped_unverified} unverified-side, "
        f"{skipped_existing} already present, {skipped_malformed} malformed/mismatch")
    return added


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--part-a",   action="store_true", help="MPT metadata only")
    ap.add_argument("--part-b",   action="store_true", help="IOU TOML only")
    ap.add_argument("--part-c",   action="store_true", help="LP-token derivation only")
    ap.add_argument("--limit",    type=int, default=500,
                    help="Max IOU issuers to check (default 500)")
    args = ap.parse_args()

    _wh_db = None
    if not args.dry_run:
        try:
            import db as _wh_db_mod
            _wh_db = _wh_db_mod
            _wh_db.write_walker_health_start("enrich_token_names", cadence_seconds=WALKER_CADENCE_SECONDS)
        except Exception:
            _wh_db = None

    _ok = False
    _msg = "init"
    try:
        any_explicit = args.part_a or args.part_b or args.part_c
        run_a = args.part_a or not any_explicit
        run_b = args.part_b or not any_explicit
        run_c = args.part_c or not any_explicit

        log(f"enrich_token_names.py start "
            f"(dry_run={args.dry_run} part_a={run_a} part_b={run_b} part_c={run_c} limit={args.limit})")

        names = _load(TOKEN_NAMES_PATH)
        log(f"loaded {len(names)} existing entries from token_names.json")

        part_a_n = part_b_n = part_c_n = 0

        if run_a:
            part_a_n = enrich_mpt(names, dry_run=args.dry_run)
            log(f"Part A done: +{part_a_n} MPT entries")

        if run_b:
            part_b_n = enrich_iou_toml(names, dry_run=args.dry_run, limit=args.limit)
            log(f"Part B done: +{part_b_n} IOU TOML entries")

        if run_c:
            part_c_n = enrich_lp_tokens(names, dry_run=args.dry_run)
            log(f"Part C done: +{part_c_n} LP-derived entries")

        total = part_a_n + part_b_n + part_c_n

        if not args.dry_run and total > 0:
            _save(TOKEN_NAMES_PATH, names)
            log(f"saved token_names.json ({len(names)} total entries, +{total} new)")
        elif args.dry_run:
            log(f"dry-run complete: would add {total} entries")
        else:
            log("no new entries found — token_names.json unchanged")

        _ok = True
        _msg = f"A+{part_a_n} B+{part_b_n} C+{part_c_n} total+{total} names_total={len(names)}"
    except Exception as e:
        _msg = f"{type(e).__name__}: {e}"
        raise
    finally:
        if _wh_db is not None:
            _wh_db.write_walker_health_end("enrich_token_names", ok=_ok, message=_msg)


if __name__ == "__main__":
    main()
