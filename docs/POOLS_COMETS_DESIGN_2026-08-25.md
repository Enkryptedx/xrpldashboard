# POOLS-COMETS — Design Pack

**Status:** PARKED — Phase A attempted 2026-08-25, parked pre-cert. Lessons attached below.
**Phase A + B merged into one post-cert build.** Phase C remains lowest priority.
**Filed:** 2026-08-25
**Reviewer:** Charlie
**Queue ref:** SATURDAY_QUEUE_2026-08-22.md §2 (renamed from "/pools pack")

---

## 0. Park Decision — 2026-08-25 (pre-cert)

Phase A was attempted this cycle and did not clear the acceptance bar. Park executed:
commit `efc242a` ships ONLY the approved callout copy (legibility entry #1). All Phase A
backend changes reverted to pre-08ec379 state. Page is plainly working through cert.

### Findings attached for post-cert build

**Finding 1 — Magnitude-scale variance (open question)**
`fireComet` scales comet size via `magScale(drops)` — sublinear log curve, max 2.0.
SIM always passes `null` (scale 1.0). Live events pass real drops, which can push
scale to 1.5–2.0 for large swaps/deposits. Even after fixing the poll handler to
pass `null` (literal SIM copy), Charlie still saw a "big again" comet — mechanism
unclear (could be the impact ring radius `3 * scale` at scale 1.875 for a 500 XRP
test deposit, which produces a ring expanding to 5.6 SVG units vs SIM's 3.0 units).
**Resolution needed in post-cert build:** decide whether live events should always
render at SIM-neutral scale (null = 1.0) or at a scaled size capped lower than 2.0.
If neutral: pass `null` from poll handler (already coded in this cycle). If scaled:
tune the magScale cap to produce visually similar size to SIM.

**Finding 2 — Swap visual expectation**
Charlie expected purple comets moving BETWEEN pools for swaps — that's Phase B
(pool→pool arcs). Phase A's swap visual is an impact ring at the pool star. The
ring fires correctly (it's SIM parity), but the legend entry "SWAP" with a violet
dot implies movement that doesn't exist yet. **Resolution needed:** either make the
legend honest ("SWAP · ring pulse" or similar wording), or ship Phase B first so
the arc actually exists when the legend says it does.

**Finding 3 — Acceptance test before code (standing process)**
Three refresh-and-watch rounds in one cycle is the ceiling. "Refresh and watch" is
not an acceptance test — it's hoping the bug self-reveals. Standing rule going
forward: before any Phase A/B code ships, the exact acceptance test must be written
and agreed (what to trigger, what to see, pass/fail criteria) BEFORE the code is
written. This cycle the test was undefined until after several rounds, which cost
multiple redesigns.

**Finding 4 — SIM parity is the correct frame (confirmed)**
One renderer, two data sources. Any comet code that doesn't exist in SIM is wrong.
The literal copy rule: SIM calls `fireComet(starEl, et, null)`. Live must call the
identical function with identical parameters, except: which pool (live-specific),
which direction (live-specific for swap ring color). Magnitude is always null.
This was implemented correctly in this cycle — the phase was parked for visual
verification reasons, not code correctness.

---

## 1. Purpose

The /pools constellation fires comets for every live on-ledger AMM event.
Three things it can't do yet:

| Phase | Gap | Visual today |
|-------|-----|-------------|
| A | Swap comets carry no direction — sign discarded before storage | White ring pulse regardless of which way XRP moved |
| B | Path payments that touch multiple pools fire once, at the first matched pool | Second pool never lights up; the arc across the constellation never draws |
| C | Top-10 star set is frozen at page-render time | A new pool overtaking the top-10 is invisible until reload |

This pack proposes how to close all three. Not a build. Every phase requires a shape ruling before code moves.

---

## 2. Non-goals

- Not a swap-fee or APR display — we track flow, not yield.
- Not per-token direction (which token moved in/out) — XRP side only; IOU/IOU pools are still directionless.
- Not a real-time TVL chart on the pool cards — Phase C only updates the constellation star set, not the browse table.
- Not a move to WebSocket push — polling at 3s stays; no SSE/socket work.

---

## 3. Phase A — Signed-delta directional comets

### 3.1 The gap (confirmed)

`xrpl_stream.py:734` — `_extract_amm_xrp_delta_drops` is documented as returning
`"absolute |Balance_final - Balance_prev| as a positive int"` (docstring).

Line 767:
```python
return abs(int(final_bal_str) - int(prev_bal_str))
```

Line 773:
```python
return abs(int(new["Balance"]))
```

Sign discarded at the source. `amm_pool_events.magnitude_xrp_drops` is always ≥ 0.
`/api/pools/recent_events` returns this unsigned value; the client at `pools.html:845–849`
explicitly defers:

```js
// Swap = directionless pulse only. Without parsing AffectedNodes
// direction we won't fake which side gained.
if (eventType === 'swap') {
  spawnImpactRing(cx, cy, '229,231,235', scale, 0.4);
  return;
}
```

The comment "Without parsing AffectedNodes" is now stale — AffectedNodes IS being parsed
(`_extract_amm_xrp_delta_drops` walks AffectedNodes). We parse it. We just throw the sign away.

### 3.2 What the sign means

For any swap, `int(final_balance) - int(prev_balance)` on the AMM's `AccountRoot`:

- **Positive** (XRP balance grew): someone paid XRP into the pool to receive tokens.
  From the pool's perspective: XRP **in**. Comet travels edge → star. Color: cyan (same as deposit).
- **Negative** (XRP balance shrank): someone received XRP from the pool by paying tokens.
  From the pool's perspective: XRP **out**. Comet travels star → edge. Color: amber (same as withdraw).
- **None**: IOU/IOU pool — no XRP AccountRoot balance shift. Remains a white ring.

### 3.3 Proposed change (four touch-points)

**Touch 1 — `xrpl_stream.py:734` (new signed extractor)**

```python
def _extract_amm_xrp_delta_drops_signed(affected_nodes, amm_account):
    """Signed XRP-side delta for the AMM's AccountRoot.
    Positive = XRP in. Negative = XRP out. None = IOU/IOU or parse error.
    Replaces the unsigned _extract_amm_xrp_delta_drops for swap direction."""
    for node in affected_nodes:
        wrapper = (node.get("ModifiedNode")
                   or node.get("CreatedNode")
                   or node.get("DeletedNode"))
        if not wrapper:
            continue
        if wrapper.get("LedgerEntryType") != "AccountRoot":
            continue
        final = wrapper.get("FinalFields") or {}
        new = wrapper.get("NewFields") or {}
        acct = final.get("Account") or new.get("Account")
        if acct != amm_account:
            continue
        prev = wrapper.get("PreviousFields") or {}
        final_bal_str = final.get("Balance")
        prev_bal_str = prev.get("Balance")
        if final_bal_str is not None and prev_bal_str is not None:
            try:
                return int(final_bal_str) - int(prev_bal_str)  # signed
            except (TypeError, ValueError):
                return None
        if new.get("Balance") is not None:
            try:
                return int(new["Balance"])  # new account = net positive
            except (TypeError, ValueError):
                return None
        return None
    return None
```

Keep `_extract_amm_xrp_delta_drops` (unsigned) intact — it's still used for deposit/withdraw
magnitude sizing where sign is irrelevant (tx type already tells us direction).

**Touch 2 — `amm_pool_events` schema (one new column)**

```sql
ALTER TABLE amm_pool_events ADD COLUMN xrp_delta_signed_drops INTEGER;
```

Same migration pattern as the existing `magnitude_xrp_drops` column add (see `xrpl_stream.py:85`).
Applied in `_ensure_amm_pool_events_table()`. Null-safe: existing rows stay NULL, client
treats NULL as "directionless" (existing white-ring behavior).

PG bridge: `pgbridge.write_amm_pool_event(ts, amm_account, event_type, mag_drops)` grows one
param: `signed_drops`. PG schema gets the same column.

**Touch 3 — `/api/pools/recent_events` (API response shape)**

Add `xrp_direction` field derived from the signed column:

```python
"xrp_direction": (
    "in"  if r["xrp_delta_signed_drops"] is not None and r["xrp_delta_signed_drops"] > 0 else
    "out" if r["xrp_delta_signed_drops"] is not None and r["xrp_delta_signed_drops"] < 0 else
    None
)
```

`magnitude_xrp_drops` stays unsigned (comet scale). `xrp_direction` carries the directionality.
Fully backwards-compatible: old clients ignore `xrp_direction`, new client uses it.

**Touch 4 — `pools.html` comet client**

```js
// After this patch, swaps with a known XRP direction fire a directional
// comet (same as deposit/withdraw). Only IOU/IOU swaps remain white rings.
if (eventType === 'swap') {
  if (ev.xrp_direction === 'in') {
    fireComet(starEl, 'deposit', magnitude);   // reuse inbound path
  } else if (ev.xrp_direction === 'out') {
    fireComet(starEl, 'withdraw', magnitude);  // reuse outbound path
  } else {
    spawnImpactRing(cx, cy, '229,231,235', scale, 0.4);  // IOU/IOU: unchanged
  }
  return;
}
```

No new animation primitives needed — deposits (inbound cyan) and withdraws (outbound amber)
already render correctly. Swaps with known direction just ride the same paths.

### 3.4 LOC budget

| File | Lines changed |
|------|--------------|
| `xrpl_stream.py` | ~30 (new function + call site + schema migration) |
| `pgbridge.py` | ~10 (write_amm_pool_event signature + PG schema) |
| `app.py` | ~8 (xrp_direction field in API response) |
| `pools.html` | ~10 (swap branch in fireComet) |
| **Total** | ~58 LOC |

### 3.5 Ruling needed

Direction interpretation: should a swap where XRP flows INTO the pool be colored cyan (same
as deposit) or a distinct third color? Cyan-for-in / amber-for-out is the simplest read and
reuses existing animation code. A third color (e.g., white comet) would differentiate "swap"
from "liquidity op" but adds complexity. Recommend cyan/amber — ruling before code.

---

## 4. Phase B — Pool→pool arcs

### 4.1 The gap (confirmed)

`xrpl_stream.py:804–819`: the handler walks `AffectedNodes` and breaks on the **first** matching
AMM account:

```python
matched_account = None
for node in affected:
    ...
    if acct and acct in _AMM_ACCOUNT_SET:
        matched_account = acct
        break   # ← hard stop here
```

A path payment routing XRP → Pool A (token A) → Pool B (token B) touches two AMM accounts.
We emit ONE event at Pool A. Pool B is silent. The arc between them never draws.

### 4.2 Proposed change (multi-emit path)

**Touch 1 — `amm_pool_event_handler`: collect all matched accounts**

```python
matched_accounts = []
for node in affected:
    ...
    if acct and acct in _AMM_ACCOUNT_SET:
        matched_accounts.append(acct)

if not matched_accounts:
    return
```

For single-pool txs (deposit, withdraw, single-AMM swap): `matched_accounts` has one entry —
behavior identical to today.

For path payments through N pools: `matched_accounts` has N entries, one per pool touched.

**Touch 2 — New `path_id` column to link events from the same tx**

Emit one row per matched account, all sharing the same `path_id` (tx hash works here —
`tx.get("hash")`). Client uses `path_id` to pair rows and draw an arc.

```sql
ALTER TABLE amm_pool_events ADD COLUMN path_id TEXT;
```

Single-pool events still get a `path_id` (their own tx hash) — no special casing needed.

**Touch 3 — API response includes `path_id`**

`/api/pools/recent_events` adds `"path_id"` to each event dict. No schema change to the
endpoint signature — just a new field.

**Touch 4 — Client: arc drawing**

When two events in the same poll share a `path_id`, draw a bezier arc from star A to star B.
Arc color: white (distinguishes from direct deposit/withdraw comets). Arc thickness scales
with combined magnitude.

**Constraint: only top-10 pools have star positions.** Phase B arcs only fire when BOTH
pools are in the constellation's rendered top-10. Paths through one top-10 and one lower
pool still fire a single comet at the top-10 star, no arc. This is acceptable for a first
pass — multi-AMM path payments typically route through the most liquid pools.

### 4.3 Complexity flag

Phase B is substantially harder than Phase A:
- Multi-row emit changes the event writer contract.
- Arc rendering requires new SVG animation primitives (bezier path tween via GSAP, not the
  existing point-travel tween).
- The "both pools in top-10" constraint is easy to code but means arcs will be rare in
  practice — path payments through two tracked top-10 pools are uncommon.

**Recommend deferring Phase B until Phase A is live and confirmed.** Arc rendering is the
right endgame but the marginal visual value per LOC is lower than Phase A.

### 4.4 LOC budget (rough)

| File | Lines |
|------|-------|
| `xrpl_stream.py` | ~25 (multi-emit loop + path_id) |
| `pgbridge.py` | ~10 (schema + write sig) |
| `app.py` | ~5 (path_id in API response) |
| `pools.html` | ~60 (arc detection + bezier animation) |
| **Total** | ~100 LOC |

### 4.5 Ruling needed

Approve Phase B in principle, or park it past cert? If approved, confirm: arc color choice
(white / purple / gradient?), and whether single-leg path payments to a non-top-10 pool
should still fire a comet at the nearest top-10 pool as a fallback or fire nothing.

---

## 5. Phase C — Live top-10 refresh

### 5.1 The gap

`pools()` route bakes `top10` from `amm_ranked_pools` at request time. The constellation's
star positions, sizes, and labels are frozen for the lifetime of the browser session.
If the #10 pool changes (TVL overtaken), the constellation won't update until reload.

In practice TVL ranking shifts slowly (hourly `rank_amms` cadence). The visual delta per
30-min refresh window is small. Phase C is the lowest urgency of the three.

### 5.2 Proposed change

**New endpoint: `/api/pools/top10`**

```python
@app.route("/api/pools/top10")
def api_pools_top10():
    ranked, meta = _ranked_amm_snapshot()
    top10 = [r for r in ranked if (r.get("tvl_usd") or 0) > 0][:10]
    return {"pools": top10, "snapshot_ts": meta.get("snapshot_ts")}
```

**Client: periodic diff**

Poll `/api/pools/top10` every 5 min (not 3s — TVL doesn't shift that fast). Compare
`amm_account` ordering to the rendered star set. On change: cross-fade star size (GSAP
`to({r: newRadius})`) and update label text. No full re-render; star positions stay fixed.

### 5.3 LOC budget

| File | Lines |
|------|-------|
| `app.py` | ~15 (new endpoint) |
| `pools.html` | ~35 (5-min poller + diff + size tween) |
| **Total** | ~50 LOC |

### 5.4 Ruling needed

Is Phase C worth doing pre-cert, or park it? TVL rankings shift slowly enough that it's
nearly invisible to users. Recommend: park until post-cert, ship Phase A first.

---

## 6. Recommended phase order and ruling request

| Phase | Effort | Visual payoff | Recommend |
|-------|--------|--------------|-----------|
| A | ~60 LOC | High — every swap gets direction | Ship first, pre-cert |
| B | ~100 LOC | Medium — arcs are striking but rare | Post-cert, after A confirmed |
| C | ~50 LOC | Low — TVL shifts slowly | Post-cert, lowest priority |

**Three rulings Charlie needs to give before Phase A moves to code:**

1. Swap direction color: cyan/amber (reuse deposit/withdraw palette) or a distinct third color?
2. Phase B: approve in principle now, or park until post-cert?
3. Phase C: park until post-cert (recommended), or any reason to pull it forward?
