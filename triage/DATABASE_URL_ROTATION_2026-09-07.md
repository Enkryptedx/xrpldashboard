# DATABASE_URL rotation — Neon postgres password

**Prepared:** 2026-09-07 · JJ · Charlie executes alone; JJ verifies only via `SELECT 1`
**Trigger:** Password fragment `npg_nuI2*` leaked into JJ transcript via `ps aux | grep pg_dump` (2026-09-07 morning). Wrapper was passing DATABASE_URL as positional argv to pg_dump, visible to any `ps` reader. Root-cause wrapper fix landed in commit `4d9650e` — argv now excludes secrets. Rotation is because the leaked value is now in the transcript, therefore compromised.

**Rules JJ follows during this walkthrough:**
- **JJ never types, sees, or asks for the new password.** Not in chat, not in commands, not in file paths.
- Every step has an OBSERVABLE proof step that doesn't require JJ to see the secret. Charlie confirms "done + here's the ping result."
- No automation of any permission dialog or credential paste. Full stop.

**⚠️  OUTAGE WINDOW WARNING (2026-09-07 post-hoc fix — walkthrough bug caught mid-execution).**

Step 1 (Neon `Reset password`) invalidates the OLD password INSTANTLY on Neon's side. **From that moment until every downstream surface (Mac / Lenovo / Render) has been updated, all connections using the old password fail with `password authentication failed`.**

Expected collateral during the outage window (roughly Steps 1 → 4):
- BetterStack `heartbeat-age` alert (Render web app can't write heartbeat to DB)
- `check_walker_stale` / `check_walker_failing` (Mac walkers lose DB, can't checkpoint)
- `check_walker_findings` (no fresh walker output for the /check surface to read)
- `check_snapshot_missed` (upcoming signed_snapshot has no fresh data to anchor)
- `check_sovereignty_loss` (sovereignty gauge reads a walker that's silent)

**None of these are real incidents. They are the mechanical consequence of rotating the shared secret while surfaces still hold the old value.** Acknowledge them in your pager + BetterStack as "known — password rotation in progress" BEFORE starting Step 1, and expect them to clear on their own within 5-15 minutes of Step 4 completion as each walker's next natural cycle picks up the new env.

If you cannot tolerate any outage window, the alternative is a two-step Neon flow: create a SECOND role with a new password, update every surface to use the second role, then delete the first role. Neon supports this. This walkthrough does not — it takes the outage in exchange for keeping one production role.

---

## Step 0 — Pre-rotation baseline (JJ runs, no secret touched)

Charlie: none. JJ documents:
- Current Neon project + database: from URL host `ep-steep-tree-ajz0h6nv.c-3.us-east-2.aws.neon.tech` → Neon project `steep-tree`, region `us-east-2`.
- Places DATABASE_URL is stored (need updating): (1) Mac `~/.config/xrpldashboard/env`, (2) Lenovo `~/.config/xrpldashboard/env` (mirror), (3) Render env-var `DATABASE_URL` for the web service.
- Places using DATABASE_URL indirectly: pg_backup wrapper (sourced from env file), every walker + web app on Render + Lenovo (all source from env at start).

---

## Step 1 — Neon auto-generates a new password; Charlie copies it (Charlie's keyboard)

**Neon generates the new password server-side and shows it once. Charlie does not invent or type one — the only human action is clicking Reset and copying the shown value to paper.** Do not bring a candidate passphrase into this step; Neon replaces it with its own high-entropy value.

Charlie:
1. Log into Neon (https://console.neon.tech).
2. Project `xrpldashboard` → Roles → user `neondb_owner` → **Reset password**.
3. Confirm the reset. Neon displays the freshly-generated password ONCE with a "shown only once" warning.
4. Copy that value to paper (or your usual paste buffer — DO NOT paste into any chat surface or file that syncs).
5. Reply "copied" — no content, just the word.

JJ waits. Does not ask for the value.

---

## Step 2 — Update Mac env file (Charlie's keyboard)

Charlie:
1. Open `~/.config/xrpldashboard/env` in your editor.
2. Update the `DATABASE_URL=postgresql://neondb_owner:<NEW>@ep-steep-tree-ajz0h6nv.c-3.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require` line — replace `<NEW>` with the value from Step 1.
3. Save.
4. Reply "mac updated" — no content.

JJ proof (Charlie pastes back ONLY the pass/fail line, not the URL):
```
$ set -a && source ~/.config/xrpldashboard/env && set +a
$ /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -c "import os; import psycopg; psycopg.connect(os.environ['DATABASE_URL']).cursor().execute('SELECT 1')"
```
Expected output: nothing (silent success) OR a single traceback line if wrong. Charlie pastes back the last line only. If silent, we're good.

---

## Step 3 — Update Lenovo env file (Charlie's keyboard, ssh)

Charlie:
1. `ssh rippled-node`
2. `nano ~/.config/xrpldashboard/env` — same DATABASE_URL line, replace `<NEW>` with the Step 1 value.
3. Save + exit.
4. Reply "lenovo updated" — no content.

JJ proof (Charlie runs, pastes back only the pass/fail):
```
$ source ~/.config/xrpldashboard/env
$ psql "$DATABASE_URL" -c 'SELECT 1'
```
Expected output: `?column?\n 1\n(1 row)`.

---

## Step 4 — Update Render env var (Charlie's keyboard, Render dashboard)

Charlie:
1. https://dashboard.render.com → the xrpldashboard web service → Environment.
2. Find `DATABASE_URL`, click edit, paste the new value.
3. Save. Render will trigger a redeploy — 60-90s.
4. Reply "render updated" when redeploy shows Live.

JJ proof (JJ runs — no secret touched):
```
$ curl -s https://xrpldashboard.com/healthz | jq .db_connect
```
Expected: `"ok"` or a matching healthy-shape. If timeouts or `"unhealthy"`, the Render env didn't update or password was pasted wrong.

---

## Step 5 — Prove the OLD password is DEAD (Charlie's keyboard)

Charlie: **JJ never sees the old value.** But Neon should have invalidated it in Step 1's reset. To prove:
1. Open a fresh psql prompt with an obviously-wrong DATABASE_URL that uses the OLD leaked fragment (Charlie constructs from paper if needed — do NOT type in chat).
2. Attempt: `psql "postgresql://neondb_owner:<OLD>@..." -c 'SELECT 1'`
3. Expected: `FATAL: password authentication failed for user "neondb_owner"` — that's the goal.
4. Reply "old dead" when confirmed.

If old password STILL WORKS, Neon didn't rotate — repeat Step 1.

---

## Step 6 — Kick the walkers + verify no stale connections (JJ runs)

JJ:
1. `launchctl kickstart -k gui/$(id -u) com.charliebruce.xrpldashboard.pg_backup` — the new wrapper (argv-free) will run with the new password from env.
2. Watch spool file grow within 2 min (per new TCC preflight + watchdog).
3. Confirm `walker_health` rows post-kick don't show `password auth failed` errors.
4. Confirm token_issuer_flags_walker's next scheduled fire uses the new creds.

Charlie: nothing. JJ pastes the pass/fail summary.

---

## Step 7 — Remove the compromised value from anywhere it may linger

Charlie:
1. Delete any local scratch files, browser paste-history, or notes-app entries that contain the old password. **Include this transcript if you can — but that's already shipped to Anthropic, so the rotation itself is the actual mitigation.**
2. Optional: check macOS Keychain / Chrome autofill for auto-saved Neon creds and clear.

JJ: nothing. This step exists so the paper-trail matches the intent.

---

## Post-rotation record

JJ writes a single line to `memory/rotation_log.md` (creates if absent):
```
2026-09-07 · DATABASE_URL (Neon neondb_owner) rotated · reason: leaked into JJ transcript via pg_backup argv · verified alive on Mac + Lenovo + Render · old dead on Neon
```

No secret value in the log — just the fact of the rotation.

---

## What this rotation does NOT do

- Doesn't rotate Neon connection strings for the pooler URL (`-pooler.` variant) if it's a different credential — check the Neon dashboard.
- Doesn't rotate CF-Access tokens, Ripple RPC keys, or any other secret in the env file. Those are separate rotation cycles.
- Doesn't retroactively scrub the JJ transcript — that's already shipped to Anthropic and beyond our reach.

**Any additional secrets adjacent to DATABASE_URL that Charlie sees while editing the env file are noticed in silence — JJ doesn't ask about them.**
