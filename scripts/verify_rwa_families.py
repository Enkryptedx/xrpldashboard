"""verify_rwa_families — one-shot two-way TOML verification for RWA
family attestation.

Charlie ruling 2026-09-22 Tue PM: rwa_family.attestation_level='verified'
only when the family's canonical domain publishes an xrp-ledger.toml
pinning the issuer address(es) AND our two_way_toml_verifier passes.
Otherwise 'labeled' with the citation.

This script performs the verification for the three families currently
carried on /rwa (Ondo, OpenEden, Midas) and PRINTS the outcome. It does
NOT mutate the DB — the caller (JJ) reviews the report and applies the
attestation_level changes with a separate SQL step so the changes are
reviewable.

Two-way test per family:
  FORWARD: fetch <domain>/.well-known/xrp-ledger.toml. Look for
           [[ISSUERS]] or [[TOKENS]] entries whose 'address' matches
           each of the family's XRPL issuer addresses (derived from
           amm_ranked_pools joined via rwa_pool_attribution).
  REVERSE: query each issuer's AccountRoot for its Domain field. Decode
           the hex → ASCII. Confirm the decoded domain equals the family
           canonical domain (host-only compare, with URL normalization
           per two_way_toml_verifier_walker._domain_matches_toml_url).

A family PASSES only if EVERY known issuer for that family passes both
directions. Any miss drops the family to 'labeled'.

Note on Midas: it's an Axelar-bridged token; the mTBILL issuer on XRPL
is *the bridge*, not Midas itself. Midas's own domain won't pin the
XRPL address unless they explicitly recognize the bridged surface. This
run will honestly reveal that — Charlie's whole point.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from two_way_toml_verifier_walker import (
    _fetch_toml, _fetch_xrpl_domain,
    _find_issuer_entry, _domain_matches_toml_url,
)


FAMILIES = [
    # (family_slug, canonical_domain, family_token_symbols)
    # family_token_symbols filters the issuer set to just the family's
    # OWN tokens — pools have two sides and the counter-asset (BITx,
    # XRP, XUSD) isn't the family's issuer.
    ("ondo",     "ondo.finance", {"USDY", "OUSG"}),
    ("openeden", "openeden.com", {"TBILL", "OpenEden"}),
    ("midas",    "midas.app",    {"mTBILL"}),
]


def _issuers_for_family(
    conn, family_slug: str, family_symbols: set[str],
) -> list[tuple[str, str, str]]:
    """Return the distinct issuer tuples for a family's OWN tokens
    (family_symbols filter). Each entry: (issuer_address, display_symbol,
    currency_hex_or_iso). Deduped across all attributed pools. Skips XRP
    and counter-assets (BITx, XUSD, etc.) that aren't the family's own
    issuance."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT p.asset_a, p.asset_b
              FROM amm_ranked_pools p
              JOIN rwa_pool_attribution a ON a.pool_address = p.amm_account
             WHERE a.family_slug = %s
            """,
            (family_slug,),
        )
        rows = cur.fetchall()
    issuers: dict[str, tuple[str, str, str]] = {}
    for a, b in rows:
        for side in (a, b):
            if not isinstance(side, dict):
                continue
            issuer = side.get("issuer")
            if not issuer:  # XRP
                continue
            display = side.get("display") or ""
            if display not in family_symbols:
                continue
            issuers[issuer] = (issuer, display, side.get("currency") or "")
    return list(issuers.values())


def verify_family(family_slug: str, domain: str, issuers: list[tuple[str, str, str]]):
    """Return (verdict, details) for a family. Verdict ∈ {'verified',
    'labeled_toml_missing', 'labeled_domain_mismatch',
    'labeled_no_issuers'}."""
    toml_url = f"https://{domain}/.well-known/xrp-ledger.toml"
    print(f"\n=== {family_slug} @ {domain} ===")
    print(f"  toml_url: {toml_url}")
    print(f"  issuers ({len(issuers)}): {[i[0] + '(' + i[1] + ')' for i in issuers]}")

    if not issuers:
        # No known XRPL issuer for this family — can't verify. Family
        # verification needs an on-ledger anchor to bind the citation to.
        return "labeled_no_issuers", {"reason": "no XRPL issuer attributed"}

    toml, err = _fetch_toml(toml_url)
    if err:
        print(f"  FORWARD fetch: FAIL ({err})")
        return "labeled_toml_missing", {"reason": err, "toml_url": toml_url}
    print(f"  FORWARD fetch: OK (parsed)")

    forward_hits = []
    forward_misses = []
    for issuer_addr, symbol, currency_hex in issuers:
        entry = _find_issuer_entry(toml, currency_hex, issuer_addr)
        if entry is None:
            # Also try uppercase-hex normalization
            entry = _find_issuer_entry(toml, currency_hex.upper(), issuer_addr)
        if entry is None:
            forward_misses.append((issuer_addr, symbol))
        else:
            forward_hits.append((issuer_addr, symbol, entry))
    print(f"  FORWARD match: {len(forward_hits)}/{len(issuers)} issuers found in toml")
    if forward_misses:
        for m in forward_misses:
            print(f"    - MISS: {m[0]} ({m[1]}) not present in [[ISSUERS]]/[[TOKENS]]")

    if forward_misses:
        return "labeled_toml_missing_issuer", {
            "missing_issuers": [m[0] for m in forward_misses],
            "toml_url": toml_url,
        }

    # Reverse: each issuer's Domain must resolve to `domain`.
    reverse_ok = True
    reverse_details = []
    for issuer_addr, symbol, _curr in issuers:
        decoded_domain, derr = _fetch_xrpl_domain(issuer_addr)
        if derr:
            reverse_ok = False
            reverse_details.append({
                "issuer": issuer_addr, "symbol": symbol,
                "error": derr, "ok": False,
            })
            print(f"  REVERSE {issuer_addr} ({symbol}): FAIL ({derr})")
            continue
        matches = _domain_matches_toml_url(decoded_domain, toml_url)
        reverse_details.append({
            "issuer": issuer_addr, "symbol": symbol,
            "on_ledger_domain": decoded_domain, "ok": matches,
        })
        if matches:
            print(f"  REVERSE {issuer_addr} ({symbol}): OK (Domain={decoded_domain})")
        else:
            reverse_ok = False
            print(f"  REVERSE {issuer_addr} ({symbol}): FAIL (Domain={decoded_domain} != {domain})")

    if not reverse_ok:
        return "labeled_domain_mismatch", {"reverse": reverse_details}

    return "verified", {
        "forward_hits": len(forward_hits),
        "reverse": reverse_details,
        "toml_url": toml_url,
    }


def main() -> int:
    print("Family verification — two-way TOML standard\n" + "=" * 45)
    results = []
    with db.pg_connect() as conn:
        for family_slug, domain, symbols in FAMILIES:
            issuers = _issuers_for_family(conn, family_slug, symbols)
            verdict, details = verify_family(family_slug, domain, issuers)
            results.append((family_slug, verdict, details))

    print("\n=== SUMMARY ===")
    verified_slugs = []
    for family_slug, verdict, details in results:
        target = "verified" if verdict == "verified" else "labeled"
        print(f"  {family_slug}: {verdict} → attestation_level='{target}'")
        if target == "verified":
            verified_slugs.append(family_slug)

    return 0


if __name__ == "__main__":
    sys.exit(main())
