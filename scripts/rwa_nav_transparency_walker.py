"""rwa_nav_transparency_walker — RESEARCH SKELETON, not live.

Charlie ruling 2026-09-22 Tue 6:56 PM ET: greenlight the transparency-PDF
path as research. Fetch each RWA family's latest transparency PDF, record
sha256 + as_of + URL, extract NAV where the layout is stable, queue for
curator read anything that doesn't parse cleanly. NO LIVE USE until Charlie
has seen three clean cycles.

Contract per family (from rwa_nav_sources.yaml → nav_transparency_pdf_url):
  1. HTTP GET the PDF (User-Agent identifies xrpldashboard).
  2. sha256 the raw bytes; record byte-length.
  3. Extract text via pypdf; try `_extract_nav_from_text` regex heuristics.
  4. Extract as_of date from PDF text (or fall back to Last-Modified header).
  5. Write cache row: rwa_nav_transparency_cache/YYYY-MM/<family>.json
     {family, url, sha256, byte_length, fetched_at, as_of, nav_usd,
      extraction_status, extractor_notes[]}.
  6. If extraction_status != 'ok': append a line to
     rwa_nav_transparency_cache/queue.md so Charlie's curator sweep catches it.

RESEARCH-ONLY: this walker writes to disk only. Nothing enters PG or the
signed leaf until Charlie flips a separate env gate after three clean cycles.

Env-gate: RWA_NAV_TRANSPARENCY_ENABLED. Default off (skip; walker_health
records 'disabled_by_env' and exits 0).

Deps: `pypdf` (pip install pypdf). Optional at import time; walker
records a clean walker_health message if missing.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import ssl
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import certifi
import yaml

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db  # noqa: E402


WALKER_NAME = "rwa_nav_transparency_walker"
WALKER_CADENCE_SECONDS = 86400  # daily
LAST_OK_STAMP = os.path.join(HERE, "launchd_state", f"{WALKER_NAME}_last_ok")

ENABLED = os.environ.get("RWA_NAV_TRANSPARENCY_ENABLED", "0") in ("1", "true", "yes")

NAV_SOURCES_YAML = os.path.join(HERE, "rwa_nav_sources.yaml")
CACHE_DIR = os.path.join(HERE, "rwa_nav_transparency_cache")
CURATOR_QUEUE_FILE = os.path.join(CACHE_DIR, "queue.md")
HTTP_TIMEOUT_SECONDS = 30.0
PDF_MAX_BYTES = 32 * 1024 * 1024  # 32 MB safety cap

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

# Charlie fills these in as he learns the operators' actual PDF URLs.
# The yaml `nav_transparency_pdf_url` field is the canonical source per
# family; the fallbacks here are the ones we KNOW exist from public docs.
_FALLBACK_HINTS = {
    # OpenEden: Trust & Transparency page names a "third-party fund
    # administrator will provide daily and monthly NAV reports" but the
    # docs page doesn't link a stable PDF URL. Curator finds the specific
    # PDF URL and puts it in the yaml as nav_transparency_pdf_url.
    "openeden": "https://docs.openeden.com/tbill/trust-and-transparency",
    # Ondo: portfolio-overview page shows current NAV in JS. Docs link
    # goes to methodology, not a report. Curator finds the disclosure PDF
    # URL and puts it in the yaml.
    "ondo_finance": "https://docs.ondo.finance/qualified-access-products/ousg/overview",
    # Midas: not_public per registry; skipped.
}


# ── Fetch + hash + extract ─────────────────────────────────────────────

def _fetch_pdf(url: str) -> tuple[bytes | None, dict]:
    """GET the URL. Returns (body, meta). body is bytes on success, None
    on failure. meta always has: http_status, content_type, last_modified,
    error (if any)."""
    meta = {
        "http_status": None,
        "content_type": "",
        "last_modified": None,
        "error": None,
    }
    try:
        req = Request(url, headers={
            "User-Agent": "xrpldashboard-nav-transparency-walker/1.0 (+https://xrpldashboard.com)",
            "Accept": "application/pdf, text/html;q=0.5",
        })
        with urlopen(req, timeout=HTTP_TIMEOUT_SECONDS, context=_SSL_CTX) as resp:
            meta["http_status"] = resp.getcode()
            meta["content_type"] = resp.headers.get("Content-Type", "")
            meta["last_modified"] = resp.headers.get("Last-Modified")
            body = resp.read(PDF_MAX_BYTES + 1)
            if len(body) > PDF_MAX_BYTES:
                meta["error"] = "response_exceeded_max_bytes"
                return None, meta
            return body, meta
    except HTTPError as e:
        meta["http_status"] = e.code
        meta["error"] = f"http_{e.code}"
    except (URLError, TimeoutError, OSError) as e:
        meta["error"] = f"fetch_error:{type(e).__name__}"
    except Exception as e:
        meta["error"] = f"unexpected:{type(e).__name__}"
    return None, meta


_NAV_PATTERNS = [
    # "NAV per token: $X.XX" / "NAV per TBILL: $X.XX"
    re.compile(r"NAV\s*(?:per\s+(?:token|TBILL|OUSG|share))?[\s:=]{0,4}\$?\s*([0-9]+\.[0-9]{2,8})", re.IGNORECASE),
    # "Net Asset Value: $X.XX"
    re.compile(r"Net\s+Asset\s+Value[^\$]{0,40}\$\s*([0-9]+\.[0-9]{2,8})", re.IGNORECASE),
    # "Token Price ... X.XX" (OpenEden phrasing)
    re.compile(r"Token\s+Price[^0-9]{0,30}([0-9]+\.[0-9]{4,8})", re.IGNORECASE),
]

_AS_OF_PATTERNS = [
    re.compile(r"[Aa]s\s+of\s+(\d{4}-\d{2}-\d{2})"),
    re.compile(r"[Aa]s\s+of\s+([A-Z][a-z]+ \d{1,2},? \d{4})"),
    re.compile(r"(?:Report|Statement)\s+[Dd]ate[:\s]+(\d{4}-\d{2}-\d{2})"),
]


def _extract_pdf_text(pdf_bytes: bytes) -> tuple[str | None, str | None]:
    """Return (text, err). text is a single string of all pages joined
    with newlines. err names the exception on failure."""
    try:
        import pypdf
    except ImportError:
        return None, "pypdf_not_installed"
    try:
        import io
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(chunks), None
    except Exception as e:
        return None, f"pypdf_error:{type(e).__name__}"


def _extract_nav_from_text(text: str) -> tuple[float | None, str | None]:
    """Try each regex; return (nav_usd, pattern_name) on first match, else
    (None, None). Rejects implausible values (< 0.01 or > 100000)."""
    for i, pat in enumerate(_NAV_PATTERNS):
        m = pat.search(text)
        if m:
            try:
                val = float(m.group(1))
                if 0.01 < val < 100000:
                    return val, f"pattern_{i}"
            except (TypeError, ValueError):
                continue
    return None, None


def _extract_as_of_from_text(text: str) -> str | None:
    """Return the earliest-matching as_of date as ISO 'YYYY-MM-DD' or None."""
    for pat in _AS_OF_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1)
        # ISO already?
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return raw
        # "September 22, 2026" style
        for fmt in ("%B %d, %Y", "%B %d %Y"):
            try:
                return dt.datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
    return None


# ── Cache write + curator queue ────────────────────────────────────────

def _cache_dir_for(now_utc: dt.datetime) -> str:
    ym = now_utc.strftime("%Y-%m")
    d = os.path.join(CACHE_DIR, ym)
    os.makedirs(d, exist_ok=True)
    return d


def _append_curator_queue(family: str, url: str, reasons: list[str]) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    line = (
        f"- {dt.datetime.now(dt.timezone.utc).isoformat()} · {family} · "
        f"{url} · needs_curator_read · {', '.join(reasons)}\n"
    )
    with open(CURATOR_QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def _write_family_cache(family: str, row: dict, now_utc: dt.datetime) -> str:
    path = os.path.join(_cache_dir_for(now_utc), f"{family}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(row, f, sort_keys=True, indent=2)
    os.replace(tmp, path)
    return path


def _stamp_last_ok() -> None:
    try:
        os.makedirs(os.path.dirname(LAST_OK_STAMP), exist_ok=True)
        with open(LAST_OK_STAMP, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


# ── Main ───────────────────────────────────────────────────────────────

def _load_registry() -> dict:
    with open(NAV_SOURCES_YAML) as f:
        return yaml.safe_load(f) or {}


def _pick_pdf_url(family_slug: str, cfg: dict) -> str | None:
    """Precedence: yaml's nav_transparency_pdf_url wins; fall back to the
    known hints for openeden/ondo; None disables the family for this run."""
    url = cfg.get("nav_transparency_pdf_url")
    if url:
        return url
    return _FALLBACK_HINTS.get(family_slug)


def process_family(family_slug: str, cfg: dict, now_utc: dt.datetime) -> dict:
    """Fetch + hash + extract for one family. Always writes a cache row;
    queues for curator when extraction is incomplete."""
    reasons: list[str] = []
    url = _pick_pdf_url(family_slug, cfg)
    row: dict = {
        "family": family_slug,
        "url": url,
        "fetched_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "sha256": None,
        "byte_length": None,
        "http_status": None,
        "content_type": None,
        "last_modified": None,
        "as_of": None,
        "nav_usd": None,
        "extraction_status": "not_attempted",
        "extractor_notes": [],
    }

    if cfg.get("nav_status") == "not_public":
        row["extraction_status"] = "skipped_not_public"
        _write_family_cache(family_slug, row, now_utc)
        return row

    if not url:
        row["extraction_status"] = "no_pdf_url_configured"
        reasons.append("no_pdf_url_configured")
        _append_curator_queue(family_slug, "", reasons)
        _write_family_cache(family_slug, row, now_utc)
        return row

    body, meta = _fetch_pdf(url)
    row["http_status"] = meta["http_status"]
    row["content_type"] = meta["content_type"]
    row["last_modified"] = meta["last_modified"]
    if body is None:
        row["extraction_status"] = "fetch_failed"
        reasons.append(meta.get("error") or "fetch_failed")
        _append_curator_queue(family_slug, url, reasons)
        _write_family_cache(family_slug, row, now_utc)
        return row

    row["sha256"] = hashlib.sha256(body).hexdigest()
    row["byte_length"] = len(body)

    ct = (meta["content_type"] or "").lower()
    if "pdf" not in ct and not body.startswith(b"%PDF"):
        row["extraction_status"] = "not_a_pdf"
        row["extractor_notes"].append(f"content_type={ct!r}, first4={body[:4]!r}")
        reasons.append(f"not_a_pdf ({ct})")
        _append_curator_queue(family_slug, url, reasons)
        _write_family_cache(family_slug, row, now_utc)
        return row

    text, terr = _extract_pdf_text(body)
    if text is None:
        row["extraction_status"] = "pdf_text_extract_failed"
        row["extractor_notes"].append(terr or "unknown")
        reasons.append(terr or "pdf_text_extract_failed")
        _append_curator_queue(family_slug, url, reasons)
        _write_family_cache(family_slug, row, now_utc)
        return row

    nav_usd, pat_name = _extract_nav_from_text(text)
    as_of = _extract_as_of_from_text(text)
    row["nav_usd"] = nav_usd
    row["as_of"] = as_of

    if nav_usd is None:
        row["extraction_status"] = "needs_curator_read"
        row["extractor_notes"].append("no NAV regex matched")
        reasons.append("no_nav_regex_matched")
        if not as_of:
            reasons.append("no_as_of_date_found")
        _append_curator_queue(family_slug, url, reasons)
    else:
        row["extraction_status"] = "ok" if as_of else "ok_no_as_of"
        row["extractor_notes"].append(f"nav_pattern={pat_name}")
        if not as_of:
            reasons.append("no_as_of_date_found")
            _append_curator_queue(family_slug, url, reasons)

    _write_family_cache(family_slug, row, now_utc)
    return row


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = "not_yet_stamped"
    try:
        if not ENABLED:
            message = "disabled_by_env_RWA_NAV_TRANSPARENCY_ENABLED=0"
            ok = True
            print(f"[{WALKER_NAME}] {message} (research-only walker, not live)")
            return 0

        os.makedirs(CACHE_DIR, exist_ok=True)
        registry = _load_registry()
        if not registry:
            message = "registry_empty"
            return 1

        now_utc = dt.datetime.now(dt.timezone.utc)
        summary = []
        for family_slug, cfg in registry.items():
            if not isinstance(cfg, dict):
                continue
            r = process_family(family_slug, cfg, now_utc)
            summary.append(f"{family_slug}={r['extraction_status']}"
                           + (f"({r['nav_usd']})" if r.get("nav_usd") else ""))
            print(f"[{WALKER_NAME}] {family_slug}: status={r['extraction_status']} "
                  f"nav={r.get('nav_usd')} as_of={r.get('as_of')} "
                  f"sha256={(r.get('sha256') or '')[:12]}")

        _stamp_last_ok()
        message = " ".join(summary) or "no_families"
        ok = True
        print(f"[{WALKER_NAME}] {message}")
        return 0
    except Exception as e:
        message = f"exception:{type(e).__name__}:{e}"
        raise
    finally:
        try:
            db.write_walker_health_end(
                WALKER_NAME, ok=ok,
                message=message or ("clean_no_message" if ok else "unlabeled_failure"),
            )
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
