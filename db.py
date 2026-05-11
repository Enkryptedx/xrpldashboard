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
        # connect_timeout caps a single connect attempt; TCP keepalives let
        # a half-dead Neon connection surface as an error instead of
        # blocking the worker indefinitely (root cause of the 2026-05-08
        # wedge: socket sat in CLOSE_WAIT while the worker mutex parked).
        _writer_conn = psycopg.connect(
            pg_url(),
            autocommit=True,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
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
    except Exception:
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
    except Exception:
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

def read_recent_events(limit=10):
    """Latest N rows from `events`, no tier/type filter. Mirrors the
    homepage SQLite query so the existing _resolve_event() resolver works
    unchanged. Returns rows in column order:
    tx_hash, ledger_index, ts, type, from_addr, to_addr, amount_drops,
    currency, issuer, raw_json."""
    sql = (
        "SELECT tx_hash, ledger_index, ts, type, from_addr, to_addr, "
        "amount_drops, currency, issuer, raw_json::text FROM events "
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

    trustset rows and tagged-token rows (amount_drops NULL) pass through
    unfiltered — app.whales prices the latter in Python via price_oracle."""
    clauses = [
        "(type = 'trustset' "
        "OR (type = 'tagged' AND amount_drops IS NULL) "
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
    bound on what visitors actually see in the list."""
    counts = {"large_xfer": 0, "tagged": 0, "trustset": 0, "_total": 0}
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT type, COUNT(*) FROM events "
                "WHERE (type = 'trustset' "
                "       OR (type = 'tagged' AND amount_drops IS NULL) "
                "       OR amount_drops >= %s) "
                "GROUP BY type",
                (tier_drops,),
            )
            for type_, n in cur.fetchall():
                if type_ in counts:
                    counts[type_] = n
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
    except Exception:
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
