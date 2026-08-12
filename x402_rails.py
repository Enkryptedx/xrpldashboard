"""x402 rails-dark middleware — per docs/X402_RAILS_DARK_SCOPING.md.

Wires the x402 machine-payment flow behind a three-mode env-var switch
(off / dry_run / on) so the plumbing can ship to production with
enforcement=off (default) and be exercised on testnet without touching
mainnet money. Ships the wrapper only — no routes are wired to it in
this file. Route-wiring happens elsewhere (or in a follow-up) so this
module can be reviewed in isolation and the "which endpoints are
paid-eligible?" question stays a per-endpoint decision at the callsite.

Fence #8 (from § Fences of the scoping memo, ratified 2026-08-10):
    Code-level enforcement of SELLABLE_REQUIRES_SOVEREIGN_SOURCE.
    An endpoint may only enable `X402_ENFORCEMENT=on` if its declared
    `sovereignty_class` is "own_node". Any other value forces the mode
    down to at most "dry_run" — the wrapper refuses to serve a paid
    response over non-sovereign data even if a developer sets the env
    var. Belt-and-suspenders for the invariant.

Sovereignty classes (mirroring `docs/PAID_MACHINE_TIER_DESIGN.md` § 0):
    - "own_node"                — paid-eligible; can be enforced ON.
    - "public_infra_dependent"  — free-only until Batch B migrates the
                                  underlying walker to our own rippled.
                                  Can dry_run; cannot enforce.
    - "third_party_derived"     — free-only, ever, under the invariant.
                                  Can dry_run; cannot enforce.

Enforcement modes (env: X402_ENFORCEMENT):
    - "off" (DEFAULT)   — wrapper is a pass-through. The decorated
                          route behaves identically to an undecorated
                          route. External behavior unchanged. This is
                          the shipping default in every environment
                          except a deliberate dry_run staging box.
    - "dry_run"         — wrapper active against testnet facilitator.
                          402 on missing X-PAYMENT; 200 on valid
                          testnet receipt. No mainnet exposure.
    - "on"              — wrapper active against mainnet facilitator.
                          Blocked by Fence #8 unless the route is
                          own_node. Attorney-gate elsewhere; this
                          module only enforces the sovereignty half.

Facilitator selection is by env, not code:
    X402_FACILITATOR_URL  — the URL the facilitator wrapper submits
                            presigned txs to. Testnet default:
                            https://xrpl-facilitator-testnet.t54.ai
                            Mainnet default:
                            https://xrpl-facilitator-mainnet.t54.ai
                            (t54's operator-published endpoints; see
                            https://xrpl-x402.t54.ai/)
    X402_PAY_TO           — destination wallet. Empty by default so a
                            bad deploy fails loudly, not silently.
    X402_NETWORK          — "xrpl:0" (mainnet, default) or "xrpl:1"
                            (testnet).
    X402_ASSET            — "RLUSD" default; "XRP" or "USDC" also
                            supported by the facilitator. Not changed
                            by the wrapper — the facilitator handles
                            asset routing given the network.

The `x402-xrpl` PyPI package is imported lazily inside the enforcement
path so the module loads without it installed (needed for CI where the
dep is not pinned in requirements yet). Tests monkey-patch the import
site to avoid needing the real facilitator.

No private keys are read, stored, or referenced by this module. The
x402 flow is non-custodial by construction — the agent signs the tx
offline and the facilitator verifies + submits it. Our side never
holds a signing key. Any future edit that adds a key to this module is
a bug, not a feature; see Fence #4 (No custody, ever) in the memo.
"""
from __future__ import annotations

import functools
import os
from typing import Any, Callable, Optional

from flask import jsonify, make_response, request


# ── Sovereignty classes (Fence #8) ──────────────────────────────────
#
# Mirrors the classifications in `docs/PAID_MACHINE_TIER_DESIGN.md` § 0.
# Kept as string literals rather than an Enum so that a route declaring
# its class is a one-word literal at the callsite (readable in review)
# and so unknown values force a hard fail rather than silently
# defaulting to "own_node".

SOVEREIGNTY_OWN_NODE = "own_node"
SOVEREIGNTY_PUBLIC_INFRA_DEPENDENT = "public_infra_dependent"
SOVEREIGNTY_THIRD_PARTY_DERIVED = "third_party_derived"

_VALID_SOVEREIGNTY_CLASSES = frozenset({
    SOVEREIGNTY_OWN_NODE,
    SOVEREIGNTY_PUBLIC_INFRA_DEPENDENT,
    SOVEREIGNTY_THIRD_PARTY_DERIVED,
})


# ── Enforcement modes ───────────────────────────────────────────────

ENFORCEMENT_OFF = "off"
ENFORCEMENT_DRY_RUN = "dry_run"
ENFORCEMENT_ON = "on"

_VALID_ENFORCEMENT_MODES = frozenset({
    ENFORCEMENT_OFF,
    ENFORCEMENT_DRY_RUN,
    ENFORCEMENT_ON,
})


# ── Facilitator defaults ────────────────────────────────────────────
#
# t54.ai operates the XRPL x402 facilitator. Endpoints per
# https://xrpl-x402.t54.ai/ and the x402-xrpl PyPI README.

_DEFAULT_FACILITATOR_TESTNET = "https://xrpl-facilitator-testnet.t54.ai"
_DEFAULT_FACILITATOR_MAINNET = "https://xrpl-facilitator-mainnet.t54.ai"


# ── Environment resolution ──────────────────────────────────────────

def current_enforcement_mode() -> str:
    """Return the current X402_ENFORCEMENT mode, defaulting to 'off'.

    Unknown values are coerced to 'off' — a typo in production should
    fail *safe* (rails effectively disabled), never fail *paid* (rails
    enforcing against unintended traffic). A logged warning could be
    added by the caller if desired; this module stays silent so it
    remains importable in test environments without log surface."""
    raw = os.environ.get("X402_ENFORCEMENT", ENFORCEMENT_OFF).strip().lower()
    if raw in _VALID_ENFORCEMENT_MODES:
        return raw
    return ENFORCEMENT_OFF


def current_facilitator_url() -> str:
    """Return the facilitator URL to submit presigned txs to.

    Explicit env override wins. Otherwise choose testnet for dry_run,
    mainnet for on. In 'off' mode the return value is unused but
    defaults to mainnet for consistency."""
    explicit = os.environ.get("X402_FACILITATOR_URL", "").strip()
    if explicit:
        return explicit
    if current_enforcement_mode() == ENFORCEMENT_DRY_RUN:
        return _DEFAULT_FACILITATOR_TESTNET
    return _DEFAULT_FACILITATOR_MAINNET


def current_network() -> str:
    """Return X402_NETWORK ("xrpl:0" mainnet default, "xrpl:1" testnet).

    In dry_run mode default to testnet if the env is unset — the whole
    point of dry_run is to not touch mainnet, and forcing the developer
    to remember both env vars is a footgun."""
    explicit = os.environ.get("X402_NETWORK", "").strip()
    if explicit:
        return explicit
    if current_enforcement_mode() == ENFORCEMENT_DRY_RUN:
        return "xrpl:1"
    return "xrpl:0"


def current_pay_to() -> str:
    """Return the destination XRPL address for x402 receipts. Empty
    default is intentional — a bad deploy that flipped enforcement=on
    without setting the destination should fail loudly at request time,
    not silently route funds to a zeroed address."""
    return os.environ.get("X402_PAY_TO", "").strip()


def current_asset() -> str:
    """Return the settlement asset. Default RLUSD (Ripple-issued
    stablecoin on XRPL) — the ops wallet already has the RLUSD trust
    line set by tx 8F4C2A…31FF on 2026-08-07 per the scoping memo."""
    return os.environ.get("X402_ASSET", "RLUSD").strip() or "RLUSD"


# ── Effective mode (Fence #8 gate) ──────────────────────────────────

def effective_enforcement_mode(sovereignty_class: str) -> str:
    """Return the effective enforcement mode for an endpoint of the
    given sovereignty class, after applying Fence #8.

    Non-own_node sovereignty classes can never reach ENFORCEMENT_ON —
    any request to promote them is silently downgraded to at most
    ENFORCEMENT_DRY_RUN. This makes the invariant a code-level
    contract, not just a documentation rule.

    Unknown sovereignty classes raise ValueError. This is the one place
    the module fails hard: an endpoint that doesn't declare a valid
    class must not silently default to any behavior.
    """
    if sovereignty_class not in _VALID_SOVEREIGNTY_CLASSES:
        raise ValueError(
            f"unknown sovereignty_class {sovereignty_class!r}; "
            f"must be one of {sorted(_VALID_SOVEREIGNTY_CLASSES)}"
        )
    requested = current_enforcement_mode()
    if requested == ENFORCEMENT_OFF:
        return ENFORCEMENT_OFF
    if sovereignty_class == SOVEREIGNTY_OWN_NODE:
        return requested
    # Fence #8: non-sovereign endpoints cannot flip ON.
    if requested == ENFORCEMENT_ON:
        return ENFORCEMENT_DRY_RUN
    return requested


# ── The 402 response shape ──────────────────────────────────────────

def _build_payment_required_response(
    *,
    price_drops: int,
    pay_to: str,
    facilitator_url: str,
    network: str,
    asset: str,
    resource_path: str,
    scope_note_url: Optional[str] = None,
) -> Any:
    """Return a Flask response object with HTTP 402 and the standard
    x402 payment-requirements JSON body. Shape follows the x402
    specification (github.com/coinbase/x402) as adopted by t54's
    XRPL facilitator.

    The `scope_note_url` field is our house convention: a machine-
    readable link into `/methodology#for-machine-payments` (or the
    equivalent) so an agent that hits the 402 can programmatically
    surface the friction to its operator rather than silent-retry."""
    body = {
        "x402Version": 1,
        "accepts": [
            {
                "scheme": "exact",
                "network": network,
                "maxAmountRequired": str(price_drops),
                "resource": resource_path,
                "description": (
                    f"xrpldashboard machine-tier access to {resource_path}"
                ),
                "mimeType": "application/json",
                "payTo": pay_to,
                "maxTimeoutSeconds": 60,
                "asset": asset,
                "extra": {"facilitatorUrl": facilitator_url},
            }
        ],
        "error": "payment_required",
    }
    if scope_note_url:
        body["scopeNote"] = scope_note_url
    resp = make_response(jsonify(body), 402)
    # x402 payment requirements are never cacheable — a stale 402
    # served from an intermediate cache would waste an agent's price
    # signal or, worse, serve the wrong pay_to during a wallet rotation.
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Facilitator verification (lazy import) ──────────────────────────

def _verify_payment_header(payment_header: str, requirements: dict) -> Optional[dict]:
    """Verify a presigned X-PAYMENT header against the facilitator.

    Returns the receipt dict on success, None on any failure (invalid
    signature, insufficient amount, network mismatch, facilitator
    unreachable). None → caller returns 402; success → caller returns
    200 with the receipt in X-PAYMENT-RECEIPT.

    The x402-xrpl PyPI package is imported here, not at module top, so
    a deploy that doesn't yet have the dep pinned in requirements can
    still import this module. In tests the whole function is monkey-
    patched to avoid needing the real client."""
    try:
        # x402-xrpl exposes a facilitator client under x402_xrpl.facilitator
        # per the PyPI README. Kept behind a broad try/except at import
        # time so a missing/renamed API in a future package version
        # fails as "not verifiable" rather than crashing the whole
        # request.
        from x402_xrpl.facilitator import FacilitatorClient  # type: ignore  # noqa: F401
    except Exception:
        return None
    try:
        client = FacilitatorClient(base_url=requirements["extra"]["facilitatorUrl"])
        result = client.verify(payment_header, requirements)
        if not getattr(result, "isValid", False):
            return None
        submit_result = client.settle(payment_header, requirements)
        return {
            "tx_hash": getattr(submit_result, "transaction", None),
            "network": requirements.get("network"),
        }
    except Exception:
        return None


# ── The decorator ───────────────────────────────────────────────────

def x402_maybe_require_payment(
    *,
    sovereignty_class: str,
    price_drops: Callable[[Any], int] | int = 0,
    resource_path: Optional[str] = None,
    scope_note_url: Optional[str] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator factory: wrap a Flask view so it participates in the
    x402 flow when enforcement is active.

    Args:
        sovereignty_class: one of SOVEREIGNTY_* constants. Required.
            Determines Fence #8 gating: only own_node can reach ON.
        price_drops: either an int or a callable(flask.request) -> int.
            The advertised price in the smallest asset unit. Default 0
            — rails-dark means the plumbing exists but the price is
            zero until flip-ON. A callable is invoked at request time
            so per-endpoint metering (result-row count, byte size) can
            price dynamically.
        resource_path: string to advertise as the resource in the 402
            body. Defaults to `request.path` at request time.
        scope_note_url: absolute URL string to advertise as the human-
            readable scope note (see `_build_payment_required_response`
            for the field convention).

    Behavior at request time:
        - enforcement=off → pass-through to the wrapped view.
        - enforcement=dry_run → require X-PAYMENT (testnet), 402 if
          absent/invalid, 200 + X-PAYMENT-RECEIPT if valid.
        - enforcement=on → same as dry_run but against mainnet; only
          reachable for own_node sovereignty class (Fence #8).

    ValueError at DECORATION time if sovereignty_class is unknown —
    this catches typos at import, not at first request.
    """
    if sovereignty_class not in _VALID_SOVEREIGNTY_CLASSES:
        raise ValueError(
            f"unknown sovereignty_class {sovereignty_class!r}; "
            f"must be one of {sorted(_VALID_SOVEREIGNTY_CLASSES)}"
        )

    def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            mode = effective_enforcement_mode(sovereignty_class)
            if mode == ENFORCEMENT_OFF:
                return fn(*args, **kwargs)

            pay_to = current_pay_to()
            if not pay_to:
                # Fail loud: enforcement active but no destination
                # wallet configured. Never silently accept payments to
                # a zero address.
                return make_response(
                    jsonify({"error": "x402_misconfigured", "detail": "X402_PAY_TO unset"}),
                    500,
                )

            price = price_drops(request) if callable(price_drops) else price_drops
            price = max(int(price), 0)
            facilitator_url = current_facilitator_url()
            network = current_network()
            asset = current_asset()
            resource = resource_path or request.path

            requirements = {
                "scheme": "exact",
                "network": network,
                "maxAmountRequired": str(price),
                "resource": resource,
                "description": f"xrpldashboard machine-tier access to {resource}",
                "mimeType": "application/json",
                "payTo": pay_to,
                "maxTimeoutSeconds": 60,
                "asset": asset,
                "extra": {"facilitatorUrl": facilitator_url},
            }

            payment_header = request.headers.get("X-PAYMENT", "").strip()
            if not payment_header:
                return _build_payment_required_response(
                    price_drops=price,
                    pay_to=pay_to,
                    facilitator_url=facilitator_url,
                    network=network,
                    asset=asset,
                    resource_path=resource,
                    scope_note_url=scope_note_url,
                )

            receipt = _verify_payment_header(payment_header, requirements)
            if receipt is None:
                return _build_payment_required_response(
                    price_drops=price,
                    pay_to=pay_to,
                    facilitator_url=facilitator_url,
                    network=network,
                    asset=asset,
                    resource_path=resource,
                    scope_note_url=scope_note_url,
                )

            response = make_response(fn(*args, **kwargs))
            if receipt.get("tx_hash"):
                response.headers["X-PAYMENT-RECEIPT"] = str(receipt["tx_hash"])
            return response

        # Stamp the decorator's config onto the wrapped function so
        # tests + introspection tools can see the sovereignty class of
        # each x402-wrapped route without re-parsing decorators. Also
        # useful for a future `/api/x402/registry` surface that lists
        # every wired endpoint + its class.
        _wrapper._x402_sovereignty_class = sovereignty_class  # type: ignore[attr-defined]
        _wrapper._x402_price_drops = price_drops  # type: ignore[attr-defined]
        return _wrapper

    return _decorator
