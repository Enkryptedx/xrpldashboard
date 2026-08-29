# nft_activity http-rpc reach failure — post-sovereignty-restore residual

**Filed**: 2026-08-16 10:32 EDT (session close-out)
**Status**: Open thread, deferred to fresh cleanroom session
**Safety**: L1 sovereignty_loss pager now catches this every ~20 min tick

## The residual signature

After the sovereignty restoration at 2026-08-16 10:09 EDT (Python Local
Network TCC grant + WARP removal), five walkers came clean:

| walker | fallback rows (last 2 min, post-restore) |
|---|---|
| escrow_walker | 0 |
| oracle_walker | 0 |
| rlusd_refresher | 2 (tail; near-clean) |
| cold_storage | on natural cadence (piggyback on daily_snapshot) |
| escrow_supply | on natural cadence (piggyback on signed_snapshot) |
| **nft_activity_walker** | **77 rows in 90 s post-isolated-kick** |

All 77 rows share `reason='unreachable:ConnectError'` — every one a
TCP-connect failure to `192.168.40.95:5005` (the local sovereign
rippled). Not DNS, not handshake, not schema: the socket cannot reach
the peer at all.

Confirmed clean-of-environmental-causes at 10:30 EDT:
- WARP tunnel disconnected (`warp-cli disconnect` OK)
- `systemextensionsctl list` → **0 extension(s)** (no lingering WARP
  kernel filter)
- Python Local Network TCC grant confirmed via popup click at 10:04 EDT
  — the same grant that fixed escrow/oracle/rlusd

So it isn't the same-shape bug as the 60-day sovereignty flood.

## Distinguishing facts vs the five clean siblings

Plists compared 2026-08-16 10:23 EDT: identical Python 3.14 Framework
(`/Library/Frameworks/Python.framework/Versions/3.14/bin`), identical
wrapper shape.

**One env-var difference**:
`nft_activity_walker.plist` carries an extra
`<string>http://192.168.40.95:5005</string>` env entry — plausibly
`LOCAL_NODE` or `RIPPLED_URL`. None of the five clean walkers have
that entry.

That entry strongly implies nft_activity uses an **HTTP-RPC** fetch path
to the local node, where escrow/cold/oracle/rlusd/nft_supply use
WebSocket via `xrpl_client.py`. Two separate code paths under the
walker's roof, only one of which the sovereignty fix healed.

## Starting hypotheses for the fresh cleanroom

Cleanroom framing: fresh context, walkers stable, pager on the wall.
Read the code cold. Ranked most → least likely:

1. **HTTP vs WebSocket path split**. The walker's HTTP client (probably
   `httpx.Client` or `requests.Session`) may bind, resolve, or reuse
   connections differently than the async WS transport in
   `xrpl_client.py`. Investigation start: grep `LOCAL_NODE`,
   `RIPPLED_URL`, and the walker's own module for the `POST /` shape.
2. **TCC grant binary-scoped**. macOS Local Network TCC grants are
   per-executable-path. If the HTTP path shells out or uses a helper
   binary distinct from Framework Python (unlikely but possible), the
   grant wouldn't cover it.
3. **Connection-pool reuse / keep-alive**. HTTP keep-alive may cache a
   dead socket from the pre-TCC-grant era; a client-recreate might
   clear it. Weaker after the 10:22 EDT canary was a fresh kick — but
   a persistent Session at module scope could still hold stale peers.
4. **Port range under load**. nft_activity fetches at higher rate than
   the others (200-ledger batches × per-token RPC). Ephemeral port
   exhaustion / TIME_WAIT accumulation would produce `ConnectError`
   on a bound source. Cross-references rider #1 below.

## Safety context — why this is filed, not on fire

- **L1 sovereignty_loss pager (shipped tonight, Play 2)** now catches
  nft_activity every ~20 min tick. If the residual sustains, the pager
  fires within the 6h / 12h-sustain window. The anti-60-days lives.
- **Backfill machinery covers the data gap.** `walker_node_fallback`
  records every cascaded call; nft's own historical backfill mode can
  replay any range against s2-clio. Coverage stays ≥95.6% for the
  4.4% residual-holes window — worst case widens by hours, not days.
- **Zero cascade risk to the other five walkers.** They read WS via a
  distinct code path; a bug in the HTTP-RPC lane doesn't touch them.

## Two riders folded in

### Rider 1 — EADDRNOTAVAIL / port exhaustion under load

The Neon-side TCP failure signature seen in
`nft_activity_walker.out.log` tail (10:24–10:25 EDT) — DNS resolves to
3 Neon IPs but all three TCP connects fail with a mix of
timeout / RST / server-closed — may be the **same underlying bug's
symptom under load**, not an independent Neon thread. High walker
throughput could exhaust the ephemeral-port range
(`sysctl net.inet.ip.portrange.first` / `.last`), producing
`Can't assign requested address` at connect time for BOTH the local
rippled and Neon writes.

If the walker uses a source-bind (`local_address=` in httpx), that
narrows the source-port pool from 16 K to a per-IP subset, magnifying
the exhaustion.

Diagnostic-when-cleanroom-opens: grep the walker log for any
`EADDRNOTAVAIL` / `[Errno 49]` line during a run. If present, the port
theory promotes to top hypothesis and the fix is a client-config
adjustment (drop source-bind, or raise portrange, or tighten
keep-alive TTL).

### Rider 2 — schema-drift meta-bug (instrumentation that itself fails)

The walker's stdout tail contained a `SCHEMA-DRIFT: relation
"walker_node_fallback" does not exist` traceback from `db.py:6093` on
its failover-Postgres write path. Meaning: **when the local rippled
was unreachable AND the walker tried to log the failure to its local
failover DB, the log-write ALSO failed** because the failover DB has
no `walker_node_fallback` table.

The Neon path DOES have the table (that's why we can query 60 days of
fallback history). So today it's silent-degraded — the primary Neon
insert succeeds, the local mirror insert throws, the exception is
swallowed. No data loss for the pager's purposes, but a real bug.

Fits the **silent-success inventory** taxonomy exactly: instrumentation
that itself has an untested failure mode. Own fix ticket:
"add `walker_node_fallback` DDL to local failover DB init script, wire
to the walker's own `init_dbs.py` for idempotency, cover in the
schema-drift guardrail." Not urgent — no user impact, no data loss —
but it's the kind of bug the resource-watchmen ship rules out.

## When it reopens

Trigger: **fresh cleanroom session, walker-work slot** (currently
staged after cutover #3 in the standing queue). Not tonight, not
tomorrow. Bug is contained and observable; the pager is the safety net.

## Related files

- `deploy/keyboard_bundle_2026-08-16.md` — the bundle whose execution
  surfaced and (re)opened this thread as its own file
- `docs/REPORT_2026-08-16_b3_canary_and_assumption_inventory.md` — full
  incident report including WARP false-cause and TCC actual-cause
  narratives
- `docs/SILENT_SUCCESS_INVENTORY.md` — rider #2 joins this inventory
- `docs/RESOURCE_WATCHMEN_DESIGN.md` — the gauge design that would have
  caught rider #1 (port pressure) as an assumption GAP
