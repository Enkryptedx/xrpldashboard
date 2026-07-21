# parked/api-v1-scaffold

This branch parks the API v1 Gate 1 scaffold.

**Ship gate:** legal checklist. Do not merge until Charlie confirms the gate is clear.

**Contents:**

- `app.py` — API v1 scaffold: imports (`hashlib`, `re`, `itsdangerous.URLSafeTimedSerializer`), `robots.txt` `Allow: /api/v1/`, `_XRPL_ADDR_RE`, `_TIER_LIMITS` (free 60/hr, dev 6000/hr placeholder, institutional unbounded), `_MAGIC_LINK_MAX_AGE_S`, `_magic_serializer`, `_api_meta`, `api_response`, `api_error_response`, `_generate_api_key`, `_extract_api_key`, `_apply_ratelimit_headers`, `_authenticate_and_rate_limit`, Gate 1 routes for `/api/v1/attestation`, `/api/v1/tokens`, `/api/v1/pools`, `/api/v1/label/<address>`.
- `templates/account_api_keys.html` — key management UI stub.

**Governing memories:**

- `project_xrpldashboard_api_v1_anchors.md` — pricing, endpoint set, evidence standard.
- `project_xrpldashboard_truth_audit_redesign_flagship.md` — API v1 gated behind legal checklist; audit redesign is flagship, not API v1.
- `feedback_working_tree_not_parking_lot.md` — the near-miss that produced this branch (2026-07-21).
- `project_xrpldashboard_api_v1_scaffold_parked_branch.md` — execute record for this park action.
