#!/usr/bin/env python3
"""Populate page_view_scanner_combos_confirmed from the backfill review file.

Reads scratch/scanner_combos_confirmed_review_YYYY-MM-DD.json produced by
scanner_combos_confirmed_backfill.py, filters against the review verdict
(EXCLUDED_COMBOS list below — combos human review left unconfirmed), and
inserts the ratified subset into the confirmed ledger with
confirmed_by='reviewed'.

Idempotent: ON CONFLICT DO NOTHING on (path, user_agent). Re-running with
the same review file changes nothing.

Deploy discipline (see docs/IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md §2):
this runs BEFORE any writer/canary code repoint. Reading paths against
an empty confirmed table triggers a fresh mass-drift event.

Review verdicts recorded 2026-07-26 (Charlie ratify):
  - 16 of 17 candidates ratified with confirmed_by='reviewed'
  - 1 excluded (iPhone iOS 13_2_3 exact UA, 73/73 windows qualified,
    ratio 1.05): category anomaly — frozen-UA-string + all-windows-
    qualifying = fleet-shaped despite ratio at ceiling. Left permanently
    unconfirmed pending settling checks (identity recurrence, timing).
    Do NOT ratify without a follow-up review that produces its own note.

Two entries carry the ChatGPT-User reviewer note:
  "welcome bot — citation moat; ledger records what things are,
   not how we treat them."
"""
import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

logging.basicConfig(
    format="%(asctime)s [scanner_populate] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# Combos EXCLUDED from ratification per Charlie 2026-07-26 review.
# Exact (path, user_agent) match — no fuzzy comparison, no wildcards.
IPHONE_13_2_3_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 "
    "Mobile/15E148 Safari/604.1"
)
EXCLUDED_COMBOS = {
    ("/", IPHONE_13_2_3_UA),
}

# UA substring → reviewer note on the ratified entry
NOTES_BY_UA_SUBSTR = {
    "ChatGPT-User": (
        "welcome bot — citation moat; ledger records what things are, "
        "not how we treat them"
    ),
}


def _note_for(user_agent):
    for substr, note in NOTES_BY_UA_SUBSTR.items():
        if substr in user_agent:
            return note
    return None


def run(review_path, dry_run=False):
    if not db.pg_available():
        log.error("DATABASE_URL not configured — exiting")
        sys.exit(1)

    with open(review_path) as f:
        review = json.load(f)

    candidates = review["candidates"]
    log.info(
        "loaded %d candidates from %s (generated %s)",
        len(candidates),
        review_path,
        review["generated_at"],
    )

    ratified = []
    excluded = []
    for c in candidates:
        key = (c["path"], c["user_agent"])
        if key in EXCLUDED_COMBOS:
            excluded.append(c)
        else:
            ratified.append(c)

    log.info(
        "review verdict: %d ratified, %d excluded",
        len(ratified),
        len(excluded),
    )
    for c in excluded:
        log.info(
            "  EXCLUDED: path=%s ua=%.60s... windows_qualified=%d",
            c["path"],
            c["user_agent"],
            c["total_windows_qualified"],
        )

    if len(ratified) != 16:
        log.error(
            "expected 16 ratified entries, got %d — bailing out (safety check)",
            len(ratified),
        )
        sys.exit(2)

    if dry_run:
        log.info("--dry-run — no inserts")
        for c in ratified:
            note = _note_for(c["user_agent"])
            log.info(
                "  WOULD INSERT: path=%s ua=%.60s... ratio=%.4f hits=%d note=%r",
                c["path"],
                c["user_agent"],
                c["first_seen_ratio"],
                c["first_seen_hits"],
                note,
            )
        return

    inserted, skipped_existing = 0, 0
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            for c in ratified:
                note = _note_for(c["user_agent"])
                cur.execute(
                    "INSERT INTO page_view_scanner_combos_confirmed "
                    "(path, user_agent, confirmed_by, evidence_ratio, "
                    " evidence_row_count, evidence_window_start, "
                    " evidence_window_end, last_seen_at, notes) "
                    "VALUES (%s, %s, 'reviewed', %s, %s, %s, %s, NOW(), %s) "
                    "ON CONFLICT (path, user_agent) DO NOTHING",
                    [
                        c["path"],
                        c["user_agent"],
                        c["first_seen_ratio"],
                        c["first_seen_hits"],
                        c["first_seen_window_start"],
                        c["first_seen_window_end"],
                        note,
                    ],
                )
                if cur.rowcount == 1:
                    inserted += 1
                else:
                    skipped_existing += 1

            # Verification: total rows in the confirmed table
            cur.execute(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE confirmed_by = 'reviewed'), "
                "       COUNT(*) FILTER (WHERE confirmed_by = 'auto'), "
                "       COUNT(*) FILTER (WHERE confirmed_by = 'manual') "
                "  FROM page_view_scanner_combos_confirmed"
            )
            total, n_reviewed, n_auto, n_manual = cur.fetchone()
        conn.commit()

    log.info(
        "insert result: inserted=%d skipped_existing=%d",
        inserted,
        skipped_existing,
    )
    log.info(
        "ledger state now: total=%d reviewed=%d auto=%d manual=%d",
        total,
        n_reviewed,
        n_auto,
        n_manual,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--review",
        type=str,
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scratch",
            "scanner_combos_confirmed_review_2026-07-26.json",
        ),
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(args.review, dry_run=args.dry_run)
