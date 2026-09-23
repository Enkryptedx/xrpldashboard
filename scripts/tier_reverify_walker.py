"""tier_reverify_walker — daily two-way TOML re-verification with a
2-week grace period before any downgrade.

Charlie ruling 2026-09-23 16:09 ET (Walker A):
- Re-run the two-way TOML check on every row with source='two_way_toml'
  in token_category_current.
- PASS → re-affirm the row (upsert token_reverify_state with last_ok=TRUE
  and a cleared streak).
- FAIL → do NOT downgrade on a single failure. Mark first_fail_at,
  last_fail_at + reason in token_reverify_state, retry daily. Only
  downgrade to 'labeled' (source='two_way_toml_reverify_fail') once
  ≥ 14 days of sustained failure have elapsed. A single intervening
  PASS clears first_fail_at.

A transient fetch error (Cloudflare 5xx, DNS blip, upstream WSS wobble)
must never demote a real issuer. That's the whole point.

Cadence: daily (StartInterval=86400s). walker_health-tracked; the
meta-watcher pages if walker_health.last_success_at is > 8 days stale.

## What lands where

- token_reverify_state upsert per row on every check.
- token_category_history NEW row only on tier CHANGE:
    - FAIL→PASS restoration (source='two_way_toml_reverify')
    - Downgrade PASS→labeled after 14d sustained fail
      (source='two_way_toml_reverify_fail')
  A PASS that stays PASS does NOT insert a history row every day —
  that would spam 48 rows × 365 days = 17.5k rows/year for no signal.

## Downgrade semantics

When a row is downgraded, we insert a token_category_history row with
tier='labeled' and source='two_way_toml_reverify_fail'. The
token_category_current VIEW takes the most-recent history row per
(currency_hex, issuer), so the DB will report tier='labeled' immediately.
The prior verified history row is preserved (superseded_by chain) so we
can restore it on the next PASS via source='two_way_toml_reverify'.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import db
from two_way_toml_verifier_walker import verify_submission


WALKER_NAME = "tier_reverify_walker"
WALKER_CADENCE_SECONDS = 86400  # daily
DOWNGRADE_GRACE_DAYS = 14  # Charlie's 2-week grace before demote


def _fetch_workload():
    """Return list of dicts for every row with source='two_way_toml'
    in token_category_current — the pool to re-check daily."""
    with db.pg_connect() as c, c.cursor() as cur:
        cur.execute(
            "SELECT currency_hex, issuer, category, citation_url "
            "FROM token_category_current "
            "WHERE source = 'two_way_toml' "
            "ORDER BY observed_at DESC"
        )
        return [
            {
                "currency_hex": r[0],
                "issuer": r[1],
                "claimed_category": r[2],
                "toml_url": r[3],
            }
            for r in cur.fetchall()
        ]


def _upsert_state_pass(cur, row: dict) -> None:
    """PASS path: clear any failure streak."""
    cur.execute(
        """
        INSERT INTO token_reverify_state (
            currency_hex, issuer, last_check_at, last_ok,
            last_fail_reason, first_fail_at, last_fail_at, downgraded_at
        ) VALUES (%s, %s, NOW(), TRUE, NULL, NULL, NULL, NULL)
        ON CONFLICT (currency_hex, issuer) DO UPDATE
        SET last_check_at = EXCLUDED.last_check_at,
            last_ok = TRUE,
            last_fail_reason = NULL,
            first_fail_at = NULL,
            last_fail_at = NULL,
            downgraded_at = NULL
        """,
        (row["currency_hex"], row["issuer"]),
    )


def _upsert_state_fail(cur, row: dict, reason: str) -> None:
    """FAIL path: bump first_fail_at only if not already set."""
    cur.execute(
        """
        INSERT INTO token_reverify_state (
            currency_hex, issuer, last_check_at, last_ok,
            last_fail_reason, first_fail_at, last_fail_at, downgraded_at
        ) VALUES (%s, %s, NOW(), FALSE, %s, NOW(), NOW(), NULL)
        ON CONFLICT (currency_hex, issuer) DO UPDATE
        SET last_check_at = EXCLUDED.last_check_at,
            last_ok = FALSE,
            last_fail_reason = EXCLUDED.last_fail_reason,
            first_fail_at = COALESCE(token_reverify_state.first_fail_at,
                                     EXCLUDED.first_fail_at),
            last_fail_at = EXCLUDED.last_fail_at
        """,
        (row["currency_hex"], row["issuer"], reason),
    )


def _get_state(cur, row: dict):
    cur.execute(
        "SELECT last_ok, first_fail_at, downgraded_at, last_fail_reason "
        "FROM token_reverify_state "
        "WHERE currency_hex=%s AND issuer=%s",
        (row["currency_hex"], row["issuer"]),
    )
    return cur.fetchone()


def _insert_restoration_history(cur, row: dict) -> int:
    """Insert a token_category_history row marking the FAIL→PASS
    restoration (source='two_way_toml_reverify')."""
    cur.execute(
        """
        INSERT INTO token_category_history (
            currency_hex, issuer, category, tier, source,
            citation_url, curator_id, curator_authority,
            observed_at, taxonomy_version, note
        ) VALUES (%s, %s, %s, 'verified', 'two_way_toml_reverify',
                  %s, NULL, 'walker',
                  NOW(), '1.0.0',
                  'restored by tier_reverify_walker after fail streak cleared')
        RETURNING id
        """,
        (row["currency_hex"], row["issuer"],
         row["claimed_category"], row["toml_url"]),
    )
    return cur.fetchone()[0]


def _insert_downgrade_history(cur, row: dict, reason: str, days: int) -> int:
    """Insert a token_category_history row demoting the pair to 'labeled'
    after the grace period. Note captures the reason + duration."""
    cur.execute(
        """
        INSERT INTO token_category_history (
            currency_hex, issuer, category, tier, source,
            citation_url, curator_id, curator_authority,
            observed_at, taxonomy_version, note
        ) VALUES (%s, %s, %s, 'labeled', 'two_way_toml_reverify_fail',
                  %s, NULL, 'walker',
                  NOW(), '1.0.0', %s)
        RETURNING id
        """,
        (row["currency_hex"], row["issuer"],
         row["claimed_category"], row["toml_url"],
         f"downgraded by tier_reverify_walker after {days}d of two-way "
         f"TOML failure; last reason: {reason}"),
    )
    return cur.fetchone()[0]


def _mark_downgraded(cur, row: dict) -> None:
    cur.execute(
        "UPDATE token_reverify_state SET downgraded_at = NOW() "
        "WHERE currency_hex=%s AND issuer=%s",
        (row["currency_hex"], row["issuer"]),
    )


def run_walker() -> tuple[int, int, int, int, int]:
    """Return (checked, pass_stable, pass_restored, fail_within_grace,
    fail_downgraded)."""
    if not db.pg_available():
        raise SystemExit("STRICT-REFUSE: PG unavailable")

    rows = _fetch_workload()
    checked = 0
    pass_stable = 0
    pass_restored = 0
    fail_within_grace = 0
    fail_downgraded = 0

    for row in rows:
        checked += 1
        try:
            ok, reason, _ = verify_submission(row)
        except Exception as e:
            reason = f"unhandled_{type(e).__name__}"
            ok = False

        with db.pg_connect() as conn, conn.cursor() as cur:
            prior_state = _get_state(cur, row)
            was_failing = bool(prior_state and not prior_state[0])

            if ok:
                if was_failing:
                    hid = _insert_restoration_history(cur, row)
                    pass_restored += 1
                    print(
                        f"[tier_reverify] RESTORED {row['issuer']} "
                        f"{row['currency_hex'][:8]}… → history_id={hid}",
                        flush=True,
                    )
                else:
                    pass_stable += 1
                _upsert_state_pass(cur, row)
            else:
                _upsert_state_fail(cur, row, reason or "unknown_fail")
                # Re-read state to get the (possibly-just-set) first_fail_at
                cur.execute(
                    "SELECT first_fail_at, downgraded_at "
                    "FROM token_reverify_state "
                    "WHERE currency_hex=%s AND issuer=%s",
                    (row["currency_hex"], row["issuer"]),
                )
                first_fail_at, downgraded_at = cur.fetchone()
                days_failing = (
                    (dt.datetime.now(dt.timezone.utc) - first_fail_at).days
                    if first_fail_at else 0
                )
                if (days_failing >= DOWNGRADE_GRACE_DAYS
                        and downgraded_at is None):
                    hid = _insert_downgrade_history(
                        cur, row, reason or "unknown_fail", days_failing,
                    )
                    _mark_downgraded(cur, row)
                    fail_downgraded += 1
                    print(
                        f"[tier_reverify] DOWNGRADED {row['issuer']} "
                        f"{row['currency_hex'][:8]}… after {days_failing}d "
                        f"({reason}) → history_id={hid}",
                        flush=True,
                    )
                else:
                    fail_within_grace += 1
                    print(
                        f"[tier_reverify] FAIL(grace) {row['issuer']} "
                        f"{row['currency_hex'][:8]}… day={days_failing}/"
                        f"{DOWNGRADE_GRACE_DAYS}: {reason}",
                        flush=True,
                    )
            conn.commit()

    return checked, pass_stable, pass_restored, fail_within_grace, fail_downgraded


def main() -> int:
    db.write_walker_health_start(
        WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS,
    )
    ok = False
    message = "not_yet_stamped"
    now_utc = dt.datetime.now(dt.timezone.utc)
    print(
        f"[tier_reverify] start {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        flush=True,
    )
    t0 = time.time()
    try:
        checked, stable, restored, grace, demoted = run_walker()
        elapsed = time.time() - t0
        ok = True
        message = (
            f"checked={checked} stable={stable} restored={restored} "
            f"grace={grace} demoted={demoted} elapsed={elapsed:.1f}s"
        )
        print(f"[tier_reverify] end OK: {message}", flush=True)
    except SystemExit as e:
        message = f"strict_refuse: {e}"
        print(f"[tier_reverify] {message}", file=sys.stderr, flush=True)
        return 1
    except Exception as e:
        message = f"unhandled_{type(e).__name__}: {str(e)[:120]}"
        print(f"[tier_reverify] {message}", file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            db.write_walker_health_end(
                WALKER_NAME, ok=ok,
                message=message or ("clean_no_message" if ok else "unlabeled_failure"),
            )
        except Exception as e:
            print(
                f"[tier_reverify] walker_health end write failed: {e}",
                file=sys.stderr, flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
