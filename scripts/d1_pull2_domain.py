#!/usr/bin/env python3
"""D1 pull 2: AccountRoot.Domain decode for each of the top-100 unlabeled
issuers. Uses xrpl-py account_info + hex decode + domain-safety regex.

Output row shape: {issuer, domain_hex, domain_ascii, ascii_valid, resolves}
  - domain_hex: raw hex field from AccountRoot.Domain (None if not set)
  - domain_ascii: bytes.fromhex().decode('ascii', errors='replace') best-effort
  - ascii_valid: True if it parses as an RFC 1035 hostname (regex from verify_toml_accounts.py)
  - resolves: True if DNS A/AAAA lookup succeeds

Writes to /Users/charliebruce/xrpl_test/scratch/d1_domain_decode.json.
"""
import json
import os
import socket
import sys
import time

sys.path.insert(0, "/Users/charliebruce/xrpl_test")

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountInfo

# Reuse the domain-safety gate from the production verifier.
from verify_toml_accounts import _is_safe_domain  # noqa

XRPL_RPC = os.environ.get("XRPL_RPC", "https://s1.ripple.com:51234")
IN_PATH = "/Users/charliebruce/xrpl_test/scratch/d1_unlabeled_top100.json"
OUT_PATH = "/Users/charliebruce/xrpl_test/scratch/d1_domain_decode.json"


def decode_domain(hex_str: str | None) -> str | None:
    if not hex_str:
        return None
    try:
        return bytes.fromhex(hex_str).decode("ascii", errors="replace").strip().lower()
    except (ValueError, UnicodeDecodeError):
        return None


def dns_resolves(host: str) -> bool:
    try:
        socket.gethostbyname(host)
        return True
    except (socket.gaierror, UnicodeError):
        return False


def main():
    src = json.load(open(IN_PATH))
    issuers_seen: set[str] = set()
    ordered_issuers: list[str] = []
    for r in src["rows"]:
        iss = r["issuer"]
        if iss not in issuers_seen:
            issuers_seen.add(iss)
            ordered_issuers.append(iss)

    client = JsonRpcClient(XRPL_RPC)
    results = []
    for i, issuer in enumerate(ordered_issuers, 1):
        row = {
            "issuer": issuer,
            "domain_hex": None,
            "domain_ascii": None,
            "ascii_valid": False,
            "resolves": False,
            "err": None,
        }
        try:
            r = client.request(AccountInfo(account=issuer))
            data = (r.result.get("account_data") or {})
            hex_str = data.get("Domain")
            row["domain_hex"] = hex_str
            ascii_str = decode_domain(hex_str)
            row["domain_ascii"] = ascii_str
            if ascii_str and _is_safe_domain(ascii_str):
                row["ascii_valid"] = True
                row["resolves"] = dns_resolves(ascii_str)
        except Exception as e:
            row["err"] = str(e)[:200]
        results.append(row)
        marker = "d" if row["domain_hex"] else "-"
        marker += "v" if row["ascii_valid"] else " "
        marker += "r" if row["resolves"] else " "
        if i % 10 == 0 or row["domain_hex"]:
            print(f"  [{i:3d}/{len(ordered_issuers)}] {marker} {issuer}  domain={row['domain_ascii']}")
        # Gentle pacing on s1.
        time.sleep(0.05)

    total = len(results)
    has_domain = sum(1 for r in results if r["domain_hex"])
    ascii_valid = sum(1 for r in results if r["ascii_valid"])
    resolves = sum(1 for r in results if r["resolves"])
    out = {
        "generated_at": int(time.time()),
        "xrpl_rpc": XRPL_RPC,
        "issuer_count": total,
        "with_domain_field": has_domain,
        "with_valid_hostname": ascii_valid,
        "with_resolving_hostname": resolves,
        "rows": results,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print()
    print(f"Written: {OUT_PATH}")
    print(f"Issuers probed: {total}")
    print(f"  With Domain field:        {has_domain:3d} ({has_domain/total*100:.0f}%)")
    print(f"  With valid hostname:      {ascii_valid:3d} ({ascii_valid/total*100:.0f}%)")
    print(f"  With resolving hostname:  {resolves:3d} ({resolves/total*100:.0f}%)")


if __name__ == "__main__":
    main()
