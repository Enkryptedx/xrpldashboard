"""Cloudflare Turnstile server-side verification.

Charlie ruling 2026-09-23 14:20 ET: Turnstile guards `/contact` and
`/institutional/contact`. Cloudflare renders a widget on the form; the
browser sends its solved token as the hidden `cf-turnstile-response`
field; we POST that token + our env-held secret to Cloudflare's
`siteverify` endpoint. If the response reports `success:true`, the
submission passes to the existing XRPL-relevance filter (layer 2) and
downstream persistence. If not, we soft-drop with `turnstile_fail:<reason>`
in the bot-drop log — same pattern as the existing bot signatures.

Two env vars, both read once at import time:

  CF_TURNSTILE_SITE_KEY   — public. Rendered into the page. Safe to log.
  CF_TURNSTILE_SECRET     — private. Sent to Cloudflare's siteverify.
                            Never logged, never returned from any function.

When either env var is missing, `TURNSTILE_ENABLED` is False and the
GET routes render a "temporarily unavailable" banner + disabled submit
button; POST returns 503 rather than accepting an unverified payload.
That's the fail-closed contract Charlie asked for — "form shows
'temporarily unavailable' rather than accepting unverified posts."

We use urllib rather than adding a new dependency; the request is a
small form POST and doesn't warrant `requests` here.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_SITE_KEY = os.environ.get("CF_TURNSTILE_SITE_KEY") or ""
_SECRET = os.environ.get("CF_TURNSTILE_SECRET") or ""

TURNSTILE_SITE_KEY = _SITE_KEY
TURNSTILE_ENABLED = bool(_SITE_KEY and _SECRET)

_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_HTTP_TIMEOUT_SEC = 5.0


def verify_turnstile(token: str, remote_ip: str | None = None) -> tuple[bool, str]:
    """Verify a Turnstile response token against Cloudflare.

    Returns (verified, reason). On any transport error we treat the
    submission as unverified — never fail-open. `reason` is either
    'ok' on success or a short slug ('missing_token', 'timeout',
    'invalid-input-response', etc.) for logging/metrics.

    The secret never appears in the return value or in any log line.
    """
    if not TURNSTILE_ENABLED:
        return False, "disabled"
    if not token:
        return False, "missing_token"

    payload = {"secret": _SECRET, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    data = urllib.parse.urlencode(payload).encode("utf-8")

    try:
        req = urllib.request.Request(
            _SITEVERIFY_URL,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
            body = resp.read()
    except Exception as e:
        log.warning("turnstile_verify: transport error %s", type(e).__name__)
        return False, "transport_error"

    try:
        obj = json.loads(body)
    except Exception:
        return False, "parse_error"

    if not obj.get("success"):
        codes = obj.get("error-codes") or []
        first = codes[0] if codes else "unknown"
        return False, str(first)[:64]

    return True, "ok"
