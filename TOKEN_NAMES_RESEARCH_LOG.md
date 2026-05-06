# Token names — curation research log

Working notes from the verification pass. Records what was checked, what
sources backed the verification, and what's still pending. Update this
file as `TODO_curation_pass` entries get resolved.

## Methodology

For each entry tagged `TODO_curation_pass` in `token_names.json`, the
verification follows the policy in `TOKEN_NAMES.md`:

1. Look for the issuer's `xrp-ledger.toml` at the canonical path
   (`https://{domain}/.well-known/xrp-ledger.toml`). This is the
   strongest first-party source.
2. If no TOML, search for the issuer's own published documentation
   that names the address (blog post, support article, official
   announcement).
3. If no first-party source surfaces, leave `TODO_curation_pass` and
   record what was tried below so the next curator doesn't repeat it.

Never substitute a third-party explorer (XRPSCAN, Bithomp, xpmarket)
for a first-party source — those are downstream curation, not primary
verification.

## Verified this pass

| Entry                      | Source attached                                                                                                           | Notes                                                         |
|----------------------------|---------------------------------------------------------------------------------------------------------------------------|---------------------------------------------------------------|
| `USD.Bitstamp`             | https://blog.bitstamp.net/post/bitstamp-eur-iou-services-on-xrp-ledger/                                                   | Same `rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B` issuer for USD/EUR/BTC. |
| `BTC.Bitstamp`             | https://blog.bitstamp.net/post/bitstamp-eur-iou-services-on-xrp-ledger/                                                   | Same address as USD.Bitstamp.                                 |
| `SOLO`                     | https://medium.com/@txEcosystem/sologenic-solo-airdrop-distribution-complete-official-report-8e5d8594e43e                | Official Sologenic airdrop report; TrustLine instructions.    |
| `USDC`                     | https://www.circle.com/multi-chain-usdc/xrpl                                                                              | Circle's official multi-chain USDC page lists the XRPL mainnet issuer. |

## Verified prior to this pass

| Entry  | Source                                          |
|--------|--------------------------------------------------|
| `RLUSD`| https://ripple.com/.well-known/xrp-ledger.toml   |

## Still TODO_curation_pass after this pass

| Entry           | What was tried                                                                       | Outcome                                                                                                  |
|-----------------|--------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------|
| `USD.Gatehub`   | `gatehub.net/.well-known/xrp-ledger.toml` (no file); GateHub markets page; legal page | UK cookie banner gates the legal/xrpl-addresses URL. Re-attempt from a different IP or fetch raw HTML.   |
| `EUR.Gatehub`   | Same as above                                                                         | Same outcome — confirm via GateHub's `/legal/xrpl-addresses` page once fetchable.                       |
| `BTC.Gatehub`   | Same as above                                                                         | Same.                                                                                                    |
| `ETH.Gatehub`   | Same as above                                                                         | Same.                                                                                                    |
| `CSC`           | Not yet researched.                                                                   | Try `coinfield` / `casinocoin.org` for first-party verification.                                         |
| `CORE`          | `www.coreum.com/.well-known/xrp-ledger.toml` redirects offsite.                       | Coreum changed domains (now `tx.org` per redirect). Verify on the new domain.                            |
| `ELS`           | Not yet researched.                                                                   | Try `elysiansociety.io` or wherever Elysian publishes.                                                   |
| `XPM`           | Not yet researched.                                                                   | Try the XPM project's site if any.                                                                       |
| `EQ`            | Not yet researched.                                                                   | Equilibrium project — find the project's official site/Medium.                                           |
| `RPR`           | Not yet researched.                                                                   | Reaper / RPR — find project site.                                                                        |
| `XRdoge`        | Not yet researched.                                                                   | XRdoge has a community site; find their published issuer.                                                |
| `scrap`         | Not yet researched.                                                                   | Scrap is a memecoin — find their Twitter/X or Discord-pinned address.                                    |
| `PHNIX`         | Not yet researched.                                                                   | Phnix project — find official site.                                                                      |
| `BERT`          | Not yet researched.                                                                   | BERT — find official site.                                                                               |

## What I learned about XRPL toml discoverability

- Most XRPL participants do **not** publish `.well-known/xrp-ledger.toml`
  at the canonical path. Of the 5 issuer domains attempted in this pass
  (Bitstamp, GateHub, Sologenic, Coreum, XRPL Foundation), only Ripple
  published one at the canonical path.
- Bitstamp, GateHub, and Sologenic all confirm their addresses through
  *some* first-party channel — just not in the standard format. The
  curation work is finding *which* channel.
- Memecoins almost never publish a TOML. First-party verification will
  usually be a pinned Discord/Telegram message or X post.

## Recommended next-pass strategy

1. **Batch-fetch** `https://{domain}/.well-known/xrp-ledger.toml` for the
   top 50 issuers by trade volume. The handful that publish saves hours
   of per-issuer searching.
2. For domains that don't publish TOML, **search the issuer's own blog**
   (`site:{domain} XRPL address` or `site:{domain} issuer`) — this
   surfaced the Bitstamp source.
3. For Coreum specifically, follow the redirect to `tx.org` and
   re-fetch the TOML there — Coreum rebranded.
4. Memecoins are case-by-case; expect lower hit rate. Be willing to
   leave `TODO_curation_pass` rather than weaken the source policy.

## Second-pass attempts (2026-05-05)

Re-attempted TOML discovery on a wider set of domains. Results:

| Domain                        | Outcome                                                                                            |
|-------------------------------|----------------------------------------------------------------------------------------------------|
| `casinocoin.org`              | Redirects to `casinocoin.im`, which 404s on the canonical TOML path.                              |
| `casinocoin.im`               | No TOML at canonical path.                                                                         |
| `sologenic.org`               | 404 at canonical path (confirmed).                                                                 |
| `sologenic.com`               | Redirects to `tx.org` marketing site; no TOML.                                                     |
| `tx.org`                      | 404 at canonical path. Coreum/Sologenic appear to have unified branding under tx.org but no TOML. |
| `xrpl-labs.com`               | **Declares one account: `rMYL6sN2z5os4RWLuT6HHDhJYpBACujzNa`** (no role specified). Added to `named_accounts.json` as "XRPL Labs". |
| `xrplf.org` / `foundation.xrpl.org` | TOML now redirects to xrpl.org homepage — XRPL Foundation removed its TOML.                        |
| `bitstamp.net`                | 404 at canonical path. Bitstamp's blog remains the authoritative first-party source.              |
| `gatehub.net/legal/xrpl-addresses` | Still gated by UK cookie/restriction banner — content unfetchable from this network.            |
| `xrdoge.io`                   | Connection refused (site unreachable).                                                             |
| `phnixexchange.com`           | Connection refused (site unreachable).                                                             |

No new `token_names.json` verifications were possible from this pass.
The 14 entries flagged `TODO_curation_pass` remain so.

This pass *did* add two entries to `named_accounts.json`:

| Address                              | Label       | Source                                                                                  |
|--------------------------------------|-------------|------------------------------------------------------------------------------------------|
| `rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B`  | Bitstamp    | https://blog.bitstamp.net/post/bitstamp-eur-iou-services-on-xrp-ledger/                  |
| `rMYL6sN2z5os4RWLuT6HHDhJYpBACujzNa` | XRPL Labs   | https://xrpl-labs.com/.well-known/xrp-ledger.toml (ownership only — no role declared)   |
