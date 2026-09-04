"""Daily walker: compute + upsert authoritative all-time site totals.

Single source of truth for "how many countries" style questions. Reads
page_views, upserts one row into site_totals (id=1), and mirrors the same
payload to docs/SITE_TOTALS.json for grep-friendly consumption.

Idempotent: running twice in a row produces the same result (except for
computed_at). Safe to re-kick.

Fields tracked (all all-time, not windowed):
- total_hits           — COUNT(*) FROM page_views
- human_hits           — COUNT(*) FILTER (is_bot IS NOT TRUE)
- bot_hits             — COUNT(*) FILTER (is_bot IS TRUE)
- countries_all              — DISTINCT country IS NOT NULL (includes T1)
- countries_human            — DISTINCT country IS NOT NULL AND is_bot IS NOT TRUE
- countries_all_excluding_t1 — same as countries_all minus the T1 Tor placeholder
- countries_human_excluding_t1 — human-only minus T1

Both T1-inclusive and T1-excluding variants are stored so future ruling on
which is "official" doesn't force a schema change or recompute.

Data-integrity guard: if ANY count decreases vs the prior stored row, it's
recorded as a finding (walker_health.findings_count > 0). An append-only
page_views table should never see a count go down; a decrease signals data
loss, wrongful DELETE, or classification-rule change. That's worth paging on.

Cadence: daily (StartInterval=86400). Once-per-day brief query — compute-wake
cost is negligible even with the 5-min autosuspend that shipped 2026-09-01.

Uses DATABASE_URL_DIRECT when present (walker convention post-2026-08-31
pooler-bypass hotfix, commit 1631426); falls back to pooled DATABASE_URL.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import db

WALKER_NAME = "site_totals_walker"
WALKER_CADENCE_SECONDS = 86400  # daily

DOCS_JSON_PATH = Path("/Users/charliebruce/xrpl_test/docs/SITE_TOTALS.json")

# When state-level (MaxMind GeoLite2) capture went live in production.
# Commit a092bbb, 2026-09-01 16:51:04 -0400 → 20:51:04 UTC. Rounded down
# to 20:50 for a conservative inclusive window. Any us_states_* count is
# strictly since-deploy — nothing came before because region_code wasn't
# being written on Render-side traffic. Stored on the tally so future
# readers know the state clock started later than the country clock.
US_STATE_TRACKING_SINCE_EPOCH = 1788295800  # 2026-09-01 20:50:00 UTC

_SCHEMA_ENSURE = """
CREATE TABLE IF NOT EXISTS site_totals (
    id           INT PRIMARY KEY DEFAULT 1,
    computed_at  BIGINT NOT NULL,
    total_hits   BIGINT NOT NULL,
    human_hits   BIGINT NOT NULL,
    bot_hits     BIGINT NOT NULL,
    countries_all       INT NOT NULL,
    countries_human     INT NOT NULL,
    countries_null      INT NOT NULL,
    countries_placeholder_t1 INT NOT NULL,
    CHECK (id = 1)
);

-- Append-only companion to site_totals (2026-09-03).
--
-- The singleton `site_totals` answers "what are the current totals?" but
-- can't answer "how much did they grow?" without a manual diff against
-- prior page_view queries. This history table lets any "growth over N
-- days" question resolve to a single query without recomputing over
-- page_views' full history.
--
-- Backfill deferred by ruling — we start from tonight and let the
-- daily walker accrete. The singleton row keeps working for
-- "what's the current number" callers unchanged.
--
-- Same fields as site_totals except id/CHECK (which are singleton
-- discipline, meaningless in the history table). computed_at is
-- the natural primary key — two rows can't share a wall-clock
-- second (walker is daily), and if a re-run collides ON CONFLICT
-- DO NOTHING preserves the earlier row.
CREATE TABLE IF NOT EXISTS site_totals_history (
    computed_at   BIGINT PRIMARY KEY,
    total_hits    BIGINT NOT NULL,
    human_hits    BIGINT NOT NULL,
    bot_hits      BIGINT NOT NULL,
    countries_all       INT NOT NULL,
    countries_human     INT NOT NULL,
    countries_null      INT NOT NULL,
    countries_placeholder_t1 INT NOT NULL,
    countries_all_excluding_t1   INT NOT NULL,
    countries_human_excluding_t1 INT NOT NULL,
    us_states_all       INT NOT NULL,
    us_states_human     INT NOT NULL,
    us_state_tracking_since_epoch BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS site_totals_history_computed_at_idx
    ON site_totals_history (computed_at DESC);
"""

_SCHEMA_ADD_COLS = [
    "ALTER TABLE site_totals ADD COLUMN IF NOT EXISTS countries_all_excluding_t1 INT",
    "ALTER TABLE site_totals ADD COLUMN IF NOT EXISTS countries_human_excluding_t1 INT",
    "ALTER TABLE site_totals ADD COLUMN IF NOT EXISTS us_states_all INT",
    "ALTER TABLE site_totals ADD COLUMN IF NOT EXISTS us_states_human INT",
    "ALTER TABLE site_totals ADD COLUMN IF NOT EXISTS us_state_tracking_since_epoch BIGINT",
    # Worldwide regions (2026-09-03) — distinct region_code values
    # covering every country's subdivisions, plus a count of how
    # many countries have at least one region observed. Same clock
    # start as us_states_* (region_code capture went live in commit
    # a092bbb, 2026-09-01 20:51 UTC). See US_STATE_TRACKING_SINCE_EPOCH.
    #
    # regions_all counts every non-null region_code — includes US-VA,
    # CA-QC, ZA-KZN, etc. regions_countries_covered counts distinct
    # country values on rows where region_code IS NOT NULL.
    "ALTER TABLE site_totals ADD COLUMN IF NOT EXISTS regions_all INT",
    "ALTER TABLE site_totals ADD COLUMN IF NOT EXISTS regions_human INT",
    "ALTER TABLE site_totals ADD COLUMN IF NOT EXISTS regions_countries_covered INT",
    # Mirror on the history table so backfilled-forward growth queries
    # see the new fields.
    "ALTER TABLE site_totals_history ADD COLUMN IF NOT EXISTS regions_all INT",
    "ALTER TABLE site_totals_history ADD COLUMN IF NOT EXISTS regions_human INT",
    "ALTER TABLE site_totals_history ADD COLUMN IF NOT EXISTS regions_countries_covered INT",
]

_COMPUTE_SQL = """
SELECT
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE is_bot IS NOT TRUE) AS human,
  COUNT(*) FILTER (WHERE is_bot IS TRUE) AS bot,
  COUNT(DISTINCT country) FILTER (WHERE country IS NOT NULL) AS c_all,
  COUNT(DISTINCT country) FILTER (WHERE country IS NOT NULL AND is_bot IS NOT TRUE) AS c_hum,
  COUNT(*) FILTER (WHERE country IS NULL) AS c_null,
  COUNT(DISTINCT country) FILTER (WHERE country = 'T1') AS c_t1_placeholder,
  COUNT(DISTINCT country) FILTER (WHERE country IS NOT NULL AND country <> 'T1') AS c_all_no_t1,
  COUNT(DISTINCT country) FILTER (WHERE country IS NOT NULL AND country <> 'T1' AND is_bot IS NOT TRUE) AS c_hum_no_t1,
  COUNT(DISTINCT region_code) FILTER (WHERE region_code LIKE 'US-%%') AS us_states_all,
  COUNT(DISTINCT region_code) FILTER (WHERE region_code LIKE 'US-%%' AND is_bot IS NOT TRUE) AS us_states_human,
  COUNT(DISTINCT region_code) FILTER (WHERE region_code IS NOT NULL) AS regions_all,
  COUNT(DISTINCT region_code) FILTER (WHERE region_code IS NOT NULL AND is_bot IS NOT TRUE) AS regions_human,
  COUNT(DISTINCT country) FILTER (WHERE region_code IS NOT NULL) AS regions_countries_covered
FROM page_views
"""
# us_states_* counts are naturally since-deploy: region_code wasn't written
# before commit a092bbb (2026-09-01 20:51 UTC), so any pre-deploy rows have
# region_code IS NULL and are excluded by the LIKE filter. No explicit time
# gate needed. US_STATE_TRACKING_SINCE_EPOCH is stored on the row for future
# readers to know when the clock started.

_UPSERT_SQL = """
INSERT INTO site_totals (
    id, computed_at, total_hits, human_hits, bot_hits,
    countries_all, countries_human, countries_null, countries_placeholder_t1,
    countries_all_excluding_t1, countries_human_excluding_t1,
    us_states_all, us_states_human, us_state_tracking_since_epoch,
    regions_all, regions_human, regions_countries_covered
) VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO UPDATE SET
    computed_at = EXCLUDED.computed_at,
    total_hits = EXCLUDED.total_hits,
    human_hits = EXCLUDED.human_hits,
    bot_hits = EXCLUDED.bot_hits,
    countries_all = EXCLUDED.countries_all,
    countries_human = EXCLUDED.countries_human,
    countries_null = EXCLUDED.countries_null,
    countries_placeholder_t1 = EXCLUDED.countries_placeholder_t1,
    countries_all_excluding_t1 = EXCLUDED.countries_all_excluding_t1,
    countries_human_excluding_t1 = EXCLUDED.countries_human_excluding_t1,
    us_states_all = EXCLUDED.us_states_all,
    us_states_human = EXCLUDED.us_states_human,
    us_state_tracking_since_epoch = EXCLUDED.us_state_tracking_since_epoch,
    regions_all = EXCLUDED.regions_all,
    regions_human = EXCLUDED.regions_human,
    regions_countries_covered = EXCLUDED.regions_countries_covered
"""

_PRIOR_SQL = """
SELECT total_hits, countries_all, countries_all_excluding_t1, us_states_all,
       regions_all, regions_countries_covered
FROM site_totals WHERE id = 1
"""
# Column order must match _MONOTONIC_FIELDS positionally.

# Fields to check for monotonic-non-decreasing on append-only data.
#
# Only fields derived from purely append-only sources belong here:
# - total_hits: COUNT(*) of an append-only table
# - countries_all / countries_all_excluding_t1: DISTINCT set-membership
#   over append-only data (a country either has appeared, or hasn't; it
#   never "un-appears")
# - us_states_all: same DISTINCT set-membership property on region_code.
#   State-tracking only started 2026-09-01 20:51 UTC (commit a092bbb),
#   so the clock is younger than country tracking — see
#   US_STATE_TRACKING_SINCE_EPOCH — but the monotonic property is the
#   same from that moment on.
#
# INTENTIONALLY EXCLUDED:
# - human_hits, bot_hits, countries_human, countries_human_excluding_t1,
#   us_states_human: these all depend on is_bot classification which is
#   RETROACTIVE. When the is_bot_writer walker reruns and reclassifies
#   rows human↔bot, these counts rebalance. First observed 2026-09-01:
#   human_hits went 53,454 → 53,451 across two runs 20 min apart, from a
#   classifier rerun. Normal is_bot behavior, not a data-integrity finding.
_MONOTONIC_FIELDS = (
    "total_hits",
    "countries_all",
    "countries_all_excluding_t1",
    "us_states_all",
    # Worldwide regions (2026-09-03). Same set-membership property
    # as countries_all: a region either has appeared or hasn't. Neither
    # can legitimately un-appear on an append-only page_views table.
    "regions_all",
    "regions_countries_covered",
)


def _detect_decreases(prior, new):
    """Return list of "field: prior→new" strings for any field that decreased.

    prior is the tuple from _PRIOR_SQL (or None if first-ever run).
    new is a dict keyed by _MONOTONIC_FIELDS.
    """
    if prior is None:
        return []
    prior_dict = dict(zip(_MONOTONIC_FIELDS, prior))
    decreases = []
    for k in _MONOTONIC_FIELDS:
        p, n = prior_dict.get(k), new.get(k)
        if p is None or n is None:
            continue
        if n < p:
            decreases.append(f"{k}: {p:,} → {n:,}")
    return decreases


def _write_docs_json(payload):
    """Mirror the tally to docs/SITE_TOTALS.json for grep-friendly consumption.

    Best-effort — a docs write failure is a finding, not a walker failure.
    """
    DOCS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DOCS_JSON_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(DOCS_JSON_PATH)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)

    try:
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA_ENSURE)
                for stmt in _SCHEMA_ADD_COLS:
                    cur.execute(stmt)
                conn.commit()

                cur.execute(_PRIOR_SQL)
                prior = cur.fetchone()

                cur.execute(_COMPUTE_SQL)
                row = cur.fetchone()

            (total, human, bot, c_all, c_hum, c_null, c_t1,
             c_all_no_t1, c_hum_no_t1, us_st_all, us_st_hum,
             reg_all, reg_hum, reg_countries) = row
            now_epoch = int(datetime.now(timezone.utc).timestamp())

            new_vals = {
                "total_hits": total,
                "human_hits": human,
                "bot_hits": bot,
                "countries_all": c_all,
                "countries_human": c_hum,
                "countries_all_excluding_t1": c_all_no_t1,
                "countries_human_excluding_t1": c_hum_no_t1,
                "us_states_all": us_st_all,
                "us_states_human": us_st_hum,
                "regions_all": reg_all,
                "regions_human": reg_hum,
                "regions_countries_covered": reg_countries,
            }
            decreases = _detect_decreases(prior, new_vals)

            with conn.cursor() as cur:
                cur.execute(_UPSERT_SQL, (
                    now_epoch, total, human, bot,
                    c_all, c_hum, c_null, c_t1,
                    c_all_no_t1, c_hum_no_t1,
                    us_st_all, us_st_hum, US_STATE_TRACKING_SINCE_EPOCH,
                    reg_all, reg_hum, reg_countries,
                ))
                # 2026-09-03: also append to history (Part 3 —
                # site_totals_history). ON CONFLICT DO NOTHING
                # preserves the earlier row if a re-run happens to
                # collide on the same wall-clock second (unlikely
                # on a daily cadence but defensive against manual
                # kicks). Decrease-guard still compares against the
                # singleton row, unchanged.
                cur.execute(
                    "INSERT INTO site_totals_history ("
                    "  computed_at, total_hits, human_hits, bot_hits, "
                    "  countries_all, countries_human, countries_null, "
                    "  countries_placeholder_t1, countries_all_excluding_t1, "
                    "  countries_human_excluding_t1, us_states_all, "
                    "  us_states_human, us_state_tracking_since_epoch, "
                    "  regions_all, regions_human, regions_countries_covered"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (computed_at) DO NOTHING",
                    (
                        now_epoch, total, human, bot,
                        c_all, c_hum, c_null, c_t1,
                        c_all_no_t1, c_hum_no_t1,
                        us_st_all, us_st_hum, US_STATE_TRACKING_SINCE_EPOCH,
                        reg_all, reg_hum, reg_countries,
                    ),
                )
            conn.commit()

        payload = {
            "computed_at_epoch": now_epoch,
            "computed_at_utc": datetime.fromtimestamp(now_epoch, timezone.utc).isoformat(),
            "source": "site_totals_walker (~/xrpl_test/site_totals_walker.py)",
            "table": "site_totals (single-row upsert, id=1)",
            "authoritative_definitions": {
                "total_hits": "COUNT(*) FROM page_views — all rows ever",
                "human_hits": "COUNT(*) FILTER (is_bot IS NOT TRUE)",
                "bot_hits": "COUNT(*) FILTER (is_bot IS TRUE)",
                "countries_all": "DISTINCT country WHERE country IS NOT NULL — INCLUDES T1 (Tor exit) as a country value",
                "countries_human": "DISTINCT country WHERE country IS NOT NULL AND is_bot IS NOT TRUE",
                "countries_all_excluding_t1": "countries_all minus the T1 Tor placeholder (T1 is not a real country)",
                "countries_human_excluding_t1": "countries_human minus T1",
                "countries_null": "Rows with country IS NULL — informational, does not affect distinct counts",
                "countries_placeholder_t1": "1 if T1 has ever appeared, else 0 — used to derive the excluding_t1 variants",
                "us_states_all": "DISTINCT region_code WHERE region_code LIKE 'US-%' — any traffic, since state-tracking deploy (see us_state_tracking_since_utc)",
                "us_states_human": "DISTINCT US states from is_bot IS NOT TRUE rows — human-only readers of the site",
                "regions_all": "DISTINCT region_code (any country, any traffic) — worldwide scoreboard of every state/province/region observed since region_code capture went live 2026-09-01 20:51 UTC. Includes US-VA, CA-QC, ZA-KZN, GB-ENG, etc.",
                "regions_human": "DISTINCT region_code from is_bot IS NOT TRUE rows — human-only worldwide regions",
                "regions_countries_covered": "DISTINCT country on rows where region_code IS NOT NULL — count of countries where at least one subregion has been observed",
                "us_state_tracking_since_epoch": "Unix epoch when state-level capture went live (commit a092bbb). Any us_states_* count is strictly since this moment; nothing was captured before.",
            },
            "totals": {
                "total_hits": total,
                "human_hits": human,
                "bot_hits": bot,
                "bot_pct": round(100 * bot / total, 2) if total else 0,
                "countries_all_time_any_traffic": c_all,
                "countries_all_time_human_only": c_hum,
                "countries_all_time_any_traffic_excluding_t1": c_all_no_t1,
                "countries_all_time_human_only_excluding_t1": c_hum_no_t1,
                "rows_with_null_country": c_null,
                "includes_placeholder_t1_tor": c_t1 == 1,
                "us_states_since_tracking_started_any_traffic": us_st_all,
                "us_states_since_tracking_started_human_only": us_st_hum,
                "us_state_tracking_since_epoch": US_STATE_TRACKING_SINCE_EPOCH,
                "us_state_tracking_since_utc": datetime.fromtimestamp(
                    US_STATE_TRACKING_SINCE_EPOCH, timezone.utc).isoformat(),
                "regions_worldwide_any_traffic": reg_all,
                "regions_worldwide_human_only": reg_hum,
                "regions_countries_covered": reg_countries,
            },
            "monotonic_check": {
                "prior_row_present": prior is not None,
                "decreases_detected": decreases,
                "guarded_fields": list(_MONOTONIC_FIELDS),
                "note": (
                    "Any count decrease vs prior row is impossible on append-only "
                    "page_views. Non-empty list here → data-integrity finding "
                    "(walker_health.findings_count > 0 → BetterStack page). "
                    "Only truly monotonic fields are guarded — human/bot subsets "
                    "and us_states_human rebalance when is_bot classifier reruns."
                ),
            },
            "notes": [
                "Prior public counts of 194 / 190 / 180-something came from different queries/filters and are NOT authoritative.",
                "countries_all is the authoritative answer to 'how many countries have visited xrpldashboard' unless Charlie has ruled otherwise.",
                "countries_all_excluding_t1 is available as an alternate reading if T1 (Tor) placement is considered non-canonical.",
                "us_states_* counts have a shorter clock than country counts. The state clock started 2026-09-01 20:51 UTC when commit a092bbb went live; nothing before that has region_code populated. A small us_states_all number early in the state clock's life is expected and will grow with time.",
                "Data-integrity: an all-time count in the guarded set going down flags a walker_health finding — append-only data should never regress.",
                "Growth-over-time queries: the walker also appends a row to site_totals_history on each run (Part 3, 2026-09-03). 'How much did we grow in N days' resolves to a single query against that table — no page_views recomputation required. No backfill; the history table starts accreting from 2026-09-03 forward.",
            ],
        }
        _write_docs_json(payload)

        if decreases:
            message = f"COUNT DECREASE detected — data-integrity: {'; '.join(decreases)}"
            logging.warning(message)
            findings = len(decreases)
        else:
            message = (
                f"clean: total={total:,} human={human:,} bot={bot:,} "
                f"countries_all={c_all} (excl-T1={c_all_no_t1}) "
                f"countries_human={c_hum} (excl-T1={c_hum_no_t1}) "
                f"us_states_all={us_st_all} us_states_human={us_st_hum} "
                f"regions_all={reg_all} regions_human={reg_hum} "
                f"regions_countries_covered={reg_countries}"
            )
            logging.info(message)
            findings = 0

        db.write_walker_health_end(
            WALKER_NAME, ok=True, message=message, findings_count=findings,
        )
        sys.exit(0)

    except Exception as e:
        logging.exception("site_totals_walker failed")
        db.write_walker_health_end(
            WALKER_NAME, ok=False, message=f"unhandled exception: {e!r}",
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
