"""
Initialize sqlite stores for whale events + token volumes.

Idempotent — safe to re-run. Creates events.db and volumes.db in the
project root with the schemas documented in WHALE_WATCH.md and
TOKEN_ECONOMY.md. Future stream handlers will land their writes here.
"""

import os
import sqlite3
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS_DB = os.path.join(HERE, "events.db")
VOLUMES_DB = os.path.join(HERE, "volumes.db")


EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  tx_hash       TEXT PRIMARY KEY,
  ledger_index  INTEGER NOT NULL,
  ts            INTEGER NOT NULL,
  type          TEXT NOT NULL,
  from_addr     TEXT,
  to_addr       TEXT,
  amount_drops  INTEGER,
  currency      TEXT,
  issuer        TEXT,
  raw_json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts_idx   ON events(ts);
CREATE INDEX IF NOT EXISTS events_type_idx ON events(type);
"""

VOLUMES_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_volume (
  currency      TEXT NOT NULL,
  issuer        TEXT NOT NULL,
  hour_bucket   INTEGER NOT NULL,
  volume_xrp    REAL NOT NULL,
  trade_count   INTEGER NOT NULL,
  PRIMARY KEY (currency, issuer, hour_bucket)
);
CREATE INDEX IF NOT EXISTS token_volume_bucket_idx
  ON token_volume(hour_bucket);

CREATE TABLE IF NOT EXISTS token_price_snap (
  currency      TEXT NOT NULL,
  issuer        TEXT NOT NULL,
  ts            INTEGER NOT NULL,
  price_xrp     REAL NOT NULL,
  source_amm    TEXT NOT NULL,
  PRIMARY KEY (currency, issuer, ts)
);
CREATE INDEX IF NOT EXISTS token_price_snap_ts_idx
  ON token_price_snap(ts);
"""


def init_db(path, schema_sql, label):
    fresh = not os.path.exists(path)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
    finally:
        conn.close()
    state = "created" if fresh else "verified"
    print(f"  {label}: {state}  path={path}  tables={tables}")


def main():
    print("Initializing sqlite stores...")
    init_db(EVENTS_DB, EVENTS_SCHEMA, "events.db ")
    init_db(VOLUMES_DB, VOLUMES_SCHEMA, "volumes.db")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
