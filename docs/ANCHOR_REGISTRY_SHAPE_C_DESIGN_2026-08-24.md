# Anchor Registry Shape C — Ledger-Derive Design Pack

**Filed:** 2026-08-24  
**Owed since:** 2026-08-22 (Saturday ruling, Day 3)  
**Author:** Claude — design pack, no build. Charlie rules, then build follows.  
**Status:** Awaiting Charlie's rulings (§9)

---

## 0. Context and problem statement

The anchor canary has two jobs:
1. Prove every published anchor tx is on-chain, unchanged, forever.
2. Prove the freshest anchor is recent (weekly cadence not broken).

The incident that exposed the structural flaw (2026-08-22): after anchor #3 was stamped on-chain, ceremony Step 4 (`tools/anchor_registry_append.py`) was skipped. The canary correctly fired — `docs/anchor_registry.json` (the canary's SoT) had seq #2 as its latest anchor, even though #3 was live on-chain. The alert was accurate but the root cause was human ceremony drift: a separately-maintained local file got stale.

The filed rule (`feedback_writer_reader_shared_source_of_truth.md`): **untracked-local-file-as-monitor-SoT is a red flag.** When the writer (ceremony step) and reader (canary) have separate execution contexts and no automatic sync, drift is a matter of when, not whether.

Shape C eliminates the drift class entirely: the canary derives anchor state directly from the XRPL ledger. No local registry file. No separate append step. No file that can be stale.

---

## 1. Current architecture (read, not assumed)

```
Ceremony:
  stamp anchor tx on-ledger (Xaman / ops CLI)
  → run anchor_registry_append.py --tx-hash <hash>
     → validates tx
     → appends row to docs/anchor_registry.json
     → runs canary dry-run to prove new row verifies clean

Canary (tools/anchor_canary.py, 809 LOC):
  load docs/anchor_registry.json            ← LOCAL FILE SoT
  for each row:
    look up tx by hash (local rippled → public witness cascade)
    compare on-chain memo to registry row
  freshness check: latest row's close_time_iso < 8 days old
  root cross-check: latest row's chain_root_hex vs live chain.json
  state machine: reconcile alerts, send Telegram, save state
```

**The structural flaw:** two separate executables (ceremony append + canary) share state via a local file. The file is currently untracked in git on both hosts. Either host's file can drift independently.

**What the current code does well (keep):**
- Tx-by-hash lookup cascade (local → public witnesses) — solid
- Memo parse / strip rule / namespace handling — correct
- Root cross-check against live chain.json — the stolen-key tripwire, keep
- State machine (alert reconcile / reminder / heartbeat) — keep as-is
- Weekly heartbeat on Tuesday 09:00-10:00 ET — keep

**Why the 2026-08-16 registry-driven design was the right intermediate fix:** the PRIOR design used `account_tx` directly on the local rippled. That node has `online_delete=10000` (~13.5h retention). Weekly anchor cadence → anchor older than retention window 6/7 days → canary fired false `latest_anchor_unavailable` almost every daily run. The registry pinned which hashes to look up; `tx` by hash cascades correctly regardless of retention. That fix was correct for its time.

Shape C re-opens the `account_tx` approach but routes it through a **full-history node** (Clio), not the local rippled. The retention window problem is solved at the query endpoint, not by pinning hashes locally.

---

## 2. Shape C architecture

```
Ceremony (simplified):
  stamp anchor tx on-ledger (Xaman / ops CLI)
  verify on-chain (any tx lookup — can reuse existing cascade)
  DONE. No local append step. No registry file update.

Canary (rewritten):
  fetch_account_tx_anchors(account, full_history_node):
    account_tx on full-history node (Clio) → paginate all txs
    filter: has v1 anchor memo (namespace xrpldashboard/anchor/v1)
    parse memo → extract snapshot_date, chain_root_hex, close_time_iso
    sort by ledger_index ascending → ordered list of on-chain anchors
    return list (may be empty → hard skip)

  freshness check: list[-1].close_time_iso < 8 days old
  root cross-check: list[-1].chain_root_hex vs live chain.json
  state machine: unchanged (reconcile alerts, Telegram, state)
```

The registry is gone. The ledger IS the registry.

---

## 3. The freshness gate — retention window analysis

**The problem the 2026-08-16 redesign was solving:** `account_tx` on the local rippled (online_delete=10000) only sees ~13.5h of history. Weekly anchors fall outside this window 6/7 days.

**Shape C's solution:** query a **full-history node** instead of the local rippled.

**Clio (s2-clio.ripple.com):** Clio nodes are specifically designed to maintain full XRPL ledger history and support `account_tx` queries across the entire chain. s2-clio.ripple.com is the Ripple-operated Clio instance we already reference in the codebase. `account_tx` on Clio will reach back to the anchor account's first tx, regardless of age.

**What if the full-history node is temporarily unavailable?**

This is the freshness gate boundary condition. Three scenarios:

| Scenario | Response | Rationale |
|---|---|---|
| Full-history node responds, returns anchors | Normal operation — check freshness and root | Standard path |
| Full-history node responds, account has no v1 memos | FIRE `no_anchors_found` alert (CRITICAL) | Full-history node has the account; if no anchors found, none exist |
| Full-history node unreachable (timeout / 5xx) | LOUD SKIP — log, no alert fired | Witness problem, not a provenance problem |
| Full-history node responds but returns partial history (detectable via ledger_index gap) | LOUD SKIP with explanation | Unreliable witness; try again next cycle |

**Key principle:** an unreachable or partial witness is a canary infrastructure problem, never grounds for a false-stale alert. The canary must refuse to declare "anchor stale" when it cannot reach a reliable witness. False positives are more damaging to trust than missed cycles.

**Partial history detection:** `account_tx` returns `marker` when pagination is needed. It also returns `ledger_index_min` in the response. If `ledger_index_min` > 0 (the chain's genesis), the node may have limited history. In practice, Clio returns `ledger_index_min = 32570` (genesis), confirming full history. A response where `ledger_index_min` is near-current ledger = limited history = loud skip.

**Grace cadence:** the canary runs daily (systemd timer). One missed cycle due to Clio unavailability means the next cycle catches up. Freshness threshold is 8 days (weekly cadence + 1-day slack). A 24h Clio outage doesn't fire a false stale alert.

---

## 4. Failure semantics — the witness ladder

Shape C uses a single full-history query for discovery (Clio) rather than per-hash lookups. The cascade works differently:

```
1. Try full-history primary (s2-clio.ripple.com) with account_tx
   → success → proceed to freshness + root checks
   → unreachable → try full-history fallback (if configured)

2. If all full-history nodes unreachable:
   LOUD SKIP: log "anchor_canary: all full-history witnesses unreachable — skipping this cycle"
   No alert fired. No state mutation. Return 0.

3. If full-history node responds but returns 0 anchors for the account:
   FIRE: anchor_canary:no_anchors_on_chain (CRITICAL)
   This means: either the anchor account has never sent a v1 tx,
   or the account address is wrong, or history was purged (impossible on Clio).
   
4. If freshness check fails (latest anchor > 8 days old):
   FIRE: anchor_canary:anchor_stale (same as current)

5. If root cross-check fails:
   FIRE: anchor_canary:root_mismatch (the stolen-key tripwire — same as current)
```

The per-anchor tx verification (current design verifies each hash is on-chain and memo matches) changes character in Shape C: since we're READING from the chain rather than verifying against a local file, the "memo matches" check becomes "memo is internally consistent" (namespace, field count, date format). The chain IS the source of truth — we can't mismatch against ourselves.

What we CAN still check:
- All anchor txs are from `DEFAULT_ANCHOR_ACCOUNT` (account_tx scoped to that account handles this automatically)
- Memos are parseable v1 format
- Dates are monotonically increasing (seq A date < seq B date — basic integrity)
- Latest anchor's chain_root matches live chain.json (the forgery tripwire — unchanged)

---

## 5. Kill-plan for `docs/anchor_registry.json`

### On Mac (~/xrpl_test/)

| File | Action | When |
|---|---|---|
| `docs/anchor_registry.json` | Git-track once (commit the 3-anchor state) → then delete from live | Before deleting: commit as historical record so the 3 tx hashes live in git forever |
| `tools/anchor_registry_append.py` | Archive to `tools/archive/anchor_registry_append.py` or delete | Same PR as Shape C |
| `tools/anchor_canary.py` | Rewrite (Shape C) | The Shape C build |
| `tests/test_anchor_canary.py` | Update tests to match new interface | Same PR |

### On Lenovo (~/xrpldashboard/)

| File | Action | When |
|---|---|---|
| `docs/anchor_registry.json` | Delete (or was it even synced there?) | Verify first — may not exist |
| `tools/anchor_canary.py` | Deploy rewritten version | Same PR, pull from main |
| Systemd timer | No change needed — timer fires `anchor_canary.py`, logic changes internally | |

### Ceremony workflow change

**Before Shape C:**
```
Step 1: Sign snapshot → Ed25519
Step 2: Build anchor tx memo
Step 3: Submit tx via Xaman
Step 4: Run anchor_registry_append.py --tx-hash <hash>   ← DIES
Step 5: git add docs/anchor_registry.json && git commit  ← DIES
Step 6: Verify canary
```

**After Shape C:**
```
Step 1: Sign snapshot → Ed25519
Step 2: Build anchor tx memo
Step 3: Submit tx via Xaman
Step 4: Verify tx on-chain (existing cascade lookup, can be manual or scripted)   ← SIMPLIFIED
Step 5: Run canary --dry-run to confirm it sees the new anchor                   ← VERIFICATION
```

Steps 4-5 are now a 2-minute verification instead of a file-write ceremony. The ceremony is shorter and has no local file state to go stale.

### Does anything else still want anchor_registry.json?

- `tools/anchor_canary.py` → Shape C removes this dependency
- `tools/anchor_registry_append.py` → archived
- `tests/test_anchor_canary.py` → currently tests against a mock registry; needs update but doesn't hard-require the file
- The future `/anchors` public page → was always intended to read from the ledger directly; no dependency
- Git history of anchor_registry.json → preserved in the one-time commit before deletion

**Nothing else depends on the file. The kill-plan is clean.**

---

## 6. Bundled wound A — refuse-to-run-without-creds

### Current behavior (code-verified, anchor_canary.py:106-109)

```python
if dry_run or not token or not chat:
    reason = "dry-run" if dry_run else "credentials unset"
    print(f"[anchor-canary dry {reason}] {text}", flush=True)
    return False, reason
```

Missing credentials → the canary runs silently, formats all alerts, prints them to stdout, saves state as if they fired — and no Telegram message is sent. Alerts get marked "sent" in state. Next real run sees them as already-active, no re-fire.

This means: if credentials are missing on the Lenovo (env not loaded, bot token rotated, wrong var name), the canary silently does nothing useful for every daily cycle until someone manually inspects the stdout log.

### Shape C fix

Replace the credential-missing branch with an explicit refusal at startup:

```python
def check_credentials_or_refuse(dry_run: bool) -> None:
    """Called once at main() startup. If not dry-run and credentials are
    missing, print a loud error and exit 1. Never silently swallow creds."""
    if dry_run:
        return
    token = os.environ.get("ANCHOR_CANARY_TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("ANCHOR_CANARY_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print(
            "REFUSE_TO_RUN: ANCHOR_CANARY_TELEGRAM_BOT_TOKEN and/or "
            "ANCHOR_CANARY_TELEGRAM_CHAT_ID are unset.\n"
            "The canary cannot deliver alerts without credentials. "
            "Either set the env vars or invoke with --dry-run.\n"
            "Fix: source ~/.config/xrpldashboard/env before invocation, "
            "or add env vars to the systemd unit EnvironmentFile.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
```

Exit 1 causes the systemd service to fail visibly. The timer's status (`systemctl status xrpld-anchor-canary.timer`) shows the failure. Silence → loud failure.

**Scope:** only production mode. `--dry-run` is explicitly exempt. The credential check runs BEFORE any network I/O.

---

## 7. Bundled wound B — state-file mode separation

### Current behavior (code-verified)

`state_path()` returns `~/.anchor_canary_state.json` regardless of whether `--dry-run` is active. The reconcile loop (`reconcile()`) runs in both modes. `save_state()` is called in both modes.

**Consequence:** a dry-run invocation:
1. Runs `reconcile()` → calls `send_telegram(..., dry_run=True)` → prints to stdout (no actual send)
2. Still mutates `active_alerts` in the state dict as if alerts had fired
3. Still calls `save_state()` — writes the mutated state to `~/.anchor_canary_state.json`

Next production run: those alert IDs are already in `active_alerts`. The reconcile loop sees them as known, checks reminder cadence, may not re-fire if within the 4-hour reminder window. **A dry-run can silently consume a trigger.**

### Shape C fix

Option A (recommended): skip `save_state()` in dry-run mode entirely.
Option B: use a separate state file path in dry-run mode.

**Recommendation: Option A.** Dry-runs are meant to inspect behavior without side effects. A dry-run that doesn't persist state is a clean abstraction. The implementation is one line:

```python
# In main(), currently unconditional:
save_state(state)

# Shape C — conditional:
if not args.dry_run:
    save_state(state)
```

No state pollution from dry-runs. Dry-run is genuinely read-only from state's perspective.

**Downside:** if you run `--dry-run` then immediately run production, the production run starts with the pre-dry-run state, which may re-fire already-known alerts if reminders are due. This is acceptable — the alerts are real, the delivery is correct. A false-fire on a known alert is far better than a consumed trigger.

---

## 8. Migration order

```
Phase 0: Pre-build (keyboard, 5 min)
  git add docs/anchor_registry.json
  git commit -m "anchor: preserve 3-anchor registry as historical artifact before Shape C"
  # File will be deleted in the Shape C PR; the git history preserves the hashes.

Phase 1: Shape C build (post-cert, Day 8+)
  Rewrite tools/anchor_canary.py:
    - Remove registry_path(), load_registry()
    - Remove verify_anchor() (replaced by fetch_account_tx_anchors())
    - Add account_tx query against full-history node (Clio)
    - Add Wound A (refuse-to-run-without-creds)
    - Add Wound B (no save_state in dry-run)
    - Update check_registry_vs_chain() → check_chain_anchors()
    - Keep state machine, formatting, Telegram delivery unchanged
  Archive tools/anchor_registry_append.py → tools/archive/
  Delete docs/anchor_registry.json from repo
  Update tests/test_anchor_canary.py

Phase 2: Lenovo deploy + verify (before anchor #4 ceremony, ~2026-08-28)
  git pull on Lenovo
  source env && python3 tools/anchor_canary.py --dry-run
  # Expect: sees 3 anchors from Clio, latest within freshness threshold
  # Expect: no alerts, heartbeat would send green
  systemctl restart xrpld-anchor-canary.timer
  Watch next daily fire → confirm green

Phase 3: First anchor #4 ceremony (simplified)
  stamp tx → verify on-chain → run canary --dry-run → confirm sees 4 anchors → done
  No anchor_registry_append.py step. No git commit of registry file.
  Ceremony is 2 steps shorter.

Phase 4: Mac cleanup
  Delete docs/anchor_registry.json from Mac (git rm)
  Confirm Lenovo's copy is also gone (or was never synced)
```

**Timing:** cert is Wednesday 2026-08-27. Anchor #4 ceremony is ~2026-08-28 (Thursday, 7 days after #3 on 2026-08-21). Shape C should be built and deployed to Lenovo on Wednesday evening (post-cert) or Thursday morning before the ceremony. This is a hard deadline — if Shape C isn't ready by ceremony time, run the ceremony anyway and do the old Step 4 append as a one-time bridge.

**Fallback if Shape C isn't ready for #4:** run the old `anchor_registry_append.py` for anchor #4. Shape C can still ship afterward — it will correctly discover all 4 anchors from Clio. The registry file just gets git-tracked and then deleted.

---

## 9. LOC budget and complexity

Current `anchor_canary.py`: 809 LOC

Shape C changes:
- Remove: `registry_path()`, `load_registry()`, `check_registry_vs_chain()`, `verify_anchor()` ≈ -130 LOC
- Add: `fetch_account_tx_anchors()`, `check_chain_anchors()`, `check_credentials_or_refuse()` ≈ +90 LOC
- Wound A + B: ≈ +20 LOC
- Net: ≈ 809 - 130 + 110 = **~790 LOC** (roughly neutral)

The rewrite is not a ground-up rebuild — most of the file stays intact (HTTP, memo parsing, state machine, formatting, Telegram delivery). The registry-specific code is ≈16% of the file; that's what gets replaced.

Test coverage: `tests/test_anchor_canary.py` currently mocks the registry load. Tests will shift to mocking `account_tx` responses from Clio. Same test surface area, different mock target.

---

## 10. What Shape C does NOT change

- Tx verification for HISTORICAL anchors: in the current design, the canary verifies each tx hash is on-chain and memo matches the registry. In Shape C, we READ from the chain directly — the "match" is implicit (we parsed the memo FROM the chain tx). But we still verify:
  - All anchor txs are from the anchor account (account_tx scoped to account)
  - Memos are parseable v1 format
  - Dates are monotonically increasing
  - Latest anchor root matches live chain.json ← the forgery tripwire

- The stolen-key/forged-site tripwire (root_mismatch check) is fully preserved. This is the most important check: on-chain anchor says root=X, live chain.json says root=Y → something was forged. Shape C keeps this.

- The weekly Tuesday heartbeat cadence — unchanged.

- The Lenovo systemd timer (xrpld-anchor-canary.timer) — no changes needed.

- The Telegram delivery, alert formatting, state machine — unchanged.

---

## 11. Rulings block — Charlie rules from phone

Each item: one decision, recommendation noted.

**R1 — Full-history node selection**  
Which node(s) does Shape C query for `account_tx`?  
- Option A: `s2-clio.ripple.com:51234` (Clio, full history, same public cascade already in code)  
- Option B: Both `s1.ripple.com` and `s2-clio.ripple.com` as fallback chain  
- Option C: New env var `ANCHOR_CANARY_FULL_HISTORY_NODES` (operator-configurable, default = Clio)

**Recommendation: Option C** (env var with Clio default). Gives flexibility if Clio endpoint changes, zero friction for the current deployment (default just works), and matches the existing `ANCHOR_CANARY_PUBLIC_NODES` pattern.

*Charlie rules: ______*

---

**R2 — Retention-limited / partial-history behavior**  
If the full-history node responds but `ledger_index_min` suggests limited history (not Clio's expected genesis-anchored full history), should the canary:  
- Option A: LOUD SKIP (log, no alert, no state mutation) — recommended  
- Option B: Fire a `witness_partial_history` warning alert and proceed with available data  

**Recommendation: Option A.** An unreliable witness warrants a skip, not a guess. The 8-day freshness window has enough slack for one missed cycle.

*Charlie rules: ______*

---

**R3 — Ceremony Step 4 elimination — confirm**  
Ceremony Step 4 (`anchor_registry_append.py`) is eliminated. The new ceremony ends at: stamp tx → verify on-chain → run canary --dry-run.  
- Approved as described?  
- Any ceremony step to ADD in its place?

**Recommendation: Approved as described.** The `--dry-run` verification step is a natural ceremony close.

*Charlie rules: ______*

---

**R4 — Migration timing**  
- Option A: Ship Shape C post-cert (Wednesday 2026-08-27 evening) — deployed to Lenovo before anchor #4 ceremony Thursday  
- Option B: Ship post-cert but don't hard-deadline before #4 — run old append tool for #4 if Shape C isn't ready, Shape C catches up  
- Option C: Ship before cert — risk introducing a rewritten safety-critical tool into the cert window

**Recommendation: Option A.** Post-cert is the right timing. The build is ~790 LOC with good test coverage; a clean Wednesday evening deploy + Thursday morning dry-run verify is achievable. Option C explicitly rejected (cert window is not the right time for a canary rewrite).

*Charlie rules: ______*

---

**R5 — Wound A: refuse-to-run-without-creds — approve**  
Current: missing creds → silent dry-run (alerts marked sent, no Telegram).  
Shape C: missing creds and not `--dry-run` → exit 1, loud error to stderr.  
- Approved?

**Recommendation: Approved.** Silent credential failures are the class that generates "why didn't the canary fire?" incidents.

*Charlie rules: ______*

---

**R6 — Wound B: dry-run state isolation — approve**  
Current: dry-run writes to production state file (can consume triggers).  
Shape C: dry-run skips `save_state()` entirely (read-only from state perspective).  
- Approved?

**Recommendation: Approved.** A dry-run that has state side effects is not a dry-run.

*Charlie rules: ______*

---

**R7 — anchor_registry.json historical preservation**  
Before deletion, commit the 3-anchor file to git (so the tx hashes live in git history forever).  
- Approved? (This is Phase 0, keyboard, 5 minutes — happens before the build)

**Recommendation: Approved.** The 3 anchor tx hashes are provenance artifacts. Git history is the right permanent home.

*Charlie rules: ______*

---

*End of design pack. Seven rulings → build follows.*
