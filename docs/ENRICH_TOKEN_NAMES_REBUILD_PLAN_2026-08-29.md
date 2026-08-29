# enrich_token_names — Rebuild Plan of Record

**Filed:** 2026-08-29 (Hour 2 of six-hour keyboard sprint)
**Origin:** 9/8 fuse scope-and-cost card, ruled APPROVED by Charlie.
**Status:** cheap defusal + root-cause both landed tonight; full rebuild scheduled for next week AFTER Item-1 identity discovery ships.
**Gating ruling:** deploy-lag design pack (item #5 below) blocks the rest of the rebuild.

---

## Context

- Walker: `enrich_token_names.py` — three-part pipeline populating `token_names.json` from covenant-clean sources (A: MPT ledger metadata, B: issuer TOML self-assertion, C: LP derivation).
- 9/8 mute: `WALKER_STALENESS_MUTES["enrich_token_names"] = "2026-09-08"` in `tools/l1_pager.py`. After expiry, the pager alerts on stale walker_health rows.
- Wound class filed as: `docs/TOKEN_NAMING_DEEP_DIVE.md` (287 lines).
- Covenant read: Source D (community lists — XUMM/FirstLedger/XRPScan/Sologenic/Bithomp) is DEAD by prior ruling. Rebuild uses A/B/C + E (extended-scope TOML) only.

---

## What landed tonight (2026-08-29)

### Defusal (a)(b) — proof-of-life + tighter scope

- **R3 defusal**: `enrich_token_names.py:541-542` — `--limit` default 200 → 500 (top-issuer TOML scope). Docstring line 26 updated. Local commit pending.
- **Manual walker run** (background at 16:40 EDT): reset the 55.9h staleness clock, validate the 500-issuer scope. Result to fold into a later hour's receipt.

### Root-cause (a)(1) — plist drift fixed

- **`launchd/com.charliebruce.xrpldashboard.enrich_token_names.plist`**: `StartInterval=604800 + RunAtLoad=false` → `StartCalendarInterval (Wed 08:00 local) + RunAtLoad=true`. Rationale in-plist as comment.
- **Diagnosis**: `StartInterval` counts from launchctl load moment; two power events (2026-08-13 + 2026-08-20) reset the counter and drifted next-fire out. `walker_health` uses wall-clock so mismatch surfaced as "55.9h stale" on 2026-08-29 even though launchctl thought the walker was on schedule.
- **Storm-08-20 consideration**: user LaunchAgents can miss `StartCalendarInterval` fires during FileVault loginwindow gap. `RunAtLoad=true` catches missed slot at next login. Weekly cadence + ~5min runtime + additive naming = zero harm from occasional double-run after boot.
- **Sibling audit**: `verify_toml.plist` + `pip_audit_walker.plist` (both weekly, both `StartInterval=604800`) already carry `RunAtLoad=true`. `enrich_token_names` was the odd one out; my patch aligns it. No new drift wound in the sibling plists.
- **Keyboard-side follow-up (Hour 5 item)**: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.charliebruce.xrpldashboard.enrich_token_names.plist && cp ~/xrpl_test/launchd/com.charliebruce.xrpldashboard.enrich_token_names.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.charliebruce.xrpldashboard.enrich_token_names.plist`

### Adjacent hardening (a)(2) — weekly-walker alert re-scale

- **`tools/l1_pager.py`**: `_stale_threshold_for(cadence_s)` — cadence-tiered STALE threshold. Sub-weekly unchanged (`max(3× cadence, 24h)`), weekly-and-longer switches to `2× cadence + 1d grace` (weekly = 15d, was 21d). Same wound-class defense-in-depth: even if a future weekly walker drifts silently, the pager fires 6 days sooner.
- **New test file**: `tests/test_l1_pager_weekly_stale_threshold.py` — 6 tests, all pass. Existing l1_pager suite (28 tests) unaffected.

---

## Full rebuild scope — next week, sequenced AFTER Item-1 ships

Six work items remain. Ordered by leverage; item 5 blocks 4 + 6.

### 1. ~~Root-cause the staleness (plist drift)~~
**DONE tonight.** Plist patched in-repo, keyboard reload cards for Hour 5.

### 2. ~~Cadence-alert re-scale for weekly walkers~~
**DONE tonight.** `_stale_threshold_for()` shipped + 6 tests.

### 3. 13 TODO_curation_pass entries audit
**~1h manual + 20min tooling.** Legacy unknown/TODO strings in `token_names.json` bleeding into display. Sweep + re-run Part B (TOML) against them; anything still unresolved gets ledger-domain fallback or explicit "unnamed" tier.

### 4. Display-tier work in templates
**~15 LOC across `templates/tokens.html` + `templates/token_detail.html`.** Surface the naming source (MPT / TOML / LP / domain-fallback / unnamed) as a small tier badge so the 99.7% unnamed pairs aren't hidden.

### 5. **GATING RULING — deploy-lag decision** ⚠️
`token_names.json` writes Mac-local; prod only sees updates at next git push. Options:
- **(A) commit-and-push in the walker itself** — autonomous push; requires push-gate ruling from Charlie
- **(B) publish JSON to a signed endpoint prod reads over HTTPS** — moves state off git, decouples Mac cadence from prod deploy
- **(C) accept lag as-is + document** — status quo, honest label on `/tokens`

**Design pack owed first, no code.** Sequenced after Item-1 ships because both touch the "how does the site prove what it knows, and when" surface — Charlie may want to rule them together.

### 6. Invalidation policy
**~30 LOC in `enrich_token_names.py`.** No current mechanism to drop a stale name if issuer changes TOML. Add a `verified_at` timestamp per entry + a soft-expire after 90d.

---

## Effort summary

- **Landed tonight:** 3 code changes + 1 test file (7 lines of production code, ~50 lines test).
- **Remaining full rebuild:** 3-5 keyboard days across items 3/4/6, plus design pack for item 5.
- **Total lift for `/tokens` to reach "named + tiered + fresh + honest-about-lag":** ~1 week focused, plus Charlie ruling on #5.

---

## Covenant confirmation

Every option in this plan sits within Sources A/B/C/E:
- **A**: TOML self-assertion (Part B live, extended by R3 to top-500)
- **B**: MPT ledger metadata (Part A live)
- **C**: LP derivation (Part C live)
- **E**: Extended domain→TOML via R3's 500-issuer scope

**Source D remains dead.** No option in this plan touches XUMM/FirstLedger/XRPScan/Sologenic/Bithomp community lists.
