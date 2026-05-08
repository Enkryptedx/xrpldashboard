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
        _writer_conn = psycopg.connect(pg_url(), autocommit=True)
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
        _drop_writer_conn()


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
    except Exception:
        _drop_writer_conn()


# ─────────────────────────────────────────────────────────────────────
# Flask read helpers (request-scoped connections)
# ─────────────────────────────────────────────────────────────────────

def read_whale_events(tier_drops, filter_type=None, limit=100):
    """Return rows in the same column order as the SQLite query in
    app.whales: tx_hash, ledger_index, ts, type, from_addr, to_addr,
    amount_drops, currency, issuer, raw_json. raw_json is returned as a
    string so the existing _resolve_event() resolver works unchanged."""
    clauses = ["(type != 'large_xfer' OR amount_drops >= %s)"]
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
    '_total': N} for the /whales stat tiles."""
    counts = {"large_xfer": 0, "tagged": 0, "trustset": 0, "_total": 0}
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT type, COUNT(*) FROM events "
                "WHERE (type != 'large_xfer' OR amount_drops >= %s) "
                "GROUP BY type",
                (tier_drops,),
            )
            for type_, n in cur.fetchall():
                if type_ in counts:
                    counts[type_] = n
                counts["_total"] += n
    return counts


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
