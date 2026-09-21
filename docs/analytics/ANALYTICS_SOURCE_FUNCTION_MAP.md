# /analytics — source-function map

**Charlie ruling 2026-09-21 (Mon afternoon, item 3).** Every number on `/analytics` must come from a named function, and each function's bot/self-probe/geo filter must be the same one the morning/weekly reports use. This document is the canonical mapping. If a new panel gets added, add its row.

Reader for the source functions themselves: all `db.read_*` calls live in `/Users/charliebruce/xrpl_test/db.py`; all "shared allow-list" helpers live in `/Users/charliebruce/xrpl_test/public_analytics_filters.py`.

## Data map

| /analytics panel | ctx var | source function | window | kind | shared allow-list applied? |
|---|---|---|---|---|---|
| Rollup counters (5m / 1h / 24h / 7d, humans) | `rollups` | `db.read_page_view_stats` | rollup | `human` | via `_bot_filter_sql` on the is_bot column (NOT the shared UA/geo allow-list) |
| Top pages · 24h | `top_24h` | `db.read_top_pages` | 24h | `human` | via `_bot_filter_sql` |
| Top pages · 7d | `top_7d` | `db.read_top_pages` | 7d | `human` | via `_bot_filter_sql` |
| Countries · 24h list | `countries_24h_all` (sliced to `countries_24h`) | `db.read_country_breakdown` | 24h | `human` | via `_bot_filter_sql` |
| Countries · 24h count | `countries_24h_count` | `db.read_country_count` | 24h | `human` | **YES** — shared UA + geo allow-list (converged 2026-09-20/21) |
| Countries · all-time list | `countries_all` | `db.read_country_breakdown` | all-time | `human` | via `_bot_filter_sql` |
| Countries · all-time count | `countries_all_count` | `db.read_country_count` | all-time | `human` | **YES** — shared UA + geo allow-list |
| Continent aggregates · 24h / all | `continent_24h`, `continent_all` | `_continent_aggregate` over `countries_*` output | — | derived | inherits from `countries_*` |
| External referrers · 7d | `external_refs_7d` | `db.read_external_referrers` | 7d | humans-only | **YES** — shared UA allow-list (converged 2026-09-21) |
| UTM landings · 7d | `utm_landings_7d` | `db.read_utm_landings` | 7d | any | No — UTM presence is the filter |
| CTA · institutional-contact stats | `cta_stats` | `db.read_cta_click_stats(cta_id=…)` | all-time | any | No — CTA table has its own bot filter at write time |
| CTA · institutional-contact recent 10 | `cta_recent_raw` | `db.read_recent_cta_clicks(cta_id=…)` | last 10 | any | No |
| Bot rollup counters | `bot_rollups` | `db.read_page_view_stats` | rollup | `bot` | via `_bot_filter_sql` |
| Probed paths · 24h | `bot_top_24h` | `db.read_top_pages` | 24h | `bot` | via `_bot_filter_sql` |
| Bot countries · 24h | `bot_countries_24h` | `db.read_country_breakdown` | 24h | `bot` | via `_bot_filter_sql` |
| **Declared AI + search crawlers · 24h** | `ai_crawler_counts_24h` | `db.read_ai_crawler_counts` | 24h | declared-crawler only | reads `ai_crawler_hits` (populated by `_agent_tier_audit_header` per request) |
| **Declared AI + search crawlers · 7d** | `ai_crawler_counts_7d` | `db.read_ai_crawler_counts` | 7d | declared-crawler only | reads `ai_crawler_hits` |
| Recent visits (100) | `recent` | `db.read_recent_page_views` | last 100 | any | **YES** — self-probe UA fragments filtered (converged 2026-09-21) |

## Cross-references

- **Weekly / Sunday-close report** (`scripts/weekly_analytics.py`):
  - Countries (all-time + human): `all_time_country_tallies` — same underlying rows as `db.read_country_count(None, kind=…)` after allow-list convergence.
  - US states: `all_time_us_state_split` — no /analytics counterpart today.
  - Regions (strict `^[A-Z]{2}-[A-Z0-9]{1,3}$`): `regions_all` — no /analytics counterpart today.
  - Anomalies + three-axis diagnostic: `country_baseline_anomalies` + `country_three_axis_check` — no /analytics counterpart today.

- **Morning standing-orders report** (`scripts/standing_orders_daily_report.py`):
  - Referrer buckets (X/Reddit/news/direct/search): inline regex on `page_views.referrer`. X regex updated 2026-09-21 to include `t\.co|twitter|x\.com` (same as evening report's).
  - AI crawler counts: reads `ai_crawler_hits` grouping by `ua_class` — **same source as `read_ai_crawler_counts` above.**
  - `/check.json` external count: `page_views` filtered by UA-exclusion + `status = 200` (converged 2026-09-21 — a 429/4xx isn't a consumer of signed data).

## Still ad-hoc / follow-up owed

1. **Top-pages / country-breakdown / rollups filters** still use `_bot_filter_sql` (which is is_bot-column-based) rather than the shared allow-list. Converging these needs a `precomputed_bots`-aware version of the shared allow-list — otherwise the fast path (indexed subquery against page_view_bot_hashes + scanner_combos) gets bypassed. Filed for a later pass.
2. **Search-referrer definition** — `read_external_referrers` returns raw host counts (google.com 443/7d). Morning report separates search from social by regex; the 443 vs ~14/day gap is likely `"any google.com host as referrer"` vs `"only /search results URL as referrer"`. Needs a decision on which definition the panel should use.

## Rule going forward

- Every new number on `/analytics` must land as a named `db.read_*` (or `_derived_*`) call. No inline SQL in the route.
- Every allow-list decision must go through `public_analytics_filters` — no ad-hoc regex or hardcoded UA lists in the route or reader.
- Update this map with the new row.
