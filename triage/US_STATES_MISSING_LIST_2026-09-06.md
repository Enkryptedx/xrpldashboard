# US states not yet seen by any human visitor — running list

**Source of truth:** the `page_views` table, filtered `country='US' AND region_code LIKE 'US-%' AND is_bot IS NOT TRUE`. Distinct `region_code` values represent human-visited states.
**Region tracking clock started:** 2026-09-01 (per site_totals region_code column shipping).
**Correction note (2026-09-06 09:30 UTC):** Kansas (US-KS) first-seen 2026-09-04 06:14 UTC by a human visitor. The Thursday morning report should have flagged this but missed it. The "missing 16" list is therefore actually **missing 15** as of Sat 2026-09-05.

## Currently absent from the human-reader map (15 states)

| code | state |
|------|-------|
| US-AR | Arkansas |
| US-CT | Connecticut |
| US-DE | Delaware |
| US-ID | Idaho |
| US-MD | Maryland |
| US-MS | Mississippi |
| US-MO | Missouri |
| US-MT | Montana |
| US-NE | Nebraska |
| US-NH | New Hampshire |
| US-ND | North Dakota |
| US-RI | Rhode Island |
| US-SD | South Dakota |
| US-VT | Vermont |
| US-WV | West Virginia |

## How to check for arrivals

```sql
SELECT region_code, MIN(to_timestamp(ts)) AS first_seen
FROM page_views
WHERE is_bot IS NOT TRUE
  AND region_code = ANY(ARRAY['US-AR','US-CT','US-DE','US-ID','US-MD','US-MS','US-MO','US-MT','US-NE','US-NH','US-ND','US-RI','US-SD','US-VT','US-WV'])
GROUP BY 1 ORDER BY 2;
```

Any row returned is a state that has arrived since 2026-09-06 — remove it from the list above and note the first-seen timestamp in the daily report.

## Kansas arrival details (for the record)

- **First human visit:** 2026-09-04 06:14:53 UTC
- **Flagged in a report:** first appeared in Sun 2026-09-06 morning report (missed in Thursday's report)
- **Note this pattern:** report cadence needs to re-check the missing-list on every daily pass, not just on chat-mentioned queries
