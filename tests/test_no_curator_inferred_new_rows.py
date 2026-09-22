"""Regression guard: no code path may INSERT tier='curator-inferred' into
token_category_history.

Charlie ruling 2026-09-22 Tue PM: 'curator-inferred' is a legacy DB tier
that maps to canonical 'labeled' per
shared_tier_verifier._DB_TIER_TO_CANONICAL. Old rows keep working
(mapping preserves display), but new INSERTs must land as one of the
five canonical tiers so the drift stops accumulating.

This test walks the tracked Python source and greps for any INSERT
statement that hardcodes 'curator-inferred' as the tier value. The db.py
schema comment + shared_tier_verifier mapping are both allowed to
mention the string — the guard is on INSERT sites only.
"""
from __future__ import annotations
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Allow-list: files that may reference 'curator-inferred' as a STRING
# for legacy-tier-mapping / schema-documentation / WHERE-clause reasons.
# These files may NOT contain INSERT statements that write the tier as
# 'curator-inferred'; the test still verifies that below.
ALLOW_REFERENCES = {
    "shared_tier_verifier.py",  # maps legacy → canonical
    "db.py",                    # schema comment + backfill WHERE clause
}

# Directories to skip when walking the repo.
SKIP_DIRS = {".git", "venv", "__pycache__", "node_modules", "nav_screenshots",
             "signed_snapshots", "signed_registry_snapshots", "launchd_logs",
             "tests", "deploy", "signed_verified_tokens"}

INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+token_category_history[\s\S]{0,2000}?\)\s*(?:VALUES|SELECT|RETURNING|;)",
    re.IGNORECASE,
)
CURATOR_INFERRED = "curator-inferred"


def _walk_py():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def test_no_insert_uses_curator_inferred():
    """Fail if any INSERT INTO token_category_history VALUES … includes
    the literal string 'curator-inferred'."""
    offenders = []
    for path in _walk_py():
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        for m in INSERT_RE.finditer(src):
            block = m.group(0)
            if CURATOR_INFERRED in block:
                # Report the line number of the offending INSERT block.
                line = src[: m.start()].count("\n") + 1
                offenders.append(f"{os.path.relpath(path, REPO)}:{line}")
    assert not offenders, (
        "New INSERTs with tier='curator-inferred' are forbidden — use a "
        "canonical tier ('verified', 'self-described', 'labeled', 'bare', "
        f"'unknown'). Offenders: {offenders}"
    )


def test_curator_inferred_string_references_are_allowlisted():
    """Fail if a NEW file (outside ALLOW_REFERENCES) starts mentioning the
    legacy tier — even in a comment. Forces conscious ack of the rule."""
    unknown = []
    for path in _walk_py():
        rel = os.path.relpath(path, REPO)
        base = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        if CURATOR_INFERRED not in src:
            continue
        if base in ALLOW_REFERENCES:
            continue
        # New reference outside allow-list — must be an INSERT (already
        # tested above) or a comment about the rule. Explicitly allow
        # comments/strings that name the rule (mention 'legacy' or
        # 'no_curator_inferred').
        for i, line in enumerate(src.splitlines(), start=1):
            if CURATOR_INFERRED in line:
                if "legacy" in line.lower() or "no_curator_inferred" in line.lower():
                    continue
                unknown.append(f"{rel}:{i}: {line.strip()[:120]}")
    assert not unknown, (
        "Unexpected 'curator-inferred' references (add file to "
        f"ALLOW_REFERENCES if legitimate): {unknown}"
    )
