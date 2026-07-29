#!/usr/bin/env python3
"""D1 pull 3: TOML sweep on every resolving domain from pull 2.

For each resolving domain, GET https://{domain}/.well-known/xrp-ledger.toml,
parse [[ACCOUNTS]] and [[CURRENCIES]] (both canonical per xrpl.org).
Also check the non-canonical [[TOKENS]] since some tomls use it. Report
whether the issuer in question is declared by the domain (2-way attestation).

Row shape: {issuer, domain, http_status, toml_found, toml_declares_issuer,
            attestation_chain_closed, canonical_currencies_section,
            legacy_tokens_section, note}
"""
import json
import os
import ssl
import sys
import time
import urllib.request

sys.path.insert(0, "/Users/charliebruce/xrpl_test")

import certifi
import tomllib

DOMAIN_DECODE = "/Users/charliebruce/xrpl_test/scratch/d1_domain_decode.json"
OUT_PATH = "/Users/charliebruce/xrpl_test/scratch/d1_toml_sweep.json"
UA = "xrpldashboard-d1-toml-sweep/1.0 (+https://xrpldashboard.com)"
TIMEOUT = 10
SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def fetch_toml(domain: str) -> tuple[int, dict | None, str | None]:
    url = f"https://{domain}/.well-known/xrp-ledger.toml"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
            status = resp.status
            raw = resp.read(512 * 1024)
    except urllib.error.HTTPError as e:
        return e.code, None, f"http {e.code}"
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {str(e)[:120]}"
    try:
        data = tomllib.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        return status, None, f"parse: {type(e).__name__}: {str(e)[:120]}"
    return status, data, None


def declares_issuer(parsed: dict, issuer: str) -> dict:
    """Check attestation across canonical and non-canonical sections.

    Canonical (xrpl.org spec): [[ACCOUNTS]].address + [[CURRENCIES]].
    Non-canonical (Firstledger et al.): [[ISSUERS]].address + [[TOKENS]].issuer.

    We report both readings so the synthesizer can decide which bar
    the honest floor uses."""
    def _has_address(section, key):
        for x in (parsed.get(section) or []):
            if isinstance(x, dict) and (x.get(key) or "").strip() == issuer:
                return True
        return False

    canonical_declares = _has_address("ACCOUNTS", "address")
    noncanonical_issuers = _has_address("ISSUERS", "address")
    noncanonical_tokens = _has_address("TOKENS", "issuer")
    return {
        "canonical_declares": canonical_declares,
        "noncanonical_issuers_declares": noncanonical_issuers,
        "noncanonical_tokens_declares": noncanonical_tokens,
        "has_currencies": bool(parsed.get("CURRENCIES")),
        "has_tokens": bool(parsed.get("TOKENS")),
        "has_accounts": bool(parsed.get("ACCOUNTS")),
        "has_issuers": bool(parsed.get("ISSUERS")),
    }


def main():
    src = json.load(open(DOMAIN_DECODE))
    resolving = [r for r in src["rows"] if r["resolves"]]

    results = []
    for i, r in enumerate(resolving, 1):
        issuer = r["issuer"]
        domain = r["domain_ascii"]
        row = {
            "issuer": issuer,
            "domain": domain,
            "http_status": None,
            "toml_found": False,
            "canonical_declares": False,
            "canonical_attestation_closed": False,
            "noncanonical_declares": False,
            "noncanonical_attestation_closed": False,
            "sections": None,
            "note": None,
        }
        status, parsed, err = fetch_toml(domain)
        row["http_status"] = status
        if parsed is not None:
            row["toml_found"] = True
            d = declares_issuer(parsed, issuer)
            row["sections"] = d
            row["canonical_declares"] = d["canonical_declares"]
            row["noncanonical_declares"] = (
                d["noncanonical_issuers_declares"]
                or d["noncanonical_tokens_declares"]
            )
            # Attestation chain closed = on-chain Domain points to
            # a domain whose TOML also declares the issuer address.
            # "Canonical" = strict xrpl.org spec ([[ACCOUNTS]]).
            # "Non-canonical" includes the [[ISSUERS]] / [[TOKENS]]
            # section shape that firstledger + others emit.
            row["canonical_attestation_closed"] = row["canonical_declares"]
            row["noncanonical_attestation_closed"] = (
                row["canonical_declares"] or row["noncanonical_declares"]
            )
        else:
            row["note"] = err
        results.append(row)
        marker = ("T" if row["toml_found"] else "-")
        marker += ("C" if row["canonical_declares"] else
                   ("N" if row["noncanonical_declares"] else "-"))
        if i % 10 == 0 or row["toml_found"]:
            print(f"  [{i:3d}/{len(resolving)}] {marker} {domain[:40]:40s}  status={status}  note={err}")
        time.sleep(0.1)

    total = len(results)
    found = sum(1 for r in results if r["toml_found"])
    canon = sum(1 for r in results if r["canonical_attestation_closed"])
    non = sum(1 for r in results if r["noncanonical_attestation_closed"])
    out = {
        "generated_at": int(time.time()),
        "domains_probed": total,
        "toml_found": found,
        "canonical_attestation_closed": canon,
        "noncanonical_attestation_closed": non,
        "rows": results,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print()
    print(f"Written: {OUT_PATH}")
    print(f"Resolving domains probed: {total}")
    print(f"  TOML fetch succeeded:                        {found:3d} ({found/total*100:.0f}%)")
    print(f"  Canonical attestation closed ([[ACCOUNTS]]): {canon:3d} ({canon/total*100:.0f}%)")
    print(f"  Non-canonical attestation closed (incl [[ISSUERS]]/[[TOKENS]]): {non:3d} ({non/total*100:.0f}%)")


if __name__ == "__main__":
    main()
