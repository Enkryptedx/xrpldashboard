"""supply_block — shared schema + read helper for the homepage XRP supply
distribution block.

Charlie ruling 2026-09-24 15:37 ET (Interpretation B) + 16:08 ET (cadence
c): two walkers upsert the same singleton row, each updating its own
figure. The block reads once at request time and shows per-figure
`ledger #N · updated Ns ago`.

Fast side (StartInterval=60): total_coins from ledger(validated).
Slow side (StartInterval=900, wrapper lockfile prevents overlaps):
  full ledger_data(type=escrow) enumeration → escrow_all_* and
  escrow_ripple_* aggregates.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import db


TABLE_DDL = """
CREATE TABLE IF NOT EXISTS xrp_supply_block (
  singleton_key CHAR(1) PRIMARY KEY DEFAULT '1' CHECK (singleton_key='1'),

  -- Fast walker (StartInterval=60): one ledger(validated) call.
  total_fetched_at_utc TIMESTAMPTZ,
  total_ledger_index BIGINT,
  total_ledger_hash TEXT,
  total_ledger_close_time_iso TEXT,
  total_drops BIGINT,
  total_xrp NUMERIC(20,6),

  -- Slow walker (StartInterval=900, lockfile-guarded): full escrow enum.
  escrow_fetched_at_utc TIMESTAMPTZ,
  escrow_ledger_index BIGINT,
  escrow_ledger_hash TEXT,
  escrow_ledger_close_time_iso TEXT,
  escrow_all_total_drops BIGINT,
  escrow_all_total_xrp NUMERIC(20,6),
  escrow_all_object_count INTEGER,
  escrow_all_account_count INTEGER,
  escrow_all_account_list JSONB,
  escrow_ripple_total_drops BIGINT,
  escrow_ripple_total_xrp NUMERIC(20,6),
  escrow_ripple_object_count INTEGER,
  escrow_enum_pages INTEGER,
  escrow_enum_duration_s NUMERIC(10,2)
);
"""


def ensure_table() -> None:
    """Idempotent create — safe to call from either walker."""
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(TABLE_DDL)
        conn.commit()


def read_supply_block() -> dict | None:
    """App-side reader used by _build_xrp_distribution.

    Returns None if the singleton row doesn't exist yet (fresh table).
    Returns a dict shaped for template consumption:

      {
        "total": {
            "xrp": float, "drops": int,
            "ledger_index": int, "ledger_hash": str,
            "ledger_close_time_iso": str,
            "fetched_at_utc": datetime, "age_seconds": float | None,
        },
        "escrow_all": {
            "total_xrp": float, "total_drops": int,
            "object_count": int, "account_count": int,
            "ledger_index": int, "ledger_hash": str,
            "ledger_close_time_iso": str,
            "fetched_at_utc": datetime, "age_seconds": float | None,
            "enum_pages": int, "enum_duration_s": float,
        },
        "escrow_ripple": {
            "total_xrp": float, "total_drops": int,
            "object_count": int,
            # Same age/ledger as escrow_all — same walker.
        },
      }

    Each side (total / escrow) has its OWN fetched_at + ledger_index so
    the template can show per-figure "ledger #N · updated Ns ago".
    """
    if not db.pg_available():
        return None
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT total_fetched_at_utc, total_ledger_index, total_ledger_hash,
                       total_ledger_close_time_iso, total_drops, total_xrp,
                       escrow_fetched_at_utc, escrow_ledger_index, escrow_ledger_hash,
                       escrow_ledger_close_time_iso,
                       escrow_all_total_drops, escrow_all_total_xrp,
                       escrow_all_object_count, escrow_all_account_count,
                       escrow_ripple_total_drops, escrow_ripple_total_xrp,
                       escrow_ripple_object_count,
                       escrow_enum_pages, escrow_enum_duration_s
                  FROM xrp_supply_block
                 WHERE singleton_key = '1'
                """
            )
            row = cur.fetchone()
    if not row:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    (t_fetched, t_li, t_lh, t_lct, t_drops, t_xrp,
     e_fetched, e_li, e_lh, e_lct,
     e_all_drops, e_all_xrp, e_all_objs, e_all_accts,
     e_rip_drops, e_rip_xrp, e_rip_objs,
     e_pages, e_dur) = row
    out: dict = {}
    if t_fetched is not None:
        out["total"] = {
            "xrp": float(t_xrp),
            "drops": int(t_drops),
            "ledger_index": int(t_li),
            "ledger_hash": t_lh,
            "ledger_close_time_iso": t_lct,
            "fetched_at_utc": t_fetched,
            "age_seconds": (now - t_fetched).total_seconds(),
        }
    if e_fetched is not None:
        out["escrow_all"] = {
            "total_xrp": float(e_all_xrp),
            "total_drops": int(e_all_drops),
            "object_count": int(e_all_objs),
            "account_count": int(e_all_accts),
            "ledger_index": int(e_li),
            "ledger_hash": e_lh,
            "ledger_close_time_iso": e_lct,
            "fetched_at_utc": e_fetched,
            "age_seconds": (now - e_fetched).total_seconds(),
            "enum_pages": int(e_pages),
            "enum_duration_s": float(e_dur),
        }
        out["escrow_ripple"] = {
            "total_xrp": float(e_rip_xrp),
            "total_drops": int(e_rip_drops),
            "object_count": int(e_rip_objs),
        }
    return out


def upsert_total_side(fetched_at, ledger_index, ledger_hash,
                     ledger_close_time_iso, total_drops, total_xrp) -> None:
    """Fast-walker write: only updates total_* columns."""
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(TABLE_DDL)
            cur.execute(
                """
                INSERT INTO xrp_supply_block (
                    singleton_key,
                    total_fetched_at_utc, total_ledger_index, total_ledger_hash,
                    total_ledger_close_time_iso, total_drops, total_xrp
                ) VALUES ('1', %s, %s, %s, %s, %s, %s)
                ON CONFLICT (singleton_key) DO UPDATE SET
                    total_fetched_at_utc = EXCLUDED.total_fetched_at_utc,
                    total_ledger_index = EXCLUDED.total_ledger_index,
                    total_ledger_hash = EXCLUDED.total_ledger_hash,
                    total_ledger_close_time_iso = EXCLUDED.total_ledger_close_time_iso,
                    total_drops = EXCLUDED.total_drops,
                    total_xrp = EXCLUDED.total_xrp
                """,
                (fetched_at, ledger_index, ledger_hash,
                 ledger_close_time_iso, total_drops, total_xrp),
            )
        conn.commit()


def upsert_escrow_side(fetched_at, ledger_index, ledger_hash,
                       ledger_close_time_iso,
                       all_drops, all_xrp, all_object_count, all_account_count,
                       all_account_list_json,
                       ripple_drops, ripple_xrp, ripple_object_count,
                       enum_pages, enum_duration_s) -> None:
    """Slow-walker write: only updates escrow_* columns."""
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(TABLE_DDL)
            cur.execute(
                """
                INSERT INTO xrp_supply_block (
                    singleton_key,
                    escrow_fetched_at_utc, escrow_ledger_index, escrow_ledger_hash,
                    escrow_ledger_close_time_iso,
                    escrow_all_total_drops, escrow_all_total_xrp,
                    escrow_all_object_count, escrow_all_account_count,
                    escrow_all_account_list,
                    escrow_ripple_total_drops, escrow_ripple_total_xrp,
                    escrow_ripple_object_count,
                    escrow_enum_pages, escrow_enum_duration_s
                ) VALUES ('1', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (singleton_key) DO UPDATE SET
                    escrow_fetched_at_utc = EXCLUDED.escrow_fetched_at_utc,
                    escrow_ledger_index = EXCLUDED.escrow_ledger_index,
                    escrow_ledger_hash = EXCLUDED.escrow_ledger_hash,
                    escrow_ledger_close_time_iso = EXCLUDED.escrow_ledger_close_time_iso,
                    escrow_all_total_drops = EXCLUDED.escrow_all_total_drops,
                    escrow_all_total_xrp = EXCLUDED.escrow_all_total_xrp,
                    escrow_all_object_count = EXCLUDED.escrow_all_object_count,
                    escrow_all_account_count = EXCLUDED.escrow_all_account_count,
                    escrow_all_account_list = EXCLUDED.escrow_all_account_list,
                    escrow_ripple_total_drops = EXCLUDED.escrow_ripple_total_drops,
                    escrow_ripple_total_xrp = EXCLUDED.escrow_ripple_total_xrp,
                    escrow_ripple_object_count = EXCLUDED.escrow_ripple_object_count,
                    escrow_enum_pages = EXCLUDED.escrow_enum_pages,
                    escrow_enum_duration_s = EXCLUDED.escrow_enum_duration_s
                """,
                (fetched_at, ledger_index, ledger_hash,
                 ledger_close_time_iso,
                 all_drops, all_xrp, all_object_count, all_account_count,
                 all_account_list_json,
                 ripple_drops, ripple_xrp, ripple_object_count,
                 enum_pages, enum_duration_s),
            )
        conn.commit()
