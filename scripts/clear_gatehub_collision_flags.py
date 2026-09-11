#!/usr/bin/env python3
"""One-shot curator refresh: clear the materialized ticker_collision flag
on the 5 GateHub cold-wallet rows that flipped via ticker_canonical_issuers.json
commit 289e902 (2026-09-11 gatehub whitelist add).

Read paths already return the correct value via resolve_display() live from
the JSON; this script only reconciles the token_facts materialized column
so admin curator queries (WHERE ticker_collision=TRUE) stop listing these
five as impostor collisions.

Sourced env: DATABASE_URL_DIRECT (non-pooled endpoint — safest for a
one-shot UPDATE bypassing the pooler's session state).

Written 2026-09-11 per Charlie's explicit authorization; not scheduled.
"""
import os
import sys
import psycopg

URL = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL")
if not URL:
    sys.exit("neither DATABASE_URL_DIRECT nor DATABASE_URL is set")

GATEHUB_ADDRESSES_FLIPPED = [
    ("rcEGREd8NmkKRE8GE424sksyt1tJVFZwu", "USDC"),
    ("rcvxE9PS9YBwxtGg1qNeewV6ZB3wGubZq", "USDT"),
    ("rchGBxcD1A1C2tdxF6papQYZ8kjRKMYcL", "BTC"),
    ("rcA8X3TVMST1n3CJeAdGk1RdRCHii7N2h", "ETH"),
    ("rcRzGWq6Ng3jeYhqnmM4zcWcUh69hrQ8V", "LTC"),
]
addrs = [a for a, _ in GATEHUB_ADDRESSES_FLIPPED]

with psycopg.connect(URL) as conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE token_facts
            SET ticker_collision = FALSE,
                ticker_collision_of = NULL
            WHERE issuer = ANY(%s)
              AND ticker_collision = TRUE
            RETURNING decoded_name, currency_hex, issuer, ticker_collision, ticker_collision_of
            """,
            (addrs,),
        )
        rows = cur.fetchall()
    conn.commit()

print(f"cleared {len(rows)} rows:")
for name, cur_hex, iss, coll, coll_of in rows:
    print(f"  {name or '(no name)'} / cur={cur_hex[:20]:<20} / issuer={iss} -> collision={coll} of={coll_of}")
