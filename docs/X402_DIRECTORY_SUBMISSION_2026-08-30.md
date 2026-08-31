# x402 directory submission — steps for Charlie's keyboard

Filed 2026-08-30 with the /.well-known/x402 catalog ship. Per Charlie's item
3 in the two-hour evening sprint: serve the directory-submission steps for
his later keyboard (form, PR, or crawl — how t54's listing is requested).

## Short answer

**Neither form nor PR — auto-discovery via facilitator settlement.**

Per docs.x402.org/extensions/bazaar (fetched 2026-08-30):

> "As a seller, there is no submission form or PR process described. Instead,
> add the bazaar extension to your route configuration to make your API or
> MCP tools discoverable."
>
> "Facilitators that support the Bazaar extension may provide a
> `/discovery/resources` endpoint that returns all x402-compatible services
> registered through the respective facilitator."
>
> "Catalog indexing is a facilitator implementation detail, not something
> the x402 OSS repo controls."

Translation: our service gets listed **only when a real payment settles
through a facilitator that supports Bazaar**, and only if our route
configuration echoes the `bazaar` extension in the PaymentPayload.

That is not possible today because x402 rails are `mode=off` (dark).

## Path A — Today (free-tier, static discovery hint)

Immediate, requires no Charlie action beyond confirming deploy:

1. **Verify /.well-known/x402 is live post-deploy**

   ```sh
   curl -sS https://xrpldashboard.com/.well-known/x402 | jq '.x402Version, .resource, .accepts, .status'
   ```

   Expected: `x402Version: 1`, `resource: ".../check.json"`, `accepts: []`,
   `status.free_tier_ready: true`, `status.x402_rails_ready: false`.

2. **Optional announce in xrpl-ai / t54 dev channels**

   Point developers who ask about free XRPL identity verification at
   `https://xrpldashboard.com/.well-known/x402`. Some directories crawl
   /.well-known/ paths as a discovery hint even without formal registration.

3. **No form to fill, no PR to raise.** The catalog exists; whether any
   directory picks it up before we go live is not under our control.

## Path B — After 09-25 mode-flip (real Bazaar listing)

Blocked on Fence-#8 sovereignty items (see
`docs/SOVEREIGNTY_COVENANT_VIOLATIONS_2026-08-30.md`, RLUSD → s1.ripple.com
CRITICAL). When those close and Charlie flips `X402_RAILS_MODE=live`:

1. **Wire the `bazaar` extension into x402_rails.py**

   Currently `x402_rails.py` does not add `extensions.bazaar` to the
   `PaymentRequirements` response served on 402. Add it — the shape mirrors
   the `extensions.bazaar.info` block in `_X402_CATALOG` (app.py). One
   config entry per route we sell.

   Reference: `docs.x402.org/extensions/bazaar` §"Route configuration".

2. **Confirm facilitator support**

   XRPL Facilitator lives at `xrpl-facilitator-mainnet.t54.ai` (per
   xrpl-ai.org/build). Confirm it supports Bazaar (has a
   `/discovery/resources` endpoint) before flipping live — a facilitator
   without Bazaar support will settle payments but never index us.

   ```sh
   curl -sS https://xrpl-facilitator-mainnet.t54.ai/discovery/resources | jq '.[].resource' | head
   ```

   Expected: a JSON array of already-listed resources. If 404 or empty,
   Bazaar isn't wired at this facilitator and Path B doesn't unlock a
   listing.

3. **Ship a small no-op payment as smoke test**

   Once wired live: send a self-payment through the facilitator (min amount,
   our own address to our own address) with the `bazaar` extension echoed.
   Facilitator settles → indexes → our resource appears in `/discovery/resources`.

4. **Watch for the listing**

   ```sh
   watch -n 60 'curl -sS https://xrpl-facilitator-mainnet.t54.ai/discovery/resources | jq "map(select(.resource | contains(\"xrpldashboard\")))"'
   ```

   Once we appear, propagation to any UI-facing directory (t54's browsable
   catalog, xrpl-utilities.io's sentinel view) is up to them and typically
   takes 5-60 minutes.

## Path C — Alternate facilitators (contingency)

If t54's XRPL facilitator does not support Bazaar (checked in Path B step 2),
options:

- Contribute Bazaar support upstream (t54's facilitator repo — link TBD from
  their site; if not public, DM channel below).
- Run our own Bazaar-aware facilitator on the Lenovo box (owns the whole
  stack; matches sovereignty covenant). Non-trivial — probably 1-2 dev days
  and only worth it if t54's stays dark.

## Contact / DM channels for follow-up

Recorded here so Charlie doesn't have to hunt again:

- **t54 principal (x402 XRPL implementer)** — Charlie sent unencrypted X DM
  ~2026-08-24, probably landed in message requests. No confirmed email
  route. See `MEMORY.md → watch_t54_outreach.md` for details.
- **xrpl-ai.org / xrpl-utilities.io** — reachable via GitHub repo issues on
  the sentinel project (need to grep for the org). Not urgent tonight.
- **Discord/Slack** — no confirmed channel yet; if t54 or xrpl-ai runs one,
  add here when found.

## Charlie's ruling queue (for follow-up)

- **Pricing tier for /check.json** — free-tier locked in the catalog now
  (`accepts: []`). When mode-flip lands, need Charlie's call on: keep free,
  add a paid `high-volume` tier (X drops/req), or add a paid `signed-verdict`
  tier (X drops/req for signed response, free unsigned).
- **Signing-key publisher** — `/.well-known/check/pubkey.pem` currently
  404s (not wired). Blocking a future third-party verifier that wants to
  cache the hot pubkey. Small task, ship alongside first pricing decision.
- **Post-listing monitoring** — once we appear in a directory's
  `/discovery/resources`, we should have a walker that alerts if our entry
  ages out or gets rejected (bazaar spec has a `bazaar.status: "rejected"`
  branch). Design owed after the listing lands.
