#!/usr/bin/env python3
"""D1 v2 punch-list #4: row-level TOML mapping for the top-100 pairs.

Per top-100 pair, emit:
  rank, currency (display + hex), issuer, trades_30d, domain_ascii,
  domain_status, toml_status (found/missing/blocked), toml_shape
  (canonical/non_canonical/none), and a summary tier the design can use:
  VERIFIED (canonical), SELF_DESCRIBED (non-canonical), DOMAIN_ONLY,
  ANONYMOUS.
"""
import json

TOP100 = json.load(open("/Users/charliebruce/xrpl_test/scratch/d1_unlabeled_top100.json"))
DOMAIN = json.load(open("/Users/charliebruce/xrpl_test/scratch/d1_domain_decode.json"))
TOML = json.load(open("/Users/charliebruce/xrpl_test/scratch/d1_toml_sweep.json"))

OUT_PATH = "/Users/charliebruce/xrpl_test/scratch/d1_v2_row_level.json"

domain_by_issuer = {r["issuer"]: r for r in DOMAIN["rows"]}
toml_by_issuer = {r["issuer"]: r for r in TOML["rows"]}


def hex_to_display(cur: str) -> str:
    if not cur or len(cur) != 40:
        return cur
    try:
        s = bytes.fromhex(cur).decode("ascii", errors="replace").rstrip("\x00").strip()
        if s and all(c.isprintable() for c in s):
            return s
    except ValueError:
        pass
    return cur


rows = []
firstledger_pattern_count = 0
canonical_count = 0
non_canonical_count = 0
domain_only_count = 0
anonymous_count = 0

for r in TOP100["rows"]:
    issuer = r["issuer"]
    d = domain_by_issuer.get(issuer) or {}
    t = toml_by_issuer.get(issuer) or {}

    has_domain_field = bool(d.get("domain_hex"))
    domain_ascii = d.get("domain_ascii")
    resolves = d.get("resolves", False)
    ascii_valid = d.get("ascii_valid", False)

    toml_found = t.get("toml_found", False)
    canonical = t.get("canonical_declares", False)
    noncanonical = t.get("noncanonical_declares", False)

    if not has_domain_field:
        domain_status = "NO_DOMAIN_FIELD"
    elif not ascii_valid:
        domain_status = "INVALID_HOSTNAME"
    elif not resolves:
        domain_status = "NOT_RESOLVING"
    else:
        domain_status = "RESOLVING"

    if toml_found and canonical:
        tier = "VERIFIED"
        toml_shape = "canonical"
        canonical_count += 1
    elif toml_found and noncanonical:
        tier = "SELF_DESCRIBED"
        toml_shape = "non_canonical"
        non_canonical_count += 1
        if domain_ascii and domain_ascii.endswith(".toml.firstledger.net"):
            firstledger_pattern_count += 1
    elif has_domain_field and ascii_valid and resolves:
        tier = "DOMAIN_ONLY"
        toml_shape = "no_toml"
        domain_only_count += 1
    else:
        tier = "ANONYMOUS"
        toml_shape = "n/a"
        anonymous_count += 1

    rows.append({
        "rank": r["rank"],
        "currency_hex": r["currency"],
        "currency_display": hex_to_display(r["currency"]),
        "issuer": issuer,
        "trades_30d": r["trades_30d"],
        "domain_status": domain_status,
        "domain_ascii": domain_ascii,
        "toml_found": toml_found,
        "toml_shape": toml_shape,
        "tier": tier,
        "toml_declares_via_accounts": canonical,
        "toml_declares_via_issuers_or_tokens": noncanonical and not canonical,
    })

out = {
    "generated_at": TOP100["generated_at"],
    "summary": {
        "total_pairs": len(rows),
        "tier_counts": {
            "VERIFIED": canonical_count,
            "SELF_DESCRIBED": non_canonical_count,
            "DOMAIN_ONLY": domain_only_count,
            "ANONYMOUS": anonymous_count,
        },
        "firstledger_generated_count": firstledger_pattern_count,
    },
    "rows": rows,
}
with open(OUT_PATH, "w") as f:
    json.dump(out, f, indent=2)

print(f"Written: {OUT_PATH}")
print()
print(f"{'#':>3} {'currency':<12} {'issuer':<40} {'trades':>8} {'tier':<15} {'shape':<15} {'domain'}")
for row in rows[:25]:
    print(
        f"{row['rank']:>3} {row['currency_display'][:12]:<12} "
        f"{row['issuer'][:40]:<40} {row['trades_30d']:>8,} "
        f"{row['tier']:<15} {row['toml_shape']:<15} "
        f"{row['domain_ascii'] or ''}"
    )
print(f"... (75 more, full list in JSON)")
print()
print(f"Tier counts: VERIFIED={canonical_count} SELF_DESCRIBED={non_canonical_count} DOMAIN_ONLY={domain_only_count} ANONYMOUS={anonymous_count}")
print(f"Firstledger-generated pattern: {firstledger_pattern_count}")
