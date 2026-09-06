# Signed-receipt design note — /check.json envelope signature

**Authored:** 2026-09-06 · JJ · design only, no code
**Prompted by:** the `sig_ed25519: null` gap in the current `/check.json` envelope (canonical hash is computed on Render, but the signing key lives only on the Mac, correctly).
**Decision:** Charlie — pick a path or say file for later.

## Constraint (invariant, non-negotiable)

The Ed25519 private key that signs snapshots + would sign check receipts lives on the Mac and only on the Mac. It never leaves. Any receipt-signing design must respect this — no key sync to Render, no key in any container image, no key in a KMS proxy we don't control.

## Two options

### Option A — Mac-side signing service the Render app calls over the tunnel

**Shape:** a small HTTP service on the Mac (`sig-service`, likely on the same rippled-node box or dedicated to Mac) that accepts a canonical hash and returns an Ed25519 signature. Render's `/check` handler POSTs the hash to `https://sig.xrpldashboard.com/sign` with CF-Access headers, gets back the signature, and completes the envelope inline. Same tunnel plumbing we already use for XRPL RPC — reuse the CF-Access-authenticated Cloudflare tunnel infra, add one new hostname pointing at a new small service.

**Latency:** +1 tunnel round-trip per `/check.json` response. Measured against `/lending` and `/check` XRPL calls, that's ~30-150ms per response (LAN speed + CF edge). Adds to a request that today takes ~50-150ms cache-hit / ~1-2s cache-miss. Not fatal for humans; noticeable for machines integrating in tight loops.

**Fail-open:** if the sig-service is unreachable, envelope ships with `sig_ed25519: null` (today's shape) + a `sig_unreachable_at_utc: ISO` field. Client sees no signature, knows why. `/check.json` never blocks on the signing round-trip.

**Signing throughput:** ~1000 signs/sec on a modest Mac CPU, batched. Zero rate concern for /check volume even at 100× current traffic.

**Security surface:** one new endpoint (`sig.xrpldashboard.com`) with CF-Access + an internal-network firewall on the origin. Signing is stateless — no memory of what it signed. Log-line-only audit trail. Key stays local.

**What a machine at call-time gets:** a full receipt with `sig_ed25519` populated on 99%+ of calls (the fail-open path shows up only during tunnel/Mac outages). Machine can verify the signature immediately against the published pubkey and cryptographically anchor the answer. **Best fit for real-time integrations** (wallets, exchanges gating transactions on our verdict).

**Costs to build:** 1 new tiny Flask/asgi service (~200 lines), 1 new CF Tunnel hostname, 1 new client wrapper in `sovereign_tunnel_client.py`, 1 methodology page update. Deploy pattern is same as XRPL tunnel — Charlie has done it before.

### Option B — Batched signing (Render writes hashes, Mac signs on cadence)

**Shape:** Render's `/check` handler emits envelopes with `sig_ed25519: null` at call-time and writes a row to `check_pending_signatures` (Postgres, on Neon) with the canonical hash + response id + checked_at_utc. A Mac-side walker (`check_signer_walker.py`) runs every N minutes, reads the pending rows, signs each hash with the local key, writes the signature back to `check_signed_receipts` on Neon. Publishes a daily Merkle root at `/.well-known/checks/YYYY-MM-DD.json` (same shape as the snapshot chain), anchor commits weekly per the snapshot cadence.

**Latency:** zero at call-time (`/check.json` unchanged). Verification lag: N minutes (walker cadence). If N=1min, most call-time receipts are verifiable within ~1min. If N=5min, worst-case ~5min stale.

**What a machine at call-time gets:** an envelope with `sig_pending: true`, `sig_expected_after_utc: ISO`, `receipt_url: /.well-known/checks/receipts/<response_id>.json`. The machine polls (or webhooks) the receipt URL and gets the fully-signed version once the walker's next cycle lands. **Not usable for real-time pre-transaction gating** — a machine that needs "should I pay this address?" verified NOW can't wait N minutes.

**Chain-verifiable:** every signed receipt lands in a Merkle tree rolled up at UTC day-close, anchored on XRPL. Third parties can verify a receipt against the day's root against the on-ledger anchor tx. **Best fit for post-hoc audit** (regulators, dispute triage, forensic evidence long after the fact).

**Costs to build:** 1 new PG table (`check_pending_signatures`), 1 new PG table (`check_signed_receipts`), 1 new walker (~150 lines), 1 new receipt-serve endpoint on Render, 1 daily Merkle roll-up walker (share code with `signed_snapshot`'s Merkle helper), 1 anchor covenant update. Larger scope, more moving parts, more places to introduce bugs.

## Comparison table

| dimension | A — Mac-side sig service | B — Batched signing |
|---|---|---|
| Call-time signature | ✅ 99%+ of calls | ❌ empty at call-time; fills in ~N min later |
| Real-time pre-transaction gating | ✅ | ❌ (unless machine tolerates N-min lag) |
| Post-hoc audit trail | ✅ (per-call) | ✅ (per-call, Merkle-anchored) |
| Merkle chain of receipts | Requires adding one — not free with A | ✅ built-in |
| Latency added to /check.json | +30-150ms per response | +0ms |
| New failure modes | +1 (sig-service down) | +2 (walker down, receipt-serve down) |
| Attack surface | +1 hostname | +2 PG tables + 1 walker + 1 serve endpoint |
| Key handling | Local always | Local always |
| Build cost | Small (~200 lines + tunnel host) | Medium (~500+ lines across walker + tables + endpoint + roll-up) |
| Deploys per fix | 2 (Mac sig-service + Render client wrap) | 3 (Mac walker + Render endpoint + Render client wrap + eventual anchor update) |
| Works if Mac is down | Fail-open (unsigned) | Receipts stale beyond N until Mac recovers |
| Works if tunnel is down | Fail-open (unsigned) | Unaffected — Render write path is Neon-direct |

## What each means for a machine at call-time

- **A gets you:** a signed envelope inline, cryptographically verifiable at the moment the paid customer's tool receives it. If a wallet is asking `/check.json` "should I let this payment through?", A returns a signed answer they can log, forward, or dispute-file with the signature attached. The reason a wallet would integrate at all is to have exactly this.
- **B gets you:** an envelope you can eventually verify — think "the compliance officer files it next week" more than "the wallet gates the payment now." Also gives you a chain (which A doesn't natively) that a third-party auditor can walk end-to-end.

## My reading (not a decision)

A is a better fit for the /check product Charlie is trying to build (scam-verifier for humans + pre-transaction gate for machines). Real-time signing is what unlocks the "pay us for the machine surface" story — an unsigned answer is what /check today already returns; a signed answer at call-time is what the paid tier can charge for.

B is a better fit for a slower, higher-assurance layer built alongside A (long-term audit trail, dispute triage), not as A's replacement. Not for tonight and not for the paid tier's launch.

**Cleanest sequence:** ship A. Add B later as the daily-receipts chain if there's demand from auditors/regulators, sharing code with the existing signed_snapshot Merkle helper. Nothing about A precludes B being added on top later.

## Non-decision: shared cost saved with either

Regardless of A or B, both need:
- `/methodology` page update describing the signing model and verification steps
- A published verifier snippet (Python + JS) so integrators can check signatures against the public key without our help
- The pubkey.json + pubkey.pem endpoints (already shipping) as the trust root

## What Charlie needs to decide

**Choose A or B (or "both, A first").** Then I scope the build, propose a ship date, and either take it on next session or defer it to a fix window with more headroom than tonight's had.
