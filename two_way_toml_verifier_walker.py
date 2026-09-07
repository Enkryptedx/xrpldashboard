"""Two-way toml verification walker for L2c registry self-submissions.

Charlie ruling 2026-09-07: L2c self-submission form lands as
self-described-never-verified. Verification is a background job that
confirms two things independently and only accepts a claim if BOTH pass:

  1. FORWARD: the toml at the submitted URL declares an [[ISSUERS]]
     entry for (currency, issuer) with a category matching the claim.
  2. REVERSE: the issuer AccountRoot's Domain field on the XRPL, decoded
     from hex to ASCII, resolves to the same hostname as the toml URL.

If both pass, the submission lands in `token_category_history` with
source='form-submission', tier='self-described', citation_url=<toml_url>.
The Merkle root over token_category_history then rides in tonight's
anchored snapshot as registry_state.

If either fails, we mark two_way_toml_ok=FALSE, write the reason into
two_way_toml_error, and DO NOT insert into token_category_history. The
submitter can re-submit (rate-limited 5/issuer/7d) once they've fixed
the toml or the Domain field.

The form is closed today (FEATURE_L2C_FORM_OPEN=false); this walker is
ready for day-1 operation once Charlie opens the form after attorney ToS.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tomllib
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import db


HERE = os.path.dirname(os.path.abspath(__file__))
POLITE_TIMEOUT_SECONDS = 15.0
TOML_MAX_BYTES = 256 * 1024   # 256KB — larger tomls are suspicious


def _fetch_toml(url: str) -> tuple[dict | None, str | None]:
    """Fetch + parse the toml. Returns (parsed_dict, None) on success,
    or (None, error_reason) on any failure. Never raises."""
    try:
        p = urlparse(url)
        if p.scheme != "https":
            return None, "toml_url_not_https"
        req = Request(url, headers={
            "User-Agent": "xrpldashboard-registry-verifier/1.0 (+https://xrpldashboard.com/registry/taxonomy)",
        })
        with urlopen(req, timeout=POLITE_TIMEOUT_SECONDS) as resp:
            body = resp.read(TOML_MAX_BYTES + 1)
            if len(body) > TOML_MAX_BYTES:
                return None, f"toml_too_large_over_{TOML_MAX_BYTES}_bytes"
    except HTTPError as e:
        return None, f"toml_http_{e.code}"
    except URLError as e:
        return None, f"toml_url_error_{type(e.reason).__name__}"
    except Exception as e:
        return None, f"toml_fetch_{type(e).__name__}"

    try:
        parsed = tomllib.loads(body.decode("utf-8", errors="replace"))
    except tomllib.TOMLDecodeError as e:
        return None, f"toml_parse_error_{str(e)[:80]}"
    except Exception as e:
        return None, f"toml_decode_{type(e).__name__}"
    return parsed, None


def _find_issuer_entry(toml: dict, currency_hex: str, issuer: str) -> dict | None:
    """Look for an [[ISSUERS]] entry matching (currency, issuer). The
    xrp-ledger.toml spec uses [[ISSUERS]] arrays with 'address' key.
    Some registries use [[TOKENS]] with 'issuer' + 'currency'. Handle
    both shapes."""
    for arr_name in ("ISSUERS", "TOKENS"):
        arr = toml.get(arr_name) or []
        if not isinstance(arr, list):
            continue
        for entry in arr:
            if not isinstance(entry, dict):
                continue
            entry_addr = (entry.get("address") or entry.get("issuer") or "").strip()
            entry_curr = (entry.get("currency") or "").strip().upper()
            if entry_addr == issuer and (
                entry_curr == currency_hex or entry_curr == ""
            ):
                return entry
    return None


def _fetch_xrpl_domain(issuer: str) -> tuple[str | None, str | None]:
    """Query AccountRoot for the issuer, decode Domain field.
    Returns (decoded_hostname, None) or (None, error_reason)."""
    try:
        from xrpl.clients import JsonRpcClient
        from xrpl.models.requests import AccountInfo
    except ImportError:
        return None, "xrpl_py_missing"

    node = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
    try:
        client = JsonRpcClient(node)
        resp = client.request(AccountInfo(account=issuer, ledger_index="validated"))
        if "error" in resp.result:
            return None, f"xrpl_error_{resp.result.get('error')}"
        acct = resp.result.get("account_data") or {}
    except Exception as e:
        return None, f"xrpl_rpc_{type(e).__name__}"

    domain_hex = acct.get("Domain")
    if not domain_hex:
        return None, "issuer_has_no_domain_field"

    try:
        domain = bytes.fromhex(domain_hex).decode("ascii").strip().lower()
    except Exception as e:
        return None, f"domain_decode_{type(e).__name__}"
    if not domain or not all(32 <= ord(c) < 127 for c in domain):
        return None, "domain_not_printable_ascii"
    return domain, None


def _domain_matches_toml_url(domain: str, toml_url: str) -> bool:
    """Two-way match: the issuer's on-ledger Domain and the toml URL's
    hostname must be the same registered root (bar the .well-known
    subdirectory)."""
    try:
        toml_host = urlparse(toml_url).hostname or ""
    except Exception:
        return False
    return toml_host.lower() == domain.lower()


def verify_submission(row: dict) -> tuple[bool, str | None, int | None]:
    """Run both directions of verification on one submission row.
    Returns (ok, error_reason, landed_history_id)."""
    toml, err = _fetch_toml(row["toml_url"])
    if err:
        return False, err, None

    entry = _find_issuer_entry(toml, row["currency_hex"], row["issuer"])
    if entry is None:
        return False, "toml_missing_issuer_or_currency_entry", None

    toml_category = (
        entry.get("category")
        or entry.get("class")
        or ""
    ).strip().lower()
    if toml_category and toml_category != row["claimed_category"]:
        return (
            False,
            f"toml_category_mismatch_claim={row['claimed_category']}_"
            f"toml={toml_category}",
            None,
        )

    domain, err = _fetch_xrpl_domain(row["issuer"])
    if err:
        return False, err, None

    if not _domain_matches_toml_url(domain, row["toml_url"]):
        return (
            False,
            f"domain_mismatch_ledger={domain}_toml_host={urlparse(row['toml_url']).hostname}",
            None,
        )

    return True, None, None  # id set by insert path below


def run_walker() -> tuple[int, int, int]:
    """Process every submission with two_way_toml_ok IS NULL.
    Returns (verified, rejected, errors)."""
    if not db.pg_available():
        raise SystemExit("STRICT-REFUSE: PG unavailable")

    verified = 0
    rejected = 0
    errors = 0

    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, issuer, currency_hex, claimed_category,
                       toml_url, contact_email
                FROM registry_form_submissions
                WHERE two_way_toml_ok IS NULL
                ORDER BY submitted_at ASC
                LIMIT 200
                """
            )
            pending = cur.fetchall()

        for row_tuple in pending:
            row = {
                "id": row_tuple[0],
                "issuer": row_tuple[1],
                "currency_hex": row_tuple[2],
                "claimed_category": row_tuple[3],
                "toml_url": row_tuple[4],
                "contact_email": row_tuple[5],
            }
            try:
                ok, err_reason, _ = verify_submission(row)
            except Exception as e:
                errors += 1
                print(
                    f"[two_way_toml_verifier] row {row['id']}: "
                    f"unhandled {type(e).__name__}: {e}",
                    file=sys.stderr, flush=True,
                )
                continue

            with conn.cursor() as cur:
                if ok:
                    # Insert token_category_history row + update submission
                    cur.execute(
                        """
                        INSERT INTO token_category_history (
                            currency_hex, issuer, category, tier, source,
                            citation_url, curator_id, curator_authority,
                            observed_at, taxonomy_version, note
                        ) VALUES (
                            %s, %s, %s,
                            'self-described',
                            'form-submission',
                            %s,
                            NULL,
                            'issuer_self',
                            NOW(),
                            '1.0.0',
                            'L2c self-submission verified via two-way toml proof'
                        )
                        RETURNING id
                        """,
                        (
                            row["currency_hex"], row["issuer"],
                            row["claimed_category"], row["toml_url"],
                        ),
                    )
                    history_id = cur.fetchone()[0]
                    cur.execute(
                        """
                        UPDATE registry_form_submissions
                        SET two_way_toml_ok = TRUE,
                            two_way_toml_error = NULL,
                            landed_history_id = %s
                        WHERE id = %s
                        """,
                        (history_id, row["id"]),
                    )
                    verified += 1
                    print(
                        f"[two_way_toml_verifier] VERIFIED submission "
                        f"{row['id']} → history_id {history_id}"
                    )
                else:
                    cur.execute(
                        """
                        UPDATE registry_form_submissions
                        SET two_way_toml_ok = FALSE,
                            two_way_toml_error = %s
                        WHERE id = %s
                        """,
                        (err_reason, row["id"]),
                    )
                    rejected += 1
                    print(
                        f"[two_way_toml_verifier] REJECTED submission "
                        f"{row['id']}: {err_reason}"
                    )
                conn.commit()

    return verified, rejected, errors


def main() -> int:
    now_utc = dt.datetime.now(dt.timezone.utc)
    print(
        f"[two_way_toml_verifier] start {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    try:
        verified, rejected, errors = run_walker()
    except SystemExit as e:
        print(f"[two_way_toml_verifier] STRICT-REFUSE: {e}", file=sys.stderr)
        return 1
    print(
        f"[two_way_toml_verifier] end verified={verified} rejected={rejected} "
        f"errors={errors}"
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
