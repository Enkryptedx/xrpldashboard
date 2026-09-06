# Sourcing Disclosure Spec (rewrite, 2026-09-06)

**Original spec (2026-08-31) was lost.** Reconstructed 2026-09-06 from Charlie's re-transmission of the rule and the Sep-2 ruling that preceded it. Also filed to auto-loaded memory: `memory/project_billing_pause_rule.md`.

**Status when this spec was rewritten:** rule active. Building the middleware behind the existing `X402_ENFORCEMENT` env switch (default `off`); the rule fires even when enforcement is off so the response shape (`sourcing` field, `billed` field, `billing_reason` field) is stable and machines can rely on it now.

---

## The rule (Option B, ruled 2026-09-02)

**Any paid call whose response sourcing is anything other than `sovereign` is SERVED normally but NOT METERED or charged.** The response carries:

- `billed: false` (on the paid path when the pause fires)
- `billing_reason: <enum>`

Billing resumes automatically when sourcing returns to `sovereign`. No customer action needed. Not sticky — the next call is evaluated fresh.

## `billing_reason` enum (frozen)

| value | when |
|---|---|
| `sovereign-path-unavailable` | The tunnel / own-node fell back to public XRPL RPC. `SovereignFetcher.sourcing = "fallback-public-rpc"`. |
| `stale-cache` | Served a cached DB row past its freshness bound. `SovereignFetcher.sourcing = "stale-cache"`. |
| `sourcing-unknown` | Sourcing signal unavailable at response time — treated as non-sovereign (fail-closed on unknown, not fail-open). Should be rare; investigation-worthy every time. |

Adding a value later requires a governance decision, not a code decision. Every existing paid customer has to be able to enumerate the reason space to react to it, so we don't extend it silently.

## `unbilled_calls` table (append-only legal audit trail)

Schema:

```sql
CREATE TABLE unbilled_calls (
  id                  BIGSERIAL   PRIMARY KEY,
  ts_utc              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  endpoint            TEXT        NOT NULL,
  request_id          TEXT        NOT NULL,
  sourcing            TEXT        NOT NULL,
  billing_reason      TEXT        NOT NULL,
  client_identifier   TEXT,        -- wallet / API key / x402 payer id; NULL for anonymous
  canonical_hash      TEXT        NOT NULL,  -- same hash the proof envelope carries
  response_bytes      INTEGER     NOT NULL
);
CREATE INDEX unbilled_calls_ts_idx        ON unbilled_calls (ts_utc DESC);
CREATE INDEX unbilled_calls_endpoint_idx  ON unbilled_calls (endpoint, ts_utc DESC);
CREATE INDEX unbilled_calls_client_idx    ON unbilled_calls (client_identifier, ts_utc DESC) WHERE client_identifier IS NOT NULL;
CREATE INDEX unbilled_calls_reason_idx    ON unbilled_calls (billing_reason, ts_utc DESC);
```

**Never deleted.** Retention parity with `page_views` — the row IS the legal audit that we didn't charge for a specific call. Rotation only via explicit governance decision (never a "cleanup" cron).

## Disclosure principles (drafted 2026-09-02, restated here)

1. **Same field, same visibility on free and paid.** The `sourcing` field is IDENTICAL on free and paid responses. `billed` is `false` (or absent) on free; on paid it's `true` when metered, `false` when the pause fired. **Never hide the reason from a free caller.** If we know we cascaded to public, both a paid customer and an anonymous user see the same disclosure.

2. **In-band with the response, not polled.** The disclosure is a field on the response body itself. Not a separate endpoint the client has to hit to learn we degraded. A caller consuming `/check.json` sees the pause reason on the same response — they don't need to poll a status page.

3. **Worst-case wins when a response mixes sources.** If a single response fetched three RPCs and one cascaded to public, the response's `sourcing` is `fallback-public-rpc` and it isn't billed. Precedence: `fallback-public-rpc > public-no-tunnel-configured > stale-cache > sovereign` (already implemented as `sovereign_tunnel_client.worse_sourcing`).

4. **Logged proof per paid call.** Every unbilled paid call writes exactly one row to `unbilled_calls`. Row's `canonical_hash` matches the hash the response envelope carries so an integrator can join their receipt to our audit later. No sampling, no aggregation. One paid call → one row when the pause fires.

## Middleware placement

Lives in `x402_rails.py::pause_if_non_sovereign(response, meta)`. Called from the wrapper `x402_paid` decorator immediately AFTER the endpoint has served the response and BEFORE the metering hook fires. If sourcing != sovereign:

1. Set `response.data["billed"] = False`
2. Set `response.data["billing_reason"] = <enum>`
3. Skip the metering hook (return response without charging)
4. Write one `unbilled_calls` row (idempotent by request_id — same request retried by the client doesn't double-log)

If sourcing == sovereign:
- Set `response.data["billed"] = True`
- Call the metering hook as normal

## Symmetry on the free path

Free responses today carry `sourcing` (already shipped for `/check.json`, `/lending`, `/cold-storage`, `/pools` via `SovereignFetcher.sourcing`). Adding `billed` on the free path is trivial — it's always `false` (or absent — the field's presence is optional on free). The point of the symmetry rule is that a free caller looking at a fallback response sees the SAME reason a paid caller would — they aren't given less honesty because they're not paying.

## Testing shape

Unit tests need to cover 4 cases:

1. **Sovereign** → `billed: true`, no `billing_reason`, `unbilled_calls` NOT written, metering hook CALLED
2. **`fallback-public-rpc` sourcing** → `billed: false`, `billing_reason: sovereign-path-unavailable`, one `unbilled_calls` row written, metering hook NOT called
3. **`stale-cache` sourcing** → `billed: false`, `billing_reason: stale-cache`, one `unbilled_calls` row written, metering hook NOT called
4. **Missing sourcing field (should never happen but fail-closed)** → `billed: false`, `billing_reason: sourcing-unknown`, one `unbilled_calls` row written, metering hook NOT called

Plus one integration test: enforce that a repeated request with the same `request_id` in the same cascade produces a SINGLE `unbilled_calls` row (not two).

## Cross-refs

- Auto-loaded memory: `memory/project_billing_pause_rule.md`
- Static invariant: `docs/X402_RAILS_DARK_SCOPING.md` line 151 + Fence #8 in `x402_rails.py`
- Sourcing constants: `sovereign_tunnel_client.py::SOURCING_*`
- Worse-of helper: `sovereign_tunnel_client.py::worse_sourcing`
