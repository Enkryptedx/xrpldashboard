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

## Step 0 — Pre-rotation baseline: FULL credential-location inventory + endpoint list

**2026-09-07 post-hoc fix**: the original checklist only enumerated one env-var (`DATABASE_URL`) and tested one endpoint per host. That MISSED `DATABASE_URL_DIRECT` on the Mac and caused `db.pg_connect()` (which prefers the direct URL) to fail post-rotation. This step is now the load-bearing discipline — no rotation begins until this inventory is filled in for the current state, and no rotation is "done" until every entry in the inventory is re-tested.

### 0a — Codebase inventory (JJ runs, no secret touched)

Grep the repo for every env-var name the code could read as a Neon credential:

```
grep -rhoE "os\.environ\.get\(['\"]([^'\"]+)['\"]|os\.environ\[['\"]([^'\"]+)['\"]|os\.getenv\(['\"]([^'\"]+)['\"]" --include="*.py" | \
  grep -oE "['\"]([^'\"]+)['\"]" | sort -u | \
  grep -iE "DATABASE|POSTGRES|^['\"](PG[A-Z_]+)|NEON|PGPASSWORD"
```

As of 2026-09-07, that returns: `DATABASE_URL`, `DATABASE_URL_DIRECT`, `NEON_DATABASE_URL`. If a future refactor adds a fourth name, this grep catches it and the inventory table below expands.

### 0b — Per-host inventory (JJ runs, name-only reporting; test each with SELECT 1)

For every host, enumerate every place a Neon credential could live and record the current state. Tests are `SELECT 1` through the exact var/path — not just one representative check.

**Mac (`Charlies-Mac-mini`):**
| Location | Check | Command shape (JJ runs) |
|---|---|---|
| `~/.config/xrpldashboard/env` | `DATABASE_URL` | `psycopg.connect(os.environ['DATABASE_URL']).cursor().execute('SELECT 1')` |
| `~/.config/xrpldashboard/env` | `DATABASE_URL_DIRECT` | same shape, `DATABASE_URL_DIRECT` |
| `~/.config/xrpldashboard/env` | `NEON_DATABASE_URL` | same shape, `NEON_DATABASE_URL` (may be NOT SET) |
| `~/.config/xrpldashboard/env` | any other var whose name matches the codebase-inventory grep | ditto |
| `~/.pgpass` | file existence + `psql -h ... -U neondb_owner ...` | verify libpq honors it |
| `~/Library/LaunchAgents/*.plist` | scan `EnvironmentVariables` blocks for any Neon-credential key | `plutil -p` each plist |
| `xrpl_test/launchd/*.sh` | grep for `postgresql://neondb_owner` hardcoded URLs | `grep -l` all wrapper scripts |

**Lenovo (`rippled-node`):**
| Location | Check |
|---|---|
| `~/.config/xrpldashboard/env` | every `DATABASE|POSTGRES|PG|NEON` var name (usually just `DATABASE_URL` today) |
| `~/.pgpass` | file existence |
| `/etc/systemd/system/xrpld-*.service` | scan for `Environment=` and `EnvironmentFile=` — today the services source env inside `ExecStart` via bash, so no separate EnvironmentFile= directive. If that changes, this row catches it. |

**Render (Charlie reads the dashboard; JJ names what the code expects):**
| Dashboard var name | Set on Render? (Charlie confirms) |
|---|---|
| `DATABASE_URL` | (fill in) |
| `DATABASE_URL_DIRECT` | (fill in) |
| `NEON_DATABASE_URL` | (fill in) |
| (any other name from the 0a grep) | (fill in) |

For each hit that IS set, verify via `curl -s xrpldashboard.com/healthz` after each Render env change. Render doesn't expose per-var SELECT 1 the way a Mac terminal does; healthz is the closest proxy.

### 0c — Codebase reader-path inventory (JJ runs)

The names above are what the code CAN read. In practice `db.pg_connect()` uses `DATABASE_URL_DIRECT` if set, else `DATABASE_URL`. `_get_writer_conn` uses `DATABASE_URL_DIRECT` if set, else `DATABASE_URL`. Raw `psycopg.connect(os.environ['DATABASE_URL'])` uses whatever the caller hands it. **Test every reader-path independently after the edit** — passing a plain `SELECT 1` through `DATABASE_URL` says nothing about whether `db.pg_connect()` (which reads `DATABASE_URL_DIRECT`) will work.

### 0d — Fill inventory before starting Step 1

Copy the tables above into a scratchpad. Fill in the "Set / NOT SET" and "current state" columns before proceeding. Every entry that comes back "set" is one row that must be re-tested after the rotation. Every entry that comes back "NOT SET" is a row that stays unset (or the operator chooses to leave a note for a future decision).

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

## Step 2 — Update Mac env file — EVERY Neon-credential VAR from the inventory (Charlie's keyboard)

**2026-09-07 post-hoc fix**: the original step said "the DATABASE_URL line." That singular framing hid the fact that Charlie's Mac env file has multiple Neon-credential vars (`DATABASE_URL` pooler AND `DATABASE_URL_DIRECT` non-pooler). Both must land the new value in this step — one nano session, one save, both lines updated. Same rule applies to any additional var name discovered by the Step 0 codebase-inventory grep.

Charlie:
1. Open `~/.config/xrpldashboard/env` in your editor.
2. For EACH Neon-credential var identified in Step 0b (typically `DATABASE_URL` and `DATABASE_URL_DIRECT`; sometimes `NEON_DATABASE_URL`):
   - Use nano's Ctrl-W to jump to the var by name.
   - Ctrl-W again to `@ep-steep-tree` — cursor lands on the `@` boundary.
   - Backspace the old password to the LEFT until the char just left of the cursor is `:` (the colon after `neondb_owner`).
   - Type or paste the new value.
3. Save + exit (Ctrl-O, Enter, Ctrl-X).
4. Reply "mac updated" — no content.

JJ proof (runs SELECT 1 through EVERY var set on the Mac, not just one):
```
$ set -a && source ~/.config/xrpldashboard/env && set +a
$ python3 -c "
import os, psycopg
for name in ['DATABASE_URL', 'DATABASE_URL_DIRECT', 'NEON_DATABASE_URL']:
    v = os.environ.get(name)
    if not v: print(f'{name}: NOT SET'); continue
    try:
        psycopg.connect(v, connect_timeout=8).cursor().execute('SELECT 1')
        print(f'{name}: OK')
    except Exception as e:
        print(f'{name}: FAIL — {type(e).__name__}: {str(e).split(chr(10))[0][:120]}')
"
$ python3 -c "
import db
with db.pg_connect() as conn:
    with conn.cursor() as cur:
        cur.execute('SELECT 1')
        print('db.pg_connect: OK')
"
```

Every set var must return OK. The `db.pg_connect()` check is separate and non-optional — that's the read path most walkers use, and it exercises the `DATABASE_URL_DIRECT` preference which raw psycopg.connect(DATABASE_URL) does not.

---

## Step 3 — Update Lenovo env file — EVERY Neon-credential VAR from the inventory + restart long-lived walkers (Charlie's keyboard, ssh)

Same discipline as Step 2 — every var identified in Step 0b's Lenovo row gets updated in one nano session. Today that's typically just `DATABASE_URL`; if the Step 0 grep discovers a `DATABASE_URL_DIRECT` or `NEON_DATABASE_URL` on Lenovo in the future, this step catches them.

Charlie:
1. `ssh rippled-node`
2. `nano ~/.config/xrpldashboard/env` — for EACH Neon-credential var, replace the password segment. Same nano search pattern.
3. Save + exit.
4. **Restart the always-on walkers that hold long-lived Postgres connections.** Today that's only `xrpld-xrpl-stream.service` (persistent write path). Timer-driven walkers (`xrpld-anchor-canary`, `xrpld-cross-check-walker`, `xrpld-l1-pager`, `xrpld-l2-inspector`, `xrpld-ledger-definitions-walker`) source env on each fire and self-recover — no restart needed.
   ```
   sudo systemctl restart xrpld-xrpl-stream.service
   sudo systemctl status xrpld-xrpl-stream.service | head -5
   ```
   Confirm `Active: active (running)`.
5. Reply "lenovo updated" — no content.

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
