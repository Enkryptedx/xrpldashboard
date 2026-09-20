"""nft_catchup_check.py — one-off helper for nft_catchup_loop.sh.

Prints "<cursor_ledger> <lenovo_head>" on stdout so a bash loop can compare
without heredoc quoting. Exits 0 on success, non-zero on any failure (bash
loop should treat non-zero as "keep going" — the walker will surface real
issues in its own log).

Filed 2026-09-20 after the first attempt's heredoc-in-bash-c version died
under set -euo pipefail because urllib.request.Request bytes-string escaping
inside a shell heredoc couldn't be trusted. Standalone Python script keeps
the parsing/data out of the shell.
"""
from __future__ import annotations
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402


LENOVO_URL = os.environ.get("XRPL_LOCAL_NODE", "http://192.168.40.95:5006")


def main() -> int:
    with db.pg_connect() as c:
        cur = c.cursor()
        cur.execute("SELECT cursor_ledger FROM nft_walker_state WHERE walker_name='nft_activity'")
        row = cur.fetchone()
        if not row:
            print("0 0")
            return 2
        cursor = int(row[0])

    body = json.dumps({"method": "ledger_current", "params": [{}]}).encode()
    req = urllib.request.Request(
        LENOVO_URL,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=6).read()
        head = int(json.loads(resp).get("result", {}).get("ledger_current_index") or 0)
    except Exception:
        head = 0

    print(f"{cursor} {head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
