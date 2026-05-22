# Storage Audit Rule

**Rule.** Any file in `.gitignore` that is read by request-handling code MUST
have a Postgres-first cascade. The file path is the **dev fallback**, not
the production source.

## Why

Render builds from `git`. Gitignored files are absent on Render. Code that
calls `_load_json_safe(path)` at module import sees an empty list/dict and
keeps serving — no 500, no log, just **silent zero**: a page that renders
fine but reports nothing. The user only notices when they cross-check two
pages and find them inconsistent.

The architecture (`project_xrpldashboard_architecture.md`) is: Mac runs
the workers and writes both to Neon Postgres and to local files; Render
reads only from Neon. Anything that lives only in the local files dies
silently on Render.

## Known instances (closed)

| File | Read by | Fix commit |
|---|---|---|
| `volumes.db` | `token_data.py` (RPR 0-trades bug) | volumes.db PG fix |
| `amm_index.json` | `token_data.py`, `wallet_data.py` (RLUSD 0 pools, LP labels) | `c521814` |

## The pattern

1. Add `db.read_<thing>()` that returns `None` when `pg_available()` is
   False (signals fall-through to the file) and **raises** on PG query
   failure. Production should crash loudly, not serve silent zeros.
2. Wire the consumer:
   ```python
   def _load_thing():
       entries = db.read_thing()
       if entries is not None:
           return entries
       return _load_json_safe(LOCAL_PATH) or DEFAULT
   ```
3. Keep module-init load (not per-request): if PG fails at boot, the
   import fails and the service refuses to start — a far louder signal
   than per-request silent fallback.

## Symptom shape to watch for

If a page says "0 X" or "no activity" and another page on the same site
says "many X", check whether the data source is gitignored. The mismatch
between `/tokens` (PG, correct) and `/token/<cur>/<iss>` (gitignored
SQLite, zero) is the canonical example of this class.

## Audit cadence

When adding any new `.gitignore` entry for a data file, grep for reads:

```
grep -rn "your_new_file.json\|your_new_file.db" --include="*.py" .
```

If anything in `app.py` / `*_data.py` reads it, the Postgres mirror must
exist before the gitignore line lands.
