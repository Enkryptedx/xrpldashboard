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

**Raw envelope (verbatim from msg 10407):** [FROM-TRANSCRIPT]

```json
{
 "data": {
  "validated_ledger_index": 106026242,
  "close_time_iso": "2026-08-02T18:36:30Z",
  "server_state": null,
  "load_factor": 1,
  "complete_ledgers": "32570-106026242",
  "build_version": null,
  "hostid": null
 },
 "proof": {
  "source": "local_rippled",
  "as_of": "2026-08-02T18:36:30Z",
  "freshness_contract": "≤ 5min",
  "claims_ref": "ledger_stats_live",
  "methodology_url": "https://xrpldashboard.com/methodology#ledger",
  "cross_check_status": "not_applicable",
  "honest_partial": false,
  "scope_note": null
 },
 "server": {
  "name": "xrpldashboard-mcp",
  "version": "1.0.0",
  "public_key_fingerprint": "7F:D4:F2:F4:D2:57:7C:BE",
  "docs": "https://xrpldashboard.com/methodology#for-ai-agents"
 }
}
```

**Root cause of the three nulls — investigation pending, NOT a
public-s1 fallback.** The envelope's own `proof.source: "local_rippled"`
plus healthy `load_factor: 1` and `complete_ledgers: 32570-106026242`
prove the tool did reach local rippled and got a live response. The
nulls in `server_state`, `build_version`, and `hostid` are therefore
something else — one of:

1. **Local rippled isn't populating those fields** in its `server_info`
   response (config, version, or admin-scope issue on THIS node — check
   whether the Mac's rippled has `admin` permission enabled in its
   config and whether the tool is calling as admin).
2. **The tool code isn't reading them correctly** from a response that
   does contain them.

Charlie's live framing in msg 10407: *"a silent-write-style gap on the
rippled response, not just a display choice — if those fields should be
populated on a healthy node, this is worth a health check on that source."*

An **earlier draft of this log incorrectly attributed the nulls to
public s1.ripple.com admin-scoping via an Incident-2 fallback.** That
theory was based on a plausible inference before the raw envelope
existed; msg 10407's `proof.source: "local_rippled"` refutes it. The
mistake is left named here rather than silently overwritten, per the
same discipline the four-layer audit exists to enforce.

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
was supposed to prove the envelope is machine-legible. Note the
envelope's `scope_note: null` alongside three data-field nulls — that's
a *second* instance of the same gap in one call. The contract-honoring
shape would have had `honest_partial: true` and `scope_note` naming
the three null fields.

**Post-ship backlog (2 items):**
1. **Contract fix:** when any surfaced field is `null`, the tool should
   flip `honest_partial: true` and populate `scope_note` naming which
   fields + why they're null.
2. **Health check on local rippled:** the `server_state`,
   `build_version`, `hostid` nulls on a demonstrably-reached local
   rippled (per `proof.source` + healthy `load_factor` + valid
   `complete_ledgers`) is either a rippled-config issue (admin scope
   not granted to the caller) or a tool-code parse issue. Investigate,
   fix at the source, so the envelope has real values to flag.

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

**First-attempt error (verbatim):** [FROM-TRANSCRIPT — msg 10406]
> Error executing tool get_token_attestation:
> get_token_attestation: DATABASE_URL not configured

**In-band epistemic-precision moment (bonus receipt):** while the tool
was in the errored state, Charlie flagged that the tool's own
description text claims it's "the first tool to name a third party"
for the dispute-URL field — but noted this is "just descriptive
metadata attached to the tool definition — not something either of us
has verified against actual output, and now we have no output to check
it against anyway. I'd treat that line as unverified until a real
payload comes back." [FROM-TRANSCRIPT — msg 10406]

That is itself a demo-relevant receipt: the operator refused to treat
a self-descriptive claim as verified until a real payload confirmed it.
The four-layer audit posture applied to the demo of the four-layer
audit posture. Meta but real.

**Retry envelope key fields (14:44 EDT, after wrapper fix):**
- `proof.source`: `"neon_postgres"` [FROM-INVENTORY]
- `proof.freshness_contract`: `"daily"` [FROM-INVENTORY]
- `proof.claims_ref`: `"token_attestation_status"` [FROM-INVENTORY]
- `proof.honest_partial`: `false` [FROM-TRANSCRIPT — correct this time;
  no nulls in core fields, so the flag reads accurately]
- `data.tier`: `"verified"` [FROM-TRANSCRIPT]
- `data.dispute_contact_url`:
  `"https://xrpldashboard.com/contact?purpose=attestation-dispute"`
  [FROM-TRANSCRIPT]
- `data.issuer_name`: `"Ripple (Ripple USD issuer)"` [FROM-TRANSCRIPT]
- `data.attestation_tier_reason`: `null` [FROM-TRANSCRIPT]
- Sourcing note per response body: `issuer_name` sourced via `.toml`
  from `ripple.com`. [FROM-TRANSCRIPT — msg 10406]

**Star of the prompt — third-party-naming discipline visible in one call.**
The envelope both names a third party (Ripple) and carries the channel
by which that third party can dispute the label. This is the piece
competitors don't ship. [FROM-INVENTORY — codified in memory
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
- `data.verify_result`: `true` [FROM-TRANSCRIPT — msg 10406]
- `data.public_key_fingerprint`: `7F:D4:F2:F4:D2:57:7C:BE`
  [FROM-TRANSCRIPT — msg 10406] — matches the fingerprint tool 1's
  envelope carried
- `data.issues`: empty (verbatim from response: "empty — no path or
  signature mismatches") [FROM-TRANSCRIPT — msg 10406]
- `chain_root`: `e61469ddd64...79a43f` (prefix + suffix; middle
  truncated in the response body). [FROM-TRANSCRIPT — msg 10406]
- `leaf_hash`, `audit_path`, `signature_ed25519`:
  `<not held — see PNG>` (full hex sequences)

**Same-key confirmation (bonus receipt).** [FROM-TRANSCRIPT — msg 10406]
The chain_root matched what was in the original snapshot AND the
resolved fingerprint matched the one both `get_signed_snapshot`
returned and the server block itself reported — Charlie's own framing:
> so it's the same pinned key, not a different one being silently
> substituted.

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

**Precision on what this proves.** [FROM-TRANSCRIPT — Charlie's own
caveat, msg 10406, verbatim]
> One thing worth naming for the demo narrative: this only proves
> internal consistency — that the leaf hash, audit path, and signature
> all check out against the pubkey the tool itself ships with. It
> doesn't independently prove that pubkey is the correct one an outside
> party should trust; that trust still rests on whoever pins
> 7F:D4:F2:F4:D2:57:7C:BE as canonical (methodology page, DNS TXT
> record, wherever you've anchored it). If a screenshot claims
> "cryptographically verified," it's accurate to say
> verified-against-the-published-key, not
> verified-against-some-external-root-of-trust — worth being precise
> about which claim you're making if this goes in front of anyone
> who'd ask.

The honest caption is *verified-against-the-published-key* (via two
independent external channels — HTTPS and DNS), **not**
verified-against-some-external-root-of-trust. Anyone can independently
pin the fingerprint from either external channel and get the same
verification result — but the statement is about that channel, not
about universal trust.

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
- `data.verify_result`: `false` [FROM-TRANSCRIPT — msg 10406]
- `data.issues`: `["Ed25519 signature did NOT verify against published pubkey"]`
  [FROM-TRANSCRIPT — msg 10406] — exact string, matches
  `DEMO_PACKET.md` §3 Prompt 4 "Expected" line
- `data.public_key_fingerprint`: still `7F:D4:F2:F4:D2:57:7C:BE`
  [FROM-TRANSCRIPT — msg 10406] — verifier still knows *which* key it
  checked against; the failure is signature-specific, not
  key-resolution. Charlie's own framing:
  > the verifier still knows *which* key it checked against, it just
  > correctly says the sig doesn't match under that key
- `data.chain_root`: unchanged from Prompt 3 [FROM-TRANSCRIPT — msg
  10406] — Merkle path untouched, only the signature bytes were tampered

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

## Screenshot state (honest, as of 2026-08-02 16:09 EDT, msg-10417 reconciliation sweep)

**Landed (seven frames — five in the DEMO_PACKET spec-numbering, plus
two ambient session-provenance frames):**

- `00_incident1_attach_failure.jpg` — Incident 1 evidence: the red
  "Could not attach to MCP server xrpldashboard-mcp" error against the
  demo-scaffold message. Filed 2026-08-02 16:09 EDT from msg 10382
  (14:27 EDT). Bonus frame — not in the DEMO_PACKET §4 numbering but
  directly matches the RUN_LOG's "Startup incidents" section on
  Incident 1 (paperclip-attach → Settings→Developer route).
  [FROM-THREAD — msg-10417 reconciliation sweep, file_303]
- `00b_model_picker_ambient.jpg` — Claude Desktop model picker showing
  Fable 5 / Opus 5 / Sonnet 5 (chosen) / Haiku 4.5 with the P1 prompt
  visible in background. Filed 2026-08-02 16:09 EDT from msg 10388
  (14:34 EDT). Session-provenance ambient evidence for the "Sonnet 5
  chosen" line above. [FROM-THREAD — msg-10417 reconciliation sweep,
  file_305]
- `02_claude_desktop_tool_list.jpg` — Settings → Developer showing
  `xrpldashboard-mcp` with status `running`, command
  `/Users/charliebruce/xrpl_test/venv/bin/python`, args
  `/Users/charliebruce/xrpl_test/mcp_server.py`, "View Logs" affordance.
  Matches DEMO_PACKET §4 capture-spec item #2 (was named
  `02_claude_desktop_tool_list.png` in the spec; JPG variant filed).
  Filed 2026-08-02 16:09 EDT from msg 10386 (14:32 EDT). Ambient proof
  that the MCP server is registered and running in Claude Desktop as
  the demo begins. [FROM-THREAD — msg-10417 reconciliation sweep,
  file_304]
- `03a_prompt1_permission_dialog.jpg` — the P1 prompt visible on-screen
  (`"Use the xrpldashboard-mcp server. Call get_led[ger_stats]... Show
  me the raw envelope in a code block — I want to see th[e] and server
  fields, not just the ledger index."`) + Claude Desktop's permission
  dialog "Claude wants to use Get ledger stats from xrpldashboard-mcp"
  with Always-allow / Deny affordances. Filed 2026-08-02 16:09 EDT
  from msg 10390 (14:36 EDT). Proves P1 was invoked against
  `xrpldashboard-mcp` inside Claude Desktop as the client (the raw
  envelope itself is preserved verbatim in the §Prompt 1 section above
  from msg 10407). [FROM-THREAD — msg-10417 reconciliation sweep,
  file_306]
- `05_prompt3_roundtrip_verify_true.jpg` — P3 round-trip verify=true.
  Filed 2026-08-02 15:20 EDT. Carries tool-return fields
  (`verify_result: true`, `public_key_fingerprint:
  7F:D4:F2:F4:D2:57:7C:BE`, empty issues) AND Charlie's written
  epistemic-precision caveat about internal-vs-external verification —
  the caveat itself is a second-order receipt of the transparency
  posture.
- `06a_prompt4_tamper_verify_false_top.jpg` — P4 tamper, upper half.
  Filed 2026-08-02 15:54 EDT. Prompt fully visible + Claude Desktop's
  own "Devised signature tampering strategy to test verification"
  reasoning header + Charlie's edit line (`...e272d809 → ...e272d808`)
  + `verify_result: false` + the exact issue string
  `"Ed25519 signature did NOT verify against published pubkey"` +
  fingerprint `7F:D4:F2:F4:D2:57:7C:BE` still resolving.
- `06b_prompt4_tamper_verify_false_bottom.jpg` — P4 tamper, lower
  half. Filed 2026-08-02 15:54 EDT. Full closing analysis paragraph:
  *"clean negative-control result... single-bit-scale tamper... good
  pairing for the demo — true-case then false-case back to back shows
  the verifier isn't just always returning true."*

The two flagship Glow-critical frames (P4 tamper + P3 verify) plus
in-client provenance (tool list + P1 permission dialog) are all
landed. The "how do I know your green isn't stagecraft" question now
has a screenshot answer AND the "was it really running in Claude
Desktop" question has one too.

**Pending (one nice-to-have PNG, not ship-critical):**
- `04_prompt2_dispute_url.jpg` (DEMO_PACKET spec #4) — the P2 response
  frame showing `dispute_contact_url` next to `issuer_name`. The P2
  text is preserved verbatim in §Prompt 2 above from msg 10406, so the
  facts are held; only the visual frame is missing.
- `01_terminal_start.jpg` (DEMO_PACKET spec #1) — the terminal
  `registered 15 tool(s)` log line. Ambient claim covered textually
  in §Environment; not required for the transparency argument.

**Meta note on the reconciliation:** Before msg 10417 this document
listed three "pending" PNGs. Charlie's flag was correct — four of
those images had already been sent (msg 10382 attach-failure, msg
10386 tool list, msg 10388 model picker, msg 10390 permission dialog)
but weren't filed against the outstanding-items list. Sweep on
2026-08-02 16:09 EDT closed the loop; feedback memory
`feedback_delivered_but_untracked_receiving_side_close_on_arrival.md`
codifies the discipline going forward.

**Transcript upgrade:** Charlie also relayed the verbatim text of the
demo responses via msg 10406 (2026-08-02 15:32 EDT). Provenance marks
throughout this document have been upgraded from `[FROM-USER-REPORT]`
to `[FROM-TRANSCRIPT]` where the exact string is now held. Notable
newly-verbatim material: the `chain_root: e61469ddd64...79a43f`
prefix; the exact P2 error string; Charlie's fuller
internal-vs-external caveat. **Correction (post-msg-10407):** a
prior draft of this section attributed P1's three nulls to public
s1 admin-scoping via an Incident-2 fallback path. Msg 10407's raw
envelope refutes that inference — `proof.source: "local_rippled"`
plus a healthy `load_factor: 1` and `complete_ledgers:
32570-106026242` show the local rippled was reached. The wrong
inference is preserved visibly in §Prompt 1 root-cause block for
epistemic-discipline reasons; investigation of the true cause is
listed in §Post-ship backlog.

Recovery path for remaining frames: the Claude Desktop conversation
from 14:24 → 14:50 EDT is still scrollable in Charlie's client. Scroll
back, screenshot per the packet's numbering, drop into this directory,
commit.

**If the PNGs never land, the Glow submission leans on this RUN_LOG
plus the repo's test suite (74/74 green, bar re-walk 6/6 PASS).** That
is weaker than frames but honest. Text can be doubted in a way a
screenshot cannot; the four external-facing receipts still stand
(pubkey.pem HTTPS, DNS TXT, published test suite, published code) but
the "here it is running in an off-the-shelf MCP client" leg of the
argument gets thinner.

**Most Glow-critical frames, in priority order** (top two already
landed as of 2026-08-02 15:54 EDT):
1. ✅ **`06a` + `06b` `_prompt4_tamper_verify_false_{top,bottom}.jpg`** —
   the tamper adversary case, split across two frames covering full
   prompt-to-analysis. Direct answer to "how do I know the green isn't
   stagecraft."
2. ✅ **`05_prompt3_roundtrip_verify_true.jpg`** — the moat,
   verify=true + external-anchoring caveat visible.
3. ⏳ **`04_prompt2_dispute_url.jpg`** — third-party-naming discipline.
   `dispute_contact_url` next to `issuer_name: "Ripple (…)"`.
4. ⏳ **`01_terminal_start.jpg`** — `registered 15 tool(s)`. Ambient
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
