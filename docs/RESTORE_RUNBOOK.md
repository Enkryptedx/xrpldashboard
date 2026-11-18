# Restore runbook — Neon dump → vanilla Postgres

Written 2026-11-18. There was no restore runbook before this; the weekly
`pg_restore_test` was the only place the procedure existed, as code.

## The one non-obvious step: strip Neon's `pg_attver`

A Neon dump **will not restore into vanilla Postgres as-is.** Neon stamps a
per-column `pg_attver` into `pg_attribute.attoptions` for every column added
through its metadata-only `ADD COLUMN` fast path, and `pg_dump` faithfully
emits it:

```sql
ALTER TABLE ONLY public.page_view_bot_hashes
    ALTER COLUMN pattern_owned SET (pg_attver=2739598001400);
```

`pg_attver` is not an upstream attoption, so **any** vanilla Postgres — not
just an older one — rejects it:

```
ERROR:  unrecognized parameter "pg_attver"
```

This is not a version mismatch. Prod is Neon 17.12 and the tools are 17.10,
but vanilla 17.12 would reject it identically. It first bit us on 2026-11-17,
because the 2026-11-14 provenance columns (`pattern_owned`, `window_owned` on
`page_view_bot_hashes`) were the first columns in this database to carry it.
**Every future Neon-added column will carry one too**, so this step does not
go away.

### Do this

Do **not** `pg_restore` the custom-format dump directly. Pipe its SQL through
the filter and apply it with `psql` under `ON_ERROR_STOP=1`, so that
`pg_attver` is dropped and **every other error still aborts the restore**:

```bash
PG_BIN=/opt/homebrew/opt/postgresql@17/bin
"$PG_BIN/createdb" -h 127.0.0.1 -p 5432 -U postgres neondb_restored

"$PG_BIN/pg_restore" --no-owner --no-acl -f - neondb-YYYYMMDDTHHMMSSZ.dump \
  | python3 ~/xrpl_test/tools/neon_dump_filter_cli.py \
  | "$PG_BIN/psql" -h 127.0.0.1 -p 5432 -U postgres \
      -d neondb_restored -v ON_ERROR_STOP=1 -q -f -
```

If you have no Python to hand, the equivalent one-liner is a grep — but
prefer the module, which fails closed if a `pg_attver` statement ever spans
more than one line instead of silently corrupting the stream:

```bash
  | grep -v -E 'SET[[:space:]]*\([[:space:]]*pg_attver[[:space:]]*=' \
```

**What is being removed:** Neon bookkeeping with no data meaning. Never a
constraint, default, index, column or row. `ALTER COLUMN ... SET DEFAULT`,
`SET NOT NULL` and `SET STATISTICS` all pass through untouched — there are
tests pinning exactly that.

## Verify after restoring

```sql
SELECT count(*) FROM token_prices;    -- expect ~415k and rising
SELECT count(*) FROM events;          -- expect ~2.59M and rising
SELECT count(*) FROM unl_snapshots;   -- expect ~346, +1-2/day
SELECT max(snapshot_date) FROM unl_snapshots;  -- within 7 days
```

Counts below production are expected and correct: the dump is a 03:00 UTC
snapshot, so it lags prod by however long ago it was taken. A count *above*
production means you restored the wrong dump.

## TLS note for any script in the repo venv

The venv has no system CA bundle. Any script making an HTTPS call must
`import cert_env` early (it sets `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` from
certifi), or shell out to `curl`, which uses the system trust store. Without
one of those, calls die with `CERTIFICATE_VERIFY_FAILED: unable to get local
issuer certificate` — which silently broke every BetterStack heartbeat and
some real data fetches for weeks before 2026-11-18.

That fixes only *our* missing trust store. A `Hostname mismatch`, or a broken
cert on a third-party issuer TOML, is a real finding and must stay recorded
per domain — never bypassed.
