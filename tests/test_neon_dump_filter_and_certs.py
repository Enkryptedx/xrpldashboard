"""pg_attver filter + venv CA bundle.

Two 2026-11-17/18 failures are pinned here.

1. pg_restore_test died with `unrecognized parameter "pg_attver"`. Neon
   stamps `pg_attver` into attoptions for columns added via its
   metadata-only ADD COLUMN fast path (the Nov 14 provenance columns on
   page_view_bot_hashes were the first), pg_dump emits
   `ALTER ... SET (pg_attver=...)`, and ANY vanilla Postgres rejects it. A
   real disaster restore hits the identical wall, so the filter is generic
   by statement rather than by column name.

2. Every in-process TLS call in the repo venv failed
   CERTIFICATE_VERIFY_FAILED for want of a CA bundle -- 38 logged hits over
   30 days across five jobs, including real data fetches (unl_snapshot's
   vl.ripple.com pull, the geoip MaxMind download), not just heartbeats.

Hermetic apart from the one test marked as needing network, which is the
canary Charlie asked for: an HTTPS call from this venv must verify.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_REPO, os.path.join(_REPO, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cert_env  # noqa: E402
from neon_dump_filter import (  # noqa: E402
    PG_ATTVER_RE,
    UnterminatedAttverStatement,
    filter_stream,
)

# The exact statement that broke the 2026-11-17 run.
REAL = ("ALTER TABLE ONLY public.page_view_bot_hashes "
        "ALTER COLUMN pattern_owned SET (pg_attver=2739598001400);\n")


def test_drops_the_real_failing_statement():
    dropped: list[str] = []
    assert list(filter_stream([REAL], dropped)) == []
    assert len(dropped) == 1 and "pg_attver" in dropped[0]


def test_is_generic_not_scoped_to_those_two_columns():
    """The next Neon-added column must need no code change."""
    lines = [
        "ALTER TABLE ONLY public.some_future_table "
        "ALTER COLUMN whatever SET (pg_attver=1);\n",
        "ALTER TABLE ONLY public.another ALTER COLUMN x SET (pg_attver=999);\n",
    ]
    dropped: list[str] = []
    assert list(filter_stream(lines, dropped)) == []
    assert len(dropped) == 2


@pytest.mark.parametrize("variant", [
    "ALTER TABLE t ALTER COLUMN c SET (pg_attver=5);\n",
    "ALTER TABLE t ALTER COLUMN c SET ( pg_attver = 5 );\n",
    "alter table t alter column c set (PG_ATTVER=5);\n",
])
def test_tolerates_whitespace_and_case(variant):
    assert list(filter_stream([variant])) == []


def test_everything_else_passes_through_byte_for_byte():
    keep = [
        "CREATE TABLE public.page_view_bot_hashes (\n",
        "    hash_type text NOT NULL,\n",
        "    pattern_owned boolean DEFAULT false NOT NULL\n",
        ");\n",
        "COPY public.page_view_bot_hashes (hash_type, hash) FROM stdin;\n",
        "visitor\\tabc\n",
        "\\.\n",
        "ALTER TABLE ONLY public.x ADD CONSTRAINT y PRIMARY KEY (z);\n",
        "CREATE INDEX i ON public.x USING btree (y);\n",
        "ALTER TABLE ONLY public.x ALTER COLUMN c SET DEFAULT false;\n",
        "ALTER TABLE ONLY public.x ALTER COLUMN c SET NOT NULL;\n",
        "ALTER TABLE ONLY public.x ALTER COLUMN c SET STATISTICS 100;\n",
    ]
    dropped: list[str] = []
    assert list(filter_stream(list(keep), dropped)) == keep
    assert dropped == [], "the filter must only ever remove pg_attver"


def test_set_default_and_not_null_are_never_mistaken_for_attoptions():
    """Guards the obvious over-match: these carry data meaning."""
    for line in ("ALTER TABLE ONLY public.x ALTER COLUMN c SET DEFAULT false;\n",
                 "ALTER TABLE ONLY public.x ALTER COLUMN c SET NOT NULL;\n"):
        assert not PG_ATTVER_RE.search(line)


def test_unterminated_statement_fails_closed():
    """Dropping one line of a multi-line statement would corrupt the stream;
    passing it through would reintroduce the restore error. Neither is safe,
    so it raises."""
    with pytest.raises(UnterminatedAttverStatement):
        list(filter_stream(["ALTER TABLE ONLY public.x\n",
                            "    ALTER COLUMN c SET (pg_attver=5)\n"]))


# ── venv CA bundle ───────────────────────────────────────────────────────

def test_cert_env_sets_a_real_bundle():
    path = cert_env.ca_bundle_path()
    assert path and os.path.exists(path), path
    assert os.environ.get("SSL_CERT_FILE") == path
    assert os.environ.get("REQUESTS_CA_BUNDLE") == path


def test_cert_env_never_overrides_an_operator_value(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/operator/choice.pem")
    cert_env.ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"] == "/operator/choice.pem"


def test_no_verify_disabling_anywhere():
    """A CA bundle is the fix; turning verification off is not. Also guards
    the third-party TOML cert errors, which must stay recorded per domain."""
    for fname in ("cert_env.py", os.path.join("tools", "restore_test.py"),
                  os.path.join("tools", "neon_dump_filter.py")):
        src = open(os.path.join(_REPO, fname), encoding="utf-8").read()
        code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
        for bad in ("verify=False", "CERT_NONE", "_create_unverified_context",
                    "check_hostname = False"):
            assert bad not in code, f"{fname}: {bad}"


def test_https_from_this_venv_verifies():
    """The canary Charlie asked for: a real HTTPS call must verify from the
    venv that was failing CERTIFICATE_VERIFY_FAILED."""
    import urllib.request
    with urllib.request.urlopen("https://vl.ripple.com/", timeout=20) as r:
        assert r.status == 200


# ── page on FAIL ─────────────────────────────────────────────────────────

def test_failure_pages_actively_and_uses_curl():
    """Before this, a FAIL only withheld the heartbeat and waited out a
    1-day grace window, so it surfaced in an evening summary instead of
    paging. And the ping used urllib, which is what broke."""
    src = open(os.path.join(_REPO, "tools", "restore_test.py"),
               encoding="utf-8").read()
    assert "_page_betterstack_failure" in src
    assert "/fail" in src
    assert "if rc != 0:" in src
    assert '"curl"' in src
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    assert "urllib.request.urlopen" not in code, "ping must not use urllib"
