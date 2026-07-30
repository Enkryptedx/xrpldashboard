"""Layer 2 (Answer Plausibility) walker — Phase 1 seed.

Reads live Neon and evaluates the highest-value trio from
docs/TRUTH_AUDIT_DESIGN.md against the metric inventory. Rules
evaluating metrics of type DAILY_WIGGLE / STEP_CHANGE operate on the
last finalized window (yesterday-UTC and earlier); today's partial row
is provisional by construction — see db.write_rlusd_supply_history,
which overwrites yesterday's row with fully-closed calendar totals each
cycle. Coverage handoff: same-day walker stoppage is R3/freshness's
jurisdiction (frozen row across cycles); the finalized-window rules'
job is finalized-day impossible-zeros / stuck-supply, delivered with
≤24h + one cycle delay (bound assumes walker on-cadence at rollover;
walker-down case is R3's).

  R1 — flat-when-should-wiggle:
       RLUSD xrpl_supply + eth_supply, evaluated over the last
       R1_WINDOW_DAYS finalized rows. If the window had ≥1 non-zero
       change event AND the trailing 7 finalized days are frozen (all
       values equal), fire. The founding 53-day RLUSD flat would have
       been caught at day 7 by this rule.

  R2 — zero-with-large-denominator:
       RLUSD xrpl_net_change_24h + eth_net_change_24h evaluated against
       the last finalized daily row. If that row's net-change is dust
       (|Δ| < $1) AND supply is significant (> $10M) AND ≥1 of the 3
       prior finalized days had real activity (|Δ| ≥ $1000), fire.

  R4 — monotonic-violated:
       Analytics all-time human views + uniques (from /admin/stats
       aggregation). Compares against the last stored watermark for the
       metric. A decrease is accepted only if it's within the delta of
       total is_bot=TRUE page_views since the last watermark — the
       ground-truth surface that every classifier (burst_cohort_days,
       page_view_bot_hashes, scanner_combos_confirmed, is_bot_writer)
       ultimately writes to. Otherwise fire.

Plus the machinery-stays-wired rider: any walker in walker_health
missing a walker_scope_declarations row is emitted as an
UNDECLARED_WALKER alarm — same shape as R1/R2/R4, so the alarm surface
stays uniform.

Success semantics: the walker's own walker_health.ok is TRUE whenever
the walker ran cleanly. Business alarms land in
`answer_plausibility_alarms` (append-only) — /health will render live
alarms from there. Only walker crashes or DB unreachability flip ok=false.

Cadence: 10 minutes. CONTINUOUS rules per the design; DAILY rules
(RLUSD daily-history) are cheap enough to re-evaluate at CONTINUOUS
cadence given how tiny the working set is (<30 rows).
"""

from __future__ import annotations

import datetime
import logging
import sys

import db


WALKER_NAME = "answer_plausibility_walker"
WALKER_CADENCE_SECONDS = 600  # 10 min — CONTINUOUS bar per design doc

# ── R1 thresholds ────────────────────────────────────────────────────────
# 30-day window with a 7-day frozen tail. If the metric changed at all in
# the last 30 days but has been identical for the last 7, alarm. Tuned to
# catch the RLUSD 53-day flat at day 7 rather than day 53.
R1_WINDOW_DAYS = 30
R1_FROZEN_TAIL_DAYS = 7

# ── R2 thresholds ────────────────────────────────────────────────────────
# Net-change dust floor: 1 RLUSD ≈ $1. Anything below that is rounding
# noise on values in the hundreds-of-millions.
R2_NET_CHANGE_DUST = 1
# Supply denominator: only fire when the metric is materially large.
R2_SUPPLY_MIN = 10_000_000
# Prior-N-days activity floor: at least one of the last 3 days had a
# real (>= $1,000) net-change.
R2_LOOKBACK_DAYS = 3
R2_LOOKBACK_ACTIVITY_FLOOR = 1000

# ── R4 tolerance ─────────────────────────────────────────────────────────
# Accepted decrease = (is_bot=TRUE page_view delta since last watermark).
# A drop within that budget is a logged classifier update (any surface —
# burst_cohort_days, page_view_bot_hashes, scanner_combos_confirmed,
# is_bot_writer), not a regression. Any drop beyond it fires.
# Pre-2026-07-30 the budget was scoped to burst_cohort_days only, so
# any classifier writing via a sibling surface would false-alarm — see
# db.count_is_bot_true_page_views for the founding case.
R4_TOLERANCE_ABS = 0

logger = logging.getLogger(WALKER_NAME)


def _emit(alarms, metric, rule, observed, expected_behavior,
          consecutive_cycles=None, last_change_at=None, note=None):
    alarms.append({
        "metric": metric,
        "rule": rule,
        "observed": observed,
        "expected_behavior": expected_behavior,
        "consecutive_cycles": consecutive_cycles,
        "last_change_at": last_change_at,
        "note": note,
    })
    db.write_plausibility_alarm(
        metric=metric,
        rule=rule,
        observed=observed,
        expected_behavior=expected_behavior,
        consecutive_cycles=consecutive_cycles,
        last_change_at=last_change_at,
        note=note,
    )
    logger.error(
        "PLAUSIBILITY ALARM metric=%s rule=%s observed=%s "
        "expected_behavior=%s consecutive_cycles=%s last_change=%s note=%s",
        metric, rule, observed, expected_behavior,
        consecutive_cycles, last_change_at, note,
    )


# ── R1 & R2: RLUSD ───────────────────────────────────────────────────────
def _evaluate_rlusd(alarms):
    # Fetch one extra row so we can drop the in-flight partial day and
    # still evaluate a full R1_WINDOW_DAYS-day finalized window.
    raw_history = db.read_rlusd_supply_history(days=R1_WINDOW_DAYS + 1)
    today_utc = datetime.datetime.now(datetime.timezone.utc).date()
    # Finalized-window filter: keep rows strictly before today-UTC. This
    # handles both "today's row present" (skip it) and "walker hasn't
    # written today yet" (nothing to skip) uniformly.
    history = [r for r in raw_history if r["snapshot_date"] < today_utc]
    if len(history) < R1_FROZEN_TAIL_DAYS + 1:
        logger.info(
            "rlusd finalized history too short for evaluation "
            "(rows=%d, need=%d)",
            len(history), R1_FROZEN_TAIL_DAYS + 1,
        )
        return

    for field, label in (
        ("xrpl_supply", "rlusd_xrpl_supply"),
        ("eth_supply", "rlusd_eth_supply"),
    ):
        values = [row[field] for row in history if row[field] is not None]
        if len(values) < R1_FROZEN_TAIL_DAYS + 1:
            continue
        tail = values[:R1_FROZEN_TAIL_DAYS]
        prior = values[R1_FROZEN_TAIL_DAYS:]
        # Frozen tail = all identical finalized values.
        tail_frozen = all(v == tail[0] for v in tail)
        # Prior window = any change observed.
        prior_changed = any(v != prior[0] for v in prior[1:])
        if tail_frozen and prior_changed:
            # Find the last date where the value differed from the tail
            # to stamp as "last_change_at".
            last_change_row = next(
                (row for row in history
                 if row[field] is not None and row[field] != tail[0]),
                None,
            )
            last_change = (
                last_change_row["snapshot_date"] if last_change_row else None
            )
            _emit(
                alarms,
                metric=label,
                rule="R1_flat_when_should_wiggle",
                observed=str(tail[0]),
                expected_behavior="STEP_CHANGE (~daily activity when supply active)",
                consecutive_cycles=R1_FROZEN_TAIL_DAYS,
                last_change_at=last_change,
                note=f"frozen {R1_FROZEN_TAIL_DAYS}d after prior change in {R1_WINDOW_DAYS}d window",
            )

    # R2 on net-change fields — evaluates yesterday-finalized (or the
    # most recent finalized row), never today's in-flight partial day.
    latest_finalized = history[0]
    prior_days = history[1:1 + R2_LOOKBACK_DAYS]

    # Companion supply lookup by field.
    for change_field, supply_field, label in (
        ("xrpl_net_change_24h", "xrpl_supply", "rlusd_xrpl_net_change_24h"),
        # eth net-change isn't stored in the current schema yet; skip
        # gracefully if absent so R2 can extend later without a rewrite.
    ):
        change = latest_finalized.get(change_field)
        supply = latest_finalized.get(supply_field)
        if change is None or supply is None:
            continue
        if abs(change) < R2_NET_CHANGE_DUST and supply > R2_SUPPLY_MIN:
            prior_active = any(
                r.get(change_field) is not None
                and abs(r[change_field]) >= R2_LOOKBACK_ACTIVITY_FLOOR
                for r in prior_days
            )
            if prior_active:
                _emit(
                    alarms,
                    metric=label,
                    rule="R2_zero_with_large_denominator",
                    observed=f"{float(change):.6f}",
                    expected_behavior=(
                        f"DAILY_WIGGLE (supply={float(supply):,.2f} > ${R2_SUPPLY_MIN:,})"
                    ),
                    consecutive_cycles=1,
                    last_change_at=latest_finalized["snapshot_date"],
                    note=(
                        f"prior {R2_LOOKBACK_DAYS}d had ≥1 day with "
                        f"|Δ| >= ${R2_LOOKBACK_ACTIVITY_FLOOR}"
                    ),
                )


# ── R4: analytics all-time counts ────────────────────────────────────────
def _evaluate_analytics(alarms):
    stats = db.read_page_view_stats(kind="human")
    all_time = stats.get("all_time") or {}
    obs_views = int(all_time.get("views") or 0)
    obs_uniques = int(all_time.get("uniques") or 0)

    # is_bot=TRUE snapshot: total page_view rows marked bot right now.
    # This is the ground-truth surface — every classifier (burst_cohort,
    # bot_hashes, scanner_combos, is_bot_writer) mutates page_views.is_bot,
    # so its delta since the last watermark is the correct accepted-drop
    # budget. See db.count_is_bot_true_page_views for the founding case.
    is_bot_now = db.count_is_bot_true_page_views()

    for metric_name, obs_value in (
        ("analytics_alltime_human_views", obs_views),
        ("analytics_alltime_human_uniques", obs_uniques),
    ):
        prev_value, prev_seen, extra = db.read_plausibility_watermark(metric_name)
        # Continuity: pre-fix watermarks stored `cohort_reclassified`.
        # Fall back to that key for one cycle so the first post-fix run
        # doesn't false-alarm; subsequent runs read `is_bot_true_count`.
        prev_is_bot = (
            int((extra or {}).get("is_bot_true_count"))
            if extra and "is_bot_true_count" in extra
            else int((extra or {}).get("cohort_reclassified", 0))
            if extra else 0
        )
        is_bot_delta = is_bot_now - prev_is_bot

        if prev_value is not None:
            prev_int = int(prev_value)
            delta = obs_value - prev_int
            # Accepted decrease budget: any drop covered by an increase
            # in total is_bot=TRUE page_views since last watermark.
            accepted_drop = max(is_bot_delta, 0)
            if delta < -accepted_drop - R4_TOLERANCE_ABS:
                _emit(
                    alarms,
                    metric=metric_name,
                    rule="R4_monotonic_violated",
                    observed=str(obs_value),
                    expected_behavior="MONOTONIC_GROW",
                    consecutive_cycles=1,
                    last_change_at=prev_seen,
                    note=(
                        f"decreased by {abs(delta)} (prev={prev_int}); "
                        f"is_bot=TRUE delta since watermark = "
                        f"{is_bot_delta} (accepted drop budget)"
                    ),
                )

        # Watermark advances every run — capture whichever value came out
        # of this evaluation. Persisting the is_bot=TRUE snapshot too so
        # the accepted-drop budget accumulates correctly across restarts.
        db.write_plausibility_watermark(
            metric=metric_name,
            value=obs_value,
            extra={"is_bot_true_count": is_bot_now},
        )


# ── R5: cross_check persistent disagreement ──────────────────────────────
def _evaluate_cross_check_disagreements(alarms):
    """Fire when a cross_check pair disagrees on 3+ consecutive cycles.

    Q1 FALSE-GREEN: empty rows → returns without emitting (correct; no
    disagreement to report). External unreachable rows are status=
    'external_unreachable', not 'disagree' — they don't count toward
    the streak, so a 3× external-outage won't false-alarm.
    Q2 GROUND-TRUTH: SELECT pair_key, status FROM cross_check_results
      ORDER BY run_at DESC LIMIT 18; any pair with 3 consecutive 'disagree'
      rows should match alarms here.
    Q3 WHO-WATCHES: answer_plausibility_walker row in walker_health.
    """
    from collections import defaultdict
    rows = db.read_recent_cross_check_results(hours=1)  # ~6 cycles per pair
    if not rows:
        return
    by_pair: dict = defaultdict(list)
    for r in rows:  # newest first per db.read_recent_cross_check_results
        by_pair[r["pair_key"]].append(r)
    for pair_key, pair_rows in sorted(by_pair.items()):
        recent = pair_rows[:3]  # last 3 cycles
        if len(recent) < 3:
            continue
        if all(r["status"] == "disagree" for r in recent):
            latest = recent[0]
            _emit(
                alarms,
                metric=f"cross_check[{pair_key}]",
                rule="R5_CC_DISAGREE",
                observed=str(latest.get("delta")),
                expected_behavior="agree or external_unreachable",
                consecutive_cycles=len(recent),
                last_change_at=None,
                note=(
                    f"local={latest['local_value']} "
                    f"ext={latest['external_value']} "
                    f"src={latest['external_source']}"
                ),
            )


# ── UNDECLARED_WALKER: machinery-stays-wired ─────────────────────────────
def _evaluate_undeclared_walkers(alarms):
    state = db.read_coverage_register_state()
    if state is None:
        return
    wh = state.get("walker_health") or {}
    for walker_name, info in sorted(wh.items()):
        if info.get("undeclared"):
            _emit(
                alarms,
                metric=f"walker_scope[{walker_name}]",
                rule="UNDECLARED_WALKER",
                observed="missing",
                expected_behavior="has walker_scope_declarations row",
                consecutive_cycles=None,
                last_change_at=None,
                note="new walker without scope declaration — add to seed_walker_scope_declarations.py",
            )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start(
        WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS
    )
    ok = False
    message = None
    alarms = []
    try:
        _evaluate_rlusd(alarms)
        _evaluate_analytics(alarms)
        _evaluate_cross_check_disagreements(alarms)
        _evaluate_undeclared_walkers(alarms)
        rule_counts = {}
        for a in alarms:
            rule_counts[a["rule"]] = rule_counts.get(a["rule"], 0) + 1
        summary = ", ".join(
            f"{k}={v}" for k, v in sorted(rule_counts.items())
        ) or "none"
        message = f"alarms={len(alarms)} ({summary})"
        ok = True
        logger.info(message)
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end(WALKER_NAME, ok=ok, message=message)
    sys.exit(0)


if __name__ == "__main__":
    main()
