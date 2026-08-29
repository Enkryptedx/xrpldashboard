# Resource watchmen — design (not-yet-shipped)

**Filed** 2026-08-16 per Charlie's Section 4b directive. Ships after
b2 lands + next cleanroom window; not in the current session per the
cleanroom-vs-tail-of-thread rule.

**Purpose**: build cheap L1-style gauges for the resources whose
depletion is otherwise silent until hard-ceiling failure. Blast-radius
of any hitting 100% is site-down; likelihood of any hitting 100%
grows continuously; monitoring is one row per gauge per interval.

**Threshold shape**: page at 80% used (or <30d remaining for
expiries). This mirrors the sovereignty_loss shape — a warning window
before catastrophe, not the "flames visible" alert.

---

## Four gauges, one table

New table:

```sql
CREATE TABLE system_resource (
  ts           timestamptz PRIMARY KEY DEFAULT NOW(),
  host         text NOT NULL,     -- 'mac_mini' | 'lenovo' | 'render'
  gauge        text NOT NULL,     -- 'disk_root' | 'neon_size' | 'cert_days' | 'render_rss' etc.
  value        numeric NOT NULL,  -- percent-used, bytes, days-remaining
  unit         text NOT NULL,     -- 'percent' | 'bytes' | 'days'
  threshold    numeric,           -- 80 for percent, 30 for days
  status       text NOT NULL,     -- 'green' | 'yellow' | 'red'
  detail       jsonb              -- freeform: cert issuer, disk mount, plan tier
);
CREATE INDEX ON system_resource (host, gauge, ts DESC);
```

L1 pager gains one new check: `system_resource_red` — if latest row
for any (host, gauge) is `red` and older than 1h, page. (1h floor so
we don't page mid-gauge-write during a flap.)

---

## Gauge 1: disk (Mac + Lenovo)

**Ship shape**: `tools/disk_gauge.py` — cheap, pure stdlib:

```python
import shutil, socket, json
from datetime import datetime, timezone
import db  # existing helper

HOST = socket.gethostname()   # 'Charlies-Mac-mini' or 'lenovo'
MOUNTS = ["/Users/charliebruce"] if HOST.startswith("Charlies") else ["/", "/var/lib/rippled"]

def check_mount(mount):
    total, used, free = shutil.disk_usage(mount)
    pct = round(100 * used / total, 2)
    status = "red" if pct >= 80 else ("yellow" if pct >= 70 else "green")
    return {"gauge": f"disk_{mount.replace('/', '_').strip('_') or 'root'}",
            "value": pct, "unit": "percent", "threshold": 80,
            "status": status,
            "detail": {"mount": mount, "used_gb": used // 2**30, "free_gb": free // 2**30}}

def main():
    for mount in MOUNTS:
        r = check_mount(mount)
        db.write_system_resource(HOST, r)

if __name__ == "__main__":
    main()
```

**Cadence**: hourly. `launchd` plist on Mac, `systemd` timer on
Lenovo. Both call same script.

**Cost**: one `statvfs` call. ~1ms.

**Sudo needed**: none for read.

---

## Gauge 2: Neon storage quota

**Ship shape**: `tools/neon_quota_gauge.py`:

```python
import os, psycopg
import db

NEON_PLAN_CEILING_BYTES = int(os.environ.get("NEON_PLAN_CEILING_BYTES", 10 * 2**30))  # 10 GB default

def check():
    with psycopg.connect(os.environ["DATABASE_URL"]) as c, c.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        size = cur.fetchone()[0]
    pct = round(100 * size / NEON_PLAN_CEILING_BYTES, 2)
    status = "red" if pct >= 80 else ("yellow" if pct >= 70 else "green")
    return {"gauge": "neon_size", "value": pct, "unit": "percent", "threshold": 80,
            "status": status,
            "detail": {"size_gb": round(size / 2**30, 2), "ceiling_gb": NEON_PLAN_CEILING_BYTES / 2**30}}
```

**Cadence**: nightly (03:00 UTC).

**Env var**: `NEON_PLAN_CEILING_BYTES` — Charlie sets to current plan
ceiling in bytes. Free tier = 512MB = `536870912`. Launch tier = 10GB =
`10737418240`. Neon does NOT publish a "quota-used API" for free plans,
so ceiling comes from env.

**Sudo needed**: none.

**Extension**: also compute growth-slope over last 7d + 30d; project
months-until-full. Add to `detail` JSON. Nice-to-have.

---

## Gauge 3: cert + domain expiry

**Ship shape**: `tools/cert_domain_gauge.py`:

```python
import ssl, socket
from datetime import datetime, timezone
import db

DOMAIN = "xrpldashboard.com"
PORT = 443

def cert_days_remaining(domain, port):
    ctx = ssl.create_default_context()
    with socket.create_connection((domain, port), timeout=5) as s, \
         ctx.wrap_socket(s, server_hostname=domain) as ss:
        cert = ss.getpeercert()
    expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return (expires - datetime.now(timezone.utc)).days

def domain_days_remaining(domain):
    # Uses python-whois; optional dep. If unavailable, return None +
    # log — we still get the cert half.
    try:
        import whois
        w = whois.whois(domain)
        exp = w.expiration_date
        if isinstance(exp, list): exp = exp[0]
        return (exp.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
    except Exception:
        return None

def main():
    cert_days = cert_days_remaining(DOMAIN, PORT)
    dom_days  = domain_days_remaining(DOMAIN)
    for name, days in (("cert_days", cert_days), ("domain_days", dom_days)):
        if days is None: continue
        status = "red" if days <= 30 else ("yellow" if days <= 60 else "green")
        db.write_system_resource("render", {"gauge": name, "value": days, "unit": "days",
                                            "threshold": 30, "status": status,
                                            "detail": {"domain": DOMAIN}})
```

**Cadence**: daily.

**Sudo needed**: none.

**Optional dep**: `python-whois` (or `whois` CLI subprocess if we
don't want a pypi dep). Cert half works without.

---

## Gauge 4: Render memory / RSS

**Ship shape**: two options.

**Option A (in-process, cheap)**: Flask app writes RSS to Postgres
periodically. Add to existing `/healthz` handler or a new lightweight
`@app.before_request` counter that runs every Nth request.

```python
import resource, time
_last_rss_write = 0
def _maybe_log_rss():
    global _last_rss_write
    now = time.time()
    if now - _last_rss_write < 60: return  # once per minute max
    _last_rss_write = now
    rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024  # macOS: bytes; Linux: KB
    render_limit = int(os.environ.get("RENDER_MEMORY_LIMIT_MB", 512)) * 2**20
    pct = round(100 * rss_bytes / render_limit, 2)
    status = "red" if pct >= 80 else ("yellow" if pct >= 70 else "green")
    db.write_system_resource("render", {"gauge": "render_rss", "value": pct,
                                        "unit": "percent", "threshold": 80,
                                        "status": status,
                                        "detail": {"rss_mb": rss_bytes // 2**20}})
```

**Option B (external, cleaner)**: query Render's metrics API from a
Lenovo-side gauge tool. Needs Render API token; cleaner separation but
one more secret to manage.

**Recommendation**: Option A ships first. Free, no new secret.

**Cadence**: written on the request that runs at the 60s mark. If
Flask is quiet enough that no request comes in for >5min, we lose
resolution — acceptable trade for zero new infrastructure.

**Sudo needed**: none.

---

## L1 pager wiring

Add to `tools/l1_pager.py` (~30 lines):

```python
def check_system_resources(now):
    """Read latest row per (host, gauge). Any red older than 1h → alert."""
    with pgbridge.connect() as c, c.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (host, gauge)
                   host, gauge, value, unit, threshold, status, ts, detail
            FROM system_resource
            ORDER BY host, gauge, ts DESC
        """)
        rows = cur.fetchall()
    alerts = []
    for r in rows:
        host, gauge, value, unit, threshold, status, ts, detail = r
        age_min = (now - ts).total_seconds() / 60
        if status == "red" and age_min > 60:
            alerts.append({"type": "system_resource",
                           "host": host, "gauge": gauge,
                           "value": value, "unit": unit,
                           "threshold": threshold, "detail": detail})
    return alerts
```

Format branch in `format_alert()`:

```python
elif alert["type"] == "system_resource":
    return f"🟥 RESOURCE {alert['host']} {alert['gauge']}: {alert['value']}{alert['unit']} (threshold {alert['threshold']})"
```

---

## Deploy order (post-b2)

1. Ship the `system_resource` table (schema migration, additive-only).
2. Ship `disk_gauge.py` on both hosts + launchd/systemd units. Watch
   for 24h. Verify green-when-green.
3. Ship `neon_quota_gauge.py` on Lenovo (nightly).
4. Ship `cert_domain_gauge.py` on Lenovo (daily).
5. Ship `render_rss` self-report in Flask (in-process).
6. Wire L1 pager `check_system_resources()`. Test with a manual
   `UPDATE system_resource SET status='red' WHERE …` → confirm page
   fires within one 20-min pager cycle. Then reset.

**Cost budget**: ~150 lines of code + one table + two systemd units +
two launchd plists + one L1 pager check. ~2-3 hour cleanroom session.

**Not doing today**: because b3 rollback + assumption inventory +
three filings is already the session's ballast. Deploying gauges in
the same window as a rolled-back canary is exactly the tail-of-thread
tired-mistakes pattern the memory flags.

---

## Deferred (design only, no code today)

- **B2 backup quota**: needs B2 API integration. Backup already
  writes bucket-level metadata; adding a gauge is another daily
  script. Design similar to Neon quota. Ship with same batch.
- **Postgres connection pool saturation**: read Neon's pool stats via
  `SHOW max_connections` + `pg_stat_activity` count. Optional; ship
  if we see writer_connect_failed in the wild.
- **Rippled disk usage on Lenovo**: separate from OS disk — the
  rippled data directory has its own growth curve. Track via
  `du -sh /var/lib/rippled` monthly.

---

*Filed 2026-08-16. Ship trigger: post-b2 sovereignty recovery + next
cleanroom session. Owner: TBD.*
