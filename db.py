"""Postgres scaffold for xrpldashboard.

Status: parked. The storage decision (SQLite-snapshots vs hosted Postgres)
is a post-launch call. This module lays the rails so flipping the switch
later is one env-var change, not a refactor.

How it works
------------
- `DATABASE_URL` env var unset (current state): every helper here is a
  no-op or raises a clear error. Existing SQLite/JSON code paths in
  `app.py` continue to be the source of truth.
- `DATABASE_URL` set + psycopg installed: helpers connect to Postgres.
  Read paths in `app.py` are wrapped to prefer Postgres and fall back
  to SQLite on any error, so partial outages don't break the site.

Why the fallback pattern
------------------------
If we ever cut over to Postgres and it has a bad day, the website should
serve stale-but-correct data from the committed SQLite snapshots, not
500. The dual-read pattern guarantees that: Postgres is an upgrade, not
a single point of failure.

Schema (proposed, not yet applied)
----------------------------------
The schema below mirrors the existing SQLite tables in `events.db` and
`volumes.db` so the migration is a one-shot copy. Bring up Alembic when
the decision is final; for now the DDL is committed as a string for
reference.

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

    CREATE TABLE IF NOT EXISTS token_volumes (
        currency  TEXT NOT NULL,
        issuer    TEXT NOT NULL,
        bucket_ts BIGINT NOT NULL,
        volume    NUMERIC NOT NULL,
        PRIMARY KEY (currency, issuer, bucket_ts)
    );

Worker-side dual-write (parked)
-------------------------------
Once a partner conversation justifies Postgres, the workers
(`xrpl_stream.py`, `scan_all_amms.py`) should call `write_event()` and
similar helpers added here. Until then, workers keep writing only to
SQLite — no behavior change.
"""

import os
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

CREATE TABLE IF NOT EXISTS token_volumes (
    currency  TEXT NOT NULL,
    issuer    TEXT NOT NULL,
    bucket_ts BIGINT NOT NULL,
    volume    NUMERIC NOT NULL,
    PRIMARY KEY (currency, issuer, bucket_ts)
);
"""


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
    """Context-managed psycopg connection. Raises RuntimeError when
    Postgres isn't configured — callers should gate with pg_available()
    or wrap in a try/except and fall back."""
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
    re-run. Intended for one-shot bootstrap until Alembic lands."""
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_DDL)
        conn.commit()


def read_recent_events(limit=3):
    """Read the most recent N events from Postgres. Returns raw rows
    in the same column order as the SQLite query in app.py
    (`tx_hash, ledger_index, ts, type, from_addr, to_addr, amount_drops,
    currency, issuer, raw_json`) so the SQLite resolver code is reusable."""
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tx_hash, ledger_index, ts, type, from_addr, to_addr, "
                "amount_drops, currency, issuer, raw_json FROM events "
                "ORDER BY ts DESC LIMIT %s",
                (limit,),
            )
            return cur.fetchall()
