#!/usr/bin/env python3
"""is_bot column writer — forward classification + backfill.

Scope: reads page_views; writes page_views.is_bot; reads/writes
page_view_classification_meta, page_view_bot_hashes,
page_view_scanner_combos, burst_cohort_days. Writes walker_health.

Runs every 5 minutes via launchd. Idempotent — multiple runs produce
the same result. A lockfile (via flock in the wrapper script) prevents
concurrent instances.

Classification logic mirrors _bot_filter_sql exactly when
_bot_hash_table_ready = True (the table-subquery path). The #2 tables
(page_view_bot_hashes, page_view_scanner_combos) are refreshed at the
start of each run so the UPDATE uses fresh session-spread data.

Reconciliation triggers checked on every run:
  1. BOT_CLASSIFIER_VERSION mismatch → full table resync (bidirectional)
  2. burst_cohort_days max(classified_at) advance → date-range TRUE-only re-stamp
  3. page_view_scanner_combos_confirmed max(confirmed_at) advance →
     combo-targeted TRUE-only re-stamp
  4. page_view_bot_hashes content-hash advance → hash-targeted TRUE-only
     re-stamp. Expected to fire most cycles due to ip_day_hash day-scoping
     — idempotent, measured (~30ms/fire). See
     docs/IS_BOT_RECONCILIATION_SURFACES.md.

Backfill: processes one day's oldest unclassified rows per run (rows
with is_bot IS NULL and ts < forward-pass watermark). Once all
historical rows are stamped, sets backfill_complete=true in meta.
"""
import os
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

logging.basicConfig(
    format="%(asctime)s [is_bot_writer] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "is_bot_writer"
WALKER_CADENCE_SECONDS = 300  # 5 minutes

# Backfill: one day of history per writer run, oldest first. At ~200-500
# rows/day and one day per run, the historical backfill completes in ~100-200
# launchd cycles (~8-17 hours). Heartbeat after each day keeps it visible.
BACKFILL_DAYS_PER_RUN = 1
BACKFILL_CHUNK_ROWS = 1000
BACKFILL_SLEEP_SECS = 1.0  # polite pause between chunks

# Forward-pass overlap: re-process rows up to 10 minutes behind the watermark
# to catch session-spread edge cases (a new bot row classifying an earlier
# row in the same session).
FORWARD_OVERLAP_SECS = 600


def _bot_pred_sql():
    """Return (pred_sql, params) — the full bot detection predicate.

    Single source of truth for classification. Wrapped two ways:
      - _bot_update_sql: CASE WHEN pred THEN TRUE ELSE NULL END for the
        forward pass (bidirectional; can NULL a stale TRUE).
      - _restamp_true_only: WHERE pred AND is_bot IS NOT TRUE for the
        cohort / scanner-combo re-stamp triggers (TRUE-only, safe over
        historical windows — honors the LANDMINE WARNING in run()).

    Table-subquery path (page_view_bot_hashes + scanner_combos_confirmed)
    keeps classification O(1) per row — no full-table subquery. row_pred
    is included explicitly for NULL-visitor_hash rows (pre-rollout era)
    that are not reachable via the hash tables. Caller must have already
    called refresh_bot_hash_tables() so the tables are current.
    """
    path_likes = " OR ".join(f"p.path LIKE %s" for _ in db.BOT_PATH_PATTERNS)
    ua_likes = " OR ".join(
        f"COALESCE(p.user_agent,'') ILIKE %s" for _ in db.BOT_UA_PATTERNS
    )
    row_pred = f"(({path_likes}) OR ({ua_likes}))"
    params = list(db.BOT_PATH_PATTERNS) + list(db.BOT_UA_PATTERNS)

    parts = [
        row_pred,
        "(p.visitor_hash IS NOT NULL AND p.visitor_hash IN ("
        "  SELECT hash FROM page_view_bot_hashes WHERE hash_type = 'visitor'"
        "))",
        "(p.ip_day_hash IS NOT NULL AND p.ip_day_hash IN ("
        "  SELECT hash FROM page_view_bot_hashes WHERE hash_type = 'ip_day'"
        "))",
        # Scanner arm reads the persistent confirmed ledger, NOT the trailing
        # 7-day snapshot in page_view_scanner_combos. This closes the amnesia
        # gap that caused the 2026-07-26 CheckHost residual (canary delta=-45
        # on rows 2026-07-19→2026-07-23): once a combo is confirmed, the
        # verdict persists after the burst decays. See
        # docs/IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md §1, §4b.
        "(p.user_agent IS NOT NULL AND (p.path, p.user_agent) IN ("
        "  SELECT path, user_agent FROM page_view_scanner_combos_confirmed"
        "))",
    ]
    # Burst-cohort predicate — only if the table exists and has rows
    parts.append(
        "(p.country IS NOT NULL AND EXISTS ("
        "  SELECT 1 FROM burst_cohort_days b"
        "  WHERE b.country = p.country AND b.path = p.path"
        "    AND b.burst_day = date_trunc('day', to_timestamp(p.ts))::DATE"
        "))"
    )
    return "(" + " OR ".join(parts) + ")", params


def _bot_update_sql():
    """Return (case_expr, params) — CASE WHEN pred THEN TRUE ELSE NULL END.
    Forward-pass classifier that can BOTH stamp TRUE and clear a stale TRUE.
    Only safe over narrow forward windows; see LANDMINE WARNING in run().
    """
    pred, params = _bot_pred_sql()
    return f"CASE WHEN {pred} THEN TRUE ELSE NULL END", params


def _classify_window(conn, ts_start, ts_end, label="window"):
    """UPDATE is_bot for all rows in [ts_start, ts_end] — bidirectional
    (both TRUE and NULL). See LANDMINE WARNING in run(): only the forward
    pass and full-resync should widen this over historical rows."""
    case_expr, params = _bot_update_sql()
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE page_views p SET is_bot = {case_expr} "
            f"WHERE p.ts >= %s AND p.ts <= %s",
            params + [int(ts_start), int(ts_end)],
        )
        count = cur.rowcount
    conn.commit()
    log.info("classified %s: ts=[%d,%d] rows=%d", label, ts_start, ts_end, count)
    return count


def _restamp_true_only(conn, ts_start, ts_end, label="window"):
    """Forward-only, TRUE-only re-stamp over [ts_start, ts_end].

    Sets is_bot=TRUE only where predicate matches AND row is currently
    NULL/FALSE. Existing TRUE stamps are never touched — safe over
    historical windows. This is the correct shape for cohort/scanner
    trigger events, which reveal NEW bot rows without invalidating old
    verdicts (the ELSE NULL branch would silently erase TRUE stamps
    whose original scanner combo has since been evicted — see LANDMINE
    WARNING in run() and docs/IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md §3).
    """
    pred, params = _bot_pred_sql()
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE page_views p SET is_bot = TRUE "
            f"WHERE p.ts >= %s AND p.ts <= %s "
            f"AND (p.is_bot IS NULL OR p.is_bot = FALSE) "
            f"AND {pred}",
            [int(ts_start), int(ts_end)] + params,
        )
        count = cur.rowcount
    conn.commit()
    log.info(
        "re-stamped %s (TRUE-only): ts=[%d,%d] rows=%d",
        label, ts_start, ts_end, count,
    )
    return count


def run():
    if not db.pg_available():
        log.error("DATABASE_URL not configured — exiting")
        sys.exit(1)

    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok, msg = False, "unknown error"

    try:
        # ── 1. Ensure schema ───────────────────────────────────────────────
        log.info("ensuring schema")
        db.ensure_is_bot_schema()

        # ── 2. Refresh #2 tables so classification uses fresh session data ──
        log.info("refreshing bot hash tables")
        db.refresh_bot_hash_tables()

        # ── 3. Read meta ───────────────────────────────────────────────────
        meta = db.get_classification_meta()
        last_ts = int(meta.get("last_classified_ts", "0"))
        last_cv = int(meta.get("last_classifier_version", "0"))
        last_cohort_v = meta.get("last_cohort_version", "")
        last_scanner_v = meta.get("last_scanner_version", "")
        last_bot_hashes_v = meta.get("last_bot_hashes_version", "")
        backfill_complete = meta.get("backfill_complete", "false") == "true"

        now = int(time.time())

        with db.rpc_loop_safe_pg_connect() as conn:
            # ── 4. Check CLASSIFIER_VERSION ────────────────────────────────
            if last_cv != db.BOT_CLASSIFIER_VERSION:
                log.info(
                    "classifier version changed %d→%d — full resync",
                    last_cv,
                    db.BOT_CLASSIFIER_VERSION,
                )
                with conn.cursor() as cur:
                    cur.execute("SELECT MIN(ts), MAX(ts) FROM page_views")
                    oldest_ts, newest_ts = cur.fetchone()
                if oldest_ts:
                    _classify_window(conn, oldest_ts, newest_ts, label="full-resync")
                db.set_classification_meta({
                    "last_classifier_version": db.BOT_CLASSIFIER_VERSION,
                    "last_classified_ts": now,
                    "backfill_complete": "true",
                })
                db.write_heartbeat(WALKER_NAME)
                ok, msg = True, f"full resync on version bump to {db.BOT_CLASSIFIER_VERSION}"
                return

            # ── 5. Check burst_cohort_days advance ─────────────────────────
            # Was try/except-pass over SELECT MAX(created_at) — but the column
            # is `classified_at`. The typo raised UndefinedColumn, the except
            # swallowed it, current_cohort_v defaulted to "" ≠ last_cohort_v="",
            # so the branch below never fired. Historical rows for the 20
            # cohort-days added 2026-07-28 08:09 EDT (covering 05-12→07-21)
            # were never re-classified — that's the +25 canary FAIL that
            # opened this arc. Same silent-swallow class as fleet sweep
            # 7f87e72 (both members were db.write_rlusd_supply_history);
            # named twice in one day. A dead trigger must fail loud via
            # walker_health, not default-and-carry-on.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(classified_at)::TEXT FROM burst_cohort_days"
                )
                row = cur.fetchone()
                current_cohort_v = (row[0] or "") if row else ""

            if current_cohort_v and current_cohort_v != last_cohort_v:
                log.info(
                    "burst_cohort_days advanced (%s→%s) — TRUE-only re-stamp of affected dates",
                    last_cohort_v,
                    current_cohort_v,
                )
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT MIN(burst_day), MAX(burst_day) FROM burst_cohort_days"
                    )
                    row = cur.fetchone()
                if row and row[0]:
                    import datetime
                    cohort_start = int(
                        datetime.datetime.combine(
                            row[0], datetime.time.min
                        ).timestamp()
                    )
                    cohort_end = int(
                        datetime.datetime.combine(
                            row[1], datetime.time.max
                        ).timestamp()
                    )
                    _restamp_true_only(
                        conn, cohort_start, cohort_end, label="cohort-resync"
                    )
                db.set_classification_meta({"last_cohort_version": current_cohort_v})

            # ── 5b. Check page_view_scanner_combos_confirmed advance ───────
            # Sibling trigger to cohort-resync. The auto-ratchet block in
            # refresh_bot_hash_tables() inserts confirmed combos earlier
            # this same cycle (db.py:4875-4903). Without a trigger here,
            # historical rows matching a newly-confirmed (path, ua) would
            # stay unstamped until the next BOT_CLASSIFIER_VERSION bump —
            # exactly the reconciliation gap the Sunday diagnostic named.
            # TRUE-only + combo-targeted: no time window needed because a
            # combo's (path, ua) identity means every matching historical
            # row should be TRUE. Already-TRUE rows are skipped (idempotent).
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(confirmed_at)::TEXT "
                    "FROM page_view_scanner_combos_confirmed"
                )
                row = cur.fetchone()
                current_scanner_v = (row[0] or "") if row else ""

            if current_scanner_v and current_scanner_v != last_scanner_v:
                log.info(
                    "scanner_combos_confirmed advanced (%s→%s) — TRUE-only combo re-stamp",
                    last_scanner_v,
                    current_scanner_v,
                )
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE page_views p SET is_bot = TRUE "
                        "WHERE p.user_agent IS NOT NULL "
                        "AND (p.path, p.user_agent) IN ("
                        "  SELECT path, user_agent "
                        "  FROM page_view_scanner_combos_confirmed"
                        ") "
                        "AND (p.is_bot IS NULL OR p.is_bot = FALSE)"
                    )
                    count = cur.rowcount
                conn.commit()
                log.info("scanner-resync (TRUE-only): rows=%d", count)
                db.set_classification_meta({"last_scanner_version": current_scanner_v})

            # ── 5c. Check page_view_bot_hashes content advance ─────────────
            # Third sibling in the reconciliation-surface class (see
            # docs/IS_BOT_RECONCILIATION_SURFACES.md §2 for the law).
            # refresh_bot_hash_tables() at step 2 TRUNCATE+repopulates this
            # table every cycle from trailing session data. When a new
            # (hash_type, hash) lands, historical page_views rows matching
            # that hash would otherwise not be re-classified until the next
            # BOT_CLASSIFIER_VERSION bump — hence the 07-28 +25 residue
            # that eb6c2f1 cleared instance-wise. TRUE-only + hash-targeted:
            # no time window (hashes are combo-identity like scanner combos).
            # Already-TRUE rows skipped (idempotent).
            #
            # EXPECTED TO FIRE MOST CYCLES. ip_day_hash entries are
            # day-scoped (a hash is unique per ip × day), so a new UTC day
            # creates fresh hashes and old ones age out of the trailing
            # session window; the content-hash therefore changes nearly
            # every cycle. The UPDATE is TRUE-only + idempotent so
            # correctness holds regardless of fire frequency. Duration is
            # logged on every fire so week-one cost stays visible. Prod
            # EXPLAIN ANALYZE (2026-07-28): ~30ms/fire against 116k
            # page_views / 13k bot_hashes. Escape hatch if measurement
            # shows real cost: additions-only versioning via set-difference
            # against prior hash set (deferred; not built). See §4d of the
            # doc.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MD5(string_agg(hash_type || ':' || hash, ',' "
                    "                      ORDER BY hash_type, hash)) "
                    "FROM page_view_bot_hashes"
                )
                row = cur.fetchone()
                current_bot_hashes_v = (row[0] or "") if row else ""

            if current_bot_hashes_v and current_bot_hashes_v != last_bot_hashes_v:
                _fire_start = time.monotonic()
                log.info(
                    "page_view_bot_hashes advanced (%s→%s) — TRUE-only hash re-stamp",
                    (last_bot_hashes_v or "<empty>")[:12],
                    current_bot_hashes_v[:12],
                )
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE page_views p SET is_bot = TRUE "
                        "WHERE ("
                        "  (p.visitor_hash IS NOT NULL AND p.visitor_hash IN ("
                        "    SELECT hash FROM page_view_bot_hashes "
                        "    WHERE hash_type = 'visitor'))"
                        "  OR (p.ip_day_hash IS NOT NULL AND p.ip_day_hash IN ("
                        "    SELECT hash FROM page_view_bot_hashes "
                        "    WHERE hash_type = 'ip_day'))"
                        ") "
                        "AND (p.is_bot IS NULL OR p.is_bot = FALSE)"
                    )
                    count = cur.rowcount
                conn.commit()
                _fire_ms = int((time.monotonic() - _fire_start) * 1000)
                log.info(
                    "bot_hashes-resync (TRUE-only): rows=%d duration_ms=%d",
                    count, _fire_ms,
                )
                db.set_classification_meta(
                    {"last_bot_hashes_version": current_bot_hashes_v}
                )

            # ── 6. Forward incremental pass ────────────────────────────────
            # LANDMINE WARNING (do not widen this window without the confirmed
            # ledger doing the work): _bot_update_sql produces
            # CASE WHEN pred THEN TRUE ELSE NULL END. The ELSE NULL branch is
            # correct for the ~10-minute forward window because the classifier
            # snapshot barely moves. Widening this window to cover historical
            # rows activates the ELSE NULL path against rows whose original
            # TRUE stamp came from a since-evicted scanner combo — the writer
            # would silently ERASE those correct verdicts, both arms would
            # agree they're human, and the canary would go green on destroyed
            # history rather than fixed classifier. See
            # docs/IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md §3.
            window_start = max(0, last_ts - FORWARD_OVERLAP_SECS)
            _classify_window(conn, window_start, now, label="forward-pass")
            db.set_classification_meta({"last_classified_ts": now})

            # ── 7. Backfill step — one day of oldest unclassified rows ─────
            if not backfill_complete:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT MIN(ts) FROM page_views WHERE is_bot IS NULL"
                    )
                    row = cur.fetchone()
                    oldest_null_ts = row[0] if row else None

                if oldest_null_ts is None or oldest_null_ts >= now - FORWARD_OVERLAP_SECS:
                    log.info("backfill complete — no more unclassified rows")
                    db.set_classification_meta({"backfill_complete": "true"})
                    backfill_complete = True
                else:
                    import datetime
                    day_dt = datetime.date.fromtimestamp(oldest_null_ts)
                    day_start = int(
                        datetime.datetime.combine(day_dt, datetime.time.min).timestamp()
                    )
                    day_end = int(
                        datetime.datetime.combine(day_dt, datetime.time.max).timestamp()
                    )
                    log.info("backfill: processing day %s", day_dt)
                    _classify_window(conn, day_start, day_end, label=f"backfill-{day_dt}")
                    db.write_heartbeat(WALKER_NAME)  # heartbeat per day-chunk

                    # Check if done after this chunk
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT COUNT(*) FROM page_views "
                            "WHERE is_bot IS NULL AND ts < %s",
                            [now - FORWARD_OVERLAP_SECS],
                        )
                        remaining = cur.fetchone()[0]
                    if remaining == 0:
                        db.set_classification_meta({"backfill_complete": "true"})
                        log.info("backfill complete after this chunk")

        db.write_heartbeat(WALKER_NAME)
        ok = True
        msg = (
            f"forward pass ok; backfill={'complete' if backfill_complete else 'in progress'}"
        )

    except Exception as e:
        msg = str(e)
        log.exception("is_bot_writer failed: %s", e)
    finally:
        db.write_walker_health_end(WALKER_NAME, ok=ok, message=msg)


if __name__ == "__main__":
    run()
