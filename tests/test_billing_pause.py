"""Unit tests for the billing-pause middleware (Option B, 2026-09-02).

Rule (verbatim, from Charlie's 2026-09-06 re-transmission):
    Any paid call whose response sourcing is anything other than
    "sovereign" is SERVED normally but NOT METERED or charged. The
    response carries billed: false and billing_reason. Billing resumes
    automatically when sourcing returns to sovereign.

Tests cover the 4 required cases + the 2 auxiliary ones:
    1. sourcing = "sovereign"                    → billed:true,  no audit row
    2. sourcing = "fallback-public-rpc"          → billed:false, reason: sovereign-path-unavailable
    3. sourcing = "public-no-tunnel-configured"  → billed:false, reason: sovereign-path-unavailable
    4. sourcing = "stale-cache"                  → billed:false, reason: stale-cache
    5. sourcing missing (fail-closed on unknown) → billed:false, reason: sourcing-unknown
    6. sourcing wrapped under "data" key         → same rule, unwraps correctly

Run standalone (bypasses conftest.py that imports app.py w/ flask_smorest):
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 tests/test_billing_pause.py
"""
import json
import os
import sys

# Make repo root importable directly (bypasses tests/conftest.py which
# pulls flask_smorest via app.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask is available — comes with the app. We construct real Response
# objects so the middleware exercises the actual .data/.get_json path.
try:
    from flask import Response
except ImportError:
    print("FAIL: flask not available in this Python; install or run with the site venv")
    sys.exit(1)

import x402_rails as rails


def _make_response(body_dict):
    """Build a Flask Response with a JSON body — same shape the x402
    wrapper hands to apply_billing_pause after fn() runs."""
    body = json.dumps(body_dict).encode("utf-8")
    r = Response(response=body, status=200, mimetype="application/json")
    r.headers["Content-Length"] = str(len(body))
    return r


class _MockWriteFn:
    """Records every db.write_unbilled_call invocation for assertion."""
    def __init__(self):
        self.calls = []
    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return True


def _case(name, sourcing_shape, expected_paused, expected_reason,
          expected_billed):
    """Run one test case. Returns (passed: bool, detail: str)."""
    if sourcing_shape == "unwrapped_sovereign":
        body = {"sourcing": "sovereign", "data": {"foo": "bar"}}
    elif sourcing_shape == "wrapped_sovereign":
        body = {"data": {"sourcing": "sovereign", "foo": "bar"}}
    elif sourcing_shape == "unwrapped_fallback":
        body = {"sourcing": "fallback-public-rpc", "data": {}}
    elif sourcing_shape == "unwrapped_no_tunnel":
        body = {"sourcing": "public-no-tunnel-configured", "data": {}}
    elif sourcing_shape == "unwrapped_stale":
        body = {"sourcing": "stale-cache", "data": {}}
    elif sourcing_shape == "missing":
        body = {"data": {"foo": "no-sourcing-field-anywhere"}}
    elif sourcing_shape == "wrapped_fallback":
        body = {"data": {"sourcing": "fallback-public-rpc"}}
    else:
        return False, f"unknown shape {sourcing_shape}"

    resp = _make_response(body)
    mock_write = _MockWriteFn()
    resp_out, was_paused = rails.apply_billing_pause(
        response=resp,
        endpoint="/check.json",
        request_id=f"test-{name}",
        client_identifier="test-client",
        canonical_hash="test-hash",
        db_write_fn=mock_write,
    )

    body_out = json.loads(resp_out.get_data(as_text=True))
    billed_out = body_out.get("billed")
    reason_out = body_out.get("billing_reason")

    problems = []
    if was_paused != expected_paused:
        problems.append(f"was_paused={was_paused}, expected {expected_paused}")
    if billed_out != expected_billed:
        problems.append(f"billed={billed_out}, expected {expected_billed}")
    if reason_out != expected_reason:
        problems.append(f"billing_reason={reason_out}, expected {expected_reason}")
    # Audit row assertion: written iff paused
    audit_written = len(mock_write.calls) == 1
    if audit_written != expected_paused:
        problems.append(f"audit_written={audit_written}, expected {expected_paused}")

    if problems:
        return False, "; ".join(problems)
    return True, "ok"


CASES = [
    # (name,                             sourcing_shape,           paused, reason,                               billed)
    ("sovereign",                         "unwrapped_sovereign",   False, None,                                 True),
    ("sovereign_wrapped_under_data",      "wrapped_sovereign",     False, None,                                 True),
    ("fallback_public_rpc",               "unwrapped_fallback",    True,  rails.BILLING_REASON_SOVEREIGN_PATH_UNAVAILABLE, False),
    ("public_no_tunnel_configured",       "unwrapped_no_tunnel",   True,  rails.BILLING_REASON_SOVEREIGN_PATH_UNAVAILABLE, False),
    ("stale_cache",                       "unwrapped_stale",       True,  rails.BILLING_REASON_STALE_CACHE,     False),
    ("sourcing_missing_fail_closed",      "missing",               True,  rails.BILLING_REASON_SOURCING_UNKNOWN, False),
    ("fallback_wrapped_under_data",       "wrapped_fallback",      True,  rails.BILLING_REASON_SOVEREIGN_PATH_UNAVAILABLE, False),
]


def _test_idempotency():
    """Middleware calls db_write_fn every time it fires — dedup is
    enforced at the DB layer via UNIQUE (request_id). Confirm we don't
    dedupe in Python (double-write is expected here; the DB will
    ON CONFLICT DO NOTHING)."""
    mock_write = _MockWriteFn()
    body = {"sourcing": "fallback-public-rpc"}
    resp1 = _make_response(body)
    resp2 = _make_response(body)
    rails.apply_billing_pause(
        response=resp1, endpoint="/check.json", request_id="dup-req-1",
        client_identifier=None, canonical_hash="h1", db_write_fn=mock_write,
    )
    rails.apply_billing_pause(
        response=resp2, endpoint="/check.json", request_id="dup-req-1",  # same id
        client_identifier=None, canonical_hash="h1", db_write_fn=mock_write,
    )
    if len(mock_write.calls) != 2:
        return False, f"expected 2 write calls (dedup at DB), got {len(mock_write.calls)}"
    return True, "ok"


def _test_no_json_body_no_op():
    """A response with a non-JSON body must NOT be modified and must NOT
    log an audit row (shouldn't happen for machine tier, but define the
    fallback shape)."""
    mock_write = _MockWriteFn()
    resp = Response(response=b"not json", status=200, mimetype="text/plain")
    resp_out, was_paused = rails.apply_billing_pause(
        response=resp, endpoint="/check.json", request_id="notjson",
        client_identifier=None, canonical_hash="", db_write_fn=mock_write,
    )
    if was_paused:
        return False, "was_paused=True on non-JSON response (should be False)"
    if len(mock_write.calls) != 0:
        return False, f"audit-logged non-JSON response (got {len(mock_write.calls)} calls, expected 0)"
    if resp_out.get_data() != b"not json":
        return False, "modified non-JSON body"
    return True, "ok"


def main():
    pass_count = 0
    fail_count = 0
    for name, shape, paused, reason, billed in CASES:
        ok, detail = _case(name, shape, paused, reason, billed)
        if ok:
            print(f"  PASS {name}")
            pass_count += 1
        else:
            print(f"  FAIL {name}: {detail}")
            fail_count += 1

    ok, detail = _test_idempotency()
    print(f"  {'PASS' if ok else 'FAIL'} idempotency: {detail}")
    pass_count += ok
    fail_count += not ok

    ok, detail = _test_no_json_body_no_op()
    print(f"  {'PASS' if ok else 'FAIL'} no_json_body_no_op: {detail}")
    pass_count += ok
    fail_count += not ok

    print(f"\n== {pass_count} PASS / {fail_count} FAIL ==")
    return fail_count


if __name__ == "__main__":
    sys.exit(main())
