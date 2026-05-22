#!/usr/bin/env python3
"""Origin guard — fail-loud audit of third-party origins in templates.

Scans every template under templates/ for `<script src>`, `<link href>`,
and `<iframe src>` attributes that load from a non-self origin. Visitor-
facing claims on /security ("no analytics, no pixels, no widgets") and
/about ("we do not depend on third-party analytics APIs") are integrity
promises — they must stay verifiable as the template set grows.

Counts visitor-facing third-party origins only. Outbound `<a href>` is
visitor-initiated navigation, not a page-initiated load, so it's
excluded.

A small EXPECTED_ORIGINS allowlist documents the deps we already
disclose on /security. Anything outside it fails the run. The endgame
is an empty allowlist — every entry there is also a task on the
self-host roadmap.

Run with:
    python3 tools/origin_guard.py

Exits 0 if every external origin in the templates is on the allowlist
(or self-hosted). Exits non-zero with a list otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates"

# Origins explicitly disclosed on /security as currently-loaded third-
# party deps. Keep this list shrinking — every entry corresponds to a
# vendoring task on #93. The test does NOT pass silently when an entry
# is removed from here — if a template still loads from a removed
# origin, the guard fails. That's the point.
EXPECTED_ORIGINS = {
    "cdn.jsdelivr.net",  # Geist fonts CSS, Lenis, GSAP, ScrollTrigger, CountUp — vendor-host pending
}

# Tags whose src/href attributes load network resources the page
# initiates. <a href> is excluded because it's user-initiated navigation
# (visitors clicking outbound links isn't a "third-party script" claim
# violation). <img src> is excluded for now since all current img loads
# are /static or data: — extend if that changes.
SCAN_TAGS = ("script", "link", "iframe")

# Strip Jinja comments before scanning so commented-out blocks don't
# trip the guard. Jinja `{# ... #}` blocks don't render to HTML.
JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)

# Match the relevant attribute on each of the scan-tags. Cheap and
# good-enough for static templates — we are not parsing HTML.
ATTR_RE = re.compile(
    r"<(?P<tag>" + "|".join(SCAN_TAGS) + r")\b[^>]*?\b(?:src|href)\s*=\s*['\"](?P<url>[^'\"]+)['\"]",
    re.IGNORECASE,
)

# Origin extractor — pull the host from an absolute URL. Relative paths
# (`/static/foo.js`, `foo.css`) and protocol-relative URLs without a
# host fall through and are treated as same-origin.
ORIGIN_RE = re.compile(r"^https?://([^/]+)/?", re.IGNORECASE)


def scan_template(path: Path) -> list[tuple[int, str, str, str]]:
    """Return [(line_number, tag, origin, full_url)] for each external
    origin loaded by a script/link/iframe in `path`. Same-origin URLs
    (relative paths, /static/*, etc.) are ignored.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    stripped = JINJA_COMMENT_RE.sub(
        lambda m: "\n" * m.group(0).count("\n"),
        raw,
    )

    findings: list[tuple[int, str, str, str]] = []
    for match in ATTR_RE.finditer(stripped):
        url = match.group("url")
        origin_match = ORIGIN_RE.match(url)
        if not origin_match:
            continue  # relative or non-http
        origin = origin_match.group(1).lower()
        line_no = stripped.count("\n", 0, match.start()) + 1
        findings.append((line_no, match.group("tag").lower(), origin, url))
    return findings


def main() -> int:
    if not TEMPLATE_DIR.is_dir():
        print(f"origin_guard: template dir not found: {TEMPLATE_DIR}", file=sys.stderr)
        return 2

    all_findings: dict[str, list[tuple[Path, int, str, str]]] = {}
    template_files = sorted(TEMPLATE_DIR.glob("*.html"))
    for path in template_files:
        for line_no, tag, origin, url in scan_template(path):
            all_findings.setdefault(origin, []).append((path, line_no, tag, url))

    unexpected = {o: f for o, f in all_findings.items() if o not in EXPECTED_ORIGINS}
    expected_present = {o: f for o, f in all_findings.items() if o in EXPECTED_ORIGINS}
    stale_allowlist = EXPECTED_ORIGINS - set(all_findings)

    print(f"origin_guard: scanned {len(template_files)} templates")
    print(f"  unexpected origins: {len(unexpected)}")
    print(f"  expected origins still present: {len(expected_present)}")

    if expected_present:
        print()
        print("Disclosed (on EXPECTED_ORIGINS allowlist):")
        for origin, hits in sorted(expected_present.items()):
            paths = sorted({p.name for p, _, _, _ in hits})
            print(f"  {origin}  ({len(hits)} loads across {len(paths)} templates)")
            for p, ln, tag, url in hits[:3]:
                print(f"    {p.name}:{ln} <{tag}> {url}")
            if len(hits) > 3:
                print(f"    … and {len(hits) - 3} more")

    if unexpected:
        print()
        print("UNEXPECTED third-party origins (NOT on allowlist):")
        for origin, hits in sorted(unexpected.items()):
            print(f"  {origin}")
            for p, ln, tag, url in hits:
                print(f"    {p.name}:{ln} <{tag}> {url}")

    if stale_allowlist:
        print()
        print("Allowlist entries with no current loads (drop from EXPECTED_ORIGINS):")
        for origin in sorted(stale_allowlist):
            print(f"  {origin}")

    print()
    if unexpected:
        print("FAIL: unexpected external origins present. Either vendor them,")
        print("      remove them, or add to EXPECTED_ORIGINS with a disclosure")
        print("      on /security.")
        return 1
    if stale_allowlist:
        print("WARN: allowlist drift — entries above are unused; clean them up.")
        return 0  # warn, don't fail, on cleanup-pending state
    print("OK: every third-party origin is disclosed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
