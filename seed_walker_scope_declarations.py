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
        "backward-walk bounded by 2026-04-01 cutoff (ledger 103252853)",
        "Walks backward from the forward-cursor's seed ledger toward "
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
