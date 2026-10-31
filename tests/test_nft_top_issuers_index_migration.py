"""Hermetic guard for the 2026-10-31 nft_activity (close_time, issuer) index.

No database, no network, no credential: every assertion reads the committed
migration file and the committed walker source.

Why guard a migration that is already applied? Two reasons:

1. The recorded DDL must keep matching the query it was created for. The
   index is only useful if its leading column is `close_time` -- reversed to
   (issuer, close_time) it cannot serve the 7-day range predicate, which is
   exactly the trap the pre-existing nft_activity_collection_time_idx
   (issuer, taxon, close_time) already fell into. A silent edit to either
   side would quietly restore the 25s timeout.

2. The migration must stay replayable. It is already applied in production,
   so any environment re-running it needs IF NOT EXISTS, and CONCURRENTLY
   must not be wrapped in a transaction block.
"""
from __future__ import annotations

import os
import re
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION = os.path.join(
    HERE, "migrations", "2026_10_31_nft_activity_close_time_issuer_idx.sql"
)
WALKER = os.path.join(HERE, "nft_activity_walker.py")
INDEX_NAME = "nft_activity_close_time_issuer_idx"


def _sql() -> str:
    with open(MIGRATION, encoding="utf-8") as fh:
        return fh.read()


def _statements(sql: str) -> str:
    """The SQL with comment lines stripped, so assertions test real DDL."""
    return "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )


class MigrationFileTests(unittest.TestCase):

    def test_migration_file_exists_and_is_dated(self):
        self.assertTrue(os.path.isfile(MIGRATION), MIGRATION)
        name = os.path.basename(MIGRATION)
        self.assertRegex(name, r"^\d{4}_\d{2}_\d{2}_.+\.sql$")
        self.assertTrue(name.startswith("2026_10_31_"))

    def test_records_the_create_index_statement(self):
        body = _statements(_sql())
        self.assertIn(INDEX_NAME, body)
        self.assertRegex(body, r"(?is)create\s+index\s+concurrently")

    def test_records_the_vacuum_analyze_statement(self):
        body = _statements(_sql())
        self.assertRegex(body, r"(?is)vacuum\s*\(\s*analyze\s*\)\s+nft_activity")

    def test_is_idempotent(self):
        """Already applied in production -- a replay must be a no-op."""
        self.assertRegex(_statements(_sql()), r"(?is)if\s+not\s+exists")

    def test_concurrently_is_not_wrapped_in_a_transaction(self):
        """CREATE INDEX CONCURRENTLY fails inside BEGIN/COMMIT."""
        body = _statements(_sql()).lower()
        self.assertNotIn("begin;", body)
        self.assertNotIn("commit;", body)
        self.assertNotIn("start transaction", body)

    def test_column_order_is_close_time_then_issuer(self):
        """THE load-bearing assertion.

        Reversed, the index cannot serve `close_time >= NOW() - INTERVAL`
        and the top_issuers stage goes back to a parallel heap scan.
        """
        body = _statements(_sql())
        m = re.search(
            r"(?is)" + INDEX_NAME + r".*?on\s+nft_activity\s*\(([^)]*)\)", body
        )
        self.assertIsNotNone(m, "could not find the index definition")
        cols = [c.strip().lower() for c in m.group(1).split(",")]
        self.assertEqual(cols, ["close_time", "issuer"])

    def test_no_credential_or_host_recorded(self):
        """A migrations file is committed to a public repo."""
        sql = _sql()
        self.assertNotIn("neon.tech", sql)
        self.assertNotIn("postgresql://", sql)
        self.assertNotIn("postgres://", sql)
        self.assertNotRegex(sql, r"password\s*=")
        # No private-network addressing either.
        self.assertNotRegex(sql, r"\b192\.168\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(sql, r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_states_it_is_already_applied(self):
        """Stops a future reader treating it as pending work."""
        self.assertRegex(_sql(), r"(?i)already applied")


class QueryAlignmentTests(unittest.TestCase):
    """The index must still match the query that justified it."""

    def test_walker_query_filters_close_time_and_groups_by_issuer(self):
        with open(WALKER, encoding="utf-8") as fh:
            src = fh.read()
        # The top_issuers stage, as committed.
        self.assertIn("tele.timer.stage(\"top_issuers\")", src)
        self.assertRegex(src, r"(?is)select\s+issuer,\s*count\(\*\)\s+as\s+events")
        self.assertRegex(src, r"(?is)from\s+nft_activity")
        self.assertRegex(src, r"(?is)close_time\s*>=\s*now\(\)\s*-\s*interval\s*'7 days'")
        self.assertRegex(src, r"(?is)group\s+by\s+issuer")

    def test_query_still_requires_issuer_not_null(self):
        """Satisfiable from the index tuple; keeps the scan index-only."""
        with open(WALKER, encoding="utf-8") as fh:
            src = fh.read()
        self.assertRegex(src, r"(?is)issuer\s+is\s+not\s+null")


if __name__ == "__main__":
    unittest.main()
