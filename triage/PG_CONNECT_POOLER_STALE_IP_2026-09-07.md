# db.pg_connect + Neon pooler URL + hostaddr override → intermittent 'password authentication failed'

**Observed:** 2026-09-07 15:50 EDT, ~2h after the DATABASE_URL rotation.

## Symptom

`db.pg_connect()` fails intermittently with `password authentication failed for user 'neondb_owner'` when the DATABASE_URL is the Neon `-pooler` variant. Same URL used with `psycopg.connect(url, ...)` directly (no hostaddr override) works every time.

Multiple failures observed against different IPs (`3.21.66.37`, `18.216.137.125`) — suggests `_resolve_ipv4_hostaddr()` is picking different pooler-tenant IPs across calls, and only SOME of them respond correctly to the current password.

## Isolating experiment

```
# WORKS (raw connect, no hostaddr):
python3 -c "import os, psycopg; psycopg.connect(os.environ['DATABASE_URL']).cursor().execute('SELECT 1')"
→ OK

# FAILS (via db.pg_connect, which sets hostaddr):
python3 -c "import db; conn = db.pg_connect().__enter__(); conn.cursor().execute('SELECT 1')"
→ FAIL: password authentication failed for user 'neondb_owner'
```

Only difference in the failing path: `hostaddr=<ipv4>` kwarg. Neon's pooler uses SNI-based routing at the TLS layer; when you dial a specific IP but present a hostname SNI, some pooler tenants at that IP appear to route to a Postgres backend that has not received the rotated credential.

## Impact

Any walker or web request that uses `db.pg_connect()` may fail sporadically. Symptoms Charlie has already seen post-rotation:
- `signed_snapshot.py --dry-run` → `walker_health_summary got 0 rows` (a different downstream failure suggesting some walkers can't write)
- `signed_registry_snapshot.py` → `registry_state PG read failed` (my smoke test tonight)

Web reads through `db.pg_connect()` on Render will hit this if Render's `_resolve_ipv4_hostaddr()` picks the wrong IP.

## Suggested fixes (do not apply without Charlie's ruling)

### Option A — skip hostaddr for pooler URLs
```python
_url = ...
_v4 = _resolve_ipv4_hostaddr(_url)
_extra = {}
if _v4 and "-pooler" not in _url:
    # Neon pooler routes by SNI; hostaddr override breaks it
    _extra["hostaddr"] = _v4
```

### Option B — drop hostaddr entirely
The `_resolve_ipv4_hostaddr` was added for IPv6 vs IPv4 disambiguation in some deploys. Modern psycopg / libpq handle this well without the override. If A/B tests show the override is no longer needed, remove it.

### Option C — use DATABASE_URL_DIRECT for pg_connect
`DATABASE_URL_DIRECT` is honored by pg_connect for the non-pooler endpoint. If Charlie sets `DATABASE_URL_DIRECT` to the direct (non-pooler) endpoint, pg_connect uses that instead. Direct endpoint doesn't SNI-route the same way; hostaddr is safer there.

Recommend Option A as the minimal fix — it preserves hostaddr for the non-pooler case where the override was originally useful.

## Related

- `db.py::pg_connect` (uses hostaddr)
- `db.py::_resolve_ipv4_hostaddr`
- Rotation walkthrough doc `triage/DATABASE_URL_ROTATION_2026-09-07.md`
  (uses the non-pooler URL as its template — should mention that
  Charlie's actual env may hold the pooler URL and both work with raw
  psycopg.connect but only the non-pooler variant works cleanly with
  db.pg_connect today)
