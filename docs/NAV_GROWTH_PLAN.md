# Nav growth plan — priority-collapse when top nav fills

**Status:** parked design, not yet activated.
**Activation trigger:** when link **#20** is proposed for `_nav.html`.
**Owner decision:** Charlie approves both the trigger firing and the initial cluster shape before code lands.

---

## Current state (as of 2026-07-27)

`templates/_nav.html` renders **18** `_nav_link` items in one flat row:

    /  /learn  /amendments  /network  /sidechain  /pools  /whales  /tokens
    /mpts  /rwa  /rlusd  /lending  /health  /methodology  /regulation
    /about  /institutional

Plus: XRP price chip, liveness chip, language switcher.

This still fits on desktop widths (~1280px+) without wrapping. Reviewer feedback in `project_xrpldashboard_backlog_nav_crowding.md` flagged three items as candidates for a "docs" submenu today (`/security /methodology /health`), but there is no observed user-friction signal yet — mobile burger collapses the row cleanly, desktop reads fine. So today: no change, plan on the shelf.

## The trigger

Activate the plan when someone (Charlie, JJ, or a reviewer) proposes link **#20**. That's the number where a flat row starts wrapping at common laptop widths and where the eye stops parsing it as a menu and starts parsing it as noise.

Not "when nav feels crowded." Not "when we ship page X." A specific integer, easy to test at review time.

## The shape

Priority-collapse, not category-drawer. Reasons:

- **Top-of-nav real estate is a claim** about what the site is for. The moment we hide `/whales` behind a "Data" drawer, we're demoting it below the drawer label. The drawer label wins the click; the destination loses it.
- **Category drawers push the burden onto the user** ("is /pools under Data or Markets?"). A flat priority list with a single "More" overflow keeps the mental model at one level.
- **Reversible.** Priority list is a rank; category tree is a taxonomy. Ranks re-order in seconds; taxonomies calcify.

### Target layout (activated form)

**Top row (~8 links, always visible):**

    /  /amendments  /whales  /pools  /tokens  /rwa  /rlusd  /learn

Rationale: these are (a) the highest-traffic destinations by analytics, (b) the load-bearing "what makes xrpldashboard xrpldashboard" surfaces, and (c) the ones that answer questions a user brought to the site rather than ones the site wants them to know about.

**"More" overflow (single dropdown, everything else in natural clusters):**

- **Network health:** /network · /sidechain · /health
- **Institutional & regulation:** /institutional · /regulation
- **Reference:** /mpts · /lending · /methodology · /about

Ordering inside "More" is the natural cluster shown above — not alphabetical, not by traffic. Users who open "More" are looking for something specific; clusters help them locate it.

### What lives outside the plan (not in "More")

Kept flush right, unchanged:

- XRP price chip
- Liveness chip
- Language switcher

These are always-on affordances, not destinations.

## Non-goals

- **No category-drawer refactor.** Keeps the single-level model.
- **No hover-menus.** Mobile-hostile and screen-reader-hostile. "More" opens on click, closes on click-outside.
- **No hamburger on desktop.** The site is a dashboard; primary destinations stay visible.
- **No search-in-nav.** The site has `/check` as its search-shaped surface. Adding another one dilutes it.

## Rebalance rule (post-activation)

Once activated, the top-8 ranking is not permanent. Rebalance quarterly against analytics:

1. Pull last-90-days pageview counts for every nav destination.
2. Compute rank.
3. If a top-8 link has fallen out of the top-8 by pageviews AND a "More" link has risen into the top-8 by pageviews, propose the swap in a `queue-audit` note.
4. Swap ships only if Charlie approves — analytics is a signal, not a mandate. Editorial priority can override traffic (e.g., `/regulation` may stay top-row during a live legislative window even if traffic is lower).

## Accessibility notes

- `"More"` button uses `<details><summary>` (same primitive as the language switcher) so it works with keyboard nav and screen readers with zero JS.
- `aria-current="page"` on the active top-row link stays as today.
- If the active page lives inside "More," the "More" button gets a visual + `aria-current` marker so the user's location is legible without opening the drawer.

## What we're deliberately not building

- Second-tier nav under a top-row hover.
- Breadcrumbs on interior pages.
- A sitemap page distinct from `/about`.

Each of these is a real option, but none has an observed-demand signal today. Park; add only on evidence.

## When this plan itself gets revisited

- Trigger fires (link #20 proposed) → activate.
- OR mobile-nav complaint pattern shows up in `/check` submissions or feedback channel → re-open the design.
- OR desktop analytics show scan-depth dropping (users clicking exclusively the leftmost 3-4 links, ignoring the tail) → re-open.

Otherwise: park. Nav design is a claim about the site's mission; re-doing it because it feels stale is a mission-drift bar-lower.
