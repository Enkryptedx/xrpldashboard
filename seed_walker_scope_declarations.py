"""One-shot seeder for walker_scope_declarations — the Phase 1b escrow-
lesson enforcement table.

Every XRPL-reading walker MUST have a row here matching a walker_health
row by walker_name. The Coverage Register renders UNDECLARED (its own
alarm class) for any walker in walker_health missing from this table.

Ground-truth-reconciled 2026-07-07: walker_names below match exactly
the walker_health rows registered on the Mac node. This seeder was
initially built with pseudo-names (whales_walker, escrow_supply,
nft_activity_walker_forward) that had no walker_health counterparts —
the register's first run caught the mismatch (15 undeclared walkers,
including 3 rename-corrected below). Kept the correction record because
it validates the tool's escrow-lesson claim on its own dogfood.

Idempotent: re-run to update filter_notes after any walker scope change.

xrpl_stream is a special case — its freshness surfaces via
worker_heartbeat, not walker_health. Kept here as an editorial-only
declaration; the register renders "no walker_health row" for it, which
is correct until we extend the register to also read worker_heartbeat.
That extension is Gate 2 backlog, not Gate 1 blocker.
"""

import db


SCOPES = [
    # ── FULL: no filter ──────────────────────────────────────────────
    (
        "xrpl_stream",
        "full firehose (heartbeat-tracked, not in walker_health)",
        "WebSocket subscribe(streams=[transactions]); every validated "
        "ledger, every tx, no account or type filter. Phase 1a novelty "
        "sensor observes LedgerEntryType via meta.AffectedNodes on the "
        "same stream — zero additional RPC calls. Freshness reported via "
        "worker_heartbeat; register does not (yet) surface heartbeat-"
        "tracked walkers — Gate 2 backlog.",
        False,
    ),
    (
        "ledger_definitions_walker",
        "local rippled server_definitions",
        "server_definitions RPC to localhost:5005 only. No public "
        "fallback — a public node returns its own build's vocabulary, "
        "not ours. Sentinel `Invalid: -1` stripped from both "
        "TRANSACTION_TYPES and LEDGER_ENTRY_TYPES.",
        False,
    ),
    (
        "coverage_register_walker",
        "Phase 1b register self-writer (this walker)",
        "Reads Phase 0 (ledger_definitions), Phase 1a (tx_type_seen + "
        "ledger_entry_type_seen), coverage_labels, walker_scope_declarations, "
        "and walker_health; computes the three-way diff; appends to "
        "coverage_register_history when state differs OR >24h has "
        "elapsed. Own walker_health row so the register's own freshness "
        "is legibly stamped.",
        False,
    ),
    (
        "cross_check_walker",
        "Layer 3 external-legitimacy cross-checker (Phase 3: 6 pairs)",
        "Compares site values against independent public sources. "
        "Pairs: (1) RLUSD-ETH penny-exact vs Ankr/llamarpc eth_call totalSupply() "
        "(independent from publicnode.com primary); (2) RLUSD-XRPL penny-exact vs "
        "xrplcluster.com gateway_balances (independent from s1.ripple.com primary); "
        "(3) XRP price band ±2% vs CoinGecko simple/price; (4) amendment count-exact: "
        "localhost:5005 feature RPC enabled set vs mainnet Amendments ledger object "
        "on s1.ripple.com; (5) validator UNL count-exact live vl.ripple.com vs stored "
        "unl_snapshots; (6) ledger vocabulary set-equal: local ledger_definitions "
        "(from ledger_definitions_walker) vs s1.ripple.com server_definitions. "
        "Disagreement = investigation trigger, never auto-correction. Results land in "
        "cross_check_results (append-only); status='disagree' rows are the alarm "
        "surface. walker_health.ok = False only on internal exception. 10-min cadence. "
        "Does NOT audit itself — if this walker goes undeclared, "
        "answer_plausibility_walker's UNDECLARED_WALKER rule fires.",
        False,
    ),
    (
        "answer_plausibility_walker",
        "Layer 2 rule evaluator (Phase 1 seed: R1/R2/R4 + UNDECLARED)",
        "Reads live Neon and evaluates the rule set from "
        "docs/TRUTH_AUDIT_DESIGN.md against the metric inventory. Phase 1 "
        "seed covers R1 (flat-when-should-wiggle) on RLUSD xrpl_supply + "
        "eth_supply, R2 (zero-with-large-denominator) on RLUSD "
        "xrpl_net_change_24h, R4 (monotonic-violated) on analytics "
        "all-time human views + uniques with burst-cohort delta as "
        "accepted cause, and UNDECLARED_WALKER on any walker in "
        "walker_health missing a scope declaration. This walker audits "
        "others; it does not get to be undeclared itself. Alarms land in "
        "answer_plausibility_alarms (append-only); walker_health.ok "
        "reflects only whether the walker itself ran cleanly.",
        True,
    ),
    (
        "is_bot_writer",
        "forward-classify + backfill page_views.is_bot for all rows",
        "Reads page_views; writes page_views.is_bot. Also reads/writes "
        "page_view_classification_meta, page_view_bot_hashes, "
        "page_view_scanner_combos, burst_cohort_days. Classification "
        "logic mirrors _bot_filter_sql exactly when _bot_hash_table_ready "
        "is True. Forward pass processes rows at/after the watermark on a "
        "5-min cadence; backfill processes one day of unclassified "
        "history per run until backfill_complete=true. Reconciliation "
        "triggers on BOT_CLASSIFIER_VERSION mismatch (full resync) or "
        "burst_cohort_days max(created_at) advance (date-range resync). "
        "Full scope in intent — every page_views row eventually stamped. "
        "In-flight window: rows newer than watermark-10min may lag by "
        "one writer cycle. See scripts/is_bot_writer.py.",
        False,
    ),
    (
        "is_bot_canary",
        "Layer 3 drift audit of page_views.is_bot column vs live predicate",
        "Compares human-row counts between Path A (is_bot column filter, "
        "WHERE is_bot IS NOT TRUE) and Path B (live _bot_filter_sql "
        "subquery — the authoritative comparison arm, never deleted). "
        "Alarm on divergence outside the in-flight window (rows newer "
        "than now-10min excluded to allow writer to stamp). Two windows "
        "per daily run: (1) trailing 7 days minus in-flight; (2) "
        "deterministic rotating historical week — (ISO_week_number % 4) "
        "+ 2 weeks ago, gated on backfill_complete=true. Results land as "
        "walker_health rows so the answer_plausibility framework catches "
        "them without a new alert path. Honest partial: the 4-week "
        "rotation means 3-of-4 historical weeks are unchecked in any "
        "given run; trailing-7d covers hot data every run. Flip-to-"
        "column BLOCKED as of 2026-07-26 pending classifier-drift "
        "diagnosis (see docs/IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md).",
        True,
    ),
    (
        "nft_activity_activity",
        "forward-only from cursor toward HEAD-3 (all NFT tx types)",
        "Cursor-tracked forward-only ingest of all NFT-related tx types "
        "(NFTokenMint, NFTokenBurn, NFTokenCreateOffer, NFTokenAcceptOffer, "
        "NFTokenCancelOffer). No account filter — full firehose scoped to "
        "the NFT tx family. Uses xrpl_client.get_client (local rippled "
        "primary, public fallback). Cadence 5 min. NOTE ON NAME: the "
        "walker_health row is 'nft_activity_activity' by design — the "
        "walker source builds `walker_health_name = f\"{WALKER_NAME}_"
        "{args.mode}\"` where WALKER_NAME='nft_activity' and args.mode "
        "is 'activity' or 'backfill' (nft_activity_walker.py:560). Reads "
        "as a typo but is the intentional mode-suffix output.",
        False,
    ),
    (
        "rank_amms",
        "every AMM in amm_index.json ranked by TVL",
        "AMMInfo RPC per indexed AMM. amm_index.json comes from the "
        "bootstrap full-ledger scan; new AMMs surface here after the "
        "next bootstrap or via xrpl_stream's amm_create_seen delta. No "
        "account or asset-pair filter — the ranking is the full known "
        "AMM population.",
        False,
    ),

    # ── PARTIAL: declared filters ────────────────────────────────────
    (
        "credentials_walker",
        "curated 14-account institutional issuer seed",
        "Fixed 14-account seed defined in the walker source (institutional "
        "credential issuers). Credentials issued by non-seeded accounts "
        "are absent from /credentials. Seed audit + consolidation with "
        "permissioned_domains_walker parked in backlog.",
        True,
    ),
    (
        "permissioned_domains_walker",
        "curated 14-account institutional issuer seed",
        "Shares credentials_walker's hardcoded 14-account seed. "
        "Permissioned domains outside the seed are invisible to "
        "/credentials PD strip. Consolidation to a single source parked "
        "in backlog.",
        True,
    ),
    (
        "escrow_walker",
        "category='ripple' in named_accounts.json minus RLUSD issuer",
        "AccountObjects(type='escrow') for the Ripple monthly-release "
        "cohort defined by category='ripple' in named_accounts.json, "
        "with the RLUSD issuer excluded. ~102 escrow objects per pass — "
        "Ripple's monthly-release program only, NOT the full XRPL escrow "
        "universe. The 2026-07-12 full-ledger census found 404 TokenEscrow "
        "objects total; that fuller walk is in flight and will supersede "
        "this cohort-only scope. Until then, new Ripple-tagged escrows "
        "outside this seed and all non-Ripple escrows are invisible to "
        "/cold-storage's browser and calendar. This IS the original "
        "escrow lesson — declaring it here is the fix.",
        True,
    ),
    (
        "amendment_majority_walker",
        "Amendments ledger object (Majorities) on our own node, per flag "
        "ledger",
        "Reads the Amendments ledger object from our own node every 15 min "
        "and records gained/lost/regained majority transitions into "
        "amendment_majority_history, keyed by (amendment_hash, "
        "Majority.CloseTime). Built 2026-09-25 after five outlets printed "
        "an Oct-5 activation for PermissionDelegationV1_1 that reset to "
        "Oct-8 with no first-party record. Scope is the Majorities array "
        "ONLY: an amendment that never takes a majority, or one enabled "
        "inside a single flag interval, is invisible here. Enable points "
        "are resolved by walking back flag ledgers for the EnableAmendment "
        "pseudo-tx, so a tx older than MAX_FLAG_LEDGERS_BACK falls back to "
        "the first ledger we OBSERVED the hash in, which is later than the "
        "true enable time and is labelled as observed on /amendments. "
        "Declared 2026-11-02 alongside the walker_health wiring: it "
        "previously wrote no walker_health row, so l1_pager could not see "
        "126 statement-timeout aborts.",
        True,
    ),
    # ── Batch 2026-11-02: the 46 walker_health rows that had no declaration ──
    # Each scope below was read from that walker's own source, not inferred
    # from its name. Two names needed resolving first: plist_drift_canary
    # lives in ~/xrpl_test_private_infra (the in-repo wrapper execs it), and
    # `new_accounts` is written by new_accounts_walker.py -- a name/file
    # mismatch. RETIRED/PAUSED rows exist to stop the UNDECLARED alarm
    # without deleting any walker_health row or history.
    (
        "ai_crawler_daily_rollup",
        "ai_crawler_hits aggregated by (ua_class, page_family, day), "
        "self-probes excluded",
        "Rolls ai_crawler_hits into ai_crawler_daily_rollup for the public "
        "/observatory page, which serves ONE-WEEK-DELAYED weekly views. "
        "page_family is a small CLOSED vocabulary, so a new URL shape lands "
        "in the catch-all bucket until the classifier is extended -- that "
        "traffic is counted but not distinguishable. Self-probes are "
        "excluded by design, so our own canary hits never inflate the feed.",
        True,
    ),
    (
        "amendment_tally_reconstruct",
        "past dates NOT covered by our signed leaves, answered by the "
        "Validator History Service",
        "Backfills per-amendment vote tallies for dates our signed daily "
        "leaves never covered, by asking VHS what it can answer NOW about "
        "that date. Rows are stored source='vhs_reconstructed' and are "
        "NEVER signed -- a reconstruction is a third-party recollection, not "
        "a first-party observation, and the two must stay distinguishable. "
        "Invisible: anything VHS itself cannot answer for that date.",
        True,
    ),
    (
        "auto_pull_mac",
        "ff-only git pull of ~/xrpl_test on the Mac, daily 05:20 local",
        "Not an XRPL reader: it keeps the Mac checkout level with origin/main "
        "so the launchd fleet stops running stale code. For the Mac the pull "
        "IS the deploy, because launchd reads the tree each tick. ff-only on "
        "purpose -- it refuses rather than merging or discarding when the "
        "tree has diverged, which means a dirty working tree (token_names "
        ".json is chronically dirty here) leaves the host behind until a "
        "human intervenes. The Lenovo has NO equivalent job.",
        True,
    ),
    (
        "changes_walker",
        "diff of today's signed snapshot vs yesterday's, after the 01:00 UTC "
        "cycles land",
        "Computes the /changes envelope and writes changes/YYYY-MM-DD.json. "
        "Deliberately scheduled AFTER the 01:00 UTC signed_snapshot and "
        "registry-snapshot cycles so today's row is already in Postgres when "
        "the diff runs. If either upstream cycle is late, this walker diffs "
        "against whatever is present and the day's entry is thin rather than "
        "absent -- the feed never blocks on an upstream miss.",
        True,
    ),
    (
        "claim_verify_sampler_walker",
        "SAMPLE of 10 public claims per weekly run, not the full claim set",
        "Weekly re-check of 10 public claims against their primary sources: "
        "3 warnings from the visitor-facing feed, 3 tier=verified rows with "
        "source='two_way_toml', and the remainder spread across other "
        "surfaces. This is a SAMPLER: the site makes far more than 10 "
        "claims, so a false claim outside the week's draw stays unchecked "
        "until it is sampled. Coverage is probabilistic by design, and the "
        "honest reading is 'spot-checked weekly', never 'all claims "
        "verified'.",
        True,
    ),
    (
        "cold_storage_walker",
        "category='ripple' in named_accounts.json, account_info balances",
        "Snapshots liquid balances for the tracked cohort via "
        "account_info(ledger_index=validated) through xrpl_client.get_client "
        "(Mac LOCAL_NODE = LAN Lenovo rippled), upserting cold_storage_"
        "snapshot every 15 min. /cold-storage reads the table, never the "
        "node. Same cohort-only blind spot as escrow_walker: exchange cold "
        "wallets and foundation reserves are NOT tracked, and the page says "
        "so. Note this one does NOT exclude the RLUSD issuer, unlike "
        "escrow_supply/escrow_walker -- it reports balances, not escrows.",
        True,
    ),
    (
        "daemon_log_capper",
        "launchd_logs/*.err.log and *.out.log, truncated IN PLACE",
        "Not an XRPL reader: size-caps the launchd logs that must NOT be "
        "rename-rotated, by truncating in place so the inode survives and "
        "the writing process keeps its file handle. newsyslog's rename+gzip "
        "would leave long-lived walkers writing into an unlinked inode, "
        "silently losing every subsequent line. Scope is the log tree only; "
        "it makes no claim about log CONTENT.",
        True,
    ),
    (
        "dockvault_mirror_freshness_canary",
        "last_ok state files of the two DockVault mirror jobs",
        "Reads the last_ok timestamp files for both mirror jobs and raises "
        "findings_count>0 when either is stale. It checks FRESHNESS, not "
        "correctness: a mirror that runs on schedule while copying the wrong "
        "tree, or copying a damaged source faithfully, passes this canary. "
        "Built after two days of silent mirror failure on the only backup of "
        "the memory + deployment files.",
        True,
    ),
    (
        "geoip_db_refresh",
        "one weekly MaxMind GeoLite2-City download into geoip_db_blob",
        "The ONE place that talks to MaxMind on a schedule. Downloads "
        "GeoLite2-City weekly, gzips it, and upserts it into Postgres "
        "geoip_db_blob; every Render container and Mac walker then boots from "
        "our store, so a deploy costs zero MaxMind downloads. Built after the "
        "'Daily GeoIP Database Download Limit Reached' incident. Invisible: "
        "geo accuracy itself -- it guarantees we HAVE a database, not that "
        "MaxMind's answers are right.",
        True,
    ),
    (
        "geoip_health_canary",
        "/healthz geoip.available plus region_code liveness, 15-min",
        "Checks that state-level geo lookup is actually live: GET /healthz "
        "and require geoip.available true. Exists because MaxMind's download "
        "limit left /healthz.geoip disabled and page_views.region_code NULL "
        "for every visit from 13:00Z while every walker stayed green -- "
        "nothing measured 'is region live'. Measures availability, not "
        "correctness of any specific lookup.",
        True,
    ),
    (
        "homepage_summary_walker",
        "precomputed homepage aggregates, all 10 locales, 5-min",
        "Pre-renders the PG + XRPL summaries the homepage used to aggregate "
        "at request time (~4.8s cold). Writes one summary row per locale so "
        "the route reads instead of computes. Scope is the homepage's own "
        "aggregates only; a locale that fails to render leaves the previous "
        "row in place, so the page can serve slightly stale copy rather than "
        "blocking -- the message reports rendered=N/10 so a partial pass is "
        "visible.",
        True,
    ),
    (
        "jj_ro_canary",
        "the jj_ro read-only Postgres role's connect+select path",
        "Exercises the jj_ro read-only DB path end to end. Built after that "
        "path broke with 'SASL authentication failed' some time after 06:10 "
        "ET 2026-10-30 and silently recovered by 06:06 ET 2026-10-31 -- "
        "nobody could say when it broke or returned, because nothing probed "
        "it. Covers the read-only role only; it says nothing about the "
        "writer path the walkers use.",
        True,
    ),
    (
        "mac_tunnel_health_canary",
        "Mac cloudflared connector for tunnel xrpld-mac (sig.xrpldashboard "
        ".com -> 127.0.0.1:8842)",
        "Watches the Mac-side cloudflared connector that carries "
        "sig.xrpldashboard.com to the local sig-service. A Mac-side outage "
        "silently drops sig-receipt signing, which is why this mirrors the "
        "drift-guard coverage the Lenovo MCP connector already had. Scope is "
        "this ONE tunnel; other connectors on either host are out of scope.",
        True,
    ),
    (
        "mcp_connect_redirect",
        "clicks on the nine allowlisted MCP directory redirect slugs",
        "Stamps a walker_health row with ref=<slug> BEFORE issuing the 302, "
        "so a click that never completes the MCP handshake (agent fetched "
        "the URL then closed) is still counted at the directory layer. This "
        "counts ARRIVALS AT THE DOOR; mcp_session_start counts sessions that "
        "actually call a tool, and the two numbers are expected to differ. "
        "The slug allowlist is a CLOSED set of nine directories, so traffic "
        "from anywhere else is invisible here. 30-day cadence means a long "
        "quiet gap is normal, not a fault.",
        True,
    ),
    (
        "mcp_session_start",
        "one stamp per MCP session, bound to the ?ref=<slug> that opened it",
        "Each MCP session is stamped ONCE with the referring directory slug, "
        "so revenue reads can count distinct sessions per directory. "
        "Idempotent per session_key: the first call captures the ref and "
        "writes exactly one row. The binding map lives IN-PROCESS, so a "
        "restart wipes counters and starts a fresh count -- same operational "
        "model as the rate-limit ceiling. Sessions that arrive without a ref "
        "are not attributed.",
        True,
    ),
    (
        "mcp_ua_matrix_canary",
        "a fixed 16-client-class User-Agent matrix against the MCP endpoint",
        "Replays the same 16 UA classes used in the 2026-08-26 four-AI audit "
        "that proved the Cloudflare 1010 discrimination, and alarms if a UA "
        "that should be allowed comes back with the 'browser signature "
        "banned' body. The matrix is FIXED: a client class outside those 16 "
        "could be blocked without this canary noticing.",
        True,
    ),
    (
        "memory_index_size_guard",
        "byte and line size of MEMORY.md against the auto-memory read window",
        "Hourly check that the auto-memory root index stays inside the "
        "loader's limits: a hard byte cap (~25000 bytes) and a soft line "
        "guideline (~200 lines). Past either, content is silently dropped "
        "from the window -- the file still exists, it just stops being read. "
        "Guards SIZE only; it makes no claim about whether the content is "
        "current or correct.",
        True,
    ),
    (
        "nav_dropdown_screenshot_canary",
        "pixel checksum of the Live-group nav dropdown REGION on 3 pages, "
        "weekly",
        "Loads 3 pages headless, opens the Live-group nav dropdown, and "
        "compares the dropdown region's pixel checksum against last week's. "
        "Built after the /observatory transparent-dropdown bug, which no "
        "HTML-level test could see. Scope is that one region on those 3 "
        "pages: a visual regression anywhere else on the site is invisible, "
        "and an intentional redesign trips it by design.",
        True,
    ),
    (
        "new_accounts",
        "validated transactions whose metadata CREATES an AccountRoot, "
        "forward-only",
        "Forward-only ingest of XRPL account creations, detected from "
        "metadata that creates an AccountRoot node. Written by "
        "new_accounts_walker.py -- the walker_health name and the filename "
        "differ. v1 records COUNT only; the funder is stored in the row for "
        "a future concentration view but is NOT surfaced. Forward-only means "
        "history before the cursor's start is permanently absent unless "
        "backfilled.",
        True,
    ),
    (
        "nft_activity_summary",
        "nft_activity_walker --mode summary: /nfts funnel, active/quiet, "
        "churn badge",
        "One of five modes of nft_activity_walker, dispatched via --mode. "
        "Precomputes the /nfts funnel, the active/quiet split and the churn "
        "badge into summary rows so the route reads instead of aggregating. "
        "Reads what --mode activity has already ingested, so its numbers are "
        "only as complete as that forward-only cursor -- a lagging ingest "
        "shows as a lagging summary, not as an error.",
        True,
    ),
    (
        "pip_audit_walker",
        "requirements.txt AND the installed set of every venv that runs our "
        "code, weekly",
        "Weekly Python dependency vulnerability scan: pip-audit against "
        "requirements.txt AND against `pip freeze` for each venv (main, "
        "py311, lenovo). Auditing only requirements.txt was the lesson filed "
        "2026-10-27 -- a pinned file says nothing about what is actually "
        "INSTALLED. Findings surface through walker_health so the L1 pager "
        "sees them; WALKER_MESSAGE_MUTES in tools/l1_pager.py is the "
        "acknowledged-CVE escape valve. Weekly cadence means a fresh "
        "advisory can sit up to 7 days before being seen.",
        True,
    ),
    (
        "plist_drift_canary",
        "~/Library/LaunchAgents/com.charliebruce.xrpldashboard.*.plist vs "
        "private_infra/launchd",
        "Compares the LIVE plist tree (what launchd actually loads) against "
        "the TRACKED copies in ~/xrpl_test_private_infra/launchd, plus the "
        "wrappers those plists exec and the Lenovo deploy units. private_"
        "infra is their ONLY version control, because xrpldashboard's "
        ".gitignore:139 ignores launchd/ wholesale. Source lives in "
        "private_infra, not this repo -- the in-repo wrapper execs it. "
        "Detects drift and untracked units; it does not validate that a "
        "plist's schedule is sensible.",
        True,
    ),
    (
        "public_route_200_canary",
        "the fixed ROUTES list in public_route_200_canary.py, every 15 min",
        "GETs every public surface and requires 200 with a sane body size. "
        "Built after /methodology served 500 undetected for ~7 days because "
        "nothing GET'd the route after deploys. The ROUTES list is "
        "MANUALLY kept in sync with PUBLIC_ROUTES in app.py, so a new route "
        "that nobody adds here is never probed -- that hand-sync is the "
        "standing weakness. Byte thresholds catch an empty-shell 200 but "
        "not wrong CONTENT.",
        True,
    ),
    (
        "refresh_ofac_sdn",
        "the digital-currency-address subset of the OFAC SDN list",
        "Downloads the OFAC SDN list and extracts only the digital-currency-"
        "address entries into a local JSON snapshot that check_data.py's "
        "_signal_ofac_sdn_match consumes. Sanctioned parties with no listed "
        "crypto address are therefore NOT in our snapshot, and a match here "
        "means 'this address is on the list', never 'this counterparty is "
        "clean'. Commits the refreshed JSON straight to main from the Mac, "
        "so it moves origin/main without a PR.",
        True,
    ),
    (
        "rippled_cfg_drift_guard",
        "three port stanzas in Lenovo's /etc/rippled/rippled.cfg",
        "Verifies the ip/admin lines of [port_rpc_admin_local] "
        "(127.0.0.1/127.0.0.1), [port_rpc_public_lan] (192.168.40.95, NO "
        "admin key) and the third stanza stay sovereign-safe. Any deviation "
        "raises findings_count. Scope is those stanzas: other rippled.cfg "
        "sections, and the running process's actual bound ports, are not "
        "checked -- it reads the FILE.",
        True,
    ),
    (
        "roll_call_recorder",
        "UNL validator amendment yes-votes on VOTING ledgers "
        "(index % 256 == 255)",
        "Long-running Lenovo unit subscribed to the validations + ledger "
        "streams on our OWN rippled's loopback WebSocket (no new LAN port). "
        "Collects the amendment yes-votes UNL validators attach to their "
        "validations of each VOTING ledger. Only voting ledgers carry these "
        "votes, so the record is inherently once-per-256-ledgers, and a "
        "validator outside our UNL is not counted.",
        True,
    ),
    (
        "route_5xx_rate_walker",
        "rolling 1h 5xx percentage per route, from page_views",
        "Queries page_views every 15 min for each route's rolling 1-hour "
        "5xx rate and pages when any route exceeds 5%. Built after the "
        "/registry/taxonomy incident to give a proactive signal on ANY "
        "route rather than only the ones a canary names. Derived from our "
        "own request log, so a failure that never reaches the app (edge "
        "error, DNS, Render 502 before the worker) is invisible here.",
        True,
    ),
    (
        "rwa_supply_nav_walker",
        "verified rwa_family rows with a NAV source in rwa_nav_sources.yaml",
        "Daily on-ledger RWA supply multiplied by the cited NAV for each "
        "verified family that has an entry in rwa_nav_sources.yaml naming "
        "its NAV source URL, publish cadence and XRPL issuer. A family "
        "without a yaml entry contributes NOTHING -- the figure is an honest "
        "floor, not a market total, and NAV freshness is bounded by each "
        "issuer's own publish cadence.",
        True,
    ),
    (
        "signed_verified_tokens",
        "every (ticker, currency_hex, canonical_issuer) with a verified "
        "provenance claim",
        "Hourly Ed25519-signed manifest of tickers whose canonical issuer we "
        "have verified, served at /.well-known/verified-tokens.json "
        "(PG-first, disk fallback). Signed with the receipt key, NOT the "
        "snapshot key. Absence from the manifest means 'we have not verified "
        "it', never 'it is fraudulent' -- the manifest is an allowlist of "
        "confirmed provenance, not a denylist.",
        True,
    ),
    (
        "site_totals_walker",
        "all-time aggregates over page_views -> site_totals (id=1)",
        "Daily single source of truth for 'how many countries' style "
        "questions: reads page_views, upserts one row into site_totals, and "
        "mirrors the payload to docs/SITE_TOTALS.json for grep-friendly "
        "use. Idempotent. Bounded by page_views retention and by the bot "
        "classification applied there -- totals are human-traffic totals "
        "under whatever filters page_views carries, not raw hit counts.",
        True,
    ),
    (
        "supply_fast_walker",
        "ONE ledger(validated) request every 60s -> xrp_supply_block "
        "total_* columns",
        "Half of a deliberate pair: this one makes a single ledger request "
        "each minute and upserts only the total_* columns of the "
        "xrp_supply_block singleton. burned_since_genesis is derived at "
        "render time from total_xrp, so it is never stored. The other half "
        "of the row (the slower escrow/reserve side) is owned by a different "
        "walker -- this one must not be read as the whole supply picture.",
        True,
    ),
    (
        "tier_reverify_walker",
        "token_category_current rows with source='two_way_toml', daily",
        "Re-runs the two-way TOML check on every row claiming "
        "source='two_way_toml', with a TWO-WEEK grace period before any "
        "downgrade, so a transient TOML fetch failure cannot strip a token's "
        "verified tier. Scope is two_way_toml rows only: tiers set by other "
        "provenance routes are not re-checked here, and the grace window "
        "means a genuinely revoked claim stays verified for up to 14 days.",
        True,
    ),
    (
        "token_icon_walker",
        "icon URLs in [[TOKENS]] of verified / self-described issuer TOMLs",
        "Hourly emblem fetcher: reads the icon URL from each verified or "
        "self-described issuer's TOML [[TOKENS]] entry, fetches with a "
        "256 KB cap and short timeout, and rasterizes. Only issuers that "
        "publish an icon in their TOML are covered, so a missing emblem "
        "usually means the issuer never declared one -- not that we failed "
        "to fetch it.",
        True,
    ),
    (
        "token_issuer_flags_walker",
        "DISTINCT issuers in token_volume, AccountInfo(signer_lists=True)",
        "Every 30 min, enumerates distinct issuers from token_volume and "
        "fetches AccountInfo with signer lists to back the 'Ledger-level "
        "capabilities' panel on /token/<currency>/<issuer>. Scope follows "
        "token_volume: an issuer with no recorded volume has no flags row, "
        "so a brand-new or dormant issuer's panel is empty rather than "
        "wrong.",
        True,
    ),
    (
        "tokens_summary_walker",
        "precomputed /tokens inputs (aggregates 24h/7d/all, 50-row ranked "
        "lists)",
        "Every 300s, computes /tokens' heavy inputs into a single row using "
        "the SAME app.py functions the route used to call live, so the "
        "precomputed answer cannot drift from the on-demand one. Ranked "
        "lists are capped at 50 rows each: anything below the cut is absent "
        "from the summary, and the route falls back to live compute only if "
        "the row is missing.",
        True,
    ),
    (
        "wallet_summary_walker",
        "pre-rendered top-20 wallets, 5-min",
        "Pre-renders the top-20 wallet views, whose cold render was ~8s "
        "because the adaptive-cap fetch pulls up to 20,000 tx envelopes from "
        "the LAN node -- past the 4s label-agreement canary timeout. Scope "
        "is the top-20 only: any other wallet still cold-renders live, so "
        "this walker's health says nothing about those.",
        True,
    ),
    (
        "whales_summary_walker",
        "pre-rendered /whales tier + type combinations, 5-min",
        "Pre-renders the /whales tier and type combinations (?tier=1m, "
        "?tier=50k, ?type=trustset) that cost 5-19s cold. An in-process SWR "
        "ceiling was not enough across Render instances, which is why this "
        "is a walker. Only the precomputed combinations are covered; an "
        "unusual query-string combination is computed live.",
        True,
    ),
    (
        "wss_relay_canary",
        "wss://wss.xrpldashboard.com reachability + ledgerClosed cadence "
        "from the public internet",
        "Verifies the own-node WSS relay is reachable from OUTSIDE and is "
        "actually pushing ledgerClosed events on cadence. After the live-feed "
        "sovereignty flip this relay is the primary feed for /tokens, "
        "/whales, /pools and /wallet, so silent death there degrades four "
        "pages at once. Probes the public path specifically -- a relay "
        "healthy on loopback but unreachable through the tunnel still fails, "
        "which is the point.",
        True,
    ),
    (
        "xrpl_env_drift_guard",
        "the 5 XRPL_ vars in ~/.config/xrpldashboard/env must point at :5006",
        "Hourly check that all five XRPL_ env vars in the env FILE point at "
        "the Lenovo LAN non-admin RPC port 5006 established in Wave-0. Reads "
        "the file directly, NOT the runtime environment, because the drift "
        "we care about is in the file that every wrapper sources. A process "
        "already running with a stale value in memory is therefore not "
        "detected here.",
        True,
    ),
    (
        "xrpl_stream_restart_rate",
        "xrpl_stream restart counts on Lenovo, rolling 24h and 7d",
        "Hourly count of xrpl_stream restarts over rolling 24h and 7d "
        "windows, written to walker_health so the rate is visible in "
        "Postgres instead of buried in xrpl_stream.log -- log analysis found "
        "1,218 watchdog-triggered restarts across 23 days. Counts restarts; "
        "it does not diagnose WHY, and a restart loop fast enough to stay "
        "inside one hour still reports as a single hour's total.",
        True,
    ),
    # ── RETIRED (owner ruling 2026-11-02) ──────────────────────────────
    # Registered, not deleted. UNDECLARED is a LEFT JOIN on this table, so a
    # row stops the alarm while every walker_health row and all history stay
    # intact. All five have cadence_seconds NULL and no loaded plist.
    (
        "escrow_seed_walk",
        "RETIRED 2026-11-02 — superseded, no plist, no schedule",
        "Retired: no launchd plist, cadence_seconds NULL, last run "
        "2026-10-16. The escrow seed walk was a staged one-shot; its "
        "stage-2 machinery remains in scripts/escrow_seed_walk.py for "
        "reference. Declared RETIRED so it stops rendering UNDECLARED "
        "without deleting its walker_health row or history. Revive by "
        "restoring a plist and giving it a cadence.",
        True,
    ),
    (
        "supply_block_walker",
        "RETIRED 2026-11-02 — superseded by supply_fast_walker, no plist",
        "Retired: no plist, cadence NULL, last run 2026-09-26. The "
        "xrp_supply_block singleton is now maintained by supply_fast_walker "
        "(60s, total_* columns) and its slower counterpart. Declared "
        "RETIRED to stop the alarm; row and history preserved.",
        True,
    ),
    (
        "openclaw_cli_backend_drift",
        "RETIRED 2026-11-02 — no plist, no schedule",
        "Retired: no plist, cadence NULL, last run 2026-09-26. Declared "
        "RETIRED to stop the UNDECLARED alarm; walker_health row and "
        "history preserved for the record.",
        True,
    ),
    (
        "rwa_nav_transparency_walker",
        "RETIRED 2026-11-02 — no plist, never completed a scheduled run",
        "Retired: no plist, cadence NULL, last run 2026-09-25. RWA NAV "
        "coverage now runs through rwa_supply_nav_walker (daily, "
        "rwa_nav_sources.yaml). Declared RETIRED; row and history "
        "preserved.",
        True,
    ),
    (
        "nft_activity_reconcile-type-rollup",
        "RETIRED 2026-11-02 — composite pseudo-name, not a real unit",
        "Retired: no plist, no source file, cadence NULL, last run "
        "2026-09-26. The hyphenated name is a COMPOSITE of two "
        "nft_activity_walker modes (--mode reconcile and the type rollup), "
        "not a schedulable unit -- the same pseudo-name class this seeder's "
        "2026-07-07 docstring records getting wrong. The real work runs "
        "under nft_activity_summary and nft_activity_walker's other modes. "
        "Declared RETIRED; row and history preserved.",
        True,
    ),
    # ── RETIRED, corrected 2026-11-02 ──────────────────────────────────
    # This row first shipped (same day) as PAUSED pointing at
    # paused_walkers.py. That was WRONG and the correction matters, because
    # two near-identical names exist and they are TWO DIFFERENT WALKERS:
    #
    #   escrow_supply_walker  (this row, escrow_supply_walker.py)
    #       cohort EscrowCreate total for /cold-storage's "locked" figure.
    #       RETIRED 2026-09-25 — its own walker_health message says so:
    #       "RETIRED 2026-09-25: plist archived (private_infra 5101578)".
    #       Last run 2026-09-24 21:21Z, ok=True. No plist exists.
    #
    #   supply_escrow_walker  (supply_escrow_walker.py, NOT this row)
    #       full ledger-wide ledger_data(type=escrow) census on our own
    #       node. Genuinely PAUSED 2026-10-15, owner-gated at escrow
    #       stage 4, and it IS the name listed in paused_walkers.py.
    #
    # Different files, different jobs (cohort total vs full-ledger census),
    # last runs three weeks apart, different states. The earlier row
    # attributed one walker's state to the other.
    (
        "escrow_supply_walker",
        "RETIRED 2026-09-25 — superseded by xrp_supply_block's "
        "escrow_ripple_* columns",
        "Retired, not paused: the plist was archived 2026-09-25 "
        "(private_infra 5101578) and no launchd unit exists, so "
        "cadence_seconds is NULL and the last run is 2026-09-24 21:21Z. "
        "Superseded because the /cold-storage escrow side is now read from "
        "xrp_supply_block's escrow_ripple_* columns via "
        "db._read_escrow_side_from_supply_block(), which shapes them like a "
        "legacy escrow_supply_snapshot row — so this walker's output had no "
        "remaining consumer. Declared RETIRED so it stops rendering "
        "UNDECLARED; the walker_health row and all history are preserved, "
        "and escrow_supply_walker.py is left in the tree for reference. "
        "Do NOT confuse with supply_escrow_walker, which is a different "
        "walker, is paused rather than retired, and is the name "
        "paused_walkers.py actually lists.",
        True,
    ),
    (
        "escrow_unlock_walker",
        "category='ripple' in named_accounts.json minus RLUSD issuer, "
        "scoped by escrow OWNER, 3-day lookback",
        "account_tx for the same 20-account Ripple monthly-release cohort "
        "escrow_walker uses, over a 3-day lookback, recording every "
        "EscrowFinish/EscrowCreate into escrow_unlock_events (PK tx_hash). "
        "Backs the monthly release history section on /cold-storage and "
        "/cold-storage/unlocks.json. Scope is by escrow OWNER, not "
        "submitter: EscrowFinish may be submitted by any account, and the "
        "third-party submitter on the Sept 2026 cohort releases finished "
        "364 unrelated escrows that same month — scoping by submitter "
        "would have put all 364 on the page. What is therefore INVISIBLE "
        "here: escrow releases by any owner outside this cohort, and any "
        "cohort release older than the 3-day lookback that the one-time "
        "12-month backfill did not already record. Rows are tagged "
        "source='own-node' where our ~54k-ledger window reaches and "
        "'witness:<host>' otherwise; the UPSERT upgrades witness to "
        "own-node and never downgrades. A month inside the 12-month "
        "display window with zero observed rows is reported as a finding, "
        "never rendered as a month with no releases. NOT bound by the "
        "stage-2 incremental policy: nothing here derives or serves a "
        "running supply total.",
        True,
    ),
    (
        "supply_escrow_walker",
        "PAUSED (intentional) — full escrow object enumeration on own "
        "node, pending incremental stream-tracking redesign",
        "PAUSED 2026-10-14 (intentional, see escrow redesign). Its launchd "
        "job is booted out, so it cannot refresh walker_health and would "
        "otherwise page the canary on a walker that is not scheduled to "
        "run. cadence_seconds is NULLed in walker_health so "
        "compute_walker_staleness() treats it as never-STALE (db.py:2985, "
        "the documented path for event-triggered / manual / paused "
        "walkers). The row is deliberately KEPT so /coverage still lists "
        "it as paused rather than silently dropping it. WHY PAUSED: the "
        "full ledger_data(type=escrow) state-tree enumeration degraded "
        "from 90.9 to 23.7 pages/s and stopped finishing inside its hard "
        "cap, so passes were being cut off mid-walk. Pending replacement "
        "is an incremental EscrowCreate/EscrowFinish/EscrowCancel stream "
        "tracker rather than a full re-enumeration every cycle. The "
        "homepage escrow figure is stamped 'as of <UTC>' + 'refresh "
        "paused' in amber while this is paused (commit f6dd8f6), so the "
        "frozen number is disclosed, not presented as current. Restore by "
        "reloading the launchd job — the walker re-declares "
        "cadence_seconds on its next write_walker_health_start().",
        True,
    ),
    (
        "oracle_walker",
        "category='oracle' rows in named_accounts.json (v1 = DIA only)",
        "AccountObjects(type='oracle') for the curated cohort tagged "
        "category='oracle' in named_accounts.json. Currently DIA "
        "(rP24Lp7bcUHvEW7T7c8xkxtQKKd9fZyra7). A one-time 2026-07-02 full "
        "ledger walk found 4 non-DIA oracle owners, all hobby-flavor; "
        "none surfaced on /price-data. XRPLWin's directory carries the "
        "full XRPL oracle inventory (linked out from template).",
        True,
    ),
    (
        "bridge_signer_walker",
        "single account: Axelar XRPL Gateway",
        "Scans account_tx for rfmS3zqrQrka8wVyhXifEeyTwe8AMz2Yhw only. "
        "Filters TransactionType == 'SignerListSet' with tesSUCCESS. "
        "Ingests from public s1.ripple.com:51234 (env-overridable) — per "
        "the polite-pace-on-public-XRPL-infra rule, freshness rests on "
        "Ripple public infrastructure, not the local rippled. Other "
        "bridges (FXRP, Squid, future Wormhole NTT) are out of scope "
        "until named_accounts adds them with category='bridge'.",
        True,
    ),
    (
        "nft_activity_backfill",
        "PAUSED (intentional) — backward-walk bounded by 2026-04-01 "
        "cutoff (ledger 103252853)",
        "PAUSED 2026-10-12 (intentional, see pending NFT fix). Its launchd "
        "job is unloaded, so it cannot refresh walker_health and was "
        "paging the canary on a walker that is not scheduled to run. "
        "cadence_seconds is NULLed in walker_health so "
        "compute_walker_staleness() treats it as never-STALE (db.py:2985, "
        "the documented path for event-triggered / manual / paused "
        "walkers). The row is deliberately KEPT so /coverage still lists "
        "it as paused rather than silently dropping it. Restore by "
        "reloading the launchd job — the walker re-declares "
        "cadence_seconds=900 on its next write_walker_health_start(). "
        "Scope when running: walks backward from the forward-cursor's "
        "seed ledger toward "
        "ledger 103252853 (2026-04-01 00:00:00 UTC, binary-searched via "
        "s2-clio.ripple.com on 2026-07-04). NFT activity BEFORE this "
        "cutoff is permanently absent — /nfts 'all-time' claims must be "
        "read as 'from 2026-04-01'. Uses s2-clio.ripple.com:51234 "
        "directly per the polite-pace-on-public-XRPL-infra rule; multi-"
        "week backfill acceptable, freshness rests on Ripple public "
        "infrastructure.",
        True,
    ),
    (
        "amm_tvl_recorder",
        "top-N AMM pools by current TVL (default 200)",
        "Reads the current point-in-time snapshot from amm_ranked_pools "
        "(written by rank_amms) and appends one row per pool to "
        "amm_tvl_history. Top-N cutoff means pools ranked below N are "
        "invisible to the TVL history chart. No RPC — pure PG-to-PG.",
        True,
    ),
    (
        "daily_snapshot",
        "every named_accounts.json account + top-N AMM pools",
        "Point-in-time snapshot of XRP balance, sequence, and owner "
        "count for every account in named_accounts.json + top-N AMM "
        "pools (default 200) by TVL. Accounts outside named_accounts "
        "and AMMs below the top-N cutoff are absent from the daily "
        "snapshot artifact. Output: historical_snapshots/YYYY-MM-DD.json.",
        True,
    ),
    (
        "lending_snapshot",
        "XLS-66 broker/vault/loan picture, top brokers enriched",
        "Full broker/vault/loan picture via lending_data.fetch_lending_data, "
        "with top brokers by TVL enriched by depositor counts (mpt_holders "
        "RPC on each vault's ShareMPTID). Dormant pre-amendment-activation: "
        "if amendments aren't live, writes 'activated: false' and exits — "
        "no wasted RPC calls.",
        True,
    ),
    (
        "mpt_snapshot",
        "every MPTokenIssuance on mainnet",
        "Walks ledger_data (1-5h) to enumerate every MPTokenIssuance on "
        "mainnet, decodes XLS-89 metadata, enriches with mpt_holders "
        "walk. Full-population walk, but time-bounded by ledger_data "
        "cost — very-late-appearing issuances between runs are invisible "
        "until the next daily.",
        False,
    ),
    (
        "mpt_holders_refresh",
        "reads mpt_issuance_list.json (no fresh ledger_data walk)",
        "Hourly refresh reads mpt_issuance_list.json (written at end of "
        "each daily mpt_snapshot run) — does NOT re-walk ledger_data. "
        "Fetches current OutstandingAmount via ledger_entry + mpt_holders "
        "walk per issuance. New MPT issuances that appear between daily "
        "mpt_snapshot runs are invisible to the hourly supply history "
        "until the next daily.",
        True,
    ),
    (
        "rlusd_refresher",
        "Ethereum + XRPL RLUSD combined state",
        "Fetches Ethereum + XRPL RLUSD state via rlusd_live._refresh_cache_once "
        "(the shared helper) and writes to rlusd_state_cache in PG. "
        "Replaces the per-gunicorn-worker lazy refresher pattern that "
        "died silently on every deploy (caught 2026-05-27). 5-min "
        "cadence. Uses public Ethereum + XRPL public infrastructure.",
        False,
    ),
    (
        "signed_snapshot",
        "canonical daily metrics (Ed25519-signed)",
        "Daily canonical metrics (ledger index, pool count + TVL, MPT "
        "count, watchlist totals) canonicalized, SHA-256'd, Ed25519-"
        "signed. Snapshot scope is limited to the canonical metrics "
        "list; a metric not in this list is not covered by the daily "
        "integrity commitment.",
        True,
    ),
    (
        "unl_snapshot",
        "vl.ripple.com + vl.xrplf.org published lists (both)",
        "Fetches both canonical published XRPL validator lists "
        "(vl.ripple.com + vl.xrplf.org), decodes signed manifests, one "
        "row per (source, day) in unl_snapshots. Third-party UNL lists "
        "(if any exist) are out of scope. Freshness rests on the two "
        "list publishers' infrastructure.",
        True,
    ),
    (
        "token_price_ratio_cache",
        "in-process price cache inside xrpl_stream",
        "Sub-writer inside xrpl_stream — refreshes the token price ratio "
        "cache on TTL. Not a standalone walker; the walker_health row is "
        "written from inside the stream process. Coverage scope inherits "
        "xrpl_stream's full firehose. Freshness rests on the stream "
        "process staying alive.",
        False,
    ),
    (
        "verify_toml",
        "domains in xrpl_org_domains.json + XRPL Foundation registry",
        "First-party XRPL identity scanner. ORG MODE: fetches xrp-ledger.toml "
        "from every domain in xrpl_org_domains.json and promotes every "
        "address listed in [[ACCOUNTS]]. Discovery of new orgs relies on "
        "manual curation of xrpl_org_domains.json + Foundation registry "
        "polling. Ownership proof = the toml URL. Weekly cadence.",
        True,
    ),
    (
        "enrich_token_names",
        "MPT (all) + IOU-TOML (top-200 unlabeled) + AMM LP (derived)",
        "Populates token_names.json from three sources: (A) mpt_snapshot.json "
        "→ source='mpt_metadata' for every named MPT issuance (XLS-89 "
        "on-ledger, highest trust tier); (B) top-N unlabeled IOU issuers "
        "from token_volume (--limit default 200) with account_info Domain "
        "lookup → external xrp-ledger.toml fetch → match on (code, issuer) "
        "in [[CURRENCIES]] → source='toml' on full match, source="
        "'domain_fallback' when Domain resolves but TOML lacks the currency "
        "block; (C) amm_index.json → protocol-deterministic LP token code "
        "derivation → source='lp_derived'. INSERT-ONLY: never overwrites "
        "existing entries, so an issuer that migrates domain or renames a "
        "token keeps its stale name until the entry is manually cleared. "
        "IOU issuers beyond the 200-per-pass window and issuers without a "
        "Domain field are invisible until the unlabeled population shrinks "
        "or the limit is raised. Weekly cadence.",
        True,
    ),
    (
        "pg_backup_canary",
        "Layer 3 freshness canary for the encrypted Neon dump pipeline",
        "Daily launchd job (launchd/run_pg_backup_canary.sh) that lists "
        "b2crypt:xrpldashboard-backup-<host>/postgres/ for the newest "
        "neondb-*.dump, parses the embedded UTC timestamp from the "
        "filename, computes age in hours, and fails if age > "
        "CANARY_MAX_AGE_HOURS (default 25h — one-hour slack over the "
        "03:30 backup). Filename parsing beats rclone-modtime here "
        "because crypt remotes can lose modtime metadata; filenames "
        "survive the encryption boundary intact. Not an XRPL walker; row "
        "exists so canary freshness surfaces on /walker_health under the "
        "same alarm framework. Pairs with pg_restore_test (weekly "
        "correctness). Honest partial: only inspects the newest file's "
        "filename age — an intermediate day of missing dumps between "
        "two fresh ones is invisible until the next stale-day rollover.",
        True,
    ),
    (
        "pg_restore_test",
        "Layer 3 correctness of the encrypted Neon dump pipeline",
        "Weekly launchd job (tools/restore_test.py) that pulls the newest "
        "neondb-*.dump from b2crypt, spins up an ephemeral local Postgres "
        "on port 5439, restores into a throwaway DB, runs smoke queries "
        "(row counts + freshness on token_prices/events/unl_snapshots), "
        "tears everything down. Answers 'are backups usable?' — pairs "
        "with pg_backup_canary's 'are backups running?'. Sundays 04:00. "
        "Honest partial: smoke queries verify structural correctness "
        "and non-emptiness of a handful of tables, not full row-level "
        "recovery of every walker's output.",
        True,
    ),
    (
        "mcp_server_heartbeat",
        "read-only MCP tool proxy over existing Neon tables (Day 7: 15 tools registered)",
        "Self-writing 60s heartbeat from mcp_server.py's background daemon "
        "thread. Not an XRPL walker — the row exists so the MCP server's own "
        "liveness surfaces on /walker_health under the same alarm framework "
        "as every other walker (goes yellow within 60s, red within ~5min if "
        "the process dies). Registered tool inventory (Day 7, 15 tools): "
        "Day 2 ledger primitives — get_ledger_stats, get_amendment_status, "
        "get_unl_status. Day 3 value flows — get_whale_events, "
        "get_whale_watchlist, get_rlusd_supply, get_rlusd_flow_24h. Day 4 "
        "AMM + tokens — get_amm_pool, get_amm_top_by_tvl, "
        "get_token_attestation, get_rwa_families, get_rwa_pools, "
        "get_mpt_snapshot. Day 7 signed-snapshot moat — get_signed_snapshot "
        "(daily Ed25519-signed envelope with Merkle audit path), "
        "verify_snapshot_signature (stateless verification path). "
        "Enforcement: every tool routes through mcp_server.wrap_envelope, "
        "which is the single response-wrap function; no tool bypasses it "
        "(docs/AGENT_TIER_DESIGN.md §Enforcement).",
        True,
    ),
    (
        "mcp_server_last_tool_call",
        "Q1 heartbeat-gap watermark — last successful MCP tool response",
        "Not a walker. Watermark row stamped by mcp_server.stamp_tool_call "
        "at the tail of every tool that successfully emitted an envelope. "
        "Purpose: distinguish 'heartbeat thread alive' from 'HTTP listener "
        "serving tools' — the Q1 gap declared in mcp_server.py's header. "
        "If the FastMCP HTTP listener crashes while the heartbeat daemon "
        "thread survives, mcp_server_heartbeat.last_success_at stays fresh "
        "while this row goes stale within one cadence, and /walker_health "
        "surfaces the delta. No cadence declared — updates happen on tool "
        "traffic, not a timer. Absence of freshness relative to the tool "
        "call rate IS the signal; declared_scope only ensures "
        "answer_plausibility_walker's UNDECLARED_WALKER rule doesn't fire "
        "the first time a tool call lands. Traffic-driven, so honest_partial "
        "= True (staleness during a legitimate quiet period is expected).",
        True,
    ),
    (
        "census_watcher",
        "dormant / on-demand condition-triggered launcher",
        "Not scheduled. Manual launcher that polls localhost:5005 "
        "server_info and fires census_escrow_phase1c once when "
        "load_factor <= 5.0 AND server_state in {full, proposing} for "
        "10 consecutive 60s polls. Self-registers in walker_health at "
        "kickoff. Last fired 2026-07-13 (74 polls, 10-good streak) — the "
        "escrow census question (404 TokenEscrow objects) was answered "
        "there. Hardcoded DEADLINE_UTC 2026-07-16 has since passed; walker "
        "will refuse to arm without a source bump. Row kept declared so "
        "re-arming for a future census pass doesn't trip UNDECLARED_WALKER "
        "on answer_plausibility_walker.",
        True,
    ),
]


def main():
    if not db.pg_available():
        raise SystemExit("DATABASE_URL not set")

    # Purge any stale rows that don't match the current declaration set
    # (e.g. rename-corrections from the reconciliation cycle). Prevents
    # orphan pseudo-walker rows from lingering.
    valid_names = {row[0] for row in SCOPES}
    import psycopg
    import os
    with psycopg.connect(os.environ["DATABASE_URL"]) as c, c.cursor() as cur:
        cur.execute("SELECT walker_name FROM walker_scope_declarations")
        existing = {r[0] for r in cur.fetchall()}
        stale = existing - valid_names
        for name in sorted(stale):
            cur.execute(
                "DELETE FROM walker_scope_declarations WHERE walker_name=%s",
                (name,),
            )
            print(f"[PURGE] {name}")
        c.commit()

    n_ok = 0
    for row in SCOPES:
        walker_name, declared_scope, filter_note, honest_partial = row
        if db.upsert_walker_scope_declaration(
            walker_name=walker_name,
            declared_scope=declared_scope,
            filter_note=filter_note,
            honest_partial=honest_partial,
        ):
            n_ok += 1
            partial = "PARTIAL" if honest_partial else "FULL"
            print(f"[{partial}] {walker_name}: {declared_scope}")
        else:
            print(f"[FAIL] {walker_name}")
    print(f"\nSeeded {n_ok}/{len(SCOPES)} walker scope declarations.")


if __name__ == "__main__":
    main()
