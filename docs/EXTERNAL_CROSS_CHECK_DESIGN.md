# External cross-check design — nightly sanity vs XRPScan + Bithomp

**Standing filing.** Established 2026-08-16.

**Purpose**: catch the class of failure where our walker returns
plausible-but-wrong data. Signature verification + snapshot chain-link
catches TAMPERING with our published data; they don't catch our own
walker producing bad numbers in the first place. External free APIs
that publish the same numbers give us a nightly sanity check.

**Cost**: 2 HTTP calls/night. Free tier. No API keys.

**Ships when**: after b2 lands + Wave 1 walker cutover done. Not
urgent; nothing has been reported wrong. But this is exactly the
tripwire that would catch subtle drift the moment it starts.

---

## What we cross-check

Two numbers, both currently displayed on our /methodology and homepage:

### 1. Total XRP burned

- **Our value**: `signed_snapshots.body -> 'xrp' -> 'burned_lifetime'`
  (or wherever we compute it — need to confirm exact key from
  `docs/CLAIMS.yaml`).
- **XRPScan**: `GET https://api.xrpscan.com/api/v1/metrics` → look for
  `totalCoins` or similar; XRP burned = 100B - totalCoins.
- **Bithomp**: `GET https://bithomp.com/api/v2/statistics` → similar
  denominator field.

Cross-check: compute both externals' "burned" number. If our snapshot
and both externals agree to within ±10,000 XRP (fee dust tolerance),
green. If they disagree, page.

**Failure mode caught**: a walker producing garbage burned-XRP total
because of a JSON parsing bug, a walker reading from the wrong
account, or a walker hitting a stale rippled cache.

### 2. Total RLUSD supply

- **Our value**: `rlusd_refresher_walker` writes to whichever table
  (need to confirm — grep for `rlusd_supply` or similar).
- **XRPScan**: `GET https://api.xrpscan.com/api/v1/account/{RLUSD_ISSUER}/obligations`
- **Bithomp**: `GET https://bithomp.com/api/v2/objects/rlusd` (path TBD).

Cross-check: same shape. ±0.01 RLUSD tolerance (rounding).

**Failure mode caught**: rlusd_refresher walker producing wrong
supply because of gateway_balances parsing regression, wrong issuer
address, or cached response reuse.

---

## Design shape (not-yet-code)

New tool: `tools/external_cross_check.py`

```python
"""Nightly external cross-check. Two facts, three sources each.
Alert if any pairwise disagreement exceeds tolerance."""
import os, json, urllib.request
from datetime import datetime, timezone

def _fetch_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())

def _our_burned_xrp():
    # Read from latest signed_snapshot in Postgres
    ...

def _xrpscan_burned_xrp():
    m = _fetch_json("https://api.xrpscan.com/api/v1/metrics")
    return 100_000_000_000 - m["totalCoins"]

def _bithomp_burned_xrp():
    ...

def check_burned_xrp():
    ours = _our_burned_xrp()
    xrpscan = _xrpscan_burned_xrp()
    bithomp = _bithomp_burned_xrp()
    tol = 10_000  # XRP fee dust
    diffs = {
        "ours_vs_xrpscan": abs(ours - xrpscan),
        "ours_vs_bithomp": abs(ours - bithomp),
        "xrpscan_vs_bithomp": abs(xrpscan - bithomp),
    }
    if max(diffs.values()) > tol:
        return {"status": "diverged", "values": {"ours": ours, "xrpscan": xrpscan, "bithomp": bithomp}, "diffs": diffs}
    return {"status": "converged", "values": {"ours": ours, "xrpscan": xrpscan, "bithomp": bithomp}}

# Same shape for check_rlusd_supply()

# Main: run both, write row to external_cross_check table with
# converged/diverged status. L2 inspector reads table nightly; a
# diverged row = red page. If both externals disagree with each other
# but ours matches one, note it — don't panic (externals sometimes
# drift too).
```

Schema:

```sql
CREATE TABLE external_cross_check (
  ts          timestamptz PRIMARY KEY DEFAULT NOW(),
  fact_name   text NOT NULL,       -- 'burned_xrp' | 'rlusd_supply'
  our_value   numeric NOT NULL,
  xrpscan     numeric,             -- NULL if fetch failed
  bithomp     numeric,
  max_diff    numeric,
  status      text NOT NULL,       -- 'converged' | 'diverged' | 'fetch_failed'
  detail      jsonb                -- error text, source URLs, timings
);
CREATE INDEX ON external_cross_check (fact_name, ts DESC);
```

Cadence: nightly systemd timer on Lenovo (same host as L1/L2), 03:00 UTC
or wherever the load minimum sits. Or nested inside L2 inspector's
existing timer (adds one more check to L2's report).

L1 pager gets one new check: `external_cross_check_diverged` — if
latest row for either fact is `diverged` and more than 12h old, page.
(12h window because we want to catch drift, not transient API
hiccups.)

---

## What we're NOT cross-checking

- **NFT counts**: no free external API publishes NFT total-supply
  numbers in a form we can pull cheaply. Ours is authoritative for
  now.
- **AMM pool state**: too many pools, too much churn; cross-check by
  count doesn't tell us anything useful.
- **Anchor tx hashes**: already cross-checked by anchor_canary via
  s1/s2/s2-clio witness cascade.

---

## Response to divergence

Divergence is loud but not always our fault:
- **Ours ≠ both externals**: strongly suggests our walker broke.
  Diagnose walker; roll back last change if suspicious.
- **Ours = XRPScan ≠ Bithomp**: likely Bithomp lag or API change on
  their side. Note, don't panic; re-check next night.
- **Ours = Bithomp ≠ XRPScan**: symmetric.
- **All three disagree**: likely XRPL network event (recent burn we
  haven't picked up yet, or supply change). Cross-check against
  s1.ripple.com directly to break the tie.

Rule: 3-out-of-3 disagreement on same day is a data event, not a bug.
2-out-of-3 disagreement puts our number under review.

---

## Trigger for build

Post-b2 sovereignty recovery. Ship as part of L2 v2 or as its own
tool — whichever hits first. ~2-3 hours of work end-to-end (fetch
functions, schema, L1 check, one test that mocks the three sources).

---

*Filed 2026-08-16.*
