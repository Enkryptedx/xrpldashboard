# Lenovo Node Migration — Plan of Record

**Date drafted:** 2026-07-31
**Owner:** Charlie (hardware phases), JJ (software phases via SSH)
**Target:** Migrate rippled node from Mac Studio M4 → Lenovo IdeaCentre Mini 01IRH10R
**Status:** APPROVED. Phase 0 fires the evening this doc lands (RAM stick delivered same day).

---

## Fences (top of doc, non-negotiable)

1. **The site never blinks.** Walkers stay pointed at public nodes throughout every phase. Nothing on `xrpldashboard.com` cuts over to the Lenovo until the Lenovo has proven full sync + vocab parity + census-read parity in overlap.
2. **Mac serves until Phase 4 closes.** The Mac node keeps serving whatever it serves today for the entire migration. Demotion is the last step, not a mid-phase step.
3. **Nothing cuts over unproven.** Every repoint is one consumer at a time, verified against the Mac node's answers before the next consumer moves.
4. **Hardware-on-evidence.** No RAM upgrade beyond the CT16G56C46S5 (→ 32GB) unless observed memory pressure fires the strain trigger in Phase 4. The DDR5 SODIMM price crisis (Q3 2026 through Q4 2027) is why. We do not pre-buy against hypothetical strain.
5. **Payment surface untouched.** API v1 stays parked. This migration is infra-only.

---

## Phase 0 — Install the RAM (TONIGHT, Charlie's hands)

**Goal:** Lenovo boots with 32GB visible, dual-channel active. Nothing else.

**Deliberately boring.** No OS work. No login. No network config. Just physical install + BIOS verify + shutdown.

- [ ] Power down the Lenovo. Unplug the power cable.
- [ ] Wait 30 seconds. Press and hold the power button for 5 seconds to drain residual charge.
- [ ] Open the case per the IdeaCentre Mini 01IRH10R service manual (single access panel — no tools past a Phillips head).
- [ ] Locate the empty SODIMM slot (the stock 16GB stick occupies the other slot).
- [ ] Touch a bare metal surface on the chassis to discharge static before handling the new stick.
- [ ] Seat the **Crucial CT16G56C46S5** (16GB DDR5-5600 SODIMM) in the empty slot: align notch, 30° angle, press home, rotate flat until clips lock.
- [ ] Close the case. Reconnect power. Boot.
- [ ] Enter BIOS (F1 or F2 at Lenovo splash — 01IRH10R defaults to F1).
- [ ] **Verify:** system shows **32GB installed** AND **dual-channel active** (BIOS memory page or Advanced → Memory Config).
- [ ] Exit BIOS without saving. Let it boot to whatever it currently boots to (or POST-halt if unformatted — fine).
- [ ] **Optional but recommended:** one memtest86+ pass (USB stick, ~90 min for 32GB DDR5). Skip only if you're time-boxed.
- [ ] Shut down. Done for tonight.

**Success criteria for "installed":** 32GB visible in BIOS + dual-channel confirmed + boots clean (no beep codes, no POST errors). Nothing more.

**If anything's off:** stop. Photograph the BIOS memory screen, note the exact behavior, message JJ. Don't force it.

---

## Phase 1 — Ubuntu install (Charlie, ~30 min, separate evening)

**Goal:** Lenovo running Ubuntu Server 24.04 LTS, reachable on the LAN via SSH, with a known IP.

Not tonight. Give the RAM install its own success before starting a new task.

- [ ] Download **Ubuntu Server 24.04 LTS** ISO (ubuntu.com/download/server — the LTS, not the interim release).
- [ ] Install **Balena Etcher** on the Mac (etcher.balena.io). Insert an 8GB+ USB stick.
- [ ] Flash the ISO to the USB with Etcher. Wait for verification to complete.
- [ ] On the Lenovo: insert USB, power on, hit **F12** at the splash for boot menu, select the USB device.
- [ ] Ubuntu installer choices:
  - **Language / keyboard:** English (US) or your preference
  - **Network:** DHCP is fine for install. We'll pin the address in the router post-install (or set static during install if you know the LAN subnet — either works).
  - **Storage:** "Use entire disk" is fine; do **NOT** encrypt (LUKS blocks unattended reboots — we want the node to come back up after a power blip without a keyboard). Leave the default LVM layout; the node's history growth headroom lives in the free VG space.
  - **Hostname:** `xrpl-node` (or your preference — call it out to JJ so DNS/tooling matches)
  - **Timezone:** America/New_York (matches Mac)
  - **Your name / username / strong password:** yes, strong. This account gets sudo. Write the password down somewhere you actually trust (1Password, paper in a safe — not a sticky note).
- [ ] **The one critical checkbox: install OpenSSH server.** This is the checkbox that lets JJ take over from Phase 2. If you skip it, we're re-booting from USB.
- [ ] Skip all the "featured server snaps" (Nextcloud, Docker, etc.). We install what we need in Phase 2.
- [ ] Complete install. Reboot when prompted. Remove USB.
- [ ] Login on the console once. Run `ip -brief a` and note the LAN IP (the one that isn't 127.0.0.1).
- [ ] **In the router:** set a DHCP reservation for the Lenovo's MAC address to the IP it currently has (or the IP you want). This locks the address without touching Ubuntu's netplan.
- [ ] Message JJ: **"Lenovo up at 192.168.X.Y, username=`<name>`, ready for Phase 2."**

---

## Phase 2 — Base hardening + rippled build (JJ, via SSH)

**Goal:** Locked-down base OS + rippled at current release, syncing from the network.

Runs from JJ's session over SSH. Charlie doesn't need to babysit past dropping the SSH key in place.

- [ ] Charlie: `ssh-copy-id <user>@<lenovo-ip>` from the Mac (uses the Mac's existing key). Verify JJ can log in.
- [ ] Disable password SSH; keys only. Edit `/etc/ssh/sshd_config`: `PasswordAuthentication no`, `PermitRootLogin no`. Reload sshd.
- [ ] `ufw` firewall: allow 22 (SSH from LAN only if practical), 51235 (rippled peer port), deny inbound everything else. rippled RPC (5005/6006) stays localhost-only — never expose it externally.
- [ ] `unattended-upgrades` enabled for security updates. Standard Ubuntu package.
- [ ] `fail2ban`: **judgment call.** Keys-only SSH + LAN-only exposure means fail2ban is belt-on-belt. Skip unless port 22 ends up WAN-exposed (which it won't in this plan). Revisit if scope changes.
- [ ] Package prerequisites for rippled build: `build-essential`, `cmake`, `git`, `python3`, `libssl-dev`, `pkg-config`, `protobuf-compiler` — plus whatever the current rippled BUILD.md names (verify at build time; the list moves per release).
- [ ] **rippled release choice — RECOMMENDATION: build from the current tagged release** (not the Ubuntu package, not `develop`). Reasons:
  1. The R5 amendment-vocabulary alarm on the Mac (30/75 known vs s1.ripple.com's 31/82) points at Mac node running a stale binary; a current-release build resolves the vocab gap AND gives us the newest optimizations.
  2. The Ubuntu package lags releases by weeks-to-months. Not what we want for a node whose whole job is being current.
  3. Building on the target hardware gives us the exact binary we want, no surprise deps.
  Trade-off: adds ~2h to Phase 2 for the initial build vs. `apt install rippled`. Acceptable.
- [ ] Clone `github.com/XRPLF/rippled` at the current release tag. Configure with `-DCMAKE_BUILD_TYPE=Release` and reasonable core count (`-j` = `nproc` - 1 to keep the box responsive).
- [ ] Configure `rippled.cfg`:
  - `node_size=medium` (matches Mac's current config; the 32GB gives us medium comfortably with headroom)
  - `[node_db]` type=NuDB, path on the largest volume, `online_delete` set to something sane (e.g., 2000 for medium)
  - `[ips]` seeded with the standard boot list
  - `[validators_file]` pointing at the standard published validators.txt
  - RPC bind localhost-only (`127.0.0.1:5005`), admin localhost-only
- [ ] Install as a systemd service (`rippled.service`), enabled, but **do not start yet** — start in the sync step below deliberately so we can watch it.
- [ ] **Sync-from-network is the default path.** Start `rippled`, tail the log, confirm peer connections form, watch `server_info` progress from `disconnected` → `connected` → `syncing` → `full`. On a fresh medium node this typically takes 12-36h; we're not in a hurry.
- [ ] **Accelerator option (LAN copy from Mac node):** if network sync is dragging past ~48h or peers are misbehaving, we can stop rippled on both boxes, `rsync` the Mac's NuDB directory over Thunderbolt/1GbE to the Lenovo, restart both. **Only** worth it if network sync is genuinely stuck — otherwise it's operator overhead for no gain. Do NOT default to LAN copy; sync-from-network is cleaner and validates the peer path we need working anyway.

**Phase 2 exit criteria:** `server_info` reports `server_state: full`, `complete_ledgers` reaches a healthy contiguous range, and the node has been `full` and stable for at least 4 hours.

---

## Phase 3 — Overlap + verification + consumer repoint (JJ)

**Goal:** Lenovo node proven at parity with Mac node, then localhost consumers moved one at a time.

Both nodes run in parallel through this phase. Nothing on prod moves until every check passes.

- [ ] Both nodes running. Both fully synced. Log in `walker_health` isn't touched yet.
- [ ] **Verification checklist (all must PASS before any repoint):**
  - [ ] **Vocab parity:** `feature` RPC output on Lenovo vs `s1.ripple.com` — same known-amendment count (target: matches s1 exactly, resolves the 30/75 vs 31/82 gap that flagged in R5 monitoring)
  - [ ] **Ledger index parity:** `server_info` on Lenovo vs Mac — both within 1-2 ledgers of each other, both within 1-2 of the network
  - [ ] **Census-read spot-checks:** run three representative census reads (e.g., total AMMs, total MPTs, total TokenEscrows) against both nodes — results must match exactly
  - [ ] **Load behavior:** Lenovo's `load_factor` under normal query pressure stays comparable to Mac's (order-of-magnitude, not 10× worse)
  - [ ] **Log health:** 24h of Lenovo `debug.log` contains no repeated FATAL / repeated peer-blacklisting / repeated resource-exhaustion signals
- [ ] **Repoint consumers one at a time**, verify each before the next:
  1. Least-critical walker first (e.g., a low-frequency backfill) — flip its config to point at Lenovo, watch for one full cycle, confirm identical outputs to the Mac-pointed baseline.
  2. Next-least-critical. Same verify pattern.
  3. Continue through the list; the RLUSD live-state fetcher and the four-layer audit walkers move **last** because they're the most-visible if wrong.
- [ ] `answer_plausibility_walker` remains pointed at public nodes throughout Phase 3 (its whole job is cross-checking us against external truth; pointing it at our own new node defeats the point).
- [ ] **Mac node demotes to warm standby** at Phase 3 exit: still running, still synced, no longer receiving consumer queries. It's the fallback if Phase 4 surfaces a problem.

### Phase 3.A — Batch A census correction (2026-08-02, twice-corrected)

The "24 consumers of the local node" figure from the original design was wrong twice, in the same class of error both times: **env-var presence in a plist ≠ traffic path in code.** The only trustworthy audit is grep-and-read the walker code.

- **First correction:** "24 consumers" counted walkers that made any XRPL RPC call. Most of those were pointed at `https://s1.ripple.com:51234` or similar public infra, never at the Mac. True count of walkers routed through `xrpl_client.py`'s local-node cascade was smaller.
- **Second correction (mid-batch, ~08:20 EDT 2026-08-02):** the initial "true Mac-dep = 4" count still miscounted `nft_activity_backfill`. Reading the walker before touching its plist surfaced that backfill mode reads a different lever entirely — `XRPL_BACKFILL_CLIO` (default `https://s2-clio.ripple.com:51234/`) — because local rippled would return `lgrNotFound` for every backfill request against its ~10k-ledger window vs the ~2M-ledger backfill span. Setting `XRPL_LOCAL_NODE` on the backfill plist would look like a repoint but change zero traffic.

**Ratified Batch A = 3 walkers.** All shipped GREEN 2026-08-02:

| # | Walker | Plist | Outcome |
|---|---|---|---|
| 1 | `oracle_walker` | `launchd/com.charliebruce.xrpldashboard.oracle_walker.plist` | GREEN — 2 DIA rows, pair_count 14 |
| 2 | `escrow_walker` | `launchd/com.charliebruce.xrpldashboard.escrow_walker.plist` | GREEN — 101 rows, 20/20 Ripple cohort |
| 3 | `nft_activity_walker` (activity mode) | `launchd/com.charliebruce.xrpldashboard.nft_activity_walker.plist` | GREEN — 86 ledgers, 225 nft rows, cursor 106020511 → 106020597 in one cycle |

**NOT Batch A — architectural public-Clio dependency:**
- `nft_activity_backfill` — reads `XRPL_BACKFILL_CLIO`, hits public Clio by design. Lenovo has the same ~10k `ledger_history` as Mac; a backfill repoint would fail with `lgrNotFound` for every request in the ~2M-ledger span. **Stays on public Clio.**

**Batch C (audit-tier, held for LAST per Phase 3):**
- `ledger_definitions_walker` (R5 vocab-receipt walker)
- `cross_check_walker` (Layer 3 audit — compares LOCAL vs s1; the whole point is external cross-check, so the local end being Mac vs Lenovo is the actual comparison being made — move deliberately, code-path re-audit REQUIRED before staging per twice-proven lesson)

**Also on public s1/s2 today, moving is a SEPARATE architectural decision post-soak:**
- Approximately 10 walkers hit `XRPL_NODE` / `XRPL_RPC` / `XRPL_CLIO_NODE` with defaults on public infra (`enrich_token_names`, `verify_toml_accounts`, `permissioned_domains_walker`, `credentials_walker`, `mpt_holders_refresh`, `bridge_signer_walker`, `rank_amms`, `lending_snapshot`, `lending_data`, `amm_tvl_recorder`, `coverage_register_walker`; plus snapshots).
- Migration's stated purpose is **Mac relief** — that purpose only touches the walkers actually burdening the Mac (Batch A + Batch C). Moving the others is "should our own node become the primary source, replacing distributed public infrastructure?" — a separate decision post-30-day-soak, memo-owned, not a side effect of tonight's env-var mechanics.

**Twice-proven lesson:** future Batch B/C staging (and any fleet-wide "point at Lenovo" impulse) MUST grep-and-read the walker code before touching its plist. Sibling backlog: normalize the fleet's 3+ RPC-target env-var names (`XRPL_LOCAL_NODE` / `XRPL_NODE` / `XRPL_RPC` / `XRPL_CLIO_NODE`) to `XRPL_LOCAL_NODE` fleet-wide + add `walker_health.rpc_url` column so which-URL-was-hit becomes recorded fact — would have made this class of miscount impossible.

**Phase 3 exit criteria:** all verification checks passed, all non-public-node consumers repointed to Lenovo, Mac is warm-standby-only, no prod alarms fired during the transition.

---

## Phase 4 — 30-day soak → Mac retirement

**Goal:** Lenovo proves it in production for a full month. Mac retires cleanly.

- [ ] Day 0 of soak = the day Phase 3 exited.
- [ ] Watch: `walker_health` for the Lenovo-served walkers, four-layer audit output (Layer 2 alarms especially), BetterStack heartbeats, R5 vocab alarm (should stay green now).
- [ ] Watch: **Lenovo memory-pressure telemetry** — this is the ONLY signal that reopens the 64GB question. Add a walker or a small systemd timer that logs `/proc/meminfo` + swap usage + rippled RSS to a small table, hourly. Alarm on: canary fires, sustained swap use (>0 for >1h), OS stalls, or rippled restarts under memory pressure.
- [ ] **Strain-trigger clause (armed):** if and ONLY if memory-pressure telemetry fires during the soak, we reopen the RAM question at that week's DDR5 SODIMM prices. No re-litigating the 32GB decision on speculation. The trigger is the observation, not the anxiety.
- [ ] At day 30, if no strain trigger fired and no alarms outstanding: **Mac node retires.**
  - Stop `rippled` on the Mac
  - Archive `rippled.cfg`, `validators.txt`, and a copy of the last-known `server_info` output to `/Users/charliebruce/xrpl_test/_private/mac_node_archive_2026-08-XX/`
  - Uninstall rippled from the Mac (or leave the binary in place and just disable the LaunchAgent — Charlie's preference)
  - Update `docs/DEPLOY.md` and `docs/DIAGNOSTIC_BRIEF_local_rippled_2026-07-16.md` to reflect the Lenovo as the primary local node
  - Mac breathes: freed RAM, freed disk, freed CPU. Walkers + Flask + everything else get the whole box.

**Phase 4 exit criteria:** 30 clean days, Mac archived and retired, docs updated, JJ + Charlie both confirm no surprises during the soak.

---

## Amendment — Early retirement (2026-08-04, pre-soak-completion)

**Ruling:** Charlie's word (msg 10608). Mac node retired **early**, mid-Phase-4 soak, on 2026-08-04.

**Reasoning:** the standby was serving nobody. Post-null-triple investigation (2026-08-04) surfaced that the Mac's rippled was `server_state: disconnected`, `peers: 0`, `validated_ledger.age: 3.9 days` — the node had been broken for days without alarm. Simultaneously the Lenovo showed `server_state: full`, `validated_ledger.age: 4 seconds`, and was carrying every real consumer (Batch A walkers + MCP server after the source-truth fix at `847f131`). The standby posture was insurance at full cost — 6.3 GB RSS, 37.5% of Mac RAM, 62 GB of disk — with zero coverage. Retiring closes the cost side; the parachute keeps resurrection cheap.

**Retirement mechanism (executed 2026-08-04 EDT):**
```
launchctl unload -w /Users/charliebruce/Library/LaunchAgents/com.charliebruce.rippled.plist
```
`-w` writes to the launchd override state so the service does NOT auto-relaunch at next login. The plist file itself is untouched on disk.

**Parachute (all preserved on disk, one-command resurrection):**
- Plist: `/Users/charliebruce/Library/LaunchAgents/com.charliebruce.rippled.plist` (Jun 24 20:25, 1050B)
- Config: `/Users/charliebruce/.config/rippled/rippled.cfg` (Jun 24 19:36, 55795B)
- Binary: `/Users/charliebruce/rippled/.build/rippled`
- Data dir: `/Users/charliebruce/rippled-data/` (~62 GB — larger than the pre-retirement ~23 GB estimate; NuDB grew during the disconnected period)
- Logs: `/Users/charliebruce/rippled-data/logs/launchd-{out,err}.log`
- NuDB rebuild ops script: `ops/rippled_nudb_rebuild.sh` (hardcoded to `localhost:5005` — dormant, valid for resurrection)

**Resurrection command:** `launchctl load -w /Users/charliebruce/Library/LaunchAgents/com.charliebruce.rippled.plist`.  Post-load the Mac rippled comes back on `127.0.0.1:5005`. To route the MCP server / walker fleet back to the Mac, flip `XRPL_LOCAL_NODE` in `~/.config/xrpldashboard/env` from `http://192.168.40.95:5005` (Lenovo) to `http://127.0.0.1:5005` (Mac).

**Disk-space note:** the 62 GB NuDB stays on-disk until soak-end (~2026-08-31 by original Phase 4 timeline). A Charlie-word deletion decision after that. Not reclaiming automatically — resurrection lands unusable if the data dir is gone and would require a rebuild sync.

**Post-retirement verification (2026-08-04, immediately after unload):**
- `launchctl list | grep rippled` → empty (unloaded)
- `ps -p 28264` → gone
- `lsof -iTCP:5005 -sTCP:LISTEN` → empty (port free)
- Mac RAM relief: pre-retirement `Pages free: 3886` (~60 MB) → post-retirement `Pages free: 504,507` (~7.82 GB). **+7.8 GB reclaimed in 30 seconds.** Pre-retirement `Pages active: 332,902` → post `131,440` (kernel actively reclaiming). Wired pages unchanged (~95 K).
- FD relief: pre-retirement 45 FDs on the rippled process → 0 (process gone).
- Consumer sweep: only two live code paths carry a `localhost:5005` fallback (walkers + MCP), both env-var-overridable, both wrapper scripts source `~/.config/xrpldashboard/env` which now sets `XRPL_LOCAL_NODE=http://192.168.40.95:5005`. Confirmed by grep for `http[s]?://(127\.0\.0\.1|localhost):5005` across `*.py`, `*.sh`, `*.plist` — no remaining hardcoded consumer expects the Mac port. Only exception: `ops/rippled_nudb_rebuild.sh` (Mac-side ops tooling, part of the parachute).

**Retirement satellite sweep (added 2026-08-04, same day, from a live miss):**

The retirement checklist above swept **consumers** (walkers, MCP, code paths). It did NOT sweep **watchers** — long-running processes whose only job is to observe the retired service. On 2026-08-04 ~09:49 EDT (~2h after the unload) a macOS `display notification` popup surfaced from `ops/rippled_nudb_rebuild.sh` running with `WATCH` mode, PID 12105 (parent 12102, launched Jul 19). The watcher had been polling `127.0.0.1:5005` on a rebuild-progress loop for 22,269 iterations; post-retirement its T2_NO_SYNC_PROGRESS_2H tripwire fired because the port went silent. False-positive alarm — the port was silent because we asked it to be — but a live signal that a process still expected the Mac node.

**Cleanup executed 2026-08-04:** killed PIDs 12105 + 12102; parked `ALERT_20260719-0541.txt` + `rebuild_20260719-0541.log` (7.8 MB, 22,269 iterations of progress state) to `parked/mac-node-retirement-2026-08-04/`. Full second-pass sweep across `launchctl list`, `crontab -l`, `ps aux | grep 5005`, and remaining launchd plists found one candidate — `com.charliebruce.xrpldashboard.ledger_definitions_walker` — but its wrapper `launchd/run_ledger_definitions_walker.sh` sources `~/.config/xrpldashboard/env` (canonical `XRPL_LOCAL_NODE=http://192.168.40.95:5005` since 2026-08-02 Batch A), and the walker Python reads that var directly (`ledger_definitions_walker.py:36`). Correctly pointed at Lenovo already; last stale exit=1 was a transient Lenovo reachability blip, not a Mac-node dependency.

**Generalized rule (codify for future retirements):** *retiring a service means sweeping its process tree — consumers AND watchers AND monitors — not just the process.* The retirement checklist above assumed "unload the plist, sweep the code paths that call it, done." That misses the class of processes whose relationship to the retired service is *observation*, not *use*: rebuild watchers, health probes launched from ad-hoc shells, monitor loops with tripwires, tail-follow scripts. They don't appear in `git grep` because they're runtime state, not code. They don't appear in `launchctl list` if they were started manually in a terminal. They surface when their alarm fires against a now-silent port. Next retirement's checklist adds a pre-unload step: **`ps auxf | grep <port>` and `pgrep -fl <retired-service-name>` — anything that isn't the service itself is a watcher or a consumer, catalog it, decide kill-or-repoint before the unload lands.** The 2026-08-04 popup was the live-catch that surfaced the missing rule; sweep found no other survivors.

**Deferred (unchanged by early retirement):**
- Original Phase 4 archive-of-cfg-and-validators step: still queued for the soak-end Charlie-word deletion decision.
- Docs `DEPLOY.md` and `DIAGNOSTIC_BRIEF_local_rippled_2026-07-16.md` update: queued for soak-end. Retirement was early; the "primary local node is Lenovo" reality has been true since 2026-08-02 Batch A completion — those docs already lag.
- Uninstall of the rippled binary itself: **not done**. Parachute holds all files in place; the binary at `~/rippled/.build/rippled` stays until Charlie's word.

---

## Adjacent items (do at the right phase, not before)

- **Kraken API key rotation** (63-day stall as of 2026-07-31): this is the first Lenovo login opening move at Phase 1 completion. Rotate keys → check activity → redact/delete `~/Desktop/_old_bots/kraken_*.py` on the Mac. Codified in MEMORY.md as `project_kraken_api_key_rotation_park_with_trigger`.
- **MCP server daemonization:** deferred until Charlie authorizes launchd/systemd on the Lenovo post-Phase 3. Design in `docs/AGENT_TIER_DESIGN.md`. Not part of this migration; noted here so it's on the radar for when the Lenovo is stable.
- **CF WARP / WiFi outage fallback:** the 2026-07-24 WiFi outage stayed on Charlie's side. Not a Lenovo concern, but the Lenovo being on Ethernet (recommended over WiFi for a node) sidesteps the whole class.

---

## What this document is NOT

- Not a design doc. The design lives in code + existing infra; this is an execution checklist.
- Not a decision doc. Every decision (32GB not 64GB, Lenovo-node-only workload split, sync-from-network default, 30-day soak) is already made and captured in MEMORY.md. This doc executes them.
- Not a live-changing document. Once Phase 0 fires, edits only after phase completion (post-Phase 1 lessons, post-Phase 3 lessons). No mid-phase re-litigating.

---

## References

- `MEMORY.md` — `project_lenovo_model_confirmed`, `project_xrpldashboard_m4_stays_workload_split`, `project_ddr5_sodimm_price_crisis_2026-07`, `feedback_new_hardware_over_refurb`, `project_kraken_api_key_rotation_park_with_trigger`
- `docs/DIAGNOSTIC_BRIEF_local_rippled_2026-07-16.md` — Mac node current shape
- `docs/AGENT_TIER_DESIGN.md` — MCP server (deferred to post-Phase 3)
- `docs/WORKING_TREE_DISCIPLINE.md` — four-destination rule applies at every phase boundary
