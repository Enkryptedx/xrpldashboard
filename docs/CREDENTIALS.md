# Credentials runbook

**What this doc is.** Every place a production credential lives, how to
rotate one cleanly across all of them, how to notice when they have drifted,
and how to recover when they have.

**What this doc is not.** A secrets index. No actual credentials live in
this file or in this repo — values are held in Neon, Render, the Mac
worker env file, and 1Password.

**Why this exists.** On 2026-05-17 a Neon password rotation went out of
sync between the Mac worker env and the Render env var. The Mac kept
writing to local JSON snapshots, the Postgres mirror silently rejected
every push, and the `/rwa` Midas card sat at `$0.00` for hours. The
failure was loud in one log file and invisible everywhere else. The
sections below are the institutional memory of that incident.

---

## 1. Touchpoint inventory

The Neon `DATABASE_URL` (role `neondb_owner`) is the credential that has
to stay synchronized across every touchpoint below. There is no other
shared production credential at the moment.

| # | Touchpoint | Lives in | Read by | Updated via |
|---|---|---|---|---|
| 1 | Neon | Neon dashboard, role `neondb_owner` on the project that owns endpoint `ep-steep-tree-ajz0h6nv-pooler` | The credential itself — source of truth | Neon dashboard → Roles → `neondb_owner` → Reset password |
| 2 | Render env var | Render dashboard → xrpldashboard service → Environment → `DATABASE_URL` | Prod Flask web (`app.py`) and any Render-side workers (travel mode) | Render dashboard, click Edit on the row, paste, Save |
| 3 | Mac worker env file | `~/.config/xrpldashboard/env` (chmod 600), line 5 `export DATABASE_URL='…'` | Every launchd worker via its `run_*.sh` wrapper that runs `source "$ENV_FILE"` | `nano ~/.config/xrpldashboard/env`, edit line 5, save |
| 4 | 1Password | Charlie's vault, entry for the Neon production role (entry name TBD by Charlie if not yet created) | Operator memory / backup | 1Password app, edit password field |

A fifth touchpoint may exist if `DATABASE_URL_DIRECT` is ever set (see
`db.py:368`, used by `_get_writer_conn` to prefer Neon's unpooled
endpoint). It is currently unset and the writer falls back to
`DATABASE_URL`. If a future change introduces it, add it as a row here.

### Worker wrappers that source the Mac env file

These all read `~/.config/xrpldashboard/env` at process fork. Listing
them so the rotation procedure can verify all are healthy after a change.

- `launchd/run_rank_amms.sh` — 4-hour AMM TVL rerank
- `launchd/run_xrpl_stream.sh` — long-running AMMCreate / whale stream
- `launchd/run_daily_snapshot.sh` — once-daily aggregate snapshot
- `launchd/run_mpt_snapshot.sh` — MPT registry snapshot
- `launchd/run_mpt_holders_refresh.sh` — MPT holder set refresh
- `launchd/run_amm_tvl_recorder.sh` — top-N AMM TVL time series
- `launchd/run_signed_snapshot.sh` — daily signed proof-of-snapshot

`launchd/run_b2_backup.sh` does **not** source this env file — it
operates on the filesystem via rclone-crypt and uses Backblaze
credentials held in `~/.config/rclone/rclone.conf`, not Neon.

### Worker processes that read DATABASE_URL via `os.environ`

Sourced once at process fork. An in-flight Python process will **not**
pick up changes to the env file — see Recovery, step 5.

- `app.py` — prod web reader
- `db.py` — shared connection helper used by every worker
- `rank_amms.py`, `amm_tvl_recorder.py`, `signed_snapshot.py`,
  `backfill_amm_pools.py`, `daily_twitter_post.py` — workers
- `xrpscan_labels_import.py`, `account_labels_import.py` — one-shot CLIs

---

## 2. Standard rotation procedure

Plain rotation (no compromise suspected, no drift detected). Estimated
time: 5 minutes.

**Save the new password before Neon shows it for the last time.** Neon's
"Reset password" modal displays the new value once. Capture it before
acknowledging or closing the dialog, otherwise you will have to reset
again.

1. Open 1Password to the Neon production role entry and leave it on
   the edit screen, ready to paste. (If no entry exists yet, create
   one before step 2 — the new password is unrecoverable after the
   Neon modal closes.)
2. Open Neon dashboard → the project that owns endpoint
   `ep-steep-tree-ajz0h6nv-pooler` → Roles → `neondb_owner` → Reset
   password.
3. **Immediately** copy the new password from the Neon modal and paste
   it into 1Password's password field. Save 1Password.
4. Open Render dashboard → xrpldashboard service → Environment → find
   `DATABASE_URL` → Edit → paste the full new connection string (entire
   URL, no surrounding quotes, no `export` prefix) → Save Changes.
   Render begins auto-redeploy.
5. On the Mac, edit `~/.config/xrpldashboard/env`:
   ```
   nano ~/.config/xrpldashboard/env
   ```
   On line 5 only, replace the segment between `neondb_owner:` and
   `@ep-steep-tree`. Keep the single quotes, keep everything after `@`
   verbatim. Save (`Ctrl-O`, `Enter`, `Ctrl-X`).
6. Verify shape (catches the 2026-05-17 truncation incident before it
   bites). The line should be ~169 chars and end in
   `&channel_binding=require'`:
   ```
   awk 'NR==5 {print length($0), substr($0,length($0)-25)}' ~/.config/xrpldashboard/env
   ```
7. Test the new credential from a fresh subshell:
   ```
   cd ~/xrpl_test && source ~/.config/xrpldashboard/env && \
     python3 -c "import db; c = db._get_writer_conn(); print('OK' if c else 'FAIL')"
   ```
8. Confirm Render's redeploy completed (Render dashboard → Events) and
   that prod is reachable:
   ```
   curl -sI "https://xrpldashboard.com/health?_=$(date +%s)" | head -1
   ```

In-flight workers will continue running with the **old** password until
their next launchd-scheduled restart. The 4-hour `rank_amms` cycle and
the long-running `xrpl_stream` are the two that may keep failing auth
for hours after the rotation. Either:
- accept the gap (next scheduled restart picks up the new env), or
- kickstart manually:
  ```
  launchctl kickstart -k gui/$UID/com.charliebruce.xrpldashboard.rank_amms
  launchctl kickstart -k gui/$UID/com.charliebruce.xrpldashboard.xrpl_stream
  ```

Kickstarting `rank_amms` mid-run discards roughly 70 minutes of progress
on the next full sweep. Acceptable when fixing drift; avoid otherwise.

---

## 3. Drift detection

Drift is the state where the credential is correct in one touchpoint and
stale or different in another. The symptom space:

| Symptom | Where to look | Command |
|---|---|---|
| Prod data is stale even though local file is fresh | rank_amms log for `writer_connect_failed` or `password authentication failed` | `grep -c "writer_connect_failed" ~/xrpl_test/launchd_logs/rank_amms.out.log` |
| All workers writing locally, nothing reaching Postgres | Any worker log for `postgres mirror failed (non-fatal)` | `grep -h "mirror failed" ~/xrpl_test/launchd_logs/*.log \| tail` |
| Render web returns errors on data pages | Render logs for `OperationalError` | Render dashboard → Logs → filter for `password authentication failed` |
| `/health` shows stale heartbeat for `amm_ranker` | Heartbeat is updated by `_mirror_to_postgres` on success; staleness > 1× rank cycle implies mirror failure | `curl -s "https://xrpldashboard.com/health?_=$(date +%s)" \| grep -A2 amm_ranker` |

The cheapest single signal is the rank_amms log. Mirror failures land as
`writer_connect_failed` once per `SAVE_EVERY=100` pools — roughly every
25 seconds at the steady-state 3.7 pools/sec rate. A healthy run has
zero of these lines per 4-hour cycle.

When `#71` (mirror failure visibility) ships, `_mirror_to_postgres`
will surface consecutive failures to `/health`. Until then, the
rank_amms log is the smoking gun.

### What "expected" looks like

Right after a clean rotation:
```
$ grep -c "writer_connect_failed" ~/xrpl_test/launchd_logs/rank_amms.out.log
98     # historical failures from before the rotation; the count freezes here
```
A growing count is the alarm.

---

## 4. Recovery procedure

When drift has been detected (the situation we hit on 2026-05-17). The
goal is to identify the out-of-sync touchpoint, get it back in sync, and
verify each touchpoint independently before walking away.

### Step 1 — Identify which side is stale

Pull the password (shape only, never the value) from each touchpoint and
compare:

```
# Mac env file — length and trailing fragment only
awk 'NR==5' ~/.config/xrpldashboard/env | \
  python3 -c "
import sys, re
line = sys.stdin.read()
m = re.search(r'neondb_owner:([^@]+)@', line)
if m:
    pw = m.group(1)
    print(f'mac env: {len(pw)} chars, ends ...{pw[-4:]!r}')
else:
    print('mac env: MALFORMED line 5 — see Step 1a')
"
```

In Render: dashboard → Environment → `DATABASE_URL` → click the eye
icon to reveal → eyeball the last 4 chars of the password segment.
Match against the `mac env: ends ...` output above.

In 1Password: open the Neon production role entry, reveal the
password, eyeball the last 4 chars. This is the source of truth for
"what the password is supposed to be" — provided 1Password was last
updated as part of a clean rotation (Section 2 step 3).

**If 1Password matches Neon** and **Mac and Render also match
1Password** → not a drift problem; rotate Neon and start over.

**If 1Password does not match** Mac/Render → 1Password is the stale
record. Update 1Password from whichever live touchpoint still works
(test-connect first to be sure).

**If Mac and Render disagree** → drift confirmed. Pick the one that
test-connects to Neon and propagate to the other.

#### Step 1a — Mac env file is malformed (the 2026-05-17 case)

The 2026-05-17 incident truncated line 5 mid-edit, leaving an orphan URL
tail on line 6. Symptom: `awk 'NR==5' ~/.config/xrpldashboard/env`
shows a line that doesn't end in `&channel_binding=require'`, and
`source` of the env file errors with `unmatched '` at some line.

Repair without retyping the password:
```
python3 - <<'EOF'
path = "/Users/charliebruce/.config/xrpldashboard/env"
with open(path) as f:
    lines = f.read().splitlines()
# If line 5 is truncated (no @host, no closing quote), append the tail.
if not (lines[4].endswith("'") and "@ep-steep" in lines[4]):
    lines[4] = lines[4] + "@ep-steep-tree-ajz0h6nv-pooler.c-3.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require'"
# If line 6 is an orphan URL fragment, drop it.
if len(lines) > 5 and lines[5].startswith("@ep-steep-tree"):
    del lines[5]
with open(path, "w") as f:
    f.write("\n".join(lines) + "\n")
EOF
```

Always `cp` to a `.bak.$(date +%s)` first.

### Step 2 — Propagate the correct password

Source of truth, in order of precedence:
1. 1Password if it test-connects to Neon
2. Whichever live touchpoint (Mac or Render) test-connects
3. If nothing test-connects: Neon → Reset password (Section 2), and
   propagate the new value to all three touchpoints

For each stale touchpoint, follow the relevant numbered step in
Section 2 (step 4 for Render, step 5 for Mac env, step 1 for
1Password). Skip the steps that aren't affected.

### Step 3 — Verify each touchpoint independently

Order matters: verify from the outside in.

a. **Test-connect from a fresh Mac subshell** (so the new env is read
   afresh, not inherited from a stale shell):
   ```
   cd ~/xrpl_test && source ~/.config/xrpldashboard/env && \
     python3 -c "import db; c = db._get_writer_conn(); print('OK' if c else 'FAIL')"
   ```
b. **Confirm Render redeploy succeeded** (dashboard → Events should show
   the redeploy as "Live" with the post-rotation timestamp).
c. **Server-side curl with cache-buster** (browser/agent fetches can
   land on a Cloudflare edge that's hours behind origin):
   ```
   curl -sI "https://xrpldashboard.com/health?_=$(date +%s)" | head -1
   ```
d. **Eyeball /rwa** (the page that surfaced the 2026-05-17 incident):
   ```
   curl -s "https://xrpldashboard.com/rwa?_=$(date +%s)" | grep -oE 'mTBILL|\$[0-9]+\.[0-9]+' | head
   ```

### Step 4 — Confirm the smoking gun has stopped

Wait one mirror-cycle (~25 seconds), then re-check:
```
grep -c "writer_connect_failed" ~/xrpl_test/launchd_logs/rank_amms.out.log
```
The count should be frozen at whatever pre-recovery value it had.
If it is still growing, the in-flight worker is using a stale process
env — see Section 2 on whether to wait for the next launchd cycle or
kickstart manually.

### Step 5 — Update this doc

If the recovery procedure surfaced a new touchpoint, a new symptom, or
a new gotcha, add it before closing the incident. The 2026-05-17
incident itself produced sections 3, 4, 1a above; future incidents
should produce more of the same.

---

## Related task tracking

- `#71` — mirror failure visibility (`_mirror_to_postgres` should
  escalate to `/health` after N consecutive failures, not log silently)
- `#72` — this doc
- `feedback_unrecoverable_secrets_first.md` (memory) — 1Password
  step belongs **before** generating an unrecoverable secret, not
  after. Applied in Section 2 step 1–3.
- `feedback_cdn_verification.md` (memory) — production verification
  must be server-side curl with cache-buster. Applied in Section 4
  step 3.
- `feedback_stale_midrun_snapshot.md` (memory) — when diagnosing a
  worker, verify `finished_at` or `cursor == len(index)` before
  drawing conclusions. Applied in Section 3 by reading the running
  worker's log rather than mid-run JSON output.
