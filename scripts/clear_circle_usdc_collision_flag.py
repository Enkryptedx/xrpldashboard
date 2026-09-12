#!/usr/bin/env python3
"""One-shot curator refresh: clear the materialized ticker_collision flag
on Circle's real USDC issuer row after ticker_canonical_issuers.json
whitelisted rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE (2026-09-11 Circle-USDC
ruling).

Prior state: our USDC canonical_issuers list was empty; the collision
detector flagged Circle's real address as impostor. token_facts row
carries the stored `ticker_collision=TRUE` flag, which drives the
Warnings feed and /tokens?range=warnings filter. Read-path display
already renders correctly the moment the JSON edit lands (resolve_display
reads live). This script only reconciles the materialized column so
Circle's USDC stops appearing in the warnings feed and filter.

Sourced env: DATABASE_URL_DIRECT (non-pooled — safest for a one-shot
UPDATE bypassing pooler session state). Same pattern as
scripts/clear_gatehub_collision_flags.py from earlier tonight.

Written 2026-09-11 per Charlie's explicit authorization; not scheduled.
"""
import os
import sys
import psycopg

URL = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL")
if not URL:
    sys.exit("neither DATABASE_URL_DIRECT nor DATABASE_URL is set")

CIRCLE_USDC_ISSUER = "rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE"
CIRCLE_USDC_CURRENCY_HEX = "5553444300000000000000000000000000000000"

with psycopg.connect(URL) as conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE token_facts
            SET ticker_collision = FALSE,
                ticker_collision_of = NULL
            WHERE issuer = %s
              AND currency_hex = %s
              AND ticker_collision = TRUE
            RETURNING decoded_name, currency_hex, issuer,
                      ticker_collision, ticker_collision_of
            """,
            (CIRCLE_USDC_ISSUER, CIRCLE_USDC_CURRENCY_HEX),
        )
        rows = cur.fetchall()
    conn.commit()

print(f"cleared {len(rows)} rows:")
for name, cur_hex, iss, coll, coll_of in rows:
    print(f"  {name or '(no name)'} / cur={cur_hex[:20]:<20} / "
          f"issuer={iss} -> collision={coll} of={coll_of}")
