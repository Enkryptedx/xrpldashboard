#!/usr/bin/env python3
"""openclaw_cli_backend_drift walker — hourly check that the
--fallback-model claude-opus-4-8 flag remains in the OpenClaw
Anthropic-CLI launcher (dist/cli-backend-*.js).

Guards the 2026-08-31 monkey-patch that Homebrew/npm can overwrite on
any OpenClaw upgrade. The dist file is hash-named, so an upgrade may
drop a NEW filename with NO patch while leaving the old file behind —
either state can silently reintroduce the sonnet cascade the patch
was written to kill.

Two distinct signals — do not conflate:

  ok=True + findings_count=0 → every live cli-backend-*.js in
      OPENCLAW_DIST_DIR contains BOTH `--fallback-model` and
      `claude-opus-4-8`. Green.
  ok=True + findings_count=N → N cli-backend-*.js files are MISSING
      the flag pair. Paged via l1_pager.check_walker_findings.
  ok=False → the check itself failed: dist dir missing, no
      cli-backend-*.js found, unreadable file. Increments
      consecutive_failures, pages via check_walker_failing after 3.

Cadence: 3600s (1 hour). Rationale: OpenClaw upgrades happen at most
weekly; hourly cadence caps undetected drift at one hour.

Message format on drift (surfaced to /walker_health + the pager):
    "drift: 1/1 cli-backend files MISSING flag: cli-backend-XYZ.js"
Message format on green:
    "OK: N cli-backend files carry --fallback-model claude-opus-4-8"
Message format on run failure:
    "check failed: <error summary>"

Related: 2026-08-31 runner-fix (project_openclaw_fallback_pin_drift_watcher.md).
"""
import glob
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

logging.basicConfig(
    format="%(asctime)s [openclaw_cli_backend_drift] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "openclaw_cli_backend_drift"
WALKER_CADENCE_SECONDS = 3600

DEFAULT_DIST_DIR = "/opt/homebrew/lib/node_modules/openclaw/dist"
REQUIRED_FLAG = "--fallback-model"
REQUIRED_MODEL = "claude-opus-4-8"
ANTHROPIC_BACKEND_MARKER = "buildAnthropicCliBackend"


def run_check(dist_dir: str) -> tuple[bool, int, str]:
    if not os.path.isdir(dist_dir):
        return False, 0, f"check failed: dist dir not found: {dist_dir}"

    pattern = os.path.join(dist_dir, "cli-backend-*.js")
    all_files = sorted(glob.glob(pattern))
    if not all_files:
        return False, 0, f"check failed: no cli-backend-*.js in {dist_dir}"

    anthropic_files: list[str] = []
    missing: list[str] = []
    for path in all_files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                body = fh.read()
        except OSError as exc:
            return False, 0, f"check failed: cannot read {os.path.basename(path)}: {exc}"
        if ANTHROPIC_BACKEND_MARKER not in body:
            continue
        anthropic_files.append(os.path.basename(path))
        if REQUIRED_FLAG not in body or REQUIRED_MODEL not in body:
            missing.append(os.path.basename(path))

    if not anthropic_files:
        return False, 0, (
            f"check failed: no {ANTHROPIC_BACKEND_MARKER} launcher found among "
            f"{len(all_files)} cli-backend files (openclaw layout may have changed)"
        )

    if missing:
        listed = ",".join(missing)
        return True, len(missing), (
            f"drift: {len(missing)}/{len(anthropic_files)} Anthropic-CLI launcher "
            f"file(s) MISSING {REQUIRED_FLAG} {REQUIRED_MODEL}: {listed}"
        )

    return True, 0, (
        f"OK: {len(anthropic_files)} Anthropic-CLI launcher file(s) carry "
        f"{REQUIRED_FLAG} {REQUIRED_MODEL} (of {len(all_files)} cli-backend files total)"
    )


def main() -> int:
    dist_dir = os.environ.get("OPENCLAW_DIST_DIR", DEFAULT_DIST_DIR)

    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    log.info("start: dist_dir=%s", dist_dir)

    ok, n_findings, msg = run_check(dist_dir)
    if ok and n_findings == 0:
        log.info("PASS: %s", msg)
    elif ok:
        log.warning("DRIFT: %s", msg)
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
