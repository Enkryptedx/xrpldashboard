# Round 2 prompt for Claude Fable 5.1 — draft for Charlie's approval

Not yet run. This is the prompt Charlie will paste into Claude Code on the Mac (with the repo open in the workspace) once he approves the shape.

---

## The prompt (single block, paste-ready)

You are auditing xrpldashboard.com from the inside — you have the full repo mounted in your workspace. Your job is code-trace, confirm-or-refute, propose-diff. You are not deploying, not committing, and not creating features that weren't asked for.

The site is a free educational XRPL data dashboard that also serves machine-readable surfaces for AI agents. Anchor #5 was stamped on-chain on 2026-09-04 (tx BB72E91012DA3B05C060FAD64DD405DC09A8BAB3EC1A079AFD82367D65A3B44B, chain_root 8e259732deab4050bab0c2af4a6e184949627670c3109fccbe049007d162abbb, 7 anchored data metrics + 3 v4 meta metrics). Round 1 of a multi-AI audit rotation just completed — Perplexity did the outside view (external-fact-with-citations lens). Its findings are in `triage/ROUND1_PERPLEXITY_NORMALIZED_2026-09-05.md`; the reconciled shortlist for you is in `triage/ROUND2_BRIEF_2026-09-05.md`. Read both before starting.

Your specialization is code depth: you have the highest SWE-bench Pro score of the frontier models, best-in-class long code refactoring, and careful spec/doctrine writing. Round 2 uses those strengths deliberately. You are Anthropic-family same as JJ (Opus 4-7 who wrote this brief) — this round trades family diversity for code depth on purpose; Rounds 3 and 4 will restore diversity via GPT-6 Astra and Gemini 3.1 Pro.

Your remit, in order:

FIRST — read the two triage files listed above. Do not restart your own audit; do not re-run what Perplexity did; do not re-audit findings already refuted (F-02, F-03, F-04, F-05, F-06, F-19, F-20 are all cross-checked in ROUND1_NORMALIZED — trust that reconciliation, do not re-litigate). Trust the reconciled shortlist in the brief file.

SECOND — for each finding in Groups (i) through (v) of the brief, do exactly this:
1. Trace the claim to concrete file:line evidence in the repo. Cite it.
2. Confirm or refute the finding against the code. If confirmed, describe what the code currently does and why the finding is real. If refuted, explain why with evidence.
3. Propose the source diff. Not a paragraph of "you should" — actual before/after for the specific lines. If the change spans many files, propose the shape (e.g. "one glossary.json referenced from these 12 places") plus a representative diff of the reference wiring.
4. Note preconditions or open questions Charlie needs to rule on (e.g. "F-25 robots.txt: keep the /wallet disallow — privacy-first — or open it? Design call.").

THIRD — for Group (i) TAXONOMY specifically, in addition to the per-finding trace, draft a single canonical `/glossary.json` file that resolves F-18 + F-29. Include: `trade`, `volume`, `supply`, `circulating supply`, `whale`, `verified`, `self-described`, `bare`, `labeled`, `named`, `identity claim`, `attested`, `sanctions hit`, `claim`. For "verified" propose the enum with N levels + evidence-per-level. Draft the file as a real JSON blob, not a description of one.

FOURTH — for Group (ii) PROVENANCE, in addition to per-finding traces, propose one shared `<citation-anchor>` Jinja macro or component that all data pages can use to render the per-figure provenance trail (raw RPC request + response + calculation + ledger + observed_at). Show the macro + one page's integration as a concrete example.

Constraints that bound the work:

You are NOT deploying, committing, or opening PRs. Every output is text — file paths, diffs, and reasoning. JJ will verify against the live repo and reconcile before any Fix window.

You are NOT inventing new features. Every proposal must trace back to a specific Round 1 finding in the brief. If you spot something outside the brief that you think matters, list it in a "found beyond brief" section with the same trace/confirm/propose discipline — but flag it clearly as your own addition.

You are NOT re-running the outside view. Perplexity did that. If your trace reveals a finding is more or less severe than Perplexity thought, note the update and cite the evidence.

You have no direct network access to the running site — assume `xrpldashboard.com` is unreachable from your sandbox. All your evidence must come from the repo files. If a finding requires live prod evidence to confirm, mark it "needs-live-verification" and JJ will run the fetch.

You are running as Claude Code with tool access (Read, Grep, Bash for the repo). Use those aggressively. The repo is at `/Users/charliebruce/xrpl_test`. Key entry points: `app.py` (Flask routes), `templates/*.html`, `rlusd_live.py`, `tokens.py`, `credentials_state.py`, `xrpl_client.py`, `sovereign_tunnel_client.py`, `signed_snapshot.py`, `db.py` (Postgres schema), `docs/ONLEDGER_ANCHOR_SPEC.md`, `docs/methodology.html` (rendered on-site).

Output format:

Return a single markdown document titled `Round 2 — Fable 5.1 findings + proposals`. Structure it exactly as the brief does — same groups (i) through (v), plus Group (vii) POSITIVE which you should list at the top ("protect these"). Each finding: `## F-nn` heading, `Claim`, `Trace`, `Verdict`, `Proposed diff` (or "Charlie decision needed"), `Open questions`. Then TWO extra sections: the canonical glossary.json (Group (i) work), and the citation-anchor macro (Group (ii) work). Then a closing "Round 3 read" paragraph — what should the next model's job be, and which model fits it (Group (vi) UX findings are what's left; Astra vs Gemini is the choice). Save your output to `triage/ROUND2_FABLE_RAW_2026-09-05.md`.

Do not stop early. Findings you can't fully resolve get "needs-live-verification" or "Charlie decision needed"; those are still outputs.

---

## Charlie's approval checklist

Before running:
- [ ] Prompt shape ok? (specialization-in-sequence, no-restart, no-deploy, tool-access-encouraged)
- [ ] Groups (i)-(v) is the right scope for this round? (UX group (vi) held for R3/R4 — right call?)
- [ ] Family caveat noted?
- [ ] Output file path ok? (`triage/ROUND2_FABLE_RAW_2026-09-05.md`)

If yes to all: send "Run Round 2" and I'll give you the keyboard steps.

---

## Keyboard steps for Charlie (once approved)

Round 2 runs as Claude Code (Fable 5.1) on the Mac with the repo mounted. Same shape as your earlier Claude Code sessions.

**Send back only:** `Round 2 saved` (once Fable's output is in `triage/ROUND2_FABLE_RAW_2026-09-05.md`), or `blocked: <what>` if Claude Code refuses / errors.

Steps:
1. Open Claude Code in your terminal, inside `~/xrpl_test/`:
   ```
   cd ~/xrpl_test
   claude
   ```
2. Once the Claude prompt is up, select **Fable 5.1** as the model (usually with `/model` command or a menu).
3. Paste the prompt above (the block between the `---` lines starting "You are auditing…" and ending "…are still outputs.").
4. Let it work. Depending on how thorough it is, this may run 5-15 minutes with heavy tool use. Do not interrupt unless it appears stuck (silence for >5 min).
5. When Fable finishes, ask it: "Save your full output to `triage/ROUND2_FABLE_RAW_2026-09-05.md` verbatim (as a Write tool call)." It should confirm the file was written.
6. Send back `Round 2 saved`.

After that, JJ picks up the file and does the reconciliation pass.
