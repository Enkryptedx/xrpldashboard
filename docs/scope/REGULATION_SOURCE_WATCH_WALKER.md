# /regulation source-watch walker — scope doc

**Status:** scope only. Filed 2026-09-09 during the pre-freeze build window (freeze Sat 2026-09-12 morning). No code lands from this document. **Post-freeze** — the manual re-verify done 2026-09-09 (commit `64d3976`, `LAST_VERIFIED_REGULATION` → 2026-09-09) is the fix for now; this walker is the durable follow-up, not a tonight-scope item.

## Problem it solves

`/regulation` is **manually curated** — there is no walker cadence behind its freshness chip (unlike `_freshness_badge.html` surfaces). The chip goes amber at `days_since > 7` (`templates/_regulation_freshness.html`), which is a *time* trigger, not a *change* trigger. So two failure modes exist:

1. **Silent drift** — a watched source (the bill's cloture schedule, an ethics-deal announcement) changes and nobody notices until the 7-day amber banner fires, or until a reader emails us. The 2026-09-06 review→2026-09-09 bump gap is exactly this: three days where the "week of 2026-09-15" wording was stale-able and un-flagged.
2. **False confidence** — the page reads "Last verified: <date>" but that only proves someone *looked*, not that the underlying sources are unchanged.

The walker closes (1): **flag "review needed" when a watched source changes.** It is **flag-only — it never writes page content.** A human (Charlie, or a re-verify subagent) always composes the actual copy edit. This preserves the primary-source-discipline the page is built on.

## Watched sources (per-source scraping logic differs — this is the hard part)

| Source | What to watch | Access shape | Difficulty |
|---|---|---|---|
| Congress.gov actions list, H.R. 3633 | new action rows (cloture filed/invoked, floor vote, passage) | **Cloudflare-gated** — returned 403 "Just a moment…" on direct fetch 2026-09-09. Needs the Congress.gov **API** (api.congress.gov, key required) or GovTrack mirror (lags ~1 day, misses floor-scheduling rows). | High |
| Senate floor schedule | cloture ripen date/time, floor votes queued | JS-rendered / redirect-gated (democrats.senate.gov schedule 404'd, republican leader page JS). Congressional Record daily digest is the primary but is a large text scrape. | High |
| SEC / CFTC press + rulemaking pages | new releases touching CLARITY implementation | SEC has RSS (press-releases feed); CFTC less consistent. RSS is the tractable one. | Medium |

## Why this is not a tonight-scope task

- **Three different access modalities** (authenticated API vs HTML-scrape-behind-JS vs RSS), each with its own parser and its own failure/staleness handling. Live-proven this session: Congress.gov 403'd on direct fetch and the search provider bot-challenged repeatedly, so naïve scraping is fragile.
- **"Changed" detection is non-trivial.** A content hash of a Cloudflare challenge page or a reordered nav is a false positive; the walker must diff the *semantic* action-list, not raw bytes. That needs a normalize-then-hash step per source.
- **Change ≠ page-edit-needed.** Many source changes (a reworded press blurb) don't warrant a copy edit. The walker flags; it must not cry wolf, so a per-source "material change" heuristic is required, not just "bytes differ."
- Adding a plist walker is a **feature deploy** — during the Sat freeze that is not a permitted change class, so it must land pre-freeze with full test coverage or wait until after Charlie's return.

## Proposed shape (when built)

- **`scripts/regulation_source_watch.py`** — per-source fetchers → normalized action/record list → stored fingerprint (`data/regulation_source_watch_state.json`) → on material diff, write a `review_needed` flag (a row/stamp a canary reads) + `_last_ok` heartbeat stamp on clean runs. **Never touches `templates/regulation.html` or `LAST_VERIFIED_REGULATION`.**
- **Congress.gov via the official API** (api.congress.gov) with a key, not HTML scraping — sidesteps the Cloudflare wall. SEC via RSS. Senate schedule via Congressional Record daily digest (lowest-priority source; the API + RSS cover the load-bearing signals).
- **plist**: `StartInterval` (per `macos_startinterval_over_calendar`), `RunAtLoad: true`, `_last_ok` stamp; after install, `launchctl kickstart -k gui/501/<label>` and verify `runs > 0, exit = 0` before marking done (per `plist_kick_after_convert`).
- **Canary**: add the label to `dockvault_mirror_freshness_canary`'s JOBS list so a silent walker pages.
- **Meta-watch**: the walker's own staleness is caught by the freshness-canary; the `review_needed` flag surfaces to Charlie via the daily 5-line report, not an auto-edit.

## Estimated effort

~½–1 day for a robust first cut (API-key provisioning for Congress.gov + one clean fetcher + fingerprint/diff + plist + canary + tests), more if Congress.gov API access needs sign-up. Not the ~1–2h "small" that would justify shipping tonight.
