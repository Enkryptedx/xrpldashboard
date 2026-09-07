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

## Passphrase custody

**Per Charlie's ruling 2026-09-07: the receipt keypair passphrase lives
on paper only.** No Keychain, no 1Password, no env file.

Consequence: the sig-service starts LOCKED after every Mac reboot or
service restart. Charlie unlocks it manually via a localhost curl,
typing the paper value at a `read -s` prompt so it never lands in
shell history.

Unlock ritual (after every restart):

```
ssh into the Mac (or open Terminal directly) and run:

read -s "PP?receipt paraphrase: "
curl -s -X POST http://127.0.0.1:8842/unlock \
  -H "Content-Type: application/json" \
  --data-binary "$(python3 -c 'import json, os; print(json.dumps({"passphrase": os.environ["PP"]}))')"
unset PP
```

Response `{"unlocked": true, ...}` = ready to sign.

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

## Install (Render side)

The client wrapper (`sovereign_tunnel_client.sign_receipt(...)`) will
land in a follow-up. Shape:

```python
def sign_receipt(canonical_hash, response_id, kind="check", issued_at_utc=None):
    """POST to sig.xrpldashboard.com/sign via CF-Access-authenticated tunnel.
    Returns signature dict on success. Fails-OPEN on network / 503 —
    envelope ships with sig_ed25519 = null and sig_unreachable_at_utc set."""
    ...
```

The tunnel hostname `sig.xrpldashboard.com` needs to be added to the
existing Cloudflare Tunnel that already handles `rpc.xrpldashboard.com`
(sovereign XRPL). Same CF-Access policy applies. The tunnel origin
target is `http://127.0.0.1:8842`.

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
