"""Every walker must import with no DB and no network. A walker that cannot
import is a walker that never runs -- and never reports that it isn't.

WHAT HAPPENED (2026-10-17 → 10-19). The retry-before-negative PR renamed
two_way_toml_verifier_walker._fetch_toml to _fetch_toml_once. Two walkers
imported the old private name:

  scripts/token_icon_walker.py:49            top-level import
  scripts/claim_verify_sampler_walker.py:214 lazy, inside a helper

token_icon_walker then exited 1 at its import line on every hourly tick for
47 hours. Nothing paged. walker_health kept showing the LAST GOOD RUN with
ok=True and consecutive_failures=0, because the health row is written by the
walker itself and the walker never got far enough to write anything. The
only evidence was `launchctl list` showing last_exit=1 and a stack of
identical tracebacks in launchd_logs/token_icon_walker.err.log.

claim_verify_sampler is weekly; its lazy import would have fired ~10-24.

TWO GUARDS, so this class of failure cannot recur silently:
  1. Smoke-import every *_walker.py under a hermetic env. Any ImportError,
     SyntaxError or import-time crash fails CI before merge.
  2. Forbid importing underscore-prefixed names from
     two_way_toml_verifier_walker anywhere outside that module. Private
     names are not an API; the public one is fetch_toml_with_retry.

Hermetic: DATABASE_URL is cleared for the import; walkers that would touch
the network do so only inside main(), which is never called here.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WALKERS = sorted(
    list(ROOT.glob("*_walker.py")) + list((ROOT / "scripts").glob("*_walker.py"))
)
assert len(WALKERS) >= 30, f"expected the walker fleet, found {len(WALKERS)}"


def _import_hermetic(path: pathlib.Path):
    """Import a file as a throwaway module with DB env cleared and output
    swallowed. Returns the module; raises whatever the import raises."""
    saved = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = ""
    for p in (str(ROOT), str(ROOT / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        spec = importlib.util.spec_from_file_location(f"smoke_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            spec.loader.exec_module(mod)
        return mod
    finally:
        if saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved


# ── guard 1: every walker imports ───────────────────────────────────
@pytest.mark.parametrize("walker", WALKERS, ids=[w.relative_to(ROOT).as_posix() for w in WALKERS])
def test_walker_imports_cleanly(walker):
    try:
        _import_hermetic(walker)
    except SystemExit as e:  # a walker that sys.exit()s at import is also dead
        pytest.fail(f"{walker.name} called sys.exit({e.code}) at import time")


# ── guard 2: no private-name imports from the TOML verifier ─────────
PRIVATE_IMPORT = re.compile(
    r"from\s+two_way_toml_verifier_walker\s+import\s+([^\n]+)"
)


def _private_names_imported(text: str) -> list[str]:
    found = []
    for m in PRIVATE_IMPORT.finditer(text):
        names = m.group(1)
        # strip a trailing comment and parenthesised multi-line lists
        names = names.split("#", 1)[0].replace("(", "").replace(")", "")
        for n in names.split(","):
            n = n.strip().split(" as ")[0].strip()
            if n.startswith("_"):
                found.append(n)
    return found


def test_no_module_imports_private_names_from_toml_verifier():
    offenders = {}
    for py in list(ROOT.glob("*.py")) + list((ROOT / "scripts").glob("*.py")):
        if py.name == "two_way_toml_verifier_walker.py":
            continue
        priv = _private_names_imported(py.read_text(encoding="utf-8"))
        if priv:
            offenders[py.relative_to(ROOT).as_posix()] = priv
    assert offenders == {}, (
        "private names are not an API; use fetch_toml_with_retry: " + repr(offenders)
    )


# ── the two specific regressions ────────────────────────────────────
class TestTokenIconWalker:
    def test_binds_the_public_retrying_fetcher(self):
        mod = _import_hermetic(ROOT / "scripts" / "token_icon_walker.py")
        import two_way_toml_verifier_walker as v
        assert mod.fetch_toml_with_retry is v.fetch_toml_with_retry

    def test_no_longer_references_the_dead_name(self):
        src = (ROOT / "scripts" / "token_icon_walker.py").read_text(encoding="utf-8")
        assert "_fetch_toml(" not in src
        assert "import _fetch_toml" not in src


class TestClaimVerifySampler:
    def test_lazy_helper_resolves(self, monkeypatch):
        """The lazy import only runs when the helper is CALLED, which is why
        guard 1 cannot catch it. Call it with the fetcher stubbed."""
        mod = _import_hermetic(ROOT / "scripts" / "claim_verify_sampler_walker.py")
        import two_way_toml_verifier_walker as v
        seen = []
        monkeypatch.setattr(v, "fetch_toml_with_retry",
                            lambda url, *a, **k: (seen.append(url) or ({"ok": 1}, None)))
        assert mod._fetch_toml_helper("https://x.example/.well-known/xrp-ledger.toml") == ({"ok": 1}, None)
        assert seen == ["https://x.example/.well-known/xrp-ledger.toml"]

    def test_no_longer_references_the_dead_name(self):
        src = (ROOT / "scripts" / "claim_verify_sampler_walker.py").read_text(encoding="utf-8")
        assert "import _fetch_toml" not in src


def test_public_fetcher_signature_is_what_callers_expect():
    """Both callers unpack (toml, err). Pin that contract."""
    import inspect
    import two_way_toml_verifier_walker as v
    sig = inspect.signature(v.fetch_toml_with_retry)
    assert list(sig.parameters)[0] == "url"
    assert sig.return_annotation in (inspect.Signature.empty, "tuple[dict | None, str | None]") or \
        "tuple" in str(sig.return_annotation)
