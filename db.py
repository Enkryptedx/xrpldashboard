"""Postgres bridge for xrpldashboard.

Why this exists
---------------
Workers (xrpl_stream.py, amm_scan_pools.py) only run on Charlie's Mac.
The Flask web app runs on Render. Without a shared store, prod reads
frozen committed SQLite snapshots — whales/tokens panels look dead.

This module is the bridge: workers dual-write to Postgres alongside
SQLite, and the Flask app reads from Postgres when DATABASE_URL is set,
falling back to SQLite on any error so partial outages serve stale-but-
correct data instead of 500s.

Activation
----------
- DATABASE_URL unset (local dev / first deploy): every helper here is a
  silent no-op for writes and `pg_available()` returns False for reads.
  Existing SQLite paths in app.py and the workers stay authoritative —
  zero behavior change.
- DATABASE_URL set + psycopg installed: writes mirror to Postgres,
  reads prefer Postgres.

Schema mirrors the SQLite tables (events.db / volumes.db) one-to-one
so the dual-write logic is symmetrical and easy to audit.

Worker connection model
-----------------------
Workers fire many writes per second during XRPL bursts, so we cache a
single long-lived module connection (`_writer_conn`). On any failure we
drop and reopen on the next call. Flask request paths use `pg_connect()`
context-managed connections, which is fine at our request volume.
"""

import datetime
import os
import json
import time
from contextlib import contextmanager

try:
    import psycopg  # psycopg 3 (psycopg[binary])
except ImportError:
    psycopg = None


# Rate-limited error logger. Workers fire many writes per second; if Postgres
# goes down, surfacing every exception would drown Render's log viewer. We
# emit the first failure of each category immediately, then at most once per
# minute per category, tagged with the suppressed-since count so the volume
# stays legible. The 2026-05-12 MPT walk wrote the JSON file fine but its
# 5.6-hour-deferred Postgres push failed silently — exactly the class of
# silent-write failure this helper makes visible.
_LAST_ERR_LOG = {}  # category -> (last_ts, suppressed_count)
_ERR_LOG_INTERVAL_S = 60
_SCHEMA_DRIFT_SEEN = set()  # category -> True once we've stack-dumped


def _is_schema_drift(exc):
    if psycopg is None:
        return False
    return isinstance(exc, (
        psycopg.errors.UndefinedTable,
        psycopg.errors.UndefinedColumn,
        psycopg.errors.UndefinedFunction,
        psycopg.errors.UndefinedObject,
    ))


def _log_err(category, exc):
    # Schema-drift class NEVER self-heals; treating it like a transient
    # error hid the tx_type_hourly gap for 6 minutes on 2026-07-07.
    # Always loud, never rate-limited. First hit dumps the stack so the
    # caller is grep-able.
    if _is_schema_drift(exc):
        if category not in _SCHEMA_DRIFT_SEEN:
            import traceback
            tb = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))
            print(
                f"[db] !!! SCHEMA-DRIFT {category}: {type(exc).__name__}: {exc}\n"
                f"[db]     Won't self-heal — apply init_schema() or the "
                f"relevant migration.\n{tb}",
                flush=True,
            )
            _SCHEMA_DRIFT_SEEN.add(category)
        else:
            print(
                f"[db] !!! SCHEMA-DRIFT {category}: {type(exc).__name__}: {exc}",
                flush=True,
            )
        return

    now = time.time()
    last_ts, suppressed = _LAST_ERR_LOG.get(category, (0, 0))
    if now - last_ts < _ERR_LOG_INTERVAL_S:
        _LAST_ERR_LOG[category] = (last_ts, suppressed + 1)
        return
    tail = f" ({suppressed} suppressed in last {_ERR_LOG_INTERVAL_S}s)" if suppressed else ""
    print(f"[db] {category}: {type(exc).__name__}: {exc}{tail}", flush=True)
    _LAST_ERR_LOG[category] = (now, 0)


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS events (
    tx_hash      TEXT PRIMARY KEY,
    ledger_index BIGINT NOT NULL,
    ts           BIGINT NOT NULL,
    type         TEXT NOT NULL,
    from_addr    TEXT,
    to_addr      TEXT,
    amount_drops BIGINT,
    currency     TEXT,
    issuer       TEXT,
    raw_json     JSONB
);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts DESC);
CREATE INDEX IF NOT EXISTS events_type_ts_idx ON events (type, ts DESC);

CREATE TABLE IF NOT EXISTS token_volume (
    currency    TEXT NOT NULL,
    issuer      TEXT NOT NULL,
    hour_bucket BIGINT NOT NULL,
    volume_xrp  DOUBLE PRECISION NOT NULL DEFAULT 0,
    trade_count BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (currency, issuer, hour_bucket)
);
CREATE INDEX IF NOT EXISTS token_volume_bucket_idx
    ON token_volume (hour_bucket DESC);

CREATE TABLE IF NOT EXISTS amm_pool_events (
    id          BIGSERIAL PRIMARY KEY,
    ts          BIGINT NOT NULL,
    amm_account TEXT NOT NULL,
    event_type  TEXT NOT NULL
);
ALTER TABLE amm_pool_events ADD COLUMN IF NOT EXISTS magnitude_xrp_drops BIGINT;
CREATE INDEX IF NOT EXISTS amm_pool_events_ts_idx
    ON amm_pool_events (ts DESC);

CREATE TABLE IF NOT EXISTS worker_heartbeat (
    worker        TEXT PRIMARY KEY,
    ts            BIGINT NOT NULL,
    txns_seen     BIGINT,
    last_ledger   BIGINT,
    extra         JSONB
);

CREATE TABLE IF NOT EXISTS amm_ranked_pools (
    id             BIGSERIAL PRIMARY KEY,
    amm_account    TEXT,
    pair           TEXT NOT NULL,
    fee_pct        DOUBLE PRECISION,
    fee_raw        INTEGER,
    amount_a       DOUBLE PRECISION,
    amount_b       DOUBLE PRECISION,
    asset_a        JSONB,
    asset_b        JSONB,
    tvl_usd        DOUBLE PRECISION,
    tvl_status     TEXT,
    kind           TEXT,
    lp_token_value NUMERIC,
    snapshot_ts    BIGINT NOT NULL
);
ALTER TABLE amm_ranked_pools ADD COLUMN IF NOT EXISTS lp_token_value NUMERIC;
CREATE INDEX IF NOT EXISTS amm_ranked_pools_tvl_idx
    ON amm_ranked_pools (tvl_usd DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS page_views (
    id            BIGSERIAL PRIMARY KEY,
    ts            BIGINT NOT NULL,
    path          TEXT NOT NULL,
    visitor_hash  TEXT,
    referrer      TEXT,
    user_agent    TEXT,
    country       TEXT,
    utm_source    TEXT,
    ip_day_hash   TEXT
);
ALTER TABLE page_views ADD COLUMN IF NOT EXISTS utm_source TEXT;
-- ip_day_hash links requests from the same client IP across UA rotation,
-- so a single scanner cycling through Chrome/Firefox/Safari UA strings
-- gets bucketed as one bot session instead of N distinct "people".
-- Backfilled NULL on pre-rollout rows; the bot-filter session join uses
-- COALESCE so NULLs do not poison classification.
ALTER TABLE page_views ADD COLUMN IF NOT EXISTS ip_day_hash TEXT;
CREATE INDEX IF NOT EXISTS page_views_ts_idx ON page_views (ts DESC);
CREATE INDEX IF NOT EXISTS page_views_path_ts_idx
    ON page_views (path, ts DESC);
CREATE INDEX IF NOT EXISTS page_views_visitor_idx
    ON page_views (visitor_hash, ts DESC);
CREATE INDEX IF NOT EXISTS page_views_ip_day_idx
    ON page_views (ip_day_hash, ts DESC);

-- Single-row JSONB blob mirroring mpt_snapshot.json. Mac worker writes;
-- Render Flask reads. Render has no local snapshot file so PG is the
-- only source there; without this, /mpts on prod blocks ~10min walking
-- the ledger on every cold request.
CREATE TABLE IF NOT EXISTS mpt_snapshot (
    id         INTEGER PRIMARY KEY,
    payload    JSONB NOT NULL,
    written_at BIGINT NOT NULL,
    CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS historical_snapshot_meta (
    id         INTEGER PRIMARY KEY,
    payload    JSONB NOT NULL,
    written_at BIGINT NOT NULL,
    CHECK (id = 1)
);

-- Per-account daily snapshot. Mac worker (daily_snapshot.py) dual-writes
-- here alongside historical_snapshots/YYYY-MM-DD.json so Render-side
-- editorial surfaces can read per-account XRP + trust-line history
-- without reaching for the Mac's filesystem. PRIMARY KEY (snapshot_date,
-- address) gives same-day-replace semantics matching the JSON file's
-- daily-overwrite cadence — a launchd retry after a transient failure
-- UPSERTs cleanly.
--
-- trust_lines JSONB carries the account_lines payload (list of
-- {currency, issuer, balance, limit, no_ripple, ...}) for non-zero
-- balances only. JSONB chosen over a normalized table for Phase 1 —
-- editorial surfaces can extract specific currency:issuer pairs with
-- a JSONB path query. If trust-line-level history queries become a
-- hot path later, normalization is a forward migration.
CREATE TABLE IF NOT EXISTS historical_account_snapshots (
    snapshot_date  DATE NOT NULL,
    address        TEXT NOT NULL,
    name           TEXT,
    category       TEXT,
    balance_xrp    DOUBLE PRECISION,
    balance_drops  BIGINT,
    sequence       BIGINT,
    owner_count    INTEGER,
    trust_lines    JSONB,
    error          TEXT,
    written_at     BIGINT NOT NULL,
    PRIMARY KEY (snapshot_date, address)
);
CREATE INDEX IF NOT EXISTS historical_account_snapshots_addr_date_idx
    ON historical_account_snapshots (address, snapshot_date DESC);

-- Single-row JSONB blob for the /credentials live tracker. Holds
-- amendment status, the latest cumulative SHAMap walk result, and the
-- latest recent-activity tx scan. Walker writes (currently a daemon
-- thread inside a gunicorn worker, future: launchd job); every Flask
-- worker reads on each request so cold-start workers never render
-- placeholders.
CREATE TABLE IF NOT EXISTS credentials_snapshot (
    id         INTEGER PRIMARY KEY,
    payload    JSONB NOT NULL,
    written_at BIGINT NOT NULL,
    CHECK (id = 1)
);

-- Account labels registry. Three layers, one row per address:
--   manual      — curated by hand from xrpscan, bithomp, etc. (the source
--                 column records WHERE the label came from so visitors
--                 see "via xrpscan" rather than us implying we discovered it)
--   xrpscan     — bulk imports from xrpscan's public labels (when added)
--   derived:*   — auto-generated from on-chain state we already have
--                 (AMM pools, MPT issuers). These get rewritten on each
--                 importer run; manual/xrpscan never get overwritten by them.
-- One PRIMARY KEY (address) means a derived label can't shadow a curated
-- one — the importer must UPSERT with source-priority logic.
CREATE TABLE IF NOT EXISTS account_labels (
    address     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT,
    source      TEXT NOT NULL,
    confidence  DOUBLE PRECISION DEFAULT 1.0,
    extra       JSONB,
    updated_at  BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS account_labels_category_idx
    ON account_labels (category);
CREATE INDEX IF NOT EXISTS account_labels_source_idx
    ON account_labels (source);

-- Per-pool TVL time-series. amm_tvl_recorder.py appends a row per
-- top-N pool every 15 min so we accumulate forward history (the
-- amm_ranked_pools snapshot is point-in-time only and gets wiped on
-- each rerank). Eventually drives a 30-day sparkline on /pools.
CREATE TABLE IF NOT EXISTS amm_tvl_history (
    amm_account TEXT NOT NULL,
    ts          BIGINT NOT NULL,
    tvl_usd     DOUBLE PRECISION,
    amount_a    DOUBLE PRECISION,
    amount_b    DOUBLE PRECISION,
    pair        TEXT,
    PRIMARY KEY (amm_account, ts)
);
CREATE INDEX IF NOT EXISTS amm_tvl_history_ts_idx
    ON amm_tvl_history (ts DESC);
CREATE INDEX IF NOT EXISTS amm_tvl_history_account_ts_idx
    ON amm_tvl_history (amm_account, ts DESC);

-- Per-MPT supply + concentration time-series. mpt_snapshot.py appends a
-- row per eligible MPT each hourly run — eligible meaning the holders
-- walk completed cleanly (reason ∈ {complete, no_holders}). Pending /
-- incomplete / skipped_test walks do NOT enter history, so any chart
-- rendered off this table is gap-free where it has data; gaps mean the
-- walk wasn't conclusive that hour, not that the issuance went dark.
--
-- outstanding_amount is NUMERIC because XRPL MPT amounts can exceed
-- bigint (the on-ledger Amount field is uint64). PRIMARY KEY (issuance,
-- ts) makes the worker idempotent across retries.
CREATE TABLE IF NOT EXISTS mpt_supply_history (
    mpt_issuance_id       TEXT NOT NULL,
    snapshot_ts           BIGINT NOT NULL,
    outstanding_amount    NUMERIC,
    holders_with_balance  INT,
    holders_authorized    INT,
    top1_share            NUMERIC(6,3),
    top3_share            NUMERIC(6,3),
    PRIMARY KEY (mpt_issuance_id, snapshot_ts)
);
CREATE INDEX IF NOT EXISTS mpt_supply_history_issuance_ts_idx
    ON mpt_supply_history (mpt_issuance_id, snapshot_ts DESC);

-- Last-good cross-chain RLUSD state for SSR cold-start fallback. Single-row
-- JSONB blob — same pattern as mpt_snapshot. rlusd_live's refresher dual-
-- writes here on every successful full-supply tick; /rlusd reads when the
-- live fetch fails so a cold worker still renders real numbers + a "Last
-- updated · X ago · reconnecting" chip instead of blank dashes.
CREATE TABLE IF NOT EXISTS rlusd_state_cache (
    id         INTEGER PRIMARY KEY,
    payload    JSONB NOT NULL,
    written_at BIGINT NOT NULL,
    CHECK (id = 1)
);

-- Daily RLUSD supply history (append-only). Companion to rlusd_state_cache:
-- the cache is a ~30s live singleton (overwrites every refresh, mostly
-- duplicate rows if flipped to append); RLUSD only meaningfully changes
-- ~20×/day. Daily-grain history captures change cadence without codifying
-- 30s noise. See migrations/2026_05_25_rlusd_supply_history.sql and
-- feedback_history_flip_cadence_rule.md.
CREATE TABLE IF NOT EXISTS rlusd_supply_history (
    snapshot_date    DATE     NOT NULL,
    xrpl_supply      NUMERIC  NOT NULL,
    eth_supply       NUMERIC  NOT NULL,
    total_supply     NUMERIC  NOT NULL,
    xrpl_holders     INTEGER,
    eth_holders      INTEGER,
    xrpl_mints_24h   NUMERIC,
    xrpl_burns_24h   NUMERIC,
    eth_mints_24h    NUMERIC,
    eth_burns_24h    NUMERIC,
    written_at_iso   TEXT     NOT NULL,
    PRIMARY KEY (snapshot_date)
);
CREATE INDEX IF NOT EXISTS rlusd_supply_history_date_idx
    ON rlusd_supply_history (snapshot_date DESC);

-- Institutional RWA families with on-chain presence on XRPL. Seeded from
-- migrations/2026_05_14_rwa_schema.sql; SCHEMA_DDL re-declaration here keeps
-- fresh installs in sync. Idempotent.
CREATE TABLE IF NOT EXISTS rwa_family (
    family_slug        TEXT PRIMARY KEY,
    family_name        TEXT NOT NULL,
    description        TEXT,
    external_url       TEXT,
    attestation_level  TEXT NOT NULL
        CHECK (attestation_level IN ('verified','inferred','preliminary')),
    created_at         TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS rwa_pool_attribution (
    pool_address  TEXT NOT NULL,
    family_slug   TEXT NOT NULL REFERENCES rwa_family(family_slug),
    confidence    TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    provenance    TEXT NOT NULL,
    notes         TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (pool_address, family_slug)
);
CREATE INDEX IF NOT EXISTS idx_rwa_pool_attribution_family
    ON rwa_pool_attribution (family_slug);

-- Daily Ed25519-signed integrity snapshots. Mac worker (signed_snapshot.py)
-- writes one row per UTC day with the full signed JSON envelope; Flask on
-- Render reads from here to serve /.well-known/snapshots/<date>.json and
-- /snapshots/. Without PG mirroring, only the Mac can serve these files —
-- visitors hitting xrpldashboard.com (Render) get 404, which would silently
-- break the entire "verifiable history" claim. PRIMARY KEY (snapshot_date)
-- gives same-day replace semantics matching signed_snapshot.py.
CREATE TABLE IF NOT EXISTS signed_snapshots (
    snapshot_date  DATE PRIMARY KEY,
    envelope       JSONB NOT NULL,
    leaf_hash      TEXT NOT NULL,
    leaf_index     INTEGER NOT NULL,
    chain_root     TEXT NOT NULL,
    pubkey_fp      TEXT NOT NULL,
    written_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signed_snapshots_leaf_index
    ON signed_snapshots (leaf_index);

-- Single-row live chain head, mirroring signed_snapshots/chain.json. Kept
-- separate from signed_snapshots so /.well-known/snapshots/chain.json can
-- read one row instead of aggregating across all per-day rows. The leaves
-- array on disk is reconstructable from signed_snapshots, but exposing it
-- precomputed keeps the route O(1).
CREATE TABLE IF NOT EXISTS signed_snapshot_chain (
    id              INTEGER PRIMARY KEY DEFAULT 1,
    current_root    TEXT NOT NULL,
    leaves_total    INTEGER NOT NULL,
    schema_version  INTEGER NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (id = 1)
);

-- CTA click events. One row per click on an instrumented call-to-action
-- (currently only the /institutional "Become a launch partner" mailto).
-- Server-side click endpoints log here before issuing the 302 redirect
-- so we capture clicks even when JS is disabled and have durable forensic
-- detail (referrer, optional ?ref= channel tag) the analytics tool can't
-- give us. Visitor hash reuses the same day-bucketed HMAC pattern as
-- page_views so a click can be correlated with a view by the same visitor
-- without storing identifiers we could otherwise dox.
CREATE TABLE IF NOT EXISTS cta_clicks (
    id            BIGSERIAL PRIMARY KEY,
    ts            BIGINT NOT NULL,
    cta_id        TEXT NOT NULL,
    ref_param     TEXT,
    referrer      TEXT,
    visitor_hash  TEXT,
    user_agent    TEXT,
    country       TEXT
);
CREATE INDEX IF NOT EXISTS cta_clicks_ts_idx ON cta_clicks (ts DESC);
CREATE INDEX IF NOT EXISTS cta_clicks_cta_ts_idx ON cta_clicks (cta_id, ts DESC);

-- Per-token XRP-equivalent price. Derived by token_prices.py from the
-- in-memory ranked pool list at the end of each rank_amms.py run. One
-- row per (currency, issuer); tokens whose XRP-paired pool has reserve
-- below MIN_POOL_XRP_RESERVE are intentionally NOT written — at that
-- depth the implied constant-product price is fictional (single small
-- swap moves it 30%+). The absence of a row IS the signal: "price not
-- derivable from a deep-enough pool"; consumers must not backfill.
--
-- History-append since migrations/2026_05_25_token_prices_history_append.sql:
-- PK is (currency, issuer, snapshot_ts) so every walker run appends a new
-- row per token (INSERT … ON CONFLICT DO NOTHING). token_prices_latest_idx
-- powers the "current price" read path; the snapshot_ts index powers the
-- time-series read path.
CREATE TABLE IF NOT EXISTS token_prices (
    currency           TEXT             NOT NULL,
    issuer             TEXT             NOT NULL,
    snapshot_ts        BIGINT           NOT NULL,
    xrp_price          DOUBLE PRECISION NOT NULL,
    pool_amm_account   TEXT             NOT NULL,
    pool_xrp_reserve   DOUBLE PRECISION NOT NULL,
    pool_token_reserve DOUBLE PRECISION NOT NULL,
    derivation_method  TEXT             NOT NULL,
    PRIMARY KEY (currency, issuer, snapshot_ts)
);
CREATE INDEX IF NOT EXISTS token_prices_latest_idx
    ON token_prices (currency, issuer, snapshot_ts DESC);
CREATE INDEX IF NOT EXISTS token_prices_snapshot_idx
    ON token_prices (snapshot_ts DESC);

-- Daily UNL composition snapshots. unl_snapshot.py fetches each
-- canonical published UNL (vl.ripple.com, vl.xrplf.org) once per day,
-- decodes the signed manifest server-side, and persists the validator
-- list as JSONB. Daily cadence matches the observed rotation rate:
-- Ripple UNL changed validators once in 39 months of Wayback history,
-- XRPLF twice in 18 months — re-sequencing happens ~4×/year per list
-- but validator-set changes are ~yearly. The diff between consecutive
-- snapshots powers the "Validator-set composition over time" section
-- on /network.
--
-- Payload shape (one row per source per day):
--   {"sequence": 85,
--    "expiration_iso": "2027-04-06T17:51:34Z",
--    "validator_count": 35,
--    "validators": [{"pubkey": "...", "domain": "bitso.com"|null,
--                    "manifest_b64": "..."}]}
--
-- PRIMARY KEY (source, snapshot_date) is daily-granular (DATE, not
-- BIGINT) so multiple same-day runs land idempotent upserts and
-- restart-replay is built in. ~730 rows/year total.
CREATE TABLE IF NOT EXISTS unl_snapshots (
    source           TEXT  NOT NULL,
    snapshot_date    DATE  NOT NULL,
    payload          JSONB NOT NULL,
    fetched_at_iso   TEXT  NOT NULL,
    PRIMARY KEY (source, snapshot_date)
);
CREATE INDEX IF NOT EXISTS unl_snapshots_date_idx
    ON unl_snapshots (snapshot_date DESC);

-- XLS-80 PermissionedDomains Phase 1: walker + schema, no UI yet.
-- Append-only history shape from day 1 — the trajectory IS the data we
-- want (count rises 0→1→N as institutions adopt the primitive). Do NOT
-- collapse this to a singleton blob the way credentials_snapshot does;
-- per cadence-rule, write_cadence (daily) ≈ change_rate so a separate
-- daily-grain history table is the right shape.
CREATE TABLE IF NOT EXISTS permissioned_domains (
    snapshot_date         DATE NOT NULL,
    domain_id             TEXT NOT NULL,
    owner_account         TEXT NOT NULL,
    sequence              INTEGER NOT NULL,
    accepted_credentials  JSONB NOT NULL,
    cred_count            SMALLINT NOT NULL,
    previous_txn_id       TEXT,
    ledger_close_time     BIGINT NOT NULL,
    fetched_at_iso        TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, domain_id)
);
CREATE INDEX IF NOT EXISTS permissioned_domains_history_idx
    ON permissioned_domains (domain_id, snapshot_date DESC);
CREATE INDEX IF NOT EXISTS permissioned_domains_owner_idx
    ON permissioned_domains (owner_account);

-- Phase 1: created empty. Phase 2/3 populates from a PermissionedDomainSet
-- /Delete tx scan once we have a UI consuming events. The table exists now
-- so future migration is a column-add, not a schema introduction.
CREATE TABLE IF NOT EXISTS permissioned_domain_events (
    event_id          BIGSERIAL PRIMARY KEY,
    tx_hash           TEXT NOT NULL UNIQUE,
    tx_type           TEXT NOT NULL,
    domain_id         TEXT NOT NULL,
    owner_account     TEXT NOT NULL,
    ledger_index      BIGINT NOT NULL,
    ledger_close_time BIGINT NOT NULL,
    payload           JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS permissioned_domain_events_domain_idx
    ON permissioned_domain_events (domain_id, ledger_close_time DESC);
CREATE INDEX IF NOT EXISTS permissioned_domain_events_time_idx
    ON permissioned_domain_events (ledger_close_time DESC);

-- One row per walker pass. Without this audit trail, an empty
-- permissioned_domains table is indistinguishable from "walker never
-- ran" or "walker errored silently." Phase 2's empty-state copy reads
-- straight off this table — "last walker pass <fetched_at_iso>, scanned
-- <accounts_queried> seed accounts, found <domains_found> domains" —
-- so the page reads as live infrastructure even at N=0.
CREATE TABLE IF NOT EXISTS permissioned_domain_walker_runs (
    snapshot_date      DATE PRIMARY KEY,
    fetched_at_iso     TEXT NOT NULL,
    seed_set_size      INTEGER NOT NULL,
    accounts_queried   INTEGER NOT NULL,
    domains_found      INTEGER NOT NULL,
    exhausted          BOOLEAN NOT NULL,
    walker_duration_ms INTEGER NOT NULL,
    notes              TEXT
);

-- Walker health — one row per scheduled background walker. start writes
-- last_run_started; end UPDATEs the outcome + bumps consecutive_failures
-- or resets to 0. /mpts (and future per-walker pages) read this so the
-- template renders a "may be stale" banner when a walker is silently
-- failing, instead of serving last-good as if it were live.
-- See migrations/2026_05_27_walker_health.sql + 2026_05_30_walker_health_cadence.sql.
-- cadence_seconds is each walker's self-declared expected run frequency
-- (mirrors its launchd plist StartInterval). The /walker_health page
-- uses it to compute staleness multiples (green/yellow/red) per row
-- without hardcoding thresholds. NULL allowed for walkers that haven't
-- declared one yet — page renders such rows with "unknown cadence".
CREATE TABLE IF NOT EXISTS walker_health (
    walker_name           TEXT PRIMARY KEY,
    last_run_started      TIMESTAMPTZ NOT NULL,
    last_run_completed    TIMESTAMPTZ,
    last_run_ok           BOOLEAN NOT NULL,
    last_run_message      TEXT,
    last_success_at       TIMESTAMPTZ,
    last_failure_at       TIMESTAMPTZ,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    cadence_seconds       INTEGER
);

-- bridge_signer_history — append-only ledger of Axelar XRPL Gateway
-- SignerList state. Source for /sidechain verifier-set rotation history.
--
-- Two row kinds, distinguished by tx_hash:
--   1) Bootstrap (tx_hash = 'BOOTSTRAP'): one row per gateway, written
--      once by bridge_signer_walker.py on its first run. Captures the
--      current SignerList state observed via account_objects when
--      there is no underlying SignerListSet transaction to anchor to.
--   2) Steady-state (tx_hash = actual SignerListSet tx hash): one row
--      per observed rotation. Walker scans account_tx in the validated
--      ledger range and INSERTs on PK conflict-skip.
--
-- Query patterns:
--   - "current state": ORDER BY ledger_index DESC LIMIT 1
--   - "rotations only": WHERE tx_hash != 'BOOTSTRAP' ORDER BY ledger_index
--   - "rotation count": SELECT COUNT(*) WHERE tx_hash != 'BOOTSTRAP'
--
-- See migrations/2026_06_08_bridge_signer_history.sql + bridge_signer_walker.py.
CREATE TABLE IF NOT EXISTS bridge_signer_history (
    ledger_index   BIGINT NOT NULL,
    close_time     TIMESTAMPTZ NOT NULL,
    quorum         INTEGER,
    signer_count   INTEGER,
    signer_entries JSONB,
    tx_hash        TEXT NOT NULL,
    written_at     BIGINT NOT NULL,
    PRIMARY KEY (ledger_index, tx_hash)
);
CREATE INDEX IF NOT EXISTS bridge_signer_history_ledger_desc_idx
    ON bridge_signer_history (ledger_index DESC);

CREATE TABLE IF NOT EXISTS walker_node_fallback (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    walker_name TEXT NOT NULL,
    reason      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS walker_node_fallback_ts_idx
    ON walker_node_fallback (ts DESC);
CREATE INDEX IF NOT EXISTS walker_node_fallback_walker_idx
    ON walker_node_fallback (walker_name, ts DESC);

-- Escrows snapshot — one row per active EscrowCreate ledger object owned
-- by a tracked named-account. Backs the /cold-storage per-escrow browser
-- and the upcoming-releases calendar. Populated by escrow_walker.py on
-- a 30-min launchd cadence; /cold-storage reads through the 5-min TTL
-- cache in escrow_snapshot.py.
--
-- v1 scope: replace-on-write (the walker deletes every row and inserts
-- fresh state each pass). Fine for ~102 rows and a current-state view.
-- If a "realized releases per month" chart is ever wanted, extend to
-- an append-with-history sibling table keyed by (owner, sequence,
-- observed_ledger_index) rather than reworking this one — door marked
-- in escrow_walker.py.
--
-- amount_json holds the full XRPL Amount value: a string of drops for
-- XRP escrows, a {currency, issuer, value} object for IOU escrows,
-- a {mpt_issuance_id, value} object for MPT escrows. denom is a
-- convenience column derived at write time so the read path can
-- filter without JSON parsing.
-- PK is the ledger entry hash (`index`), a 64-char hex unique per Escrow
-- object. Ledger objects don't carry a top-level Sequence field —
-- reconstructing one would require a follow-up account_tx lookup per
-- object, which is not worth the round trips for a snapshot table.
CREATE TABLE IF NOT EXISTS escrows_snapshot (
    ledger_index_hash     TEXT PRIMARY KEY,     -- Escrow object's `index`
    owner                 TEXT NOT NULL,
    owner_name            TEXT,
    destination           TEXT,
    denom                 TEXT NOT NULL,        -- 'XRP' | 'IOU' | 'MPT'
    amount_drops          BIGINT,               -- XRP only; NULL for IOU/MPT
    amount_json           JSONB NOT NULL,
    finish_after          BIGINT,               -- ripple-time seconds
    cancel_after          BIGINT,               -- ripple-time seconds
    condition_present     BOOLEAN NOT NULL,
    previous_txn_id       TEXT,
    previous_txn_lgr_seq  BIGINT,
    snapshot_ledger_index BIGINT,
    fetched_at            BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS escrows_snapshot_finish_after_idx
    ON escrows_snapshot (finish_after ASC);
CREATE INDEX IF NOT EXISTS escrows_snapshot_denom_idx
    ON escrows_snapshot (denom);
CREATE INDEX IF NOT EXISTS escrows_snapshot_owner_idx
    ON escrows_snapshot (owner);

-- oracles_snapshot: per-Oracle-object rows from oracle_walker (XLS-47
-- PriceOracle). v1 seed = DIA (rP24...) — the one production provider
-- currently publishing to mainnet. Full ledger walk 2026-07-02 covered
-- ~10% of state and found only hobby providers (NexusESP32 microcontroller,
-- threexrp, ripitlabs, Ctrl Alt) — signal quality justifies curated scope
-- over XRPLWin's raw directory. If a new production provider ever needs
-- adding, drop it in named_accounts.json with category="oracle" and the
-- walker picks it up next cycle.
-- price_data_json is a pre-decoded list [{base, quote, price_raw_hex,
-- scale, price_float}] so the read layer and template do no math.
CREATE TABLE IF NOT EXISTS oracles_snapshot (
    ledger_index_hash     TEXT PRIMARY KEY,     -- Oracle object's `index`
    owner                 TEXT NOT NULL,
    owner_name            TEXT,
    document_id           BIGINT,               -- OracleDocumentID (uint32)
    provider              TEXT,                 -- decoded from hex ASCII
    uri                   TEXT,                 -- decoded, optional
    asset_class           TEXT,                 -- decoded, optional
    last_update_time      BIGINT,               -- Unix seconds (XLS-47)
    price_data_json       JSONB NOT NULL,
    pair_count            INTEGER NOT NULL,
    previous_txn_id       TEXT,
    previous_txn_lgr_seq  BIGINT,
    snapshot_ledger_index BIGINT,
    fetched_at            BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS oracles_snapshot_owner_idx
    ON oracles_snapshot (owner);
CREATE INDEX IF NOT EXISTS oracles_snapshot_last_update_idx
    ON oracles_snapshot (last_update_time DESC);

-- On-site institutional contact form submissions. The prior mailto:-only
-- CTA lost visitors on mobile (no mail app), corporate lockdowns, and
-- webmail users (21 clicks, 0 emails received May-Jun 2026). This table
-- backs the /institutional/contact form so submissions land server-side
-- regardless of the sender's mail-client posture. visitor_hash reuses
-- the same day-bucketed HMAC as page_views for optional cross-signal.
CREATE TABLE IF NOT EXISTS institutional_inquiries (
    id            BIGSERIAL PRIMARY KEY,
    ts            BIGINT NOT NULL,
    name          TEXT,
    email         TEXT NOT NULL,
    org           TEXT,
    best_time     TEXT,
    message       TEXT NOT NULL,
    ref_param     TEXT,
    referrer      TEXT,
    visitor_hash  TEXT,
    user_agent    TEXT,
    country       TEXT,
    email_alerted BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS institutional_inquiries_ts_idx
    ON institutional_inquiries (ts DESC);

-- General on-site contact form (bug reports, feedback, questions,
-- corrections). Complements institutional_inquiries (which is qualified
-- sales leads only). Same landing model: mailto: lost visitors on mobile
-- (no mail app), corporate lockdowns, and webmail-only users; the form
-- catches submissions server-side regardless of the sender's mail-client
-- posture. purpose enumerates the CTA source so click analytics + inbox
-- routing can segment by intent (bug-report, donation, general,
-- learn-feedback, verify-attestation, rwa-attestation, subprocessor-404,
-- methodology-discrepancy, data-correction, institutional-general).
CREATE TABLE IF NOT EXISTS contact_inquiries (
    id            BIGSERIAL PRIMARY KEY,
    ts            BIGINT NOT NULL,
    purpose       TEXT NOT NULL,
    name          TEXT,
    email         TEXT NOT NULL,
    message       TEXT NOT NULL,
    ref_param     TEXT,
    referrer      TEXT,
    visitor_hash  TEXT,
    user_agent    TEXT,
    country       TEXT,
    email_alerted BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS contact_inquiries_ts_idx
    ON contact_inquiries (ts DESC);
CREATE INDEX IF NOT EXISTS contact_inquiries_purpose_idx
    ON contact_inquiries (purpose);

-- NFT tables — back /nfts (Fable funnel + active/quiet + churn badge).
-- All figures counted from a cutoff ledger forward. True full-history
-- existing count lives in a separate stock table (nft_existing_snapshot);
-- the funnel flow numbers here are strictly since-cutoff. Two truths,
-- surfaced separately on the page — never blended. Cutoff configured at
-- walker start via nft_walker_state.backfill_target (immutable once set).
--
-- Per-tx append log. One row per NFT tx we observe inside the window.
-- Unique tx_hash makes ingest idempotent so re-runs (backfill overlap,
-- retry after node error) can't dupe rows. raw JSONB kept through v1
-- for re-derivation without re-fetching Clio; v2 disk optimization
-- decides whether to drop it once extraction is settled.
CREATE TABLE IF NOT EXISTS nft_activity (
    id              BIGSERIAL PRIMARY KEY,
    ledger_index    BIGINT       NOT NULL,
    close_time      TIMESTAMPTZ  NOT NULL,   -- ledger close_time, not ingest
    tx_hash         TEXT         NOT NULL UNIQUE,
    tx_type         TEXT         NOT NULL,   -- Mint | Burn | CreateOffer |
                                             --   AcceptOffer | CancelOffer
    nftoken_id      TEXT,                    -- null on some pre-Accept offers
    issuer          TEXT,                    -- decoded from nftoken_id
    taxon           BIGINT,                  -- decoded from nftoken_id
    buyer           TEXT,                    -- AcceptOffer only
    seller          TEXT,                    -- AcceptOffer only
    price_drops     BIGINT,                  -- XRP-denominated sales
    currency        TEXT,                    -- 'XRP' or issued-currency code
    currency_issuer TEXT,                    -- null for XRP
    is_broker       BOOLEAN,                 -- AcceptOffer w/ Broker present
    raw             JSONB                    -- full tx for later re-derivation
);
CREATE INDEX IF NOT EXISTS nft_activity_collection_time_idx
    ON nft_activity (issuer, taxon, close_time);
CREATE INDEX IF NOT EXISTS nft_activity_type_time_idx
    ON nft_activity (tx_type, close_time);
CREATE INDEX IF NOT EXISTS nft_activity_nftoken_idx
    ON nft_activity (nftoken_id);

-- Rolled-up per-collection stats. Recomputed by walker --mode rollup from
-- nft_activity. net_minted_since_cutoff (mints - burns since the cutoff
-- ledger) is a flow — NOT true existing NFT count. True stock lives in
-- nft_existing_snapshot. The name carries the truth so downstream code
-- can't misread it. Two numbers, two labels, never blended.
CREATE TABLE IF NOT EXISTS nft_collection_stats (
    issuer                    TEXT   NOT NULL,
    taxon                     BIGINT NOT NULL,
    mints_total               BIGINT NOT NULL DEFAULT 0,   -- since cutoff
    burns_total               BIGINT NOT NULL DEFAULT 0,   -- since cutoff
    net_minted_since_cutoff   BIGINT NOT NULL DEFAULT 0,   -- FLOW, not stock
    sales_30d                 BIGINT NOT NULL DEFAULT 0,
    distinct_buyers_30d       BIGINT NOT NULL DEFAULT 0,
    distinct_sellers_30d      BIGINT NOT NULL DEFAULT 0,
    last_sale_at              TIMESTAMPTZ,
    floor_bands_json          JSONB,                       -- top-5 asks snap
    churn_metrics_json        JSONB,                       -- intra-collection
    is_active                 BOOLEAN NOT NULL DEFAULT FALSE, -- buyers_30d≥5
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (issuer, taxon)
);
CREATE INDEX IF NOT EXISTS nft_collection_stats_active_sales_idx
    ON nft_collection_stats (is_active, sales_30d DESC);

-- Walker cursor. Single row keyed by walker_name (only 'nft_activity' for
-- now, but scoped so a future --mode ledger-index or per-issuer walker
-- can share the shape). backfill_target is set once on first run and
-- must never change — mutating it retroactively would drift the cutoff
-- label. backfill_ledger walks DOWN from cursor_ledger's start toward
-- backfill_target; NULL when backfill is complete.
CREATE TABLE IF NOT EXISTS nft_walker_state (
    walker_name       TEXT PRIMARY KEY,
    cursor_ledger     BIGINT NOT NULL,   -- next ledger to ingest (forward)
    backfill_ledger   BIGINT,            -- next ledger to backfill (down)
    backfill_target   BIGINT,            -- cutoff ledger; immutable once set
    last_run_at       TIMESTAMPTZ,
    last_success_at   TIMESTAMPTZ
);

-- One-time (initially) full-state count via Clio ledger_data(NFTokenPage).
-- Refresh cadence stays MANUAL until change-rate observed for ~2 weeks —
-- verify-before-automating discipline. This is the STOCK number (true
-- existing NFTs on ledger right now), separate from the funnel FLOW.
CREATE TABLE IF NOT EXISTS nft_existing_snapshot (
    snapshot_at          TIMESTAMPTZ PRIMARY KEY,
    ledger_index         BIGINT  NOT NULL,
    total_existing_nfts  BIGINT  NOT NULL,
    total_pages_walked   BIGINT  NOT NULL,
    walk_duration_sec    INTEGER NOT NULL,
    source               TEXT    NOT NULL   -- e.g. 's2-clio.ripple.com'
);

-- Per-hour transaction-type counters populated by tx_type_bucket_handler
-- in xrpl_stream.py. Feeds the "Ledger activity" section on /network:
-- what share of on-chain activity is Payments vs DEX offers vs AMM vs
-- NFT ops vs TrustSet vs Escrow/etc. Count-only — no dollar value. The
-- data-side counterpart of the count-not-value discipline we already
-- apply on /tokens. Rolling windows (24h/7d/30d) sum hour buckets.
CREATE TABLE IF NOT EXISTS tx_type_hourly (
    tx_type      TEXT   NOT NULL,
    hour_bucket  BIGINT NOT NULL,
    count        BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (tx_type, hour_bucket)
);
CREATE INDEX IF NOT EXISTS tx_type_hourly_bucket_idx
    ON tx_type_hourly (hour_bucket DESC);
"""


# ─────────────────────────────────────────────────────────────────────
# Configuration / availability
# ─────────────────────────────────────────────────────────────────────

def pg_url():
    """Return the DATABASE_URL value, or None if unset/blank."""
    val = os.environ.get("DATABASE_URL", "").strip()
    return val or None


def pg_available():
    """True iff DATABASE_URL is set AND psycopg is importable. Cheap to
    call from request hot paths — no network I/O, no caching needed."""
    return bool(pg_url()) and psycopg is not None


@contextmanager
def pg_connect():
    """Context-managed psycopg connection for short-lived (request-scope)
    work. Raises RuntimeError when Postgres isn't configured — callers
    should gate with pg_available() or wrap in a try/except."""
    if not pg_available():
        raise RuntimeError(
            "Postgres not configured: set DATABASE_URL and install psycopg[binary]."
        )
    conn = psycopg.connect(pg_url())
    try:
        yield conn
    finally:
        conn.close()


def init_schema():
    """Apply SCHEMA_DDL to the configured Postgres. Idempotent — safe to
    re-run. Run once after provisioning the DB:
        DATABASE_URL=... python3 -c 'from db import init_schema; init_schema()'
    """
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_DDL)
        conn.commit()


# ─────────────────────────────────────────────────────────────────────
# Worker-side: cached long-lived writer connection
# ─────────────────────────────────────────────────────────────────────

_writer_conn = None


def _get_writer_conn():
    """Lazily open / cache the worker writer connection. Returns None
    when Postgres isn't configured. On connection failure, returns None
    and clears the cache so the next call retries."""
    global _writer_conn
    if not pg_available():
        return None
    if _writer_conn is not None:
        return _writer_conn
    try:
        # Prefer DATABASE_URL_DIRECT (Neon unpooled endpoint) when set —
        # this is the long-lived cached conn, and PgBouncer transaction
        # mode rebalances server conns between client transactions.
        # Today's writes are all autocommit single-statement so it works
        # through the pooler, but adding SET LOCAL / prepared statements
        # would silently break. Falls back to DATABASE_URL if unset.
        # connect_timeout caps a single connect attempt; TCP keepalives let
        # a half-dead Neon connection surface as an error instead of
        # blocking the worker indefinitely (root cause of the 2026-05-08
        # wedge: socket sat in CLOSE_WAIT while the worker mutex parked).
        writer_url = os.environ.get("DATABASE_URL_DIRECT", "").strip() or pg_url()
        _writer_conn = psycopg.connect(
            writer_url,
            autocommit=True,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
    except Exception as e:
        _log_err("writer_connect_failed", e)
        _writer_conn = None
    return _writer_conn


def _drop_writer_conn():
    """Force the cached writer connection to be re-opened on next use.
    Called after a write error so a stale/dead socket doesn't poison
    every subsequent call."""
    global _writer_conn
    try:
        if _writer_conn is not None:
            _writer_conn.close()
    except Exception:
        pass
    _writer_conn = None


# ─────────────────────────────────────────────────────────────────────
# Worker-side: write helpers (silent no-ops when DATABASE_URL unset)
# ─────────────────────────────────────────────────────────────────────

def write_event(
    tx_hash, ledger_index, ts, type_, from_addr, to_addr,
    amount_drops, currency, issuer, raw_json,
):
    """Mirror an events.db row to Postgres. Silent no-op when PG isn't
    configured. Errors drop the cached connection so the next call
    reconnects — never raises to the caller (worker SQLite write must
    not be blocked by a flaky Postgres)."""
    conn = _get_writer_conn()
    if conn is None:
        return
    if not isinstance(raw_json, str):
        raw_json = json.dumps(raw_json, default=str)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO events "
                "(tx_hash, ledger_index, ts, type, from_addr, to_addr, "
                " amount_drops, currency, issuer, raw_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (tx_hash) DO NOTHING",
                (tx_hash, ledger_index, ts, type_, from_addr, to_addr,
                 amount_drops, currency, issuer, raw_json),
            )
    except Exception as e:
        _log_err("write_event_failed", e)
        _drop_writer_conn()


def upsert_token_volume(currency, issuer, hour_bucket, trade_delta=1, volume_xrp_delta=0.0):
    """Increment trade_count and volume_xrp for a (currency, issuer, hour_bucket)
    bucket. volume_xrp_delta defaults to 0.0 for callers that don't have a
    priced value (AMM deposit/withdraw, or Payment for a token with no XRP
    pool above the token_prices dust gate). Silent no-op when PG isn't
    configured."""
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO token_volume "
                "(currency, issuer, hour_bucket, volume_xrp, trade_count) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (currency, issuer, hour_bucket) DO UPDATE "
                "SET trade_count = token_volume.trade_count + EXCLUDED.trade_count, "
                "    volume_xrp = token_volume.volume_xrp + EXCLUDED.volume_xrp",
                (currency, issuer, hour_bucket, volume_xrp_delta, trade_delta),
            )
    except Exception as e:
        _log_err("upsert_token_volume_failed", e)
        _drop_writer_conn()


def upsert_tx_type_counts(deltas):
    """Increment tx_type_hourly counters from an in-memory batch.

    `deltas` is a dict {(tx_type, hour_bucket): count}. Handler batches
    counts in RAM for 30-60s then flushes here in one round-trip using
    executemany + ON CONFLICT DO UPDATE. Silent no-op when PG isn't
    configured. Individual row failure logged via _log_err rate limiter.
    """
    if not deltas:
        return
    conn = _get_writer_conn()
    if conn is None:
        return
    rows = [(t, b, c) for (t, b), c in deltas.items() if c > 0]
    if not rows:
        return
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tx_type_hourly (tx_type, hour_bucket, count) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (tx_type, hour_bucket) DO UPDATE "
                "SET count = tx_type_hourly.count + EXCLUDED.count",
                rows,
            )
    except Exception as e:
        _log_err("upsert_tx_type_counts_failed", e)
        _drop_writer_conn()


def read_tx_type_counts(hours_back):
    """Return {tx_type: total_count} summed over the last `hours_back`
    hour buckets, plus (earliest_bucket, latest_bucket, distinct_buckets)
    so callers can render an honest window label.

    Returns ({}, None, None, 0) when PG isn't configured or on error —
    the caller renders the "collecting data" placeholder instead of
    silently showing zeros.
    """
    if not pg_available():
        return {}, None, None, 0
    cutoff = int(time.time() // 3600) - int(hours_back)
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tx_type, SUM(count) FROM tx_type_hourly "
                "WHERE hour_bucket >= %s GROUP BY tx_type",
                (cutoff,),
            )
            counts = {row[0]: int(row[1] or 0) for row in cur.fetchall()}
            cur.execute(
                "SELECT MIN(hour_bucket), MAX(hour_bucket), "
                "       COUNT(DISTINCT hour_bucket) FROM tx_type_hourly "
                "WHERE hour_bucket >= %s",
                (cutoff,),
            )
            row = cur.fetchone() or (None, None, 0)
            return counts, row[0], row[1], int(row[2] or 0)
    except Exception as e:
        _log_err("read_tx_type_counts_failed", e)
        return {}, None, None, 0


def read_tx_type_first_bucket():
    """Earliest hour_bucket ever recorded in tx_type_hourly, or None.
    Used to render the honest "since [date]" label while windows have
    not fully accrued yet."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT MIN(hour_bucket) FROM tx_type_hourly")
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        _log_err("read_tx_type_first_bucket_failed", e)
        return None


def write_amm_pool_event(ts, amm_account, event_type, magnitude_xrp_drops=None):
    """Append a row to the amm_pool_events ring buffer in Postgres.
    Silent no-op when PG isn't configured."""
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO amm_pool_events "
                "(ts, amm_account, event_type, magnitude_xrp_drops) "
                "VALUES (%s, %s, %s, %s)",
                (ts, amm_account, event_type, magnitude_xrp_drops),
            )
    except Exception as e:
        _log_err("write_amm_pool_event_failed", e)
        _drop_writer_conn()


def write_heartbeat(worker, txns_seen=None, last_ledger=None, extra=None):
    """Stamp a heartbeat row for `worker` (e.g. 'xrpl_stream'). Used by
    Flask /health on Render to verify the Mac-hosted worker is alive,
    since file-mtime liveness checks don't cross machines."""
    conn = _get_writer_conn()
    if conn is None:
        return
    extra_json = json.dumps(extra, default=str) if extra is not None else None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker, ts, txns_seen, last_ledger, extra) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (worker) DO UPDATE SET "
                "  ts = EXCLUDED.ts, "
                "  txns_seen = EXCLUDED.txns_seen, "
                "  last_ledger = EXCLUDED.last_ledger, "
                "  extra = EXCLUDED.extra",
                (worker, int(time.time()), txns_seen, last_ledger, extra_json),
            )
    except Exception as e:
        _log_err(f"write_heartbeat_failed[{worker}]", e)
        _drop_writer_conn()


def read_heartbeat(worker):
    """Return dict {ts, txns_seen, last_ledger, extra} for `worker`,
    or None if missing / Postgres unavailable. Best-effort: never raises."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ts, txns_seen, last_ledger, extra "
                    "FROM worker_heartbeat WHERE worker = %s",
                    (worker,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "ts": int(row[0]),
                    "txns_seen": row[1],
                    "last_ledger": row[2],
                    "extra": row[3],
                }
    except Exception:
        return None


def read_heartbeat_prefix(prefix):
    """Return the freshest heartbeat row whose worker key starts with `prefix`,
    in the same shape as read_heartbeat. Host-tagged keys (xrpl_stream:mac,
    future :render) need this — a literal-key read picks up whichever orphan
    row was last written under the bare key and silently goes stale forever
    after the writer is renamed."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ts, txns_seen, last_ledger, extra "
                    "FROM worker_heartbeat WHERE worker LIKE %s "
                    "ORDER BY ts DESC LIMIT 1",
                    (prefix + "%",),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "ts": int(row[0]),
                    "txns_seen": row[1],
                    "last_ledger": row[2],
                    "extra": row[3],
                }
    except Exception:
        return None


def replace_amm_ranked_pools(rows):
    """Atomically swap the entire amm_ranked_pools table for `rows`.

    Mirrors the file-level snapshot semantics of amm_ranked.json: rank_amms.py
    rewrites the whole file at each SAVE_EVERY checkpoint, so we do the same
    here. Wrapped in a transaction so /pools readers never observe an empty
    table mid-swap (Postgres READ COMMITTED keeps them on the prior snapshot
    until COMMIT). Silent no-op when PG isn't configured.

    `rows` is the in-memory ranked list (list of dicts in the same shape as
    amm_ranked.json entries). Empty input is treated as "skip" rather than
    "wipe" — it's almost always a bug to push 0 pools to prod.
    """
    if not rows:
        return
    conn = _get_writer_conn()
    if conn is None:
        return
    snapshot_ts = int(time.time())
    payload = [
        (
            r.get("amm_account"),
            r.get("pair") or "?",
            r.get("fee_pct"),
            r.get("fee_raw"),
            r.get("amount_a"),
            r.get("amount_b"),
            json.dumps(r.get("asset_a"), default=str) if r.get("asset_a") is not None else None,
            json.dumps(r.get("asset_b"), default=str) if r.get("asset_b") is not None else None,
            r.get("tvl_usd"),
            r.get("tvl_status"),
            r.get("kind"),
            r.get("lp_token_value"),
            snapshot_ts,
        )
        for r in rows
    ]
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("DELETE FROM amm_ranked_pools")
                cur.executemany(
                    "INSERT INTO amm_ranked_pools "
                    "(amm_account, pair, fee_pct, fee_raw, amount_a, amount_b, "
                    " asset_a, asset_b, tvl_usd, tvl_status, kind, "
                    " lp_token_value, snapshot_ts) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, "
                    " %s, %s, %s, %s, %s)",
                    payload,
                )
    except Exception as e:
        _log_err("replace_amm_ranked_pools_failed", e)
        _drop_writer_conn()


def write_token_prices(rows):
    """Append a new snapshot of token prices. PK is
    (currency, issuer, snapshot_ts) — every walker cycle adds one row per
    token, preserving prior days as historical record. Readers must select
    the latest row per token via DISTINCT ON / ORDER BY snapshot_ts DESC
    (see read_token_prices_map and read_token_price). ON CONFLICT DO
    NOTHING handles same-second collisions if the walker is re-run
    manually within one second. Empty input is a no-op."""
    if not rows:
        return 0
    conn = _get_writer_conn()
    if conn is None:
        return 0
    payload = [
        (
            r["currency"],
            r["issuer"],
            r["snapshot_ts"],
            r["xrp_price"],
            r["pool_amm_account"],
            r["pool_xrp_reserve"],
            r["pool_token_reserve"],
            r["derivation_method"],
        )
        for r in rows
    ]
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO token_prices "
                    "(currency, issuer, snapshot_ts, xrp_price, "
                    " pool_amm_account, pool_xrp_reserve, "
                    " pool_token_reserve, derivation_method) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    payload,
                )
        return len(rows)
    except Exception as e:
        _log_err("write_token_prices_failed", e)
        _drop_writer_conn()
        return 0


def read_token_prices_map():
    """Return {(currency, issuer): xrp_price} for the LATEST row per token.
    token_prices is history-append (PK includes snapshot_ts), so a naive
    SELECT returns every snapshot's rows. DISTINCT ON collapses to one
    row per (currency, issuer) using the most recent snapshot_ts.
    Backed by token_prices_latest_idx for index-only scan. Returns {}
    when PG isn't configured or the table is empty."""
    if not pg_available():
        return {}
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT ON (currency, issuer) "
                    "  currency, issuer, xrp_price "
                    "FROM token_prices "
                    "ORDER BY currency, issuer, snapshot_ts DESC"
                )
                return {(c, i): float(p) for (c, i, p) in cur.fetchall()}
    except Exception as e:
        _log_err("read_token_prices_map_failed", e)
        return {}


def read_token_price(currency, issuer):
    """Single-row variant for the token-detail page. Returns the latest
    XRP price for one token, or None when the token has no row (no XRP
    pool above floor). ORDER BY snapshot_ts DESC LIMIT 1 picks the most
    recent snapshot from the history-append table."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT xrp_price FROM token_prices "
                    "WHERE currency = %s AND issuer = %s "
                    "ORDER BY snapshot_ts DESC LIMIT 1",
                    (currency, issuer),
                )
                row = cur.fetchone()
                return float(row[0]) if row else None
    except Exception as e:
        _log_err("read_token_price_failed", e)
        return None


def write_unl_snapshot(source, payload, fetched_at_iso, snapshot_date=None):
    """Upsert one daily UNL row for `source` ("ripple" | "xrplf"). Idempotent
    on (source, snapshot_date) — same-day re-runs overwrite the prior row
    rather than appending. snapshot_date defaults to today (UTC). Returns
    1 on success, 0 on no-op (PG unavailable or insert failed)."""
    if not pg_available():
        return 0
    if snapshot_date is None:
        snapshot_date = datetime.datetime.now(datetime.timezone.utc).date()
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO unl_snapshots "
                    "(source, snapshot_date, payload, fetched_at_iso) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (source, snapshot_date) DO UPDATE SET "
                    "  payload = EXCLUDED.payload, "
                    "  fetched_at_iso = EXCLUDED.fetched_at_iso",
                    (source, snapshot_date, json.dumps(payload, default=str),
                     fetched_at_iso),
                )
            conn.commit()
        return 1
    except Exception as e:
        _log_err("write_unl_snapshot_failed", e)
        return 0


def read_recent_unl_snapshots(source, limit=30):
    """Return the most-recent `limit` snapshots for `source`, newest first.
    Each row: {"snapshot_date": "YYYY-MM-DD", "payload": {...},
    "fetched_at_iso": "..."}. Returns [] when PG unavailable or no rows
    (cold-start before the first daily run)."""
    if not pg_available():
        return []
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT snapshot_date, payload, fetched_at_iso "
                    "FROM unl_snapshots WHERE source = %s "
                    "ORDER BY snapshot_date DESC LIMIT %s",
                    (source, limit),
                )
                rows = cur.fetchall()
        return [
            {
                "snapshot_date": d.isoformat() if d else None,
                "payload": p,
                "fetched_at_iso": f,
            }
            for (d, p, f) in rows
        ]
    except Exception as e:
        _log_err("read_recent_unl_snapshots_failed", e)
        return []


def read_amm_ranked_pools():
    """Return the ranked-pools snapshot as a list of dicts in the same shape
    as amm_ranked.json — so app.py and templates don't care whether the
    source was the file or Postgres. Returns [] when PG is unavailable or
    the table is empty (caller falls back to the file)."""
    if not pg_available():
        return []
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT amm_account, pair, fee_pct, fee_raw, "
                    "       amount_a, amount_b, asset_a, asset_b, "
                    "       tvl_usd, tvl_status, kind, snapshot_ts "
                    "FROM amm_ranked_pools"
                )
                return [
                    {
                        "amm_account": r[0],
                        "pair": r[1],
                        "fee_pct": r[2],
                        "fee_raw": r[3],
                        "amount_a": r[4],
                        "amount_b": r[5],
                        "asset_a": r[6],
                        "asset_b": r[7],
                        "tvl_usd": r[8],
                        "tvl_status": r[9],
                        "kind": r[10],
                        "_snapshot_ts": r[11],
                    }
                    for r in cur.fetchall()
                ]
    except Exception:
        return []


def read_amm_index_entries():
    """Mirror of amm_index.json for the wallet/token detail layers.

    amm_index.json is gitignored (built by the Mac worker), so the file
    is absent on Render and any code that loads it at import time sees
    an empty list — silent zero, identical bug class to volumes.db.

    Returns a list of dicts shaped like amm_index entries
    (Account / Asset / Asset2 / TradingFee / LPTokenBalance) sourced
    from the amm_ranked_pools snapshot. LPTokenBalance.value carries
    the real amm_info `lp_token.value` (the issued LP token supply for
    the pool); callers may use it for sorting or display.

    Pools written before the lp_token_value column was backfilled have
    NULL — those entries are skipped here to avoid downstream "0 LP"
    mislabels. Once one full rank_amms cycle has run, all rows carry
    the real value.

    Returns None when Postgres isn't configured — signals the caller
    to fall back to the local file (dev / Mac path). RAISES on PG
    query failure: production should fail loudly rather than render
    an empty AMM index, per the volumes.db post-mortem.
    """
    if not pg_available():
        return None
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT amm_account, asset_a, asset_b, fee_raw, lp_token_value "
                "FROM amm_ranked_pools"
            )
            rows = cur.fetchall()
    out = []
    for amm_acct, asset_a, asset_b, fee_raw, lp_token_value in rows:
        if not amm_acct:
            continue
        if lp_token_value is None:
            continue
        a = asset_a or {}
        b = asset_b or {}
        a_cur = a.get("currency")
        b_cur = b.get("currency")
        if not a_cur or not b_cur:
            continue
        Asset = {"currency": a_cur}
        if a.get("issuer"):
            Asset["issuer"] = a["issuer"]
        Asset2 = {"currency": b_cur}
        if b.get("issuer"):
            Asset2["issuer"] = b["issuer"]
        out.append({
            "Account": amm_acct,
            "Asset": Asset,
            "Asset2": Asset2,
            "TradingFee": int(fee_raw or 0),
            "LPTokenBalance": {"value": str(lp_token_value)},
        })
    return out


def read_amm_snapshot_ts():
    """Return the snapshot_ts of the most-recent amm_ranked_pools write
    (all rows in a snapshot share the same ts), or None when empty/unavailable.
    Used by /pools for the freshness label in lieu of file mtime."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(snapshot_ts) FROM amm_ranked_pools")
                row = cur.fetchone()
                return int(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def write_mpt_snapshot(data):
    """Mirror mpt_snapshot.json into Postgres so Render can serve /mpts
    without doing a 10-minute ledger walk on cold request. Single-row
    table; UPSERT on the fixed id=1. Silent no-op when PG isn't configured.

    `data` is the full snapshot dict (issuances, by_class, total, etc.) —
    we round-trip it as JSONB and Flask reads it back into the same shape.
    Empty/unsuccessful payloads are skipped: pushing ok=False would mask
    a still-good prior snapshot."""
    if not data or not data.get("ok"):
        return
    conn = _get_writer_conn()
    if conn is None:
        return
    written_at = int(data.get("written_at") or time.time())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mpt_snapshot (id, payload, written_at) "
                "VALUES (1, %s::jsonb, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "    payload = EXCLUDED.payload, "
                "    written_at = EXCLUDED.written_at",
                (json.dumps(data, default=str), written_at),
            )
    except Exception as e:
        _log_err("write_mpt_snapshot_failed", e)
        _drop_writer_conn()


def read_mpt_snapshot():
    """Return the MPT snapshot dict from Postgres, or None when PG is
    unavailable / table empty. Adds `snapshot_age_seconds` and a
    `from_postgres` flag so the template can label the freshness source."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload, written_at FROM mpt_snapshot WHERE id = 1"
                )
                row = cur.fetchone()
                if not row:
                    return None
                payload, written_at = row
                if not isinstance(payload, dict):
                    return None
                payload["snapshot_age_seconds"] = round(
                    time.time() - int(written_at), 1
                )
                payload["from_snapshot"] = True
                payload["from_postgres"] = True
                return payload
    except Exception:
        return None


def write_credentials_snapshot(data):
    """Persist the /credentials state blob so every gunicorn worker
    (and any future standalone walker) reads the same view. Silent no-op
    when PG isn't configured. Empty payloads are dropped: never overwrite
    a good snapshot with one that lacks any successful component."""
    if not data:
        return
    if not (data.get("amendment") or data.get("cumulative") or data.get("recent")):
        return
    conn = _get_writer_conn()
    if conn is None:
        return
    written_at = int(data.get("written_at") or time.time())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO credentials_snapshot (id, payload, written_at) "
                "VALUES (1, %s::jsonb, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "    payload = EXCLUDED.payload, "
                "    written_at = EXCLUDED.written_at",
                (json.dumps(data, default=str), written_at),
            )
    except Exception as e:
        _log_err("write_credentials_snapshot_failed", e)
        _drop_writer_conn()


def read_credentials_snapshot():
    """Return the credentials snapshot dict from Postgres, or None when
    PG is unavailable / table empty. Adds `snapshot_age_seconds` so the
    template can show freshness."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload, written_at FROM credentials_snapshot WHERE id = 1"
                )
                row = cur.fetchone()
                if not row:
                    return None
                payload, written_at = row
                if not isinstance(payload, dict):
                    return None
                payload["snapshot_age_seconds"] = round(
                    time.time() - int(written_at), 1
                )
                payload["written_at"] = int(written_at)
                payload["from_postgres"] = True
                return payload
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# Walker health — every scheduled walker writes a start row at the top
# of its run and updates the same row with the outcome at the end via
# try/finally. Lets /mpts and future per-walker pages render a stale
# banner instead of silently serving last-good when the worker has been
# failing for weeks. Pattern triggered by 2026-05-27 mpt_snapshot
# investigation (11 of last 13 walks silently failed).
# ─────────────────────────────────────────────────────────────────────


def write_walker_health_start(walker_name, cadence_seconds=None):
    """UPSERT walker_health at the top of a walker run. Sets
    last_run_started=now() and last_run_ok=False as a defensive default
    so an uncaught crash before the end-of-run write still shows as a
    failure to the reader (rather than the prior run's ok=True).
    consecutive_failures is NOT touched here; the end-of-run write owns it.

    cadence_seconds: walker's self-declared expected run frequency
    (mirrors its launchd plist StartInterval). When provided, /walker_health
    uses it to compute per-row staleness thresholds. None leaves the
    existing value untouched (lets a partial rollout coexist with already-
    declared walkers).

    Silent no-op when PG isn't configured."""
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO walker_health "
                "  (walker_name, last_run_started, last_run_ok, cadence_seconds) "
                "VALUES (%s, now(), false, %s) "
                "ON CONFLICT (walker_name) DO UPDATE SET "
                "  last_run_started = EXCLUDED.last_run_started, "
                "  last_run_ok = false, "
                "  last_run_completed = NULL, "
                "  last_run_message = NULL, "
                "  cadence_seconds = COALESCE(EXCLUDED.cadence_seconds, walker_health.cadence_seconds)",
                (walker_name, cadence_seconds),
            )
    except Exception as e:
        _log_err(f"write_walker_health_start_failed[{walker_name}]", e)
        _drop_writer_conn()


def write_walker_health_end(walker_name, ok, message=None):
    """Update walker_health with the run outcome. On ok=True: stamps
    last_success_at=now() and zeroes consecutive_failures. On ok=False:
    stamps last_failure_at=now() and increments consecutive_failures.
    The row must already exist (start-of-run write created it); if not,
    we still UPSERT so a walker that forgot to call start isn't invisible.
    Silent no-op when PG isn't configured."""
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            if ok:
                cur.execute(
                    "INSERT INTO walker_health "
                    "  (walker_name, last_run_started, last_run_completed, "
                    "   last_run_ok, last_run_message, last_success_at, "
                    "   consecutive_failures) "
                    "VALUES (%s, now(), now(), true, %s, now(), 0) "
                    "ON CONFLICT (walker_name) DO UPDATE SET "
                    "  last_run_completed = now(), "
                    "  last_run_ok = true, "
                    "  last_run_message = EXCLUDED.last_run_message, "
                    "  last_success_at = now(), "
                    "  consecutive_failures = 0",
                    (walker_name, message),
                )
            else:
                cur.execute(
                    "INSERT INTO walker_health "
                    "  (walker_name, last_run_started, last_run_completed, "
                    "   last_run_ok, last_run_message, last_failure_at, "
                    "   consecutive_failures) "
                    "VALUES (%s, now(), now(), false, %s, now(), 1) "
                    "ON CONFLICT (walker_name) DO UPDATE SET "
                    "  last_run_completed = now(), "
                    "  last_run_ok = false, "
                    "  last_run_message = EXCLUDED.last_run_message, "
                    "  last_failure_at = now(), "
                    "  consecutive_failures = walker_health.consecutive_failures + 1",
                    (walker_name, message),
                )
    except Exception as e:
        _log_err(f"write_walker_health_end_failed[{walker_name}]", e)
        _drop_writer_conn()


def read_walker_health_all():
    """Return every walker_health row as a list of dicts (alphabetical by
    walker_name). Each row includes `last_success_age_seconds` and
    `cadence_seconds` so /walker_health can compute per-row severity
    without a second query. Returns [] when PG is unavailable."""
    if not pg_available():
        return []
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT walker_name, last_run_started, last_run_completed, "
                    "       last_run_ok, last_run_message, "
                    "       last_success_at, last_failure_at, "
                    "       consecutive_failures, cadence_seconds "
                    "  FROM walker_health "
                    "  ORDER BY walker_name"
                )
                rows = cur.fetchall()
                now = time.time()
                out = []
                for row in rows:
                    last_success_at = row[5]
                    age_secs = (
                        now - last_success_at.timestamp()
                        if last_success_at is not None else None
                    )
                    out.append({
                        "walker_name": row[0],
                        "last_run_started": row[1],
                        "last_run_completed": row[2],
                        "last_run_ok": row[3],
                        "last_run_message": row[4],
                        "last_success_at": last_success_at,
                        "last_failure_at": row[6],
                        "consecutive_failures": row[7],
                        "cadence_seconds": row[8],
                        "last_success_age_seconds": (
                            round(age_secs, 1) if age_secs is not None else None
                        ),
                    })
                return out
    except Exception:
        return []


def read_walker_health(walker_name):
    """Return the walker_health row as a dict, or None when PG is
    unavailable / row missing. Adds `last_success_age_seconds` for
    template staleness checks."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT last_run_started, last_run_completed, "
                    "       last_run_ok, last_run_message, "
                    "       last_success_at, last_failure_at, "
                    "       consecutive_failures "
                    "  FROM walker_health WHERE walker_name = %s",
                    (walker_name,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                last_success_at = row[4]
                age_secs = None
                if last_success_at is not None:
                    age_secs = (
                        time.time() - last_success_at.timestamp()
                    )
                return {
                    "walker_name": walker_name,
                    "last_run_started": row[0],
                    "last_run_completed": row[1],
                    "last_run_ok": row[2],
                    "last_run_message": row[3],
                    "last_success_at": last_success_at,
                    "last_failure_at": row[5],
                    "consecutive_failures": row[6],
                    "last_success_age_seconds": (
                        round(age_secs, 1) if age_secs is not None else None
                    ),
                }
    except Exception:
        return None


def read_latest_bridge_signers():
    """Return the most recent bridge_signer_history row as a dict, or
    None when PG is unavailable / table empty. Used by /sidechain to
    render quorum + signer count + (for uniform weights) the M-of-N
    framing visitors actually understand."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ledger_index, close_time, quorum, signer_count, "
                    "       signer_entries, tx_hash "
                    "  FROM bridge_signer_history "
                    " ORDER BY ledger_index DESC LIMIT 1"
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "ledger_index": row[0],
                    "close_time": row[1],
                    "quorum": row[2],
                    "signer_count": row[3],
                    "signer_entries": row[4],
                    "tx_hash": row[5],
                }
    except Exception:
        return None


def read_bridge_signer_rotations():
    """Return every observed SignerListSet rotation (tx_hash != 'BOOTSTRAP')
    ordered oldest → newest, with the prior row's signer_count and quorum
    attached for diff display. The bootstrap row participates as the
    "previous state" for the first rotation but is not itself returned.

    Empty list when PG unavailable, table empty, or no real rotations
    have been observed yet."""
    if not pg_available():
        return []
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ledger_index, close_time, quorum, signer_count, "
                    "       tx_hash "
                    "  FROM bridge_signer_history "
                    " ORDER BY ledger_index ASC"
                )
                rows = cur.fetchall()
                out = []
                prev = None
                for r in rows:
                    if r[4] == "BOOTSTRAP":
                        prev = (r[3], r[2])
                        continue
                    out.append({
                        "ledger_index": r[0],
                        "close_time": r[1],
                        "quorum": r[2],
                        "signer_count": r[3],
                        "tx_hash": r[4],
                        "prev_signer_count": prev[0] if prev else None,
                        "prev_quorum": prev[1] if prev else None,
                    })
                    prev = (r[3], r[2])
                return out
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────
# /permissioned-domains (XLS-80) — Phase 1: walker writes, no UI yet.
# Append-only history: one row per (snapshot_date, domain_id) and one
# audit row per walker pass in permissioned_domain_walker_runs.
# ─────────────────────────────────────────────────────────────────────


def write_permissioned_domains(rows, fetched_at_iso, snapshot_date):
    """Insert today's snapshot rows into permissioned_domains. Idempotent:
    ON CONFLICT (snapshot_date, domain_id) DO NOTHING so same-day re-runs
    don't duplicate. Silent no-op when PG isn't configured. Empty `rows`
    is valid (zero domains found today)."""
    if not pg_available():
        return
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            for r in rows or []:
                cur.execute(
                    "INSERT INTO permissioned_domains ("
                    "  snapshot_date, domain_id, owner_account, sequence, "
                    "  accepted_credentials, cred_count, previous_txn_id, "
                    "  ledger_close_time, fetched_at_iso"
                    ") VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s) "
                    "ON CONFLICT (snapshot_date, domain_id) DO NOTHING",
                    (
                        snapshot_date,
                        r["domain_id"],
                        r["owner_account"],
                        r["sequence"],
                        json.dumps(r.get("accepted_credentials") or [], default=str),
                        r["cred_count"],
                        r.get("previous_txn_id"),
                        r["ledger_close_time"],
                        fetched_at_iso,
                    ),
                )
        conn.commit()
    except Exception as e:
        _log_err("write_permissioned_domains_failed", e)
        _drop_writer_conn()


def write_permissioned_domain_walker_run(metadata, snapshot_date):
    """Upsert one row per snapshot_date into permissioned_domain_walker_runs.
    Same-day second invocation overwrites with the newer pass (latest
    duration_ms / domains_found wins). Silent no-op when PG isn't configured."""
    if not pg_available():
        return
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO permissioned_domain_walker_runs ("
                "  snapshot_date, fetched_at_iso, seed_set_size, "
                "  accounts_queried, domains_found, exhausted, "
                "  walker_duration_ms, notes"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (snapshot_date) DO UPDATE SET "
                "  fetched_at_iso = EXCLUDED.fetched_at_iso, "
                "  seed_set_size = EXCLUDED.seed_set_size, "
                "  accounts_queried = EXCLUDED.accounts_queried, "
                "  domains_found = EXCLUDED.domains_found, "
                "  exhausted = EXCLUDED.exhausted, "
                "  walker_duration_ms = EXCLUDED.walker_duration_ms, "
                "  notes = EXCLUDED.notes",
                (
                    snapshot_date,
                    metadata["fetched_at_iso"],
                    metadata["seed_set_size"],
                    metadata["accounts_queried"],
                    metadata["domains_found"],
                    metadata["exhausted"],
                    metadata["walker_duration_ms"],
                    metadata.get("notes"),
                ),
            )
        conn.commit()
    except Exception as e:
        _log_err("write_permissioned_domain_walker_run_failed", e)
        _drop_writer_conn()


def read_permissioned_domains_latest():
    """Return the most recent snapshot row per domain_id. Empty list when
    PG unavailable or table empty (which is the expected steady state
    until XLS-80 adoption begins)."""
    if not pg_available():
        return []
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT ON (domain_id) "
                    "  snapshot_date, domain_id, owner_account, sequence, "
                    "  accepted_credentials, cred_count, previous_txn_id, "
                    "  ledger_close_time, fetched_at_iso "
                    "FROM permissioned_domains "
                    "ORDER BY domain_id, snapshot_date DESC"
                )
                cols = [c.name for c in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def read_permissioned_domain_walker_runs(limit=30):
    """Return the most recent walker-run metadata rows, newest first.
    Phase 2's empty-state copy reads off this — last run, scan size,
    duration, exhaustion flag. Empty list when PG unavailable."""
    if not pg_available():
        return []
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT snapshot_date, fetched_at_iso, seed_set_size, "
                    "  accounts_queried, domains_found, exhausted, "
                    "  walker_duration_ms, notes "
                    "FROM permissioned_domain_walker_runs "
                    "ORDER BY snapshot_date DESC LIMIT %s",
                    (int(limit),),
                )
                cols = [c.name for c in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def read_permissioned_domain_events(limit=50, offset=0):
    """Phase 1: returns [] because the events table is intentionally empty
    until Phase 2/3 adds tx-history population. Helper exists now so Phase
    2 templates can import the consumer API without a follow-up change."""
    if not pg_available():
        return []
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tx_hash, tx_type, domain_id, owner_account, "
                    "  ledger_index, ledger_close_time, payload "
                    "FROM permissioned_domain_events "
                    "ORDER BY ledger_close_time DESC "
                    "LIMIT %s OFFSET %s",
                    (int(limit), int(offset)),
                )
                cols = [c.name for c in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def read_rlusd_state_cache():
    """Return (payload_dict, written_at_epoch) or (None, None) when PG is
    unavailable / table empty. Powers the SSR cold-start fallback on /rlusd
    so visitors never see a blank treasury page when the live fetch fails."""
    if not pg_available():
        return None, None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload, written_at FROM rlusd_state_cache WHERE id = 1"
                )
                row = cur.fetchone()
                if row:
                    return row[0], row[1]
    except Exception:
        pass
    return None, None


def write_rlusd_state_cache(payload):
    """Upsert the single-row last-good cache. Called from rlusd_live's
    refresh loop after every successful full-supply build. Best-effort —
    failures stay silent so a PG hiccup never breaks the live refresh."""
    if not pg_available():
        return False
    conn = _get_writer_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rlusd_state_cache (id, payload, written_at) "
                "VALUES (1, %s::jsonb, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "    payload = EXCLUDED.payload, "
                "    written_at = EXCLUDED.written_at",
                (json.dumps(payload, default=str), int(time.time())),
            )
        return True
    except Exception:
        return False


def write_rlusd_supply_history(payload):
    """Append/refresh today's RLUSD supply row in rlusd_supply_history.

    Called from signed_snapshot.collect_metrics() with the same payload
    already read out of rlusd_state_cache for the per-cycle metrics.
    Idempotent on snapshot_date — same-day re-runs UPSERT the latest
    values from the most recent cache refresh.

    snapshot_date and written_at_iso are both derived from
    payload['fetched_at'] (epoch seconds) — the data's own freshness
    timestamp, not now(). See feedback_history_flip_cadence_rule.md
    for the underlying principle.

    Best-effort: returns True on success, False on any failure (PG
    unavailable, payload missing required keys, etc.). Never raises —
    a logging hiccup here must not break the daily snapshot pass.
    """
    if not pg_available():
        return False
    if not isinstance(payload, dict):
        return False
    xrpl_branch = payload.get("xrpl") or {}
    eth_branch = payload.get("eth") or {}
    xrpl_supply = xrpl_branch.get("supply")
    eth_supply = eth_branch.get("supply")
    fetched_at = payload.get("fetched_at")
    if xrpl_supply is None or eth_supply is None or fetched_at is None:
        return False
    try:
        fetched_dt = datetime.datetime.fromtimestamp(
            int(fetched_at), tz=datetime.timezone.utc
        )
    except (TypeError, ValueError, OSError):
        return False
    snapshot_date = fetched_dt.date()
    written_at_iso = fetched_dt.isoformat()
    conn = _get_writer_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rlusd_supply_history ("
                "    snapshot_date, xrpl_supply, eth_supply, total_supply, "
                "    xrpl_holders, eth_holders, "
                "    xrpl_mints_24h, xrpl_burns_24h, "
                "    eth_mints_24h, eth_burns_24h, "
                "    written_at_iso"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (snapshot_date) DO UPDATE SET "
                "    xrpl_supply    = EXCLUDED.xrpl_supply, "
                "    eth_supply     = EXCLUDED.eth_supply, "
                "    total_supply   = EXCLUDED.total_supply, "
                "    xrpl_holders   = EXCLUDED.xrpl_holders, "
                "    eth_holders    = EXCLUDED.eth_holders, "
                "    xrpl_mints_24h = EXCLUDED.xrpl_mints_24h, "
                "    xrpl_burns_24h = EXCLUDED.xrpl_burns_24h, "
                "    eth_mints_24h  = EXCLUDED.eth_mints_24h, "
                "    eth_burns_24h  = EXCLUDED.eth_burns_24h, "
                "    written_at_iso = EXCLUDED.written_at_iso",
                (
                    snapshot_date,
                    float(xrpl_supply),
                    float(eth_supply),
                    float(xrpl_supply) + float(eth_supply),
                    xrpl_branch.get("holders"),
                    eth_branch.get("holders"),
                    xrpl_branch.get("mints_24h"),
                    xrpl_branch.get("burns_24h"),
                    eth_branch.get("mints_24h"),
                    eth_branch.get("burns_24h"),
                    written_at_iso,
                ),
            )
        return True
    except Exception:
        return False


def write_snapshot_meta(meta):
    """Mirror the rollup of historical_snapshots/ into Postgres so Render
    can render the /institutional snapshot strip without access to the
    Mac's local directory. Single-row table; UPSERT on the fixed id=1.

    The payload's keys must match what _historical_snapshot_meta_from_disk()
    returns, because read_snapshot_meta() hands it straight to the template
    (which reads days_collected, accounts_tracked, pools_tracked,
    mpts_tracked, first_date). Renaming any of those breaks the strip."""
    if not meta:
        return
    conn = _get_writer_conn()
    if conn is None:
        return
    written_at = int(meta.get("written_at") or time.time())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO historical_snapshot_meta (id, payload, written_at) "
                "VALUES (1, %s::jsonb, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "    payload = EXCLUDED.payload, "
                "    written_at = EXCLUDED.written_at",
                (json.dumps(meta, default=str), written_at),
            )
    except Exception as e:
        _log_err("write_snapshot_meta_failed", e)
        _drop_writer_conn()


def write_account_snapshots(snapshot_date, rows):
    """Mirror per-account snapshot rows (as returned by daily_snapshot.
    snapshot_accounts) into Postgres. Silent no-op when PG isn't
    configured. Errors drop the cached connection so the next call
    reconnects — never raises (worker JSON write must not be blocked
    by a flaky Postgres).

    snapshot_date: 'YYYY-MM-DD' string (matches the JSON filename so a
                   same-day rerun UPSERTs onto the same row set).
    rows: list of dicts from snapshot_accounts(); each row may carry
          balance_xrp/balance_drops/sequence/owner_count/trust_lines, or
          an `error` string when account_info failed.
    """
    if not rows:
        return
    conn = _get_writer_conn()
    if conn is None:
        return
    written_at = int(time.time())
    try:
        with conn.cursor() as cur:
            for r in rows:
                addr = r.get("address")
                if not addr:
                    continue
                trust_lines = r.get("trust_lines")
                trust_lines_json = (
                    json.dumps(trust_lines, default=str)
                    if trust_lines is not None else None
                )
                cur.execute(
                    "INSERT INTO historical_account_snapshots "
                    "(snapshot_date, address, name, category, balance_xrp, "
                    " balance_drops, sequence, owner_count, trust_lines, "
                    " error, written_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
                    "ON CONFLICT (snapshot_date, address) DO UPDATE SET "
                    "    name = EXCLUDED.name, "
                    "    category = EXCLUDED.category, "
                    "    balance_xrp = EXCLUDED.balance_xrp, "
                    "    balance_drops = EXCLUDED.balance_drops, "
                    "    sequence = EXCLUDED.sequence, "
                    "    owner_count = EXCLUDED.owner_count, "
                    "    trust_lines = EXCLUDED.trust_lines, "
                    "    error = EXCLUDED.error, "
                    "    written_at = EXCLUDED.written_at",
                    (snapshot_date, addr, r.get("name"), r.get("category"),
                     r.get("balance_xrp"), r.get("balance_drops"),
                     r.get("sequence"), r.get("owner_count"),
                     trust_lines_json, r.get("error"), written_at),
                )
    except Exception as e:
        _log_err("write_account_snapshots_failed", e)
        _drop_writer_conn()


def read_snapshot_meta():
    """Return the snapshot-meta payload from Postgres, or None when PG is
    unavailable / table empty. Adds snapshot_age_seconds + from_postgres
    so /institutional can label freshness and source."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload, written_at FROM historical_snapshot_meta WHERE id = 1"
                )
                row = cur.fetchone()
                if not row:
                    return None
                payload, written_at = row
                if not isinstance(payload, dict):
                    return None
                payload["snapshot_age_seconds"] = round(
                    time.time() - int(written_at), 1
                )
                payload["from_postgres"] = True
                return payload
    except Exception:
        return None


# Sources we trust enough to never let a `derived:*` importer overwrite.
# Ordered loosely by trust, but the only check that matters is the
# membership test below.
_CURATED_LABEL_SOURCES = frozenset({"manual", "xrpscan", "bithomp", "toml"})


def upsert_account_label(address, name, source, category=None,
                         confidence=None, extra=None):
    """Insert or update one account label.
    - Curated sources (manual / xrpscan / bithomp) always win: they
      overwrite anything, including older curated labels.
    - Derived sources (derived:amm, derived:mpt) only write when the
      existing row is also derived (or absent). They MUST NOT shadow a
      curated label, even when the importer would generate a better
      derived one — the human-curated string is what we trust on display.

    Silent no-op when PG isn't configured. Returns True on a successful
    write, False otherwise."""
    if not address or not name or not source:
        return False
    conn = _get_writer_conn()
    if conn is None:
        return False
    extra_json = json.dumps(extra, default=str) if extra is not None else None
    now = int(time.time())
    is_curated = source in _CURATED_LABEL_SOURCES
    try:
        with conn.cursor() as cur:
            if is_curated:
                cur.execute(
                    "INSERT INTO account_labels "
                    "(address, name, category, source, confidence, extra, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s) "
                    "ON CONFLICT (address) DO UPDATE SET "
                    "  name = EXCLUDED.name, "
                    "  category = EXCLUDED.category, "
                    "  source = EXCLUDED.source, "
                    "  confidence = EXCLUDED.confidence, "
                    "  extra = EXCLUDED.extra, "
                    "  updated_at = EXCLUDED.updated_at",
                    (address, name, category, source,
                     confidence if confidence is not None else 1.0,
                     extra_json, now),
                )
            else:
                cur.execute(
                    "INSERT INTO account_labels "
                    "(address, name, category, source, confidence, extra, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s) "
                    "ON CONFLICT (address) DO UPDATE SET "
                    "  name = EXCLUDED.name, "
                    "  category = EXCLUDED.category, "
                    "  source = EXCLUDED.source, "
                    "  confidence = EXCLUDED.confidence, "
                    "  extra = EXCLUDED.extra, "
                    "  updated_at = EXCLUDED.updated_at "
                    "WHERE account_labels.source LIKE 'derived:%%'",
                    (address, name, category, source,
                     confidence if confidence is not None else 0.6,
                     extra_json, now),
                )
        return True
    except Exception as e:
        _log_err("upsert_account_label_failed", e)
        _drop_writer_conn()
        return False


def bulk_upsert_derived_labels(rows):
    """Bulk path for the derived passes (AMM/MPT importer). One COPY-like
    executemany instead of N round-trips, which matters at 20k+ AMM
    accounts. Each row is a tuple:
        (address, name, category, source, confidence, extra_json_or_None)
    The ON CONFLICT DO UPDATE … WHERE clause enforces the curated-vs-
    derived priority server-side: a derived label NEVER overwrites a
    curated one (`source NOT LIKE 'derived:%'`). Returns count attempted,
    not count actually written — Postgres doesn't surface the skip count
    via executemany.rowcount cleanly; recomputing it would cost another
    round-trip and isn't worth it for a log line."""
    if not rows:
        return 0
    conn = _get_writer_conn()
    if conn is None:
        return 0
    now = int(time.time())
    payload = [
        (addr, name, cat, src,
         conf if conf is not None else 0.6,
         extra_json, now)
        for (addr, name, cat, src, conf, extra_json) in rows
        if addr and name and src
    ]
    if not payload:
        return 0
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO account_labels "
                "(address, name, category, source, confidence, extra, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s) "
                "ON CONFLICT (address) DO UPDATE SET "
                "  name = EXCLUDED.name, "
                "  category = EXCLUDED.category, "
                "  source = EXCLUDED.source, "
                "  confidence = EXCLUDED.confidence, "
                "  extra = EXCLUDED.extra, "
                "  updated_at = EXCLUDED.updated_at "
                "WHERE account_labels.source LIKE 'derived:%%'",
                payload,
            )
        return len(payload)
    except Exception as e:
        _log_err("bulk_upsert_derived_labels_failed", e)
        _drop_writer_conn()
        return 0


def read_account_label(address):
    """Single-address lookup. Returns dict or None. Used by per-address
    pages (whales, wallet detail) where one query per request is fine."""
    if not address or not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, category, source, confidence, extra, updated_at "
                    "FROM account_labels WHERE address = %s",
                    (address,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "address": address, "name": row[0], "category": row[1],
                    "source": row[2], "confidence": row[3], "extra": row[4],
                    "updated_at": int(row[5]),
                }
    except Exception:
        return None


def read_account_labels(addresses):
    """Bulk lookup. Returns dict keyed by address. Used by list pages
    where we'd otherwise do N round-trips. Missing addresses are simply
    absent from the result — caller decides how to render unlabeled rows."""
    if not addresses or not pg_available():
        return {}
    addrs = list({a for a in addresses if a})
    if not addrs:
        return {}
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT address, name, category, source, confidence, extra "
                    "FROM account_labels WHERE address = ANY(%s)",
                    (addrs,),
                )
                return {
                    r[0]: {
                        "address": r[0], "name": r[1], "category": r[2],
                        "source": r[3], "confidence": r[4], "extra": r[5],
                    }
                    for r in cur.fetchall()
                }
    except Exception:
        return {}


def count_account_labels_by_source():
    """Returns dict {source: count} for /admin/stats or the labels page.
    Cheap — one round trip, one GROUP BY."""
    if not pg_available():
        return {}
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source, COUNT(*) FROM account_labels GROUP BY source"
                )
                return {r[0]: int(r[1]) for r in cur.fetchall()}
    except Exception:
        return {}


def write_amm_tvl_snapshot(rows, ts=None):
    """Append a batch of per-pool TVL rows to amm_tvl_history. `rows` is an
    iterable of dicts shaped like amm_ranked.json entries (amm_account,
    pair, tvl_usd, amount_a, amount_b). Skips rows without an amm_account
    or without a finite tvl_usd — we'd rather have a sparse series than
    a misleading 0. ON CONFLICT DO NOTHING so re-running at the same
    second is harmless. Silent no-op when PG isn't configured."""
    if not rows:
        return 0
    conn = _get_writer_conn()
    if conn is None:
        return 0
    ts = int(ts if ts is not None else time.time())
    payload = []
    for r in rows:
        acct = r.get("amm_account")
        tvl = r.get("tvl_usd")
        if not acct or tvl is None:
            continue
        payload.append((
            acct, ts, tvl,
            r.get("amount_a"), r.get("amount_b"),
            r.get("pair") or "?",
        ))
    if not payload:
        return 0
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO amm_tvl_history "
                "(amm_account, ts, tvl_usd, amount_a, amount_b, pair) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (amm_account, ts) DO NOTHING",
                payload,
            )
        return len(payload)
    except Exception as e:
        _log_err("write_amm_tvl_snapshot_failed", e)
        _drop_writer_conn()
        return 0


def write_mpt_supply_history(rows, ts=None):
    """Append per-MPT supply + concentration rows to mpt_supply_history.

    `rows` is the issuance list from mpt_snapshot.fetch_mpt_data after the
    holders walk. We write one row per issuance whose holders walk
    completed cleanly — reason ∈ {complete, no_holders}. Pending /
    incomplete / skipped_test do NOT enter history; the next clean walk
    backfills. This keeps the time-series gap-free where it has data
    (gaps mean "walk was inconclusive that hour", not "issuance went
    dark"), which is documented on /methodology.

    top1/top3 share are computed inline so the renderer doesn't have to
    re-derive them on every read. Share = balance / outstanding * 100,
    using the same OutstandingAmount denominator the detail page uses.
    top3 is NULL when fewer than 3 positive-balance holders exist.

    ON CONFLICT DO NOTHING (idempotent on retry within the same second).
    Silent no-op when PG isn't configured."""
    if not rows:
        return 0
    conn = _get_writer_conn()
    if conn is None:
        return 0
    ts = int(ts if ts is not None else time.time())
    payload = []
    for r in rows:
        issuance_id = r.get("issuance_id")
        if not issuance_id:
            continue
        h = r.get("holders") or {}
        if h.get("reason") not in ("complete", "no_holders"):
            continue
        try:
            outstanding = int(r.get("outstanding_amount") or 0)
        except (TypeError, ValueError):
            outstanding = 0
        with_balance = h.get("with_balance")
        authorized = h.get("authorized")
        top1_share = None
        top3_share = None
        if outstanding > 0:
            top = h.get("top") or []
            amounts = []
            for entry in top:
                try:
                    a = int(entry.get("mpt_amount") or 0)
                except (TypeError, ValueError):
                    continue
                if a > 0:
                    amounts.append(a)
            if amounts:
                top1_share = round(amounts[0] / outstanding * 100, 3)
            if len(amounts) >= 3:
                top3_share = round(sum(amounts[:3]) / outstanding * 100, 3)
        payload.append((
            issuance_id, ts, outstanding,
            with_balance, authorized,
            top1_share, top3_share,
        ))
    if not payload:
        return 0
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO mpt_supply_history "
                "(mpt_issuance_id, snapshot_ts, outstanding_amount, "
                " holders_with_balance, holders_authorized, "
                " top1_share, top3_share) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (mpt_issuance_id, snapshot_ts) DO NOTHING",
                payload,
            )
        return len(payload)
    except Exception as e:
        _log_err("write_mpt_supply_history_failed", e)
        _drop_writer_conn()
        return 0


def read_mpt_supply_history(mpt_issuance_id, since_ts=None, limit=2160):
    """Return oldest-first history rows for one MPT. Default limit of 2160
    covers the full 90-day raw retention window at hourly cadence — beyond
    that the planned daily rollup table takes over. Returns [] on error /
    PG unavailable.

    Each row: (snapshot_ts, outstanding_amount, holders_with_balance,
    top1_share, top3_share). outstanding_amount is returned as int (XRPL
    raw units); shares as float pct or None."""
    if not pg_available():
        return []
    clauses = ["mpt_issuance_id = %s"]
    params = [mpt_issuance_id]
    if since_ts is not None:
        clauses.append("snapshot_ts >= %s")
        params.append(int(since_ts))
    where = " AND ".join(clauses)
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT snapshot_ts, outstanding_amount, holders_with_balance, "
                    f"       top1_share, top3_share "
                    f"FROM mpt_supply_history WHERE {where} "
                    f"ORDER BY snapshot_ts ASC LIMIT %s",
                    [*params, int(limit)],
                )
                out = []
                for r in cur.fetchall():
                    out.append((
                        int(r[0]),
                        int(r[1]) if r[1] is not None else None,
                        int(r[2]) if r[2] is not None else None,
                        float(r[3]) if r[3] is not None else None,
                        float(r[4]) if r[4] is not None else None,
                    ))
                return out
    except Exception:
        return []


def read_amm_tvl_history(amm_account, since_ts=None, limit=2880):
    """Return (ts, tvl_usd) rows for one pool, oldest-first. Default limit
    of 2880 covers 30 days at 15-min cadence. Returns [] on error or when
    PG is unavailable."""
    if not pg_available():
        return []
    clauses = ["amm_account = %s"]
    params = [amm_account]
    if since_ts is not None:
        clauses.append("ts >= %s")
        params.append(int(since_ts))
    where = " AND ".join(clauses)
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT ts, tvl_usd FROM amm_tvl_history "
                    f"WHERE {where} ORDER BY ts ASC LIMIT %s",
                    [*params, int(limit)],
                )
                return [(int(r[0]), float(r[1]) if r[1] is not None else None)
                        for r in cur.fetchall()]
    except Exception:
        return []


def prune_amm_pool_events(cap_rows):
    """Trim amm_pool_events to the most recent `cap_rows` entries.
    Mirrors the SQLite prune in xrpl_stream — call at the same cadence."""
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM amm_pool_events WHERE id <= "
                "(SELECT MAX(id) - %s FROM amm_pool_events)",
                (cap_rows,),
            )
    except Exception as e:
        _log_err("prune_amm_pool_events_failed", e)
        _drop_writer_conn()


# ─────────────────────────────────────────────────────────────────────
# Flask read helpers (request-scoped connections)
# ─────────────────────────────────────────────────────────────────────

def read_recent_events(limit=10):
    """Latest N rows from `events`, value-movement only. Mirrors the
    homepage SQLite query so the existing _resolve_event() resolver works
    unchanged. Excludes trustset (signal, not movement) so the homepage
    globe pulse and the institutional /api/whales/recent contract stay
    aligned with "large XRP transfer feed". Returns rows in column order:
    tx_hash, ledger_index, ts, type, from_addr, to_addr, amount_drops,
    currency, issuer, raw_json."""
    sql = (
        "SELECT tx_hash, ledger_index, ts, type, from_addr, to_addr, "
        "amount_drops, currency, issuer, raw_json::text FROM events "
        "WHERE type != 'trustset' "
        "ORDER BY ts DESC LIMIT %s"
    )
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return cur.fetchall()


def read_whale_events(tier_drops, filter_type=None, limit=100):
    """Return rows in the same column order as the SQLite query in
    app.whales: tx_hash, ledger_index, ts, type, from_addr, to_addr,
    amount_drops, currency, issuer, raw_json. raw_json is returned as a
    string so the existing _resolve_event() resolver works unchanged.

    Trustset rows are signal, not movement, so the default view excludes
    them; filter_type='trustset' short-circuits the tier gate and returns
    only trustset rows (which always have amount_drops=NULL). Tagged-token
    rows pass through unfiltered — app.whales prices them in Python via
    price_oracle."""
    if filter_type == "trustset":
        clauses = ["type = 'trustset'"]
        params = []
    else:
        clauses = [
            "((type = 'tagged' AND amount_drops IS NULL) "
            "OR amount_drops >= %s)"
        ]
        params = [tier_drops]
        if filter_type:
            clauses.append("type = %s")
            params.append(filter_type)
    where = "WHERE " + " AND ".join(clauses)
    sql = (
        "SELECT tx_hash, ledger_index, ts, type, from_addr, to_addr, "
        "amount_drops, currency, issuer, raw_json::text FROM events "
        f"{where} ORDER BY ts DESC LIMIT %s"
    )
    params.append(limit)
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def read_whale_type_counts(tier_drops):
    """Return dict like {'large_xfer': N, 'tagged': N, 'trustset': N,
    '_total': N} for the /whales stat tiles. Counts here mirror the SQL
    pre-filter used by read_whale_events — token-denominated tagged events
    will still be Python-filtered downstream, so this count is an upper
    bound on what visitors actually see in the list. `_total` excludes
    trustset: it powers the "All" tile, which mirrors the default-view row
    list (value movement only)."""
    counts = {"large_xfer": 0, "tagged": 0, "trustset": 0, "_total": 0}
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT type, COUNT(*) FROM events "
                "WHERE type = 'trustset' "
                "   OR (type = 'tagged' AND amount_drops IS NULL) "
                "   OR amount_drops >= %s "
                "GROUP BY type",
                (tier_drops,),
            )
            for type_, n in cur.fetchall():
                if type_ in counts:
                    counts[type_] = n
                if type_ != "trustset":
                    counts["_total"] += n
    return counts


def read_whale_flag(address, window_days, tier_drops):
    """True if `address` appears as sender or recipient in any XRP-denominated
    whale-tier event in the last `window_days`. Powers the WHALE badge on
    /wallet — same tier threshold as the /whales page so the two surfaces
    stay consistent. XRP-denominated only (amount_drops NOT NULL); tagged
    token whales would need price_oracle pricing per row to match, which
    is too expensive for a per-render badge check."""
    if not pg_available():
        return False
    cutoff_ts = time.time() - window_days * 86400
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM events "
                    "WHERE (from_addr = %s OR to_addr = %s) "
                    "  AND amount_drops >= %s "
                    "  AND ts >= %s "
                    "LIMIT 1",
                    (address, address, tier_drops, cutoff_ts),
                )
                return cur.fetchone() is not None
    except Exception:
        return False


def read_whale_radar_stats(min_drops, hours_back=24):
    """Return the two HUD readouts on /whales radar: count of large_xfer
    events at-or-above min_drops in the last hours_back, and the most
    recent large_xfer's amount_drops (None if none in window). Single
    round-trip, no joins, cheap to call per page render."""
    cutoff_ts = time.time() - hours_back * 3600
    out = {"last_24h": 0, "last_amount_drops": None}
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE type = 'large_xfer' AND amount_drops >= %s "
                "AND ts >= %s",
                (min_drops, cutoff_ts),
            )
            row = cur.fetchone()
            if row:
                out["last_24h"] = int(row[0] or 0)
            cur.execute(
                "SELECT amount_drops FROM events "
                "WHERE type = 'large_xfer' AND amount_drops >= %s "
                "ORDER BY ts DESC LIMIT 1",
                (min_drops,),
            )
            row = cur.fetchone()
            if row and row[0]:
                out["last_amount_drops"] = int(row[0])
    return out


def read_token_volume_aggregates(hours_back=None, limit=50):
    """Aggregate token_volume rows for /tokens. Returns list of tuples
    (currency, issuer, total_trades, hours_active). When hours_back is
    None, aggregates all buckets."""
    where = ""
    params = []
    if hours_back is not None:
        cutoff = int(time.time() // 3600) - hours_back
        where = "WHERE hour_bucket >= %s"
        params.append(cutoff)
    sql = (
        "SELECT currency, issuer, "
        "       SUM(trade_count) AS trades, "
        "       COUNT(*) AS hours_active "
        f"FROM token_volume {where} "
        "GROUP BY currency, issuer "
        "ORDER BY trades DESC LIMIT %s"
    )
    params.append(limit)
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def read_token_history(currency, issuer, spark_hours=168):
    """Per-token trade history for /token/<cur>/<iss>. Mirrors the
    SQLite path in token_data._trade_history so the detail page renders
    real numbers on Render (where volumes.db is gitignored). Returns
    dict with totals, 24h/7d counts, first/last buckets, and an
    hourly sparkline aligned to now_hour."""
    now_hour = int(time.time() // 3600)
    cutoff_24h = now_hour - 24
    cutoff_7d = now_hour - 24 * 7
    cutoff_spark = now_hour - spark_hours
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(trade_count), 0), "
                "       COALESCE(SUM(volume_xrp), 0), "
                "       COUNT(*), MIN(hour_bucket), MAX(hour_bucket) "
                "FROM token_volume WHERE currency = %s AND issuer = %s",
                (currency, issuer),
            )
            row_all = cur.fetchone() or (0, 0, 0, None, None)
            trades_all, volume_all, hours_active, first_b, last_b = row_all

            cur.execute(
                "SELECT COALESCE(SUM(trade_count), 0) FROM token_volume "
                "WHERE currency = %s AND issuer = %s AND hour_bucket >= %s",
                (currency, issuer, cutoff_24h),
            )
            trades_24h = (cur.fetchone() or (0,))[0]

            cur.execute(
                "SELECT COALESCE(SUM(trade_count), 0) FROM token_volume "
                "WHERE currency = %s AND issuer = %s AND hour_bucket >= %s",
                (currency, issuer, cutoff_7d),
            )
            trades_7d = (cur.fetchone() or (0,))[0]

            cur.execute(
                "SELECT hour_bucket, trade_count FROM token_volume "
                "WHERE currency = %s AND issuer = %s AND hour_bucket >= %s "
                "ORDER BY hour_bucket ASC",
                (currency, issuer, cutoff_spark),
            )
            by_hour = {b: c for (b, c) in cur.fetchall()}

    sparkline = [by_hour.get(cutoff_spark + 1 + i, 0) for i in range(spark_hours)]
    # hours_active is rendered on the "last 7 days" sparkline card; derive
    # it from the sparkline result so the count is anchored to the same
    # window the card displays (cutoff_spark == cutoff_7d == now_hour - 168).
    # Previously this returned COUNT(*) across all buckets, producing values
    # > 168 under a "last 7 days" label.
    hours_active_7d = len(by_hour)
    return {
        "trades_all": int(trades_all or 0),
        "volume_all_xrp": float(volume_all or 0),
        "hours_active": hours_active_7d,
        "first_bucket": first_b,
        "last_bucket": last_b,
        "trades_24h": int(trades_24h or 0),
        "trades_7d": int(trades_7d or 0),
        "sparkline": sparkline,
    }


def read_token_volume_bucket_stats():
    """Return (min_bucket, max_bucket, distinct_buckets) — same shape as
    the SQLite stats query in app.tokens. Used for the latest-bucket
    label and freshness signal."""
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(hour_bucket), MAX(hour_bucket), "
                "COUNT(DISTINCT hour_bucket) FROM token_volume"
            )
            return cur.fetchone() or (None, None, 0)


def read_recent_amm_pool_events(seconds):
    """Recent AMM pool events for the /pools constellation poll.
    Returns list of dicts (id, ts, amm_account, event_type, magnitude_xrp_drops)
    ordered by id. magnitude_xrp_drops is None for IOU/IOU pool events and
    for rows written before the magnitude walker landed."""
    cutoff = int(time.time()) - seconds
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, ts, amm_account, event_type, magnitude_xrp_drops "
                "FROM amm_pool_events WHERE ts >= %s "
                "ORDER BY id ASC LIMIT 200",
                (cutoff,),
            )
            return [
                {"id": r[0], "ts": r[1], "amm_account": r[2],
                 "event_type": r[3], "magnitude_xrp_drops": r[4]}
                for r in cur.fetchall()
            ]


def read_max_event_ts():
    """Latest event timestamp in seconds, or None when empty/error."""
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(ts) FROM events")
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None


def read_max_token_bucket():
    """Latest hour_bucket in token_volume, or None when empty/error."""
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(hour_bucket) FROM token_volume")
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None


def count_table(table):
    """Row count for a known table. Used by /health on Render where the
    Mac-side SQLite files don't exist but Neon has the mirrored data.
    Allowlisted table names — never interpolate untrusted input here."""
    if table not in ("events", "token_volume", "amm_pool_events", "page_views"):
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                row = cur.fetchone()
                return int(row[0]) if row else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# Page views (private analytics surface — see /admin/stats)
# ─────────────────────────────────────────────────────────────────────

# Path patterns that identify scanner/bot probes rather than human visits.
# Every public site gets these constantly — WordPress login attempts, env
# file leak probes, PHP fingerprinting. We're not WordPress and not PHP, so
# any hit on these is bot noise, not a real user. SQL LIKE patterns.
BOT_PATH_PATTERNS = (
    "%/.env%",
    "%wp-login%",
    "%wp-admin%",
    "%wp-includes%",
    "%wp-content%",
    "%wordpress%",
    "%wlwmanifest%",
    "%.php",
    "%.php?%",
    "%phpmyadmin%",
    "%/.git%",
    "%/.aws%",
    "%/.ssh%",
    "%cgi-bin%",
    "%admin.php",
    "%config.json",
    # NOTE: %backup% means any future route containing "backup" in its
    # path will be mis-bucketed as bot traffic. Avoid that substring.
    "%backup%",
    "%dump.sql",
    # Credential-probe scanner targets. Real visitors never request
    # these. Anchored on filenames + leading `/.` so a future legit
    # route like /credentials, /private/foo, or /idea-tracker can't
    # collide. Each line covers a behavior CLASS, not just one URL
    # from one observed scanner. Conservative bias: omit any pattern
    # whose substring could plausibly appear in a real product route.
    "%/.bash_history%",
    "%/.bashrc%",
    "%/.zshrc%",
    "%/.profile%",
    "%/.gitconfig%",
    "%/.netrc%",
    "%/.npmrc%",
    "%/.pypirc%",
    "%/.htpasswd%",
    "%/.htaccess%",
    "%/.user.ini%",
    "%/.idea/%",
    "%/.vscode/%",
    "%/.circleci/%",
    "%/.drone%",
    "%/.buildkite/%",
    "%/.docker/%",
    "%/.gcloud/%",
    "%/.config/gcloud%",
    "%/.kube/%",
    "%/.azure/%",
    "%/.github/workflows/%",
    "%/id_rsa%",
    "%/id_dsa%",
    "%/authorized_keys%",
    "%/credentials.json",
    "%/credentials.db",
    "%/credentials.yml",
    "%/credentials.yaml",
    "%/secrets.json",
    "%/secrets.yml",
    "%/secrets.yaml",
    "%/secrets.env",
    "%/private/credentials%",
    # Cloud / service-account credential blobs.
    "%/service-account.json",
    "%/serviceaccount.json",
    "%/firebase-adminsdk.json",
    "%-credentials.json",
    "%_credentials.json",
    "%/aws.json",
    "%/gcp.json",
    "%/azure.json",
    "%/cloud.json",
    "%/api-keys.json",
    "%/api_keys.json",
    "%/keys.json",
    "%/secret.json",
    "%/private.json",
    "%/private_key.pem",
    "%/server.key",
    "%/server.pem",
    # Web server config files.
    "%/nginx.conf",
    "%/nginx.config",
    "%/server.xml",
    "%/web.config",
    # JEE leaks — Flask never serves these directory roots.
    "%/META-INF/%",
    "%/WEB-INF/%",
    # Framework debug/management endpoints (Spring Boot, Symfony).
    "%/actuator/%",
    "%/_profiler%",
    "%/profiler/phpinfo%",
    "%/heapdump",
    "%/threaddump",
    "%/configprops",
    # DevOps/CI/Container/Terraform files.
    "%/Dockerfile",
    "%/Jenkinsfile",
    "%/docker-compose%",
    "%/k8s.yml",
    "%/k8s.yaml",
    "%/kubernetes.yml",
    "%/kubernetes.yaml",
    "%/helm/values%",
    "%/azure-pipelines.yml",
    "%/bitbucket-pipelines.yml",
    "%/.travis.yml",
    "%/terraform.tfstate%",
    "%/terraform.tfvars%",
    "%/.terraform/%",
    # Settings/config file names (anchored — won't match legit routes).
    "%/application.yml",
    "%/application.yaml",
    "%/application.properties",
    "%/appsettings.json",
    "%/appsettings.Development.json",
    "%/appsettings.Production.json",
    "%/database.yml",
    "%/database.yaml",
    "%/database.json",
    "%/database.ini",
    "%/database.sql",
    "%/parameters.yml",
    "%/parameters.yaml",
    "%/config.yml",
    "%/config.yaml",
    "%/config.ini",
    "%/config.env",
    "%/configuration.yml",
    "%/configuration.json",
    "%/settings.yml",
    "%/settings.ini",
    "%/settings.py",
    # Server / app log files at URL root.
    "%/access.log",
    "%/error.log",
    "%/debug.log",
    "%/trace.log",
    "%/app.log",
    "%/server.log",
    "%/application.log",
    "%/laravel.log",
    # DB / archive dumps probed at URL root.
    "%/db.sql",
    "%/db.sql.gz",
    "%/dump.sql.gz",
    "%/data.sql",
    "%/db.zip",
    "%/db.yml",
    "%/site.zip",
    "%/www.zip",
    "%/web.zip",
    "%/dump.zip",
    # WP backup variants that bypass the existing %wp-* path filter.
    "%/wp-config.bak",
    "%/wp-config.txt",
    "%/wp-config.php.bak",
    "%/wp-config.php.old",
    "%/wp-config.php~",
    # Bare /env (the /.env case is covered above).
    "%/env",
    # /trace as Spring management probe (not part of /actuator/ path).
    "%/trace",
    # WordPress JSON API + RSS/Atom feed paths. xrpldashboard serves no
    # /wp-json/ or /feed/ routes, so any hit is a recon scanner.
    "%/wp-json/%",
    "%/feed/rss%",
    "%/feed/atom%",
)


# User-Agent patterns that identify automated traffic — named SEO/AI
# crawlers and non-browser HTTP clients that hit otherwise-legitimate
# paths. Re-buckets these from human → bot in /analytics rollups.
# SQL ILIKE patterns (case-insensitive — crawlers don't always preserve
# UA casing). Three groups:
#  (1) Named crawlers — self-identifying, near-zero false-positive risk.
#  (2) Non-browser HTTP clients — real users don't browse with curl or
#      python-requests. Werkzeug was the /mpt/NOTREAL probe signal.
#  (3) Generic substrings — FALSE-POSITIVE RISK: any future product or
#      route whose UA contains "bot/", "spider", or "crawler" gets
#      mis-bucketed. The "bot/" form (slash forces version-string shape)
#      reduces matches on words like "robot" or "abbot".
BOT_UA_PATTERNS = (
    "%AhrefsBot%",
    "%bingbot%",
    "%Googlebot%",
    "%GPTBot%",
    "%ChatGPT-User%",
    "%ClaudeBot%",
    "%anthropic-ai%",
    "%PerplexityBot%",
    "%MJ12bot%",
    "%Applebot%",
    "%Twitterbot%",
    "%SemrushBot%",
    "%DotBot%",
    "%YandexBot%",
    "%Baiduspider%",
    "%facebookexternalhit%",
    "%LinkedInBot%",
    "%Slackbot%",
    "%DuckDuckBot%",
    "%Go-http-client%",
    "%python-requests%",
    "%aiohttp%",
    "%Werkzeug%",
    "%curl/%",
    "%Wget%",
    "%Scrapy%",
    "%Java/%",
    "%bot/%",
    "%spider%",
    "%crawler%",
)


def _bot_filter_sql(kind):
    """Builds a WHERE-clause fragment that selects human / bot / all rows.
    Returns (fragment, params). Fragment starts with `AND ` so it can be
    appended to an existing WHERE. `kind` is "human", "bot", or "all".
    A row counts as bot if EITHER (a) the row itself matches the path or
    user_agent patterns, or (b) its visitor_hash OR ip_day_hash appears on
    ANY bot-shaped row anywhere in page_views. Clause (b)'s two-key form
    catches both rotating-IP scanners (same UA, many IPs → linked by
    visitor_hash) and rotating-UA scanners (same IP, many UAs → linked by
    ip_day_hash). Pre-rollout rows have NULL ip_day_hash; COALESCE keeps
    them out of the join entirely rather than collapsing to '' which would
    falsely link unrelated NULL-hash rows."""
    if kind == "all":
        return "", []
    path_likes = " OR ".join("path LIKE %s" for _ in BOT_PATH_PATTERNS)
    # COALESCE collapses NULL UA to '' so ILIKE returns FALSE rather than
    # NULL; otherwise three-valued logic drops NULL-UA rows from BOTH the
    # human and bot buckets (NOT NULL is NULL, not TRUE).
    ua_likes = " OR ".join(
        "COALESCE(user_agent, '') ILIKE %s" for _ in BOT_UA_PATTERNS
    )
    row_pred = f"(({path_likes}) OR ({ua_likes}))"
    # Two separate IN subqueries, one per session key. Each only links rows
    # whose key is NOT NULL on both sides — so NULL ip_day_hash rows (the
    # entire pre-rollout history) can't collapse into one mega-session.
    session_pred = (
        f"(visitor_hash IS NOT NULL AND visitor_hash IN ("
        f"  SELECT visitor_hash FROM page_views "
        f"  WHERE visitor_hash IS NOT NULL AND {row_pred}"
        f"))"
        f" OR "
        f"(ip_day_hash IS NOT NULL AND ip_day_hash IN ("
        f"  SELECT ip_day_hash FROM page_views "
        f"  WHERE ip_day_hash IS NOT NULL AND {row_pred}"
        f"))"
    )
    # Catches rotating-IP scanners that share one UA: any (path, user_agent)
    # combo where the SAME ua dominated the SAME path with >=30 distinct
    # visitors in the last 7d AND hits ≈ visitors (mean ≤ 1.10 hits/visitor).
    # Real shared links produce repeat visits; scanner farms hit each
    # rotated IP exactly once. Inverse of the ip_day_hash signal, which
    # catches same-IP rotating-UA scanners.
    scanner_pred = (
        "(user_agent IS NOT NULL AND (path, user_agent) IN ("
        "  SELECT path, user_agent FROM page_views "
        "  WHERE user_agent IS NOT NULL AND ts > %s "
        "  GROUP BY path, user_agent "
        "  HAVING COUNT(*) >= 30 "
        "     AND COUNT(*) <= COUNT(DISTINCT visitor_hash) * 1.10"
        "))"
    )
    full_pred = f"({row_pred} OR {session_pred} OR {scanner_pred})"
    # Params: once for row_pred, once for each of the two session subqueries,
    # plus the scanner_pred ts threshold (7d ago).
    params = list(BOT_PATH_PATTERNS) + list(BOT_UA_PATTERNS)
    params += list(BOT_PATH_PATTERNS) + list(BOT_UA_PATTERNS)
    params += list(BOT_PATH_PATTERNS) + list(BOT_UA_PATTERNS)
    params.append(int(time.time()) - 7 * 86400)
    if kind == "bot":
        return f"AND {full_pred}", params
    return f"AND NOT {full_pred}", params


def log_page_view(path, visitor_hash=None, referrer=None,
                  user_agent=None, country=None, utm_source=None,
                  ip_day_hash=None):
    """Insert one page-view row. Best-effort: never raises. Uses the
    cached writer connection (same pattern as worker writes) so we don't
    eat connection-setup latency on every request."""
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO page_views "
                "(ts, path, visitor_hash, referrer, user_agent, country, "
                " utm_source, ip_day_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (int(time.time()), path, visitor_hash,
                 referrer, user_agent, country, utm_source, ip_day_hash),
            )
    except Exception as e:
        _log_err("log_page_view_failed", e)
        _drop_writer_conn()


def read_page_view_stats(kind="human"):
    """Return rollup counts at common windows for /admin/stats. Each value
    is a dict with `views` (raw row count) and `uniques` (distinct
    visitor_hash). `kind` is "human" (default), "bot", or "all". Returns
    zeros on error so the page renders even if PG hiccups."""
    windows = {
        "now":     5 * 60,
        "hour":    60 * 60,
        "today":   24 * 60 * 60,
        "week":    7 * 24 * 60 * 60,
    }
    out = {k: {"views": 0, "uniques": 0} for k in windows}
    out["all_time"] = {"views": 0, "uniques": 0}
    if not pg_available():
        return out
    now = int(time.time())
    bot_frag, bot_params = _bot_filter_sql(kind)
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                for key, sec in windows.items():
                    cur.execute(
                        "SELECT COUNT(*), COUNT(DISTINCT visitor_hash) "
                        f"FROM page_views WHERE ts >= %s {bot_frag}",
                        [now - sec, *bot_params],
                    )
                    v, u = cur.fetchone() or (0, 0)
                    out[key] = {"views": int(v or 0), "uniques": int(u or 0)}
                cur.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT visitor_hash) "
                    f"FROM page_views WHERE 1=1 {bot_frag}",
                    bot_params,
                )
                v, u = cur.fetchone() or (0, 0)
                out["all_time"] = {"views": int(v or 0), "uniques": int(u or 0)}
    except Exception:
        pass
    return out


def read_top_pages(window_seconds, limit=10, kind="human"):
    """Top paths by view count over the trailing `window_seconds`.
    `kind` is "human" (default), "bot", or "all". Returns list of
    (path, views, uniques)."""
    if not pg_available():
        return []
    cutoff = int(time.time()) - int(window_seconds)
    bot_frag, bot_params = _bot_filter_sql(kind)
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT path, COUNT(*) AS views, "
                    "       COUNT(DISTINCT visitor_hash) AS uniques "
                    f"FROM page_views WHERE ts >= %s {bot_frag} "
                    "GROUP BY path ORDER BY views DESC LIMIT %s",
                    [cutoff, *bot_params, limit],
                )
                return [(r[0], int(r[1]), int(r[2])) for r in cur.fetchall()]
    except Exception:
        return []


def read_recent_page_views(limit=100):
    """Last `limit` page views, newest first. Returns list of dicts."""
    if not pg_available():
        return []
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ts, path, visitor_hash, referrer, "
                    "       user_agent, country "
                    "FROM page_views ORDER BY ts DESC LIMIT %s",
                    (limit,),
                )
                return [
                    {
                        "ts": int(r[0]),
                        "path": r[1],
                        "visitor_hash": r[2],
                        "referrer": r[3],
                        "user_agent": r[4],
                        "country": r[5],
                    }
                    for r in cur.fetchall()
                ]
    except Exception:
        return []


def read_utm_landings(window_seconds, limit=15):
    """Top utm_source values over the trailing window. Counts inbound
    landings carrying a ?utm_source=... query param. Returns list of
    (utm_source, hits). Empty until UTM-tagged outbound links exist."""
    if not pg_available():
        return []
    cutoff = int(time.time()) - int(window_seconds)
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT utm_source, COUNT(*) AS hits "
                    "FROM page_views "
                    "WHERE ts >= %s "
                    "  AND utm_source IS NOT NULL AND utm_source != '' "
                    "GROUP BY utm_source "
                    "ORDER BY hits DESC LIMIT %s",
                    [cutoff, limit],
                )
                return [(r[0], int(r[1])) for r in cur.fetchall()]
    except Exception:
        return []


def read_external_referrers(window_seconds, limit=15):
    """Top external referrer hosts over the trailing window. Excludes
    self-referrals (xrpldashboard.com) and null/empty referrers. Folds
    `www.` so `www.example.com` and `example.com` collapse to one row.
    Returns list of (host, hits)."""
    if not pg_available():
        return []
    cutoff = int(time.time()) - int(window_seconds)
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT host, COUNT(*) AS hits FROM ("
                    "  SELECT regexp_replace("
                    "           substring(referrer FROM 'https?://([^/]+)'),"
                    "           '^www\\.', '') AS host "
                    "  FROM page_views "
                    "  WHERE ts >= %s "
                    "    AND referrer IS NOT NULL AND referrer != '' "
                    "    AND referrer !~ 'xrpldashboard' "
                    ") sub "
                    "WHERE host IS NOT NULL AND host != '' "
                    "GROUP BY host "
                    "ORDER BY hits DESC LIMIT %s",
                    [cutoff, limit],
                )
                return [(r[0], int(r[1])) for r in cur.fetchall()]
    except Exception:
        return []


def read_country_breakdown(window_seconds, limit=10, kind="human"):
    """Top countries by view count over the trailing window. `kind` is
    "human" (default), "bot", or "all". Pass `window_seconds=None` for
    no time filter (all-time). Country may be None when CF-IPCountry
    wasn't present (e.g. local dev, or non-Cloudflare front).
    Returns list of (country, views, uniques)."""
    if not pg_available():
        return []
    bot_frag, bot_params = _bot_filter_sql(kind)
    if window_seconds is None:
        time_frag, time_params = "WHERE 1=1", []
    else:
        cutoff = int(time.time()) - int(window_seconds)
        time_frag, time_params = "WHERE ts >= %s", [cutoff]
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(country, '?') AS c, "
                    "       COUNT(*) AS views, "
                    "       COUNT(DISTINCT visitor_hash) AS uniques "
                    f"FROM page_views {time_frag} {bot_frag} "
                    "GROUP BY c ORDER BY views DESC LIMIT %s",
                    [*time_params, *bot_params, limit],
                )
                return [(r[0], int(r[1]), int(r[2])) for r in cur.fetchall()]
    except Exception:
        return []


def read_country_count(window_seconds, kind="human"):
    """Count of distinct origins (countries + Cloudflare special codes
    like T1 for Tor) seen in the trailing window. Mirrors
    read_country_breakdown's bot-filter + window semantics so the count
    lines up with the table. Pass window_seconds=None for all-time."""
    if not pg_available():
        return 0
    bot_frag, bot_params = _bot_filter_sql(kind)
    if window_seconds is None:
        time_frag, time_params = "WHERE 1=1", []
    else:
        cutoff = int(time.time()) - int(window_seconds)
        time_frag, time_params = "WHERE ts >= %s", [cutoff]
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(DISTINCT COALESCE(country, '?')) "
                    f"FROM page_views {time_frag} {bot_frag}",
                    [*time_params, *bot_params],
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception:
        return 0


def log_cta_click(cta_id, ref_param=None, referrer=None,
                  visitor_hash=None, user_agent=None, country=None):
    """Insert one CTA click row. Best-effort: never raises. Same
    cached-writer pattern as log_page_view."""
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cta_clicks "
                "(ts, cta_id, ref_param, referrer, visitor_hash, "
                " user_agent, country) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (int(time.time()), cta_id, ref_param, referrer,
                 visitor_hash, user_agent, country),
            )
    except Exception as e:
        _log_err("log_cta_click_failed", e)
        _drop_writer_conn()


def read_recent_cta_clicks(limit=100, cta_id=None):
    """Last `limit` CTA clicks, newest first. Optionally filter by cta_id.
    Returns list of dicts."""
    if not pg_available():
        return []
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                if cta_id:
                    cur.execute(
                        "SELECT ts, cta_id, ref_param, referrer, "
                        "       visitor_hash, user_agent, country "
                        "FROM cta_clicks WHERE cta_id = %s "
                        "ORDER BY ts DESC LIMIT %s",
                        (cta_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT ts, cta_id, ref_param, referrer, "
                        "       visitor_hash, user_agent, country "
                        "FROM cta_clicks ORDER BY ts DESC LIMIT %s",
                        (limit,),
                    )
                return [
                    {
                        "ts": int(r[0]),
                        "cta_id": r[1],
                        "ref_param": r[2],
                        "referrer": r[3],
                        "visitor_hash": r[4],
                        "user_agent": r[5],
                        "country": r[6],
                    }
                    for r in cur.fetchall()
                ]
    except Exception:
        return []


def read_cta_click_stats(cta_id=None):
    """Return rollup counts at common windows for the CTA click admin view.
    Each value is a dict with `clicks` (raw row count) and `uniques`
    (distinct visitor_hash). Filter by cta_id when provided."""
    windows = {
        "now":     5 * 60,
        "hour":    60 * 60,
        "today":   24 * 60 * 60,
        "week":    7 * 24 * 60 * 60,
    }
    out = {k: {"clicks": 0, "uniques": 0} for k in windows}
    out["all_time"] = {"clicks": 0, "uniques": 0}
    if not pg_available():
        return out
    now = int(time.time())
    cta_frag = "AND cta_id = %s" if cta_id else ""
    cta_param = [cta_id] if cta_id else []
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                for key, sec in windows.items():
                    cur.execute(
                        "SELECT COUNT(*), COUNT(DISTINCT visitor_hash) "
                        f"FROM cta_clicks WHERE ts >= %s {cta_frag}",
                        [now - sec, *cta_param],
                    )
                    v, u = cur.fetchone() or (0, 0)
                    out[key] = {"clicks": int(v or 0), "uniques": int(u or 0)}
                cur.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT visitor_hash) "
                    f"FROM cta_clicks WHERE 1=1 {cta_frag}",
                    cta_param,
                )
                v, u = cur.fetchone() or (0, 0)
                out["all_time"] = {"clicks": int(v or 0), "uniques": int(u or 0)}
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────
# Institutional contact form
# ─────────────────────────────────────────────────────────────────────

def insert_institutional_inquiry(
    name, email, org, best_time, message,
    ref_param=None, referrer=None,
    visitor_hash=None, user_agent=None, country=None,
):
    """Insert one /institutional/contact submission. Returns the new row id,
    or None if Postgres isn't configured. Raises on real DB errors so the
    caller can surface a submission failure to the visitor (unlike click
    logging, which is best-effort telemetry)."""
    if not pg_available():
        return None
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO institutional_inquiries "
                "(ts, name, email, org, best_time, message, ref_param, "
                " referrer, visitor_hash, user_agent, country) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (int(time.time()), name, email, org, best_time, message,
                 ref_param, referrer, visitor_hash, user_agent, country),
            )
            row = cur.fetchone()
        conn.commit()
    return int(row[0]) if row else None


def mark_institutional_inquiry_alerted(row_id):
    """Flip email_alerted=TRUE after the Brevo alert send succeeds. Best-
    effort — a failure here just means the row stays flagged as unalerted."""
    if not pg_available() or row_id is None:
        return
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE institutional_inquiries "
                    "SET email_alerted = TRUE WHERE id = %s",
                    (row_id,),
                )
            conn.commit()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# General contact form (B1 — /contact)
# ─────────────────────────────────────────────────────────────────────

def insert_contact_inquiry(
    purpose, name, email, message,
    ref_param=None, referrer=None,
    visitor_hash=None, user_agent=None, country=None,
):
    """Insert one /contact submission. Returns the new row id, or None if
    Postgres isn't configured. Raises on real DB errors so the caller can
    surface a submission failure to the visitor (unlike click logging,
    which is best-effort telemetry)."""
    if not pg_available():
        return None
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO contact_inquiries "
                "(ts, purpose, name, email, message, ref_param, "
                " referrer, visitor_hash, user_agent, country) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (int(time.time()), purpose, name, email, message,
                 ref_param, referrer, visitor_hash, user_agent, country),
            )
            row = cur.fetchone()
        conn.commit()
    return int(row[0]) if row else None


def mark_contact_inquiry_alerted(row_id):
    """Flip email_alerted=TRUE after the Brevo alert send succeeds. Best-
    effort — a failure here leaves the row flagged as unalerted for later
    reconciliation."""
    if not pg_available() or row_id is None:
        return
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE contact_inquiries "
                    "SET email_alerted = TRUE WHERE id = %s",
                    (row_id,),
                )
            conn.commit()
    except Exception:
        pass


def read_recent_institutional_inquiries(limit=50):
    """Newest-first list of inquiries. Returns list of dicts."""
    if not pg_available():
        return []
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, ts, name, email, org, best_time, message, "
                    "       ref_param, referrer, country, email_alerted "
                    "FROM institutional_inquiries "
                    "ORDER BY ts DESC LIMIT %s",
                    (limit,),
                )
                return [
                    {
                        "id": int(r[0]),
                        "ts": int(r[1]),
                        "name": r[2],
                        "email": r[3],
                        "org": r[4],
                        "best_time": r[5],
                        "message": r[6],
                        "ref_param": r[7],
                        "referrer": r[8],
                        "country": r[9],
                        "email_alerted": bool(r[10]),
                    }
                    for r in cur.fetchall()
                ]
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────
# Signed integrity snapshots
# ─────────────────────────────────────────────────────────────────────
#
# Mac-side signed_snapshot.py dual-writes the daily envelope to disk
# (./signed_snapshots/YYYY-MM-DD.json) AND to PG via write_signed_snapshot.
# Render's Flask app reads via the read_* helpers — disk is irrelevant on
# Render because the file is generated locally on the Mac.
#
# The envelope row + the one-row chain head are upserted atomically in a
# single transaction so a partial PG failure can never leave the chain
# head pointing at a leaf the envelope row doesn't yet have.

def write_signed_snapshot(envelope, current_root, leaves_total, schema_version):
    """Mirror one signed-snapshot envelope into PG. `envelope` is the full
    JSON dict produced by signed_snapshot.py (everything the per-date route
    returns). `current_root` / `leaves_total` / `schema_version` are taken
    from the updated chain head (post-write).

    Idempotent: re-running for the same date overwrites the row (matches
    signed_snapshot.py's same-day replace semantics). The chain head is
    always set to the post-write values; never decrements.

    Returns True on success, False on PG failure (caller logs and continues
    — disk is the source of truth, PG mirror catches up next run)."""
    if not pg_available():
        return False
    conn = _get_writer_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO signed_snapshots "
                "  (snapshot_date, envelope, leaf_hash, leaf_index, "
                "   chain_root, pubkey_fp, written_at) "
                "VALUES (%s::date, %s::jsonb, %s, %s, %s, %s, NOW()) "
                "ON CONFLICT (snapshot_date) DO UPDATE SET "
                "  envelope    = EXCLUDED.envelope, "
                "  leaf_hash   = EXCLUDED.leaf_hash, "
                "  leaf_index  = EXCLUDED.leaf_index, "
                "  chain_root  = EXCLUDED.chain_root, "
                "  pubkey_fp   = EXCLUDED.pubkey_fp, "
                "  written_at  = EXCLUDED.written_at",
                (
                    envelope["snapshot_date_utc"],
                    json.dumps(envelope, default=str),
                    envelope["leaf_hash"],
                    int(envelope["leaf_index"]),
                    envelope["chain_root"],
                    envelope.get("signing_pubkey_fingerprint") or "",
                ),
            )
            cur.execute(
                "INSERT INTO signed_snapshot_chain "
                "  (id, current_root, leaves_total, schema_version, updated_at) "
                "VALUES (1, %s, %s, %s, NOW()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "  current_root   = EXCLUDED.current_root, "
                "  leaves_total   = EXCLUDED.leaves_total, "
                "  schema_version = EXCLUDED.schema_version, "
                "  updated_at     = EXCLUDED.updated_at",
                (current_root, int(leaves_total), int(schema_version)),
            )
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        _log_err("write_signed_snapshot_failed", e)
        _drop_writer_conn()
        return False


def read_signed_snapshot(date_str):
    """Return the signed envelope dict for `date_str` (ISO YYYY-MM-DD), or
    None when not in PG. Caller validates the date format first."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT envelope FROM signed_snapshots "
                    "WHERE snapshot_date = %s::date",
                    (date_str,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                payload = row[0]
                return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def read_signed_snapshot_chain():
    """Return the live chain head dict matching disk's chain.json shape:
        {schema_version, current_root, leaves_total, first_date, leaves,
         root_history}
    or None when PG has no rows. `leaves` is rebuilt from signed_snapshots
    so the route doesn't need to JOIN at request time; `root_history` is
    the per-date chain_root trail (every leaf is also a root after its
    own write)."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_root, leaves_total, schema_version "
                    "FROM signed_snapshot_chain WHERE id = 1"
                )
                head = cur.fetchone()
                if not head:
                    return None
                current_root, leaves_total, schema_version = head
                cur.execute(
                    "SELECT snapshot_date, leaf_hash, chain_root, "
                    "       envelope->'metrics' "
                    "FROM signed_snapshots ORDER BY leaf_index ASC"
                )
                rows = cur.fetchall()
                leaves = []
                root_history = []
                for snapshot_date, leaf_hash, chain_root, metrics_jsonb in rows:
                    date_str = snapshot_date.isoformat()
                    ledger_index = None
                    if isinstance(metrics_jsonb, list):
                        for m in metrics_jsonb:
                            if isinstance(m, dict) and m.get(
                                "name"
                            ) == "xrpl_validated_ledger_index":
                                try:
                                    ledger_index = int(m.get("value"))
                                except (TypeError, ValueError):
                                    ledger_index = None
                                break
                    leaves.append({
                        "date": date_str,
                        "leaf_hash": leaf_hash,
                        "ledger_index": ledger_index,
                    })
                    root_history.append({"date": date_str, "root": chain_root})
                return {
                    "schema_version": int(schema_version),
                    "current_root": current_root,
                    "leaves_total": int(leaves_total),
                    "first_date": leaves[0]["date"] if leaves else None,
                    "leaves": leaves,
                    "root_history": root_history,
                }
    except Exception:
        return None


def read_signed_snapshot_dates():
    """Newest-first list of YYYY-MM-DD strings of every signed snapshot in
    PG. Powers the date grid on /snapshots/. Empty list when PG empty."""
    if not pg_available():
        return []
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT snapshot_date FROM signed_snapshots "
                    "ORDER BY snapshot_date DESC"
                )
                return [r[0].isoformat() for r in cur.fetchall()]
    except Exception:
        return []


def write_walker_node_fallback(walker_name, reason):
    """Append one row when xrpl_client.XrplClient falls back from the
    local rippled node to a public endpoint. Used to monitor fallback
    rate during/after the local-node cutover. Silent no-op when PG isn't
    configured."""
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO walker_node_fallback (walker_name, reason) "
                "VALUES (%s, %s)",
                (walker_name, reason),
            )
    except Exception as e:
        _log_err(f"write_walker_node_fallback_failed[{walker_name}]", e)
        _drop_writer_conn()


def replace_escrows_snapshot(rows, snapshot_ledger_index=None):
    """Replace-on-write full refresh of the escrows_snapshot table.
    `rows` is a list of dicts with keys: ledger_index_hash (str, the
    Escrow object's `index` field, PK), owner, owner_name, destination,
    denom ('XRP'|'IOU'|'MPT'), amount_drops (int or None), amount_json
    (JSON-serializable), finish_after, cancel_after, condition_present,
    previous_txn_id, previous_txn_lgr_seq.

    All in one transaction so a partial failure leaves the previous
    snapshot intact. Returns True on success, False on any error /
    PG-unavailable — walker treats False as a soft-fail and
    walker_health_end records it.

    v1 = replace. If a future "realized releases per month" chart is
    wanted, add a sibling escrows_history append table rather than
    reworking this one (see SCHEMA_DDL note)."""
    if not pg_available():
        return False
    fetched_at = int(time.time())
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM escrows_snapshot")
                for r in rows:
                    cur.execute(
                        "INSERT INTO escrows_snapshot "
                        "  (ledger_index_hash, owner, owner_name, destination, "
                        "   denom, amount_drops, amount_json, finish_after, "
                        "   cancel_after, condition_present, previous_txn_id, "
                        "   previous_txn_lgr_seq, snapshot_ledger_index, "
                        "   fetched_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, "
                        "        %s, %s, %s, %s)",
                        (
                            r["ledger_index_hash"],
                            r["owner"],
                            r.get("owner_name"),
                            r.get("destination"),
                            r["denom"],
                            r.get("amount_drops"),
                            json.dumps(r["amount_json"]),
                            r.get("finish_after"),
                            r.get("cancel_after"),
                            bool(r.get("condition_present")),
                            r.get("previous_txn_id"),
                            r.get("previous_txn_lgr_seq"),
                            snapshot_ledger_index,
                            fetched_at,
                        ),
                    )
            conn.commit()
        return True
    except Exception as e:
        _log_err("replace_escrows_snapshot_failed", e)
        return False


def read_escrows_snapshot():
    """Return the full escrows_snapshot as a list of dicts sorted by
    (owner_name, finish_after). Includes derived `token_escrow_observed`
    (any denom != 'XRP') and the shared fetched_at. Empty list when PG
    unavailable / table empty — caller renders "no data yet"."""
    if not pg_available():
        return {"rows": [], "fetched_at": None, "token_escrow_observed": False,
                "snapshot_age_seconds": None, "snapshot_ledger_index": None}
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ledger_index_hash, owner, owner_name, destination, "
                    "       denom, amount_drops, amount_json, finish_after, "
                    "       cancel_after, condition_present, "
                    "       previous_txn_id, previous_txn_lgr_seq, "
                    "       snapshot_ledger_index, fetched_at "
                    "  FROM escrows_snapshot "
                    " ORDER BY owner_name NULLS LAST, finish_after NULLS LAST"
                )
                cols = [d.name for d in cur.description]
                raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        _log_err("read_escrows_snapshot_failed", e)
        return {"rows": [], "fetched_at": None, "token_escrow_observed": False,
                "snapshot_age_seconds": None, "snapshot_ledger_index": None}
    fetched_at = raw[0]["fetched_at"] if raw else None
    snap_ledger = raw[0]["snapshot_ledger_index"] if raw else None
    age = (int(time.time()) - fetched_at) if fetched_at else None
    return {
        "rows": raw,
        "fetched_at": fetched_at,
        "snapshot_age_seconds": age,
        "snapshot_ledger_index": snap_ledger,
        "token_escrow_observed": any(r["denom"] != "XRP" for r in raw),
    }


def read_upcoming_escrow_releases(limit=50, now_ripple_seconds=None):
    """Return upcoming (FinishAfter in the future) escrows ordered by
    FinishAfter ASC, capped at `limit`. `now_ripple_seconds` optional
    for tests; defaults to now converted to ripple-time. Same row
    shape as read_escrows_snapshot.rows."""
    if not pg_available():
        return []
    if now_ripple_seconds is None:
        RIPPLE_EPOCH = 946684800
        now_ripple_seconds = int(time.time()) - RIPPLE_EPOCH
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ledger_index_hash, owner, owner_name, destination, "
                    "       denom, amount_drops, amount_json, finish_after, "
                    "       cancel_after, condition_present "
                    "  FROM escrows_snapshot "
                    " WHERE finish_after IS NOT NULL "
                    "   AND finish_after > %s "
                    " ORDER BY finish_after ASC "
                    " LIMIT %s",
                    (now_ripple_seconds, limit),
                )
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        _log_err("read_upcoming_escrow_releases_failed", e)
        return []


def replace_oracles_snapshot(rows, snapshot_ledger_index=None):
    """Replace-on-write full refresh of oracles_snapshot. `rows` is a list
    of dicts with keys: ledger_index_hash (PK), owner, owner_name,
    document_id (int|None), provider (decoded str|None), uri (decoded
    str|None), asset_class (decoded str|None), last_update_time (Unix
    seconds|None), price_data_json (list of decoded pair dicts),
    pair_count (int), previous_txn_id, previous_txn_lgr_seq.

    All in one transaction — a partial failure leaves the prior snapshot
    intact. Returns True on success, False on error / PG-unavailable.
    Walker treats False as soft-fail and walker_health_end records it."""
    if not pg_available():
        return False
    fetched_at = int(time.time())
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM oracles_snapshot")
                for r in rows:
                    cur.execute(
                        "INSERT INTO oracles_snapshot "
                        "  (ledger_index_hash, owner, owner_name, document_id, "
                        "   provider, uri, asset_class, last_update_time, "
                        "   price_data_json, pair_count, previous_txn_id, "
                        "   previous_txn_lgr_seq, snapshot_ledger_index, "
                        "   fetched_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, "
                        "        %s, %s, %s, %s, %s)",
                        (
                            r["ledger_index_hash"],
                            r["owner"],
                            r.get("owner_name"),
                            r.get("document_id"),
                            r.get("provider"),
                            r.get("uri"),
                            r.get("asset_class"),
                            r.get("last_update_time"),
                            json.dumps(r["price_data_json"]),
                            r["pair_count"],
                            r.get("previous_txn_id"),
                            r.get("previous_txn_lgr_seq"),
                            snapshot_ledger_index,
                            fetched_at,
                        ),
                    )
            conn.commit()
        return True
    except Exception as e:
        _log_err("replace_oracles_snapshot_failed", e)
        return False


def read_oracles_snapshot():
    """Return oracles_snapshot as {rows, fetched_at, snapshot_age_seconds,
    snapshot_ledger_index}. Sorted by (owner_name, last_update_time DESC)
    so the freshest per-owner oracle leads. Empty rows list when PG
    unavailable / table empty — template renders "no data yet"."""
    empty = {"rows": [], "fetched_at": None, "snapshot_age_seconds": None,
             "snapshot_ledger_index": None}
    if not pg_available():
        return empty
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ledger_index_hash, owner, owner_name, document_id, "
                    "       provider, uri, asset_class, last_update_time, "
                    "       price_data_json, pair_count, previous_txn_id, "
                    "       previous_txn_lgr_seq, snapshot_ledger_index, "
                    "       fetched_at "
                    "  FROM oracles_snapshot "
                    " ORDER BY owner_name NULLS LAST, "
                    "          last_update_time DESC NULLS LAST"
                )
                cols = [d.name for d in cur.description]
                raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        _log_err("read_oracles_snapshot_failed", e)
        return empty
    fetched_at = raw[0]["fetched_at"] if raw else None
    snap_ledger = raw[0]["snapshot_ledger_index"] if raw else None
    age = (int(time.time()) - fetched_at) if fetched_at else None
    return {
        "rows": raw,
        "fetched_at": fetched_at,
        "snapshot_age_seconds": age,
        "snapshot_ledger_index": snap_ledger,
    }


# ─────────────────────────────────────────────────────────────────────
# NFT walker helpers — back /nfts (funnel + active/quiet + churn badge).
# nft_activity is append-only with a unique tx_hash so backfill/retry
# overlap is safe. nft_walker_state is single-row-per-walker; the walker
# reads it at run start and writes it back at run end (or on batch
# boundaries during a long backfill).
# ─────────────────────────────────────────────────────────────────────


def read_nft_walker_state(walker_name):
    """Return the walker's state row as a dict, or None if it doesn't
    exist yet (walker's first run — caller seeds it). None also on
    PG-unavailable."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT walker_name, cursor_ledger, backfill_ledger, "
                    "       backfill_target, last_run_at, last_success_at "
                    "  FROM nft_walker_state "
                    " WHERE walker_name = %s",
                    (walker_name,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cols = [d.name for d in cur.description]
                return dict(zip(cols, row))
    except Exception as e:
        _log_err(f"read_nft_walker_state_failed[{walker_name}]", e)
        return None


def seed_nft_walker_state(walker_name, cursor_ledger, backfill_target):
    """Insert the initial walker_state row. INSERT ... DO NOTHING so a
    re-seed can't overwrite an in-progress cursor (or the immutable
    backfill_target). backfill_ledger is set to cursor_ledger initially —
    backfill mode walks it downward toward backfill_target. Returns True
    if a fresh row was created, False if a row already existed."""
    if not pg_available():
        return False
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO nft_walker_state "
                    "  (walker_name, cursor_ledger, backfill_ledger, "
                    "   backfill_target, last_run_at, last_success_at) "
                    "VALUES (%s, %s, %s, %s, NULL, NULL) "
                    "ON CONFLICT (walker_name) DO NOTHING",
                    (walker_name, cursor_ledger, cursor_ledger, backfill_target),
                )
                created = cur.rowcount > 0
            conn.commit()
        return created
    except Exception as e:
        _log_err(f"seed_nft_walker_state_failed[{walker_name}]", e)
        return False


def write_nft_walker_cursor(walker_name, cursor_ledger, success=True):
    """Advance cursor_ledger (forward-mode) and stamp last_run_at (and
    last_success_at when success=True). backfill_ledger and
    backfill_target are untouched by forward-mode runs. Silent no-op
    when PG unavailable."""
    if not pg_available():
        return False
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                if success:
                    cur.execute(
                        "UPDATE nft_walker_state SET "
                        "  cursor_ledger = %s, "
                        "  last_run_at = now(), "
                        "  last_success_at = now() "
                        "WHERE walker_name = %s",
                        (cursor_ledger, walker_name),
                    )
                else:
                    cur.execute(
                        "UPDATE nft_walker_state SET "
                        "  last_run_at = now() "
                        "WHERE walker_name = %s",
                        (walker_name,),
                    )
            conn.commit()
        return True
    except Exception as e:
        _log_err(f"write_nft_walker_cursor_failed[{walker_name}]", e)
        return False


def set_nft_walker_backfill_target(walker_name, backfill_target):
    """Set backfill_target once. Refuses to overwrite a non-NULL value —
    the cutoff label is immutable so /nfts callouts like
    'since 2026-04-01' don't drift retroactively. Returns True if the
    write happened, False if already set (or PG unavailable)."""
    if not pg_available():
        return False
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE nft_walker_state SET backfill_target = %s "
                    "WHERE walker_name = %s AND backfill_target IS NULL",
                    (backfill_target, walker_name),
                )
                written = cur.rowcount > 0
            conn.commit()
        return written
    except Exception as e:
        _log_err(f"set_nft_walker_backfill_target_failed[{walker_name}]", e)
        return False


def write_nft_walker_backfill_cursor(walker_name, backfill_ledger, success=True):
    """Advance backfill_ledger DOWNWARD (walker walks from higher ledger
    toward backfill_target). Stamps last_run_at (and last_success_at when
    success=True). Silent no-op when PG unavailable."""
    if not pg_available():
        return False
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                if success:
                    cur.execute(
                        "UPDATE nft_walker_state SET "
                        "  backfill_ledger = %s, "
                        "  last_run_at = now(), "
                        "  last_success_at = now() "
                        "WHERE walker_name = %s",
                        (backfill_ledger, walker_name),
                    )
                else:
                    cur.execute(
                        "UPDATE nft_walker_state SET "
                        "  last_run_at = now() "
                        "WHERE walker_name = %s",
                        (walker_name,),
                    )
            conn.commit()
        return True
    except Exception as e:
        _log_err(f"write_nft_walker_backfill_cursor_failed[{walker_name}]", e)
        return False


def insert_nft_activity_batch(rows):
    """Batched INSERT of nft_activity rows. Each row is a dict with keys:
    ledger_index, close_time (datetime | int-unix), tx_hash, tx_type,
    nftoken_id, issuer, taxon, buyer, seller, price_drops, currency,
    currency_issuer, is_broker, raw (JSON-serializable). ON CONFLICT
    (tx_hash) DO NOTHING so backfill/forward overlap and per-invocation
    retries are safe. Returns count of rows actually inserted."""
    if not pg_available() or not rows:
        return 0
    inserted = 0
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                for r in rows:
                    ct = r["close_time"]
                    if isinstance(ct, (int, float)):
                        # Ledger close_time is ripple-epoch seconds; upstream
                        # helper is expected to convert to unix epoch. Accept
                        # either datetime or unix-int for flexibility.
                        ct = datetime.datetime.fromtimestamp(
                            int(ct), tz=datetime.timezone.utc
                        )
                    cur.execute(
                        "INSERT INTO nft_activity "
                        "  (ledger_index, close_time, tx_hash, tx_type, "
                        "   nftoken_id, issuer, taxon, buyer, seller, "
                        "   price_drops, currency, currency_issuer, is_broker, "
                        "   raw) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                        "        %s, %s, %s, %s::jsonb) "
                        "ON CONFLICT (tx_hash) DO NOTHING",
                        (
                            r["ledger_index"],
                            ct,
                            r["tx_hash"],
                            r["tx_type"],
                            r.get("nftoken_id"),
                            r.get("issuer"),
                            r.get("taxon"),
                            r.get("buyer"),
                            r.get("seller"),
                            r.get("price_drops"),
                            r.get("currency"),
                            r.get("currency_issuer"),
                            r.get("is_broker"),
                            json.dumps(r.get("raw")) if r.get("raw") is not None else None,
                        ),
                    )
                    inserted += cur.rowcount
            conn.commit()
        return inserted
    except Exception as e:
        _log_err("insert_nft_activity_batch_failed", e)
        return 0


def count_nft_activity():
    """Return total nft_activity row count. Cheap tap-check for D1
    status ping and per-run logging. 0 on PG-unavailable."""
    if not pg_available():
        return 0
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM nft_activity")
                return int(cur.fetchone()[0])
    except Exception as e:
        _log_err("count_nft_activity_failed", e)
        return 0
