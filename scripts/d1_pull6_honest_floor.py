#!/usr/bin/env python3
"""D1 pull 6: honest-floor math + write D1_DATA_RESULTS.md.

For each of the top-100 unlabeled issuer,currency pairs, decide whether
it fails ALL five criteria (permanently unnameable at scale):
  1. Not in token_names.json  (by construction — the selection filter)
  2. Issuer's AccountRoot.Domain is empty OR not a resolving hostname
  3. No xrp-ledger.toml at that domain (canonical OR non-canonical)
  4. Not labeled by Bithomp (BLOCKED by ToS — treated as unavailable)
  5. Not labeled by XRPScan

Honest floor % = sum(trades_30d for pairs failing all) / total_trades_30d.

We report two versions:
  - STRICT (canonical): only [[ACCOUNTS]]-declared attestation counts
  - PRAGMATIC: also accepts firstledger-style [[ISSUERS]]/[[TOKENS]]

Then writes /Users/charliebruce/xrpl_test/D1_DATA_RESULTS.md.
"""
import json

TOP100 = json.load(open("/Users/charliebruce/xrpl_test/scratch/d1_unlabeled_top100.json"))
DOMAIN = json.load(open("/Users/charliebruce/xrpl_test/scratch/d1_domain_decode.json"))
TOML = json.load(open("/Users/charliebruce/xrpl_test/scratch/d1_toml_sweep.json"))
BITHOMP = json.load(open("/Users/charliebruce/xrpl_test/scratch/d1_bithomp.json"))
XRPSCAN = json.load(open("/Users/charliebruce/xrpl_test/scratch/d1_xrpscan.json"))

OUT_MD = "/Users/charliebruce/xrpl_test/D1_DATA_RESULTS.md"

# Index by issuer.
domain_by_issuer = {r["issuer"]: r for r in DOMAIN["rows"]}
toml_by_issuer = {r["issuer"]: r for r in TOML["rows"]}
xrpscan_labeled = set()

# XRPScan pull only kept 10 samples plus counts — we know N labeled but not
# WHICH. We need the full labeled set for per-row logic. Re-derive from
# the samples plus counts by re-probing? No — probe covered every issuer,
# we just didn't store per-issuer flags. Add that now via a quick re-scan.
# Actually: the honest floor formula needs per-issuer XRPScan hits. So
# we run one more quick pass, stored to memory only, then compute.
import ssl
import time
import urllib.request

import certifi

SSL_CTX = ssl.create_default_context(cafile=certifi.where())
UA = "xrpldashboard-d1/1.0 (+https://xrpldashboard.com)"

def xrpscan_hit(issuer: str) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            f"https://api.xrpscan.com/api/v1/account/{issuer}",
            headers={"User-Agent": UA},
        )
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        name = ((data.get("accountName") or {}).get("name") or "").strip()
        return (bool(name), name)
    except Exception:
        return (False, "")

print("Re-scanning XRPScan for per-issuer flags…")
xrpscan_flag = {}
xrpscan_labels_sample = []
issuers = sorted({r["issuer"] for r in TOP100["rows"]})
for i, issuer in enumerate(issuers, 1):
    hit, name = xrpscan_hit(issuer)
    xrpscan_flag[issuer] = hit
    if hit and len(xrpscan_labels_sample) < 10:
        xrpscan_labels_sample.append({"issuer": issuer, "name": name})
    if i % 20 == 0:
        print(f"  {i}/{len(issuers)}")
    time.sleep(0.1)

# Also derive: does the on-chain Domain point to a domain whose TOML
# was found + whose TOML declares this issuer (canonical or lenient)?
def toml_status(issuer):
    r = toml_by_issuer.get(issuer)
    if not r:
        return ("NO_DOMAIN", False, False)
    if not r["toml_found"]:
        return ("NO_TOML", False, False)
    canon = r.get("canonical_attestation_closed") or False
    lenient = r.get("noncanonical_attestation_closed") or False
    return ("TOML_FOUND", canon, lenient)

def domain_status(issuer):
    r = domain_by_issuer.get(issuer)
    if not r:
        return "NO_ISSUER_ROW"
    if not r["domain_hex"]:
        return "NO_DOMAIN_FIELD"
    if not r["ascii_valid"]:
        return "INVALID_HOSTNAME"
    if not r["resolves"]:
        return "NOT_RESOLVING"
    return "RESOLVING"

# Iterate top-100 pairs.
strict_unnameable_trades = 0
lenient_unnameable_trades = 0
strict_unnameable_count = 0
lenient_unnameable_count = 0
per_pair = []
for row in TOP100["rows"]:
    issuer = row["issuer"]
    trades = row["trades_30d"]
    dstat = domain_status(issuer)
    _, canon, lenient = toml_status(issuer)
    xrpscan_hit_flag = xrpscan_flag.get(issuer, False)

    # STRICT: has canonical TOML attestation OR xrpscan label => nameable.
    strict_fail_all = (
        dstat != "RESOLVING" or not canon
    ) and not xrpscan_hit_flag
    # PRAGMATIC: canonical OR non-canonical TOML attestation OR xrpscan.
    lenient_fail_all = (
        dstat != "RESOLVING" or not lenient
    ) and not xrpscan_hit_flag

    if strict_fail_all:
        strict_unnameable_trades += trades
        strict_unnameable_count += 1
    if lenient_fail_all:
        lenient_unnameable_trades += trades
        lenient_unnameable_count += 1
    per_pair.append({
        "rank": row["rank"],
        "currency": row["currency"],
        "issuer": issuer,
        "trades_30d": trades,
        "domain_status": dstat,
        "domain_ascii": (domain_by_issuer.get(issuer) or {}).get("domain_ascii"),
        "toml_canon_attested": canon,
        "toml_lenient_attested": lenient,
        "xrpscan_labeled": xrpscan_hit_flag,
        "strict_fail_all": strict_fail_all,
        "lenient_fail_all": lenient_fail_all,
    })

total_30d_trades = TOP100["totals"]["total_trades_30d"]
top100_trades = sum(r["trades_30d"] for r in TOP100["rows"])
unlabeled_trades = TOP100["totals"]["unlabeled_trades_30d"]

# Honest floor: strict/lenient failing trades within top-100 as a share
# of TOTAL /tokens 30d trades. This is the floor over top-100; there is
# a tail beyond top-100 that is by definition also unlabeled (7,035
# additional pairs). The brief asks for the % of 30d /tokens trade
# volume that meets ALL failing criteria — the top-100 covers 82.4% of
# unlabeled trades. We report both:
#   - floor_within_top100_of_total: safe lower bound
#   - floor_tail_conservative: assume 100% of tail also fails, upper est
tail_trades = unlabeled_trades - top100_trades  # trades in unlabeled pairs 101…7135
strict_floor_low = strict_unnameable_trades / total_30d_trades if total_30d_trades else 0
strict_floor_hi = (strict_unnameable_trades + tail_trades) / total_30d_trades if total_30d_trades else 0
lenient_floor_low = lenient_unnameable_trades / total_30d_trades if total_30d_trades else 0
lenient_floor_hi = (lenient_unnameable_trades + tail_trades) / total_30d_trades if total_30d_trades else 0

# Enrichment potential (cells the top-100 unlocks under each source):
domain_resolving_count = sum(1 for p in per_pair if p["domain_status"] == "RESOLVING")
toml_lenient_count = sum(1 for p in per_pair if p["toml_lenient_attested"])
toml_canon_count = sum(1 for p in per_pair if p["toml_canon_attested"])
xrpscan_count = sum(1 for p in per_pair if p["xrpscan_labeled"])
toml_lenient_trades = sum(p["trades_30d"] for p in per_pair if p["toml_lenient_attested"])
toml_canon_trades = sum(p["trades_30d"] for p in per_pair if p["toml_canon_attested"])
xrpscan_trades = sum(p["trades_30d"] for p in per_pair if p["xrpscan_labeled"])

# Top 20 nameable via lenient TOML (to name and shame in the MD).
lenient_hits = sorted(
    [p for p in per_pair if p["toml_lenient_attested"] and not p["toml_canon_attested"]],
    key=lambda p: -p["trades_30d"],
)[:10]
xrpscan_hits = sorted(
    [p for p in per_pair if p["xrpscan_labeled"]],
    key=lambda p: -p["trades_30d"],
)[:10]
failing_hits = sorted(
    [p for p in per_pair if p["lenient_fail_all"]],
    key=lambda p: -p["trades_30d"],
)[:10]


def hex_or_ascii(cur: str) -> str:
    if cur and len(cur) == 40 and all(c in "0123456789ABCDEFabcdef" for c in cur):
        try:
            decoded = bytes.fromhex(cur).decode("ascii", errors="replace").rstrip("\x00").strip()
            if decoded and all(c.isprintable() for c in decoded):
                return f"{decoded} ({cur[:8]}…)"
        except ValueError:
            pass
    return cur


def fmt_pct(x): return f"{x*100:.1f}%"


md = f"""# D1 — Data Results (JJ execution)

Generated {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())} on the xrpldashboard prod dataset.

Measure = **trade_count** (`token_volume.volume_xrp` is unpopulated by the
walker as of this run; the live /tokens page ranks by trade_count and we
match that measure).

## Universe

| Metric | Value |
|---|---|
| Distinct (currency, issuer) pairs traded in the last 30d | {TOP100["totals"]["total_pairs"]:,} |
| Pairs in `token_names.json` | {TOP100["totals"]["total_pairs"] - TOP100["totals"]["unlabeled_pairs"]:,} |
| Unlabeled pairs | {TOP100["totals"]["unlabeled_pairs"]:,} |
| Total 30d trades | {total_30d_trades:,} |
| Trades in labeled pairs | {TOP100["totals"]["labeled_trades_30d"]:,} ({fmt_pct(TOP100["totals"]["labeled_share_of_total"])}) |
| Trades in unlabeled pairs | {unlabeled_trades:,} ({fmt_pct(TOP100["totals"]["unlabeled_share_of_total"])}) |
| Top-100 unlabeled share of unlabeled trades | {fmt_pct(TOP100["totals"]["top100_share_of_unlabeled"])} |
| Top-100 unlabeled share of TOTAL trades | {fmt_pct(TOP100["totals"]["top100_share_of_total"])} |

## Pull 1 — Top-100 unlabeled pairs by 30d trade count

Top-10 preview (full table in `scratch/d1_unlabeled_top100.json`):

| # | currency | issuer | trades 30d | share of unlabeled | share of total |
|---:|---|---|---:|---:|---:|
"""
for r in TOP100["rows"][:10]:
    md += f"| {r['rank']} | {hex_or_ascii(r['currency'])} | `{r['issuer']}` | {r['trades_30d']:,} | {fmt_pct(r['share_of_unlabeled_trades'])} | {fmt_pct(r['share_of_total_trades'])} |\n"

md += f"""

## Pull 2 — AccountRoot.Domain decode (xrpl-py `account_info`)

| Metric | Count | % of 97 unique issuers |
|---|---:|---:|
| Issuers probed | {DOMAIN["issuer_count"]} | 100% |
| With Domain field set | {DOMAIN["with_domain_field"]} | {fmt_pct(DOMAIN["with_domain_field"]/DOMAIN["issuer_count"])} |
| With valid hostname (RFC 1035 gate) | {DOMAIN["with_valid_hostname"]} | {fmt_pct(DOMAIN["with_valid_hostname"]/DOMAIN["issuer_count"])} |
| With resolving hostname (DNS A/AAAA) | {DOMAIN["with_resolving_hostname"]} | {fmt_pct(DOMAIN["with_resolving_hostname"]/DOMAIN["issuer_count"])} |

**Notes / gotchas:**
- 24 issuers have no Domain field at all — permanently un-attestable via this channel until minter updates AccountRoot.
- Handful of decode failures: `https://xcaliburxrp.com` was set as a URL not a hostname (invalid), gets rejected by the safety gate.
- 100 pairs → 97 unique issuers because a few issuers mint multiple currency codes.

## Pull 3 — TOML sweep on resolving domains

| Metric | Count | % of 65 resolving |
|---|---:|---:|
| Domains probed | {TOML["domains_probed"]} | 100% |
| TOML fetch succeeded (HTTP 200 + parseable) | {TOML["toml_found"]} | {fmt_pct(TOML["toml_found"]/TOML["domains_probed"])} |
| Canonical attestation closed (`[[ACCOUNTS]]`) | {TOML["canonical_attestation_closed"]} | {fmt_pct(TOML["canonical_attestation_closed"]/TOML["domains_probed"])} |
| Non-canonical attestation closed (`[[ISSUERS]]`/`[[TOKENS]]`) | {TOML["noncanonical_attestation_closed"]} | {fmt_pct(TOML["noncanonical_attestation_closed"]/TOML["domains_probed"])} |

**Gotcha (critical):** 53 of the 54 successful TOMLs use a non-canonical
`[[ISSUERS]]` + `[[TOKENS]]` section shape rather than the xrpl.org spec's
`[[ACCOUNTS]]` + `[[CURRENCIES]]`. Nearly all of these are auto-generated
by firstledger.net's launcher (subdomain pattern `*.toml.firstledger.net`).

The ONE canonical hit is `tokens.brazacripto.com.br` (Brazilian issuer).

Interpretation for Fable: this is a **spec vs. reality gap**. If we accept
non-canonical shape, TOML attestation covers 54/65 resolving domains — most
of the top-100 by trade count. If we hold the strict xrpl.org line, only
1 of 65 attests. Recommend the honest floor be calculated BOTH ways and
the hero use the pragmatic reading (with a note on the standards question).

## Pull 4 — Bithomp public label cross-ref  🚫 LEGALLY BLOCKED

Bithomp Terms and Conditions (fetched 2026-07-03) explicitly prohibit
"duplicate, reproduce, copy, store, derive from, or translate any Data"
and "selling, renting, leasing, sublicensing, redistributing, or
syndicating access to the Bithomp API or any part thereof" without a
signed Executed Agreement.

Because re-exporting labels to xrpldashboard visitors is a covered use,
we do NOT scrape and we do NOT store Bithomp labels here. Un-auth probe
confirmed the API rejects requests without a key (HTTP 403).

**Recommendation:** exclude from the enrichment pipeline until a signed
agreement is in place. Do not scrape the HTML explorer as a workaround —
the data restriction applies regardless of extraction channel.

## Pull 5 — XRPScan public label cross-ref

| Metric | Count | % of 97 issuers |
|---|---:|---:|
| Issuers probed | {XRPSCAN["issuers_probed"]} | 100% |
| With XRPScan `accountName.name` set | {XRPSCAN["labeled"]} | {fmt_pct(XRPSCAN["labeled_share"])} |
| With `verified=true` label | {XRPSCAN["verified_labels"]} | {fmt_pct(XRPSCAN["verified_share"])} |

**Licensing note:** XRPScan ToS (fetched 2026-07-03) requires attribution
("Source: XRPScan") for shared content and restricts "bulk reproduction,
resale, or redistribution in commercial or enterprise contexts" without
written consent. xrpldashboard is a free public site (arguably
non-commercial), so attribution is the primary requirement; whether
systematic enrichment falls under "bulk redistribution" is a gray zone.

**Sample rows (non-verified, from the top-10 sample kept):**

| issuer | XRPScan name | domain | verified |
|---|---|---|:-:|
"""
for s in xrpscan_labels_sample:
    md += f"| `{s['issuer']}` | {s['name']} | | — |\n"

md += f"""

## Enrichment audit summary (JJ data → Fable table)

| Source | Newly-nameable **pairs** (of top-100) | Newly-nameable **trades** (share of total 30d) | Notes / gotchas |
|---|---:|---:|---|
| Domain field set (no attestation) | {sum(1 for p in per_pair if (domain_by_issuer.get(p['issuer']) or dict()).get('domain_hex'))} | — | signal only; visitor label like "issuer says its domain is X" |
| TOML — canonical (`[[ACCOUNTS]]`) | {toml_canon_count} | {fmt_pct(toml_canon_trades/total_30d_trades)} | strict xrpl.org spec; only 1 hit |
| TOML — non-canonical (`[[ISSUERS]]`/`[[TOKENS]]`) | {toml_lenient_count} | {fmt_pct(toml_lenient_trades/total_30d_trades)} | pragmatic; firstledger-generated dominates |
| Bithomp public labels | 🚫 blocked | 🚫 blocked | ToS prohibits re-export without signed agreement |
| XRPScan public labels | {xrpscan_count} | {fmt_pct(xrpscan_trades/total_30d_trades)} | attribution required; bulk-redist gray-zone |

## Honest floor — permanently-unnameable share of 30d /tokens trades

Failing ALL of (not-in-`token_names.json` + no resolving Domain + no TOML
attestation + no XRPScan label). Bithomp excluded per licensing.

| Reading | Top-100 pairs failing all | Trades in those pairs | Floor (top-100 only) | Floor (top-100 + assume tail also fails) |
|---|---:|---:|---:|---:|
| **STRICT** (canonical TOML only) | {strict_unnameable_count} | {strict_unnameable_trades:,} | **{fmt_pct(strict_floor_low)}** | **{fmt_pct(strict_floor_hi)}** |
| **PRAGMATIC** (canonical OR non-canonical TOML) | {lenient_unnameable_count} | {lenient_unnameable_trades:,} | **{fmt_pct(lenient_floor_low)}** | **{fmt_pct(lenient_floor_hi)}** |

Recommended reading for the hero bar: **PRAGMATIC lower bound = {fmt_pct(lenient_floor_low)}**
(safe truth-first number — pairs beyond top-100 could yet be nameable via
future enrichment, so we do not include them in the floor).

Top-10 permanently-unnameable pairs (lenient definition), sorted by
30d trades — these are the wallets we can't attach a name to under any
source we can cite:

| currency | issuer | trades 30d | domain status | note |
|---|---|---:|---|---|
"""
for p in failing_hits:
    dstat = p["domain_status"]
    note = ""
    if dstat == "NO_DOMAIN_FIELD":
        note = "no on-chain domain"
    elif dstat == "NOT_RESOLVING":
        note = f"domain `{p['domain_ascii']}` does not resolve"
    elif dstat == "INVALID_HOSTNAME":
        note = f"invalid: `{p['domain_ascii']}`"
    elif dstat == "RESOLVING":
        note = "domain resolves but TOML missing/silent + no XRPScan label"
    md += f"| {hex_or_ascii(p['currency'])} | `{p['issuer']}` | {p['trades_30d']:,} | {dstat} | {note} |\n"

md += f"""

## Methodology gaps flagged for Fable

1. **Volume measure mismatch.** The brief asks for share of 30d "trade
   volume." Our `token_volume.volume_xrp` column is populated as 0 by
   the walker. We use `trade_count` as the honest available measure and
   note that a single trade of 1 XRP counts the same as one of 1M XRP.
   If XRP-value share matters, we need a walker fix before Fable answers
   the "% of volume" version of the honest floor.

2. **Tail beyond top-100.** 7,035 additional unlabeled pairs generate
   17.6% of unlabeled trades. We do not assume they all fail — the
   floor reported is the *safe lower bound* (top-100 failing pairs / total).

3. **Firstledger-generated TOMLs.** These non-canonical shapes come from
   an automated tokeniser (not manual issuer sign-off). Whether they
   count as "attestation" is a policy call, not a data call. Fable
   should decide the standard we hold the hero to.

4. **XRPScan bulk-redistribution gray zone.** Attribution is clear;
   whether we can systematically enrich from XRPScan is a call for
   Charlie + XRPScan.

5. **Bithomp legally unavailable** unless a signed agreement is signed.
   Excluded from the pipeline.

## Files

- Top-100 raw: `scratch/d1_unlabeled_top100.json`
- Domain decode: `scratch/d1_domain_decode.json`
- TOML sweep: `scratch/d1_toml_sweep.json`
- Bithomp audit: `scratch/d1_bithomp.json`
- XRPScan probe: `scratch/d1_xrpscan.json`
"""

with open(OUT_MD, "w") as f:
    f.write(md)

print(f"Written: {OUT_MD}")
print()
print(f"STRICT floor (top-100 only): {fmt_pct(strict_floor_low)}")
print(f"STRICT floor (with tail):    {fmt_pct(strict_floor_hi)}")
print(f"PRAGMATIC floor (top-100):   {fmt_pct(lenient_floor_low)}")
print(f"PRAGMATIC floor (with tail): {fmt_pct(lenient_floor_hi)}")
