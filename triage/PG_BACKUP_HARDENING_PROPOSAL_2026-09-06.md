# pg_backup Hardening — Proposal (no code, decision-first)

**Filed:** 2026-09-06
**Trigger:** 09-04 nightly failed after ~8min upload
**Failure signature (log-verified, not assumed):**

```
2026/09/04 22:08:06 ERROR : neondb-20260905T020004Z.dump:
  Post request rcat error: no tomes available (503 service_unavailable): trying again in 1s
2026/09/04 22:08:06 NOTICE: Failed to rcat with 2 errors
```

**Root cause:** B2 upstream 503 "no tomes available" during `rcat` upload.
NOT a Neon statement_timeout. Prior framing (events-table timeout) was wrong —
the `-c statement_timeout=0` fix from 08-30 is holding. Actual weakness:
`rclone rcat`'s default retry budget is small, and streaming (vs file-based
upload) means a transient B2 hiccup takes the entire 5000-second dump with it.

**Dump-size trajectory** (self-limiting analysis before overengineering):

```
09-01  6.25 GB   (baseline)
09-02  6.33 GB   +80 MB
09-03  6.44 GB   +110 MB
09-04  FAIL      (B2 503, not size-related)
09-05  6.64 GB   +200 MB
```

Growth is dominated by `events` table (~150–200 MB/day). Neon's Basic plan
allows dumps of any size; the pg_dump wire-format has no upper bound. The
"6.6 GB inside Neon's limits" framing in Charlie's ask was speculative —
the actual pain point is upload reliability, not database size.

---

## Options (cheapest → most hardening)

### Option A — bump `rclone rcat` retry budget (CHEAPEST)

**Change:** add flags to the existing rcat call.

**Current:**
```
rclone rcat "$DEST" --log-level INFO --log-file "$LOG_FILE"
```

**Proposed:**
```
rclone rcat "$DEST" \
    --log-level INFO --log-file "$LOG_FILE" \
    --retries 10 --retries-sleep 30s \
    --low-level-retries 20
```

- 09-04 failed after `2 errors` — rcat's default `--retries=3` gives up in
  under a minute of B2 flapping. B2's `no tomes available` typically clears
  in 2–15 min per Backblaze status history; 10 retries × 30s sleep gives
  a 5-minute window.
- Zero new moving parts. One-line diff. No new files, no new plist.
- **Doesn't fix:** dumps that take 5000s can still be lost if B2 is unhealthy
  the whole night. Also, a stream failure still discards the whole dump
  (no partial resume).

**Cost to ship:** 5 min.
**Recovery on next failure:** unchanged (dump lost, re-run tomorrow).
**Wound guard:** [[monitor_health_writer_reader_shared_source]] — canary
already reads the B2 listing, so a still-failed upload is detected in ≤24h.

### Option B — spool to disk, then `rclone copy` (RESUMABLE)

**Change:** stop using `rcat`. Write dump to a temp file on local disk,
then upload with `rclone copy` (which resumes partial uploads and has
chunked retry semantics for large files).

**Sketch:**
```
TMPDUMP="/Volumes/DockVault/pg_backup_tmp/${DUMP_NAME}"
mkdir -p "$(dirname "$TMPDUMP")"
PGOPTIONS='-c statement_timeout=0' \
  pg_dump -Fc --no-owner --no-acl "$DUMP_URL" -f "$TMPDUMP"
rclone copy "$TMPDUMP" "$DEST_PREFIX/" --log-level INFO --log-file "$LOG_FILE" \
    --retries 10 --low-level-retries 20 --checksum
rm -f "$TMPDUMP"
```

- **Wins:** `rclone copy` uses multipart with per-chunk retries. A B2 503
  during chunk 47 of 130 re-uploads chunk 47, not the whole file. Dump work
  (5000s) is decoupled from upload work — if upload fails, dump is preserved
  on disk and next run can retry the upload without re-dumping.
- **Requires:** 7 GB free on the disk holding `TMPDUMP`. DockVault SSD is
  ideal (external, isolated from mac_startup_disk). Adds a `df` precheck.
- **New failure mode:** if rm fails, uncleaned tmp files accumulate. Add a
  `trap` cleanup and a pre-run cleanup of any pre-existing tmp.
- **Doesn't fix:** one bad `events` table row still tanks the whole dump.

**Cost to ship:** 30 min (spool logic + df precheck + trap cleanup + one full
dry-run to prove the tmp path).
**Recovery on next failure:** dump preserved on disk, upload retriable
without re-dumping.

### Option C — split by table (BLAST-RADIUS ISOLATION)

**Change:** two dumps per night — `events` alone, and everything-else.

**Sketch:**
```
pg_dump -Fc ... --table=events -f events.dump
pg_dump -Fc ... --exclude-table=events -f rest.dump
```

- **Wins:** if the `events` dump fails, `rest` (schema + all other tables
  + walker state + snapshots + auth) is still safe. A 6.6 GB dump lost is
  6.6 GB of context lost; a 200 MB `rest.dump` lost is far less painful.
  Also parallelizable in a future round.
- **Cost:** restore procedure grows a step. Docs need a note. Prune keeps
  N × 2 files. Two rclone uploads per night = two chances for B2 flap.

**Cost to ship:** 90 min (split, restore docs, prune bump, canary awareness
of two files vs one).
**Recovery on next failure:** partial failures don't lose everything;
`rest.dump` still holds daily.

### Option D — client-side chunking (`split | rclone`) (MAX HARDENING)

**Change:** pipe pg_dump through `split` into fixed-size chunks and upload
each with independent retries.

- **Wins:** any single chunk can retry independently. Best possible upload
  resilience.
- **Costs:** new restore procedure (`cat` before pg_restore), chunk-file
  bookkeeping, more surface area to test. Full reassembly must be verified
  by canary. Overkill for a 6.6 GB nightly on a currently 1-in-30-fail rate.

**Cost to ship:** ~3 hours + updated canary + restore-test rewrite.
**Recommendation:** don't ship. Come back if Options A+B don't hold.

---

## Recommendation

**Ship A + B together.** Both are additive; both are self-contained; combined
they turn a "one 503 wipes the night" failure into "one 503 wipes the upload
attempt, next run resumes." Option C is fine follow-up if events dump time
starts dominating cadence, but the 09-04 signature was upload-side, not
dump-side, so blast-radius framing isn't the primary risk today.

Neither option ships tonight — Charlie asked for a proposal, not a
build. Awaiting his pick before code lands.

**Non-obvious pre-flight (Option B):** the `TMPDUMP` path must not live
on the Mac startup disk. That disk hits ~200 GB free routinely and a
7 GB write could push cache/log processes to disk-full. DockVault is
the right home; if DockVault is offline the shim should fall back to
skip-with-loud-log, not silently write to `/tmp`.
