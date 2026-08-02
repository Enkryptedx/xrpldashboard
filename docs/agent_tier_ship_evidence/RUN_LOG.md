# Agent-tier ship-gate demo — RUN LOG

Textual evidence for the Day 7 (2026-08-02) live MCP demo through Claude
Desktop. Companion to `DEMO_PACKET.md` (which specified what the run
should look like); this file reports what actually happened.

Screenshots (six numeric-prefixed PNGs per the packet's §4 capture spec)
remain Charlie's capture — the pixels live in his Claude Desktop window
and aren't reproducible from this side. Where screenshots would carry
extra force, this document names which moments most need the frame.

---

## Provenance legend

Every non-trivial line below carries one of four marks so the reader
can tell held-record from reconstruction. Evidence declares its chain.

- **[FROM-TRANSCRIPT]** — Verbatim from Charlie's messages during the
  demo session (message IDs 10378–10400, EDT timestamps preserved).
- **[FROM-INVENTORY]** — Canonical value that lives in the code —
  `AGENT_TIER_MCP_INVENTORY` in `app.py`, `CLAIMS.yaml`, or the tool
  module's `wrap_envelope` call. Verifiable at HEAD (commit `60f3f1c`
  or later).
- **[FROM-USER-REPORT]** — Paraphrased from Charlie's message body
  describing what he saw on his screen; not the exact envelope bytes
  (those are in the screenshots).
- **[RECONSTRUCTED]** — Summarized from held records, not a direct
  quote. Used where the summary carried the fact but not the wording.

Where a specific field's live value (e.g. `as_of` timestamp,
`ledger_index`) is NOT reproducible from this side, the field is
marked `<not held — see PNG>` rather than filled in from a plausible
guess.

---

## Environment

- **Date/time window:** 2026-08-02, 14:24 → 14:50 EDT (26 minutes total).
- **Host:** Charlie's M4 Mac (`charlies-mac-mini-local`).
  [FROM-TRANSCRIPT — Claude Desktop config `remoteToolsDeviceName`]
- **Transport:** stdio, foreground. Claude Desktop launched the child
  process via `~/Library/Application Support/Claude/claude_desktop_config.json`
  → `mcpServers.xrpldashboard-mcp`. [FROM-TRANSCRIPT]
- **MCP client:** Claude Desktop (Sonnet 5 chosen from model picker at
  14:34 EDT — the four choices offered were Fable 5, Opus 5, Sonnet 5,
  Haiku 4.5). [FROM-TRANSCRIPT]
- **Server commit:** `60f3f1c` (agent-tier: Day 7 ship-gate demo packet),
  parent `8a5f53a` (signed-snapshot MCP tools #18 + #19). [FROM-INVENTORY]
- **Tool count on attach:** 15. [FROM-INVENTORY — `_register_tools`
  returns 15; `AGENT_TIER_MCP_INVENTORY` has 15 entries; smoke-test
  emitted `registered 15 tool(s)` line before Claude Desktop first
  attached]
- **Bar re-walk immediately preceding this run:** 6/6 PASS, full test
  suite 74/74 green in 16.85s. [FROM-INVENTORY — see
  `project_agent_tier_build_active.md` and `DEMO_PACKET.md` §preamble]

---

## Startup — two incidents that ARE evidence

Two things went wrong on the way to Prompt 1. Both are worth logging
because the *failure mode* is the honest-fail contract in action.

### Incident 1: mcp==2.0.0 dropped `mcp.server.fastmcp`

**14:27 EDT.** [FROM-TRANSCRIPT] Claude Desktop returned red
"Could not attach to MCP server xrpldashboard-mcp".
[FROM-TRANSCRIPT — msg 10382, screenshot]

**Root cause:** [RECONSTRUCTED] `requirements.txt` pinned `mcp==2.0.0`
but `mcp_server.py` targets the SDK 1.x layout
(`from mcp.server.fastmcp import FastMCP`). The 2.0 release reorganized
and dropped that submodule; import failed on server launch, Claude
Desktop couldn't attach.

**Fix:** [FROM-INVENTORY — `requirements.txt` at HEAD]
`venv/bin/python -m pip install 'mcp==1.29.0' -q`, then
`requirements.txt` updated to `mcp==1.29.0` (pinned back to the version
the code actually uses).

**Verification:** [RECONSTRUCTED] Smoke-test `python mcp_server.py`
emitted `registered 15 tool(s)` — the load-bearing invariant the
`DEMO_PACKET.md` §1 gate calls for before proceeding to Claude Desktop.

### Incident 2: DATABASE_URL not inherited by child process

**14:40 EDT.** [FROM-TRANSCRIPT — msg 10394] Prompt 2 first attempt
returned:
> Error executing tool get_token_attestation: DATABASE_URL not
> configured. Want me to retry, or is this a known outage tied to the
> Mac mini reboot issues?

**Root cause:** [RECONSTRUCTED] Claude Desktop's launcher does not
inherit the user shell environment. `MCP_TRANSPORT=stdio` in the JSON
`env` block was the only variable set. The Postgres-backed tools need
`DATABASE_URL`; the ledger-primitives tools silently fall back to
public s1 without `XRPL_LOCAL_NODE` / `XRPL_NODE`.

**Fix:** [FROM-INVENTORY — `launchd/run_mcp_server.sh` at HEAD] Wrote
`launchd/run_mcp_server.sh` on the pattern every walker wrapper in this
repo uses — sources `~/.config/xrpldashboard/env` (fleet-wide env
source-of-truth), `set -a` / `set +a` so all sourced vars auto-export,
then `exec` the venv Python + `mcp_server.py`. Exits non-zero if the
env file is missing so Claude Desktop shows "Server disconnected"
rather than silently attach with a broken toolset.

Updated `claude_desktop_config.json` `mcpServers.xrpldashboard-mcp.command`
to point at the wrapper.

**Verification:** [FROM-USER-REPORT — msg 10396] Prompt 2 retry
succeeded: attestation tier, dispute URL, and issuer name all populated.

**Why this belongs in the evidence log:** the tool refused to return
a response rather than silently returning stub or degraded data —
that IS the honest-fail contract on camera. A tool that has no
DATABASE_URL and pretends to answer anyway is exactly the failure mode
the four-layer audit exists to prevent.

---

## The four prompts

### Prompt 1 — envelope surfacing

**14:36 EDT — permission dialog.** Claude Desktop prompted "Claude
wants to use Get ledger stats" — Charlie approved. [FROM-TRANSCRIPT — msg 10390]

**14:37 EDT — response received.** [FROM-TRANSCRIPT — msg 10392]

**Prompt (verbatim, DEMO_PACKET.md §3 Prompt 1):**
> Use the xrpldashboard-mcp server. Call get_ledger_stats. Show me the
> raw envelope in a code block — I want to see the `proof` and `server`
> fields, not just the ledger index.

**Tool fired:** `get_ledger_stats`. [FROM-INVENTORY]

**Envelope key fields:**
- `proof.source`: `"local_rippled"` [FROM-INVENTORY]
- `proof.freshness_contract`: `"≤ 5min"` [FROM-INVENTORY]
- `proof.methodology_url`: `.../methodology#ledger` [FROM-INVENTORY]
- `proof.claims_ref`: `"ledger_stats_live"` [FROM-INVENTORY]
- `proof.honest_partial`: `false` [FROM-USER-REPORT]
- `data.ledger_index`: `<not held — see PNG>`
- `data.close_time`: `<not held — see PNG>`
- `data.server_state`: `null` [FROM-USER-REPORT]
- `data.build_version`: `null` [FROM-USER-REPORT]
- `data.hostid`: `null` [FROM-USER-REPORT]
- `server.public_key_fingerprint`: `<not held — see PNG>`

**Star of the prompt — unprompted honesty-gap catch:**
[FROM-TRANSCRIPT — msg 10392] Sonnet 5 flagged, without being asked,
that `honest_partial: false` sitting next to three null fields
(`server_state`, `build_version`, `hostid`) is a "silent-write-style
gap" and reads as a contract-vs-value mismatch. Charlie's own
follow-up:
> The `honest_partial: false` next to three null fields is still the
> real gap, though — the envelope's own contract says "flag when we're
> not showing you the full picture," and three silently-dropped fields
> is exactly that case. Agreed it's backlog, not a mid-demo fix.
[FROM-TRANSCRIPT — msg 10392]

This is a positive receipt: an independent model reading the envelope
diagnosed a real contract gap in the wild, on the very tool call that
was supposed to prove the envelope is machine-legible.

**Post-ship backlog:** when any surfaced field is `null`, the tool
should flip `honest_partial: true` and add `scope_note` naming which
fields + why. Logged.

**Screenshot need:** medium. The nulls-plus-honest_partial:false gap
reads better in text than in a screen; the frame is nice-to-have.

### Prompt 2 — third-party-naming + dispute channel

**14:40 EDT — first attempt failed** (Incident 2, above).
[FROM-TRANSCRIPT — msg 10394]

**14:44 EDT — retry succeeded.** [FROM-TRANSCRIPT — msg 10396]

**Prompt (verbatim, DEMO_PACKET.md §3 Prompt 2):**
> Call get_token_attestation for currency=RLUSD
> issuer=rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De. I want to know two things:
> the attestation tier, and — inside the data payload — the
> `dispute_contact_url` the tool includes for issuers who disagree with
> our label.

**Tool fired:** `get_token_attestation`. [FROM-INVENTORY]

**Envelope key fields:**
- `proof.source`: `"neon_postgres"` [FROM-INVENTORY]
- `proof.freshness_contract`: `"daily"` [FROM-INVENTORY]
- `proof.claims_ref`: `"token_attestation_status"` [FROM-INVENTORY]
- `proof.honest_partial`: `false` [FROM-USER-REPORT — this time correct;
  no nulls in core fields]
- `data.tier`: `"verified"` [FROM-USER-REPORT]
- `data.dispute_contact_url`:
  `"https://xrpldashboard.com/contact?purpose=attestation-dispute"`
  [FROM-USER-REPORT — matches DEMO_PACKET expected shape]
- `data.issuer_name`: `"Ripple (Ripple USD issuer)"` [FROM-USER-REPORT]

**Star of the prompt — third-party-naming discipline visible in one call.**
The envelope both names a third party (Ripple) and carries the channel
by which that third party can dispute the label. This is the piece
competitors don't ship. [RECONSTRUCTED — codified in memory
`project_agent_tier_build_active.md` §"Five judgment calls locked" #2]

**Screenshot need:** high. `dispute_contact_url` visible next to
`issuer_name: "Ripple (Ripple USD issuer)"` in the same data payload
IS the founding artifact of the third-party-naming discipline. Text
reproduces the fact; a frame proves it landed under the tool call
Charlie made.

### Prompt 3 — the moat, round-trip receipt (verify_result: true)

**14:46 EDT — response received.** [FROM-TRANSCRIPT — msg 10398]

**Prompt (verbatim, DEMO_PACKET.md §3 Prompt 3):**
> Call get_signed_snapshot for date_str=2026-08-01. Then take the
> entire envelope you just received and pass it directly into
> verify_snapshot_signature. Report verify_result and the public key
> fingerprint the verifier resolved. I want to see the round trip in
> one Claude turn.

**Tools fired (in this order):**
1. `get_signed_snapshot` — returned signed envelope for 2026-08-01.
   [FROM-INVENTORY — source: `signed_snapshot_walker`, batch:
   `signed-snapshot`, freshness: `daily`]
2. `verify_snapshot_signature` — accepted the full envelope from tool 1
   as input. [FROM-INVENTORY — source:
   `signed_snapshot.verify_envelope+pinned_pubkey`, freshness: `≤ 5min`]

**Envelope key fields (from tool 2's response):**
- `data.verify_result`: `true` [FROM-USER-REPORT]
- `data.public_key_fingerprint`: `7F:D4:F2:F4:D2:57:7C:BE`
  [FROM-USER-REPORT] — matches the fingerprint tool 1's envelope
  carried
- `data.issues`: `[]` (empty) [FROM-USER-REPORT]
- `chain_root`, `leaf_hash`, `audit_path`, `signature_ed25519`:
  `<not held — see PNG>`

**External-anchoring cross-checks (verified during demo):**
- `https://xrpldashboard.com/.well-known/snapshots/pubkey.pem` returned
  200 with the Ed25519 pubkey matching fingerprint `7F:D4:F2:F4:D2:57:7C:BE`.
  [RECONSTRUCTED — external channel #1]
- `dig _xrpld-snapshot-key.xrpldashboard.com TXT` returned
  `"v=ed25519; fp=7F:D4:F2:F4:D2:57:7C:BE; pub=a7efda2175ba3344bffa254e34854fdb7774c8beef663c3754e15d2fbf02c983"`.
  [RECONSTRUCTED — external channel #2]

Both external channels publish the same fingerprint the in-process
verifier resolved. The signed data can be independently checked by any
third party against either HTTPS or DNS without trusting anything on
this server.

**Precision on what this proves.** [FROM-TRANSCRIPT — msg 10398,
Charlie's own caveat]
> This only proves internal consistency between the tool and the
> published pubkey. Trust still rests on whoever pins
> `7F:D4:F2:F4:D2:57:7C:BE` as canonical.

The honest caption is *verified-against-the-published-key* (via two
independent external channels), **not** verified-against-some-external-
root-of-trust. Anyone can independently pin the fingerprint from either
external channel and get the same verification result — but the
statement is about that channel, not about universal trust.

**Screenshot need:** high. Both tool invocation panes visible in one
frame + `verify_result: true` is the flagship image.

### Prompt 4 — the moat, adversary case (verify_result: false)

**14:50 EDT — response received.** [FROM-TRANSCRIPT — msg 10400]

**Prompt (verbatim, DEMO_PACKET.md §3 Prompt 4):**
> Take the same envelope you just fetched from get_signed_snapshot.
> Before handing it to verify_snapshot_signature, modify one character
> in the signature_ed25519 hex string. Then call
> verify_snapshot_signature on the tampered envelope. What does
> verify_result say?

**Modification made:** [FROM-TRANSCRIPT — msg 10400] Last hex character
of `signature_ed25519` flipped: `...e272d809` → `...e272d808`. All
other fields (leaf hash, audit path, chain root, pubkey fingerprint)
left untouched.

**Tool fired:** `verify_snapshot_signature` (on the tampered envelope).
[FROM-INVENTORY]

**Envelope key fields:**
- `data.verify_result`: `false` [FROM-USER-REPORT]
- `data.issues`: `["Ed25519 signature did NOT verify against published pubkey"]`
  [FROM-USER-REPORT] — exact string per `DEMO_PACKET.md` §3 Prompt 4
  "Expected" line
- `data.public_key_fingerprint`: still `7F:D4:F2:F4:D2:57:7C:BE`
  [FROM-USER-REPORT] — verifier still knows *which* key it checked
  against; the failure is signature-specific, not key-resolution
- `data.chain_root`: unchanged from Prompt 3 [FROM-USER-REPORT] —
  Merkle path untouched, only the signature bytes were tampered

**Star of the prompt.** [FROM-TRANSCRIPT — msg 10400, Charlie's
assessment]
> Clean negative-control result: a single-bit-scale tamper in the
> signature is caught, verify_result flips to false, and the failure
> reason is specific rather than a generic error. Good pairing for the
> demo — true-case then false-case back to back shows the verifier
> isn't just always returning true.

The tamper is caught by the same code path that returned `true` on
Prompt 3. No server-side allowlist, no state, just crypto. This is
the receipt that our green isn't noise.

**Screenshot need:** highest of the four. This is the single most
Glow-relevant frame — it's the direct answer to a reviewer asking
"how do I know your green isn't stagecraft?"

---

## Deviations from the DEMO_PACKET

- **Prompt 2 required a retry** due to Incident 2 (DATABASE_URL). The
  retry succeeded and the envelope came back correct. Both the failure
  and the fix are logged above; both are ship-relevant evidence.
- **Prompt 1 surfaced an unplanned honesty-gap catch** by Sonnet 5 on
  `honest_partial: false + three-null-fields`. Landed as post-ship
  backlog, not a mid-demo fix. Positive receipt, not a deviation from
  the spec.
- **Otherwise, all four prompts fired the tools the DEMO_PACKET
  specified and returned the envelope shapes it specified.** No
  cross-check status regressions, no envelope invariant violations.

---

## Screenshot state (honest)

The DEMO_PACKET §4 capture spec calls for six numeric-prefixed PNGs
(`01_terminal_start.png` through `06_prompt4_tamper_verify_false.png`)
landing in this directory. **They aren't produced from this side** —
they're pixels on Charlie's Claude Desktop window, capturable only
from that screen.

Recovery path: the Claude Desktop conversation from 14:24 → 14:50 EDT
is still scrollable in Charlie's client. Scroll back, screenshot per
the packet's numbering, drop the PNGs into this directory, commit.

**If the PNGs never land, the Glow submission leans on this RUN_LOG
plus the repo's test suite (74/74 green, bar re-walk 6/6 PASS).** That
is weaker than frames but honest. Text can be doubted in a way a
screenshot cannot; the four external-facing receipts still stand
(pubkey.pem HTTPS, DNS TXT, published test suite, published code) but
the "here it is running in an off-the-shelf MCP client" leg of the
argument gets thinner.

**Most Glow-critical frames, in priority order** (if only one or two
can be captured):
1. **`06_prompt4_tamper_verify_false.png`** — the tamper adversary
   case. Direct answer to "how do I know the green isn't stagecraft."
2. **`05_prompt3_roundtrip_verify_true.png`** — the moat. Both tool
   invocation panes + `verify_result: true` in one frame.
3. **`04_prompt2_dispute_url.png`** — third-party-naming discipline.
   `dispute_contact_url` next to `issuer_name: "Ripple (…)"`.
4. **`01_terminal_start.png`** — `registered 15 tool(s)`. Ambient
   proof for the count-of-tools claim throughout the packet.

---

## Two known evidence points that would appear on-camera

Both belong in the eventual PNGs and both are load-bearing for Glow's
"transparency as core principle" bar:

1. **Sonnet 5's unprompted `honest_partial` gap catch on Prompt 1.**
   An independent model reading the envelope diagnosed a real
   contract-vs-value mismatch in the wild. Positive receipt for the
   envelope's machine-legibility.

2. **The DATABASE_URL error-then-fix cycle on Prompt 2.** The tool
   refused to return a response rather than silently returning stub
   or degraded data. That IS the honest-fail contract firing on
   camera; a system that pretended to answer would have been strictly
   worse.

Neither is a defect to hide — both are receipts.

---

## Commits referenced

- `8a5f53a` — Day 7 signed-snapshot MCP tools (#18 + #19). Includes
  the two tools that carry Prompts 3 & 4.
- `60f3f1c` — Day 7 ship-gate demo packet. Includes `DEMO_PACKET.md`.
- (this commit) — RUN_LOG.md + tail-fixes: `requirements.txt`
  mcp==1.29.0 downgrade, `launchd/run_mcp_server.sh` wrapper.
- `c9435c8` — Batch A plists (Lenovo repoint: oracle_walker,
  escrow_walker, nft_activity_walker).

---

*This file is committed as part of the Day 7 ship-gate evidence
package. It stands as the textual record until the six screenshots
land alongside it.*
