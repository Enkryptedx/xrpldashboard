#!/usr/bin/env python3
"""verify_unlabeled_tokens.py — auto-propose token_names.json entries for
the top unlabeled tokens, using XRPL on-ledger Domain + the issuer's own
xrp-ledger.toml file.

For each top unlabeled token:
  1. Look up the issuer's account on the XRPL via account_info.
  2. Read the issuer's Domain field (set by the issuer themselves).
  3. Fetch https://<domain>/.well-known/xrp-ledger.toml.
  4. Find the [[CURRENCIES]] entry matching our (currency, issuer).
  5. Print a proposed token_names.json entry with verified_via set to the
     toml URL. Category guessed from asset_class / asset_subclass when the
     toml provides them, else marked TODO_review for human pick.

This is the canonical XRPL verification pattern (XLS-26d) — same source
XRPSCAN, Bithomp, and xrpl.to use. The Domain is signed by the issuer's
key on-ledger, so the toml is first-party by construction.

Usage:
  python verify_unlabeled_tokens.py            # default: 7d, top 25
  python verify_unlabeled_tokens.py --top 50 --hours 24
  python verify_unlabeled_tokens.py --json proposed.json   # write file
"""
import argparse
import json
import os
import sqlite3
import sys
import time

import httpx

try:
    import tomllib
except ImportError:
    print("Need Python 3.11+ for tomllib. Project venv uses 3.14, so run via venv.", file=sys.stderr)
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app
import db

XRPL_RPC = "https://s2.ripple.com:51234"
HTTP_TIMEOUT = 8.0
USER_AGENT = "xrpldashboard-curation-helper/1.0 (+https://xrpldashboard.com)"
HTTP = httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def fetch_top_rows(hours_back, limit):
    """(currency, issuer, trades, hours_active) — PG first, SQLite fallback."""
    if db.pg_available():
        try:
            return db.read_token_volume_aggregates(hours_back=hours_back, limit=limit)
        except Exception as e:
            print(f"# pg read failed ({e}); falling back to local volumes.db", file=sys.stderr)
    if not os.path.exists(app.VOLUMES_DB_PATH):
        sys.exit(f"no volumes.db at {app.VOLUMES_DB_PATH}")
    where, params = "", []
    if hours_back is not None:
        cutoff = int(time.time() // 3600) - hours_back
        where = "WHERE hour_bucket >= ?"
        params.append(cutoff)
    params.append(limit)
    conn = sqlite3.connect(f"file:{app.VOLUMES_DB_PATH}?mode=ro", uri=True)
    try:
        return conn.execute(
            f"SELECT currency, issuer, SUM(trade_count) AS trades, "
            f"COUNT(*) AS hours_active FROM token_volume {where} "
            f"GROUP BY currency, issuer ORDER BY trades DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()


def _normalize_domain(raw):
    """Issuer-set Domain fields are messy: some include 'https://' prefixes,
    trailing slashes, paths, or whitespace. Reduce to host[:port][/path]."""
    s = raw.strip()
    for prefix in ("https://", "http://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
    return s.strip("/").strip()


def get_issuer_domain(account):
    """Return (normalized_domain, None) or (None, error_message)."""
    try:
        resp = HTTP.post(XRPL_RPC, json={
            "method": "account_info",
            "params": [{"account": account, "ledger_index": "validated"}],
        })
        data = resp.json()
    except Exception as e:
        return None, f"rpc fetch failed: {e}"
    result = (data or {}).get("result", {})
    if result.get("status") != "success":
        return None, f"rpc error: {result.get('error_message') or result.get('error') or 'unknown'}"
    domain_hex = (result.get("account_data") or {}).get("Domain")
    if not domain_hex:
        return None, "issuer has no Domain field on ledger"
    try:
        raw = bytes.fromhex(domain_hex).decode("ascii")
    except Exception:
        return None, f"domain field not ascii hex: {domain_hex}"
    normalized = _normalize_domain(raw)
    if not normalized:
        return None, f"domain empty after normalization (raw: {raw!r})"
    return normalized, None


def fetch_toml(domain):
    """Return (text, url, None) or (None, url, error)."""
    url = f"https://{domain}/.well-known/xrp-ledger.toml"
    try:
        resp = HTTP.get(url, headers={"Accept": "text/plain, application/toml, */*"})
    except Exception as e:
        return None, url, f"fetch failed: {e}"
    if resp.status_code != 200:
        return None, url, f"HTTP {resp.status_code}"
    return resp.text, url, None


def guess_category(toml_entry):
    """Map xrp-ledger.toml asset_class / asset_subclass to our category enum.
    Returns one of stablecoin / fiat / wrapped_major / native_utility /
    memecoin / other / TODO_review (when the toml didn't say enough)."""
    cls = (toml_entry.get("asset_class") or "").strip().lower()
    sub = (toml_entry.get("asset_subclass") or "").strip().lower()
    if sub == "stablecoin":
        return "stablecoin"
    if cls == "fiat":
        return "fiat"
    if sub == "wrapped" or cls == "wrapped":
        return "wrapped_major"
    if cls in ("commodity", "stock", "fund"):
        return "other"
    if cls == "crypto":
        # Without more signal we can't safely classify a crypto token as
        # "memecoin" or "native_utility" — leave for a human pick.
        return "TODO_review"
    return "TODO_review"


def find_currency_match(toml_data, currency_hex, issuer):
    """Return (matched_entry_dict, None) or (None, reason).

    XRPL community tomls use either [[TOKENS]] (XLS-26d / firstledger /
    most projects) or [[CURRENCIES]] (older spec). Field names: 'currency'
    or 'code'. Match by issuer + currency, accepting either the raw
    on-ledger form (3-char or 40-char hex) or the decoded ASCII display."""
    entries = []
    for section in ("TOKENS", "CURRENCIES"):
        v = toml_data.get(section)
        if isinstance(v, list):
            entries.extend(v)
    if not entries:
        return None, "no [[TOKENS]] or [[CURRENCIES]] entries in toml"
    decoded = app._decode_currency_hex(currency_hex) if len(currency_hex) == 40 else currency_hex
    target_upper = currency_hex.upper()
    decoded_lower = (decoded or "").lower()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_iss = (entry.get("issuer") or "").strip()
        if entry_iss and entry_iss != issuer:
            continue
        code = (entry.get("currency") or entry.get("code") or "").strip()
        if not code:
            continue
        if code.upper() == target_upper:
            return entry, None
        if decoded_lower and code.lower() == decoded_lower:
            return entry, None
    return None, "no token entry matched our (currency, issuer)"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=24 * 7,
                    help="lookback window in hours (default 168 = 7d)")
    ap.add_argument("--top", type=int, default=25,
                    help="how many top unlabeled to attempt (default 25)")
    ap.add_argument("--scan", type=int, default=400,
                    help="scan depth for candidate pool (default 400)")
    ap.add_argument("--json", type=str, default=None,
                    help="write proposed entries to this JSON file (otherwise stdout)")
    args = ap.parse_args()

    rows = fetch_top_rows(args.hours, args.scan)
    labels = app._load_token_names_dict()
    unlabeled = [r for r in rows if (r[0], r[1]) not in labels][: args.top]

    by_issuer = {}
    for cur, iss, trades, hours in unlabeled:
        by_issuer.setdefault(iss, []).append({
            "currency": cur, "trades": int(trades or 0), "hours": int(hours or 0),
        })

    print(f"# top {len(unlabeled)} unlabeled tokens grouped to {len(by_issuer)} issuers")
    print(f"# window: last {args.hours}h  ·  scan: {args.scan} candidates\n")

    proposed = {}  # key → entry dict (token_names.json shape)
    notes = []    # human-readable lines per issuer

    for issuer, tokens in by_issuer.items():
        line = [f"--- {issuer}  ({len(tokens)} unlabeled)"]
        domain, err = get_issuer_domain(issuer)
        if not domain:
            line.append(f"    skip: {err}")
            print("\n".join(line)); continue
        line.append(f"    domain: {domain}")
        toml_text, toml_url, err = fetch_toml(domain)
        if not toml_text:
            line.append(f"    skip: {err}  (tried {toml_url})")
            print("\n".join(line)); continue
        try:
            toml_data = tomllib.loads(toml_text)
        except Exception as e:
            line.append(f"    skip: toml parse failed ({e})")
            print("\n".join(line)); continue
        for tok in tokens:
            entry, err = find_currency_match(toml_data, tok["currency"], issuer)
            if not entry:
                line.append(f"    [{tok['currency'][:8]}…] no match — {err}")
                continue
            # Prefer the symbol for display (matches existing entries like
            # RLUSD, USDC, USD.Bitstamp). For 3-char codes the toml's
            # 'currency' field IS the symbol; for 40-char hex codes the
            # toml usually echoes the hex, so decode it to ASCII (e.g.
            # "5A455250500..." → "ZERPS"). Project name + desc go in _note.
            toml_cur = (entry.get("currency") or entry.get("code") or "").strip()
            if len(tok["currency"]) == 40:
                display = (app._decode_currency_hex(tok["currency"])
                           or (toml_cur if len(toml_cur) <= 12 else None)
                           or tok["currency"][:8])
            else:
                display = toml_cur or tok["currency"]
            category = guess_category(entry)
            note_parts = []
            project_name = (entry.get("name") or "").strip()
            if project_name and project_name.lower() != display.lower():
                note_parts.append(project_name)
            desc = (entry.get("desc") or "").strip()
            if desc:
                # Collapse whitespace and cap so the JSON stays readable.
                desc_clean = " ".join(desc.split())
                if len(desc_clean) > 220:
                    desc_clean = desc_clean[:217].rstrip() + "…"
                note_parts.append(desc_clean)
            key = f"{tok['currency']}:{issuer}"
            entry_out = {
                "currency_hex": tok["currency"],
                "currency_display": display,
                "issuer": issuer,
                "category": category,
                "verified_via": toml_url,
            }
            if note_parts:
                entry_out["_note"] = " — ".join(note_parts)
            proposed[key] = entry_out
            line.append(
                f"    [{tok['currency'][:8]}…] → {display!r} "
                f"({category}, {tok['trades']:,} trades)"
            )
        print("\n".join(line))

    print()
    print(f"# proposed {len(proposed)} verified entries (out of {len(unlabeled)} attempted)")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(proposed, f, indent=2)
        print(f"# wrote {args.json}")
    else:
        print(json.dumps(proposed, indent=2))


if __name__ == "__main__":
    main()
