# Receipt keypair generation — walkthrough for Charlie's keyboard

**Prepared:** 2026-09-06 late evening ET · JJ · for tomorrow morning
**Purpose:** Generate ONE Ed25519 keypair used for both
  (a) signed `/check.json` receipts AND
  (b) signed daily registry snapshots.
  Separate from the snapshot/anchor key. Never leaves the Mac. Passphrase
  stored on paper only (per `no_1password_keychain`).

**Send-back protocol:** each step is one message from JJ. Charlie runs the
command, pastes back the output (no secret content). JJ confirms + advances.
Nothing before Charlie is at the keyboard + ready — the whole sequence must
run without interruption because it involves the passphrase entered live.

---

## Pre-flight checklist (JJ does this now, one line each)

- [x] `openssl` installed (LibreSSL 3.3.6 default macOS ✓)
- [x] Publisher path `/Users/charliebruce/xrpl_test/public/.well-known/snapshots/` writable ✓ (verified same path snapshot pubkey uses)
- [x] `sig-service` code NOT built yet — walkthrough only generates key + publishes pubkey; sig-service ships after key exists
- [ ] Charlie has paper + pen for the passphrase before we start

## Send-back sequence — 6 messages, one action per

### Message 1 (from JJ)
> **Ready when you are.** Paper + pen in hand? Reply "go" and I send step 1.

### Message 2 (from JJ, after Charlie's "go")
> **Step 1 — pick a passphrase and write it down.**
> - 20+ characters, mixed case + numbers + a symbol
> - NOT any password you use elsewhere
> - Write it on the paper. Nothing to run yet.
> Reply "written" when the paper is in your pocket.

### Message 3 (from JJ, after "written")
> **Step 2 — generate the private key.** Copy-paste this ONE command:
> ```
> openssl genpkey -algorithm ED25519 -aes-256-cbc \
>   -out ~/.config/xrpldashboard/receipt_key_v1.pem
> ```
> openssl will prompt twice for the passphrase — enter the same one from your paper both times. Reply "done" when the shell returns.

### Message 4 (from JJ, after "done")
> **Step 3 — extract the public key.** Copy-paste this ONE command:
> ```
> openssl pkey -in ~/.config/xrpldashboard/receipt_key_v1.pem \
>   -pubout -out ~/xrpl_test/public/.well-known/snapshots/receipt_pubkey.pem
> ```
> Enter the passphrase once when prompted. Paste back the shell return (no content — just confirmation the shell prompt reappeared).

### Message 5 (from JJ, after Charlie confirms)
> **Step 4 — verify the pubkey file exists + get its fingerprint.** Paste this ONE command AND paste back its output (safe, it's the pubkey fingerprint):
> ```
> openssl pkey -in ~/xrpl_test/public/.well-known/snapshots/receipt_pubkey.pem \
>   -pubin -outform DER | openssl dgst -sha256 | awk '{print $2}'
> ```
> The output will look like `a1b2c3...` — 64 hex chars. Paste it here.

### Message 6 (from JJ, after fingerprint)
> **Step 5 — publish the pubkey JSON.** JJ writes this file directly (no secrets involved), you just confirm the file lands. Reply "go" and I write it.
>
> After JJ writes the JSON, Charlie's ONE remaining task is the DNS TXT record — a separate walkthrough scheduled for a different sitting since it requires the Cloudflare dashboard.

### Post-walkthrough (JJ, after all above lands)
> **Recap for the log:**
> - Private key: `~/.config/xrpldashboard/receipt_key_v1.pem` (encrypted with your passphrase)
> - Public key PEM: `~/xrpl_test/public/.well-known/snapshots/receipt_pubkey.pem`
> - Public key JSON: `~/xrpl_test/public/.well-known/snapshots/receipt_pubkey.json` (JJ-written; includes fingerprint + algorithm + created_at + domain-separator)
> - Fingerprint: (whatever Charlie pasted)
> - Passphrase: on paper only — not stored anywhere digital, not in Keychain, not in 1Password
>
> **Next session:** JJ builds the sig-service (~250-line Flask app). Charlie's keyboard needed only if the sig-service ever needs to READ the private key at server start — which it does. That's a launchctl plist that prompts for the passphrase on load; deferred to that session's own walkthrough.

---

## Rules JJ follows during the walkthrough

- **One action per message. Wait for Charlie's response before advancing.** No batching, no "run these 3 commands" — Charlie is switching between terminal and Telegram.
- **No secret content ever appears in chat.** JJ never asks for the passphrase. JJ never asks for the private key content. If Charlie accidentally pastes something secret-looking, JJ drops it and flags per the Tier 0 `secrets_work_removed_from_scope` rule.
- **Pre-scan every command JJ sends** per the `pre_send_shell_check` rule: `!` inside double quotes → refactor; unquoted globs → single-quote; destructive verbs (`rm mv cp dd`) → not in this walkthrough; unintended `$(…)` / backticks → none; secrets-path reads that print values → none.
- **If any step errors:** JJ pauses the sequence, asks Charlie to paste the error, JJ diagnoses BEFORE sending the next command. Never advance past an error.
- **Fallback if openssl fails:** the walkthrough uses macOS default openssl (LibreSSL 3.3.6). If Charlie's system has homebrew openssl at `/opt/homebrew/bin/openssl` and it errors differently, JJ has a Python-cryptography fallback ready (`python3 -c "import cryptography.hazmat.primitives.asymmetric.ed25519 as e; …"`) — Charlie won't see this unless the openssl path fails.

## Rules JJ enforces on the PUBKEY JSON content

JSON structure at `receipt_pubkey.json`:
```
{
  "purpose": "receipt-signing + registry-snapshot-signing",
  "algorithm": "ed25519",
  "domain_separator": "xrpldashboard/receipt/v1",
  "public_key_pem_path": "/.well-known/snapshots/receipt_pubkey.pem",
  "fingerprint_sha256": "<hex from Step 5>",
  "created_at": "<ISO date>",
  "curator": "Charlie Bruce",
  "notes": "This key signs check.json receipts and registry daily snapshots. Distinct from the snapshot/anchor key which signs historical ledger snapshots. Verifier libraries should apply the domain separator before Ed25519 verify to prevent cross-domain replay."
}
```

Not renderable in chat until step 5 has landed. JJ generates the file, Charlie confirms it's on disk, then the DNS TXT publish is the follow-up walkthrough.

## Post-walkthrough checklist

- [ ] Private key created + passphrase-encrypted
- [ ] Public key PEM extracted
- [ ] Fingerprint captured (paste to session record)
- [ ] Pubkey JSON written by JJ
- [ ] Pubkey JSON serves via Render at `/.well-known/snapshots/receipt_pubkey.json` (after next push)
- [ ] DNS TXT record for `receipt._xrpldashboard.com` — separate walkthrough
- [ ] sig-service Flask app — separate JJ session after all of the above land

Once complete, gates:
- L2c self-submission form (registry spec step 8)
- Signed daily registry snapshot (registry spec step 9)
- Signed `/check.json` receipts (registry spec step 12 → paid tier)
