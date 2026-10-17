"""two_way_toml_verifier_walker: retry transient TOML failures.

WHY THIS WALKER AND ONLY THIS WALKER. Of the five TOML fetchers in the repo,
this is the only fail-CLOSED one. On failure it writes
`two_way_toml_ok = FALSE` plus the error reason into
registry_form_submissions and counts the row REJECTED:

    UPDATE registry_form_submissions
    SET two_way_toml_ok = FALSE, two_way_toml_error = %s
    rejected += 1

So a single transient blip does not merely skip a check -- it PERSISTS a
rejection against a legitimate issuer self-submission, and nothing re-opens
it. The other four either skip (verify_toml_accounts: "SKIP -- {err}" +
error_count), return a neutral third state (check_data._fetch_toml_fast ->
couldnt_check, on a 2s request-path budget tightened 2026-08-27), or are
documented fail-open under a ~1 fetch/sec rate limit
(token_issuer_flags_walker, owner's 2026-09-06 A+B ruling). Wiring retry into
those would either fix nothing or breach a documented ruling.

FOUND THE HARD WAY, 2026-10-16: verifying XAGa's issuer, the first fetch of
silver.assetiko.com returned URLError and the immediate retry returned 200
naming the issuer 4x. That was an ad-hoc check, so the cost was only a
near-miss. Through this walker it would have rejected a real submission and
written that rejection to the database.

ONLY TRANSIENT FAILURES ARE RETRIED. A 404, a non-https URL, an oversized
body or a TOML parse error fails identically on a second attempt, so
retrying them spends a request against a third-party domain and delays the
verdict for nothing.

Hermetic: injected fetcher and sleeper. No network, no DB, no wall-clock.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
os.environ.setdefault("DATABASE_URL", "")

MODULE_PATH = os.path.join(REPO_ROOT, "two_way_toml_verifier_walker.py")
_spec = importlib.util.spec_from_file_location("_twtv_under_test", MODULE_PATH)
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)

OK_TOML = {"ACCOUNTS": [{"address": "rSomeIssuer"}]}


def seq_fetcher(*results):
    """Fetcher yielding each (parsed, err) in order; records call count."""
    calls = {"n": 0}

    def _f(url):
        i = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        return results[i]
    _f.calls = calls
    return _f


# ── THE case owner asked for ────────────────────────────────────────
class TestTransientThenSuccess:
    def test_blip_then_success_yields_a_POSITIVE_verdict(self):
        """The whole point: one URLError must not persist a rejection."""
        slept = []
        f = seq_fetcher((None, "toml_url_error_URLError"), (OK_TOML, None))
        parsed, err = w.fetch_toml_with_retry(
            "https://silver.assetiko.com/.well-known/xrp-ledger.toml",
            fetcher=f, sleeper=slept.append)
        assert err is None, "a transient blip still produced a rejection"
        assert parsed == OK_TOML
        assert f.calls["n"] == 2
        assert slept == [w.RETRY_SLEEP_SECONDS]

    def test_first_attempt_success_does_not_sleep_or_refetch(self):
        slept = []
        f = seq_fetcher((OK_TOML, None))
        parsed, err = w.fetch_toml_with_retry("https://x/y", fetcher=f,
                                               sleeper=slept.append)
        assert (parsed, err) == (OK_TOML, None)
        assert f.calls["n"] == 1
        assert slept == []

    def test_persistent_transient_still_fails_closed(self):
        """Retry is not infinite forgiveness -- a genuinely unreachable
        domain must still be rejected, with the real reason preserved."""
        f = seq_fetcher((None, "toml_url_error_URLError"))
        parsed, err = w.fetch_toml_with_retry("https://x/y", fetcher=f,
                                               sleeper=lambda _s: None)
        assert parsed is None
        assert err == "toml_url_error_URLError"
        assert f.calls["n"] == w.RETRY_ATTEMPTS


# ── deterministic failures must NOT be retried ──────────────────────
class TestDeterministicNotRetried:
    @pytest.mark.parametrize("err", [
        "toml_url_not_https",
        "toml_http_404",
        "toml_http_403",
        "toml_too_large_over_262144_bytes",
        "toml_parse_error_Expected '=' after a key",
        "toml_decode_UnicodeDecodeError",
    ])
    def test_single_attempt_only(self, err):
        """These fail identically on a second try. Retrying spends a request
        on a third-party domain and delays the verdict for nothing."""
        slept = []
        f = seq_fetcher((None, err))
        parsed, got = w.fetch_toml_with_retry("https://x/y", fetcher=f,
                                               sleeper=slept.append)
        assert (parsed, got) == (None, err)
        assert f.calls["n"] == 1, f"{err} should not be retried"
        assert slept == []

    def test_5xx_is_retried_but_4xx_is_not(self):
        """Server-side failures are worth one more try; client errors are a
        statement of fact."""
        assert w._is_transient("toml_http_500") is True
        assert w._is_transient("toml_http_503") is True
        assert w._is_transient("toml_http_404") is False
        assert w._is_transient("toml_http_403") is False

    def test_transient_classification(self):
        assert w._is_transient("toml_url_error_URLError") is True
        assert w._is_transient("toml_fetch_TimeoutError") is True
        assert w._is_transient("toml_url_not_https") is False
        assert w._is_transient(None) is False
        assert w._is_transient("") is False


# ── contract + wiring ───────────────────────────────────────────────
class TestContractAndWiring:
    def test_contract_matches_the_single_shot_fetcher(self):
        """Same (parsed, err) shape, so the call site needed no reshaping."""
        f = seq_fetcher((OK_TOML, None))
        assert w.fetch_toml_with_retry("https://x", fetcher=f,
                                       sleeper=lambda _s: None) == (OK_TOML, None)

    def test_single_shot_fetcher_still_exists(self):
        assert callable(w._fetch_toml_once)

    def test_call_site_uses_the_retrying_version(self):
        src = open(MODULE_PATH, encoding="utf-8").read()
        assert 'fetch_toml_with_retry(row["toml_url"])' in src
        assert '_fetch_toml(row["toml_url"])' not in src

    def test_attempts_floor_is_one(self):
        """A misconfigured 0 must still make one request, not zero."""
        f = seq_fetcher((OK_TOML, None))
        w.fetch_toml_with_retry("https://x", attempts=0, fetcher=f,
                                sleeper=lambda _s: None)
        assert f.calls["n"] == 1

    def test_policy_defaults(self):
        assert w.RETRY_ATTEMPTS == 2
        assert w.RETRY_SLEEP_SECONDS == 1.0


# ── the exclusion note must survive ─────────────────────────────────
def test_docstring_records_why_the_others_are_excluded():
    """A future reader must not 'finish the job' and undo the 2s paste-box
    budget or the 1 fetch/sec rate limit."""
    src = open(MODULE_PATH, encoding="utf-8").read()
    for marker in ("verify_toml_accounts.py", "check_data.py",
                   "token_issuer_flags_walker.py", "fail-OPEN",
                   "2026-08-27", "2026-09-06"):
        assert marker in src, f"exclusion rationale lost: {marker}"
