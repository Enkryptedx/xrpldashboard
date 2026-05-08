"""Tests for the Postgres scaffold (db.py).

The scaffold is dormant by default — these tests pin that behavior so a
future change can't accidentally activate Postgres for users who haven't
configured it.
"""

import pytest

import db


@pytest.fixture
def no_database_url(monkeypatch):
    """Ensure DATABASE_URL is unset for the test, regardless of the
    real environment. Pinned per-test to avoid leaking into siblings."""
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def fake_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost:5432/xrpld")


class TestPgUrl:
    def test_unset_returns_none(self, no_database_url):
        assert db.pg_url() is None

    def test_blank_returns_none(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "")
        assert db.pg_url() is None

    def test_whitespace_returns_none(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "   ")
        assert db.pg_url() is None

    def test_set_returns_value(self, fake_database_url):
        assert db.pg_url() == "postgresql://user:pw@localhost:5432/xrpld"


class TestPgAvailable:
    def test_unset_returns_false(self, no_database_url):
        # Default state: Postgres scaffold is dormant. The site runs on
        # SQLite snapshots and this test guards that promise.
        assert db.pg_available() is False

    def test_set_but_no_psycopg(self, fake_database_url, monkeypatch):
        # If psycopg isn't installed, pg_available must still be False
        # so callers don't try to connect to a missing driver.
        monkeypatch.setattr(db, "psycopg", None)
        assert db.pg_available() is False


class TestPgConnect:
    def test_raises_when_unconfigured(self, no_database_url):
        with pytest.raises(RuntimeError, match="not configured"):
            with db.pg_connect():
                pass


class TestSchemaDdl:
    def test_ddl_is_string(self):
        assert isinstance(db.SCHEMA_DDL, str)

    def test_ddl_creates_events_table(self):
        # Light structural check — the migration story still owns this,
        # but the DDL must at least name the tables we promise. Table
        # names match the SQLite originals (singular `token_volume`,
        # `amm_pool_events`) so the dual-write code is symmetrical.
        assert "CREATE TABLE IF NOT EXISTS events" in db.SCHEMA_DDL
        assert "CREATE TABLE IF NOT EXISTS token_volume" in db.SCHEMA_DDL
        assert "CREATE TABLE IF NOT EXISTS amm_pool_events" in db.SCHEMA_DDL


class TestDualReadFallback:
    """The whole point of the scaffold is that the existing SQLite path
    keeps working when Postgres is dormant. These tests pin that the
    /, /whales path still produces valid output without DATABASE_URL."""

    def test_whale_events_helper_runs_without_postgres(self, no_database_url):
        import app as app_module

        out = app_module._recent_whale_events(limit=3)
        # Either [] (empty DB) or a list of resolved event dicts —
        # the contract is "doesn't raise."
        assert isinstance(out, list)

    def test_homepage_still_renders(self, client, no_database_url):
        # Regression guard: importing db.py must not break the homepage
        # for users who haven't configured Postgres.
        r = client.get("/")
        assert r.status_code == 200
