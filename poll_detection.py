"""Poll / auto-refresh detection for page_views ingest.

WHY THIS IS ITS OWN MODULE (and not part of public_analytics_filters)
---------------------------------------------------------------------
A poll is NOT a bot. The two concepts must never share a code path, a
fragment list, or a column, because conflating them would mark real
readers as crawlers — which is exactly the mistake this module exists to
prevent.

HARD RULE (Charlie, 2026-10-14 19:39 ET)
----------------------------------------
Leaving a page open — for any duration, producing any number of polls —
MUST NEVER:
  * set `is_bot`,
  * reduce the visitor's human status,
  * feed any bot or crawler rule.

Polls are tagged `is_poll` ONLY. The visitor still counts as exactly one
human visitor. A tab open for 17 hours is a highly engaged reader, not a
scraper. `tests/test_poll_detection.py` pins every clause of that rule.

WHAT WENT WRONG
---------------
`templates/index.html` refreshes its live panels with
`fetch(window.location.pathname + '?_=' + Date.now())` every ~30s while
the tab is visible. The ingest (`app._log_page_view`) is an
`@after_request` hook that logs EVERY GET, `/` is not skip-listed, and
nothing in it distinguished a `fetch()` from a navigation. Because
`path = request.path` drops the query string, each refresh landed as a
plain `/` pageview.

Measured 2026-10-14: 45,593 of 80,078 all-time "human" pageviews (56.9%)
were self-same-path refetches — `/` alone was 39,510 pv from 1,462
visitors. One Korean visitor produced 4,111 pageviews in 17.8 hours from a
single open tab, which earlier looked exactly like a crawler flood.
UNIQUE VISITOR COUNTS WERE NEVER AFFECTED — only pageviews inflated.

The same bug was already known and fixed for `/analytics/live`, which is
skip-listed with the comment "fires every 15s from the /analytics JS
interval; must not appear in page_views or it inflates RIGHT NOW counts".
The homepage refresh reintroduced it by re-fetching `/` itself.

DETECTION, IN PRIORITY ORDER
----------------------------
1. Our own explicit marker (`?_live=1`). Unambiguous, under our control,
   and independent of browser behaviour.
2. `Sec-Fetch-Dest`. A top-level navigation sends `document`; a `fetch()`
   sends `empty`. This catches ALL current and future JS polling
   site-wide with no template change.
3. Absent/blank header -> fall back to today's behaviour (count it).
   Older clients and most bots send no `Sec-Fetch-*` at all, so the
   fallback must be "treat as a pageview", never "treat as a poll".
"""

# Query-string marker our own auto-refresh fetches append. Values other
# than "1" are ignored so a crawler appending ?_live=whatever cannot
# suppress its own pageview.
POLL_QUERY_MARKER = "_live"
POLL_QUERY_VALUE = "1"

# A pageview is a top-level document navigation. Anything else that
# declares its destination is a subresource/programmatic fetch.
NAVIGATION_FETCH_DEST = "document"


def is_poll_request(sec_fetch_dest=None, live_marker=None):
    """Return True when this request is an auto-refresh/poll, else None.

    Returns None rather than False so the value drops straight into the
    nullable `page_views.is_poll` column, matching how `is_bot` is stored
    (TRUE or NULL, never FALSE). Readers test `is_poll IS NOT TRUE`.

    `sec_fetch_dest`: the raw `Sec-Fetch-Dest` header, or None if absent.
    `live_marker`: the raw `?_live=` query value, or None if absent.

    This function knows nothing about user agents, countries or bots, and
    it must stay that way — see the HARD RULE above.
    """
    if live_marker is not None and str(live_marker) == POLL_QUERY_VALUE:
        return True
    if sec_fetch_dest is None:
        return None
    dest = str(sec_fetch_dest).strip().lower()
    if not dest:
        return None
    if dest == NAVIGATION_FETCH_DEST:
        return None
    return True
