"""is_bot_canary: behavioural rules re-derived, independence preserved.

WHY THIS EXISTS (2026-11-05 BetterStack incident, 16:41 UTC)
------------------------------------------------------------
PR #131 added db.refresh_bot_hash_tables() rules 3 and 4, which tag
per-VISITOR shapes that no path or UA fragment can express:

    rule 3  entire history = one no-referrer pageview of /contact
    rule 4  an aocr.org referrer onto /claims/<id>

The canary's `humans-column` arm counts `is_bot IS NULL`, which reflects
those rules. Its `humans-predicate` arm re-evaluated the legacy UA/path
predicate, which does not. So the two arms measured different things and the
gap grew with every tagging run, reaching delta=-341 (3.79%) over the
trailing 7d and paging BetterStack. The page was a false alarm about an
IMPROVEMENT: rule 3 alone accounted for 1,154 correctly-stamped rows.

THE FIX AND THE TRAP IT AVOIDS
------------------------------
The first proposal was to have the predicate arm exclude visitors found in
`page_view_bot_hashes`. That would have been wrong, and the code already
said so:

    "This is the permanent authoritative comparison arm.
     Never replace with a table-subquery path; that would defeat the canary."

`page_view_bot_hashes` is the writer's OUTPUT. Joining it makes both arms
agree by construction, so the canary could no longer catch a writer that
stamps the WRONG rows -- the one failure it exists to detect.

So rules 3 and 4 are RE-DERIVED from `page_views` instead. Same rule
definition (constants imported from db, so the two implementations cannot
drift on meaning), independent computation (from raw rows). That is the same
distinction scanner_pred already draws: reading a shared EVIDENCE table is
fine, reading a shared STAMP table is not.

Hermetic: string inspection plus a stubbed pair of counts. No DB, no network.
"""
from __future__ import annotations

import os
import sys

from psycopg._queries import PostgresClientQuery
from psycopg.adapt import Transformer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (HERE, os.path.join(HERE, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import db  # noqa: E402
import is_bot_canary as canary  # noqa: E402


class TestIndependenceIsPreserved:
    """The whole point: the predicate arm must not read the writer's output."""

    def test_predicate_never_reads_the_stamp_table(self):
        frag, _ = canary._legacy_bot_pred()
        assert "page_view_bot_hashes" not in frag, (
            "the predicate arm must re-derive, not read the writer's stamp "
            "table -- doing so makes the canary tautological"
        )

    def test_evidence_table_is_still_allowed(self):
        """scanner_pred reads page_view_scanner_combos_confirmed by design.
        Guards against someone 'fixing' independence by deleting that too."""
        frag, _ = canary._legacy_bot_pred()
        assert "page_view_scanner_combos_confirmed" in frag

    def test_wrong_stamp_still_fails_the_canary(self, monkeypatch):
        """THE REGRESSION TEST Charlie asked for.

        Simulate a writer that stamped a genuine human row as bot. The
        column therefore reports one fewer human than the independent
        re-derivation. The canary MUST still fail -- if it passed here, the
        arms would be reading the same source and the canary would be dead.
        """
        monkeypatch.setattr(canary, "_count_human_column", lambda *a: 8_665)
        monkeypatch.setattr(canary, "_count_human_predicate", lambda *a: 9_006)
        ok, detail = canary._check_window(None, "trailing-7d", 0, 1)
        assert ok is False, detail
        assert "MISMATCH" in detail
        assert "over-stamps" in detail

    def test_under_stamp_also_fails(self, monkeypatch):
        """The mirror case: writer missed bots the re-derivation catches."""
        monkeypatch.setattr(canary, "_count_human_column", lambda *a: 9_006)
        monkeypatch.setattr(canary, "_count_human_predicate", lambda *a: 8_665)
        ok, detail = canary._check_window(None, "trailing-7d", 0, 1)
        assert ok is False, detail
        assert "under-stamps" in detail


class TestBehaviouralBotIsNotDrift:
    def test_agreeing_arms_are_green(self, monkeypatch):
        """With rules 3/4 re-derived, a correctly-tagged behavioural bot is
        excluded by BOTH arms, so it produces no delta and no page. This is
        the 2026-11-05 false alarm, fixed."""
        monkeypatch.setattr(canary, "_count_human_column", lambda *a: 8_665)
        monkeypatch.setattr(canary, "_count_human_predicate", lambda *a: 8_665)
        ok, detail = canary._check_window(None, "trailing-7d", 0, 1)
        assert ok is True
        assert "delta=0 OK" in detail

    def test_the_incident_delta_would_now_be_within_tolerance(self, monkeypatch):
        """341 rows was 3.79% while only one arm excluded them. Once both
        do, the residual is ambient cache noise, which the 1% band absorbs."""
        monkeypatch.setattr(canary, "_count_human_column", lambda *a: 8_665)
        monkeypatch.setattr(canary, "_count_human_predicate", lambda *a: 8_700)
        ok, detail = canary._check_window(None, "trailing-7d", 0, 1)
        assert ok is True
        assert "WITHIN-TOLERANCE" in detail


class TestRederivationShapeAndSafety:
    def test_contact_rule_shape_is_rederived(self):
        frag, _ = canary._legacy_bot_pred()
        assert "COUNT(DISTINCT path) = 1" in frag
        assert "MIN(path) = %s" in frag
        assert "referrer IS NOT NULL AND referrer <> ''" in frag

    def test_claims_rule_shape_is_rederived(self):
        frag, _ = canary._legacy_bot_pred()
        assert "SUBSTRING(referrer FROM 'https?://([^/]+)') = ANY(%s)" in frag

    def test_rule_definition_comes_from_db_constants(self):
        """Params must carry db's own constants, so the canary and the writer
        cannot drift on what the rule MEANS while computing it separately."""
        _, params = canary._legacy_bot_pred()
        assert db.CONTACT_SCANNER_PATH in params
        assert db.CLAIMS_SCANNER_PATH_PREFIX + "%" in params
        assert list(db.CLAIMS_REFERRER_HOSTS) in params

    def test_fragment_renders_through_psycopg(self):
        """PR #132 lesson: a bare '%.' in SQL handed to a parameterized
        execute() crashed the daily report. Both new patterns are
        parameters, so no literal % may enter the SQL."""
        frag, params = canary._legacy_bot_pred()
        sql = f"SELECT COUNT(*) FROM page_views WHERE ts >= %s AND ts < %s {frag}"
        PostgresClientQuery(Transformer()).convert(sql, [1, 2] + params)
