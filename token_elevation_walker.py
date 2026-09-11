"""Token-tier auto-elevation walker.

Charlie ruling 2026-09-10 (Part C item 5): hourly scan of
token_category_current for `labeled` or `self-described` rows with
https .toml citation_url. For each candidate, invoke
shared_tier_verifier.resolve_tier(cx, iss, elevate=True) which:
  - Checks the 24h TTL cache (process-local — cache is fresh each
    walker run; per-pair attempt budgeting is bounded by the run cadence).
  - Runs two_way_toml_verifier.verify_submission (FORWARD toml lookup +
    REVERSE ledger Domain compare, certifi-backed SSL).
  - On success: writes a new token_category_history row with
    tier='verified', source='two_way_toml', curator_authority='automated'.
  - Fails open on any error — leaves current tier unchanged, logs reason.

Stamps launchd_state/token_elevation_walker_last_ok on every clean run
(success-only, per the JOBS convention — SKIP/FAIL leaves stale so
persistent silence surfaces via the meta-watcher, not looking healthy).

Meta-watched via dockvault_mirror_freshness_canary JOBS (2h ceiling).
Never on the request path — /whales, /token, /check read the cached
result via shared_tier_verifier.resolve_tier(..., elevate=False).
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db
import shared_tier_verifier as stv


LAUNCHD_STATE_DIR = "/Users/charliebruce/xrpl_test/launchd_state"
STAMP_PATH = os.path.join(LAUNCHD_STATE_DIR, "token_elevation_walker_last_ok")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    print(f"[{_now_iso()}] token_elevation_walker: {msg}", flush=True)


def find_candidates() -> list[tuple[str, str, str, str, str]]:
    """Return (currency_hex, issuer, citation_url, current_tier, category)
    for every labeled or self-described pair with an https .toml citation
    that has NOT already been elevated via two_way_toml (avoid busy-loops)."""
    if not db.pg_available():
        raise SystemExit("STRICT-REFUSE: PG unavailable")
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT currency_hex, issuer, citation_url, tier, category
                FROM token_category_current
                WHERE tier IN ('labeled', 'self-described')
                  AND citation_url IS NOT NULL
                  AND citation_url LIKE %s
                ORDER BY currency_hex, issuer
                """,
                ("https://%.toml",),
            )
            return list(cur.fetchall())


def run() -> dict:
    """Process every candidate. Returns a summary dict + per-pair records."""
    start_ts = time.time()
    candidates = find_candidates()
    _log(f"start — {len(candidates)} candidate(s) (labeled/self-described with https .toml citation)")

    elevated_now: list[dict] = []
    unchanged: list[dict] = []

    for cx, iss, cit, prior_tier, prior_cat in candidates:
        rec = stv.resolve_tier(cx, iss, elevate=True)
        # _try_elevate returns source='db-registry+two_way_toml' (with '+')
        # ONLY on a fresh success in THIS process. Prior-run verifications
        # come back as source='db-registry:two_way_toml' (with ':').
        was_elevated_this_run = (
            rec.tier == stv.TIER_VERIFIED
            and rec.source
            and "+two_way_toml" in rec.source
        )
        row = {
            "currency_hex": cx,
            "issuer": iss,
            "citation_url": cit,
            "prior_tier": prior_tier,
            "final_tier": rec.tier,
            "final_source": rec.source,
        }
        if was_elevated_this_run:
            elevated_now.append(row)
            _log(f"  ELEVATED {cx[:16]}…|{iss[:10]}… ({prior_tier} → verified)")
        else:
            unchanged.append(row)

    # Stamp on any successful completion (regardless of elevation counts).
    os.makedirs(LAUNCHD_STATE_DIR, exist_ok=True)
    with open(STAMP_PATH, "w") as f:
        f.write(f"{int(time.time())}\n")

    duration_s = time.time() - start_ts
    summary = {
        "candidates": len(candidates),
        "elevated_this_run": len(elevated_now),
        "unchanged": len(unchanged),
        "duration_s": round(duration_s, 2),
        "elevated_details": elevated_now,
    }
    _log(
        f"end — candidates={summary['candidates']} "
        f"elevated={summary['elevated_this_run']} "
        f"unchanged={summary['unchanged']} "
        f"duration={summary['duration_s']}s "
        f"stamp={STAMP_PATH}"
    )
    return summary


if __name__ == "__main__":
    s = run()
    if s["elevated_this_run"]:
        for e in s["elevated_details"]:
            print(f"  + {e['currency_hex']}|{e['issuer']}  "
                  f"({e['prior_tier']} → {e['final_tier']} · {e['final_source']})")
