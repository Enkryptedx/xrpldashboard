# D1 — Data Results (JJ execution)

Generated 2026-07-03 15:55 UTC on the xrpldashboard prod dataset.

Measure = **trade_count** (`token_volume.volume_xrp` is unpopulated by the
walker as of this run; the live /tokens page ranks by trade_count and we
match that measure).

## Universe

| Metric | Value |
|---|---|
| Distinct (currency, issuer) pairs traded in the last 30d | 7,163 |
| Pairs in `token_names.json` | 28 |
| Unlabeled pairs | 7,135 |
| Total 30d trades | 7,069,512 |
| Trades in labeled pairs | 1,033,258 (14.6%) |
| Trades in unlabeled pairs | 6,036,254 (85.4%) |
| Top-100 unlabeled share of unlabeled trades | 82.4% |
| Top-100 unlabeled share of TOTAL trades | 70.3% |

## Pull 1 — Top-100 unlabeled pairs by 30d trade count

Top-10 preview (full table in `scratch/d1_unlabeled_top100.json`):

| # | currency | issuer | trades 30d | share of unlabeled | share of total |
|---:|---|---|---:|---:|---:|
| 1 | BITx (42495478…) | `rBitcoiNXev8VoVxV7pwoQx1sSfonVP9i3` | 898,749 | 14.9% | 12.7% |
| 2 | C9BR (43394252…) | `rBCf85rmfGBTAQrqyiNv5fxTvJ5A4EQXWg` | 517,237 | 8.6% | 7.3% |
| 3 | ARK | `rf5Jzzy6oAFBJjLhokha1v8pXVgYYjee3b` | 255,112 | 4.2% | 3.6% |
| 4 | ASC | `r3qWgpz2ry3BhcRJ8JE6rxM8esrfhuKp4R` | 236,802 | 3.9% | 3.3% |
| 5 | Xoge (586F6765…) | `rJMtvf5B3GbuFMrqybh5wYVXEH4QE8VyU1` | 208,810 | 3.5% | 3.0% |
| 6 | BEAR (42454152…) | `rBEARGUAsyu7tUw53rufQzFdWmJHpJEqFW` | 193,816 | 3.2% | 2.7% |
| 7 | ZERPS (5A455250…) | `rJztCAZEQvKcMSQTpKUZRJJjA11aUTk9Aw` | 175,304 | 2.9% | 2.5% |
| 8 | PLX | `rGLEgQdktoN4Be5thhk6seg1HifGPBxY5Q` | 146,602 | 2.4% | 2.1% |
| 9 | PLR | `rNSYhWLhuHvmURwWbJPBKZMSPsyG5Qek17` | 135,229 | 2.2% | 1.9% |
| 10 | welth (77656C74…) | `rDSEfDcJ5UzQyK3UNmUDGvZT1Z7cmoiFv7` | 131,444 | 2.2% | 1.9% |


## Pull 2 — AccountRoot.Domain decode (xrpl-py `account_info`)

| Metric | Count | % of 97 unique issuers |
|---|---:|---:|
| Issuers probed | 97 | 100% |
| With Domain field set | 73 | 75.3% |
| With valid hostname (RFC 1035 gate) | 66 | 68.0% |
| With resolving hostname (DNS A/AAAA) | 65 | 67.0% |

**Notes / gotchas:**
- 24 issuers have no Domain field at all — permanently un-attestable via this channel until minter updates AccountRoot.
- Handful of decode failures: `https://xcaliburxrp.com` was set as a URL not a hostname (invalid), gets rejected by the safety gate.
- 100 pairs → 97 unique issuers because a few issuers mint multiple currency codes.

## Pull 3 — TOML sweep on resolving domains

| Metric | Count | % of 65 resolving |
|---|---:|---:|
| Domains probed | 65 | 100% |
| TOML fetch succeeded (HTTP 200 + parseable) | 54 | 83.1% |
| Canonical attestation closed (`[[ACCOUNTS]]`) | 1 | 1.5% |
| Non-canonical attestation closed (`[[ISSUERS]]`/`[[TOKENS]]`) | 54 | 83.1% |

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
| Issuers probed | 97 | 100% |
| With XRPScan `accountName.name` set | 28 | 28.9% |
| With `verified=true` label | 14 | 14.4% |

**Licensing note:** XRPScan ToS (fetched 2026-07-03) requires attribution
("Source: XRPScan") for shared content and restricts "bulk reproduction,
resale, or redistribution in commercial or enterprise contexts" without
written consent. xrpldashboard is a free public site (arguably
non-commercial), so attribution is the primary requirement; whether
systematic enrichment falls under "bulk redistribution" is a gray zone.

**Sample rows (non-verified, from the top-10 sample kept):**

| issuer | XRPScan name | domain | verified |
|---|---|---|:-:|
| `r3dVizzUAS3U29WKaaSALqkieytA2LCoRe` | XCORE | | — |
| `r3qWgpz2ry3BhcRJ8JE6rxM8esrfhuKp4R` | Reaper | | — |
| `r9sH6YEVRyg8uYaKfyk1EfH36Lfq7a8PUD` | Terry Toto | | — |
| `rB3y9EPnq1ZrZP3aXgfyfdXQThzdXMrLMc` | USDB | | — |
| `rBEARGUAsyu7tUw53rufQzFdWmJHpJEqFW` | BEAR | | — |
| `rBitcoiNXev8VoVxV7pwoQx1sSfonVP9i3` | BITx | | — |
| `rDKJWtrPqKiMBzK9FTVvebMVec7FJtJLx6` | Horizon | | — |
| `rGG3wQ4kUzd7Jnmk1n5NWPZjjut62kCBfC` | ARMY | | — |
| `rLBnhMjV6ifEHYeV4gaS6jPKerZhQddFxW` | eolas TRSRY | | — |
| `rM8hNqA3jRJ5Zgp3Xf3xzdZcx2G37guiZk` | XRP Healthcare | | — |


## Enrichment audit summary (JJ data → Fable table)

| Source | Newly-nameable **pairs** (of top-100) | Newly-nameable **trades** (share of total 30d) | Notes / gotchas |
|---|---:|---:|---|
| Domain field set (no attestation) | 75 | — | signal only; visitor label like "issuer says its domain is X" |
| TOML — canonical (`[[ACCOUNTS]]`) | 1 | 0.1% | strict xrpl.org spec; only 1 hit |
| TOML — non-canonical (`[[ISSUERS]]`/`[[TOKENS]]`) | 55 | 25.3% | pragmatic; firstledger-generated dominates |
| Bithomp public labels | 🚫 blocked | 🚫 blocked | ToS prohibits re-export without signed agreement |
| XRPScan public labels | 30 | 28.0% | attribution required; bulk-redist gray-zone |

## Honest floor — permanently-unnameable share of 30d /tokens trades

Failing ALL of (not-in-`token_names.json` + no resolving Domain + no TOML
attestation + no XRPScan label). Bithomp excluded per licensing.

| Reading | Top-100 pairs failing all | Trades in those pairs | Floor (top-100 only) | Floor (top-100 + assume tail also fails) |
|---|---:|---:|---:|---:|
| **STRICT** (canonical TOML only) | 70 | 2,992,280 | **42.3%** | **57.4%** |
| **PRAGMATIC** (canonical OR non-canonical TOML) | 27 | 1,521,115 | **21.5%** | **36.6%** |

Recommended reading for the hero bar: **PRAGMATIC lower bound = 21.5%**
(safe truth-first number — pairs beyond top-100 could yet be nameable via
future enrichment, so we do not include them in the floor).

Top-10 permanently-unnameable pairs (lenient definition), sorted by
30d trades — these are the wallets we can't attach a name to under any
source we can cite:

| currency | issuer | trades 30d | domain status | note |
|---|---|---:|---|---|
| C9BR (43394252…) | `rBCf85rmfGBTAQrqyiNv5fxTvJ5A4EQXWg` | 517,237 | NO_DOMAIN_FIELD | no on-chain domain |
| Xoge (586F6765…) | `rJMtvf5B3GbuFMrqybh5wYVXEH4QE8VyU1` | 208,810 | NO_DOMAIN_FIELD | no on-chain domain |
| PLX | `rGLEgQdktoN4Be5thhk6seg1HifGPBxY5Q` | 146,602 | NO_DOMAIN_FIELD | no on-chain domain |
| PLR | `rNSYhWLhuHvmURwWbJPBKZMSPsyG5Qek17` | 135,229 | INVALID_HOSTNAME | invalid: `https://xrpillars.com/` |
| BOX | `rhy4FUHtXrMZhbkBfeYvDv4nz6R7M4cu1t` | 97,941 | NO_DOMAIN_FIELD | no on-chain domain |
| EverBurn (45766572…) | `rhUHDGG5po5Dg6oxtaodMPTR4xytToSL1Y` | 83,654 | INVALID_HOSTNAME | invalid: `https://x.com/ebt_xrpl` |
| XUSD (58555344…) | `rpsMREjEPnBMejecRyTUyn2CEdNyLj7nTp` | 39,803 | NO_DOMAIN_FIELD | no on-chain domain |
| ARMY (41524D59…) | `r319FqohpKLwjtcV2mosyC5sy125fDk4uH` | 28,149 | NO_DOMAIN_FIELD | no on-chain domain |
| XQK | `rHKrPGdpaqNRqRvmsiqQhD6azqc4npWoLC` | 25,239 | NO_DOMAIN_FIELD | no on-chain domain |
| BXE | `rM1J2Mc2eCSFpCz5QXxhDG2KWkGQWgy87r` | 24,318 | NO_DOMAIN_FIELD | no on-chain domain |


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
