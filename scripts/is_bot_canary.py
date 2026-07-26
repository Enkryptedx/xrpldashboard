#!/usr/bin/env python3
"""is_bot column drift canary — Layer 3 cross-check.

Compares human-row counts between:
  Path A: is_bot column  (WHERE is_bot IS NOT TRUE)
  Path B: live predicate (_bot_filter_sql legacy subquery form — never deleted,
          retained permanently as the authoritative comparison arm)

Alarm on any divergence outside the in-flight window. Delivers results as
walker_health rows (name='is_bot_canary') so the existing answer_plausibility
framework sees it without a new alert path.

Runs daily via launchd.

Windows checked:
  1. Trailing 7 days, excluding the in-flight window (rows newer than
     now − 10min that the writer may not have stamped yet)
  2. Rotating historical week: (ISO_week_number % 4) + 2 weeks ago,
     deterministic — 4-position monthly rotation. Gates on
     backfill_complete=true in meta.

Acceptance test: someone reading this in six months can tell, without
looking at any other file, exactly what is being compared, over what
windows, and what a failure means. See doc string below each section.
"""
import os
import sys
import time
import datetime
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

logging.basicConfig(
    format="%(asctime)s [is_bot_canary] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "is_bot_canary"
WALKER_CADENCE_SECONDS = 86400  # daily

# Rows newer than this are excluded — the writer may not have stamped them yet.
# Writer cadence = 5min; we use 2× headroom.
IN_FLIGHT_SECS = 600


def _count_human_column(conn, ts_start, ts_end):
    """Count human rows via is_bot column: WHERE is_bot IS NOT TRUE."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM page_views "
            "WHERE is_bot IS NOT TRUE AND ts >= %s AND ts < %s",
            [int(ts_start), int(ts_end)],
        )
        return cur.fetchone()[0]


def _legacy_bot_pred():
    """Build the full legacy bot predicate using raw subqueries, independent
    of ANY module-level flag (_is_bot_column_ready, _bot_hash_table_ready,
    _burst_cohort_table_ready). Always includes all four classifier components:
    row_pred, session_pred (visitor + ip_day), scanner_pred, and cohort_pred.

    This is the permanent authoritative comparison arm for the canary — it
    must never be replaced by a table-subquery or column path, because the
    point is to verify the column against something truly independent.

    Returns (fragment, params) where fragment starts with AND NOT.
    """
    path_likes = " OR ".join("path LIKE %s" for _ in db.BOT_PATH_PATTERNS)
    ua_likes = " OR ".join(
        "COALESCE(user_agent, '') ILIKE %s" for _ in db.BOT_UA_PATTERNS
    )
    row_pred = f"(({path_likes}) OR ({ua_likes}))"
    session_pred = (
        f"(visitor_hash IS NOT NULL AND visitor_hash IN ("
        f"  SELECT visitor_hash FROM page_views "
        f"  WHERE visitor_hash IS NOT NULL AND {row_pred}"
        f")) OR (ip_day_hash IS NOT NULL AND ip_day_hash IN ("
        f"  SELECT ip_day_hash FROM page_views "
        f"  WHERE ip_day_hash IS NOT NULL AND {row_pred}"
        f"))"
    )
    scanner_pred = (
        "(user_agent IS NOT NULL AND (path, user_agent) IN ("
        "  SELECT path, user_agent FROM page_views "
        "  WHERE user_agent IS NOT NULL AND ts > %s "
        "  GROUP BY path, user_agent "
        "  HAVING COUNT(*) >= 30 "
        "     AND COUNT(*) <= COUNT(DISTINCT visitor_hash) * 1.10"
        "))"
    )
    # cohort_pred: always include if the table exists (checked by the canary
    # via try/except, not via the module flag which is Flask-app-only).
    cohort_pred = (
        "(country IS NOT NULL"
        " AND (country, path, date_trunc('day', to_timestamp(ts))::DATE)"
        "     IN (SELECT country, path, burst_day FROM burst_cohort_days))"
    )
    full_pred = (
        f"({row_pred} OR {session_pred} OR {scanner_pred} OR {cohort_pred})"
    )
    params = []
    params += list(db.BOT_PATH_PATTERNS) + list(db.BOT_UA_PATTERNS)  # outer row_pred
    params += list(db.BOT_PATH_PATTERNS) + list(db.BOT_UA_PATTERNS)  # session visitor
    params += list(db.BOT_PATH_PATTERNS) + list(db.BOT_UA_PATTERNS)  # session ip_day
    params.append(int(time.time()) - 7 * 86400)                       # scanner ts
    return f"AND NOT {full_pred}", params


def _count_human_predicate(conn, ts_start, ts_end):
    """Count human rows via the full legacy predicate — independent of all
    module-level flags. This is the permanent authoritative comparison arm.
    Never replace with a table-subquery path; that would defeat the canary."""
    bot_frag, bot_params = _legacy_bot_pred()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM page_views "
            f"WHERE ts >= %s AND ts < %s {bot_frag}",
            [int(ts_start), int(ts_end)] + bot_params,
        )
        return cur.fetchone()[0]


def _check_window(conn, label, ts_start, ts_end):
    """Compare column vs predicate count over [ts_start, ts_end).
    Returns (ok: bool, detail: str)."""
    col = _count_human_column(conn, ts_start, ts_end)
    pred = _count_human_predicate(conn, ts_start, ts_end)
    delta = col - pred
    start_dt = datetime.datetime.fromtimestamp(ts_start, datetime.timezone.utc).strftime("%Y-%m-%d")
    end_dt = datetime.datetime.fromtimestamp(ts_end, datetime.timezone.utc).strftime("%Y-%m-%d")
    if delta == 0:
        detail = f"{label} [{start_dt}→{end_dt}]: humans-column={col} humans-predicate={pred} delta=0 OK"
        log.info(detail)
        return True, detail
    else:
        # delta = column-humans − predicate-humans. Both count HUMANS.
        # delta > 0  → column finds MORE humans (writer under-stamps bots
        #              vs live classifier — check writer lag, cohort feed,
        #              or CLASSIFIER_VERSION bump not yet re-swept)
        # delta < 0  → column finds FEWER humans (writer over-stamps bots
        #              — likely bot_hashes cache retaining classifications
        #              the live subquery can no longer rederive, e.g. one-
        #              off probe UAs linked via visitor/ip_day session)
        if delta > 0:
            hint = "writer under-stamps: check backfill/cohort/CLASSIFIER_VERSION"
        else:
            hint = "writer over-stamps: check bot_hashes cache-vs-live drift"
        detail = (
            f"{label} [{start_dt}→{end_dt}]: humans-column={col} humans-predicate={pred} "
            f"delta={delta:+d} MISMATCH — {hint}"
        )
        log.error(detail)
        return False, detail


def run():
    if not db.pg_available():
        log.error("DATABASE_URL not configured — exiting")
        sys.exit(1)

    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok, messages = True, []

    try:
        meta = db.get_classification_meta()
        backfill_complete = meta.get("backfill_complete", "false") == "true"

        if not backfill_complete:
            msg = "backfill not yet complete — skipping historical-week window; trailing-7d only"
            log.info(msg)

        now = int(time.time())
        window_end = now - IN_FLIGHT_SECS  # exclude in-flight rows

        with db.rpc_loop_safe_pg_connect() as conn:
            # ── Window 1: trailing 7 days ──────────────────────────────────
            # This is the primary liveness check. Any drift in recent data
            # shows up here within one day of the drift occurring.
            w1_ok, w1_msg = _check_window(
                conn,
                label="trailing-7d",
                ts_start=now - 7 * 86400,
                ts_end=window_end,
            )
            if not w1_ok:
                ok = False
            messages.append(w1_msg)

            # ── Window 2: rotating historical week ─────────────────────────
            # Catches drift in pre-backfill or pre-burst_cohort history that
            # the trailing-7d window would miss. Deterministic rotation:
            # (ISO week number % 4) + 2 weeks ago → 4 positions over a month.
            # Gates on backfill_complete so the canary doesn't false-alarm
            # on unclassified historical rows.
            if backfill_complete:
                iso_week = datetime.date.today().isocalendar()[1]
                weeks_ago = (iso_week % 4) + 2  # 2, 3, 4, or 5 weeks ago
                ref_monday = (
                    datetime.date.today()
                    - datetime.timedelta(
                        days=datetime.date.today().weekday()
                        + 7 * weeks_ago
                    )
                )
                hist_start = int(
                    datetime.datetime.combine(ref_monday, datetime.time.min).timestamp()
                )
                hist_end = int(
                    datetime.datetime.combine(
                        ref_monday + datetime.timedelta(days=6),
                        datetime.time.max,
                    ).timestamp()
                )
                hist_end = min(hist_end, window_end)
                if hist_end > hist_start:
                    w2_ok, w2_msg = _check_window(
                        conn,
                        label=f"historical-week(-{weeks_ago}w)",
                        ts_start=hist_start,
                        ts_end=hist_end,
                    )
                    if not w2_ok:
                        ok = False
                    messages.append(w2_msg)
            else:
                messages.append("historical-week: skipped (backfill in progress)")

        full_msg = " | ".join(messages)
        if ok:
            log.info("canary PASS: %s", full_msg)
        else:
            log.error("canary FAIL: %s", full_msg)

    except Exception as e:
        ok = False
        full_msg = f"canary error: {e}"
        log.exception("is_bot_canary failed: %s", e)

    db.write_walker_health_end(WALKER_NAME, ok=ok, message=full_msg)


if __name__ == "__main__":
    run()
