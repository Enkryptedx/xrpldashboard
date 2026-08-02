# Floor re-run 2026-08 — STOP

**Verdict: STOP. The re-run cannot be executed today because neither
precondition from the July methodology has been met.**

Both shorthand assumptions embedded in the "slid from Aug 2" tracking
line have outlived the details. Corrections lead:

---

## Correction 1 — the value-weighted floor was never computed

The July record (`~/.openclaw/workspace/research/coverage_gaps_track1_clob.md`,
2026-07-10) is explicit: the 96.5% / 3.4% / 0.1% split from the 61-ledger
sample at ledger-time 2026-07-11T02:47Z is **BY COUNT**, not by value.
The value-weighted split has never been measured. The doc says so
verbatim:

> "Value share (by XRP): not measured. The Neon `token_volume.volume_xrp`
> column exists but the walker doesn't tag AMM vs CLOB path per trade
> — this is the same gap D1_DATA_RESULTS_v3 flagged (value-weighting
> migration due ~Aug 2)."

The re-run has been queued in memory as if the July prelim was a
value-weighted number that a Charlie-approved second run would confirm
or move. It was never that. It was a count-based prelim with a walker
migration parked behind it. If the memory line said "re-run the July
value-weighted floor," the shorthand was wrong at the source.

## Correction 2 — the walker migration required to produce value-weighted CLOB/AMM share has not shipped

Verified today (2026-08-02) against `xrpl_test/`:

- **`xrpl_stream.py` schema of the write path** — `token_event_handler`
  (line 413) writes `(currency, issuer, hour_bucket, volume_xrp,
  trade_count)` into `token_volume`. No `path_type`, no `amm_touched`,
  no CLOB tag. Same shape as July.
- **`db.py` `token_volume` schema** (line 119) confirms —
  `PRIMARY KEY (currency, issuer, hour_bucket)`, no path column.
- **AMMDeposit / AMMWithdraw path** (line 456) deliberately writes
  `volume_xrp = 0.0` — pricing those legs is still deferred per the
  in-file comment (line 433-435).
- **Pure DEX OfferCreate trades that don't touch an AMM** are not
  captured in `token_volume` at all — `token_event_handler` only
  handles Payment / AMMDeposit / AMMWithdraw. `amm_pool_events`
  captures the AMM-touching subset via `_AMM_ACCOUNT_SET` matching
  (line 592ff) but not pure order-book fills. **CLOB fills are still
  invisible to value-weighting.**
- **Git log 2026-07-10 → 2026-08-02** shows no commits touching
  `xrpl_stream.py` or `db.py` that add path attribution to token_volume.
  Real xrpl_stream.py changes in the window: one commit (`0672218`,
  "xrpl_stream health") — unrelated. `db.py` commits are is_bot,
  BetterStack, walker_health, RLUSD calendar-day — none about
  value-weighting CLOB vs AMM.

The walker migration the July doc anchored to hasn't started, let alone
landed. The queue position ("after value-weighting migration ~Aug 2")
was aspirational — it slid because the migration slid, not because
downstream analysis slid on its own schedule.

## Correction 3 — the 30-day window doesn't exist yet even for count-based numbers

The July record's own caveat:

> "30-day tx-type mix (stream only counts since 2026-07-07; only 3.3d
> actuals available — do NOT quote 30-day numbers before ~2026-08-06)."

Today = 2026-08-02. Stream start = 2026-07-07. Elapsed = **26 days**,
4 days short of the earliest date at which a "30-day" claim is honest.
Even a count-only re-run at this instant would violate the July note's
own guardrail.

---

## What actually holds today (the honest current state)

- **Count-weighted CLOB share, July prelim:** 96.5% (61-ledger sample,
  ledger-time 2026-07-11T02:47Z). This is a single-point snapshot from
  a ~4-minute window, not a distribution.
- **Value-weighted CLOB share:** unknown. Cannot be computed against
  current schema. Would require, in order: (1) walker migration to tag
  AMM-vs-CLOB path per trade at write time; (2) at least 7-14 days of
  post-migration data to have a distribution; (3) the 30-day rolling
  window that the kill criterion is defined against.
- **Kill criterion (from July doc):** CLOB share of value settled falls
  below 5% over a rolling 30-day window → CLOB build parked. Threshold
  is intact; the measurement it gates is not runnable.

## Dataset notes (for completeness, not for use)

If Charlie chooses to run the count-only version anyway with the 4-day
short window flagged, the following gaps would need explicit provenance
marks:

- **Stream data window:** 2026-07-07 → 2026-08-02 (~26 days).
- **AMM pool_events data window:** ~same, with two known gaps to
  investigate before quoting: (a) any AMM collapse / AMM_scan outage
  in the July-Aug window (Charlie flagged as a data-gap event), (b)
  2026-07-24 DNS day (Render custom-domain re-verify outage 10:00-10:11
  EDT — worker side unaffected as far as I know, but needs
  `walker_health` confirmation).
- **CLOB (pure DEX) coverage:** zero. Not captured in either table.

None of this is a substitute for the value-weighted computation the
July methodology defined.

---

## Two gated verdicts, framed as decisions Charlie makes

### Gate 1 — institutional conversations

**Question:** does the data justify entering conversations that frame
XRPL as CLOB-first (value-weighted)?

**Frame:** The count-weighted July prelim (96.5% CLOB by trade count)
does support "CLOB is the dominant venue by activity" — a defensible
claim in room-and-audience appropriate settings, with the caveat
attached that "we measure this by trade count today; value-weighted
measurement is queued behind a walker migration." Anything stronger
than that (e.g. "CLOB does N% of XRPL value") is unsupported and would
be a citation-risk moment if a counterparty checks. Recommendation
posture: institutional conversations that need count-based framing =
OK now; conversations that will be pushed to value-weighted specifics
should wait for migration + 30-day window.

### Gate 2 — CLOB build

**Question:** does the analysis justify the ~4.5-build-day + ~85MB/30d
CLOB build?

**Frame:** Kill criterion is not yet measurable. The build cost is a
known finite cost; the kill signal is a value-weighted share threshold
that requires the walker migration to shipped first. The correct
sequencing is: **ship the value-weighting migration first, then let
7-14 days of post-migration data build a distribution, then run this
analysis, then decide the build.** Skipping to the build decision now
means either (a) building on the count-based prelim and accepting that
if the value distribution is materially different the build was wrong,
or (b) building on faith. Recommendation posture: **CLOB build stays
gated, but the gate is now visibly upstream — the walker migration is
the true blocker, not the analysis.**

---

## What would move this off STOP

Two things, in order:

1. **Walker migration lands** — `xrpl_stream.token_event_handler`
   gains a path-type column write (Payment via path-find with
   AMM-account hop = AMM_HYBRID; Payment with pure DEX hop = CLOB;
   OfferCreate captured separately with the same tag). `db.py`
   `token_volume` schema gains a `path_type` column (or a sibling
   table if we want to keep the primary key stable). Rough scope:
   half-day to a day of work; the AMM_ACCOUNT_SET is already loaded
   in memory (line 592) so the classification input is on hand.

2. **Post-migration data accumulates** — 7-14 days minimum for a
   distribution read; 30 days to compute against the kill threshold
   as defined.

When both hold, this doc gets re-opened and Charlie's directive
executes as intended.

---

## Provenance discipline

- Correction 1: [FROM-RESEARCH-DOC — `coverage_gaps_track1_clob.md`
  2026-07-10, quoted verbatim]
- Correction 2: [FROM-CODE — `xrpl_test/xrpl_stream.py` line 413-490,
  `xrpl_test/db.py` line 119-128, read 2026-08-02] + [FROM-GIT-LOG —
  `git log --since=2026-07-10 -- xrpl_stream.py db.py`, 2026-08-02]
- Correction 3: [FROM-RESEARCH-DOC — same file, data-timing warning
  block, quoted verbatim] + [FROM-CALENDAR — today = 2026-08-02]
- Count-weighted July prelim (96.5% / 3.4% / 0.1%): [FROM-RESEARCH-DOC
  — same file, 61-ledger sample block]
- Kill criterion 5% / 30d: [FROM-RESEARCH-DOC — same file,
  "Kill criteria #2" block]

**Not marked as fact:** whether the walker migration is on anyone's
active build queue (I don't have a signal on that either way); whether
Charlie now wants to prioritize the migration or park the whole
analysis question until later.

---

*Filed 2026-08-02 by sandbox as STOP report per msg 10429 rule
("a re-run against a guessed methodology isn't a re-run").*
