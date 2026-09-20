# Press citations of xrpldashboard.com

Running record of first-party press citations that resolve to specific
articles, with verification of the cited numbers where recoverable. Kept
in verify-then-record order — no entry lands here without an accuracy
column marked y / partial / n / unverifiable.

Column key:
- Cited: the exact figures/claims attributed to /amendments (or another page)
- Verified: y = independently reconstructed from live data + our code path.
  partial = some figures confirmed, others cannot be reconstructed from
  our archived state. n = wrong. unverifiable = we don't archive the
  underlying data and no third-party archive was available.
- Sovereignty: whether our page served from the sovereign path (Lenovo
  rpc tunnel) or fell back to public RPC at the cited fetch time —
  read from walker_node_fallback.

---

## 2026-09-18 · CryptoSlate · Oluwapelumi Adejumo (Editor)

- **URL**: https://cryptoslate.com/xrpls-new-lending-tool-could-lock-up-your-xrp-from-minutes-to-decades/
- **What was cited**: "A live dashboard snapshot fetched Sept. 17 did not
  surface LendingProtocolV1_1 in the responding node's feature feed…
  placed the base LendingProtocol amendment at 13 of 35 trusted-validator
  votes and SingleAssetVault at 16 of 35, below the displayed threshold
  of 28."
- **Cited landing page**: /amendments
- **Fetch date claimed**: 2026-09-17 (hour unspecified)

### Verification (2026-09-20)

| Claim | Verdict | Basis |
|---|---|---|
| Threshold 28 of 35 | **confirmed** | Math: ceil(35 × 0.80) = 28. VHS reports `threshold: "28/35"` for all in-flight amendments. Our page displays that value unchanged from `amendments_network_votes._parse_threshold` output. |
| LendingProtocolV1_1 not in responding node's feature feed | **confirmed** | Live check 2026-09-20: Lenovo rippled `feature` RPC returns 104 known features. Only 2 lending/vault names present: `LendingProtocol` (hash 565B90CA…) and `SingleAssetVault` (hash 81BD2619…). VHS lists `LendingProtocolV1_1` at hash A360E2BF…, absent from Lenovo's list — matches the documented "in Majorities but the responding node does not recognize the hash" scenario noted in the amendments_state.py header comment. |
| LendingProtocol at 13/35 votes on Sept 17 | **unverifiable** | Our stack does not archive VHS vote tallies. VHS's `date=` query parameter returns current-shape tally with count=null for our targets, not a Sept-17-as-of snapshot. Lenovo's rolling window starts at ledger 107,068,362 (~2026-09-17 21:00 UTC per close-time inspection) so most of Sept 17 is below range. |
| SingleAssetVault at 16/35 votes on Sept 17 | **unverifiable** | Same as above. |
| Numbers were "below threshold" | **directionally confirmed** | Ledger 107,070,000 (Sept 18 13:14 UTC, one day after fetch): 94 enabled amendments, only 1 in Majorities (hash 9F287AED…, NOT LendingProtocol or SingleAssetVault). Both cited amendments were NOT in Majorities — consistent with the article's "below threshold" claim. |

### Sovereignty
- `walker_node_fallback` for `walker_name='amendments_state'` during Sep 14–20: **0 rows**. Zero fallbacks to public RPC across the entire outage window.
- Same window, other walkers DID record fallbacks (`check_page`, `network_pulse`, `wallet_data` on Sep 15, `network_pulse` on Sep 17). Only amendments_state stayed sovereign throughout.
- **/amendments served from Lenovo rpc tunnel on Sept 17. Not a public-RPC fallback.**

### Referrer trail
- 4 `https://cryptoslate.com/` referrers on Fri 2026-09-18 (06:02, 11:34, 15:45, 19:06 UTC) — all bare-origin per Chrome's `strict-origin-when-cross-origin` default. Now identified as originating from this article by the author himself.

