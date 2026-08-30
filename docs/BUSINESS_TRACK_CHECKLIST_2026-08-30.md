# Business-Track Checklist — Machine Payments Wave 1

**Filed:** 2026-08-30 18:xx EDT after Charlie's strategic-build design-pack approval.
**Bound:** Business items that MUST run in parallel with the technical build. Each item is Charlie's keyboard — never mine. Serve status updates here when items move.
**Ruling calendar (from design pack):** Mon 08-31 EOD — Wound-B fix approach · Tue 09-01 EOD — envelope schema fields · Thu 09-03 EOD — homoglyph confusables source · Fri 09-04 — anchor #5 ceremony · Wed 09-10 — facilitator decision · ~09-20 — legal sign-off · ~09-25 — mode flip on.

---

## THIS WEEK (Mon 08-31 → Fri 09-04)

### 1. Stripe account
- [ ] Create Stripe account under the xrpldashboard.com business identity
- [ ] Enable "test mode" only for now — no live keys in code
- [ ] Publishable + secret keys land in `~/.config/xrpldashboard/env` (Mac) + `~/xrpldashboard/.env` (Lenovo), NEVER in git
- [ ] Add `STRIPE_PUBLISHABLE_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` to `.env.example` with placeholder values (already there? verify)
- [ ] Webhook endpoint scaffolded — `stripe_events` table already exists at `db.py:1050`

### 2. ToS draft — attestation-not-safety
- [ ] Draft ToS using the phrase "attestation, not safety advice" verbatim (Fence #8 language)
- [ ] Sections: (a) what /check produces, (b) what /check does NOT produce, (c) no warranty of counterparty behavior, (d) verdict = function of publicly observable signals at `as_of` timestamp, (e) verified tier = TOML two-way match, NOT endorsement
- [ ] Include per-response disclaimer text that mirrors ToS
- [ ] Location: `docs/TOS_v1_DRAFT.md` — legal-review pass ~09-20 before publish

### 3. Pricing settle — freemium keyed first
- [ ] Confirm tier structure: Free anon (60/hr IP), Free keyed (1k/day, email req), Pro ($49/mo, 100k/mo + batch-100 + WATCH), Team ($299/mo, 1M/mo + webhooks)
- [ ] x402 pay-per-call ($0.001/call) DEFERRED to Wave 3 (v1.2) — needs Wed 09-10 facilitator decision
- [ ] Rules for tier upgrades / downgrades / cancellations (Stripe subscription lifecycle)

### 4. Accounting / tax / funds destination
- [ ] Where do Stripe payouts land? (Charlie's business bank account — confirm routing)
- [ ] Where do RLUSD payments land? (Operator wallet `rwrcJL…TXfd` already LIVE, RLUSD trust line set) — but RLUSD → USD off-ramp path is a separate question
- [ ] 1099-K / sales tax posture per jurisdiction (Stripe handles most, but confirm)
- [ ] Bookkeeping category: "Attestation-as-a-Service revenue" — call this out to accountant

---

## WEDNESDAY 2026-09-10 (facilitator decision)

- [ ] **RULING: T54 client vs self-host `t54-labs/x402-xrpl`**
  - Both open-source, both production-tested
  - T54 client = faster to integrate, dependency on their uptime SLA (none published)
  - Self-host = more work but sovereign; matches Fence-doctrine
  - Concrete t54 outreach hook either way: "we're integrating your rails" gives cold-outreach reason
- [ ] This ruling GATES Wave 3 (x402 dry-run on /check.json/batch + /watch)

---

## ~2026-09-20 (legal sign-off)

- [ ] ToS draft reviewed (self-review OR lawyer — Charlie's call)
- [ ] Attestation-not-safety framing survives review
- [ ] Publish `docs/TOS_v1.md` at `/tos` route (needs one-line app.py route)

---

## ~2026-09-25 (charging-live flip)

- [ ] Charlie ships from his keyboard: `X402_ENFORCEMENT=dry_run` → `on` for `/check.json/batch` + `/watch` only
- [ ] Verify metering table receiving writes for first minute after flip
- [ ] Watch for 402 responses in access logs
- [ ] First test payment: Charlie's own wallet → operator wallet → verify RLUSD credit posts

---

## Standing rules — do not violate

- **Fence #4:** No custody, ever. Operator wallet is transit only. RLUSD off-ramp to USD is Charlie's manual step, not automated.
- **Fence #8:** SELLABLE_REQUIRES_SOVEREIGN_SOURCE. Only own-node-sourced endpoints can charge. Anything routed through third-party RPC or API pull downgrades to at most `dry_run`. Enforced in `x402_rails.py:effective_enforcement_mode`.
- **Charlie's keyboard for:** x402 mode changes · DB schema migrations (staging first) · charging-live flip · mainnet credential-issuer transactions · git pushes from `Enkryptedx` identity.

---

## Status log (append as items move)

- **2026-08-30 18:xx EDT** — File filed. Nothing checked off yet.
