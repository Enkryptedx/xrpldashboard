"""
Wound-B reproduction — canary writer_connect_failed.

Purpose: reproduce the exact `unsupported startup parameter in options:
statement_timeout` error the pg_backup_canary hits when writing walker_health
telemetry through the pooler. Reveal the ACTUAL mechanism before naming a
cause (per feedback_verify_script_architecture_before_verdict.md and
feedback_grep_before_naming_from_memory.md).

Run:
    source ~/.config/xrpldashboard/env  # exports DATABASE_URL
    python3 tools/repro_canary_writer_connect.py

Expected output: labeled trials showing WHICH connection shape triggers the
error and WHICH does not. That narrows the mechanism to one of:
  - psycopg auto-adding `options=` from an env var (PGOPTIONS)
  - db.py connection helper injecting a startup param
  - driver default we're unaware of

Do NOT run this on Neon during production hours if you're worried about
transient connection load. It opens ~6 short-lived connections. Safe otherwise.
"""

from __future__ import annotations

import os
import sys
import traceback

try:
    import psycopg  # psycopg3
    DRIVER = "psycopg3"
except ImportError:
    try:
        import psycopg2 as psycopg  # type: ignore
        DRIVER = "psycopg2"
    except ImportError:
        print("FATAL: no psycopg / psycopg2 installed", file=sys.stderr)
        sys.exit(2)


DB_URL = os.environ.get("DATABASE_URL")
if not DB_URL:
    print("FATAL: DATABASE_URL not set (source ~/.config/xrpldashboard/env)", file=sys.stderr)
    sys.exit(2)

# Derive direct-endpoint variant (strip `-pooler.` → `.` per def145d convention)
DIRECT_URL = DB_URL.replace("-pooler.", ".")

# Sanitize for display — hide the password
def mask(url: str) -> str:
    import re
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", url)


def trial(label: str, url: str, extra_kwargs: dict | None = None) -> None:
    print(f"\n=== TRIAL: {label} ===")
    print(f"  URL: {mask(url)}")
    print(f"  extra kwargs: {extra_kwargs or {}}")
    print(f"  PGOPTIONS env: {os.environ.get('PGOPTIONS', '<unset>')}")
    try:
        kwargs = extra_kwargs or {}
        if DRIVER == "psycopg3":
            conn = psycopg.connect(url, **kwargs)
        else:
            conn = psycopg.connect(url, **kwargs)  # type: ignore
        try:
            cur = conn.cursor()
            cur.execute("SELECT current_setting('statement_timeout'), version()")
            row = cur.fetchone()
            print(f"  RESULT: OK — statement_timeout={row[0]!r}")
            print(f"  version: {row[1][:60]}")
        finally:
            conn.close()
    except Exception as e:
        print(f"  RESULT: FAIL — {type(e).__name__}")
        # Print only the first 3 lines of message; server error text is what we want
        msg = str(e).strip().splitlines()
        for line in msg[:3]:
            print(f"    {line}")


def main() -> None:
    print(f"Driver: {DRIVER}")
    print(f"POOLER URL:  {mask(DB_URL)}")
    print(f"DIRECT URL:  {mask(DIRECT_URL)}")
    if DB_URL == DIRECT_URL:
        print("WARNING: DATABASE_URL does not contain '-pooler.' — is env set correctly?")

    # Trial 1: baseline — pooler URL, no extra args, no PGOPTIONS env
    if "PGOPTIONS" in os.environ:
        saved = os.environ.pop("PGOPTIONS")
    else:
        saved = None
    trial("pooler, no extras, no PGOPTIONS", DB_URL)

    # Trial 2: pooler URL + PGOPTIONS=-c statement_timeout=25s in env
    os.environ["PGOPTIONS"] = "-c statement_timeout=25s"
    trial("pooler + PGOPTIONS='-c statement_timeout=25s'", DB_URL)

    # Trial 3: pooler URL + kwargs options
    os.environ.pop("PGOPTIONS", None)
    trial("pooler + connect(options='-c statement_timeout=25s')", DB_URL,
          {"options": "-c statement_timeout=25s"})

    # Trial 4: direct URL, no extras
    trial("direct (pooler stripped), no extras", DIRECT_URL)

    # Trial 5: direct URL + PGOPTIONS
    os.environ["PGOPTIONS"] = "-c statement_timeout=25s"
    trial("direct + PGOPTIONS='-c statement_timeout=25s'", DIRECT_URL)

    # Trial 6: mimic canary's actual codepath by importing db.py's connector
    os.environ.pop("PGOPTIONS", None)
    if saved is not None:
        os.environ["PGOPTIONS"] = saved
    print("\n=== TRIAL: canary's db.py get_conn() (if importable) ===")
    try:
        sys.path.insert(0, os.path.expanduser("~/xrpl_test"))
        from db import get_conn  # type: ignore
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT current_setting('statement_timeout'), current_setting('application_name')")
            row = cur.fetchone()
            print(f"  RESULT: OK — statement_timeout={row[0]!r} application_name={row[1]!r}")
        finally:
            conn.close()
    except Exception as e:
        print(f"  RESULT: FAIL — {type(e).__name__}")
        for line in str(e).strip().splitlines()[:5]:
            print(f"    {line}")
        traceback.print_exc(limit=3)

    print("\n=== INTERPRETATION KEY ===")
    print("  Only trials 2, 3 (pooler + options via env or kwarg) should FAIL")
    print("  with 'unsupported startup parameter in options: statement_timeout'.")
    print("  Trial 6 (canary's own get_conn) — if it FAILS, db.py is injecting")
    print("  options= at connection time. If it OK's, canary's failure has a")
    print("  different mechanism (maybe app_name or something else in options).")


if __name__ == "__main__":
    main()
