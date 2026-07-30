# D3 — NFT collection rollup spec

Status: **DRAFT — DO NOT EXECUTE UNTIL BACKFILL ≥ ~50% COVERAGE**
(target: 2026-05-15 cutoff, ETA ~2026-07-14).
Codified after Charlie's 2026-07-05 truth-first callout on the 1.87:1 top-of-tip skew.

## Purpose

Fill `nft_collection_stats` (schema already shipped in D2) with per-collection
metrics rolled up from `nft_activity`. Powers the /nfts page's active-vs-quiet
shape and the funnel narrative — but only once historical depth is honest.

## Non-negotiables (standing rules baked in)

1. **Intra-collection only.** Every metric reads `nft_activity` filtered to a
   single `(issuer, taxon)`. **Never** JOIN across collections. **Never** look
   up a wallet's activity outside this collection. The guardrail comment in
   `nft_activity_walker.py::compute_churn_metrics` is load-bearing — do not
   remove it. See `memory/feedback_nft_churn_intra_collection_only.md`.
2. **Flow ≠ stock.** `net_minted_since_cutoff` is a FLOW (mints − burns since
   the cutoff ledger). It is **not** the true existing-NFT count; that lives
   in `nft_existing_snapshot`. Two numbers, two labels, never blended in UI or
   downstream code.
3. **Don't run until backfill ≥ ~50%.** Running on 5 days of top-of-tip data
   produces a misleading active-vs-quiet shape and a mint:sale ratio that is
   ~5× tighter than the historical truth. Enforce with a startup guard:
   ```python
   if backfill_ledger > int(backfill_target + (start - backfill_target) * 0.5):
       return False, "backfill <50% — rollup would produce top-of-tip skew"
   ```
   (Bypassable via `--force` for local testing only, never in launchd plist.)
4. **Cadence.** Once launched: `StartInterval 21600` (6h). Rollup is O(rows)
   and idempotent; hourly is overkill and hammers Postgres unnecessarily.

## Metrics — per (issuer, taxon)

All queries filter `WHERE issuer = %s AND taxon = %s AND close_time >= <cutoff>`
where `<cutoff>` is the ledger_close_time of `backfill_target`
(2026-04-01 00:00 UTC, ledger 103252853). Column-level notes:

| Column | Source SQL | Notes |
|---|---|---|
| `mints_total` | `COUNT(*) WHERE tx_type='Mint'` | Since cutoff. |
| `burns_total` | `COUNT(*) WHERE tx_type='Burn'` | Since cutoff. |
| `net_minted_since_cutoff` | `mints_total - burns_total` | FLOW. |
| `sales_30d` | `COUNT(*) WHERE tx_type='AcceptOffer' AND close_time > now() - '30 days'` | Real sales only (AcceptOffer). CreateOffer/CancelOffer are intent, not settlement. |
| `distinct_buyers_30d` | `COUNT(DISTINCT buyer) WHERE tx_type='AcceptOffer' AND …` | |
| `distinct_sellers_30d` | `COUNT(DISTINCT seller) WHERE tx_type='AcceptOffer' AND …` | |
| `last_sale_at` | `MAX(close_time) WHERE tx_type='AcceptOffer'` | Ever, not 30d. Enables "quiet since" copy. |
| `is_active` | `distinct_buyers_30d >= 5` | Threshold picked to match Fable's active-vs-quiet frame. Bumpable but codify in this doc + a comment at the SQL. |
| `floor_bands_json` | Top-5 open asks snapshot | Deferred to D3b (needs open-offer walker; D3 leaves this NULL). |

## Churn metrics — `churn_metrics_json`

**INTRA-COLLECTION ONLY.** No wallet lookups outside the (issuer, taxon).

```json
{
  "sale_to_resale_median_days": 42,
  "wallets_that_flipped_within_30d": 17,
  "one_time_holders": 210,
  "repeat_buyers": 24,
  "computed_at": "2026-07-14T…Z"
}
```

Computed per-collection from `nft_activity` rows where `tx_type='AcceptOffer'`:

- `sale_to_resale_median_days`: median days between successive `AcceptOffer`
  rows for the same `nftoken_id` within this collection. NULL if <10 pairs.
- `wallets_that_flipped_within_30d`: distinct buyers who resold within 30d,
  again scoped to this collection's sales.
- `one_time_holders`: buyers with exactly 1 AcceptOffer in this collection,
  no subsequent sell.
- `repeat_buyers`: distinct buyers with ≥2 AcceptOffer buys in this collection.

If ever tempted to cross to a different collection to enrich this: **STOP.
Flag Charlie.** That's the strangers-graph drift line.

## Rollup transaction shape

- One transaction per collection. `INSERT … ON CONFLICT (issuer, taxon) DO UPDATE`.
- Skip collections with 0 rows in `nft_activity` (nothing to summarize).
- Batch commit every 100 collections so a mid-run kill leaves a partial but
  consistent state, not a lock.
- Log per-collection duration; anything >2s is a flag (likely a churn-metric
  regression on a huge collection).

## Walker wire-up

`nft_activity_walker.py`:

- `run_rollup()` currently raises `NotImplementedError`. Replace with the
  logic above.
- `MODE_DISPATCH` already includes `"rollup": run_rollup` — no dispatch change.
- New launchd plist: `com.charliebruce.xrpldashboard.nft_rollup.plist` with
  `--mode rollup` and `StartInterval 21600`. Not created in D3; wait until
  D3 is executed and green before adding the plist.
- First run: `python nft_activity_walker.py --mode rollup` manually, watch
  the log, verify `nft_collection_stats` populates.

## walker_health integration

Rollup mode wraps its run in the existing `write_walker_health_start` /
`write_walker_health_end` calls. Row name: `nft_activity_rollup`.
`last_run_message` shape:
```
collections=124 rows_written=124 skipped_empty=8 duration_ms=…
```

## /nfts page implications (out of scope for D3)

Once populated, /nfts can surface:
- Active collections count (`WHERE is_active = TRUE`)
- Sorted by `sales_30d DESC` for the "what's actually trading" list
- Churn badge per collection (from `churn_metrics_json`)
- The funnel hero uses `mints_total : sales_total` **at the point where
  backfill coverage supports the story** — Charlie's call, not automatic.

## Gate 3 (D3 render review)

Before wiring the /nfts UI: paste the JSON payload for two spot-checked
collections (one active, one quiet) so Charlie can eyeball whether the
numbers look real vs top-of-tip skewed. Then Gate 4 = ship the /nfts
surface.

## Failure modes / rejection triggers

- Backfill still <50% → hard-refuse to run (rule 3).
- `nft_activity` rows have a null `close_time` → hard-refuse (should be zero
  per the close_time fix; a nonzero here means the fix regressed).
- Any SQL that reads `nft_activity` without filtering by `(issuer, taxon)` →
  reject in review (rule 1).
- `is_active` threshold changed silently → codify the new threshold in this
  doc and the SQL comment before merging.
