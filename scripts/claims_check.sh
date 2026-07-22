#!/usr/bin/env bash
# claims_check.sh — Truth-Audit Layer 4 (Change-Safety) pre-push visibility.
#
# Reads CLAIMS.yaml at repo root, compares against `git diff --name-only`
# (staged + unstaged + untracked changes vs BASE, default HEAD), and
# prints which public claims the diff might affect. A path is
# considered to touch a claim if it appears (as a substring) in any of
# that claim's data_paths.
#
# Visibility, not a gate — exits 0 always. The doc's reasoning stands:
# blocking gets bypassed and punishes honest claim-updates. Surfacing
# lets a human eyeball whether the claim still holds before pushing.
#
# Manifest-maintenance heuristic:
#   - New read_*/write_* function in db.py with no CLAIMS.yaml entry
#     touching it → the script says so on stderr (advisory).
#   - New template file with no CLAIMS.yaml entry → same.
#
# See docs/TRUTH_AUDIT_DESIGN.md §Layer 4 (706bc0a) for the spec.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLAIMS="${REPO_ROOT}/CLAIMS.yaml"

if [[ ! -f "$CLAIMS" ]]; then
    echo "claims_check: CLAIMS.yaml not found at repo root — skipping." >&2
    exit 0
fi

BASE="${1:-HEAD}"

CHANGED_FILES=$(
    {
        git -C "$REPO_ROOT" diff --name-only "$BASE" 2>/dev/null || true
        git -C "$REPO_ROOT" ls-files --others --exclude-standard 2>/dev/null || true
    } | sort -u
)

if [[ -z "$CHANGED_FILES" ]]; then
    echo "claims_check: no diff vs $BASE."
    exit 0
fi

export CLAIMS_CHANGED="$CHANGED_FILES"
export CLAIMS_FILE="$CLAIMS"
export CLAIMS_BASE="$BASE"
export CLAIMS_REPO_ROOT="$REPO_ROOT"

python3 - <<'PY'
import os, re, sys, subprocess

claims_path = os.environ["CLAIMS_FILE"]
changed = [l.strip() for l in os.environ["CLAIMS_CHANGED"].splitlines() if l.strip()]
base = os.environ["CLAIMS_BASE"]
repo_root = os.environ["CLAIMS_REPO_ROOT"]

# Minimal YAML reader — we intentionally avoid a PyYAML dep so this
# script runs with a bare python3. The file follows a strict two-space
# indent shape maintained by hand; if that changes, swap to PyYAML.
def parse_claims(text):
    pages = {}
    cur_page = None
    cur_claim = None
    in_data_paths = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        # Page header:  "  /rlusd:"
        m = re.match(r"^ {2}(/[^:]+):\s*$", line)
        if m:
            cur_page = m.group(1)
            pages[cur_page] = []
            cur_claim = None
            in_data_paths = False
            continue
        # New claim within a page: "      - id: foo"
        m = re.match(r"^ {6}- id:\s*(\S+)", line)
        if m:
            cur_claim = {"id": m.group(1), "data_paths": []}
            pages.setdefault(cur_page, []).append(cur_claim)
            in_data_paths = False
            continue
        # Enter data_paths block: "        data_paths:"
        if re.match(r"^ {8}data_paths:\s*$", line):
            in_data_paths = True
            continue
        # A data_path list item: '          - "…"' or "'…'"
        if in_data_paths and re.match(r"^ {10}- ", line):
            val = line.strip()[2:].strip()
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            if cur_claim is not None:
                cur_claim["data_paths"].append(val)
            continue
        # Any other 8-space key ends the data_paths block.
        if in_data_paths and re.match(r"^ {8}\S", line):
            in_data_paths = False
    return pages

with open(claims_path) as f:
    pages = parse_claims(f.read())

# Match: a data_path "touches" a changed file if the file path appears
# as a substring of the data_path string. Also match on basename so a
# rename in one direction still surfaces.
hits = {}  # page -> set(claim_id)
covered_files = set()
for f in changed:
    base_name = os.path.basename(f)
    for page, claims in pages.items():
        for c in claims:
            for dp in c["data_paths"]:
                if f in dp or (base_name and base_name in dp):
                    hits.setdefault(page, set()).add(c["id"])
                    covered_files.add(f)
                    break

if not hits:
    print("claims_check: no diff touches known claim data_paths.")
else:
    print("claims_check: this diff touches claims on the following pages —")
    for page in sorted(hits):
        ids = ", ".join(sorted(hits[page]))
        print(f"  {page}: {ids}")
    print()
    print("Verify these claims still hold before pushing. See CLAIMS.yaml.")

# Manifest-maintenance advisories (stderr, non-blocking).
# These fire independent of the main claim-touch output: even if db.py
# is heavily referenced by claims, a *new* read_/write_ function may
# feed a claim that hasn't been catalogued yet.
with open(claims_path) as f:
    claims_text = f.read()

uncovered_readers = []
uncovered_templates = []
for f in changed:
    if f == "db.py":
        try:
            diff = subprocess.check_output(
                ["git", "diff", base, "--", f], text=True, cwd=repo_root
            )
            for line in diff.splitlines():
                m = re.match(r"^\+def (read_\w+|write_\w+)\(", line)
                if m and m.group(1) not in claims_text:
                    uncovered_readers.append(m.group(1))
        except Exception:
            pass
    if f.startswith("templates/") and f.endswith(".html") and f not in covered_files:
        uncovered_templates.append(f)

if uncovered_readers or uncovered_templates:
    print("", file=sys.stderr)
    print("claims_check advisory (manifest may need update):", file=sys.stderr)
    for r in uncovered_readers:
        print(f"  db.py adds {r}() — no CLAIMS.yaml entry references it.", file=sys.stderr)
    for t in uncovered_templates:
        print(f"  {t} touched but no CLAIMS.yaml entry references it.", file=sys.stderr)

# Always exit 0.
PY
