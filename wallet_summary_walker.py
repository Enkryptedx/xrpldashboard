"""wallet_summary walker — 5-min cadence, meta-watched, stamps _last_ok.

Charlie ruling 2026-09-21 Mon PM: exchange-scale /wallet cold-render
is ~8s because the adaptive-cap fetch pulls 20,000 tx envelopes from
the LAN node. That exceeds the 4s label-agreement canary timeout,
and a real reader waits the same 8s. Pre-render the top-20
exchange-scale addresses into a summary row so the cache-miss cost
becomes a sub-ms PG PK read.

## Design

- Seed list of ~20 known exchange hot wallets (Binance × 3-5,
  Coinbase × 3, Kraken, Bitstamp, Gate.io, HTX, KuCoin, MEXC, OKX,
  Bybit) built dynamically from PG account_labels (source=xrpscan)
  + named_accounts.json each cycle. If the curator adds a new
  exchange, the walker picks it up.
- For each address, drive `/wallet/<addr>` via test_client() with
  the in-process cache bypass flag, harvest the HTML body + gen_ms.
- Persist the bodies into `wallet_summary` (address PK). Route
  reads sub-ms and seats into the in-process `_WALLET_CACHE` for
  the next request.
- Meta-watched: stamps `launchd_state/wallet_summary_walker_last_ok`
  on success only.
- Silent-safe when PG-unavailable — still stamps _last_ok if
  render succeeded so the meta-watcher doesn't flap during local
  iteration.

## Route consumer

`app.py` /wallet route: if the in-process `_WALLET_CACHE` misses,
call `db.read_wallet_summary(address)`; if it returns a body and
the row is <30 min old, seat it and serve directly. Else fall
through to the inline render (which will succeed but slowly for
exchange-scale addresses — that's the "warming up" state).
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db  # noqa: E402


WALKER_NAME = "wallet_summary_walker"
WALKER_CADENCE_SECONDS = 300  # 5 min
LAST_OK_STAMP = os.path.join(HERE, "launchd_state", "wallet_summary_walker_last_ok")

# Exchange-scale keyword seed. Any account_labels row whose name
# starts with one of these (source=xrpscan) is a candidate; the
# walker further caps at MAX_ADDRESSES per cycle.
EXCHANGE_NAME_SEEDS = (
    "Binance", "Coinbase", "Coinbase cbXRP", "Kraken", "Bitstamp",
    "Gate.io", "HTX", "KuCoin", "MEXC", "OKX", "Bybit",
    "WazirX", "Bitrue", "Gemini",
)

# Address-explicit seed from named_accounts.json — Bitstamp is
# file-based, not in account_labels.
NAMED_ACCOUNT_ADDRS = (
    "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B",  # Bitstamp (file-based)
)

MAX_ADDRESSES = 20


def _select_addresses() -> list[tuple[str, str]]:
    """Return a list of up to MAX_ADDRESSES (address, name) tuples,
    combining the file-based named_accounts seed with the top
    xrpscan-labeled exchange candidates.

    Priority: file-based first (they're curator-verified), then one
    address per exchange name in EXCHANGE_NAME_SEEDS (the lowest
    desc — usually '1' — is the primary hot wallet).
    """
    picks: list[tuple[str, str]] = []
    seen: set[str] = set()

    # 1. File-based named_accounts seed
    try:
        with open(os.path.join(HERE, "named_accounts.json"), "r", encoding="utf-8") as f:
            named = json.load(f)
        for addr in NAMED_ACCOUNT_ADDRS:
            entry = (named or {}).get(addr) or {}
            name = entry.get("name") or "(named)"
            if addr not in seen:
                picks.append((addr, name))
                seen.add(addr)
    except Exception:
        pass

    # 2. PG account_labels — primary hot wallet per named exchange
    if db.pg_available():
        try:
            with db.pg_connect() as conn, conn.cursor() as cur:
                for nm in EXCHANGE_NAME_SEEDS:
                    if len(picks) >= MAX_ADDRESSES:
                        break
                    # Prefer desc='1' or 'Hot' when available; else
                    # take the first row for this name.
                    cur.execute(
                        "SELECT address, name, extra->>'desc' AS d "
                        "FROM account_labels "
                        "WHERE source='xrpscan' AND name=%s "
                        "ORDER BY "
                        "  CASE WHEN extra->>'desc' = '1' THEN 0 "
                        "       WHEN extra->>'desc' = 'Hot' THEN 1 "
                        "       ELSE 2 END, "
                        "  address "
                        "LIMIT 2",
                        (nm,),
                    )
                    for addr, name, _d in cur.fetchall():
                        if addr not in seen and len(picks) < MAX_ADDRESSES:
                            picks.append((addr, name))
                            seen.add(addr)
        except Exception:
            pass

    return picks


def _render_wallet(app_module, addr: str) -> tuple[bytes, int]:
    """Render one /wallet/<addr> response with cache bypass so the
    walker always produces fresh output. Returns (body, gen_ms)."""
    # Force cache bypass via wallet_data internal cache clear — the
    # /wallet route doesn't have a global bypass flag but wallet_data
    # exposes its cache lock+dict.
    try:
        import wallet_data
        with wallet_data._cache_lock:
            # Only clear THIS address's entry, not the whole cache —
            # multiple concurrent renders would fight otherwise.
            for k in list(wallet_data._cache.keys()):
                if isinstance(k, tuple) and k and k[0] == addr:
                    wallet_data._cache.pop(k, None)
    except Exception:
        pass

    c = app_module.app.test_client()
    t0 = time.perf_counter()
    r = c.get(f"/wallet/{addr}")
    gen_ms = int((time.perf_counter() - t0) * 1000)
    if r.status_code != 200:
        raise RuntimeError(
            f"/wallet/{addr} returned {r.status_code} — walker refusing to persist"
        )
    return r.data, gen_ms


def _persist(bodies: dict[str, str], stats: dict) -> None:
    if not db.pg_available():
        return

    def _do(conn):
        with conn.cursor() as cur:
            # UPSERT each address's body + freshness stamp.
            for addr, body in bodies.items():
                cur.execute(
                    "INSERT INTO wallet_summary "
                    "  (address, body_html, gen_ms, computed_at) "
                    "VALUES (%s, %s, %s, now()) "
                    "ON CONFLICT (address) DO UPDATE SET "
                    "  body_html = EXCLUDED.body_html, "
                    "  gen_ms = EXCLUDED.gen_ms, "
                    "  computed_at = now()",
                    (addr, body, stats.get(addr, 0)),
                )
    db._writer_execute_with_retry("wallet_summary_persist", _do)


def _stamp_last_ok() -> None:
    try:
        os.makedirs(os.path.dirname(LAST_OK_STAMP), exist_ok=True)
        with open(LAST_OK_STAMP, "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = "not_yet_stamped"
    try:
        addrs = _select_addresses()
        if not addrs:
            message = "no_addresses_selected (PG unavailable or no matches)"
            ok = True  # Not a failure — walker did what it could
            return 0

        import app as app_module

        bodies: dict[str, str] = {}
        stats: dict[str, int] = {}
        t0 = time.time()
        for addr, name in addrs:
            try:
                body, gen_ms = _render_wallet(app_module, addr)
                bodies[addr] = body.decode("utf-8", errors="replace")
                stats[addr] = gen_ms
                print(f"[wallet_summary_walker] {name:<20} {addr}  gen={gen_ms}ms")
            except Exception as e:
                # One-address failure shouldn't sink the whole cycle.
                print(f"[wallet_summary_walker] {name} {addr} FAIL: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
        elapsed = time.time() - t0
        _persist(bodies, stats)
        _stamp_last_ok()
        slow = sorted(stats.items(), key=lambda kv: -kv[1])[:3]
        message = (
            f"warmed={len(bodies)}/{len(addrs)} elapsed={elapsed:.1f}s slowest="
            + ", ".join(f"{a[-6:]}:{v}ms" for a, v in slow)
        )
        print(f"[wallet_summary_walker] {message}")
        ok = True
        return 0
    except Exception as e:
        message = f"exception: {type(e).__name__}: {e}"
        raise
    finally:
        db.write_walker_health_end(
            WALKER_NAME, ok=ok,
            message=message or ("clean_no_message" if ok else "unlabeled_failure"),
        )


if __name__ == "__main__":
    sys.exit(main())
