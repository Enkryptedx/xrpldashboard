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


def _log_err(category, exc):
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
    id          BIGSERIAL PRIMARY KEY,
    amm_account TEXT,
    pair        TEXT NOT NULL,
    fee_pct     DOUBLE PRECISION,
    fee_raw     INTEGER,
    amount_a    DOUBLE PRECISION,
    amount_b    DOUBLE PRECISION,
    asset_a     JSONB,
    asset_b     JSONB,
    tvl_usd     DOUBLE PRECISION,
    tvl_status  TEXT,
    kind        TEXT,
    snapshot_ts BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS amm_ranked_pools_tvl_idx
    ON amm_ranked_pools (tvl_usd DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS page_views (
    id            BIGSERIAL PRIMARY KEY,
    ts            BIGINT NOT NULL,
    path          TEXT NOT NULL,
    visitor_hash  TEXT,
    referrer      TEXT,
    user_agent    TEXT,
    country       TEXT
);
CREATE INDEX IF NOT EXISTS page_views_ts_idx ON page_views (ts DESC);
CREATE INDEX IF NOT EXISTS page_views_path_ts_idx
    ON page_views (path, ts DESC);
CREATE INDEX IF NOT EXISTS page_views_visitor_idx
    ON page_views (visitor_hash, ts DESC);

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


def upsert_token_volume(currency, issuer, hour_bucket, trade_delta=1):
    """Increment trade_count for a (currency, issuer, hour_bucket) bucket.
    Mirrors the SQLite ON CONFLICT … DO UPDATE pattern in
    xrpl_stream.token_event_handler. Silent no-op when PG isn't
    configured."""
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO token_volume "
                "(currency, issuer, hour_bucket, volume_xrp, trade_count) "
                "VALUES (%s, %s, %s, 0.0, %s) "
                "ON CONFLICT (currency, issuer, hour_bucket) DO UPDATE "
                "SET trade_count = token_volume.trade_count + EXCLUDED.trade_count",
                (currency, issuer, hour_bucket, trade_delta),
            )
    except Exception as e:
        _log_err("upsert_token_volume_failed", e)
        _drop_writer_conn()


def write_amm_pool_event(ts, amm_account, event_type):
    """Append a row to the amm_pool_events ring buffer in Postgres.
    Silent no-op when PG isn't configured."""
    conn = _get_writer_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO amm_pool_events (ts, amm_account, event_type) "
                "VALUES (%s, %s, %s)",
                (ts, amm_account, event_type),
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
                    " asset_a, asset_b, tvl_usd, tvl_status, kind, snapshot_ts) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, "
                    " %s, %s, %s, %s)",
                    payload,
                )
    except Exception as e:
        _log_err("replace_amm_ranked_pools_failed", e)
        _drop_writer_conn()


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
    Returns list of dicts (id, ts, amm_account, event_type) ordered by id."""
    cutoff = int(time.time()) - seconds
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, ts, amm_account, event_type "
                "FROM amm_pool_events WHERE ts >= %s "
                "ORDER BY id ASC LIMIT 200",
                (cutoff,),
            )
            return [
                {"id": r[0], "ts": r[1], "amm_account": r[2], "event_type": r[3]}
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
    "/.env%",
    "/wp-login.php",
    "/wp-admin%",
    "/wp-includes%",
    "/wp-content%",
    "/wordpress%",
    "%.php",
    "%.php?%",
    "/phpmyadmin%",
    "/.git%",
    "/.aws%",
    "/.ssh%",
    "/cgi-bin%",
    "/admin.php",
    "/config.json",
    "/backup%",
    "/dump.sql",
)


def _bot_filter_sql(kind):
    """Builds a WHERE-clause fragment that selects human / bot / all rows.
    Returns (fragment, params). Fragment starts with `AND ` so it can be
    appended to an existing WHERE. `kind` is "human", "bot", or "all"."""
    if kind == "all":
        return "", []
    likes = " OR ".join("path LIKE %s" for _ in BOT_PATH_PATTERNS)
    if kind == "bot":
        return f"AND ({likes})", list(BOT_PATH_PATTERNS)
    return f"AND NOT ({likes})", list(BOT_PATH_PATTERNS)


def log_page_view(path, visitor_hash=None, referrer=None,
                  user_agent=None, country=None):
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
                "(ts, path, visitor_hash, referrer, user_agent, country) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (int(time.time()), path, visitor_hash,
                 referrer, user_agent, country),
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


def read_country_breakdown(window_seconds, limit=10, kind="human"):
    """Top countries by view count over the trailing window. `kind` is
    "human" (default), "bot", or "all". Country may be None when
    CF-IPCountry wasn't present (e.g. local dev, or non-Cloudflare front).
    Returns list of (country, views, uniques)."""
    if not pg_available():
        return []
    cutoff = int(time.time()) - int(window_seconds)
    bot_frag, bot_params = _bot_filter_sql(kind)
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(country, '?') AS c, "
                    "       COUNT(*) AS views, "
                    "       COUNT(DISTINCT visitor_hash) AS uniques "
                    f"FROM page_views WHERE ts >= %s {bot_frag} "
                    "GROUP BY c ORDER BY views DESC LIMIT %s",
                    [cutoff, *bot_params, limit],
                )
                return [(r[0], int(r[1]), int(r[2])) for r in cur.fetchall()]
    except Exception:
        return []


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
