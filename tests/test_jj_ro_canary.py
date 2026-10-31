"""Hermetic tests for the jj_ro read-only path canary.

No database, no network, no credential. Every test injects its own fake
connector or fake file, so this runs identically on a laptop and on a CI
runner with no secrets configured.

The centrepiece is ``test_password_containing_slash_...``: it locks the
exact bug that made the 2026-10-30/31 investigation take two wrong turns.
A URI parser truncates the authority at the first ``/``, so a password
containing ``/`` yields a short, non-resolving "host" and a bogus
``gaierror`` that is indistinguishable from a real DNS outage. The
wrapper this canary monitors does NOT behave that way, so the canary must
not either -- otherwise it reports the read-only path as broken while the
wrapper works fine.
"""
from __future__ import annotations

import os
import sys
import unittest
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jj_ro_canary as canary  # noqa: E402


HOST = "ep-steep-tree-ajz0h6nv-pooler.c-3.us-east-2.aws.neon.tech"


class FakeCursor:
    def __init__(self, row, raises=None):
        self._row = row
        self._raises = raises

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        if self._raises is not None:
            raise self._raises
        self.sql = sql

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, row=(1,), raises=None):
        self._row = row
        self._raises = raises
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True
        return False

    def cursor(self):
        return FakeCursor(self._row, self._raises)


# ── parsing ───────────────────────────────────────────────────────────────

class ParseTests(unittest.TestCase):

    def test_plain_connection_string(self):
        p = canary.parse_ro_conninfo(
            f"postgresql://jj_ro:simplepass@{HOST}/neondb?sslmode=require"
        )
        self.assertEqual(p["host"], HOST)
        self.assertEqual(p["user"], "jj_ro")
        self.assertEqual(p["password"], "simplepass")
        self.assertEqual(p["dbname"], "neondb")
        self.assertEqual(p["port"], 5432)
        self.assertEqual(p["sslmode"], "require")

    def test_password_containing_slash_still_yields_correct_host(self):
        """THE regression guard. A URI parser gets this wrong; we must not.

        This is the shape that produced two false "DNS resolution failure"
        verdicts during the incident.
        """
        raw = f"postgresql://jj_ro:ab/cd/ef@{HOST}/neondb?sslmode=require"

        p = canary.parse_ro_conninfo(raw)
        self.assertEqual(p["host"], HOST)
        self.assertEqual(p["password"], "ab/cd/ef")
        self.assertEqual(p["dbname"], "neondb")

        # Prove the naive approach really does fail on this input, so this
        # test documents a genuine difference rather than a hypothetical.
        self.assertNotEqual(urlparse(raw).hostname, HOST)

    def test_password_containing_at_uses_first_separator_like_the_wrapper(self):
        """The wrapper splits on the FIRST '@'. Mirror it, don't "improve" it.

        A URI parser splits on the LAST '@'. If this canary did that, it
        would authenticate differently from the wrapper and could pass
        while the wrapper fails.
        """
        raw = f"postgresql://jj_ro:pa@ss@{HOST}/neondb"
        p = canary.parse_ro_conninfo(raw)
        self.assertEqual(p["password"], "pa")
        self.assertEqual(urlparse(raw).hostname, HOST)
        self.assertNotEqual(p["host"], HOST)

    def test_explicit_port_is_discarded_matching_wrapper(self):
        p = canary.parse_ro_conninfo(f"postgresql://jj_ro:x@{HOST}:6543/neondb")
        self.assertEqual(p["host"], HOST)
        self.assertEqual(p["port"], 5432)

    def test_scheme_optional(self):
        p = canary.parse_ro_conninfo(f"jj_ro:x@{HOST}/neondb")
        self.assertEqual(p["host"], HOST)

    def test_query_string_stripped_from_dbname(self):
        p = canary.parse_ro_conninfo(
            f"postgresql://jj_ro:x@{HOST}/neondb?sslmode=require&application_name=z"
        )
        self.assertEqual(p["dbname"], "neondb")

    def test_empty_raises_value_error(self):
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                canary.parse_ro_conninfo(bad)

    def test_missing_userinfo_raises_value_error(self):
        with self.assertRaises(ValueError):
            canary.parse_ro_conninfo(f"postgresql://{HOST}/neondb")

    def test_parsed_password_is_never_in_returned_host(self):
        p = canary.parse_ro_conninfo(f"postgresql://jj_ro:secret/v@{HOST}/neondb")
        self.assertNotIn("secret", p["host"])


# ── error classification ──────────────────────────────────────────────────

class ClassifyTests(unittest.TestCase):

    def test_known_classes(self):
        cases = {
            "connection failed: SASL authentication failed": "sasl_auth_failure",
            'password authentication failed for user "jj_ro"': "password_auth_failure",
            "channel binding is required but not supported": "channel_binding_failure",
            "no pg_hba.conf entry for host": "pg_hba_rejection",
            'role "jj_ro" does not exist': "role_or_db_missing",
            "nodename nor servname provided": "dns_resolution_failure",
            "could not translate host name": "dns_resolution_failure",
            "timeout expired": "connect_timeout",
            "Operation timed out": "network_timeout",
            "Connection refused": "connection_refused",
            "server closed the connection unexpectedly": "connection_closed",
            "certificate verify failed": "tls_certificate_error",
            "permission denied for table page_views": "permission_denied",
            "too many connections for role": "connection_limit",
        }
        for msg, expected in cases.items():
            self.assertEqual(canary.classify_error(msg), expected, msg)

    def test_unknown_falls_back(self):
        self.assertEqual(canary.classify_error("something odd"), "unclassified_error")

    def test_empty_and_none_safe(self):
        self.assertEqual(canary.classify_error(""), "unclassified_error")
        self.assertEqual(canary.classify_error(None), "unclassified_error")

    def test_sasl_detected_case_insensitively(self):
        self.assertEqual(canary.classify_error("SASL AUTH"), "sasl_auth_failure")


# ── reading the value ─────────────────────────────────────────────────────

class ReadTests(unittest.TestCase):

    def test_environment_wins(self):
        got = canary.read_ro_raw(
            environ={"DATABASE_URL_RO": "postgresql://a:b@h/d"},
            env_path="/nonexistent",
        )
        self.assertEqual(got, "postgresql://a:b@h/d")

    def test_missing_everywhere_returns_none_not_failure(self):
        self.assertIsNone(
            canary.read_ro_raw(environ={}, env_path="/nonexistent/jj_env")
        )

    def test_blank_env_var_treated_as_absent(self):
        self.assertIsNone(
            canary.read_ro_raw(environ={"DATABASE_URL_RO": "   "},
                               env_path="/nonexistent")
        )

    def test_parsed_from_env_file_with_export_and_quotes(self):
        import io

        lines = [
            "# comment\n",
            "\n",
            'export DATABASE_URL_RO="postgresql://jj_ro:p/w@h/neondb"\n',
            "OTHER=ignored\n",
        ]

        def opener(_path):
            return io.StringIO("".join(lines))

        got = canary.read_ro_raw(environ={}, env_path="x", opener=opener)
        self.assertEqual(got, "postgresql://jj_ro:p/w@h/neondb")

    def test_unreadable_file_is_a_skip_not_a_crash(self):
        def opener(_path):
            raise OSError("permission denied")

        self.assertIsNone(
            canary.read_ro_raw(environ={}, env_path="x", opener=opener)
        )


# ── the check itself ──────────────────────────────────────────────────────

class RunCheckTests(unittest.TestCase):

    RAW = f"postgresql://jj_ro:p/w@{HOST}/neondb?sslmode=require"

    def test_ok_path(self):
        captured = {}

        def connect(**params):
            captured.update(params)
            return FakeConn(row=(1,))

        ok, detail = canary.run_check(connect, self.RAW)
        self.assertTrue(ok)
        self.assertEqual(detail["status"], "ok")
        self.assertIn("checked_at_utc", detail)
        # connected with keyword params, and with the right host
        self.assertEqual(captured["host"], HOST)
        self.assertEqual(captured["sslmode"], "require")

    def test_none_raw_is_skip_not_failure(self):
        def connect(**_):
            raise AssertionError("must not connect when there is nothing to test")

        ok, detail = canary.run_check(connect, None)
        self.assertIsNone(ok)
        self.assertEqual(detail["status"], "skipped")

    def test_connect_failure_is_classified(self):
        def connect(**_):
            raise RuntimeError("connection failed: SASL authentication failed")

        ok, detail = canary.run_check(connect, self.RAW)
        self.assertFalse(ok)
        self.assertEqual(detail["error_class"], "sasl_auth_failure")
        self.assertEqual(detail["exception_type"], "RuntimeError")

    def test_query_failure_is_classified(self):
        def connect(**_):
            return FakeConn(raises=RuntimeError("permission denied for table x"))

        ok, detail = canary.run_check(connect, self.RAW)
        self.assertFalse(ok)
        self.assertEqual(detail["error_class"], "permission_denied")

    def test_unexpected_row_is_failure(self):
        def connect(**_):
            return FakeConn(row=(42,))

        ok, detail = canary.run_check(connect, self.RAW)
        self.assertFalse(ok)
        self.assertEqual(detail["error_class"], "unexpected_select_result")

    def test_malformed_string_reported_as_malformed_not_outage(self):
        def connect(**_):
            raise AssertionError("must not connect on a malformed string")

        ok, detail = canary.run_check(connect, "garbage-with-no-at-sign")
        self.assertFalse(ok)
        self.assertEqual(detail["error_class"], "malformed_connection_string")

    def test_detail_never_contains_the_credential(self):
        secret = "sup3rsecret/slash"
        raw = f"postgresql://jj_ro:{secret}@{HOST}/neondb"

        def connect(**_):
            raise RuntimeError("connection failed: SASL authentication failed")

        _ok, detail = canary.run_check(connect, raw)
        blob = repr(detail)
        self.assertNotIn(secret, blob)
        self.assertNotIn(HOST, blob)


class ContractTests(unittest.TestCase):

    def test_walker_name_and_cadence_are_stable(self):
        self.assertEqual(canary.WALKER_NAME, "jj_ro_canary")
        self.assertEqual(canary.CADENCE_SECONDS, 300)

    def test_host_walker_invokes_the_canary(self):
        """No new schedule: the canary must ride an existing tick."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(here, "mac_tunnel_health_canary.py")).read()
        self.assertIn("jj_ro_canary", src)


if __name__ == "__main__":
    unittest.main()
