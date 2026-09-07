# XRPL Token Registry — Taxonomy CHANGELOG

All notable changes to the taxonomy vocabulary land here. This log is
authoritative — the version-bump policy in [taxonomy_v1.md](./taxonomy_v1.md#version-bump-policy)
dictates when a change is PATCH / MINOR / MAJOR.

Every entry names the effective date, the reasoning, and (for anything
above PATCH) the count of existing tokens the change reclassifies.

## 1.0.0 — 2026-09-07 (draft, awaiting Charlie's approval before public)

Initial vocabulary. Not counted as a "change" from any prior public
version — the registry did not have an approved taxonomy before this
date. Tokens that were labeled under a private working vocabulary
between 2026-08-30 and 2026-09-07 were migrated to the closest v1
equivalent (see `docs/registry/MIGRATION_2026-09-07.md` when it ships).

### Categories (12 real + 2 review-status + 2 mechanical flags)

- **Real categories (12):** `stablecoin_regulated`, `stablecoin_gateway`,
  `native_utility_chain`, `dex_utility`, `defi_lending`, `defi_yield`,
  `gaming`, `wrapped_bridge`, `rwa`, `lp_token`, `memecoin`, `community`.
- **Review-status values (2):** `not_yet_reviewed` (default — no curator
  has looked), `reviewed_unlabeled` (curator confirmed no category fits).
- **Mechanical orthogonal flags (2):** `ticker_collision`,
  `non_standard_code`. Attach to any row regardless of category.

### Design decisions ruled 2026-09-07

- **`not_yet_reviewed` vs `reviewed_unlabeled` split.** Prior single
  `unlabeled` value overclaimed curatorial attention — it read as if
  the registry had inspected every row and confirmed no category fit,
  when in fact ~10k rows had never been reviewed by a human at all.
  Split into two honest states so the site never conflates "we looked,
  no category applies" with "no one has looked yet." Coverage-gauge
  counts them separately.

- **`memecoin` rule rewritten.** Prior draft was self-contradictory
  ("never inferred from patterns" / "may be curator-inferred when the
  issuer address, currency name, and lack of any Domain / toml jointly
  rule out other categories"). Now: machines never infer memecoin. A
  human curator may assign it with `tier=curator-inferred` and their
  reasoning in the citation. That is the whole rule — no mechanical
  fallback, no pattern-match shortcut.

- **Curator authority + successor path.** Charlie Bruce is primary
  curator; JJ holds the queue for automated triage; editorial successor
  is designated by Charlie (to-be-named). If Charlie is unreachable for
  >48 hours, JJ holds the queue in read-only status and disputes are
  logged with "acknowledged, awaiting editorial review."

- **Version-bump policy.** PATCH for wording / boundary clarifications
  that reclassify zero tokens. MINOR for new category added or a
  boundary widened/narrowed that moves tokens. MAJOR for renames,
  removals, or shape restructuring — MAJOR bumps require a 30-day
  preview period where the daily signed snapshot ships BOTH the current
  and next payloads.

### Reclassification counts (initial migration)

TBD once the migration walker runs against production. Placeholder
entry so the successor can see the shape a real MINOR entry takes.
