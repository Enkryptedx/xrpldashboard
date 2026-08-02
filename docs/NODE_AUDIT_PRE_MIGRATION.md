# Node Audit — Pre-Migration

**Date:** 2026-07-31 evening (Fri) → 2026-08-01 03:04 UTC audit window
**Principle:** don't replicate a system you haven't interrogated. The Lenovo IdeaCentre Mini 01IRH10R gets a **deliberately-designed** rippled config, not an inherited one.
**Gate:** Charlie's read of this doc → decisions on history depth + any WORKAROUND-class setting he wants to consciously keep → Phase 2 proper (Ubuntu install, rippled build, config apply).

---

## Post-Phase-1 Amendment (2026-08-01, pre-Phase-2 execute)

Phase 1 (Ubuntu install + network + SSH) shipped 2026-08-01. Three reconciliations against reality before Phase 2:

1. **RAM reality:** 2×16GB TeamGroup ELITE DDR5-5600 SODIMM matched pair (32GB total, dual-channel confirmed). Better than the planned Crucial+OEM mix — matched pair means clean dual-channel with no timing negotiation surprises.
2. **Disk reality:** WD SN7100S 512GB NVMe (476.9 GB usable), **not the 1TB the §3 options table assumed.** Options B (256K/~450GB) and C (2M/~3.5TB) do not fit; both moved to "reserved for future disk upgrade."
3. **History decision RATIFIED — Option A (10,000 ledgers, ~13.5h, ~25GB NuDB).** Matches Mac exactly. Zero risk of over-committing disk. Consumer map (§3.2) confirms nothing on the current xrpldashboard surface needs deeper local history. Lenovo's headroom stays available (~274GB free in ubuntu-vg after LV carve) for a later deliberate expansion, not spent tonight.
4. **rippled tag RATIFIED — `3.3.0-rc1`** (git tag 2026-07-16). Earliest public tag that closes the R5 vocab gap (adds `sfMutableFlags` + `sfSponsor` definitions). `3.3.0-rc6` referenced elsewhere in this doc is s1.ripple.com's internal build label, **not a public git tag** — do not `git checkout 3.3.0-rc6`, it will fail.
5. **LV plan:** dedicated `/var/lib/rippled` LV carved from ubuntu-vg free space, 100GB, ext4, fstab-mounted BEFORE rippled first-start. OS growth and rippled DB growth stay divorced. 100GB gives Option A 4× headroom.
6. **Blocker fix pattern:** both hardcoded blockers (§4.2) get the env-var pattern with `localhost:5005` default. Ships safely before Lenovo cutover (no behavior change until env var is set).

Below, references to "Option B" / "3.3.0-rc6" / "1TB SSD" remain in-line as historical audit context — this amendment supersedes them for execution.

---

## Executive summary

The Mac node is **not the gold standard we're preserving** — it's a degraded box we're rescuing from. Key findings:

1. **Mac rippled is currently degraded, not healthy.** `server_state=connected` (not `full`), validated_ledger.age = 13.7 hours stale, only 1 peer, chronic LoadMonitor:WRN every few minutes. Only 91.8 seconds spent in `full` state over 39.6h uptime. This *strengthens* the migration case — the 32GB dedicated Lenovo should trivially stay `full`.
2. **R5 vocab alarm = pure version lag** (verdict: VERSION). Local rippled 3.2.0 vs s1's 3.3.0-rc6. 8 missing type definitions (5 ConfidentialMPT tx + 2 Sponsorship tx + 1 Sponsorship entry) were added between June 26 and July 10. Building 3.3.0-rc6+ on the Lenovo fixes it automatically — no config change, no code change.
3. **6 WORKAROUND-class config items** must NOT copy to Lenovo (details in §1).
4. **2 hardcoded consumer blockers** need code edits before cutover: `cross_check_walker.py:74`, `ledger_definitions_walker.py:35`. Everything else routes through `xrpl_client.py` with graceful s1/s2 cascade — 1 env var + 2 edits repoints the entire fleet.
5. **History depth is a live decision** (staged in §3) — current 10,000 ledgers (~13.5h) is minimal; the Lenovo can afford more, but no consumer today needs it.

---

## 1. Config inventory — every setting gets a verdict

**Mac live config:** `/Users/charliebruce/.config/rippled/rippled.cfg`
**Mac rippled binary:** `/Users/charliebruce/rippled/.build/rippled`
**Mac rippled version:** `xrpld 3.2.0` (git `3c43f4614f87965298773279ff5b85d4c56c637b`)
**Mac validators.txt:** `/Users/charliebruce/.config/rippled/validators.txt` (standard: vl.ripple.com + unl.xrplf.org, threshold 0)
**Backup configs present** (indicating prior surgical edits): `rippled.cfg.bak-pre-ssl-fix-20260617-172734`, `rippled.cfg.pre-online-delete-fix.2026-06-24`

### 1.1 Verdict table

| Stanza | Setting | Verdict | Reason |
|---|---|---|---|
| `[server]` | `port_rpc_admin_local` | DEFAULT | Standard admin RPC entry |
| `[server]` | `port_peer` | DEFAULT | Standard peer entry |
| `[server]` | `port_ws_admin_local` | DEFAULT | Standard admin WS entry |
| `[server]` | `#port_ws_public` (commented) | DELIBERATE | No public API surface — this node is for internal dashboarding only. **Keep.** |
| `[port_rpc_admin_local]` | `port=5005` | DEFAULT | Stock admin RPC port |
| `[port_rpc_admin_local]` | `ip=127.0.0.1` | DEFAULT | Loopback only ✓ |
| `[port_rpc_admin_local]` | `admin=127.0.0.1` | DEFAULT | Correct — localhost admin |
| `[port_rpc_admin_local]` | `protocol=http` | DEFAULT | Loopback unencrypted — fine |
| `[port_peer]` | `port=51235` | DEFAULT | Standard peer port |
| `[port_peer]` | `ip=0.0.0.0` | DEFAULT | Accept inbound peer connections |
| `[port_peer]` | `protocol=peer` | DEFAULT | Required |
| `[port_ws_admin_local]` | `port=6006` | DEFAULT | Stock admin WS port |
| `[port_ws_admin_local]` | `send_queue_limit=500` | **WORKAROUND** | Default is 100. Raised to 500, likely to prevent WS disconnects when 29 walkers + Flask fired subscription bursts. **Drop on Lenovo** — dedicated box has no such contention. Revert to default 100 (or omit key). |
| `[port_grpc]` | `port=50051` etc | DEFAULT | Dead stanza — not listed in `[server]`, so port isn't opened. Copy-paste from example. Harmless. |
| `[node_db]` | `type=NuDB` | DELIBERATE | Correct for non-validator with SSD. **Keep on Lenovo.** |
| `[node_db]` | `path=/Users/charliebruce/rippled-data/db/nudb` | DELIBERATE (Mac-specific) | Path is Mac-only. **Rewrite for Lenovo** → `/var/lib/rippled/db/nudb`. |
| `[node_db]` | `online_delete=10000` | DELIBERATE | Explicitly chosen 2026-06-24 (backup file exists). Bounds NuDB growth. Value depends on history decision (§3). |
| `[node_db]` | `advisory_delete=0` | DEFAULT | Auto-delete — correct for non-validator. **Keep.** |
| `[node_db]` | **absent:** `cache_size` | **WORKAROUND by omission** | Nodestore cache disabled — heap pressure workaround. **Must add back on Lenovo** — recommend `cache_size=16384` for 32GB box. |
| `[node_db]` | **absent:** `cache_age` | **WORKAROUND by omission** | Same. Recommend `cache_age=5`. |
| `[database_path]` | `/Users/charliebruce/rippled-data/db` | DELIBERATE (Mac-specific) | **Rewrite for Lenovo** → `/var/lib/rippled/db`. |
| `[debug_logfile]` | `/Users/charliebruce/rippled-data/logs/debug.log` | DELIBERATE (Mac-specific) | **Rewrite for Lenovo** → `/var/log/rippled/debug.log`. Ubuntu logrotate replaces macOS newsyslog. |
| `[node_size]` | `medium` | **WORKAROUND** | Pinned to prevent auto-detection surprises during 16GB co-tenancy. Highest-impact wrong-copy. **Change to `huge`** on Lenovo (32GB dedicated → auto-detect would pick `huge` anyway; explicit is safer). |
| `[ledger_history]` | `10000` | DELIBERATE | ~13.5h retention. See §3 for staged decision. |
| `[fetch_depth]` | `10000` | DELIBERATE | Matches `ledger_history`. See §3. |
| `[validators_file]` | `validators.txt` | DEFAULT | Relative path — resolves to config dir. **Keep.** |
| `[rpc_startup]` | `log_level warning` | **WORKAROUND** | Suppresses info-level messages to save disk on the co-tenancy Mac. **Change to `info` on Lenovo** — operational visibility matters more than log volume on a dedicated 500GB+ box. |
| `[ssl_verify]` | `1` | DEFAULT | Cert verification on ✓ |
| `[ssl_verify_file]` | `/etc/ssl/cert.pem` | **WORKAROUND (macOS-specific)** | Added 2026-06-17 (backup file exists) — macOS OpenSSL couldn't find system CA store, breaking validator-list HTTPS fetches. **Test on Lenovo without this line first.** If validator list fetches fail, add: `/etc/ssl/certs/ca-certificates.crt` (Ubuntu path). |
| absent | `[ips]` | DEFAULT | Uses built-in bootstrap. Fine. |
| absent | `[ips_fixed]` | DEFAULT | No static peer pinning. Fine. |
| absent | `peers_max` | DEFAULT (~21 internal) | **Consider adding `peers_max=50`** on Lenovo — better network citizenship + more resilient sync. Mac's 1-peer degradation may partly reflect no explicit peer target. |
| absent | `[compression]` | DEFAULT (off) | Optional — CPU/bandwidth tradeoff. Leave off unless egress-constrained. |
| absent | `[insight]` | DEFAULT (no StatsD) | Leave off unless we wire metrics later. |
| absent | `[network_id]` | DEFAULT (mainnet) | Fine — validators.txt sets network implicitly. |

### 1.2 Ops script drift check

`ops/rippled_nudb_rebuild.sh` paths cross-checked against live config: **NO DRIFT.** All paths (DB_DIR, NUDB, LOG_DIR, DEBUG_LOG, PLIST, binary path, LABEL) match live config exactly. Script + plist + config are internally consistent on the Mac.

**Post-migration note:** the ops script is Mac-specific (LaunchAgent, macOS `pgrep`, macOS paths). A parallel `ops/rippled_nudb_rebuild_lenovo.sh` should be authored for the Lenovo (systemd unit, Ubuntu paths) — not tonight, but before the Lenovo is trusted as sole source.

---

## 2. R5 vocab gap — root cause: VERSION

### 2.1 The anomaly

R5 (formally CC-6 `ledger_vocab_local_vs_public` in `cross_check_walker.py:448-532`) compares the Mac's `server_definitions` output against s1.ripple.com's:

| Endpoint | Entry types | Tx types |
|---|---|---|
| Mac `localhost:5005` (rippled 3.2.0) | 30 | 75 |
| s1.ripple.com:51234 (rippled 3.3.0-rc6 via Clio 2.7.1) | 31 | 82 |

Counts reproduced independently — R5's report is accurate.

### 2.2 Type-name diff

**Entry types missing locally (1):** `Sponsorship`

**Tx types missing locally (7):** `ConfidentialMPTClawback`, `ConfidentialMPTConvert`, `ConfidentialMPTConvertBack`, `ConfidentialMPTMergeInbox`, `ConfidentialMPTSend`, `SponsorshipSet`, `SponsorshipTransfer`

### 2.3 Release correlation

| Feature | XRPLF/rippled commit | Merged | Released in |
|---|---|---|---|
| ConfidentialMPT (5 tx types) — XLS-95, amendment `featureConfidentialTransfer` | `768d7603b1` | 2026-06-26 | 3.3.0-b1 (tag 2026-07-08) |
| Sponsorship (2 tx + 1 entry) — XLS-68, amendment `featureSponsor` | `fd2cc6dcb3` | 2026-07-10 | 3.3.0-rc1 (tag 2026-07-16) |

### 2.4 Verdict: VERSION lag

- Local Mac is 3.2.0 (pre-both features)
- s1 is 3.3.0-rc6 (post-both)
- No cfg line suppresses `server_definitions` (it reads the compiled-in type table — nothing to suppress)
- Comparison shape is sound (both sides use `server_definitions`, both strip `-1` sentinel, Clio proxies correctly)

**Lenovo implication:** build 3.3.0-rc6 (or the latest stable/rc as of Phase 2 kickoff) from source. R5 goes green automatically — no code change, no config change. The `LENOVO_MIGRATION.md` §Phase 2 already specifies "current release." That's the fix. Do NOT carry the 3.2.0 binary.

**Related note on `learn/confidential-transfers` design doc:** amendment `featureConfidentialTransfer` is now in a released `-rc` — the CT design doc's unpark trigger ("amendment enters voting, visible in `feature` RPC") is one release-tag closer than when the doc was written. Not there yet on mainnet, but the code exists in 3.3.0-rc6. Watch mainnet `feature` output post-Lenovo.

---

## 3. Storage + history decision — STAGED for Charlie

### 3.1 Current state (Mac, right now)

- **NuDB size:** 23 GB on disk (2 shards, clean rotation, no corruption artifacts, no orphan lock files, both `nudb.log` files 0 bytes = clean shutdown state)
- **complete_ledgers:** `105962680-105976806` (14,126 contiguous ledgers ≈ 13.5 hours at ~3.5s/ledger)
- **server_state:** `connected` ⚠️ (NOT `full`)
- **validated_ledger.age:** 49,547 seconds ≈ **13.7 hours stale** ⚠️
- **peers connected:** 1 ⚠️
- **uptime:** 39.6 hours; time in `full` state: 91.8 seconds ⚠️

The Mac is **not tracking the live chain tip** and hasn't been for 13+ hours. This is the "baseline" the Lenovo is replacing — a struggling node, not a healthy one.

### 3.2 Consumer needs (what actually reads history from the local node)

From §4's consumer map: **zero** current consumers need deep history from the local node. History-dependent walkers (nft_activity_backfill, mpt_snapshot, lending_snapshot, daily_snapshot, signed_snapshot, rank_amms, wallet_data) all point at public nodes (s1.ripple.com, s2.ripple.com, s2-clio, xrplcluster). The local node serves:
- Fresh `server_info` / `server_state` / `server_definitions` (no history needed)
- Fresh `account_info`, `account_objects`, `gateway_balances` (current state only)
- Fresh `feature` output (current amendments only)
- `account_tx` for recent walker windows (nft_activity_walker forward mode — last ~24h)

**Nothing on the current xrpldashboard consumer surface needs more than ~24h of local history.**

### 3.3 Options (staged for Charlie)

| Option | Ledgers held | Time span | Disk (approx) | Sync-time impact | Rationale |
|---|---|---|---|---|---|
| **A — MINIMAL** (mirror Mac) **← RATIFIED 2026-08-01** | 10,000 | ~13.5h | ~25 GB | Fast initial sync (hours to `full`) | Matches Mac exactly. Zero risk of over-committing 476GB disk. Headroom stays available for deliberate later expansion. |
| **B — WORKING** ~~(recommended)~~ **RESERVED — needs disk upgrade** | 256,000 | ~10 days | ~450 GB | Fast initial sync; ~10 days to reach full retention | Would not fit on the 512GB SN7100S with OS + logs + growth headroom. Reserved for a future disk upgrade. |
| **C — EXTENDED** **RESERVED — needs external storage** | 2,000,000 | ~78 days | ~3.5 TB | Fast initial sync; ~78 days to fill | Exceeds internal SSD by ~7×. Would require external storage. Not on tonight's path. |
| **D — FULL** | all | 12+ years | 15+ TB | Multi-week initial sync | Not appropriate for a 1TB Mini. Would need array-class storage. **Ruled out on hardware grounds.** |

### 3.4 Ratified: Option A (MINIMAL, 10,000 ledgers ≈ 13.5h)

**Post-Phase-1 ratification (2026-08-01)** — supersedes the pre-Phase-1 Option-B recommendation preserved below.

**Reasoning as ratified:**
1. 512GB disk reality (not 1TB) makes Option B (~450GB) unsafe. Option A is the largest option that leaves comfortable margin (25GB in a 100GB LV = 4× headroom).
2. Consumer map (§3.2) confirms zero current consumers need >24h of local history. Option A satisfies today's observed demand; deliberate expansion stays available (~274GB free in ubuntu-vg) for a real future need.
3. Matches Mac exactly — same `online_delete=10000` — so Lenovo's tracking behavior is not a new variable. The variable being tested at cutover is "same config, better hardware, healthy state" — not "different retention, different hardware, unknown state."
4. `online_delete` + `advisory_delete=0` autopilots the retention — no manual intervention.

**Config change vs Mac:** none for retention. `online_delete=10000`, `ledger_history=10000`, `fetch_depth=10000` all match Mac.

**Original pre-Phase-1 recommendation preserved for audit trail:**
> JJ recommended Option B (WORKING, 256K, ~450GB) for deliberate headroom. That recommendation assumed a 1TB SSD; Phase 1 landed a 512GB SSD, forcing the ratification above.

---

## 4. Consumer map — Phase 3 cutover checklist

### 4.1 Local-node consumers (11 total)

| # | File:Line | Consumer | Method | Cadence | Failure mode | Repoint |
|---|---|---|---|---|---|---|
| 1 | `xrpl_client.py:24` | `XrplClient` shared lib | `server_info` probe + passthrough | per-request | **GRACEFUL** → s1 → s2 cascade, logs to `walker_node_fallback` | ENV VAR `XRPL_LOCAL_NODE` |
| 2 | `nft_activity_walker.py:277` | forward walker | `account_tx`, `ledger` | 300s | GRACEFUL (via XrplClient) | ENV VAR |
| 3 | `escrow_walker.py:106` | escrow walker | `account_objects` | 1800s | GRACEFUL | ENV VAR |
| 4 | `oracle_walker.py:161` | oracle walker | `account_objects(oracle)` | 1800s | GRACEFUL | ENV VAR |
| 5 | `rlusd_live.py:350` | RLUSD refresher | `gateway_balances`, `account_info` | 300s | GRACEFUL | ENV VAR |
| 6 | `total_supply.py:39` | Flask supply | `account_info` | on request | GRACEFUL | ENV VAR |
| 7 | `escrow_supply.py:76` | Flask escrow supply | `account_objects` | on request | GRACEFUL | ENV VAR |
| 8 | `cold_storage.py:79` | Flask cold storage | `account_info` | on request | GRACEFUL | ENV VAR |
| 9 | `check_data.py:524` | `/check` utility | page checks | on demand | GRACEFUL | ENV VAR |
| 10 | `token_data.py:308` | Flask token page | token queries | on request | GRACEFUL | ENV VAR |
| 11 | `cross_check_walker.py:74` | CC pair `amendments_local_vs_mainnet` | `feature` (direct, not XrplClient) | 600s | **GRACEFUL-ish** — records `local_unavailable` forever without crash; walker's other 5 pairs continue | **HARDCODED** — needs code edit |
| 12 | `ledger_definitions_walker.py:35` | ledger definitions | `server_definitions` (direct urllib) | 21600s (6h) | **CRASH** — walker exits code 1, no auto-restart, next fire in 6h. `walker_health` STALE. Downstream R5 pair records `local_unavailable`. | **HARDCODED** — needs code edit |

### 4.2 Pre-cutover code edits (BLOCKERS)

**Edit 1:** `/Users/charliebruce/xrpl_test/ledger_definitions_walker.py:35`
```python
# was:
LOCAL_RIPPLED_URL = "http://localhost:5005"
# should be:
LOCAL_RIPPLED_URL = os.environ.get("XRPL_LOCAL_NODE", "http://localhost:5005")
```

**Edit 2:** `/Users/charliebruce/xrpl_test/cross_check_walker.py:74`
```python
# was:
XRPL_LOCAL_NODE = "http://localhost:5005"
# should be:
XRPL_LOCAL_NODE = os.environ.get("XRPL_LOCAL_NODE", "http://localhost:5005")
```

Both edits are one-liners. Both preserve existing default (localhost:5005) — safe to ship BEFORE the Lenovo is ready, no behavior change until env var is set.

### 4.3 Cutover step (Phase 3)

Once Edits 1+2 are shipped AND Lenovo rippled is `full` state AND vocab parity confirmed:

```bash
# On Lenovo (or wherever xrpldashboard runs — currently Mac):
echo 'export XRPL_LOCAL_NODE=http://<lenovo-lan-ip>:5005' >> ~/.config/xrpldashboard/env
```

Then restart walker agents (they re-read env on start). All 12 consumers repoint atomically. `walker_node_fallback` table will confirm zero fallbacks to public nodes if repoint worked.

### 4.4 Non-migrating processes

- `com.charliebruce.rippled` LaunchAgent — **decommissioned after cutover**, do NOT copy plist to Lenovo (Lenovo uses systemd)
- 29 walker LaunchAgents — stay on Mac (per `M4=everything else, Lenovo=node only` split, memory: `project_xrpldashboard_m4_stays_workload_split.md`)
- `xrpl_stream.py` (public s2.ripple.com WS) — unchanged, stays on Mac
- 15+ walkers already pointing at public nodes — unchanged, no repoint needed

---

## 5. Chronic warning sweep

Log file: `/Users/charliebruce/rippled-data/logs/debug.log` (369 MB active, 7 rotated files back to Jun 19)

| Class | Count | Sample | Verdict |
|---|---|---|---|
| `Peer:WRN` "onReadMessage: Connection reset by peer" | 369,273 | normal peer churn | benign-and-known |
| `ManifestCache:WRN` "Manifest: Revoked; Seq: 4294967295" | 455,181 | canonical revocation seq — expected from peers | benign-and-known |
| `Validations:WRN` "Need validated ledger for preferred ledger analysis" | 1,046,523 | **directly correlated** with `connected` state + 1 peer; fires when validation arrives but local store doesn't have the ledger | **needs-monitoring-on-Lenovo** — should drop to near-zero if Lenovo achieves `full` + healthy peer count. If persists >10K/day, investigate peer connectivity. |
| `LoadMonitor:WRN` "Job: sweep run: 4950ms" etc | 593,894 | **still firing right now** (03:03-03:05 UTC) — chronic IO/CPU contention | **needs-monitoring-on-Lenovo** — should be rare on dedicated 32GB box with SSD. If similar frequency, investigate disk I/O. |
| `ValidatorList:WRN` + `NetworkOPs:WRN` + `Protocol:WRN` | 7,605 | startup transients | benign-and-known |
| Amendment DBG (log level echo) | 98 | startup scan | benign-and-known |
| `fetch.*fail` / `fetch.*error` | 0 | — | clean |
| `file descriptor` / `EMFILE` | 0 | — | clean (see §5.1) |
| `online_delete` / `full.history` | 0 | — | clean |
| `load_factor` / `rate.limit` | 0 | — | clean |
| Actual `FATAL` | 0 | (initial 949K count was substring false-positive matching "Warn" in validator pubkey strings) | clean |

### 5.1 FD snapshot

- Current FDs (PID 28264): **44**
- `launchctl limit maxfiles` soft: 256 / hard: unlimited
- Usage: 17% of soft — 83% headroom right now
- **BUT:** only 1 peer connected. At ~20 peers (default max), FDs would climb to ~104 — still under 256 but only ~60% headroom
- Prior FD-exhaustion (57ac3cd telemetry) is NOT recurring in current log window

**Lenovo action (before rippled first start):** raise fd limit to 65536 via systemd unit `LimitNOFILE=65536`. Never rely on Ubuntu's default 1024.

### 5.2 State-of-health snapshot

- Uptime 39.6h, time in `full`: **91.8 seconds** total (1,854 transitions in and out of `full`)
- `jq_trans_overflow: 50959` cumulative
- Suggests chronic IO or CPU contention on the Mac — corroborates `LoadMonitor:WRN` firing pattern
- Lenovo with dedicated CPU, dedicated SSD, no co-tenancy should trivially stay `full` — this is the migration's headline expected win

---

## 6. Draft Lenovo `rippled.cfg` (with provenance comments)

**Assumed paths on Lenovo Ubuntu 24.04:**
- Config: `/etc/rippled/rippled.cfg`
- Data: `/var/lib/rippled/db/`
- Logs: `/var/log/rippled/debug.log` (Ubuntu logrotate)
- Binary: `/usr/local/bin/xrpld` (built from source, **3.3.0-rc1** — see Post-Phase-1 Amendment). **Binary-name rename ratified 2026-08-01 20:34 EDT** — 3.3.0+ upstream ships as `xrpld`, matching XRPLF's post-rename branding. A `rippled → xrpld` symlink is installed alongside so any inherited script / doc / cfg path that still names `rippled` continues to resolve; the systemd unit and cfg paths keep the `rippled.cfg` / `rippled.service` filenames (config namespace is stable, only the executable moved).
- Systemd unit: `/etc/systemd/system/rippled.service` (with `LimitNOFILE=65536`, `ExecStart=/usr/local/bin/xrpld ...`)

**Ratified history depth:** Option A — 10,000 ledgers (see §3.4 ratification).

```ini
# ================================================================
# Lenovo rippled.cfg
# Generated: 2026-07-31 via docs/NODE_AUDIT_PRE_MIGRATION.md
# Migration source: Mac rippled 3.2.0 @ ~/.config/rippled/rippled.cfg
# Every setting labelled: DELIBERATE / DEFAULT / LENOVO-NEW
# ================================================================

[server]
port_rpc_admin_local
port_peer
port_ws_admin_local
# port_ws_public — DELIBERATE: no public API surface, dashboard-only

[port_rpc_admin_local]
port = 5005                                   # DEFAULT
ip = 127.0.0.1                                # DEFAULT — loopback only for RPC
admin = 127.0.0.1                             # DEFAULT
protocol = http                               # DEFAULT — loopback unencrypted OK
# NOTE: for cross-machine RPC from Mac walkers → Lenovo node:
# either (a) SSH tunnel from Mac (recommended, safest), or
# (b) bind ip = 0.0.0.0 + admin = 192.168.x.y (Mac's LAN IP) + LAN firewall.
# Option (a) preferred — no port exposed on LAN.

[port_peer]
port = 51235                                  # DEFAULT
ip = 0.0.0.0                                  # DEFAULT — accept inbound peers
protocol = peer                               # DEFAULT

[port_ws_admin_local]
port = 6006                                   # DEFAULT
ip = 127.0.0.1                                # DEFAULT
admin = 127.0.0.1                             # DEFAULT
protocol = ws                                 # DEFAULT
# NOTE: no send_queue_limit — DROPPED Mac's WORKAROUND value of 500.
# 29-walker contention doesn't exist on this box. Default 100 is fine.

[node_db]
type = NuDB                                   # DELIBERATE — correct for non-validator + SSD
path = /var/lib/rippled/db/nudb               # LENOVO-NEW path (dedicated LV, ext4)
online_delete = 10000                         # DELIBERATE — Option A ratified 2026-08-01, ~13.5h retention (mirrors Mac)
advisory_delete = 0                           # DEFAULT — auto-delete
cache_size = 16384                            # LENOVO-NEW — Mac omitted this as RAM workaround; add on 32GB box
cache_age = 5                                 # LENOVO-NEW — same rationale

[database_path]
/var/lib/rippled/db                           # LENOVO-NEW path

[debug_logfile]
/var/log/rippled/debug.log                    # LENOVO-NEW path — Ubuntu logrotate handles rotation

[node_size]
huge                                          # LENOVO-NEW — was 'medium' on Mac (WORKAROUND); 32GB dedicated → huge

[ledger_history]
10000                                         # DELIBERATE — matches online_delete (Option A ratified)

[fetch_depth]
10000                                         # DELIBERATE — matches ledger_history (Option A ratified)

[peers_max]
50                                            # LENOVO-NEW — Mac was absent (default ~21); raise for network citizenship + sync resilience

[validators_file]
validators.txt                                # DEFAULT — resolves to /etc/rippled/validators.txt

[rpc_startup]
{ "command": "log_level", "severity": "info" } # LENOVO-NEW — was 'warning' on Mac (disk-savings WORKAROUND); 'info' on dedicated box for visibility

[ssl_verify]
1                                             # DEFAULT

# [ssl_verify_file] — INTENTIONALLY OMITTED.
# Mac had /etc/ssl/cert.pem WORKAROUND (macOS OpenSSL didn't find system CAs).
# Ubuntu OpenSSL should find /etc/ssl/certs/ca-certificates.crt automatically.
# If validator list HTTPS fetches fail on Lenovo, add:
#   [ssl_verify_file]
#   /etc/ssl/certs/ca-certificates.crt
```

**Companion `validators.txt` on Lenovo (copy verbatim from Mac):**

```ini
[validator_list_sites]
https://vl.ripple.com
https://unl.xrplf.org

[validator_list_keys]
ED2677ABFFD1B33AC6FBC3062B71F1E8397C1505E1C42C64D11AD1B28FF73F4734
ED42AEC58B701EEBB77356FFFEC26F83C1F0407263530F068C7C73D392C7E06FD1

[validator_list_threshold]
0
```

**Companion systemd unit (`/etc/systemd/system/rippled.service`):**

```ini
[Unit]
Description=Rippled node (XRPL tracking, Lenovo IdeaCentre Mini)
Documentation=https://xrpl.org/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rippled
Group=rippled
ExecStart=/usr/local/bin/xrpld --conf /etc/rippled/rippled.cfg
Restart=on-failure
RestartSec=10
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

**Unit-name note:** the systemd unit filename stays `rippled.service` (config namespace unchanged; every runbook that says `systemctl status rippled` continues to work). Only `ExecStart` names the renamed binary. The `rippled → xrpld` symlink in `/usr/local/bin/` is belt-plus-braces for any inherited invocation path.

---

### 6.1 Deploy path — canonical materialization

`deploy/lenovo/` in this repo (added 743c757, comment-placement patch 657fc8e) holds the versioned, curl-able files that get copied to the Lenovo verbatim. The block above stays the reasoning-of-record; the files below are the executable form:

- `deploy/lenovo/rippled.cfg` → `/etc/rippled/rippled.cfg`
- `deploy/lenovo/validators.txt` → `/etc/rippled/validators.txt`
- `deploy/lenovo/rippled.service` → `/etc/systemd/system/rippled.service`

**Deploy order (one shot, root on Lenovo):**

1. `useradd -r -s /usr/sbin/nologin rippled` (system user; no login shell)
2. Create the dedicated LV + mount at `/var/lib/rippled` per Post-Phase-1 Amendment §5; `chown -R rippled:rippled /var/lib/rippled`
3. `install -d -o rippled -g rippled /var/log/rippled` (logrotate picks up automatically once file exists)
4. `install -d -m 755 /etc/rippled` and copy `deploy/lenovo/rippled.cfg` + `deploy/lenovo/validators.txt` in
5. `install -m 644 deploy/lenovo/rippled.service /etc/systemd/system/rippled.service`
6. `install -m 755 <built-binary> /usr/local/bin/xrpld && ln -s xrpld /usr/local/bin/rippled`
7. `systemctl daemon-reload && systemctl enable --now rippled.service`
8. Tail `/var/log/rippled/debug.log`; watch `server_info` progress `disconnected → connected → syncing → full`

**Any cfg tweak amends this doc AND `deploy/lenovo/` in one commit** — the doc is the reasoning-of-record, the files are the deployable materialization; they never fork.

---

### 6.2 SHAMapStore silent-first-fire observation (3.3.0-rc1 fresh NuDB) — RESOLVED 2026-08-02

Original observation (2026-08-02 morning, Lenovo uptime ~8h 37m):

- `online_delete=10000` + `advisory_delete=0` per §6 config
- Retention window: **17,026 ledgers** (low bound `106000766` had not moved)
- SHAMapStore partition at debug level: **SILENT** (no prune-fire log lines)
- Cfg confirmed correct — this was NOT a misconfiguration
- Hypothesis at the time: 3.3.0-rc1's SHAMapStore first-fire on a fresh NuDB takes longer than 8h and is silent until first-fire

**Resolution — 2026-08-02 16:41 UTC read:**

- Retention: **14,506 ledgers** (`complete_ledgers 106009952-106024458`) — ~1.45× the 10K target, bounded
- LV usage: 21% of the dedicated `/var/lib/rippled` LV (~20G / 98G, ~74G free)
- SHAMapStore first-fired sometime between the ~8h37m morning read and the 16:41 UTC afternoon read
- Hypothesis confirmed: **silent-until-first-fire on fresh NuDB is 3.3.0-rc1 expected behavior**

**Adjacent observation — VERDICTED benign:** rippled uptime at the 16:41 UTC read was 6h 6m (start 10:35 UTC), while box uptime was ~20h. `journalctl -u rippled` shows **two clean SIGTERM cycles**: 10:29:14 UTC (signal 15 → clean stop → systemd restart, PID 146390 → 188864) and 10:34:40 UTC (signal 15 again → clean stop → restart, PID 188864 → 189238). Both `Deactivated successfully` → `Started` = `systemctl restart` pattern, not crash. No segfault, no abort, no SIGKILL / OOM. Cause: morning ops touch (config edit, verification, or unattended-upgrades package refresh). **Not SHAMapStore-triggered** — retention-advance timing was coincident, not causal.

**Escalation trigger UNPINNED** — no longer needed. crontab prune-watch retained for continuous confirmation; log at `/home/charlie/prune_watch.log` (crontab was installed today after the last :00 slot, so first fire is 20:00 UTC / 16:00 EDT — populates tonight).

---

### 6.3 Network pinning — static IP via netplan (not router reservation)

Pinned 2026-08-02 17:03 UTC.

**Why not a router DHCP reservation:** the household's Evolution Digital ISP gateway exposes no local web UI. Ports 80, 443, 8080, 8443, 8888, 4433, 81, 8081 all closed on `192.168.40.1` from the LAN side. This is an ISP-locked cable gateway; admin is app-only or ISP-portal-only. No usable reservation surface.

**Chosen path:** static IP on the server itself.

- File: `/etc/netplan/50-cloud-init.yaml`
- `dhcp4: false` on `enp46s0`
- Address: `192.168.40.95/24`
- Default route via `192.168.40.1`
- DNS: `1.1.1.1`, `8.8.8.8`
- `chmod 600` (netplan requires; suppresses the `netplan apply` permissions warning)

**Cloud-init lockdown** — `/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg` contains `network: {config: disabled}` so cloud-init does not re-render `50-cloud-init.yaml` back to DHCP on subsequent boots.

**Why this is preferable to a router reservation regardless:** the server owns its address independent of router state. Survives router reboot, replacement, ISP swap, or DHCP pool change. Standard practice for a headless node anyway.

**Verified:** `netplan apply` did not drop the live SSH session (same-subnet same-IP switchover is seamless). Post-apply from-Mac `ssh charlie@192.168.40.95 "hostname -I"` returned `192.168.40.95`.

**Reboot-proof-test still owed:** static-IP config was applied but not reboot-verified. Any future reboot of the Lenovo (e.g. kernel update, hardware move) is the real test. If SSH does not come back at `.95` post-reboot, the netplan config is malformed and DHCP-fallback did not happen — recovery is either console access or DHCP-lease-scan from the router side (which has no UI, so console access is the fallback plan).

---

### 6.4 Nix build environment (Determinate Nix, nix-daemon)

Determinate Nix (multi-user, `nix-daemon.service`) was installed 2026-08-01 to provide the GCC 15 toolchain required by rippled 3.3.0-rc1's C++23 build, after Ubuntu 24.04's stock compilers (GCC 13/14) proved insufficient. Build shape: `nix develop` inside the `~/rippled` checkout enters the upstream dev shell (flake-provided compiler + Conan), and the CMake build runs inside it, producing the binary at `~/rippled/.build/build/Release/xrpld`. That artifact was then installed to `/usr/local/bin/xrpld` (with `rippled` symlink) per §6 — the running node has zero Nix involvement at runtime. Disposition: keep the daemon; future rippled upgrades re-enter via the same `nix develop` path, and the install step (§6.1 order, step 6) carries the new binary out of the build tree.

---

## Charlie's decisions gate — RESOLVED 2026-08-01

All four ratified pre-Phase-2:

1. **History depth** — Option A (10K, ~13.5h, ~25GB). See Post-Phase-1 Amendment + §3.4 ratification.
2. **WORKAROUND-class settings:**
   - `send_queue_limit=500` on WS port → **dropped** (default).
   - `[rpc_startup] log_level warning` → **raised to `info`** on Lenovo for operational visibility.
   - `[ssl_verify_file]` → **omitted**, wait for symptom. Ubuntu OpenSSL finds `/etc/ssl/certs/ca-certificates.crt` automatically.
3. **Pre-cutover code edits (§4.2)** — **ship in parallel with Phase 2 build**. Env-var pattern with `localhost:5005` default preserves current Mac behavior; env-var flip becomes Phase 3 cutover lever.
4. **rippled release target** — **`3.3.0-rc1`** (git tag 2026-07-16). Earliest public tag that closes the R5 vocab gap. Note: `3.3.0-rc6` referenced elsewhere in this doc is s1.ripple.com's internal build label, not a public git tag.

Verification riding the checkout: after `git checkout 3.3.0-rc1`, grep source for `sfMutableFlags` and `sfSponsor` before declaring R5 closed. Their presence gets proven, not inferred from tag-date arithmetic.

Everything else is JJ's execution.

---

## Appendix — file locations for future audit reruns

- Config: `/Users/charliebruce/.config/rippled/rippled.cfg` + backups
- Binary: `/Users/charliebruce/rippled/.build/rippled`
- NuDB: `/Users/charliebruce/rippled-data/db/nudb`
- SQLite: `/Users/charliebruce/rippled-data/db/`
- Logs: `/Users/charliebruce/rippled-data/logs/debug.log` + rotated `.gz`
- LaunchAgent: `~/Library/LaunchAgents/com.charliebruce.rippled.plist`
- Rebuild script: `~/xrpl_test/ops/rippled_nudb_rebuild.sh`
- xrpldashboard env: `~/.config/xrpldashboard/env`
- Cross-check monitor: `~/xrpl_test/cross_check_walker.py:74, :325, :448`
- Ledger definitions walker: `~/xrpl_test/ledger_definitions_walker.py:35`
- XrplClient library: `~/xrpl_test/xrpl_client.py:24`
