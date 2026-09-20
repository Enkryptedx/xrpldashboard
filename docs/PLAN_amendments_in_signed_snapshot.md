# Plan — per-amendment vote tallies in the daily signed snapshot

**Filed**: 2026-09-20 · JJ
**Trigger**: CryptoSlate press citation (2026-09-18) of `/amendments` for
LendingProtocol / SingleAssetVault vote counts. Verification of the cited
"13 of 35" and "16 of 35" was **unverifiable** because we don't archive
VHS vote tallies. See `docs/PRESS_CITATIONS.md`.
**Blocker**: schema change — waits for the 2026-09-25 sellable-surface
freeze; after freeze any change to the signed-envelope schema bumps
`schema_version`.

---

## What changes

Add an `amendments` block to the daily signed-snapshot envelope so any
future press citation of `/amendments` is checkable against a tamper-evident
record.

```jsonc
// envelope.amendments (NEW)
{
  "as_of_ledger_index": 107119535,
  "as_of_close_time_iso": "2026-09-20T14:19:52Z",
  "responding_node_source": "lenovo_tunnel",           // or "s1.ripple.com"
  "unl_source": "vl.ripple.com",
  "threshold_display": "28/35",                         // exactly what page shows
  "per_amendment": {
    "LendingProtocol": {
      "hash": "565B90CA1AB2B9D4...",
      "enabled": false,
      "supported_by_responding_node": true,
      "network_votes": {"count": 13, "validations": 35, "threshold": 28}
    },
    "LendingProtocolV1_1": {
      "hash": "A360E2BFD775A5B0...",
      "enabled": false,
      "supported_by_responding_node": false,             // key press-verifiable field
      "network_votes": {"count": null, "validations": null, "threshold": 28}
    },
    // ... one entry per amendment in the VHS response ...
  }
}
```

## Why

- Any press citation of a vote count now has a tamper-evident daily
  reference point (VHS values captured at the moment the snapshot leaf
  was signed).
- `supported_by_responding_node` catches the "in Majorities but the
  responding node doesn't recognize the hash" case that the Sep 18
  CryptoSlate article correctly flagged for LendingProtocolV1_1.
- `responding_node_source` labels sovereign vs public so cited figures
  can be qualified as "served from Lenovo" vs "served from public RPC".

## Data sources (already in codebase)

- `amendments_state.fetch_amendments_state()` — combines `feature` RPC
  + `Amendments` ledger object; source of `enabled` / `supported`.
- `amendments_network_votes.fetch_network_vote_tallies_cached()` —
  VHS-scoped tallies; source of `count` / `validations` / `threshold`.
- `xrpl_client.get_client()` returns the responding node URL —
  source of `responding_node_source`.

## Where the code change lands

- `signed_snapshot.py`: assemble the `amendments` block BEFORE the
  envelope-sign step. Pull from the two live-fetch modules above so we
  match what `/amendments` was showing at snapshot time.
- Cache-freshness guard: reject the write if either source's cache is
  older than 600s (2× TTL) — a stale-cache snapshot would misattribute
  vote counts to a ledger that already advanced past them. Fail loud
  (`walker_health_end(ok=False)`), don't silent-noop (per Tier-0 rule
  `telemetry_fail_loud`).
- `schema_version`: bump 4 → 5. Verifier tooling must ignore
  `amendments` block on old leaves.

## When

**AFTER 2026-09-25 sellable-surface gate.** Rationale:
- The freeze locks the sovereign-source declaration for each surface.
  `responding_node_source` in the envelope must exactly match the frozen
  declaration.
- Before the gate the `envelope.amendments` shape is subject to review;
  after the gate any change requires a schema-version bump.

## Verifier changes

- `chain.json` verifier — add `amendments` field-presence check (leaves
  from before schema-version bump have no `amendments` key; leaves
  after must have it).
- Public verifier docs (`/methodology`) — add a paragraph documenting
  what the field means and how to cross-check a press citation.

## Test plan

- Unit: assemble the block from mocked VHS + `feature` responses;
  assert JSON shape matches spec.
- Integration: force the mocked cache to 700s stale; assert the writer
  refuses (walker_health_end ok=False) and the leaf is not written.
- Regression: parse a pre-schema-5 leaf, assert `amendments` key absent
  and verification still passes.
- Sanity: after first live write, hand-verify against `/amendments`
  page snapshot at the same wall-clock second.

## Related

- `docs/PRESS_CITATIONS.md` — first citation that motivated this.
- `docs/anchor_history.md` — anchor cadence & leaves referenced here.
- `docs/methodology.md` — public-facing spec of what's in the envelope.
