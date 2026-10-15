# Pageview correction: homepage auto-refresh was counted as traffic

**Date:** 2026-10-14
**Status:** cause fixed forward-only; historical rows deliberately unchanged
**Scope:** `page_views` pageview counts. **Unique-visitor counts were never
affected.**

## Summary

`templates/index.html` refreshes its live panels with

```js
fetch(window.location.pathname + '?_=' + Date.now())
```

every ~30 s while the tab is visible. The ingest, `app._log_page_view`, is an
`@after_request` hook that logs **every GET**; `/` is not in either skip
list; and nothing in it distinguished a `fetch()` from a navigation. Because
`path = request.path` drops the query string, `/?_=1760…` was stored as a
plain `/`.

The result: an open browser tab manufactured a pageview every ~30 s
indefinitely, and those rows were indistinguishable from real page loads.

## Numbers (measured read-only via `jj_query.sh`, 2026-10-14)

Window: all time, 2026-05-09 → 2026-10-14. Human definition: the canonical
`is_bot IS NULL AND sql_not_bot_ua_clause()`.

| | pageviews | visitors |
|---|---|---|
| All-time human baseline | 80,078 | 28,074 |
| **Self-same-path refetches (polls)** | **45,593** | 2,408 |
| **Share of baseline** | **56.9%** | — |

By path (top):

| path | poll pageviews | visitors |
|---|---|---|
| `/` | 39,510 | 1,462 |
| `/analytics` | 1,506 | 59 |
| `/lang/de` | 354 | 20 |
| `/amendments` | 350 | 158 |
| `/lang/es`, `/lang/en`, `/lang/zh`, `/lang/ko`, `/lang/fr`, `/lang/hi`, `/lang/pt`, `/lang/ja` | 266–340 each | 17–19 each |

Worst single visitor: **4,111 pageviews in 17.8 hours** (KR, one path, a
single open tab) — a request every **~15.6 s**, not the intended 30 s. The
next three were 4,167 / 2,017 pv (KR, 13.5 h / 17.5 h) and 1,677 pv (US,
10.4 h).

`/amendments` was **not** self-inflating: it has no `fetch()` at all, only
`setInterval(tickAll, 1000)`, a client-side countdown. `index.html` is the
only template that re-fetches its own path. Its 350 poll-classified rows are
ordinary in-site navigation (a same-origin referrer on a different path).

## How it was nearly misdiagnosed

The concentration pattern — thousands of hits from a handful of visitor
hashes on a single path — looks exactly like a crawler flood. Two earlier
candidate rules were built and discarded:

1. **A UA-fragment rule on the Chrome version.** Rejected after measuring:
   `Chrome/154` spanned 51 hashes across 18 countries and `Chrome/155` 84
   hashes across 20, so blocking the string would have deleted ordinary
   readers and needed re-editing every Chrome release.
2. **A behavioural bot rule** (`≥100 pv`, one path, no referrer, ≤2-day
   span). Measured at **0 visitors, 0 pageviews** — it required *no
   referrer*, but these hits are *self-referred*. It would have done nothing.

Had a looser version shipped, it would have marked a reader who left the
dashboard open overnight as a bot. **A poll is not a bot** became the hard
rule as a direct result.

The same failure mode was already known: `/analytics/live` is skip-listed
with the comment *"fires every 15s from the /analytics JS interval; must not
appear in page_views or it inflates RIGHT NOW counts and clogs the Recent
Visits feed."* The homepage refresh reintroduced it by re-fetching `/`.

## Fix (forward-only)

Four changes, all in one PR:

1. **`page_views.is_poll BOOLEAN`** — nullable, no default. Applied at boot
   by `db.ensure_page_views_is_poll_column()`, with
   `SET lock_timeout = '3s'` before the `ALTER` so it can never queue behind
   `pg_dump` during the 22:00 ET backup (ADD COLUMN takes ACCESS EXCLUSIVE;
   PostgreSQL's lock queue is FIFO, so a waiting ALTER would stall every
   reader behind it). On timeout it logs a distinct category and retries next
   boot; `IF NOT EXISTS` makes retries free.
2. **`Sec-Fetch-Dest` gate** — a navigation sends `document`, `fetch()` sends
   `empty`. An absent or blank header falls back to counting the request, so
   older clients and bots are unaffected.
3. **`?_live=1` marker** on the homepage fetch, so the fix does not depend
   solely on a browser header. Only the exact value `1` counts, so a crawler
   appending `?_live=anything` cannot suppress its own pageview.
4. **`MIN_REFRESH_MS` clamp** derived from `REFRESH_AT`. The refresh
   condition previously compared the *server* cache age, reset from each
   fetched page, so a page served already near 30 s re-fired within seconds —
   the ~15.6 s interval above.

### Reader contract

- **Pageview counts:** add `AND is_poll IS NOT TRUE`.
- **Visitor counts:** do **not** filter `is_poll`. A visitor whose rows are
  mostly polls is still one engaged human; filtering would delete them.

## Why history is not rewritten

Decided 2026-10-14. These rows were produced by **real people**, not bots.
Retroactively deleting or relabelling six months of them would be a second
distortion layered on the first, and the published daily/weekly series
already carry those values.

Instead: `is_poll` is NULL on every pre-existing row, the historical series
keeps its published numbers, and this note records the overcount so any
future reader of those figures can discount them correctly. The correction
applies from the deploy forward.

Unique-visitor counts need no correction and never did: **1,462 visitors on
`/` was always accurate; 39,510 pageviews was not.**

## Follow-ups

- **Public disclosure:** a paragraph in `templates/methodology.html`, pending
  the owner's wording. That page already promises *"Web dashboards can
  silently re-render history… visitors have no way to tell"*, so a 56.9%
  pageview overcount belongs there.
- **Long-session engagement metric** (proposed, not built): count visitors
  whose first and last hit span ≥1 h. This is only possible *because* polls
  are tagged rather than dropped — skip-listing them would erase the evidence
  that a tab stayed open. Caveat: `visitor_hash` is day-bucketed, so a
  session crossing UTC midnight reads as two visitors.
- Verify after deploy that `/` pageviews drop to roughly the navigation-only
  rate (~520–650/day was the observed non-poll baseline) while unique
  visitors stay flat. A fall in *visitors* would mean the gate is
  over-matching.
