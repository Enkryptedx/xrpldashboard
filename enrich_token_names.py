#!/usr/bin/env python3
"""enrich_token_names.py — populate token_names.json from first-party sources.

Part A (MPT metadata): reads mpt_snapshot.json, writes every named issuance
  with source="mpt_metadata". XLS-89 on-ledger data — highest trust tier.

Part B (IOU TOML): iterates unlabeled IOU token issuers from token_volume,
  checks on-chain Domain field, fetches xrp-ledger.toml, matches against
  [[CURRENCIES]] blocks by (code, issuer). Writes source="toml" on full
  match, source="domain_fallback" when domain/TOML found but no matching
  currency block (issuer org name only).

Does NOT overwrite existing entries — safe to re-run without data loss.

Usage:
    python enrich_token_names.py            # full run, writes token_names.json
    python enrich_token_names.py --dry-run  # report only, no writes
    python enrich_token_names.py --part-a   # MPT only
    python enrich_token_names.py --part-b   # IOU TOML only
    python enrich_token_names.py --limit 50 # cap IOU issuers checked (default 200)
"""

from __future__ import annotations

import argparse
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

HERE = os.path.abspath(os.path.dirname(__file__))
TOKEN_NAMES_PATH = os.path.join(HERE, "token_names.json")
MPT_SNAPSHOT_PATH = os.path.join(HERE, "mpt_snapshot.json")

XRPL_RPC = os.environ.get("XRPL_RPC", "https://s1.ripple.com:51234")
RPC_PAUSE = 0.12   # seconds between account_info calls
TOML_PAUSE = 0.15  # seconds between TOML fetches


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


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--part-a",   action="store_true", help="MPT metadata only")
    ap.add_argument("--part-b",   action="store_true", help="IOU TOML only")
    ap.add_argument("--limit",    type=int, default=200,
                    help="Max IOU issuers to check (default 200)")
    args = ap.parse_args()

    run_a = args.part_a or (not args.part_a and not args.part_b)
    run_b = args.part_b or (not args.part_a and not args.part_b)

    log(f"enrich_token_names.py start "
        f"(dry_run={args.dry_run} part_a={run_a} part_b={run_b} limit={args.limit})")

    names = _load(TOKEN_NAMES_PATH)
    log(f"loaded {len(names)} existing entries from token_names.json")

    total = 0

    if run_a:
        n = enrich_mpt(names, dry_run=args.dry_run)
        log(f"Part A done: +{n} MPT entries")
        total += n

    if run_b:
        n = enrich_iou_toml(names, dry_run=args.dry_run, limit=args.limit)
        log(f"Part B done: +{n} IOU TOML entries")
        total += n

    if not args.dry_run and total > 0:
        _save(TOKEN_NAMES_PATH, names)
        log(f"saved token_names.json ({len(names)} total entries, +{total} new)")
    elif args.dry_run:
        log(f"dry-run complete: would add {total} entries")
    else:
        log("no new entries found — token_names.json unchanged")


if __name__ == "__main__":
    main()
