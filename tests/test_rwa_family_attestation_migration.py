"""Regression tests for ensure_rwa_family_attestation_current — the
rwa_family boot-time reconciliation migration.

Motivating incident (2026-09-23 Wed 15:35 → 15:57 ET agreement probe):
commit b038214 targeted `family_slug='ondo_finance'` in the migration
WHERE clause, but the actual `rwa_family` row is slugged `'ondo'`. The
migration was a permanent no-op — the /rwa pill would have kept showing
'labeled' after boot, contradicting the manifest's 'verified' tier.

The two slug namespaces at play:
  - rwa_family / rwa_pool_attribution / verify_rwa_families.FAMILIES
      use: 'ondo', 'openeden', 'midas'
  - rwa_supply_nav_daily / rwa_nav_sources.yaml / _FAMILY_DISPLAY_ORDER
      use: 'ondo_finance', 'openeden', 'midas'
They diverge only on Ondo; each side is internally consistent. Mixing
them (as b038214 did) silently breaks the migration.

Charlie ruling 2026-09-23 15:57 ET: "Fix the rwa_family slug bug with a
test on the canonical slug."
"""

from __future__ import annotations

import inspect
import re

import pytest

import db


CANONICAL_RWA_FAMILY_SLUGS = {"ondo", "openeden", "midas"}


def _slugs_targeted_by_migration() -> set[str]:
    """Parse family_slug string literals out of the migration's SQL.

    Matches `family_slug='<slug>'` and `family_slug = '<slug>'` with any
    inner whitespace. Skips docstring/comment mentions by requiring the
    equals sign — WHERE-clause form only."""
    src = inspect.getsource(db.ensure_rwa_family_attestation_current)
    return set(re.findall(r"family_slug\s*=\s*'([^']+)'", src))


class TestEnsureRwaFamilyAttestationCurrent:
    def test_targets_only_canonical_rwa_family_slugs(self):
        targeted = _slugs_targeted_by_migration()
        assert targeted, (
            "expected at least one family_slug='...' clause in "
            "ensure_rwa_family_attestation_current"
        )
        bogus = targeted - CANONICAL_RWA_FAMILY_SLUGS
        assert not bogus, (
            f"Migration targets non-existent rwa_family slug(s): "
            f"{sorted(bogus)}. Canonical rwa_family slugs are "
            f"{sorted(CANONICAL_RWA_FAMILY_SLUGS)} (per "
            f"scripts/verify_rwa_families.FAMILIES). If you meant to "
            f"touch rwa_supply_nav_daily's namespace, use its own "
            f"migration — never mix the two slug spaces."
        )

    def test_never_targets_ondo_finance(self):
        """Regression pin for commit b038214: 'ondo_finance' belongs to
        the rwa_supply_nav / display-order slug space, NOT rwa_family."""
        assert "ondo_finance" not in _slugs_targeted_by_migration(), (
            "rwa_family uses 'ondo' as Ondo's canonical slug. "
            "'ondo_finance' is a different namespace (see docstring "
            "and Charlie ruling 2026-09-23 15:57 ET)."
        )

    def test_no_op_when_pg_unavailable(self, monkeypatch):
        """The migration must be safe to call in envs without Postgres
        (local dev, unit-test process). Never raises, never connects."""
        called = {"pg_connect": 0}

        def _boom():
            called["pg_connect"] += 1
            raise AssertionError("pg_connect should not be reached")

        monkeypatch.setattr(db, "pg_available", lambda: False)
        monkeypatch.setattr(db, "pg_connect", _boom)
        db.ensure_rwa_family_attestation_current()  # must not raise
        assert called["pg_connect"] == 0

    def test_guard_clause_keeps_migration_idempotent(self):
        """The migration must include `AND attestation_level='labeled'`
        so it becomes a no-op after taking, and never silently downgrades
        a manual curator action from verified back to labeled."""
        src = inspect.getsource(db.ensure_rwa_family_attestation_current)
        assert "attestation_level='labeled'" in src, (
            "guard clause missing — migration would keep re-firing and "
            "could clobber curator downgrades. See Charlie ruling "
            "2026-09-23 15:35 ET docstring in db.py."
        )


class TestCanonicalSlugsAllowlistStaysInSync:
    """If someone adds a new rwa_family in scripts/verify_rwa_families,
    the canonical-slug allowlist above needs to move too — otherwise the
    first test would false-positive block a legitimate new slug."""

    def test_verify_rwa_families_matches_allowlist(self):
        import sys, pathlib
        scripts_dir = pathlib.Path(db.__file__).parent / "scripts"
        sys.path.insert(0, str(scripts_dir))
        try:
            from verify_rwa_families import FAMILIES
        finally:
            sys.path.remove(str(scripts_dir))
        slugs = {f[0] for f in FAMILIES}
        assert slugs == CANONICAL_RWA_FAMILY_SLUGS, (
            f"verify_rwa_families.FAMILIES slugs {sorted(slugs)} != "
            f"test allowlist {sorted(CANONICAL_RWA_FAMILY_SLUGS)}. "
            f"Update CANONICAL_RWA_FAMILY_SLUGS in this test AND audit "
            f"rwa_family rows to confirm the new/removed family_slug is "
            f"reflected in the DB before merging."
        )
