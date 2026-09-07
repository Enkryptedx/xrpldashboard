# Receipt-signing service — Option A deploy guide

**Purpose.** Sign the canonical hash of a `/check.json` response envelope
so the response ships with a verifiable Ed25519 signature made by the
receipt/registry keypair (fingerprint `A4:0F:B1:0A:9D:33:64:03`). Design
note: `triage/SIGNED_RECEIPT_DESIGN_NOTE_2026-09-06.md`. Charlie ruling
2026-09-07: Option A (Mac-side service over the tunnel).

**Constraint.** The Ed25519 private key lives on Charlie's Mac and only
on the Mac. This service does not expose the key material; it exposes
signatures only. The service also does not sign anything but the one
fixed receipt schema. Attempts to sign arbitrary payloads → 400.

## Architecture

```
Render web app                  Mac (Charlies-Mac-mini)
──────────────                  ────────────────────────
/check request →                sig_service.py (Flask)
  build envelope                :8842 (loopback + tunnel)
  compute canonical hash          KeyStore (starts locked)
  POST /sign to tunnel ────▶     unlock via localhost POST
                                  loads ~/.config/xrpldashboard/
   ◀──── {signature, fp,          receipt_ed25519_enc.pem
          domain_sep, ts}
  attach signature to envelope
  return /check response to caller
```

The tunnel: `sig.xrpldashboard.com` → Cloudflare Tunnel → Mac
`127.0.0.1:8842`. CF-Access-authenticated so only Render (or Charlie's
own dev tools) can reach `/sign`. The tunnel **must not** proxy
`/unlock` — that endpoint is loopback-only enforced in code.

## Endpoints

### `GET /status`
Public. Reports service identity + unlock state.

```json
{
  "service": "xrpldashboard-sig-service",
  "unlocked": false,
  "key_fingerprint": "A4:0F:B1:0A:9D:33:64:03",
  "domain_separator": "xrpldashboard/receipt/v1",
  "allowed_kinds": ["check"]
}
```

### `POST /unlock`  (loopback only)
Body: `{"passphrase": "<paper-typed passphrase>"}`. Loads + decrypts
the private key into memory. Idempotent — calling twice is a no-op.
Rejected with 403 if the caller isn't `127.0.0.1` / `::1`.

### `POST /sign`  (tunnel-callable)
Body — exact shape, no polymorphism:

```json
{
  "canonical_hash":  "<64 lowercase hex chars, SHA-256 of the envelope>",
  "response_id":     "<UUID v4>",
  "kind":            "check",
  "issued_at_utc":   "2026-09-07T15:00:00.123456Z"
}
```

Any missing / extra field → 400. Wrong hash length → 400. `kind` not
in `{"check"}` → 400.

Response:

```json
{
  "signature_ed25519_hex":    "<128 hex chars>",
  "signing_key_fingerprint":  "A4:0F:B1:0A:9D:33:64:03",
  "domain_separator":         "xrpldashboard/receipt/v1",
  "signed_at_utc":            "2026-09-07T15:00:00.234567Z"
}
```

**What's signed.** The signature is over the bytes
`DOMAIN_SEPARATOR + 0x00 + bytes.fromhex(canonical_hash)`, i.e.
```
b"xrpldashboard/receipt/v1" + b"\x00" + <32 raw hash bytes>
```
Verifiers apply the same wrap before `Ed25519.verify()`. Cross-domain
replay protection against the snapshot key (which uses a different
domain separator) is inherent to this design.

### Rate limits + observability

- Per-caller-token rate limit: **60 sign requests / 60 seconds**.
  Bucket key = CF-Access-Authenticated-User-Email header, or
  CF-Access-Jwt-Assertion, or `request.remote_addr` fallback.
- Exceeded → 429 with `Retry-After` header.
- Audit log at `launchd_logs/sig_service/YYYY-MM-DD.jsonl`, one JSON
  event per line, 90-day retention. Events: `unlocked`,
  `unlock_failed`, `unlock_rejected_non_loopback`, `signed`,
  `sign_rejected_schema`, `sign_rate_limited`, `sign_failed`.

## Passphrase custody (updated 2026-09-07 afternoon)

**Env-file custody — same pattern as the snapshot key. Paper is the recovery copy, not the operational path.** Charlie ruling: manual unlock proved too fragile for a 24/7 signing service; matching the snapshot-key pattern gets the same operational cadence for the same class of risk.

The sig-service reads `RECEIPT_KEY_PASSPHRASE` from environment at startup, decrypts the private key, and starts READY TO SIGN. Auto-unlock is logged to the audit log (`event: unlocked_from_env`). If the env var is missing or the value doesn't decrypt, the service starts LOCKED with a warning on stderr; the manual `POST /unlock` path remains available as a fallback (loopback-only).

### Setup — one-time (Charlie's keyboard)

1. Open the env file: `nano ~/.config/xrpldashboard/env`
2. Add a new line: `export RECEIPT_KEY_PASSPHRASE=<paper value>`  (same paper value as your recovery record next to the private-key path)
3. Save + exit.
4. Restart the sig-service (or reload the LaunchAgent):
   ```
   launchctl kickstart -k gui/$(id -u)/com.charliebruce.xrpldashboard.sig_service
   ```
5. Verify: `curl -s http://127.0.0.1:8842/status | jq .unlocked` → `true`

### Recovery — if the env var is wiped

If a Mac restore, launchd env file rewrite, or accidental env-file edit wipes `RECEIPT_KEY_PASSPHRASE`, the sig-service starts LOCKED. Recovery from paper:

```
read -s "PP?receipt passphrase from paper: "
curl -s -X POST http://127.0.0.1:8842/unlock \
  -H "Content-Type: application/json" \
  --data-binary "$(python3 -c 'import json, os; print(json.dumps({"passphrase": os.environ["PP"]}))')"
unset PP
```

Then re-add the value to `~/.config/xrpldashboard/env` for the next restart to auto-unlock again.

### Rotation

To rotate the passphrase: generate a new encrypted PEM with a fresh passphrase, update the env var, restart the service. Old and new can coexist during a transition if the new PEM is saved to a `.pem.v2` path and swapped after verification. See docs/CREDENTIALS.md §5.4 for the full rotation flow.

## Install (Mac side, launchd)

`launchd/run_sig_service.sh` and
`launchd/com.charliebruce.xrpldashboard.sig_service.plist` will land in
a follow-up commit; the service can be run manually today for testing:

```
cd ~/xrpl_test
./venv/bin/python3 sig_service.py --bind 127.0.0.1:8842
```

Once the launchd install ships:

```
cp launchd/com.charliebruce.xrpldashboard.sig_service.plist \
   ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) \
   ~/Library/LaunchAgents/com.charliebruce.xrpldashboard.sig_service.plist
launchctl enable gui/$(id -u)/com.charliebruce.xrpldashboard.sig_service
```

Then unlock via the ritual above.

## Install (Render side + Cloudflare)

### 1. Client wrapper (Render code)

Ships in `sig_client.py` (this repo, imported by `app.py`'s `/check` handler):

```python
from sig_client import sign_receipt, attach_to_envelope, SIG_STATUS_SIGNED

# Fire-and-inline signing:
sig_block, status = sign_receipt(canonical_hash="a1b2...", kind="check")
if sig_block:
    envelope["sig_ed25519"] = sig_block["signature_ed25519_hex"]
    # ... plus signing_key_fingerprint, domain_separator, signed_at_utc
else:
    envelope["sig_ed25519"] = None
    envelope["sig_status"] = status   # sig_unreachable / sig_locked / etc.

# Or the one-line convenience:
attach_to_envelope(envelope, canonical_hash="a1b2...")
```

The client is fail-open. Never raises. On any error the envelope
ships with `sig_ed25519: null` and a `sig_status` field naming the
reason. Verifiers see the null + reason and know signing was skipped
for that response.

### 2. Env vars on Render

Add three vars to Render → xrpldashboard → Environment:

| Var | Value |
|---|---|
| `SIG_TUNNEL_URL` | `https://sig.xrpldashboard.com` (no trailing slash) |
| `CF_ACCESS_CLIENT_ID` | already set (used by sovereign_tunnel_client for RPC) |
| `CF_ACCESS_CLIENT_SECRET` | already set |

The Access-service-token pair (`CF_ACCESS_CLIENT_ID` / `_SECRET`) is
the same one that gates `rpc.xrpldashboard.com`. Reusing it is
intentional — same authentication surface, same rotation cadence.

### 3. Cloudflare Tunnel — add the sig hostname (Charlie's keyboard)

**Send-back:** reply **"tunnel added"** when the hostname resolves + returns 401 for an unauthenticated curl.

- Cloudflare Zero Trust dashboard → Networks → Tunnels → your existing tunnel (the one already routing `rpc.xrpldashboard.com`) → **Public hostnames** tab → **Add a public hostname**.
- **Subdomain:** `sig`
- **Domain:** `xrpldashboard.com`
- **Type:** `HTTP`
- **URL:** `127.0.0.1:8842` (the Mac's loopback where sig-service listens)
- Save.

Then add an Access application for the new hostname:

- Zero Trust dashboard → Access → Applications → **Add an application** (or extend the existing RPC application to cover the new hostname).
- **Application type:** Self-hosted
- **Session duration:** whatever the existing RPC application uses (24h is fine).
- **Subdomain:** `sig` · **Domain:** `xrpldashboard.com`
- **Policies:** add the same Service Auth policy that's already on `rpc.xrpldashboard.com`. The service token you set on Render (`CF_ACCESS_CLIENT_ID` / `_SECRET`) must appear in that policy's Include list.
- Save.

### 4. Verify the tunnel + Access setup (Charlie or JJ)

From any host with the CF-Access service token (Render or your Mac):

```
# Should return 200 with the sig-service /status JSON:
curl -sSf \
  -H "CF-Access-Client-Id: <id>" \
  -H "CF-Access-Client-Secret: <secret>" \
  https://sig.xrpldashboard.com/status
```

Without the headers should return 401 or Access's login page HTML.

### 5. Flip Render to use the signer

Once the tunnel + Access are green, deploy Render. The `/check.json`
handler starts calling `sign_receipt(...)` per response. Watch the
Render logs for `sig_status=signed` on healthy calls and
`sig_status=sig_unreachable` if the tunnel flaps — the envelope
still ships either way.

## Failure modes + observability

| Failure | HTTP | What Render should do |
|---|---|---|
| service is locked | 503 | envelope ships with `sig_ed25519: null` + `sig_unreachable_at_utc` field. Fail-open. |
| tunnel is down | timeout | same fail-open |
| passphrase changed on paper without unlock update | 400 unlock | operator alert, service stays locked |
| rate limit exceeded | 429 | envelope ships with `sig_ed25519: null` + `sig_rate_limited: true` |
| schema violation | 400 | code bug on Render side; escalate |

The `sig_ed25519: null` fail-open is by design — /check.json never
blocks on the signing round-trip. Verifiers see the absence and know
why. Latency budget: ~30-150ms per response over the tunnel; if this
becomes noticeable in prod, an async batch mode is in the design note
under Option B.

## Verification (any third party)

Fetch three artifacts:

1. `xrpldashboard.com/.well-known/snapshots/receipt_pubkey.pem`
2. `xrpldashboard.com/.well-known/snapshots/receipt_pubkey.json` (matches PEM)
3. `dig +short _xrpld-receipt-key.xrpldashboard.com TXT` (matches JSON)

All three publish the same 32-byte pubkey. Any signature that fails
`Ed25519.verify(pub, DOMAIN_SEPARATOR + 0x00 + canonical_hash, signature)`
is either forged, corrupted, or the receipt was tampered with post-signing.

Reference verifier: `xrpl_notary_verify.verify_receipt(...)` in the
repo carries the reference implementation.

## Related

- Design note: `triage/SIGNED_RECEIPT_DESIGN_NOTE_2026-09-06.md`
- Credentials runbook: `docs/CREDENTIALS.md §5`
- Ledger anchor: `docs/anchor_history.md` (a different signing key —
  the snapshot key with `_xrpld-snapshot-key` DNS entry)
