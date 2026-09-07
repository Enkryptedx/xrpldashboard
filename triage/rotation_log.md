# Credential rotation log

Append-only. One line per rotation. No secret values.

2026-09-07 · DATABASE_URL (Neon `neondb_owner`) rotated · reason: leaked into JJ transcript via pg_backup argv on 2026-09-07 morning (fragment `npg_nuI2*` exposed) · argv root-cause fixed in commit `4d9650e` (wrapper now passes creds via PGHOST/PGPASSWORD env vars, never argv) · new password verified alive on Mac + Lenovo + Render (`healthz /db=reachable` in 830ms) · old dead on Neon (functional proof: 5-alert cascade — `check_walker_stale`, `check_walker_failing`, `check_walker_findings`, `check_snapshot_missed`, `check_sovereignty_loss`, plus BetterStack `heartbeat-age` — fired within 30s of the Neon reset and cleared as each surface picked up the new value) · rotation walkthrough at `triage/DATABASE_URL_ROTATION_2026-09-07.md`.
