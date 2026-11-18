"""Every job that makes an HTTPS call gets a CA bundle — and nothing bypasses.

Context (2026-11-18). The repo venv has no system trust store, so in-process
TLS verified against an empty one and died with:

    [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

38 logged hits over 30 days across five jobs. `cert_env` landed in PR #160
but only `tools/restore_test.py` imported it; these four real-fetch jobs were
still unprotected:

    unl_snapshot.py                     vl.ripple.com / unl.xrpl.foundation
    scripts/geoip_db_refresh_walker.py  the MaxMind download
    enrich_token_names.py               issuer TOMLs
    verify_toml_accounts.py             issuer TOMLs

THE LINE THIS FILE DEFENDS

A CA bundle fixes exactly one class: OUR missing trust store. It must never
become a licence to skip verification. The other two classes stay real
findings:

  * "Hostname mismatch" — the REMOTE cert does not cover the name requested.
  * a broken cert on a third-party issuer TOML (therocktrading.com,
    nft-loving.com, digitalpalm.vip) — recorded per domain, never bypassed,
    because verify_toml is a trust surface. A token whose TOML could not be
    verified must NOT end up labelled as verified.

Hermetic: source-level. No DB, no network.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# The jobs whose real fetches were failing, and must now be covered.
FETCHERS = [
    "unl_snapshot.py",
    "enrich_token_names.py",
    "verify_toml_accounts.py",
    os.path.join("scripts", "geoip_db_refresh_walker.py"),
    os.path.join("tools", "restore_test.py"),   # landed in PR #160
]

# Idioms that would silently turn verification off.
DISABLERS = (
    "verify=False",
    "verify = False",
    "CERT_NONE",
    "_create_unverified_context",
    "check_hostname = False",
    "check_hostname=False",
    "InsecureRequestWarning",
)


def _code_only(rel: str) -> str:
    """Source with whole-line comments stripped.

    Deliberate: a bare scan of the raw file matches the comments and
    docstrings that EXPLAIN the fix, which has now produced a false failure
    twice in this work (a SET-parameter guard flagged two correct
    `UPDATE ... SET` clauses, and this guard flagged cert_env's own
    docstring). Assert on code, not prose.
    """
    path = os.path.join(_REPO, rel)
    with open(path, encoding="utf-8") as fh:
        return "\n".join(
            ln.split("#")[0] for ln in fh if not ln.lstrip().startswith("#"))


@pytest.mark.parametrize("rel", FETCHERS)
def test_fetcher_imports_cert_env(rel):
    assert "import cert_env" in _code_only(rel), (
        f"{rel} makes HTTPS calls but has no CA bundle")


@pytest.mark.parametrize("rel", FETCHERS)
def test_fetcher_never_disables_verification(rel):
    code = _code_only(rel)
    for bad in DISABLERS:
        assert bad not in code, f"{rel}: {bad} — verification must stay on"


def test_cert_env_itself_never_disables_verification():
    code = _code_only("cert_env.py")
    for bad in DISABLERS:
        assert bad not in code, bad


def test_cert_env_only_fills_a_missing_bundle(monkeypatch):
    """An operator value always wins, so this can never silently retarget
    trust."""
    import cert_env
    monkeypatch.setenv("SSL_CERT_FILE", "/operator/choice.pem")
    cert_env.ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"] == "/operator/choice.pem"


def test_cert_env_resolves_a_real_bundle():
    import cert_env
    path = cert_env.ca_bundle_path()
    assert path and os.path.exists(path), path


def test_toml_fetch_errors_are_still_recorded_per_domain():
    """The never-bypass half of the rule: a TOML that cannot be fetched or
    verified must still be reported against its domain, not dropped and not
    treated as verified."""
    code = _code_only("enrich_token_names.py")
    assert "toml_err" in code, "per-domain TOML error reporting vanished"
    assert "verified_via" in code, "provenance field vanished"


def test_unverifiable_tokens_are_not_labelled_verified():
    """Guards the trust surface directly: nothing may stamp verified_via
    from a failed fetch path."""
    code = _code_only("enrich_token_names.py")
    assert 'verified_via":     toml_url' in code or \
           '"verified_via": toml_url' in code, (
        "verified_via should be set from the TOML URL that actually "
        "verified, so a failed fetch cannot produce it")
