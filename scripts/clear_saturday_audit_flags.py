#!/usr/bin/env python3
"""Saturday-audit one-shot flag reconciler (2026-09-12).

Clears the materialized `token_facts.ticker_collision` flag on rows
that no longer warrant a warning after the morning's audit rulings:

  1. Memecoin name reuse (PEPE, DOGE, SHIB, BONK) — per Charlie's
     ruling, collision rule narrowed to identity-bearing assets only.
     Meme-name matches are expected practice and deceive no one about
     backing. `resolve_display` already skips the collision path for
     `meme_name=true` entries; this script clears the persisted flag
     so those rows drop out of `WHERE ticker_collision = TRUE` in
     the warnings feed + filter.

  2. RippleFox custodial IOUs (rKiCet8SdvWxPXnAgYarFUXMh1zCPz432Y)
     for XLM + ETH — RippleFox is a 2014-vintage gateway, on-chain
     Domain = ripplefox.com, verified by site content. Now
     whitelisted in the ticker_canonical_issuers.json gateways
     section; same reconciliation pattern as the GateHub + Circle
     scripts from earlier this weekend.

Same pattern as scripts/clear_gatehub_collision_flags.py and
scripts/clear_circle_usdc_collision_flag.py.
"""
import os
import sys
import psycopg

URL = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL")
if not URL:
    sys.exit("neither DATABASE_URL_DIRECT nor DATABASE_URL is set")

MEME_TICKERS = ("PEPE", "DOGE", "SHIB", "BONK")
RIPPLEFOX = "rKiCet8SdvWxPXnAgYarFUXMh1zCPz432Y"
RIPPLEFOX_TICKERS = ("XLM", "ETH", "USD", "CNY", "FMM", "ULT")

cleared_meme = 0
cleared_ripplefox = 0
with psycopg.connect(URL) as conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE token_facts
            SET ticker_collision = FALSE,
                ticker_collision_of = NULL
            WHERE ticker_collision_of = ANY(%s)
              AND ticker_collision = TRUE
            RETURNING decoded_name, issuer
            """,
            (list(MEME_TICKERS),),
        )
        rows = cur.fetchall()
        cleared_meme = len(rows)
        print(f"cleared {cleared_meme} meme-name rows:")
        for name, iss in rows:
            print(f"  {name or '(no name)'} / issuer={iss}")

        cur.execute(
            """
            UPDATE token_facts
            SET ticker_collision = FALSE,
                ticker_collision_of = NULL
            WHERE issuer = %s
              AND ticker_collision = TRUE
              AND ticker_collision_of = ANY(%s)
            RETURNING decoded_name, issuer, currency_hex
            """,
            (RIPPLEFOX, list(RIPPLEFOX_TICKERS)),
        )
        rows = cur.fetchall()
        cleared_ripplefox = len(rows)
        print(f"\ncleared {cleared_ripplefox} RippleFox rows:")
        for name, iss, cx in rows:
            print(f"  {name or '(no name)'} / cur={cx[:20]:<20} / issuer={iss}")

    conn.commit()

print(f"\nTOTAL: {cleared_meme + cleared_ripplefox} rows cleared")
