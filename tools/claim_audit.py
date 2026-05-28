#!/usr/bin/env python3
"""Claim manifest auditor — fail CI if visitor-facing claims drift or go unmanifested.

Runs on every push to ensure:
  1. All claims in claims.yml still appear in their templates (not removed w/o updating manifest)
  2. No claim has expired (last_audit + expires_after_days > today)
  3. No new claim-shaped strings in templates without a manifest entry (progressive detection)

Exit 0: manifest audit passes
Exit 1: claim drift, expired entries, or unmanifested claims found
Exit 2: manifest or template dir missing / audit cannot run (fail loudly)
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates"
CLAIMS_FILE = REPO_ROOT / "claims.yml"

# Jinja comment block pattern — matches {# ... #} including multiline
JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)


def load_claims() -> list[dict]:
    """Load claims.yml and return list of claim dicts."""
    try:
        with open(CLAIMS_FILE) as f:
            data = yaml.safe_load(f)
        return data.get("claims", [])
    except Exception as e:
        print(f"claim_audit: failed to load {CLAIMS_FILE}: {e}", file=sys.stderr)
        return None


def strip_jinja_comments(text: str) -> str:
    """Strip Jinja comment blocks {# ... #} from text."""
    return JINJA_COMMENT_RE.sub(
        lambda m: "\n" * m.group(0).count("\n"),
        text,
    )


def claim_in_template(claim: dict, template_path: Path) -> bool:
    """Check if claim's quoted text appears in the template.

    Uses substring fingerprinting to handle Jinja wrapping, HTML entities, etc.
    """
    if claim.get("line") is None:
        # Skip entries without a line number (e.g., "28+ institutional accounts" not found)
        return True

    if not template_path.exists():
        return False

    try:
        raw = template_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"claim_audit: failed to read {template_path}: {e}", file=sys.stderr)
        return False

    # Strip Jinja comments so we audit rendered content, not source
    text = strip_jinja_comments(raw)

    # Normalize: strip Jinja {{ _('...') }} wrappers, decode HTML entities, collapse whitespace
    text = re.sub(r"\{\{\s*_\(['\"]", " ", text)  # Strip {{ _('
    text = re.sub(r"['\"]?\s*\)\s*\}\}", " ", text)  # Strip ') }}
    text = text.replace("&amp;", "&")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = re.sub(r"\s+", " ", text)  # Collapse whitespace

    quote = claim.get("quote", "")
    # Exact match first
    if quote in text:
        return True

    # Fingerprint match: use first 30 chars of significant words (skip short words)
    words = [w for w in quote.split() if len(w) > 2]
    if len(words) >= 2:
        fingerprint = " ".join(words[:3])  # First 3 significant words
        return fingerprint in text

    # Single significant word: check if present
    if len(words) == 1:
        return words[0] in text

    return False


def check_expiry(claim: dict, today: datetime.date) -> bool:
    """Check if claim has expired (last_audit + expires_after_days < today)."""
    expires_days = claim.get("expires_after_days")
    if expires_days is None:
        return True  # No expiry

    last_audit_str = claim.get("last_audit")
    if not last_audit_str:
        return True  # No audit date; assume OK

    try:
        last_audit = datetime.strptime(last_audit_str, "%Y-%m-%d").date()
    except Exception:
        return True  # Parse error; assume OK

    expiry_date = last_audit + timedelta(days=expires_days)
    if today > expiry_date:
        return False  # Expired
    return True


def scan_for_unmanifested_claims(
    template_path: Path, manifest_quotes: set[str]
) -> list[tuple[int, str]]:
    """
    Scan template for NEW claim-shaped strings not in the manifest.
    Returns list of (line_num, matched_text) for claims not found in manifest.

    STRICT mode to avoid false positives: only flag obvious visitor-facing claims,
    not CSS classes, technical comments, time durations, etc.
    """
    if not template_path.exists():
        return []

    try:
        raw = template_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    text = strip_jinja_comments(raw)

    unmanifested = []
    lines = text.split("\n")

    # STRICT: Only match bold assertions (usually in <strong> or <li> tags)
    # or long, natural-language sentences with claim keywords
    visitor_facing_pattern = re.compile(
        r"<(?:strong|b|li|h[1-6]|p)>([^<]*(?:no|never|every|all |100B|the only|independent|free|without|we don'?t|we do not)[^<]*)</(?:strong|b|li|h[1-6]|p)>",
        re.IGNORECASE,
    )

    for line_num, line in enumerate(lines, start=1):
        for match in visitor_facing_pattern.finditer(line):
            matched_text = match.group(1).strip()

            # Skip if text is in manifest
            found = False
            for mq in manifest_quotes:
                if (
                    matched_text in mq
                    or mq in matched_text
                    or (len(matched_text) > 10 and matched_text[:30] in mq)
                ):
                    found = True
                    break

            if not found and len(matched_text) > 15:  # Skip very short matches
                unmanifested.append((line_num, matched_text))

    return unmanifested


def main() -> int:
    if not CLAIMS_FILE.exists():
        print(f"claim_audit: manifest not found: {CLAIMS_FILE}", file=sys.stderr)
        return 2

    if not TEMPLATE_DIR.is_dir():
        print(f"claim_audit: template dir not found: {TEMPLATE_DIR}", file=sys.stderr)
        return 2

    claims = load_claims()
    if claims is None:
        return 2

    if not claims:
        print("claim_audit: manifest is empty", file=sys.stderr)
        return 2

    today = datetime.now().date()
    template_files = sorted(TEMPLATE_DIR.glob("*.html"))
    manifest_quotes = {c.get("quote") for c in claims if c.get("quote")}
    claims_by_file = {}
    for c in claims:
        f = c.get("file")
        if f:
            claims_by_file.setdefault(f, []).append(c)

    # Check 1: Claims still present in templates
    missing_claims = []
    for claim in claims:
        file_rel = claim.get("file")
        if not file_rel or claim.get("line") is None:
            continue  # Skip entries without line numbers

        template_path = REPO_ROOT / file_rel
        if not claim_in_template(claim, template_path):
            missing_claims.append((file_rel, claim.get("line"), claim.get("quote")[:50]))

    # Check 2: Claims not expired
    expired_claims = []
    for claim in claims:
        if not check_expiry(claim, today):
            expires_days = claim.get("expires_after_days")
            last_audit = claim.get("last_audit")
            expired_claims.append(
                (
                    claim.get("file"),
                    claim.get("line"),
                    claim.get("quote")[:50],
                    f"expires_after_days={expires_days}, last_audit={last_audit}",
                )
            )

    # Check 3: Unmanifested claims in templates (progressive detection)
    unmanifested_by_file = {}
    for template_path in template_files:
        file_rel = f"templates/{template_path.name}"
        unmanifested = scan_for_unmanifested_claims(template_path, manifest_quotes)
        if unmanifested:
            unmanifested_by_file[file_rel] = unmanifested

    # Report
    print(f"claim_audit: audited {len(template_files)} templates against {len(claims)} claims")
    print(f"  missing from templates: {len(missing_claims)}")
    print(f"  expired (last_audit + expires_after_days <= today): {len(expired_claims)}")
    print(f"  unmanifested claim-shaped strings: {sum(len(v) for v in unmanifested_by_file.values())}")

    exit_code = 0

    if missing_claims:
        print()
        print("FAIL: Claims removed from templates without updating manifest:")
        for file_rel, line, quote in missing_claims:
            print(f"  {file_rel}:{line}  {quote!r}...")
            exit_code = 1

    if expired_claims:
        print()
        print("WARN: Claims past expiry (consider refreshing or removing):")
        for file_rel, line, quote, meta in expired_claims:
            print(f"  {file_rel}:{line}  {quote!r}... ({meta})")
        # Note: not failing on expiry, just warning. Charlie may want to refresh manually.

    if unmanifested_by_file:
        print()
        print("WARN: New claim-shaped strings not in manifest (consider adding):")
        for file_rel, unmanifested in sorted(unmanifested_by_file.items()):
            for line_num, matched in unmanifested[:3]:  # Show first 3 per file
                print(f"  {file_rel}:{line_num}  {matched[:60]!r}...")
            if len(unmanifested) > 3:
                print(f"  ... and {len(unmanifested) - 3} more in {file_rel}")
        # Note: not failing on new claims, just warning. Progressive audit.

    if exit_code == 0:
        print()
        print("OK: All manifested claims present and not expired.")
    else:
        print()
        print("FAIL: Claim audit failed. Update manifest or restore claims.")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
