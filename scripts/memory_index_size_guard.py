#!/usr/bin/env python3
"""memory_index_size_guard walker — hourly check that the auto-memory
root index (MEMORY.md) stays inside the claude auto-memory read window.

The claude auto-memory loader silently truncates MEMORY.md at a
hard-coded byte cap (~25000 bytes / 24.4 KiB) AND has a soft line
guideline (~200 lines). Anything past either limit is dropped from
the always-injected context with no error — the "quietest liar"
failure mode applied to the rulebook itself (2026-08-31 restructure).

This guard pages BEFORE truncation so a growing index never silently
loses standing rules again.

Thresholds (two tiers — soft warn first, hard-cap warn second/louder):
  bytes: WARN at 24200, HARD_WARN at 24500 — hard loader cap ~24985
  lines: WARN at 190,   HARD_WARN at 198   — hard read guideline ~200

Rationale (2026-09-03): Tier-0 full-text rules pushed the accepted
MEMORY.md size to ~23800B (Charlie's 2026-09-02 ruling). Old 20000B
warn tripped hourly on accepted-size — noise. Soft warn sits ~400B
above current accepted size (silent by default); hard warn ~500B
before truncation. Soft-to-hard gap = 300B (tight, but hard cap is
close so any growth past soft means real pressure).

Two distinct signals — do not conflate:
  ok=True + findings_count=0 → MEMORY.md within both thresholds. Green.
  ok=True + findings_count=N → over one/both thresholds. Paged via
      l1_pager.check_walker_findings. Restructure/flush needed.
      HARD tier tags message with 'CRITICAL' for louder pager copy.
  ok=False → the check itself failed: file missing, unreadable.
      Increments consecutive_failures, pages via check_walker_failing
      after 3.

Cadence: 3600s (1 hour). Related: 2026-08-31 memory restructure
(root ROOT INDEX + rules-full.md + index-archive.md sub-indexes),
2026-09-02 Tier-0 full-text expansion.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

logging.basicConfig(
    format="%(asctime)s [memory_index_size_guard] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "memory_index_size_guard"
WALKER_CADENCE_SECONDS = 3600

DEFAULT_MEMORY_MD = os.path.expanduser(
    "~/.claude/projects/-Users-charliebruce--openclaw-workspace/memory/MEMORY.md"
)
BYTES_WARN = 24200       # soft warn — above accepted Tier-0 size (23799 on 2026-09-03)
BYTES_HARD_WARN = 24500  # loud warn — ~500B before hard loader cap ~24985
LINES_WARN = 190         # soft warn — above accepted line count
LINES_HARD_WARN = 198    # loud warn — ~2 lines before hard guideline ~200


def run_check(path: str) -> tuple[bool, int, str]:
    if not os.path.isfile(path):
        return False, 0, f"check failed: MEMORY.md not found: {path}"
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return False, 0, f"check failed: cannot read {path}: {exc}"

    n_bytes = len(raw)
    n_lines = raw.count(b"\n") + (0 if raw.endswith(b"\n") or not raw else 1)

    breaches = []
    if n_bytes > BYTES_HARD_WARN:
        breaches.append(f"CRITICAL bytes={n_bytes}>{BYTES_HARD_WARN} (~500B from hard cap 24985)")
    elif n_bytes > BYTES_WARN:
        breaches.append(f"bytes={n_bytes}>{BYTES_WARN}")
    if n_lines > LINES_HARD_WARN:
        breaches.append(f"CRITICAL lines={n_lines}>{LINES_HARD_WARN} (near hard guideline 200)")
    elif n_lines > LINES_WARN:
        breaches.append(f"lines={n_lines}>{LINES_WARN}")

    if breaches:
        return True, len(breaches), (
            f"MEMORY.md over threshold ({', '.join(breaches)}) — "
            f"restructure/flush to sub-indexes before the ~24985B/~200L hard cap truncates rules"
        )

    return True, 0, f"OK: MEMORY.md {n_bytes}B / {n_lines} lines (under {BYTES_WARN}B / {LINES_WARN}L)"


def main() -> int:
    path = os.environ.get("MEMORY_MD_PATH", DEFAULT_MEMORY_MD)

    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    log.info("start: path=%s", path)

    ok, n_findings, msg = run_check(path)
    if ok and n_findings == 0:
        log.info("PASS: %s", msg)
    elif ok:
        log.warning("OVER THRESHOLD: %s", msg)
    else:
        log.error("FAIL: %s", msg)

    db.write_walker_health_end(
        WALKER_NAME,
        ok=ok,
        message=msg,
        findings_count=n_findings if ok else None,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
