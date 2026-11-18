"""One place that gives the venv a CA bundle. Import for the side effect.

THE BUG THIS FIXES
------------------
The repo venv's Python had no CA bundle configured, so every in-process TLS
call verified against an empty trust store and died with:

    [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

Measured over 30 days (2026-11-18): pg_restore_test 14, enrich_token_names
10, unl_snapshot 10, verify_toml 2, geoip_db_refresh_walker 2. Two of those
were real data fetches, not just heartbeats -- unl_snapshot's vl.ripple.com
pull and the geoip MaxMind download. pg_backup_canary was unaffected purely
because it pings with `curl`, which uses the system trust store.

Importing this module sets SSL_CERT_FILE and REQUESTS_CA_BUNDLE from
certifi, which is what urllib and requests read. Idempotent, and it never
overrides a value the operator set deliberately.

WHAT THIS DOES NOT FIX
----------------------
Only class 1 -- "unable to get local issuer certificate", i.e. OUR missing
trust store. It deliberately does not touch:

  * "Hostname mismatch" -- the REMOTE cert does not cover the name asked
    for. unl_snapshot hit this 8 times in Aug 2026 against the retired
    `vl.xrplf.org`; a CA bundle cannot and must not paper over it.
  * third-party issuer TOML certs (therocktrading.com, nft-loving.com,
    digitalpalm.vip). Those belong in the per-domain error record. Bypassing
    them would disable verification on a trust surface, which is the whole
    point of verify_toml.

Verification is never disabled here, and must never be. A companion test
asserts that, scanning for the usual disabling idioms.
"""

from __future__ import annotations

import os

__all__ = ["ensure_ca_bundle", "ca_bundle_path"]


def ca_bundle_path() -> str | None:
    """certifi's bundle path, or None when certifi is unavailable."""
    try:
        import certifi
    except ImportError:
        return None
    return certifi.where()


def ensure_ca_bundle() -> str | None:
    """Point SSL_CERT_FILE / REQUESTS_CA_BUNDLE at certifi if unset.

    Returns the path in effect, or None if certifi is missing. An existing
    value is left alone so an operator override always wins.
    """
    path = ca_bundle_path()
    if not path or not os.path.exists(path):
        return os.environ.get("SSL_CERT_FILE") or None
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if not os.environ.get(var):
            os.environ[var] = path
    return os.environ.get("SSL_CERT_FILE")


# Side effect on import: the point is that a caller only has to `import
# cert_env` once, as early as possible, and every later urllib/requests call
# in the process inherits a working trust store.
ensure_ca_bundle()
