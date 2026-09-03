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
import re
import threading
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


# Hostname/URL/user/IP redactor for exception messages logged via _log_err.
# 2026-09-03 fix: writer_connect_failed errors in launchd_logs/ named the
# Neon username and backend IPs verbatim ("password authentication failed
# for user 'neondb_owner'", "connection to server at '<ip>' failed"). Scaled
# fix at the log helper so every _log_err caller inherits the redaction.
_LEAK_RE = re.compile(
    r"host ['\"]?[A-Za-z0-9._-]+['\"]?"                # DNS-error shape: host 'X'
    r"|postgres[a-z+]*://[^\s'\"]+"                    # full URL
    r"|ep-[A-Za-z0-9-]+\.[A-Za-z0-9.-]+"               # bare Neon endpoint hostname
    r"|at \"\d{1,3}(?:\.\d{1,3}){3}\""                 # psycopg backend IP form
    r"|\bneondb_owner\b"                               # Neon DB username
)


def _sanitize_for_log(obj) -> str:
    """Redact hostnames/URLs/user/IPs before stringifying an exception for log output."""
    return _LEAK_RE.sub("[REDACTED]", str(obj))


def _log_err(category, exc):
    # Schema-drift class NEVER self-heals; treating it like a transient
    # error hid the tx_type_hourly gap for 6 minutes on 2026-07-07.
    # Always loud, never rate-limited. First hit dumps the stack so the
    # caller is grep-able.
    exc_str = _sanitize_for_log(exc)
    if _is_schema_drift(exc):
        if category not in _SCHEMA_DRIFT_SEEN:
            import traceback
            tb = _sanitize_for_log("".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)))
            print(
                f"[db] !!! SCHEMA-DRIFT {category}: {type(exc).__name__}: {exc_str}\n"
                f"[db]     Won't self-heal — apply init_schema() or the "
                f"relevant migration.\n{tb}",
                flush=True,
            )
            _SCHEMA_DRIFT_SEEN.add(category)
        else:
            print(
                f"[db] !!! SCHEMA-DRIFT {category}: {type(exc).__name__}: {exc_str}",
                flush=True,
            )
        return

    now = time.time()
    last_ts, suppressed = _LAST_ERR_LOG.get(category, (0, 0))
    if now - last_ts < _ERR_LOG_INTERVAL_S:
        _LAST_ERR_LOG[category] = (last_ts, suppressed + 1)
        return
    tail = f" ({suppressed} suppressed in last {_ERR_LOG_INTERVAL_S}s)" if suppressed else ""
    print(f"[db] {category}: {type(exc).__name__}: {exc_str}{tail}", flush=True)
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
    trade_count BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS token_volume_bucket_idx
    ON token_volume (hour_bucket DESC);
-- Migration 2026-08-02 (path-tagging): per-row path_type enables value-weighted
-- AMM/CLOB split (docs/FLOOR_RERUN_2026-08.md preconditions). Enum ∈
-- {AMM, CLOB, MIXED, DIRECT, AMM_LP, UNKNOWN}. NULL is reserved for
-- pre-2026-08-02 rows (past isn't taggable — we never backfill a guess).
-- Old PK dropped in favor of a UNIQUE INDEX that treats NULLs as distinct
-- (preserving legacy row uniqueness) while allowing new writes to bucket
-- separately by path. See migrations/2026_08_02_token_volume_path_type.sql.
ALTER TABLE token_volume ADD COLUMN IF NOT EXISTS path_type TEXT;
ALTER TABLE token_volume DROP CONSTRAINT IF EXISTS token_volume_pkey;
CREATE UNIQUE INDEX IF NOT EXISTS token_volume_path_uniq_idx
    ON token_volume (currency, issuer, hour_bucket, path_type);

-- AI-crawler telemetry (Phase 2 of the 2026-08-02 two-instrument build).
-- Every request to an agent-tier route from a bot-shaped UA writes one
-- row. `ua_class` is either an exact AI_CRAWLER_UA_SUBSTRINGS entry
-- (e.g. 'gptbot', 'claudebot', 'perplexitybot') OR the literal
-- 'UNLISTED' for bot-shaped UAs that don't match the allowlist —
-- Charlie's rule: log spoofers/unlisted agents as their own class, do
-- NOT force them into the 15.
--
-- Denominator scope: agent-tier routes only (see AGENT_TIER_ROUTE_PATHS).
-- General page traffic stays in `page_views`; the two surfaces are the
-- citation-signal / demand-signal split the ship note pinned.
--
-- No PK — this is an append-only fact table. Aggregations use the
-- (ua_class, ts) index. Retention: keep indefinitely at current rate;
-- if volume outgrows storage a `ts < now() - N days` prune is trivial.
CREATE TABLE IF NOT EXISTS ai_crawler_hits (
    ts       BIGINT NOT NULL,
    ua_class TEXT NOT NULL,
    path     TEXT NOT NULL,
    status   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_crawler_hits_class_ts_idx
    ON ai_crawler_hits (ua_class, ts DESC);
CREATE INDEX IF NOT EXISTS ai_crawler_hits_ts_idx
    ON ai_crawler_hits (ts DESC);

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
    snapshot_date        DATE     NOT NULL,
    xrpl_supply          NUMERIC  NOT NULL,
    eth_supply           NUMERIC  NOT NULL,
    total_supply         NUMERIC  NOT NULL,
    xrpl_holders         INTEGER,
    eth_holders          INTEGER,
    xrpl_mints_24h       NUMERIC,  -- historical; NULL from 2026-05-25 onward
    xrpl_burns_24h       NUMERIC,  -- historical; NULL from 2026-05-25 onward
    xrpl_net_change_24h  NUMERIC,  -- Option A: gateway_balances snapshot-diff
    eth_mints_24h        NUMERIC,
    eth_burns_24h        NUMERIC,
    written_at_iso       TEXT     NOT NULL,
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
    cadence_seconds       INTEGER,
    -- findings_count: distinct signal from ok/consecutive_failures. A clean
    -- run that surfaces N vulnerabilities/CVEs/anomalies writes ok=True
    -- (execution succeeded) AND findings_count=N (loud signal, still paged
    -- via l1_pager.check_walker_findings). Conflating findings with
    -- consecutive_failures pages after 2 clean runs of a walker doing its
    -- job correctly. Introduced 2026-08-29 alongside pip_audit_walker.
    findings_count        INTEGER
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

-- Bot-drop log for /contact form (soft-drop filter, 2026-08-12).
-- One row per submission silently discarded. No payload stored — only
-- the ts, UA, and which signature fired. Lets us track campaign decay
-- and audit that the filter is doing what it says it does.
CREATE TABLE IF NOT EXISTS contact_bot_drops (
    id        BIGSERIAL PRIMARY KEY,
    ts        BIGINT NOT NULL,
    ua        TEXT,
    signature TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contact_bot_drops_ts_idx
    ON contact_bot_drops (ts DESC);

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

-- Novelty layer for the never-blind guarantee. One row per LedgerEntryType
-- ever observed on the stream via meta.AffectedNodes; one row per
-- TransactionType ever observed. first_seen_* is set at INSERT and never
-- moved thereafter — batch buckets carry the min-ledger seen in the window
-- so a first-seen record isn't stamped up to a flush-window late.
CREATE TABLE IF NOT EXISTS ledger_entry_type_seen (
    entry_type         TEXT PRIMARY KEY,
    first_seen_ledger  BIGINT NOT NULL,
    first_seen_ts      BIGINT NOT NULL,
    last_seen_ledger   BIGINT NOT NULL,
    last_seen_ts       BIGINT NOT NULL,
    count_created      BIGINT NOT NULL DEFAULT 0,
    count_modified     BIGINT NOT NULL DEFAULT 0,
    count_deleted      BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tx_type_seen (
    tx_type            TEXT PRIMARY KEY,
    first_seen_ledger  BIGINT NOT NULL,
    first_seen_ts      BIGINT NOT NULL,
    last_seen_ledger   BIGINT NOT NULL,
    last_seen_ts       BIGINT NOT NULL,
    count_total        BIGINT NOT NULL DEFAULT 0
);

-- Ledger-vocabulary source of truth from local rippled's server_definitions.
-- Singleton (id=1) + append-only history on hash change. Feeds the Coverage
-- Register's "defined" column in Phase 1b's three-way diff.
CREATE TABLE IF NOT EXISTS ledger_definitions (
    id             INT PRIMARY KEY DEFAULT 1,
    hash           TEXT NOT NULL,
    fetched_at     BIGINT NOT NULL,
    build_version  TEXT,
    tx_types       JSONB NOT NULL,
    entry_types    JSONB NOT NULL,
    payload        JSONB NOT NULL,
    CONSTRAINT ledger_definitions_singleton CHECK (id = 1)
);
CREATE TABLE IF NOT EXISTS ledger_definitions_history (
    id                   BIGSERIAL PRIMARY KEY,
    fetched_at           BIGINT NOT NULL,
    hash                 TEXT   NOT NULL,
    hash_prev            TEXT,
    build_version        TEXT,
    tx_types_added       JSONB,
    tx_types_removed     JSONB,
    entry_types_added    JSONB,
    entry_types_removed  JSONB,
    payload              JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_definitions_history_ts_idx
    ON ledger_definitions_history (fetched_at DESC);

-- Phase 1b Coverage Register.
--
-- coverage_labels — curated editorial mapping from a vocabulary key
-- (kind, name) to a display label + linked page. Small (~100 rows).
-- Populated once at ship via seed_coverage_labels.py so day-one doesn't
-- open with every seen type in the amber "unlabeled" state drowning the
-- genuinely interesting defined-but-unseen greys (Loan, Vault, Delegate,
-- Batch et al. awaiting first-ever XRPL sighting).
CREATE TABLE IF NOT EXISTS coverage_labels (
    kind        TEXT NOT NULL CHECK (kind IN ('tx','entry')),
    name        TEXT NOT NULL,
    label       TEXT NOT NULL,
    short_desc  TEXT NOT NULL,
    linked_page TEXT,
    updated_at  BIGINT NOT NULL,
    PRIMARY KEY (kind, name)
);

-- Escrow-lesson enforcement table. Every walker that reads from the XRPL
-- MUST have a row here; register renders UNDECLARED (its own alarm class)
-- for any walker in walker_health missing from this table. The register's
-- opening honesty concession — cold_storage's 20-account seed, the
-- credentials+PD 14-account seed, the nft_activity 2026-04-01 time cutoff
-- — surfaces as declared PARTIAL rows rather than the tool announcing
-- more coverage than it has.
--
-- updated_at is informational only, rendered as "declared: YYYY-MM-DD".
-- Never freshness-gated — a declaration has no cadence, and 2× staleness
-- makes no sense for a curation fact. Staleness comes only from
-- walker_health.last_success_at on the walker itself.
CREATE TABLE IF NOT EXISTS walker_scope_declarations (
    walker_name     TEXT PRIMARY KEY,
    declared_scope  TEXT NOT NULL,
    filter_note     TEXT NOT NULL,
    honest_partial  BOOLEAN NOT NULL,
    updated_at      BIGINT NOT NULL
);

-- Append-only history of the coverage-register state. Written by the
-- dedicated coverage_register_walker (own walker_health row), NOT by
-- request-side reads — computed-on-read gives a live view but the
-- artifact of the week nobody visited needs its own owner. Per the
-- HIGH-RISK-writer discipline codified 2026-07: named writer, watermark,
-- not an implication.
--
-- Writer appends when state differs from the most recent row OR >24h
-- has elapsed (guarantee floor so the record exists even in quiescent
-- weeks). Day-zero row seeded from the first_seen_baseline artifact.
CREATE TABLE IF NOT EXISTS coverage_register_history (
    id                    BIGSERIAL PRIMARY KEY,
    fetched_at            BIGINT NOT NULL,
    definitions_hash      TEXT NOT NULL,
    defined_tx_count      INT NOT NULL,
    defined_entry_count   INT NOT NULL,
    seen_tx_count         INT NOT NULL,
    seen_entry_count      INT NOT NULL,
    undefined_tx          JSONB NOT NULL,
    undefined_entry       JSONB NOT NULL,
    new_since_last        JSONB NOT NULL,
    stale_walkers         JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS coverage_register_history_ts_idx
    ON coverage_register_history (fetched_at DESC);

-- API v1 key issuance. One row per issued key. Full plaintext key is
-- displayed once at generation and never stored — only SHA-256(key). Lookup
-- path is SHA-256(bearer) -> key_hash unique index -> row. key_prefix is
-- the human-recognizable head of the key ("xd_live_abcd1234") shown on the
-- account page so users can identify which key is which without ever seeing
-- the full secret again. Anchored in project_xrpldashboard_api_v1_anchors.
CREATE TABLE IF NOT EXISTS api_keys (
    id                     BIGSERIAL PRIMARY KEY,
    created_at             BIGINT NOT NULL,
    email                  TEXT NOT NULL,
    key_hash               TEXT NOT NULL UNIQUE,
    key_prefix             TEXT NOT NULL,
    tier                   TEXT NOT NULL DEFAULT 'free',
    status                 TEXT NOT NULL DEFAULT 'active',
    revoked_at             BIGINT,
    stripe_customer_id     TEXT,
    stripe_subscription_id TEXT,
    last_used_at           BIGINT
);
CREATE INDEX IF NOT EXISTS api_keys_email_status_idx
    ON api_keys (email, status);
CREATE INDEX IF NOT EXISTS api_keys_hash_idx
    ON api_keys (key_hash);

-- Stripe webhook event log. Stub for the billing rollout; empty tonight.
-- stripe_event_id UNIQUE gives replay idempotency (Stripe retries deliver
-- the same event id). processed_at NULL = unprocessed, non-null = handled.
CREATE TABLE IF NOT EXISTS stripe_events (
    id              BIGSERIAL PRIMARY KEY,
    stripe_event_id TEXT NOT NULL UNIQUE,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL,
    processed_at    BIGINT,
    created_at      BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS stripe_events_type_idx
    ON stripe_events (event_type);
CREATE INDEX IF NOT EXISTS stripe_events_processed_idx
    ON stripe_events (processed_at NULLS FIRST);

-- Per-key per-hour request counter. Neon-backed so the count survives
-- process restarts and is authoritative across Gunicorn workers. Hour
-- bucket = unix_ts // 3600. Old rows are cheap to sweep with a periodic
-- DELETE WHERE hour_bucket < now/3600 - 168 (7d retention is plenty for
-- rate-limit purposes).
-- Per-identity per-hour request counter. Identity is EITHER a key_id
-- (keyed caller) OR an ip_hash (anonymous caller). Exactly one is
-- populated per row; the CHECK enforces the XOR. Uniqueness across
-- (identity, hour) uses COALESCE in a unique index since PostgreSQL
-- PKs can't include expressions.
-- Shape change 2026-08-30 for /check.json v0.9 metering; migration:
-- migrations/2026_08_30_api_request_counters_ip_hash.sql.
CREATE TABLE IF NOT EXISTS api_request_counters (
    id            BIGSERIAL PRIMARY KEY,
    key_id        BIGINT,
    ip_hash       TEXT,
    hour_bucket   BIGINT NOT NULL,
    request_count INT NOT NULL DEFAULT 0,
    CONSTRAINT api_request_counters_identity_xor
        CHECK ((key_id IS NULL) <> (ip_hash IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS api_request_counters_identity_hour_uniq
    ON api_request_counters (
        COALESCE(key_id, 0),
        COALESCE(ip_hash, ''),
        hour_bucket
    );
CREATE INDEX IF NOT EXISTS api_request_counters_bucket_idx
    ON api_request_counters (hour_bucket);

-- Cold-storage per-address balance cache. Mac walker (cold_storage_walker.py)
-- fetches balances for every category=ripple address in named_accounts.json
-- every 15 min via LAN rippled and upserts here. The /cold-storage route
-- reads latest rows from this table instead of making 21 live account_info
-- RPC calls per page render. Wired 2026-09-03 to stop Ripple public-node
-- dependence + kill ~214 walker_node_fallback rows/hr for walker_name=cold_storage.
CREATE TABLE IF NOT EXISTS cold_storage_snapshot (
    address       TEXT PRIMARY KEY,
    balance_xrp   NUMERIC(20, 6) NOT NULL,
    sequence      BIGINT,
    owner_count   INTEGER,
    ledger_index  BIGINT NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetch_ok      BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS cold_storage_snapshot_fetched_at_idx
    ON cold_storage_snapshot (fetched_at DESC);

-- Escrow-supply aggregate cache. Companion to cold_storage_snapshot: sums
-- EscrowCreate objects across the same category=ripple cohort (minus RLUSD
-- issuer) and stores one aggregate row (singleton, id=1 per rlusd_state_cache
-- pattern). Mac walker (escrow_supply_walker.py) refreshes every 15 min.
-- Kills ~52 walker_node_fallback rows/hr for walker_name=escrow_supply.
CREATE TABLE IF NOT EXISTS escrow_supply_snapshot (
    id                INTEGER PRIMARY KEY,
    total_xrp         NUMERIC(24, 6) NOT NULL,
    object_count      INTEGER NOT NULL,
    accounts_scanned  INTEGER NOT NULL,
    accounts_total    INTEGER NOT NULL,
    ledger_index      BIGINT NOT NULL,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (id = 1)
);
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


def _resolve_ipv4_hostaddr(url):
    """Return the URL host resolved to an IPv4 address, or None on any
    failure. Callers pass this as psycopg.connect(hostaddr=...) so libpq
    pins the TCP target to IPv4 while still using the hostname for TLS
    SNI and cert verification.

    Guards against the 2026-08-19 Render deploy loop: Neon's pooler
    returned AAAA records the outbound network couldn't route ("Network
    is unreachable" on 2600:1f16:...), and libpq stopped at the first
    address family it got. None-on-failure preserves the previous DNS
    path when IPv4 resolution isn't available."""
    import socket
    from urllib.parse import urlparse
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return None
        infos = socket.getaddrinfo(
            hostname, None, socket.AF_INET, socket.SOCK_STREAM,
        )
        if not infos:
            return None
        return infos[0][4][0]
    except Exception:
        return None


@contextmanager
def pg_connect():
    """Context-managed psycopg connection for short-lived (request-scope)
    work. Raises RuntimeError when Postgres isn't configured — callers
    should gate with pg_available() or wrap in a try/except."""
    if not pg_available():
        raise RuntimeError(
            "Postgres not configured: set DATABASE_URL and install psycopg[binary]."
        )
    # DATABASE_URL_DIRECT parity with _get_writer_conn: 2026-08-31 caught a
    # walker failing on statement_timeout error against the pooler URL.
    # _get_writer_conn honored DATABASE_URL_DIRECT, but pg_connect() did not,
    # so ANY read path (read_credentials_snapshot etc.) blew up on pool.
    _url = os.environ.get("DATABASE_URL_DIRECT", "").strip() or pg_url()
    _v4 = _resolve_ipv4_hostaddr(_url)
    _extra = {"hostaddr": _v4} if _v4 else {}
    conn = psycopg.connect(
        _url,
        connect_timeout=15,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
        **_extra,
    )
    try:
        # Post-connect SET (not startup `options=`) — Neon's PgBouncer pooler
        # rejects statement_timeout in the startup packet ("unsupported
        # startup parameter in options"). Applied here so every request-scope
        # connection is capped without depending on server-side defaults.
        with conn.cursor() as _cur:
            _cur.execute("SET statement_timeout = '25s'")
        yield conn
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# /healthz memoized connection — see ping() below.
# ─────────────────────────────────────────────────────────────────────

_healthz_conn = None
_healthz_conn_lock = threading.Lock()


def _open_healthz_conn():
    """Open a persistent probe connection. Mirrors _get_writer_conn's
    pattern: prefer DATABASE_URL_DIRECT (Neon unpooled) so session-level
    SET statement_timeout sticks across autocommit queries; fall back to
    pooled DATABASE_URL if DIRECT is unset. Autocommit + TCP keepalives
    so a half-dead socket surfaces as an error rather than blocking.

    statement_timeout='3s' here (not walker-side 25s) — /healthz sits
    behind Render's 5s health-check ceiling. If a SELECT 1 stalls past
    3s, we'd rather surface QueryCanceled than let Render's probe time
    out and start SIGTERM-ing the worker."""
    url = os.environ.get("DATABASE_URL_DIRECT", "").strip() or pg_url()
    _v4 = _resolve_ipv4_hostaddr(url)
    _extra = {"hostaddr": _v4} if _v4 else {}
    conn = psycopg.connect(
        url,
        autocommit=True,
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
        **_extra,
    )
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '3s'")
    return conn


def _drop_healthz_conn():
    """Close and clear the memoized healthz conn so the next ping() reopens."""
    global _healthz_conn
    try:
        if _healthz_conn is not None:
            _healthz_conn.close()
    except Exception:
        pass
    _healthz_conn = None


def ping():
    """Cheap PG round-trip for the /healthz routing probe. Raises on any
    failure (connection, auth, network) — /healthz then returns 503.

    Memoized per worker process: first probe pays TCP + SSL + auth (~200ms
    warm Neon, up to 8s if Neon compute is cold-starting); subsequent
    probes on the same worker reuse the physical socket (~1-5ms).
    Drop-and-retry on any query failure so a broken socket surfaces as
    one failed probe rather than a stuck worker.

    Prior design (fresh pg_connect() every 10s) contributed to the
    2026-08-17 + 08-19 + 08-28 Neon cold-start flap-storms: connect +
    SSL + auth against a scale-to-zero pooler routinely blew past
    Render's 5s ceiling. Refactor here + scale-to-zero OFF are belt +
    suspenders.

    Kept minimal so /healthz can answer 'can this container serve
    traffic?' without touching walker heartbeats — the 2026-08-07
    outage (project_healthz_outage_2026-08-07.md) proved that gating
    routing on walker liveness lets one stale Mac walker take the
    whole public site down."""
    global _healthz_conn
    with _healthz_conn_lock:
        try:
            if _healthz_conn is None or _healthz_conn.closed:
                _healthz_conn = _open_healthz_conn()
            with _healthz_conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return
        except Exception:
            _drop_healthz_conn()
        # One retry with a fresh conn — if this also fails, let it raise
        # so /healthz reports 503.
        _healthz_conn = _open_healthz_conn()
        with _healthz_conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()


@contextmanager
def rpc_loop_safe_pg_connect():
    """Use ONLY when a Postgres conn MUST survive across external RPC calls
    (e.g., a walker paginating XRPL AccountTx). This is the last-resort
    connection helper.

    Preferred hierarchy (top to bottom):
      1. write_* helpers in this module (write_event, write_signer_row,
         write_signed_snapshot, ...) — go through the cached, keepalived,
         autocommit _get_writer_conn(). Use these whenever possible.
      2. pg_connect() — short-lived, request-scope. Open, run your SQL,
         exit the context. Never hold across a network call.
      3. rpc_loop_safe_pg_connect() — THIS one. Only when 1 and 2 don't fit
         because the caller genuinely needs one conn open across many RPCs
         (e.g., writing one row per XRPL page as pagination advances).

    Why this exists (invariant this helper defends):
      A plain pg_connect() held across a long RPC loop is fatal against
      Neon Postgres: the socket goes idle during the network wait, Neon's
      pooler closes it, and the eventual conn.commit() dies with
      ProtocolViolation. Bridge_signer_walker crashed hourly for 37h on
      exactly this pattern (2026-07-16 → 2026-07-17).

    Fix mechanics: autocommit=True (every row commits immediately, no
    within-run window to lose data) + TCP keepalives (Neon's pooler sees
    traffic on the socket during long RPC gaps and leaves it open).

    Callers must still gate with pg_available() or catch RuntimeError."""
    if not pg_available():
        raise RuntimeError(
            "Postgres not configured: set DATABASE_URL and install psycopg[binary]."
        )
    # DATABASE_URL_DIRECT parity — same rationale as pg_connect() above.
    # rpc_loop_safe callers (walker RPC loops) also need the direct endpoint
    # when it's set, otherwise pool-side startup-parameter rejection bites.
    _url = os.environ.get("DATABASE_URL_DIRECT", "").strip() or pg_url()
    conn = psycopg.connect(
        _url,
        autocommit=True,
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
    )
    try:
        # Post-connect SET (not startup `options=`) — Neon's PgBouncer pooler
        # rejects statement_timeout in the startup packet. Same lesson as
        # pg_connect + _get_writer_conn (08-19 `db26af3` / `321d738`). Extends
        # the 25s cap to walker-side RPC-loop-safe writes so a pathologically
        # slow query can't wedge the walker beyond its own timeout belt.
        with conn.cursor() as _cur:
            _cur.execute("SET statement_timeout = '25s'")
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
        _v4 = _resolve_ipv4_hostaddr(writer_url)
        _extra = {"hostaddr": _v4} if _v4 else {}
        _writer_conn = psycopg.connect(
            writer_url,
            autocommit=True,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
            **_extra,
        )
        # Post-connect SET (not startup `options=`) — Neon's PgBouncer pooler
        # rejects statement_timeout in the startup packet. Applied here so the
        # cached writer conn is capped from first use; when DATABASE_URL_DIRECT
        # (unpooled) is set the setting sticks for the connection lifetime.
        with _writer_conn.cursor() as _cur:
            _cur.execute("SET statement_timeout = '25s'")
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


def _writer_execute_with_retry(category, do_write):
    """Run do_write(conn) against the cached writer conn, retrying on
    transient connection errors (Neon SSL drops, server-closed-unexpectedly,
    socket EOF — all surface as psycopg.OperationalError / InterfaceError).

    Drops the cached conn between retries so attempt N+1 gets a fresh
    socket. On persistent failure or non-transient exception, logs via
    _log_err — preserving the visible-failure behavior _log_err provides.

    Silent no-op when Postgres isn't configured."""
    if psycopg is None or not pg_available():
        return
    last_exc = None
    for attempt in range(3):
        conn = _get_writer_conn()
        if conn is None:
            return
        try:
            do_write(conn)
            return
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            last_exc = e
            _drop_writer_conn()
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
                continue
        except Exception as e:
            _log_err(f"{category}_failed", e)
            _drop_writer_conn()
            return
    _log_err(f"{category}_failed_after_retries", last_exc)


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


def upsert_token_volume(currency, issuer, hour_bucket, path_type,
                        trade_delta=1, volume_xrp_delta=0.0):
    """Increment trade_count and volume_xrp for a
    (currency, issuer, hour_bucket, path_type) bucket.

    path_type is REQUIRED and must be one of {"AMM", "CLOB", "MIXED",
    "DIRECT", "AMM_LP", "UNKNOWN"}. NULL is reserved for pre-2026-08-02
    legacy rows and is never written by this helper (past isn't taggable —
    house law: old rows stay NULL, new rows carry a stated tag).

    volume_xrp_delta defaults to 0.0 for callers that don't have a priced
    value (AMM deposit/withdraw, or Payment for a token with no XRP pool
    above the token_prices dust gate). Silent no-op when PG isn't
    configured."""
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO token_volume "
                "(currency, issuer, hour_bucket, volume_xrp, trade_count, path_type) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (currency, issuer, hour_bucket, path_type) DO UPDATE "
                "SET trade_count = token_volume.trade_count + EXCLUDED.trade_count, "
                "    volume_xrp = token_volume.volume_xrp + EXCLUDED.volume_xrp",
                (currency, issuer, hour_bucket, volume_xrp_delta, trade_delta, path_type),
            )
    except Exception as e:
        _log_err("upsert_token_volume_failed", e)
        _drop_writer_conn()


def write_ai_crawler_hit(ts, ua_class, path, status):
    """Append one row to ai_crawler_hits. Called from the request path
    in app.py; MUST be allocation-cheap and MUST NOT raise into the
    caller — any failure is rate-log-only and drops the writer conn so
    the next request tries a fresh one.

    Silent no-op when PG isn't configured (dev/local)."""
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_crawler_hits (ts, ua_class, path, status) "
                "VALUES (%s, %s, %s, %s)",
                (ts, ua_class, path, status),
            )
    except Exception as e:
        _log_err("write_ai_crawler_hit_failed", e)
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


def upsert_ledger_entry_type_seen(deltas):
    """Batch upsert of LedgerEntryType observations.

    deltas: list[dict] each with keys
      entry_type, count_created, count_modified, count_deleted,
      min_ledger, min_ts, max_ledger, max_ts
    (min_* carries the batch's earliest observation so first_seen_* is
    stamped from the actual sighting, not the flush moment.)

    Returns list[dict] each with keys
      entry_type, was_insert (bool), first_seen_ledger
    so the caller can fire the novelty / canary logs. first_seen_* is
    NEVER moved backward on conflict — it belongs to the INSERT only.
    """
    if not pg_available() or not deltas:
        return []
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            results = []
            for d in deltas:
                cur.execute(
                    "INSERT INTO ledger_entry_type_seen "
                    "(entry_type, first_seen_ledger, first_seen_ts, "
                    " last_seen_ledger, last_seen_ts, "
                    " count_created, count_modified, count_deleted) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (entry_type) DO UPDATE SET "
                    " last_seen_ledger = GREATEST("
                    "   ledger_entry_type_seen.last_seen_ledger, EXCLUDED.last_seen_ledger), "
                    " last_seen_ts     = GREATEST("
                    "   ledger_entry_type_seen.last_seen_ts,     EXCLUDED.last_seen_ts), "
                    " count_created  = ledger_entry_type_seen.count_created  + EXCLUDED.count_created, "
                    " count_modified = ledger_entry_type_seen.count_modified + EXCLUDED.count_modified, "
                    " count_deleted  = ledger_entry_type_seen.count_deleted  + EXCLUDED.count_deleted "
                    "RETURNING (xmax = 0) AS was_insert, first_seen_ledger",
                    (d["entry_type"], d["min_ledger"], d["min_ts"],
                     d["max_ledger"], d["max_ts"],
                     int(d.get("count_created", 0)),
                     int(d.get("count_modified", 0)),
                     int(d.get("count_deleted", 0))),
                )
                row = cur.fetchone()
                results.append({
                    "entry_type": d["entry_type"],
                    "was_insert": bool(row[0]),
                    "first_seen_ledger": int(row[1]),
                })
            conn.commit()
            return results
    except Exception as e:
        _log_err("upsert_ledger_entry_type_seen_failed", e)
        return []


def upsert_tx_type_seen(deltas):
    """Batch upsert of TransactionType observations. Same contract as
    upsert_ledger_entry_type_seen — first_seen_* stamped from the batch's
    earliest sighting, never moved on conflict; returns was_insert per
    row so the caller can fire canary logic on first-appearance."""
    if not pg_available() or not deltas:
        return []
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            results = []
            for d in deltas:
                cur.execute(
                    "INSERT INTO tx_type_seen "
                    "(tx_type, first_seen_ledger, first_seen_ts, "
                    " last_seen_ledger, last_seen_ts, count_total) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (tx_type) DO UPDATE SET "
                    " last_seen_ledger = GREATEST("
                    "   tx_type_seen.last_seen_ledger, EXCLUDED.last_seen_ledger), "
                    " last_seen_ts     = GREATEST("
                    "   tx_type_seen.last_seen_ts,     EXCLUDED.last_seen_ts), "
                    " count_total = tx_type_seen.count_total + EXCLUDED.count_total "
                    "RETURNING (xmax = 0) AS was_insert, first_seen_ledger",
                    (d["tx_type"], d["min_ledger"], d["min_ts"],
                     d["max_ledger"], d["max_ts"],
                     int(d.get("count_total", 0))),
                )
                row = cur.fetchone()
                results.append({
                    "tx_type": d["tx_type"],
                    "was_insert": bool(row[0]),
                    "first_seen_ledger": int(row[1]),
                })
            conn.commit()
            return results
    except Exception as e:
        _log_err("upsert_tx_type_seen_failed", e)
        return []


def check_type_defined(kind, name):
    """Look up whether `name` is in the current ledger_definitions singleton.
    kind is "tx" or "entry".

    Returns True/False on a successful lookup, or None when the singleton
    is unreachable or empty at check time — which the caller must render
    as `!!! CANARY CHECK UNAVAILABLE`, distinct from a false positive.
    Neither cry-wolf on the loudest alarm nor silently skip.
    """
    if not pg_available():
        return None
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            col = "tx_types" if kind == "tx" else "entry_types"
            cur.execute(
                f"SELECT {col} ? %s FROM ledger_definitions WHERE id = 1",
                (name,),
            )
            row = cur.fetchone()
            if not row:
                # Singleton missing — Phase 0 walker hasn't run since a
                # DB wipe. Distinct from "type genuinely absent from vocab."
                return None
            return bool(row[0])
    except Exception as e:
        _log_err("check_type_defined_failed", e)
        return None


def read_ledger_definitions():
    """Return the current ledger_definitions singleton as a dict, or None.
    Consumed by the Coverage Register (Phase 1b) as the authoritative
    "what the node knows" column of the three-way diff."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT hash, fetched_at, build_version, tx_types, "
                "entry_types, payload FROM ledger_definitions WHERE id = 1"
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "hash": row[0],
                "fetched_at": row[1],
                "build_version": row[2],
                "tx_types": row[3],
                "entry_types": row[4],
                "payload": row[5],
            }
    except Exception as e:
        _log_err("read_ledger_definitions_failed", e)
        return None


def write_ledger_definitions(hash_val, fetched_at, build_version,
                             tx_types, entry_types, payload):
    """Upsert the ledger_definitions singleton. If hash_val differs from
    the previous row, append a delta row to ledger_definitions_history
    with the full payload for forensic recovery of field-level changes
    that don't show up in add/remove sets.

    Returns dict:
      {"changed": bool, "hash_prev": str|None,
       "tx_types_added": [str], "tx_types_removed": [str],
       "entry_types_added": [str], "entry_types_removed": [str]}
    Caller uses this to loud-log the change (with a distinct louder tag
    for removals, per operator note — removals mean node downgrade or
    something genuinely wrong)."""
    if not pg_available():
        return {"changed": False, "hash_prev": None,
                "tx_types_added": [], "tx_types_removed": [],
                "entry_types_added": [], "entry_types_removed": []}
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT hash, tx_types, entry_types "
                "FROM ledger_definitions WHERE id = 1"
            )
            prev = cur.fetchone()
            prev_hash = prev[0] if prev else None
            prev_tx = set((prev[1] or {}).keys()) if prev else set()
            prev_entry = set((prev[2] or {}).keys()) if prev else set()
            new_tx = set(tx_types.keys())
            new_entry = set(entry_types.keys())

            changed = (prev_hash != hash_val)
            tx_added = sorted(new_tx - prev_tx)
            tx_removed = sorted(prev_tx - new_tx)
            entry_added = sorted(new_entry - prev_entry)
            entry_removed = sorted(prev_entry - new_entry)

            payload_json = json.dumps(payload)
            tx_types_json = json.dumps(tx_types)
            entry_types_json = json.dumps(entry_types)

            cur.execute(
                "INSERT INTO ledger_definitions "
                "(id, hash, fetched_at, build_version, tx_types, "
                " entry_types, payload) "
                "VALUES (1, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb) "
                "ON CONFLICT (id) DO UPDATE SET "
                " hash          = EXCLUDED.hash, "
                " fetched_at    = EXCLUDED.fetched_at, "
                " build_version = EXCLUDED.build_version, "
                " tx_types      = EXCLUDED.tx_types, "
                " entry_types   = EXCLUDED.entry_types, "
                " payload       = EXCLUDED.payload",
                (hash_val, fetched_at, build_version,
                 tx_types_json, entry_types_json, payload_json),
            )
            if changed:
                cur.execute(
                    "INSERT INTO ledger_definitions_history "
                    "(fetched_at, hash, hash_prev, build_version, "
                    " tx_types_added, tx_types_removed, "
                    " entry_types_added, entry_types_removed, payload) "
                    "VALUES (%s, %s, %s, %s, "
                    "        %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, "
                    "        %s::jsonb)",
                    (fetched_at, hash_val, prev_hash, build_version,
                     json.dumps(tx_added), json.dumps(tx_removed),
                     json.dumps(entry_added), json.dumps(entry_removed),
                     payload_json),
                )
            conn.commit()
            return {
                "changed": changed,
                "hash_prev": prev_hash,
                "tx_types_added": tx_added,
                "tx_types_removed": tx_removed,
                "entry_types_added": entry_added,
                "entry_types_removed": entry_removed,
            }
    except Exception as e:
        _log_err("write_ledger_definitions_failed", e)
        return {"changed": False, "hash_prev": None,
                "tx_types_added": [], "tx_types_removed": [],
                "entry_types_added": [], "entry_types_removed": []}


# ─────────────────────────────────────────────────────────────────────
# Phase 1b Coverage Register helpers
# ─────────────────────────────────────────────────────────────────────


def read_walker_scope_declarations():
    """Return {walker_name: dict} of every declared scope. Consumed by the
    Coverage Register to render PARTIAL badges and by the walker itself to
    flag walker_health rows lacking a declaration (UNDECLARED — its own
    alarm class, discharges the escrow-lesson obligation)."""
    if not pg_available():
        return {}
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT walker_name, declared_scope, filter_note, "
                "honest_partial, updated_at "
                "FROM walker_scope_declarations"
            )
            return {
                row[0]: {
                    "walker_name": row[0],
                    "declared_scope": row[1],
                    "filter_note": row[2],
                    "honest_partial": bool(row[3]),
                    "updated_at": int(row[4]),
                }
                for row in cur.fetchall()
            }
    except Exception as e:
        _log_err("read_walker_scope_declarations_failed", e)
        return {}


def upsert_walker_scope_declaration(walker_name, declared_scope,
                                    filter_note, honest_partial):
    """Seed / update a single walker's declared scope. `updated_at` is
    stamped from time.time() at write; rendered informationally on the
    register, never freshness-gated."""
    if not pg_available():
        return False
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO walker_scope_declarations "
                "(walker_name, declared_scope, filter_note, honest_partial, updated_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (walker_name) DO UPDATE SET "
                " declared_scope = EXCLUDED.declared_scope, "
                " filter_note    = EXCLUDED.filter_note, "
                " honest_partial = EXCLUDED.honest_partial, "
                " updated_at     = EXCLUDED.updated_at",
                (walker_name, declared_scope, filter_note,
                 bool(honest_partial), int(time.time())),
            )
            conn.commit()
            return True
    except Exception as e:
        _log_err("upsert_walker_scope_declaration_failed", e)
        return False


def read_coverage_labels():
    """Return {(kind, name): dict} of curated display labels."""
    if not pg_available():
        return {}
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT kind, name, label, short_desc, linked_page, updated_at "
                "FROM coverage_labels"
            )
            return {
                (row[0], row[1]): {
                    "kind": row[0],
                    "name": row[1],
                    "label": row[2],
                    "short_desc": row[3],
                    "linked_page": row[4],
                    "updated_at": int(row[5]),
                }
                for row in cur.fetchall()
            }
    except Exception as e:
        _log_err("read_coverage_labels_failed", e)
        return {}


def upsert_coverage_label(kind, name, label, short_desc, linked_page=None):
    """Seed / update a curated (kind, name) label. Idempotent."""
    if not pg_available():
        return False
    if kind not in ("tx", "entry"):
        raise ValueError(f"kind must be 'tx' or 'entry', got {kind!r}")
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO coverage_labels "
                "(kind, name, label, short_desc, linked_page, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (kind, name) DO UPDATE SET "
                " label       = EXCLUDED.label, "
                " short_desc  = EXCLUDED.short_desc, "
                " linked_page = EXCLUDED.linked_page, "
                " updated_at  = EXCLUDED.updated_at",
                (kind, name, label, short_desc, linked_page, int(time.time())),
            )
            conn.commit()
            return True
    except Exception as e:
        _log_err("upsert_coverage_label_failed", e)
        return False


_STALE_MULTIPLE = 3
_STALE_FLOOR_SECONDS = 1800


def compute_walker_staleness(cadence_seconds, last_success_ts, now_ts):
    """Read-time derived STALE state for a walker_health row.

    Pure arithmetic — no I/O, no writers, no new tables. Called on every
    /coverage render. Opt-in via declared cadence: walkers without
    cadence_seconds never STALE (event-triggered / manual / paused).

    Threshold: max(cadence × 3, 30 min). N=3 covers scheduler jitter +
    one failed-and-retried cycle + margin. 30-min floor prevents
    short-cadence walkers from false-STALE during slow deploys.

    Cross-exam verdict 2026-08-06 (project_walker_liveness_cross_exam
    _2026-08-06.md answer #1, #2, #6). Would have caught Patient A
    (ledger_definitions, macOS 26 LNP block) and Patient B (rank_amms,
    --reset hardcoded) at their observed stale ages.
    """
    if not cadence_seconds:
        return {"is_stale": False, "stale_seconds": None,
                "stale_threshold": None}
    threshold = max(_STALE_MULTIPLE * int(cadence_seconds),
                    _STALE_FLOOR_SECONDS)
    if last_success_ts is None:
        return {"is_stale": True, "stale_seconds": None,
                "stale_threshold": threshold}
    stale_seconds = now_ts - last_success_ts
    return {"is_stale": stale_seconds > threshold,
            "stale_seconds": stale_seconds,
            "stale_threshold": threshold}


def read_coverage_register_state():
    """Compute the three-way diff on read from Phase 0 (vocabulary),
    Phase 1a (seen tables), coverage_labels, walker_scope_declarations,
    and walker_health.

    Returns dict:
      {
        "definitions": {hash, build_version, fetched_at,
                        defined_tx_names, defined_entry_names},
        "seen_tx":     [{name, first_seen_ledger, last_seen_ledger,
                         last_seen_ts, count_total, label?, short_desc?,
                         linked_page?}, ...],
        "seen_entry":  [{name, ..., count_created, count_modified,
                         count_deleted, label?, ...}, ...],
        "defined_but_unseen_tx":    [name, ...],
        "defined_but_unseen_entry": [name, ...],
        "seen_but_undefined_tx":    [name, ...],  # SCREAM state
        "seen_but_undefined_entry": [name, ...],
        "unlabeled_tx":             [name, ...],  # amber curation-debt
        "unlabeled_entry":          [name, ...],
        "walker_scopes":            {walker_name: {...}},
        "walker_health":            {walker_name: {ok, last_success_at,
                                                   cadence_seconds,
                                                   is_stale, undeclared}},
      }

    Returns None if PG unreachable — caller should render "REGISTER
    UNAVAILABLE" as the honest degrade state.
    """
    if not pg_available():
        return None
    try:
        defs = read_ledger_definitions()
        if defs is None:
            return None
        defined_tx = set((defs.get("tx_types") or {}).keys())
        defined_entry = set((defs.get("entry_types") or {}).keys())

        labels = read_coverage_labels()
        scopes = read_walker_scope_declarations()

        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tx_type, first_seen_ledger, first_seen_ts, "
                "last_seen_ledger, last_seen_ts, count_total "
                "FROM tx_type_seen ORDER BY tx_type"
            )
            seen_tx_rows = cur.fetchall()

            cur.execute(
                "SELECT entry_type, first_seen_ledger, first_seen_ts, "
                "last_seen_ledger, last_seen_ts, "
                "count_created, count_modified, count_deleted "
                "FROM ledger_entry_type_seen ORDER BY entry_type"
            )
            seen_entry_rows = cur.fetchall()

            cur.execute(
                "SELECT walker_name, last_run_ok, last_success_at, "
                "cadence_seconds, consecutive_failures "
                "FROM walker_health"
            )
            wh_rows = cur.fetchall()

        seen_tx_names = {r[0] for r in seen_tx_rows}
        seen_entry_names = {r[0] for r in seen_entry_rows}

        def _tx_row(r):
            name = r[0]
            lab = labels.get(("tx", name))
            return {
                "name": name,
                "first_seen_ledger": int(r[1]),
                "first_seen_ts": int(r[2]),
                "last_seen_ledger": int(r[3]),
                "last_seen_ts": int(r[4]),
                "count_total": int(r[5]),
                "label": lab["label"] if lab else None,
                "short_desc": lab["short_desc"] if lab else None,
                "linked_page": lab["linked_page"] if lab else None,
                "labeled": lab is not None,
                "defined": name in defined_tx,
            }

        def _entry_row(r):
            name = r[0]
            lab = labels.get(("entry", name))
            return {
                "name": name,
                "first_seen_ledger": int(r[1]),
                "first_seen_ts": int(r[2]),
                "last_seen_ledger": int(r[3]),
                "last_seen_ts": int(r[4]),
                "count_created": int(r[5]),
                "count_modified": int(r[6]),
                "count_deleted": int(r[7]),
                "label": lab["label"] if lab else None,
                "short_desc": lab["short_desc"] if lab else None,
                "linked_page": lab["linked_page"] if lab else None,
                "labeled": lab is not None,
                "defined": name in defined_entry,
            }

        seen_tx = [_tx_row(r) for r in seen_tx_rows]
        seen_entry = [_entry_row(r) for r in seen_entry_rows]

        now = int(time.time())
        wh = {}
        for r in wh_rows:
            wname = r[0]
            last_success = r[2]
            cadence = r[3]
            last_success_ts = (
                int(last_success.timestamp()) if last_success else None
            )
            staleness = compute_walker_staleness(
                cadence_seconds=int(cadence) if cadence else None,
                last_success_ts=last_success_ts,
                now_ts=now,
            )
            wh[wname] = {
                "walker_name": wname,
                "last_run_ok": bool(r[1]),
                "last_success_ts": last_success_ts,
                "cadence_seconds": int(cadence) if cadence else None,
                "consecutive_failures": int(r[4]),
                **staleness,
                "undeclared": wname not in scopes,
            }

        # Flat lookup maps so the template can resolve labels for
        # defined-but-unseen rows without re-scanning seen_tx / seen_entry.
        labels_tx = {
            name: {
                "label": lab["label"],
                "short_desc": lab["short_desc"],
                "linked_page": lab["linked_page"],
            }
            for (k, name), lab in labels.items() if k == "tx"
        }
        labels_entry = {
            name: {
                "label": lab["label"],
                "short_desc": lab["short_desc"],
                "linked_page": lab["linked_page"],
            }
            for (k, name), lab in labels.items() if k == "entry"
        }

        return {
            "definitions": {
                "hash": defs.get("hash"),
                "build_version": defs.get("build_version"),
                "fetched_at": defs.get("fetched_at"),
                "defined_tx_names": sorted(defined_tx),
                "defined_entry_names": sorted(defined_entry),
                "defined_tx_count": len(defined_tx),
                "defined_entry_count": len(defined_entry),
            },
            "seen_tx": seen_tx,
            "seen_entry": seen_entry,
            "defined_but_unseen_tx": sorted(defined_tx - seen_tx_names),
            "defined_but_unseen_entry": sorted(defined_entry - seen_entry_names),
            "seen_but_undefined_tx": sorted(seen_tx_names - defined_tx),
            "seen_but_undefined_entry": sorted(seen_entry_names - defined_entry),
            "unlabeled_tx": [
                r["name"] for r in seen_tx if not r["labeled"]
            ],
            "unlabeled_entry": [
                r["name"] for r in seen_entry if not r["labeled"]
            ],
            "labels_tx": labels_tx,
            "labels_entry": labels_entry,
            "walker_scopes": scopes,
            "walker_health": wh,
        }
    except Exception as e:
        _log_err("read_coverage_register_state_failed", e)
        return None


def append_coverage_register_history(state, min_interval_seconds=86400):
    """Append a snapshot row iff state differs from the most recent row
    OR min_interval_seconds elapsed since last row (guarantee floor for
    the record artifact in quiescent weeks). Called only by
    coverage_register_walker — no request-side writers.

    Returns dict: {"appended": bool, "reason": str}."""
    if not pg_available() or state is None:
        return {"appended": False, "reason": "unavailable"}
    now = int(time.time())
    payload = {
        "definitions_hash": state["definitions"]["hash"],
        "defined_tx_count": state["definitions"]["defined_tx_count"],
        "defined_entry_count": state["definitions"]["defined_entry_count"],
        "seen_tx_count": len(state["seen_tx"]),
        "seen_entry_count": len(state["seen_entry"]),
        "undefined_tx": state["seen_but_undefined_tx"],
        "undefined_entry": state["seen_but_undefined_entry"],
        "stale_walkers": sorted(
            w for w, h in state["walker_health"].items() if h["is_stale"]
        ),
    }
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT fetched_at, definitions_hash, defined_tx_count, "
                "defined_entry_count, seen_tx_count, seen_entry_count, "
                "undefined_tx, undefined_entry, stale_walkers "
                "FROM coverage_register_history "
                "ORDER BY fetched_at DESC LIMIT 1"
            )
            prev = cur.fetchone()

            new_since_last = []
            if prev is None:
                reason = "seed"
                new_since_last = sorted(
                    [f"tx:{r['name']}" for r in state["seen_tx"]]
                    + [f"entry:{r['name']}" for r in state["seen_entry"]]
                )
            else:
                prev_state = {
                    "definitions_hash": prev[1],
                    "defined_tx_count": int(prev[2]),
                    "defined_entry_count": int(prev[3]),
                    "seen_tx_count": int(prev[4]),
                    "seen_entry_count": int(prev[5]),
                    "undefined_tx": list(prev[6] or []),
                    "undefined_entry": list(prev[7] or []),
                    "stale_walkers": list(prev[8] or []),
                }
                differs = any(
                    payload[k] != prev_state[k] for k in prev_state
                )
                elapsed = now - int(prev[0])
                if differs:
                    reason = "state_changed"
                    prev_tx_count = prev_state["seen_tx_count"]
                    prev_entry_count = prev_state["seen_entry_count"]
                    if (payload["seen_tx_count"] > prev_tx_count
                            or payload["seen_entry_count"] > prev_entry_count):
                        new_since_last = [
                            f"tx_delta:+{payload['seen_tx_count']-prev_tx_count}",
                            f"entry_delta:+{payload['seen_entry_count']-prev_entry_count}",
                        ]
                elif elapsed >= min_interval_seconds:
                    reason = "24h_floor"
                else:
                    return {"appended": False, "reason": "no_change"}

            cur.execute(
                "INSERT INTO coverage_register_history "
                "(fetched_at, definitions_hash, defined_tx_count, "
                " defined_entry_count, seen_tx_count, seen_entry_count, "
                " undefined_tx, undefined_entry, new_since_last, "
                " stale_walkers) "
                "VALUES (%s, %s, %s, %s, %s, %s, "
                "        %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb)",
                (now,
                 payload["definitions_hash"],
                 payload["defined_tx_count"],
                 payload["defined_entry_count"],
                 payload["seen_tx_count"],
                 payload["seen_entry_count"],
                 json.dumps(payload["undefined_tx"]),
                 json.dumps(payload["undefined_entry"]),
                 json.dumps(new_since_last),
                 json.dumps(payload["stale_walkers"])),
            )
            conn.commit()
            return {"appended": True, "reason": reason}
    except Exception as e:
        _log_err("append_coverage_register_history_failed", e)
        return {"appended": False, "reason": f"exception:{type(e).__name__}"}


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
    # FD-count telemetry — surfaces the FD curve per worker so a leak (steady
    # climb) is legible in walker_health at a glance vs. a spike (one-time
    # jump). Diagnostic added after BetterStack alert 2026-07-26 (xrpl_stream
    # hit macOS's 256-FD default and crashed). listdir('/dev/fd') is the
    # macOS-portable /proc/self/fd equivalent; best-effort, never raises.
    try:
        import resource
        fd_count = len(os.listdir("/dev/fd"))
        fd_soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        extra = dict(extra) if extra else {}
        extra["fd_count"] = fd_count
        extra["fd_soft"] = fd_soft
    except Exception:
        pass
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


def read_rwa_families():
    """Return every rwa_family row with its attributed pool count.

    Shape mirrors what /rwa renders (app.py:3033) but returns a flat count
    of attributed pools rather than the array — the MCP tool exposes the
    count for agents; the human page keeps the addresses. None on PG
    unavailable so the caller can raise a distinct RuntimeError instead
    of returning an empty list that looks like "no families exist."""
    if not pg_available():
        return None
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT f.family_slug, f.family_name, f.description,
                       f.external_url, f.attestation_level,
                       COUNT(p.pool_address) AS pool_count
                  FROM rwa_family f
             LEFT JOIN rwa_pool_attribution p
                    ON f.family_slug = p.family_slug
              GROUP BY f.family_slug, f.family_name, f.description,
                       f.external_url, f.attestation_level
              ORDER BY pool_count DESC NULLS LAST, f.family_name
            """)
            return [
                {
                    "family_slug": r[0],
                    "family_name": r[1],
                    "description": r[2],
                    "external_url": r[3],
                    "attestation_level": r[4],
                    "pool_count": int(r[5] or 0),
                }
                for r in cur.fetchall()
            ]


def read_rwa_pools_attributed():
    """Return every rwa_pool_attribution row with its family slug and
    provenance. Left-join against amm_ranked_pools so agents get the
    TVL alongside the attribution — the two datasets are curated
    independently but always read together on /rwa (app.py:3086).
    None on PG unavailable."""
    if not pg_available():
        return None
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.pool_address, p.family_slug, p.confidence,
                       p.provenance, p.notes, a.tvl_usd, a.tvl_status
                  FROM rwa_pool_attribution p
             LEFT JOIN amm_ranked_pools a
                    ON p.pool_address = a.amm_account
              ORDER BY p.family_slug, p.pool_address
            """)
            return [
                {
                    "pool_address": r[0],
                    "family_slug": r[1],
                    "confidence": r[2],
                    "provenance": r[3],
                    "notes": r[4],
                    "tvl_usd": r[5],
                    "tvl_status": r[6],
                }
                for r in cur.fetchall()
            ]


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
    def _do(conn):
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
    _writer_execute_with_retry(f"write_walker_health_start[{walker_name}]", _do)


def write_walker_health_end(walker_name, ok, message=None, findings_count=None):
    """Update walker_health with the run outcome. On ok=True: stamps
    last_success_at=now() and zeroes consecutive_failures. On ok=False:
    stamps last_failure_at=now() and increments consecutive_failures.
    The row must already exist (start-of-run write created it); if not,
    we still UPSERT so a walker that forgot to call start isn't invisible.
    Silent no-op when PG isn't configured.

    findings_count is a distinct signal from ok/consecutive_failures: a
    walker whose job is to surface vulnerabilities (pip_audit_walker) can
    complete cleanly AND report N findings — that's ok=True + findings_count=N.
    Only true run failures (crash/timeout/subprocess broke) set ok=False
    and increment consecutive_failures. Pass None (default) to leave the
    column untouched; pass 0 to explicitly clear it after fixes land.
    """
    def _do(conn):
        with conn.cursor() as cur:
            if ok:
                cur.execute(
                    "INSERT INTO walker_health "
                    "  (walker_name, last_run_started, last_run_completed, "
                    "   last_run_ok, last_run_message, last_success_at, "
                    "   consecutive_failures, findings_count) "
                    "VALUES (%s, now(), now(), true, %s, now(), 0, %s) "
                    "ON CONFLICT (walker_name) DO UPDATE SET "
                    "  last_run_completed = now(), "
                    "  last_run_ok = true, "
                    "  last_run_message = EXCLUDED.last_run_message, "
                    "  last_success_at = now(), "
                    "  consecutive_failures = 0, "
                    "  findings_count = EXCLUDED.findings_count",
                    (walker_name, message, findings_count),
                )
            else:
                cur.execute(
                    "INSERT INTO walker_health "
                    "  (walker_name, last_run_started, last_run_completed, "
                    "   last_run_ok, last_run_message, last_failure_at, "
                    "   consecutive_failures, findings_count) "
                    "VALUES (%s, now(), now(), false, %s, now(), 1, %s) "
                    "ON CONFLICT (walker_name) DO UPDATE SET "
                    "  last_run_completed = now(), "
                    "  last_run_ok = false, "
                    "  last_run_message = EXCLUDED.last_run_message, "
                    "  last_failure_at = now(), "
                    "  consecutive_failures = walker_health.consecutive_failures + 1, "
                    "  findings_count = EXCLUDED.findings_count",
                    (walker_name, message, findings_count),
                )
    _writer_execute_with_retry(f"write_walker_health_end[{walker_name}]", _do)


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
                    "       consecutive_failures, cadence_seconds, "
                    "       findings_count "
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
                        "findings_count": row[9],
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
                    "       consecutive_failures, findings_count "
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
                    "findings_count": row[7],
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


def write_whales_cache_daily_delta(hits_delta, misses_delta, blocked_delta=0):
    """Roll /whales in-process cache hit/miss deltas into a daily receipts row.

    Called on every cache miss (deltas since last flush) and opportunistically
    on cache hits when the last flush was >5min ago, so worker recycling only
    takes at most ~5min of unflushed hits to the grave. Residual undercount is
    honest and bounded; the whole point of these receipts is decision-grade
    evidence for the future API-gate conversation, so they need to be
    trustworthy rather than pretty.

    blocked_delta counts requests turned away by the temporary IL/Chrome-142
    fleet block before the cache is even consulted.

    Table auto-creates on first call — schema is tiny and this is best-effort
    telemetry, no separate migration warranted. Row shape:
    (date, hits, misses, blocked, last_updated). Multi-worker safe because the
    upsert does += delta, not = total.
    """
    if not pg_available():
        return False
    if hits_delta == 0 and misses_delta == 0 and blocked_delta == 0:
        return False
    conn = _get_writer_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS whales_cache_daily ("
                "    date DATE PRIMARY KEY,"
                "    hits BIGINT NOT NULL DEFAULT 0,"
                "    misses BIGINT NOT NULL DEFAULT 0,"
                "    blocked BIGINT NOT NULL DEFAULT 0,"
                "    last_updated TIMESTAMPTZ"
                ")"
            )
            cur.execute(
                "INSERT INTO whales_cache_daily "
                "    (date, hits, misses, blocked, last_updated) "
                "VALUES (CURRENT_DATE, %s, %s, %s, NOW()) "
                "ON CONFLICT (date) DO UPDATE SET "
                "    hits = whales_cache_daily.hits + EXCLUDED.hits, "
                "    misses = whales_cache_daily.misses + EXCLUDED.misses, "
                "    blocked = whales_cache_daily.blocked + EXCLUDED.blocked, "
                "    last_updated = EXCLUDED.last_updated",
                (hits_delta, misses_delta, blocked_delta),
            )
        return True
    except Exception as e:
        _log_err("write_whales_cache_daily_delta", e)
        return False


def write_analytics_cache_daily_delta(hits_delta, misses_delta):
    """Roll /analytics in-process cache hit/miss deltas into a daily receipts row.

    Deliberately parallel to write_whales_cache_daily_delta — same shape,
    separate table. Two mirror tables is minimal build; consolidation into a
    single page_cache_daily with a page column can happen if a third mirror
    ever appears. Row shape: (date, hits, misses, last_updated). Upsert
    does += delta, so multi-worker safe."""
    if not pg_available():
        return False
    if hits_delta == 0 and misses_delta == 0:
        return False
    conn = _get_writer_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS analytics_cache_daily ("
                "    date DATE PRIMARY KEY,"
                "    hits BIGINT NOT NULL DEFAULT 0,"
                "    misses BIGINT NOT NULL DEFAULT 0,"
                "    last_updated TIMESTAMPTZ"
                ")"
            )
            cur.execute(
                "INSERT INTO analytics_cache_daily "
                "    (date, hits, misses, last_updated) "
                "VALUES (CURRENT_DATE, %s, %s, NOW()) "
                "ON CONFLICT (date) DO UPDATE SET "
                "    hits = analytics_cache_daily.hits + EXCLUDED.hits, "
                "    misses = analytics_cache_daily.misses + EXCLUDED.misses, "
                "    last_updated = EXCLUDED.last_updated",
                (hits_delta, misses_delta),
            )
        return True
    except Exception as e:
        _log_err("write_analytics_cache_daily_delta", e)
        return False


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
    # eth_mints_24h / eth_burns_24h in the history table mean UTC calendar day
    # for the row's snapshot_date, NOT the trailing-24h rolling number the live
    # /rlusd footer shows. The eth_branch payload carries both: `mints_24h`
    # (rolling, for live) and `mints_calendar_today` (today's UTC day so far,
    # for the row we're writing here). Same for burns. See
    # project_xrpldashboard_rlusd_false_flat_2026-07-17 + Charlie's 2026-07-18
    # calendar-day mandate ("row labeled with a date is a claim about that
    # date"). Old rows carried trailing-24h wearing date labels — accidentally
    # correct on days near midnight, off by up to a full day otherwise.
    eth_mints_today = eth_branch.get("mints_calendar_today")
    eth_burns_today = eth_branch.get("burns_calendar_today")
    # XRPL side: net supply change from gateway_balances snapshot-diff
    # (Option A). Mirrors ETH structure — the "_today" value is used for
    # today's row, "_prev" finalizes yesterday's row below. See
    # rlusd_xrpl_option_a.py for the boundary semantics.
    xrpl_net_change_today = xrpl_branch.get("net_change_calendar_today")
    conn = _get_writer_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rlusd_supply_history ("
                "    snapshot_date, xrpl_supply, eth_supply, total_supply, "
                "    xrpl_holders, eth_holders, "
                "    xrpl_mints_24h, xrpl_burns_24h, xrpl_net_change_24h, "
                "    eth_mints_24h, eth_burns_24h, "
                "    written_at_iso"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (snapshot_date) DO UPDATE SET "
                "    xrpl_supply         = EXCLUDED.xrpl_supply, "
                "    eth_supply          = EXCLUDED.eth_supply, "
                "    total_supply        = EXCLUDED.total_supply, "
                "    xrpl_holders        = EXCLUDED.xrpl_holders, "
                "    eth_holders         = EXCLUDED.eth_holders, "
                "    xrpl_mints_24h      = EXCLUDED.xrpl_mints_24h, "
                "    xrpl_burns_24h      = EXCLUDED.xrpl_burns_24h, "
                "    xrpl_net_change_24h = EXCLUDED.xrpl_net_change_24h, "
                "    eth_mints_24h       = EXCLUDED.eth_mints_24h, "
                "    eth_burns_24h       = EXCLUDED.eth_burns_24h, "
                "    written_at_iso      = EXCLUDED.written_at_iso",
                (
                    snapshot_date,
                    float(xrpl_supply),
                    float(eth_supply),
                    float(xrpl_supply) + float(eth_supply),
                    xrpl_branch.get("holders"),
                    eth_branch.get("holders"),
                    # xrpl_mints_24h / xrpl_burns_24h intentionally None:
                    # the Option A net-change column replaces them
                    # semantically. Historical rows keep whatever they had
                    # (NULL after the 2026-07-17 migration).
                    None,
                    None,
                    xrpl_net_change_today,
                    eth_mints_today,
                    eth_burns_today,
                    written_at_iso,
                ),
            )
            # Finalize yesterday's row with its fully-closed calendar-day
            # totals — if the walker didn't happen to run exactly at 00:00Z,
            # yesterday's row was written with "today so far" values that
            # under-count the last few minutes of the day. Each cycle we
            # overwrite yesterday's cells with the finalized numbers.
            # Idempotent by construction (both Etherscan and gateway_balances
            # return the same figures for a closed day every time).
            eth_mints_prev = eth_branch.get("mints_calendar_prev")
            eth_burns_prev = eth_branch.get("burns_calendar_prev")
            xrpl_net_prev = xrpl_branch.get("net_change_calendar_prev")
            prev_date = snapshot_date - datetime.timedelta(days=1)
            # ETH pair — updated together (both derived from the same
            # Etherscan call). Skip if either failed.
            if eth_mints_prev is not None and eth_burns_prev is not None:
                cur.execute(
                    "UPDATE rlusd_supply_history SET "
                    "  eth_mints_24h = %s, "
                    "  eth_burns_24h = %s "
                    "WHERE snapshot_date = %s",
                    (eth_mints_prev, eth_burns_prev, prev_date),
                )
            # XRPL net change — independent RPC path, skip if it failed.
            # Split from the ETH UPDATE so a single-chain outage doesn't
            # hold up the other chain's finalization.
            if xrpl_net_prev is not None:
                cur.execute(
                    "UPDATE rlusd_supply_history SET "
                    "  xrpl_net_change_24h = %s "
                    "WHERE snapshot_date = %s",
                    (xrpl_net_prev, prev_date),
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
      overwrite anything, including older curated labels — EXCEPT when
      the existing row carries `extra.name_locked = true`, in which case
      the name is preserved (metadata still refreshes). This protects
      hand-cleaned names (e.g., "Reaper Financial") from being clobbered
      by the weekly TOML rerun's auto-derived shape (e.g., "reaper.financial
      (Ascension issuer)" — last-write-wins across multi-token issuers).
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
                # name_locked path: preserve existing name + merge extra
                # so the lock flag itself survives. jsonb `||` favors right
                # side on key collision, so walker's fresh {mode, domain,
                # verified_via, verified_at_unix} still refreshes; existing
                # `name_locked` stays because walker's extra doesn't set it.
                cur.execute(
                    "INSERT INTO account_labels "
                    "(address, name, category, source, confidence, extra, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s) "
                    "ON CONFLICT (address) DO UPDATE SET "
                    "  name = CASE WHEN (account_labels.extra->>'name_locked')::boolean IS TRUE "
                    "              THEN account_labels.name "
                    "              ELSE EXCLUDED.name END, "
                    "  category = EXCLUDED.category, "
                    "  source = EXCLUDED.source, "
                    "  confidence = EXCLUDED.confidence, "
                    "  extra = CASE WHEN (account_labels.extra->>'name_locked')::boolean IS TRUE "
                    "               THEN account_labels.extra || EXCLUDED.extra "
                    "               ELSE EXCLUDED.extra END, "
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

def read_recent_events(limit=10, tagged_floor_drops=None):
    """Latest N rows from `events`, value-movement only. Mirrors the
    homepage SQLite query so the existing _resolve_event() resolver works
    unchanged. Excludes trustset (signal, not movement) so the homepage
    globe pulse and the institutional /api/whales/recent contract stay
    aligned with "large XRP transfer feed". Returns rows in column order:
    tx_hash, ledger_index, ts, type, from_addr, to_addr, amount_drops,
    currency, issuer, raw_json.

    tagged_floor_drops: if not None, tagged rows are only returned when
    amount_drops IS NULL (token-denominated) OR amount_drops >= this
    floor. Prevents sub-dollar watchlist activity from dominating the
    homepage card. Non-tagged types (large_xfer) pass regardless — they
    are already gated at capture time by xrpl_stream at its walker floor
    (default 50K XRP). Callers that need the higher /whales display
    floor (100K XRP) must filter downstream — see
    mcp_tools_value_flows.tool_get_whale_events for an example."""
    if tagged_floor_drops is None:
        sql = (
            "SELECT tx_hash, ledger_index, ts, type, from_addr, to_addr, "
            "amount_drops, currency, issuer, raw_json::text FROM events "
            "WHERE type != 'trustset' "
            "ORDER BY ts DESC LIMIT %s"
        )
        params = (limit,)
    else:
        sql = (
            "SELECT tx_hash, ledger_index, ts, type, from_addr, to_addr, "
            "amount_drops, currency, issuer, raw_json::text FROM events "
            "WHERE type != 'trustset' "
            "  AND (type != 'tagged' "
            "       OR amount_drops IS NULL "
            "       OR amount_drops >= %s) "
            "ORDER BY ts DESC LIMIT %s"
        )
        params = (tagged_floor_drops, limit)
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
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
    "%WhaleFlowRadar%",
    "%Claude-User%",
    "%HeadlessChrome%",
    "%Python-urllib%",
    "%BuiltWith%",
    "%FACTANKER%",
    "%xrpld-%",
    "%Palo Alto Networks%",
)

# Increment whenever BOT_PATH_PATTERNS, BOT_UA_PATTERNS, scanner thresholds,
# or any other _bot_filter_sql input changes. The is_bot writer detects a
# mismatch vs the last stored version and triggers a full resync pass.
# v2 (2026-07-26): scanner arm switched from trailing-7d snapshot
# (page_view_scanner_combos) to persistent confirmed ledger
# (page_view_scanner_combos_confirmed). Full resync required so historical
# rows are re-stamped against the confirmed combo set — otherwise the canary
# reports positive delta on all historical rows for combos that qualified
# once but no longer show in the trailing 7d snapshot. See
# docs/IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md §2 deploy-order.
# v3 (2026-07-28): one-time hammer to clear the +25 canary residue after
# fix 12f2c94 revived the dead cohort trigger and added the scanner
# sibling. The 26 disputed rows attributed almost entirely to the
# ip_day_hash arm — page_view_bot_hashes has no advance-trigger (yet;
# design law + implementation to follow). Bidirectional full-resync
# clears this instance; the bot_hashes-advance trigger closes the class.
# v4 (2026-09-02): added 6 UA patterns to BOT_UA_PATTERNS that were
# leaking through as is_bot=NULL and polluting human-only counts:
# %WhaleFlowRadar% (scraper hitting /cold-storage from ZA — put ZA at
# #1 in "human" traffic overnight), %Claude-User% (Anthropic on-demand
# fetch, sibling to already-listed ChatGPT-User), %HeadlessChrome%
# (standard automation signature), %Python-urllib% (stdlib script),
# %BuiltWith% (tech-detection scraper), %FACTANKER% (named
# robots-observatory tool). Additions-only — no existing TRUE rows can
# be un-matched. Full bidirectional resync via version bump is safe
# under this constraint.
# v5 (2026-09-02): two more UA patterns after midday analytics flagged
# both surviving in the "human" bucket:
# %xrpld-% (contains-match, ILIKE case-insensitive) — Charlie's own
#   internal automated verifiers (xrpld-l2-inspector, xrpld-anchor-canary,
#   any future xrpld-* named tool). Not readers.
# %Palo Alto Networks% — Cortex XDR security scanner UA, showed up
#   fresh at low volume. Not a browser.
# Same additions-only property as v4 → bidirectional full-resync safe.
BOT_CLASSIFIER_VERSION = 5

# ── Burst-cohort classifier ───────────────────────────────────────────────────
# Catches rotating-IP fleet attacks where each IP hits a *real* page exactly
# once with a stock browser UA — invisible to the session-key linker (no bot
# paths) and to the (path, ua)-volume classifier (real humans also visit those
# pages repeatedly, lifting the ratio above the ≤1.10 floor).
#
# Signal: a spike in unique IPs for a (country, path) pair far above that
# combination's 30-day baseline. The IL/whales burst (July 2026 founding
# example: ~2/day baseline → 774/day peak) is the reference case.
#
# Methodology disclosure: the rule classifies by COHORT (day + country + path),
# not by individual history. A genuine visitor from IL who visited /whales on a
# burst day is retrospectively classified as bot. We accept this false-positive
# at the current traffic scale; the review trigger (daily_human_traffic > 1,000)
# flags when IL/whales genuine single-visits become frequent enough to recheck
# the threshold. See /methodology#burst-cohort-classifier.
#
# Three-audience gate: this rule passes all three tests —
#   humans: only burst-day cohorts are affected; normal-day IL /whales hits are
#           unaffected (the cohort entry isn't inserted for normal days).
#   AI crawlers: they identify themselves in UA and match BOT_UA_PATTERNS first,
#                so they never reach the cohort predicate. The cohort predicate
#                can only match declared-browser UAs (Chrome, Safari, etc.),
#                which is structurally the fleet fingerprint.
#   scraper fleets: correctly reclassified.

_BURST_COHORT_SPIKE_MULTIPLE = 10   # >10× trailing-30d median fires
_BURST_COHORT_SPIKE_FLOOR = 50      # absolute minimum unique-IPs to trigger —
                                    # prevents tiny-baseline countries false-
                                    # firing (e.g. 1/day baseline → 11 = trigger)

# Module-level flag: True once burst_cohort_days table exists and the initial
# scan has run. _bot_filter_sql skips the cohort predicate until then so the
# filter can't error on a missing table at startup.
_burst_cohort_table_ready = False
_bot_hash_table_ready = False
# Set True by the is_bot writer after the backfill completes and the
# canary has soaked for 3 days. Flip is a separate deploy step; this
# stays False until that confirmation lands.
# FLIPPED 2026-07-31 after 3-day soak PASS (07-29/07-30/07-31 all delta=0,
# trailing-7d + historical-week windows). Analytics reads switch from the
# hash-tables path to the stamped is_bot column + partial index.
# Rollback: flip back to False, deploy — no schema change, minutes.
# Post-flip drift guard: is_bot_canary at 06:00 daily.
_is_bot_column_ready = True


def scan_burst_cohorts(lookback_days=90):
    """Scan page_views history for burst-cohort days and upsert into
    burst_cohort_days. Idempotent: re-running updates multiplier/baseline
    in place, safe to call daily.

    Algorithm:
    1. For each (country, path) pair seen in lookback_days, compute
       daily unique-IP counts (using visitor_hash as the IP proxy).
    2. For each candidate day, compute the trailing-30-day median of
       unique-IP counts for that (country, path) *excluding* the candidate.
    3. Flag days where unique_ips > max(MULTIPLE × median, FLOOR).
    4. Upsert into burst_cohort_days.

    Sets _burst_cohort_table_ready = True on success.

    Returns dict: {"inserted": N, "total_cohort_days": M, "reclassified_rows": R}
    """
    global _burst_cohort_table_ready
    conn = _get_writer_conn()
    if conn is None:
        return {"error": "no_writer_conn"}
    try:
        # Create table on first call.
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS burst_cohort_days (
                    country      TEXT        NOT NULL,
                    path         TEXT        NOT NULL,
                    burst_day    DATE        NOT NULL,
                    unique_ips   INTEGER     NOT NULL,
                    baseline_med NUMERIC,
                    multiplier   NUMERIC,
                    classified_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (country, path, burst_day)
                )
            """)
        _burst_cohort_table_ready = True

        cutoff_ts = int(time.time()) - lookback_days * 86400

        # Step 1+2+3: compute daily unique-IP counts per (country, path),
        # then for each (country, path, day) compute the trailing-30d median
        # of the *other* days in the window.  We do this in one pass via a
        # window function: median of all days in ±30d around the candidate
        # that are NOT the candidate day itself.
        with conn.cursor() as cur:
            cur.execute("""
                WITH daily_counts AS (
                    SELECT
                        country,
                        path,
                        date_trunc('day', to_timestamp(ts))::DATE AS day,
                        COUNT(DISTINCT visitor_hash)              AS unique_ips
                    FROM page_views
                    WHERE ts >= %s
                      AND country IS NOT NULL
                      AND path IS NOT NULL
                    GROUP BY 1, 2, 3
                ),
                with_baseline AS (
                    SELECT
                        d.country,
                        d.path,
                        d.day,
                        d.unique_ips,
                        PERCENTILE_CONT(0.5) WITHIN GROUP (
                            ORDER BY b.unique_ips
                        ) AS baseline_med
                    FROM daily_counts d
                    LEFT JOIN daily_counts b
                        ON  b.country = d.country
                        AND b.path    = d.path
                        AND b.day    != d.day
                        AND b.day BETWEEN d.day - INTERVAL '30 days'
                                      AND d.day + INTERVAL '30 days'
                    GROUP BY d.country, d.path, d.day, d.unique_ips
                )
                SELECT
                    country, path, day, unique_ips, baseline_med,
                    CASE WHEN baseline_med > 0
                         THEN ROUND(unique_ips::NUMERIC / baseline_med::NUMERIC, 1)
                         ELSE NULL END AS multiplier
                FROM with_baseline
                WHERE unique_ips >= %s
                  AND (
                      baseline_med IS NULL
                      OR unique_ips > baseline_med * %s
                  )
                  AND unique_ips >= %s
                ORDER BY multiplier DESC NULLS FIRST
            """, (
                cutoff_ts,
                _BURST_COHORT_SPIKE_FLOOR,
                _BURST_COHORT_SPIKE_MULTIPLE,
                _BURST_COHORT_SPIKE_FLOOR,
            ))
            burst_rows = cur.fetchall()

        if not burst_rows:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM burst_cohort_days")
                total = cur.fetchone()[0]
            return {"inserted": 0, "total_cohort_days": total, "reclassified_rows": 0}

        # Upsert cohort days.
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO burst_cohort_days
                    (country, path, burst_day, unique_ips, baseline_med, multiplier,
                     classified_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (country, path, burst_day) DO UPDATE SET
                    unique_ips    = EXCLUDED.unique_ips,
                    baseline_med  = EXCLUDED.baseline_med,
                    multiplier    = EXCLUDED.multiplier,
                    classified_at = EXCLUDED.classified_at
            """, [
                (r[0], r[1], r[2], r[3], r[4], r[5])
                for r in burst_rows
            ])
            inserted = cur.rowcount

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM burst_cohort_days")
            total = cur.fetchone()[0]

        # Count rows that will now be reclassified (for receipts).
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*)
                FROM page_views p
                WHERE p.country IS NOT NULL
                  AND (p.country, p.path,
                       date_trunc('day', to_timestamp(p.ts))::DATE)
                       IN (SELECT country, path, burst_day
                           FROM burst_cohort_days)
            """)
            reclassified = cur.fetchone()[0]

        return {
            "inserted": inserted,
            "total_cohort_days": total,
            "reclassified_rows": reclassified,
        }
    except Exception as e:
        _log_err("scan_burst_cohorts", e)
        return {"error": str(e)}


# ─── Layer 2 (Answer Plausibility) storage helpers ───────────────────────
# See migrations/2026_07_21_answer_plausibility.sql for the schema.
# The alarms table is append-only; watermarks are upserted per metric.

def write_plausibility_alarm(
    metric,
    rule,
    observed,
    expected_behavior,
    consecutive_cycles=None,
    last_change_at=None,
    note=None,
):
    """Append one alarm row. Named-failure format per TRUTH_AUDIT_DESIGN.md."""
    conn = _get_writer_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO answer_plausibility_alarms "
                "  (metric, rule, observed, expected_behavior, "
                "   consecutive_cycles, last_change_at, note) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (metric, rule, str(observed), expected_behavior,
                 consecutive_cycles, last_change_at, note),
            )
        return True
    except Exception as e:
        _log_err(f"write_plausibility_alarm[{metric}/{rule}]", e)
        _drop_writer_conn()
        return False


def read_plausibility_watermark(metric):
    """Return (last_value, last_seen_at, extra) or (None, None, None)."""
    if not pg_available():
        return (None, None, None)
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT last_value, last_seen_at, extra "
                "FROM answer_plausibility_watermarks WHERE metric=%s",
                (metric,),
            )
            row = cur.fetchone()
            if row is None:
                return (None, None, None)
            return (row[0], row[1], row[2])
    except Exception:
        return (None, None, None)


def write_plausibility_watermark(metric, value, extra=None):
    """Upsert the watermark for one metric."""
    import json
    conn = _get_writer_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO answer_plausibility_watermarks "
                "  (metric, last_value, last_seen_at, extra) "
                "VALUES (%s, %s, NOW(), %s) "
                "ON CONFLICT (metric) DO UPDATE SET "
                "  last_value = EXCLUDED.last_value, "
                "  last_seen_at = EXCLUDED.last_seen_at, "
                "  extra = EXCLUDED.extra",
                (metric, value,
                 json.dumps(extra) if extra is not None else None),
            )
        return True
    except Exception as e:
        _log_err(f"write_plausibility_watermark[{metric}]", e)
        _drop_writer_conn()
        return False


# ── Layer 3 (External Legitimacy) ────────────────────────────────────────────
# See migrations/2026_07_22_cross_check.sql for the schema.

def write_cross_check_result(
    pair_key, check_type, external_source,
    local_value=None, external_value=None,
    tolerance=None, delta=None, status="agree", note=None,
):
    """Append one cross-check result row. Append-only per design doc."""
    conn = _get_writer_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cross_check_results "
                "  (pair_key, check_type, local_value, external_value, "
                "   external_source, tolerance, delta, status, note) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (pair_key, check_type, local_value, external_value,
                 external_source, tolerance, delta, status, note),
            )
        return True
    except Exception as e:
        _log_err(f"write_cross_check_result[{pair_key}]", e)
        _drop_writer_conn()
        return False


def read_recent_cross_check_results(hours=48, status_filter=None):
    """Return rows from cross_check_results ordered newest first.

    When status_filter is provided (e.g. 'disagree'), only those rows are
    returned. Covers the Sunday queue audit opening act and /health surface.
    """
    import datetime
    if not pg_available():
        return []
    try:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(hours=hours))
        with pg_connect() as conn, conn.cursor() as cur:
            if status_filter:
                cur.execute(
                    "SELECT id, run_at, pair_key, check_type, local_value, "
                    "  external_value, external_source, tolerance, delta, "
                    "  status, note "
                    "FROM cross_check_results "
                    "WHERE status = %s AND run_at > %s "
                    "ORDER BY run_at DESC",
                    (status_filter, cutoff),
                )
            else:
                cur.execute(
                    "SELECT id, run_at, pair_key, check_type, local_value, "
                    "  external_value, external_source, tolerance, delta, "
                    "  status, note "
                    "FROM cross_check_results "
                    "WHERE run_at > %s "
                    "ORDER BY run_at DESC",
                    (cutoff,),
                )
            rows = cur.fetchall()
        cols = ("row_id", "run_at", "pair_key", "check_type", "local_value",
                "external_value", "external_source", "tolerance", "delta",
                "status", "note")
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        _log_err("read_recent_cross_check_results_failed", e)
        return []


def read_rlusd_supply_history(days=30):
    """Return list of dicts with the fields R1/R2 evaluate against, most-
    recent first. Bounded by ``days`` (most recent N calendar rows).
    Empty list if PG unreachable or table empty."""
    if not pg_available():
        return []
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT snapshot_date, xrpl_supply, eth_supply, "
                "       xrpl_net_change_24h "
                "FROM rlusd_supply_history "
                "ORDER BY snapshot_date DESC "
                "LIMIT %s",
                (int(days),),
            )
            rows = cur.fetchall()
        return [
            {
                "snapshot_date": r[0],
                "xrpl_supply": r[1],
                "eth_supply": r[2],
                "xrpl_net_change_24h": r[3],
            }
            for r in rows
        ]
    except Exception:
        return []


def count_burst_cohort_reclassified_rows():
    """Total page_view rows currently reclassified as bot by burst-cohort
    membership. Retained for callers that specifically want the
    burst-cohort surface (analytics detail views, docs). NOT the right
    denominator for R4's accepted-drop budget — see
    count_is_bot_true_page_views for that."""
    if not pg_available():
        return 0
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(unique_ips), 0) "
                "FROM burst_cohort_days"
            )
            row = cur.fetchone()
            return int(row[0] or 0)
    except Exception:
        return 0


def count_is_bot_true_page_views():
    """Total page_view rows currently marked is_bot=TRUE. This is the
    ground-truth surface the live classifier reads (page_view_stats
    counts humans as `is_bot IS NOT TRUE`), so its delta since the last
    R4 watermark is the correct accepted-drop budget: every classifier
    that promotes a row to bot — burst_cohort_days trigger,
    page_view_bot_hashes trigger, scanner_combos_confirmed trigger, or
    the mainline is_bot_writer — increments this count by one.

    Founding case 2026-07-30: R4 was scoped to burst_cohort_days only
    (5,636 rows) while the live is_bot=TRUE pool was 77,739 rows across
    four surfaces. Eight overnight false alarms every one carrying the
    note "burst-cohort delta since watermark = 0 (accepted drop
    budget)" while a legitimate reclassifier had promoted 297+ rows via
    a sibling surface R4 couldn't see."""
    if not pg_available():
        return 0
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM page_views WHERE is_bot=TRUE")
            row = cur.fetchone()
            return int(row[0] or 0)
    except Exception:
        return 0


def ensure_is_bot_schema():
    """Idempotent: add is_bot column + partial index + classification meta
    table if they don't exist. Safe to call on every writer run.
    CREATE INDEX CONCURRENTLY requires autocommit — handled separately.

    Also ensures page_view_scanner_combos_confirmed exists (see
    migrations/2026_07_26_scanner_combos_confirmed.sql). Same call-site
    ownership as the rest of the is_bot schema — one function, one place
    a future reader looks for classification-related DDL."""
    if not pg_available():
        return
    try:
        with rpc_loop_safe_pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE page_views "
                    "ADD COLUMN IF NOT EXISTS is_bot BOOLEAN DEFAULT NULL"
                )
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS page_view_classification_meta ("
                    "  key TEXT PRIMARY KEY,"
                    "  value TEXT NOT NULL,"
                    "  updated_at TIMESTAMPTZ DEFAULT NOW()"
                    ")"
                )
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS page_view_scanner_combos_confirmed ("
                    "  path                  TEXT        NOT NULL,"
                    "  user_agent            TEXT        NOT NULL,"
                    "  confirmed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
                    "  confirmed_by          TEXT        NOT NULL,"
                    "  evidence_ratio        NUMERIC,"
                    "  evidence_row_count    INTEGER,"
                    "  evidence_window_start BIGINT,"
                    "  evidence_window_end   BIGINT,"
                    "  last_seen_at          TIMESTAMPTZ,"
                    "  notes                 TEXT,"
                    "  PRIMARY KEY (path, user_agent),"
                    "  CHECK (confirmed_by IN ('auto', 'reviewed', 'manual'))"
                    ")"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "page_view_scanner_combos_confirmed_by_idx "
                    "ON page_view_scanner_combos_confirmed "
                    "(confirmed_by, confirmed_at DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "page_view_scanner_combos_confirmed_last_seen_idx "
                    "ON page_view_scanner_combos_confirmed "
                    "(last_seen_at DESC NULLS LAST)"
                )
        # CREATE INDEX CONCURRENTLY needs autocommit (outside transaction)
        dsn = pg_url()
        idx_conn = psycopg.connect(
            dsn,
            autocommit=True,
            connect_timeout=15,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
        try:
            with idx_conn.cursor() as cur:
                cur.execute(
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_page_views_human "
                    "ON page_views (ts) WHERE is_bot IS NOT TRUE"
                )
        finally:
            idx_conn.close()
    except Exception as e:
        _log_err("ensure_is_bot_schema_failed", e)


def get_classification_meta(keys=None):
    """Read page_view_classification_meta rows. Returns dict of key→value.
    If keys is provided, filters to those keys only."""
    if not pg_available():
        return {}
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                if keys:
                    ph = ",".join(["%s"] * len(keys))
                    cur.execute(
                        f"SELECT key, value FROM page_view_classification_meta "
                        f"WHERE key IN ({ph})",
                        list(keys),
                    )
                else:
                    cur.execute(
                        "SELECT key, value FROM page_view_classification_meta"
                    )
                return dict(cur.fetchall())
    except Exception:
        return {}


def set_classification_meta(updates):
    """Upsert key→value pairs into page_view_classification_meta.
    `updates` is a dict of {key: value} (all values stored as text)."""
    if not pg_available() or not updates:
        return
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                for key, value in updates.items():
                    cur.execute(
                        "INSERT INTO page_view_classification_meta (key, value) "
                        "VALUES (%s, %s) "
                        "ON CONFLICT (key) DO UPDATE SET "
                        "  value = EXCLUDED.value, updated_at = NOW()",
                        (key, str(value)),
                    )
            conn.commit()
    except Exception as e:
        _log_err("set_classification_meta_failed", e)


def refresh_bot_hash_tables():
    """Materialise bot classification data into two small Postgres tables so
    every analytics() render can filter against an indexed join instead of
    binding ~11k literal params or scanning page_views twice per query.

    Table 1 — page_view_bot_hashes (hash_type TEXT, hash TEXT PK):
      Stores visitor_hash and ip_day_hash values of rows that match
      BOT_PATH_PATTERNS / BOT_UA_PATTERNS (row_pred). Mirrors exactly what
      the legacy session_pred IN-subqueries select against, so classification
      is identical to the legacy path.

    Table 2 — page_view_scanner_combos (path TEXT, user_agent TEXT PK):
      Stores (path, user_agent) pairs that meet the scanner-fleet criteria
      (≥30 hits, hits ≈ visitors in the last 7d). Replaces the per-query
      GROUP BY / HAVING subquery with a single indexed lookup.

    Both tables are truncated + reinserted on each call (full refresh).
    Creates the tables on first run — no separate migration needed.

    Sets _bot_hash_table_ready = True on success so _bot_filter_sql can
    switch to the table path. Safe to call from a background thread.
    """
    global _bot_hash_table_ready
    if not pg_available():
        return
    path_likes = " OR ".join("path LIKE %s" for _ in BOT_PATH_PATTERNS)
    ua_likes = " OR ".join(
        "COALESCE(user_agent, '') ILIKE %s" for _ in BOT_UA_PATTERNS
    )
    row_pred = f"(({path_likes}) OR ({ua_likes}))"
    row_params = list(BOT_PATH_PATTERNS) + list(BOT_UA_PATTERNS)
    scanner_ts = int(time.time()) - 7 * 86400
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                # Create tables idempotently
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS page_view_bot_hashes ("
                    "  hash_type TEXT NOT NULL,"
                    "  hash TEXT NOT NULL,"
                    "  PRIMARY KEY (hash_type, hash)"
                    ")"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pvbh_lookup "
                    "ON page_view_bot_hashes (hash_type, hash)"
                )
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS page_view_scanner_combos ("
                    "  path TEXT NOT NULL,"
                    "  user_agent TEXT NOT NULL,"
                    "  PRIMARY KEY (path, user_agent)"
                    ")"
                )

                # Visitor hashes from row_pred
                cur.execute(
                    f"SELECT DISTINCT visitor_hash FROM page_views "
                    f"WHERE visitor_hash IS NOT NULL AND {row_pred}",
                    row_params,
                )
                visitor_hashes = [r[0] for r in cur.fetchall()]

                # IP-day hashes from row_pred
                cur.execute(
                    f"SELECT DISTINCT ip_day_hash FROM page_views "
                    f"WHERE ip_day_hash IS NOT NULL AND {row_pred}",
                    row_params,
                )
                ip_day_hashes = [r[0] for r in cur.fetchall()]

                # Scanner (path, ua) combos — capture hits + distinct_visitors
                # so we can auto-ratchet into page_view_scanner_combos_confirmed
                # with evidence fields (see block below).
                cur.execute(
                    "SELECT path, user_agent, COUNT(*)::int AS hits, "
                    "       COUNT(DISTINCT visitor_hash)::int AS dv "
                    "  FROM page_views "
                    " WHERE user_agent IS NOT NULL AND ts > %s "
                    " GROUP BY path, user_agent "
                    "HAVING COUNT(*) >= 30 "
                    "   AND COUNT(*) <= COUNT(DISTINCT visitor_hash) * 1.10",
                    [scanner_ts],
                )
                scanner_combos_full = cur.fetchall()
                scanner_combos = [(p, u) for p, u, h, d in scanner_combos_full]

                # Atomic refresh: truncate + reinsert in one transaction.
                # Flattened single-statement INSERT keeps round-trips to one.
                cur.execute("TRUNCATE page_view_bot_hashes")
                all_hash_rows = (
                    [("visitor", h) for h in visitor_hashes]
                    + [("ip_day", h) for h in ip_day_hashes]
                )
                if all_hash_rows:
                    ph = ",".join(["(%s,%s)"] * len(all_hash_rows))
                    flat = [v for row in all_hash_rows for v in row]
                    cur.execute(
                        f"INSERT INTO page_view_bot_hashes (hash_type, hash) "
                        f"VALUES {ph}",
                        flat,
                    )

                cur.execute("TRUNCATE page_view_scanner_combos")
                if scanner_combos:
                    ph = ",".join(["(%s,%s)"] * len(scanner_combos))
                    flat = [v for row in scanner_combos for v in row]
                    cur.execute(
                        f"INSERT INTO page_view_scanner_combos (path, user_agent) "
                        f"VALUES {ph}",
                        flat,
                    )

                # Auto-ratchet into page_view_scanner_combos_confirmed.
                # Detection is transient; conviction is permanent. Any combo
                # that meets the scanner rule gets a confirmed_by='auto' row
                # on first detection; original evidence is preserved on
                # conflict (only last_seen_at advances). Reversal is a
                # governance decision, not automatic. See
                # docs/IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md §4c.
                if scanner_combos_full:
                    window_end_ts = int(time.time())
                    window_start_ts = scanner_ts
                    ratchet_rows = [
                        (
                            p, u,
                            round(hits / max(dv, 1), 4),  # evidence_ratio
                            hits,                          # evidence_row_count
                            window_start_ts,
                            window_end_ts,
                        )
                        for p, u, hits, dv in scanner_combos_full
                    ]
                    r_ph = ",".join(
                        ["(%s,%s,'auto',%s,%s,%s,%s,NOW())"] * len(ratchet_rows)
                    )
                    r_flat = [v for row in ratchet_rows for v in row]
                    cur.execute(
                        f"INSERT INTO page_view_scanner_combos_confirmed "
                        f"(path, user_agent, confirmed_by, evidence_ratio, "
                        f" evidence_row_count, evidence_window_start, "
                        f" evidence_window_end, last_seen_at) "
                        f"VALUES {r_ph} "
                        f"ON CONFLICT (path, user_agent) DO UPDATE "
                        f"  SET last_seen_at = NOW()",
                        r_flat,
                    )

            conn.commit()
        _bot_hash_table_ready = True
    except Exception as e:
        _log_err("refresh_bot_hash_tables_failed", e)


def compute_bot_hash_sets(conn):
    """Materialize the visitor_hash and ip_day_hash sets that _bot_filter_sql's
    session_pred IN-subqueries would otherwise re-evaluate on every call.
    Two queries over page_views instead of the 44 a full analytics render fires
    (22 downstream reads × 2 IN-subqueries each).

    Uses row_pred only (path + UA patterns) — mirroring exactly what the legacy
    session_pred subqueries select against. scanner_pred and cohort_pred stay as
    per-query predicates in the fast path: scanner uses an IN-materialized
    subquery Postgres hashes once per query (fast, and needs a fresh ts
    threshold each render); cohort is a tiny table.

    Returns (visitor_hashes: frozenset, ip_day_hashes: frozenset).
    """
    path_likes = " OR ".join("path LIKE %s" for _ in BOT_PATH_PATTERNS)
    ua_likes = " OR ".join(
        "COALESCE(user_agent, '') ILIKE %s" for _ in BOT_UA_PATTERNS
    )
    row_pred = f"(({path_likes}) OR ({ua_likes}))"
    params = list(BOT_PATH_PATTERNS) + list(BOT_UA_PATTERNS)
    vh, ih = set(), set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT visitor_hash FROM page_views "
                f"WHERE visitor_hash IS NOT NULL AND {row_pred}",
                params,
            )
            for (v,) in cur.fetchall():
                vh.add(v)
            cur.execute(
                f"SELECT DISTINCT ip_day_hash FROM page_views "
                f"WHERE ip_day_hash IS NOT NULL AND {row_pred}",
                params,
            )
            for (i,) in cur.fetchall():
                ih.add(i)
    except Exception:
        pass
    return frozenset(vh), frozenset(ih)


def _bot_filter_sql(kind, precomputed=None):
    """Builds a WHERE-clause fragment that selects human / bot / all rows.
    Returns (fragment, params). Fragment starts with `AND ` so it can be
    appended to an existing WHERE. `kind` is "human", "bot", or "all".

    `precomputed`: optional (visitor_hashes, ip_day_hashes) frozensets from
    compute_bot_hash_sets(). When passed, the two IN-subqueries collapse
    to literal IN lists — a ~3.5× per-query speedup at prod Neon on ~100k
    page_views (measured 2026-07-23: 2.4s subquery form → 0.7s literal form).

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
    if _is_bot_column_ready:
        # Fastest path: is_bot column + partial index on page_views.
        # Each query is a simple index scan — no filter evaluation per row.
        # The column is maintained by the is_bot writer and canary-verified.
        if kind == "bot":
            return "AND is_bot = TRUE", []
        return "AND is_bot IS NOT TRUE", []
    if _bot_hash_table_ready:
        # Fastest path: all session/scanner state lives in small Postgres
        # tables refreshed every warmer cycle (~30s). Postgres materialises
        # each as a hash-set — O(1) per page_views row, no param binding.
        # No Python-side params needed beyond row_pred + cohort_pred.
        parts = [row_pred]
        params = list(BOT_PATH_PATTERNS) + list(BOT_UA_PATTERNS)
        parts.append(
            "(visitor_hash IS NOT NULL AND visitor_hash IN ("
            "  SELECT hash FROM page_view_bot_hashes"
            "  WHERE hash_type = 'visitor'"
            "))"
        )
        parts.append(
            "(ip_day_hash IS NOT NULL AND ip_day_hash IN ("
            "  SELECT hash FROM page_view_bot_hashes"
            "  WHERE hash_type = 'ip_day'"
            "))"
        )
        parts.append(
            "(user_agent IS NOT NULL AND (path, user_agent) IN ("
            "  SELECT path, user_agent FROM page_view_scanner_combos"
            "))"
        )
        if _burst_cohort_table_ready:
            parts.append(
                "(country IS NOT NULL "
                " AND (country, path, date_trunc('day', to_timestamp(ts))::DATE)"
                "     IN (SELECT country, path, burst_day FROM burst_cohort_days))"
            )
        full_pred = "(" + " OR ".join(parts) + ")"
        if kind == "bot":
            return f"AND {full_pred}", params
        return f"AND NOT {full_pred}", params
    if precomputed is not None:
        # Mid-tier: session_pred subqueries replaced with literal IN lists
        # from compute_bot_hash_sets(). scanner_pred stays per-query.
        # Active during the window between worker start and first warmer cycle.
        visitor_hashes, ip_day_hashes = precomputed
        parts = [row_pred]
        params = list(BOT_PATH_PATTERNS) + list(BOT_UA_PATTERNS)
        if visitor_hashes:
            placeholders = ",".join(["%s"] * len(visitor_hashes))
            parts.append(
                f"(visitor_hash IS NOT NULL AND visitor_hash IN ({placeholders}))"
            )
            params.extend(visitor_hashes)
        if ip_day_hashes:
            placeholders = ",".join(["%s"] * len(ip_day_hashes))
            parts.append(
                f"(ip_day_hash IS NOT NULL AND ip_day_hash IN ({placeholders}))"
            )
            params.extend(ip_day_hashes)
        parts.append(
            "(user_agent IS NOT NULL AND (path, user_agent) IN ("
            "  SELECT path, user_agent FROM page_views "
            "  WHERE user_agent IS NOT NULL AND ts > %s "
            "  GROUP BY path, user_agent "
            "  HAVING COUNT(*) >= 30 "
            "     AND COUNT(*) <= COUNT(DISTINCT visitor_hash) * 1.10"
            "))"
        )
        params.append(int(time.time()) - 7 * 86400)
        if _burst_cohort_table_ready:
            parts.append(
                "(country IS NOT NULL "
                " AND (country, path, date_trunc('day', to_timestamp(ts))::DATE)"
                "     IN (SELECT country, path, burst_day FROM burst_cohort_days))"
            )
        full_pred = "(" + " OR ".join(parts) + ")"
        if kind == "bot":
            return f"AND {full_pred}", params
        return f"AND NOT {full_pred}", params
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
    # Burst-cohort predicate: classify all rows whose (country, path, day)
    # appears in burst_cohort_days as bot. The table is tiny (≤50 rows
    # typical), so Postgres materialises it as a hash-set — no seq-scan
    # cost on page_views beyond what's already happening. Only active when
    # _burst_cohort_table_ready is True (set after the first successful scan
    # so the filter can't error at startup before the table exists).
    if _burst_cohort_table_ready:
        cohort_pred = (
            "(country IS NOT NULL "
            " AND (country, path, date_trunc('day', to_timestamp(ts))::DATE)"
            "     IN (SELECT country, path, burst_day FROM burst_cohort_days))"
        )
        full_pred = f"({row_pred} OR {session_pred} OR {scanner_pred} OR {cohort_pred})"
    else:
        full_pred = f"({row_pred} OR {session_pred} OR {scanner_pred})"
    # Params: once for row_pred, once for each of the two session subqueries,
    # plus the scanner_pred ts threshold (7d ago). cohort_pred has no params
    # (the subquery is parameter-free; the table holds the values).
    params = list(BOT_PATH_PATTERNS) + list(BOT_UA_PATTERNS)
    params += list(BOT_PATH_PATTERNS) + list(BOT_UA_PATTERNS)
    params += list(BOT_PATH_PATTERNS) + list(BOT_UA_PATTERNS)
    params.append(int(time.time()) - 7 * 86400)
    if kind == "bot":
        return f"AND {full_pred}", params
    return f"AND NOT {full_pred}", params


def _bot_filter_sql_lite(kind):
    """Row-level-only bot filter — path LIKE + UA ILIKE, NO session-key
    subqueries and NO scanner-detection subquery. Used by the /analytics/live
    delta endpoint where we need sub-100ms latency and can accept a slight
    undercount (a scanner whose row-level heuristics don't fire may register
    as human for 5-min purposes until it's classified retrospectively).

    The full filter's IN-subqueries scan the whole page_views table per
    query (~100k rows). At 15s polling cadence per open tab, that turns
    /analytics/live into the same DB-hammering shape we're solving here.
    The lite filter costs microseconds against the row set already narrowed
    by the ts-DESC index.

    Same interface as _bot_filter_sql: returns (fragment, params). Fragment
    starts with `AND ` so it appends to an existing WHERE."""
    if kind == "all":
        return "", []
    path_likes = " OR ".join("path LIKE %s" for _ in BOT_PATH_PATTERNS)
    ua_likes = " OR ".join(
        "COALESCE(user_agent, '') ILIKE %s" for _ in BOT_UA_PATTERNS
    )
    row_pred = f"(({path_likes}) OR ({ua_likes}))"
    params = list(BOT_PATH_PATTERNS) + list(BOT_UA_PATTERNS)
    if kind == "bot":
        return f"AND {row_pred}", params
    return f"AND NOT {row_pred}", params


def log_page_view(path, visitor_hash=None, referrer=None,
                  user_agent=None, country=None, utm_source=None,
                  ip_day_hash=None, region_code=None):
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
                " utm_source, ip_day_hash, region_code) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (int(time.time()), path, visitor_hash,
                 referrer, user_agent, country, utm_source, ip_day_hash,
                 region_code),
            )
    except Exception as e:
        _log_err("log_page_view_failed", e)
        _drop_writer_conn()


def read_page_view_stats(kind="human", precomputed_bots=None):
    """Return rollup counts at common windows for /admin/stats. Each value
    is a dict with `views` (raw row count) and `uniques` (distinct
    visitor_hash). `kind` is "human" (default), "bot", or "all". Returns
    zeros on error so the page renders even if PG hiccups.

    `precomputed_bots`: pass the tuple from compute_bot_hash_sets() to
    skip the two IN-subqueries in _bot_filter_sql — ~3.5× per-query
    speedup on prod Neon. See _bot_filter_sql docstring."""
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
    bot_frag, bot_params = _bot_filter_sql(kind, precomputed=precomputed_bots)
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


def read_page_view_stats_live(kind="human"):
    """Lite variant for /analytics/live — only the `now` (5-min) and `hour`
    windows, using _bot_filter_sql_lite so each query costs microseconds
    against the ts-index-narrowed row set. Sub-100ms even at 240 polls/hr.

    Returns the same shape as read_page_view_stats' `now` + `hour` slots:
    {"now": {"views": N, "uniques": M}, "hour": {"views": N, "uniques": M}}.
    """
    out = {
        "now":  {"views": 0, "uniques": 0},
        "hour": {"views": 0, "uniques": 0},
    }
    if not pg_available():
        return out
    now_ts = int(time.time())
    windows = {"now": 5 * 60, "hour": 60 * 60}
    bot_frag, bot_params = _bot_filter_sql_lite(kind)
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                for key, sec in windows.items():
                    cur.execute(
                        "SELECT COUNT(*), COUNT(DISTINCT visitor_hash) "
                        f"FROM page_views WHERE ts >= %s {bot_frag}",
                        [now_ts - sec, *bot_params],
                    )
                    v, u = cur.fetchone() or (0, 0)
                    out[key] = {"views": int(v or 0), "uniques": int(u or 0)}
    except Exception:
        pass
    return out


def read_top_pages(window_seconds, limit=10, kind="human",
                   precomputed_bots=None):
    """Top paths by view count over the trailing `window_seconds`.
    `kind` is "human" (default), "bot", or "all". Returns list of
    (path, views, uniques). `precomputed_bots` — see read_page_view_stats."""
    if not pg_available():
        return []
    cutoff = int(time.time()) - int(window_seconds)
    bot_frag, bot_params = _bot_filter_sql(kind, precomputed=precomputed_bots)
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


def read_country_breakdown(window_seconds, limit=10, kind="human",
                           precomputed_bots=None):
    """Top countries by view count over the trailing window. `kind` is
    "human" (default), "bot", or "all". Pass `window_seconds=None` for
    no time filter (all-time). Country may be None when CF-IPCountry
    wasn't present (e.g. local dev, or non-Cloudflare front).
    Returns list of (country, views, uniques).
    `precomputed_bots` — see read_page_view_stats."""
    if not pg_available():
        return []
    bot_frag, bot_params = _bot_filter_sql(kind, precomputed=precomputed_bots)
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


def read_country_count(window_seconds, kind="human", precomputed_bots=None):
    """Count of distinct origins (countries + Cloudflare special codes
    like T1 for Tor) seen in the trailing window. Mirrors
    read_country_breakdown's bot-filter + window semantics so the count
    lines up with the table. Pass window_seconds=None for all-time.
    `precomputed_bots` — see read_page_view_stats."""
    if not pg_available():
        return 0
    bot_frag, bot_params = _bot_filter_sql(kind, precomputed=precomputed_bots)
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


def log_contact_bot_drop(ua, signature):
    """Record a silently-dropped /contact submission in contact_bot_drops.
    Best-effort — never raises, never blocks the fake-200 response path.
    No payload stored; only ts, UA, and which filter signature fired."""
    if not pg_available():
        return
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO contact_bot_drops (ts, ua, signature) "
                    "VALUES (%s, %s, %s)",
                    (int(time.time()), (ua or "")[:300], signature),
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


# ─────────────────────────────────────────────────────────────────────
# API v1 — key issuance, lookup, rate-limit counter
# Anchors: project_xrpldashboard_api_v1_anchors.md
# ─────────────────────────────────────────────────────────────────────

def insert_api_key(email, key_hash, key_prefix, tier="free"):
    """Insert a new API key row. Returns the new row's id, or None on failure."""
    if not pg_available():
        return None
    try:
        conn = _get_writer_conn()
        if conn is None:
            return None
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO api_keys
                     (created_at, email, key_hash, key_prefix, tier, status)
                   VALUES (%s, %s, %s, %s, %s, 'active')
                   RETURNING id""",
                (int(time.time()), email.lower(), key_hash, key_prefix, tier),
            )
            return int(cur.fetchone()[0])
    except Exception as e:
        _log_err("insert_api_key_failed", e)
        _drop_writer_conn()
        return None


def read_api_key_by_hash(key_hash):
    """Look up an API key by its SHA-256 hash. Returns dict (id, email,
    key_prefix, tier, status) or None. This is the hot-path auth call —
    called on every /api/v1/* request."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, email, key_prefix, tier, status
                       FROM api_keys WHERE key_hash = %s""",
                    (key_hash,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "id": int(row[0]),
                    "email": row[1],
                    "key_prefix": row[2],
                    "tier": row[3],
                    "status": row[4],
                }
    except Exception as e:
        _log_err("read_api_key_by_hash_failed", e)
        return None


def read_active_api_key_for_email(email):
    """Return the most recent active API key row for an email, or None."""
    if not pg_available():
        return None
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, email, key_prefix, tier, status, created_at,
                              last_used_at
                       FROM api_keys
                       WHERE email = %s AND status = 'active'
                       ORDER BY created_at DESC
                       LIMIT 1""",
                    (email.lower(),),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "id": int(row[0]),
                    "email": row[1],
                    "key_prefix": row[2],
                    "tier": row[3],
                    "status": row[4],
                    "created_at": int(row[5]) if row[5] else None,
                    "last_used_at": int(row[6]) if row[6] else None,
                }
    except Exception as e:
        _log_err("read_active_api_key_for_email_failed", e)
        return None


def revoke_api_keys_for_email(email):
    """Mark all active keys for an email as revoked. Returns count revoked."""
    if not pg_available():
        return 0
    try:
        conn = _get_writer_conn()
        if conn is None:
            return 0
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE api_keys
                   SET status = 'revoked', revoked_at = %s
                   WHERE email = %s AND status = 'active'""",
                (int(time.time()), email.lower()),
            )
            return cur.rowcount
    except Exception as e:
        _log_err("revoke_api_keys_for_email_failed", e)
        _drop_writer_conn()
        return 0


def touch_api_key_last_used(key_id):
    """Best-effort update of last_used_at. Silently no-ops on failure —
    we don't want a stale-writer error to 500 a successful API request."""
    if not pg_available():
        return
    try:
        conn = _get_writer_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET last_used_at = %s WHERE id = %s",
                (int(time.time()), key_id),
            )
    except Exception as e:
        _log_err("touch_api_key_last_used_failed", e)
        _drop_writer_conn()


def increment_api_request_counter(key_id):
    """Atomically increment the (key_id, current_hour_bucket) counter and
    return the new count. On PG failure returns None so the caller can
    fail-open with a warning header (better than 500ing everyone if Neon
    hiccups). The counter is authoritative across Gunicorn workers."""
    if not pg_available():
        return None
    now = int(time.time())
    bucket = now // 3600
    try:
        conn = _get_writer_conn()
        if conn is None:
            return None
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO api_request_counters
                     (key_id, hour_bucket, request_count)
                   VALUES (%s, %s, 1)
                   ON CONFLICT (key_id, hour_bucket)
                   DO UPDATE SET request_count =
                     api_request_counters.request_count + 1
                   RETURNING request_count""",
                (key_id, bucket),
            )
            return int(cur.fetchone()[0])
    except Exception as e:
        _log_err("increment_api_request_counter_failed", e)
        _drop_writer_conn()
        return None


# ─────────────────────────────────────────────────────────────────────────
# cold_storage_snapshot + escrow_supply_snapshot
#
# Wired 2026-09-03. Mac-side walkers (cold_storage_walker.py +
# escrow_supply_walker.py) upsert here every 15 min via LAN rippled;
# /cold-storage route on Render reads from these tables instead of
# making 21 live account_info + 19 account_objects RPC calls per render.
# Kills ~214/hr + ~52/hr walker_node_fallback churn to public XRPL.
# Staleness handled by the route (SOURCING_STALE_CACHE banner if oldest
# fetched_at > threshold).
# ─────────────────────────────────────────────────────────────────────────

def replace_cold_storage_snapshot(rows):
    """Replace-on-write full refresh of cold_storage_snapshot. `rows` is
    a list of dicts with keys: address (PK), balance_xrp (float), sequence
    (int|None), owner_count (int|None), ledger_index (int), fetch_ok (bool).

    Wraps in one transaction so a partial failure leaves the previous
    snapshot intact. Returns True on success, False on any DB error —
    walker treats False as soft-fail and walker_health_end records it.
    """
    if not pg_available():
        return False
    if not rows:
        return False
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cold_storage_snapshot")
                for r in rows:
                    cur.execute(
                        "INSERT INTO cold_storage_snapshot "
                        "  (address, balance_xrp, sequence, owner_count, "
                        "   ledger_index, fetched_at, fetch_ok) "
                        "VALUES (%s, %s, %s, %s, %s, NOW(), %s)",
                        (
                            r["address"],
                            r["balance_xrp"],
                            r.get("sequence"),
                            r.get("owner_count"),
                            r["ledger_index"],
                            bool(r.get("fetch_ok", True)),
                        ),
                    )
            conn.commit()
        return True
    except Exception as e:
        _log_err("replace_cold_storage_snapshot_failed", e)
        return False


def read_cold_storage_snapshot():
    """Read all cold_storage_snapshot rows. Returns dict:
        {"rows": [...], "fetched_at": datetime|None, "age_seconds": int|None}
    Empty rows + None on PG unavailable / table empty — caller renders
    "no data yet" state + SOURCING_STALE_CACHE (or similar) banner.
    age_seconds is computed from the OLDEST fetched_at across rows
    (worst-case freshness for the batch)."""
    if not pg_available():
        return {"rows": [], "fetched_at": None, "age_seconds": None}
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT address, balance_xrp, sequence, owner_count, "
                    "       ledger_index, fetched_at, fetch_ok "
                    "  FROM cold_storage_snapshot "
                    " ORDER BY balance_xrp DESC NULLS LAST"
                )
                cols = [d.name for d in cur.description]
                raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        _log_err("read_cold_storage_snapshot_failed", e)
        return {"rows": [], "fetched_at": None, "age_seconds": None}
    if not raw:
        return {"rows": [], "fetched_at": None, "age_seconds": None}
    oldest = min(r["fetched_at"] for r in raw)
    age = (datetime.datetime.now(datetime.timezone.utc) - oldest).total_seconds()
    return {"rows": raw, "fetched_at": oldest, "age_seconds": int(age)}


def upsert_escrow_supply_snapshot(total_xrp, object_count, accounts_scanned,
                                  accounts_total, ledger_index):
    """Upsert the singleton escrow_supply_snapshot row (id=1). Returns
    True on success, False on DB error."""
    if not pg_available():
        return False
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO escrow_supply_snapshot "
                    "  (id, total_xrp, object_count, accounts_scanned, "
                    "   accounts_total, ledger_index, fetched_at) "
                    "VALUES (1, %s, %s, %s, %s, %s, NOW()) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  total_xrp = EXCLUDED.total_xrp, "
                    "  object_count = EXCLUDED.object_count, "
                    "  accounts_scanned = EXCLUDED.accounts_scanned, "
                    "  accounts_total = EXCLUDED.accounts_total, "
                    "  ledger_index = EXCLUDED.ledger_index, "
                    "  fetched_at = EXCLUDED.fetched_at",
                    (total_xrp, object_count, accounts_scanned,
                     accounts_total, ledger_index),
                )
            conn.commit()
        return True
    except Exception as e:
        _log_err("upsert_escrow_supply_snapshot_failed", e)
        return False


def read_escrow_supply_snapshot():
    """Read singleton escrow_supply_snapshot row. Returns dict:
        {"total_xrp": ..., "object_count": ..., "accounts_scanned": ...,
         "accounts_total": ..., "ledger_index": ..., "fetched_at": ...,
         "age_seconds": int}
    or all-None dict when unavailable/empty."""
    empty = {"total_xrp": None, "object_count": None, "accounts_scanned": None,
             "accounts_total": None, "ledger_index": None, "fetched_at": None,
             "age_seconds": None}
    if not pg_available():
        return empty
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT total_xrp, object_count, accounts_scanned, "
                    "       accounts_total, ledger_index, fetched_at "
                    "  FROM escrow_supply_snapshot WHERE id = 1"
                )
                row = cur.fetchone()
    except Exception as e:
        _log_err("read_escrow_supply_snapshot_failed", e)
        return empty
    if not row:
        return empty
    fetched_at = row[5]
    age = int((datetime.datetime.now(datetime.timezone.utc) - fetched_at).total_seconds())
    return {
        "total_xrp": float(row[0]),
        "object_count": int(row[1]),
        "accounts_scanned": int(row[2]),
        "accounts_total": int(row[3]),
        "ledger_index": int(row[4]),
        "fetched_at": fetched_at,
        "age_seconds": age,
    }
