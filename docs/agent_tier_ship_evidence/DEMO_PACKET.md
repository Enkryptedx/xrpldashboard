# Agent-tier ship-gate demo packet

Day 7, 2026-08-02. The three prompts and one round-trip that turn the
Claude Desktop MCP integration into evidence of the moat.

Bar re-walk immediately preceding this packet: **6/6 PASS** (envelope
discipline, CLAIMS refs, methodology anchors, walker_scope_declarations,
rate-limit probe, signed-snapshot round-trip). Full test suite: 74/74
green in 16.85s.

Terminal, Claude Desktop config, prompts, capture spec below.

---

## 1. Start the MCP server (foreground, stdio transport — Claude Desktop's default)

Charlie's terminal, from `~/xrpl_test`:

```sh
cd ~/xrpl_test
source venv/bin/activate
MCP_TRANSPORT=stdio python mcp_server.py
```

Claude Desktop launches the child process itself using the config below;
this direct-terminal command is the fallback / smoke-test path (run it
once to confirm no import errors; Ctrl-C; then let Claude Desktop own the
process). If Charlie wants to keep it running foreground for the demo
(so log lines are visible on the second monitor), that also works —
Claude Desktop reconnects on restart.

The **streamable-http** transport is used by curl/Postman testing only;
Claude Desktop speaks stdio. Do NOT use `MCP_TRANSPORT=streamable-http`
for this demo.

Expected first-line log:
```
YYYY-MM-DD HH:MM:SS INFO mcp_server: starting mcp_server_heartbeat (cadence=60s)
YYYY-MM-DD HH:MM:SS INFO mcp_server: registered 15 tool(s) on xrpldashboard-mcp
YYYY-MM-DD HH:MM:SS INFO mcp_server: mcp starting: transport=stdio ...
```

If "registered 15 tool(s)" doesn't appear, do NOT proceed to Claude
Desktop — the tool count is the load-bearing invariant every other
surface derives from.

---

## 2. Claude Desktop config

File path (macOS): `~/Library/Application Support/Claude/claude_desktop_config.json`

If the file already exists, merge the `xrpldashboard-mcp` entry into the
existing `mcpServers` object. Do NOT overwrite it — Charlie may have
other MCP servers configured.

```json
{
  "mcpServers": {
    "xrpldashboard-mcp": {
      "command": "/Users/charliebruce/xrpl_test/venv/bin/python",
      "args": ["/Users/charliebruce/xrpl_test/mcp_server.py"],
      "env": {
        "MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

Notes:
- **Command must be the venv Python**, not `/usr/bin/python3` — the venv
  is where `mcp`, `flask`, `cryptography`, and the other deps live.
- **`args` must be absolute paths.** Claude Desktop's launcher does not
  cd into the module's directory before spawning; the server's
  `HERE = os.path.dirname(os.path.abspath(__file__))` handles snapshot
  loading, but a relative `mcp_server.py` won't be found.
- **After editing this file, fully quit and relaunch Claude Desktop**
  (menu bar → Claude → Quit, then reopen). Config is read at process
  start; hot-reload is not supported as of Claude Desktop 0.7.x.
- Once relaunched, the "Attach" menu (paperclip icon) should list
  `xrpldashboard-mcp` with 15 tools. If it lists 0 tools or fails to
  connect, tail Claude Desktop's log:
  `~/Library/Logs/Claude/mcp-server-xrpldashboard-mcp.log`.

---

## 3. The three prompts (verbatim)

Typed into Claude Desktop chat, with `xrpldashboard-mcp` attached.
Purpose ordering: (1) confirms the envelope is machine-readable and
carries proof metadata; (2) confirms third-party-naming discipline
surfaces `dispute_contact_url`; (3) confirms the moat exists.

### Prompt 1 — envelope surfacing

```
Use the xrpldashboard-mcp server. Call get_ledger_stats. Show me the
raw envelope in a code block — I want to see the `proof` and `server`
fields, not just the ledger index.
```

**Expected shape:** `{data: {ledger_index, close_time, ...}, proof:
{source: "local_rippled", as_of, freshness_contract: "≤ 5min",
methodology_url: "…#ledger", claims_ref: "ledger_stats_live",
cross_check_status, honest_partial: false}, server: {name: "…", version,
public_key_fingerprint, docs}}`.

**Screenshot target:** the full envelope in the code block, `proof.source`
and `proof.claims_ref` visible.

### Prompt 2 — third-party-naming + dispute channel

```
Call get_token_attestation for currency=RLUSD issuer=rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De.
I want to know two things: the attestation tier, and — inside the data
payload — the `dispute_contact_url` the tool includes for issuers who
disagree with our label.
```

**Expected shape:** `data.tier ∈ {verified, self-described, null}`,
`data.dispute_contact_url = "https://xrpldashboard.com/contact?purpose=attestation-dispute"`.

**Screenshot target:** the tier value + the dispute URL, both visible in
the data payload. This is the point competitors don't have.

### Prompt 3 — the moat, round-trip receipt

```
Call get_signed_snapshot for date_str=2026-08-01. Then take the entire
envelope you just received and pass it directly into
verify_snapshot_signature. Report verify_result and the public key
fingerprint the verifier resolved. I want to see the round trip in one
Claude turn.
```

**Expected sequence:** two tool calls back-to-back. First returns the
signed envelope for 2026-08-01 (leaf_hash, chain_root, audit_path,
signature_ed25519, signing_pubkey_fingerprint). Second returns
`data.verify_result: true`, `data.issues: []`, `data.public_key_fingerprint`
matching the fingerprint inside the first envelope.

**Screenshot target:** both tool invocation panes visible in one screen,
and `verify_result: true` circled. This IS the flagship — the writer
and the verifier share no state, and the number cannot have been
silently changed between fetch and verify.

### (Optional) Prompt 4 — the moat, adversary case

```
Take the same envelope you just fetched from get_signed_snapshot. Before
handing it to verify_snapshot_signature, modify one character in the
signature_ed25519 hex string. Then call verify_snapshot_signature on the
tampered envelope. What does verify_result say?
```

**Expected:** `verify_result: false`, `issues: ["Ed25519 signature did
NOT verify against published pubkey"]`. The tamper is caught by the
same code path — no server-side allowlist, no state, just crypto.

**Screenshot target:** the failure envelope with the specific issue
named. This is the receipt that our green ISN'T noise: when the moat
breaks, the tool says so.

**Recommendation on the 4th call:** ship it. Round-trip + adversary is
the complete story — one shot per property (integrity of the happy path;
integrity of the tamper path). Adds ~30s to the demo. Without it, a
skeptic asks "how do I know it isn't just always returning true?"

---

## 4. Capture spec

Land under `docs/agent_tier_ship_evidence/` in the same commit that
declares the demo run.

- `01_terminal_start.png` — the "registered 15 tool(s)" log line visible
  in the terminal.
- `02_claude_desktop_tool_list.png` — Claude Desktop paperclip menu
  showing `xrpldashboard-mcp` with 15 tools.
- `03_prompt1_envelope.png` — Prompt 1 response, envelope in code block,
  `proof.source` + `proof.claims_ref` visible.
- `04_prompt2_dispute_url.png` — Prompt 2 response, tier + dispute URL
  both visible in the data payload.
- `05_prompt3_roundtrip_verify_true.png` — Prompt 3 response, BOTH tool
  invocation panes visible, `verify_result: true` visible.
- `06_prompt4_tamper_verify_false.png` — Prompt 4 response, tampered
  envelope's `verify_result: false` + issues array naming the failure.

Filenames deliberately numeric-prefixed so the directory listing IS the
demo transcript. Charlie captures at native retina resolution; PNG, not
JPEG.

---

## 5. What breaks the demo (know before running)

Failure modes and their tells, so if any of these happen mid-demo the
recovery is obvious:

- **Claude Desktop shows 0 tools:** `env: MCP_TRANSPORT=stdio` missing
  from config, or Python-path typo. Log at
  `~/Library/Logs/Claude/mcp-server-xrpldashboard-mcp.log` names it.
- **`get_ledger_stats` raises "connection refused":** local rippled
  isn't running / `XRPL_NODE` env var not exported into Claude Desktop's
  child process. Add `"XRPL_NODE": "https://s1.ripple.com:51234"` to
  the `env` block as a fallback for the demo — the tool doesn't care
  whether local or public, the envelope surfaces which via `data.source`.
- **`get_signed_snapshot("2026-08-01")` raises "no signed snapshot on
  disk":** signed_snapshot walker hasn't produced 2026-08-01.json —
  substitute the most recent date in `signed_snapshots/`.
- **`verify_snapshot_signature` raises "missing required fields":**
  Claude Desktop passed the envelope as a string, not a dict. In the
  chat re-prompt: "pass the envelope as a JSON object, not a string."
- **`verify_result: false` on the happy path (Prompt 3):** something is
  actually broken — DO NOT show this on camera. Land privately, root-cause,
  re-shoot. This is the one case where "the demo doesn't work" is real
  evidence, not stagecraft.

---

## 6. What ships with the packet

Post-demo commit:
- The six screenshots under this directory.
- A one-page `RUN_LOG.md` noting the timestamp, Claude Desktop version,
  MCP tools returned, and any deviations from the prompts above.
- LAST_VERIFIED_AGENT_TIER_METHODOLOGY bump ONLY if the demo surfaced
  a shape change; the surfaces are already stamped 2026-08-02 from
  Day 6, and the Day 7 commit didn't change them.

That's the ship gate. The next human act is Charlie typing Prompt 1
into Claude Desktop and hitting return.
