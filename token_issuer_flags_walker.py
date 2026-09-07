"""token_issuer_flags_walker — Mac-side AccountRoot flags snapshotter
for /token/<currency>/<issuer>'s "Ledger-level capabilities" panel.

Every 30 min, enumerates DISTINCT issuers from token_volume via
db.read_token_volume_issuers(), fetches AccountInfo(signer_lists=True)
per issuer through xrpl_client.get_client (Mac's LOCAL_NODE = LAN Lenovo
rippled), and upserts the flag/rate/key/signer-list summary into
token_issuer_flags_snapshot in Neon. The /token detail route reads that
table directly via db.read_token_issuer_flags() so the page stops
firing one live AccountInfo RPC per render.

Wired 2026-09-06 (approved Sep 4, dropped from every work order until
this one). Kills ~3/day walker_node_fallback rows for walker_name=
token_page. Same author intent as cold_storage_walker /
escrow_supply_walker (background writer → DB read).

Cadence: 30 min. Issuer flags change rarely (a TransferRate tweak or
a RegularKey rotation is a manual AccountSet transaction); 30 min gives
3 write cycles inside the /token route's 90-min staleness threshold —
one missed fire is invisible, two consecutive missed fires trip the
stale-cache banner.

Fetch failures are per-issuer: a single AccountInfo error leaves that
issuer with fetch_ok=False in the snapshot but doesn't abort the batch.
Only a total wipeout (0 fetched_ok across all issuers, i.e. LAN rippled
unreachable) returns (False, ...) so walker_health goes red.
"""
import logging
import ssl
import sys
import time
import urllib.request
import urllib.error

from xrpl.models.requests import AccountInfo

# macOS Python framework installs ship without a system CA bundle, so
# urllib.request.urlopen chokes with CERTIFICATE_VERIFY_FAILED on any
# HTTPS site. Use certifi's Mozilla bundle. Both Mac and Lenovo have
# certifi (it's a hard dep of the xrpl-py stack).
import certifi
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

import db
from xrpl_client import get_client

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # fallback

logging.basicConfig(
    format="%(asctime)s [token_issuer_flags_walker] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WALKER_NAME = "token_issuer_flags_walker"
WALKER_CADENCE_SECONDS = 1800  # 30 min

# xrp-ledger.toml fetch policy — 2026-09-06 (extension B of Charlie's
# A+B ruling). Per-issuer 24h cache; global ~1 fetch/sec rate limit;
# 5-second HTTP timeout; fail-open (a missing/broken toml never breaks
# the AccountRoot capture). See docs/TOKEN_CATEGORY_INFERRED.md if
# authored — otherwise the DDL comment on token_category_inferred is
# the design of record.
TOML_FETCH_CACHE_HOURS = 24
TOML_FETCH_INTERVAL_SEC = 1.0    # min gap between successive fetches
TOML_FETCH_TIMEOUT_SEC = 5
TOML_MAX_BYTES = 200_000         # refuse tomls larger than 200 KB
TOML_MAX_FETCHES_PER_CYCLE = 200 # bound total network per walker fire
_TOML_USER_AGENT = (
    "xrpldashboard-token-category-walker/1.0 "
    "(+https://xrpldashboard.com/methodology)"
)

# Map arbitrary toml `category` string values → our 5 lane classes.
# Toml authors write freely (some use "stablecoin", others "stable coin",
# "wrapped btc", "utility", "gaming", "meme", etc.). Anything we don't
# recognize passes through as 'other' so the visual lane defaults grey.
_TOML_CATEGORY_MAP = {
    "stablecoin": "stablecoin",
    "stable": "stablecoin",
    "stable coin": "stablecoin",
    "fiat": "stablecoin",
    "wrapped": "wrapped_major",
    "wrapped_major": "wrapped_major",
    "wrapped-major": "wrapped_major",
    "native": "native_utility",
    "native_utility": "native_utility",
    "native-utility": "native_utility",
    "utility": "native_utility",
    "memecoin": "memecoin",
    "meme": "memecoin",
    "meme_coin": "memecoin",
    "meme-coin": "memecoin",
}


def _normalize_toml_category(value):
    """Taxonomy v1 (2026-09-06 Charlie ruling): default for unknown /
    absent toml category is 'unlabeled' — 'other' was retired as a
    category, kept only as a client-side compatibility alias while
    old inferred rows drain."""
    if not value or not isinstance(value, str):
        return "unlabeled"
    return _TOML_CATEGORY_MAP.get(value.strip().lower(), "unlabeled")


def _fetch_toml(domain, log):
    """One HTTP GET for a domain's xrp-ledger.toml. Returns (status,
    text|None, error|None). status ∈ 'ok', 'http_error', 'timeout',
    'connection_error', 'too_large', 'unknown'. Fail-open: no exception
    is raised; caller records the log entry and moves on."""
    url = f"https://{domain.rstrip('/')}/.well-known/xrp-ledger.toml"
    req = urllib.request.Request(url, headers={"User-Agent": _TOML_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TOML_FETCH_TIMEOUT_SEC, context=_SSL_CONTEXT) as resp:
            code = resp.getcode()
            if code != 200:
                return f"http_{code}", None, f"HTTP {code}"
            raw = resp.read(TOML_MAX_BYTES + 1)
            if len(raw) > TOML_MAX_BYTES:
                return "too_large", None, f">{TOML_MAX_BYTES} bytes"
            try:
                return "ok", raw.decode("utf-8"), None
            except UnicodeDecodeError as e:
                return "decode_error", None, f"utf-8 decode: {e}"
    except urllib.error.HTTPError as e:
        return f"http_{e.code}", None, str(e)
    except urllib.error.URLError as e:
        return "connection_error", None, str(e)
    except TimeoutError as e:
        return "timeout", None, str(e)
    except Exception as e:
        return "unknown", None, f"{type(e).__name__}: {e}"


def _parse_toml_tokens(text, issuer):
    """Return list of {currency_hex, currency_display, category, source_url}
    entries from a toml [[TOKENS]] section. Only tokens whose 'issuer' field
    matches the caller's issuer are returned (a toml could technically list
    tokens for multiple issuers; scope narrows to what the walker owns).
    Silently skips malformed entries."""
    try:
        data = tomllib.loads(text)
    except Exception:
        return []
    entries = data.get("TOKENS") or []
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        e_iss = e.get("issuer")
        if e_iss and e_iss != issuer:
            continue
        # Currency can be a 3-char code or 40-char hex. Toml spec uses
        # `currency` for both. Preserve raw; the merge layer normalizes.
        e_cur = e.get("currency")
        if not e_cur or not isinstance(e_cur, str):
            continue
        # Some tomls quote the 3-char code, others the 40-hex. Length
        # test alone is enough — we don't try to normalize here.
        out.append({
            "currency_hex": e_cur,
            "currency_display": e.get("name") or e_cur,
            "category": _normalize_toml_category(e.get("category")),
        })
    return out


def collect_toml_categories(flag_rows, log):
    """For each snapshot row with a valid domain and past-cache due-time,
    fetch the toml, parse [[TOKENS]] for our issuer, and return the flat
    list of (currency_hex, issuer, currency_display, category, source_url)
    tuples ready for db.upsert_token_category_inferred(). Rate-limited to
    ~1 req/sec + capped per cycle so a bad domain host can't hold the
    walker."""
    if not flag_rows:
        return []
    fresh = db.read_token_toml_fetch_due(cache_hours=TOML_FETCH_CACHE_HOURS)
    candidates = []
    for r in flag_rows:
        iss = r["issuer"]
        if iss in fresh:
            continue
        # domain_hex → ASCII decoded and printable check
        dh = r.get("domain_hex")
        if not dh:
            continue
        try:
            dom = bytes.fromhex(dh).decode("ascii").strip()
        except (ValueError, UnicodeDecodeError):
            continue
        if not dom or not all(32 <= ord(c) < 127 for c in dom):
            continue
        # Strip scheme if the issuer wrote a URL in Domain
        for prefix in ("https://", "http://"):
            if dom.startswith(prefix):
                dom = dom[len(prefix):]
        dom = dom.split("/", 1)[0]
        if "." not in dom:
            continue
        candidates.append((iss, dom))
    if not candidates:
        return []
    candidates = candidates[:TOML_MAX_FETCHES_PER_CYCLE]

    out_rows = []
    for i, (iss, dom) in enumerate(candidates):
        if i > 0:
            time.sleep(TOML_FETCH_INTERVAL_SEC)
        status, text, err = _fetch_toml(dom, log)
        db.upsert_token_toml_fetch_log(iss, dom, status, err)
        if status != "ok" or not text:
            continue
        toml_url = f"https://{dom}/.well-known/xrp-ledger.toml"
        for e in _parse_toml_tokens(text, iss):
            out_rows.append({
                "currency_hex": e["currency_hex"],
                "issuer": iss,
                "currency_display": e["currency_display"],
                "category": e["category"],
                "source_url": toml_url,
            })
    log.info("toml pass: candidates=%d rows=%d", len(candidates), len(out_rows))
    return out_rows


def _fetch_flags(client, issuer):
    """Return dict of snapshot fields for one issuer, or None on error.

    signer_lists=True piggybacks multi-sig detection on the same
    round-trip — same call shape as token_data._capability_signals()
    was making live per page render."""
    try:
        resp = client.request(
            AccountInfo(account=issuer, ledger_index="validated", signer_lists=True)
        )
    except Exception as e:
        log.warning("account_info request failed for %s: %s", issuer, type(e).__name__)
        return None
    result = resp.result or {}
    if "error" in result:
        # account_not_found is legitimate — a token_volume row can outlive its
        # issuer if the account was deleted. Snapshot with zero-flags fetch_ok=True
        # so the page can render "issuer no longer exists" via existing checks
        # rather than a stale banner. Everything else is a fetch error.
        err = result.get("error")
        if err == "actNotFound":
            return {
                "flags": 0,
                "transfer_rate": None,
                "regular_key": None,
                "has_signer_list": False,
                "domain_hex": None,
                "ledger_index": int(result.get("ledger_current_index") or 0),
                "fetch_ok": True,
            }
        log.warning("account_info error for %s: %s", issuer, err)
        return None

    acct = result.get("account_data") or {}
    # signer_lists may live on account_data OR at the top level depending
    # on rippled version — token_data.py handles both shapes; we do too.
    signer_lists = acct.get("signer_lists") or result.get("signer_lists") or []
    try:
        transfer_rate = int(acct["TransferRate"]) if acct.get("TransferRate") else None
    except (TypeError, ValueError):
        transfer_rate = None
    try:
        ledger_index = int(result.get("ledger_index") or result.get("ledger_current_index") or 0)
    except (TypeError, ValueError):
        ledger_index = 0
    try:
        flags = int(acct.get("Flags") or 0)
    except (TypeError, ValueError):
        flags = 0
    return {
        "flags": flags,
        "transfer_rate": transfer_rate,
        "regular_key": acct.get("RegularKey"),
        "has_signer_list": bool(signer_lists),
        "domain_hex": acct.get("Domain"),
        "ledger_index": ledger_index,
        "fetch_ok": True,
    }


def run() -> tuple[bool, int, str]:
    issuers = db.read_token_volume_issuers()
    if not issuers:
        return False, 0, "no distinct issuers in token_volume (PG unavailable or empty table?)"

    client = get_client(WALKER_NAME)
    rows = []
    fetched_ok = 0
    max_ledger = 0
    for issuer in issuers:
        snap = _fetch_flags(client, issuer)
        if snap is None:
            rows.append({
                "issuer": issuer,
                "flags": 0,
                "transfer_rate": None,
                "regular_key": None,
                "has_signer_list": False,
                "domain_hex": None,
                "ledger_index": 0,
                "fetch_ok": False,
            })
            continue
        fetched_ok += 1
        max_ledger = max(max_ledger, snap["ledger_index"])
        rows.append({"issuer": issuer, **snap})

    if fetched_ok == 0:
        return False, 0, f"all {len(issuers)} issuers errored — LAN rippled unreachable?"

    ok = db.replace_token_issuer_flags_snapshot(rows)
    if not ok:
        return False, 0, "replace_token_issuer_flags_snapshot returned False"

    # Extension B (2026-09-06): toml pass — best-effort fetch of
    # xrp-ledger.toml for every issuer that has a valid Domain AND is due
    # per the 24h cache. Failures fail-open (do not fail the walker); a
    # dead toml source loses coverage for that issuer but doesn't lose
    # the AccountRoot flags that already wrote OK above.
    inferred_count = 0
    try:
        toml_rows = collect_toml_categories(
            [r for r in rows if r.get("fetch_ok")], log
        )
        if toml_rows:
            db.upsert_token_category_inferred(toml_rows)
            inferred_count = len(toml_rows)
    except Exception as e:
        log.warning("toml pass exception (ignored): %s: %s", type(e).__name__, e)

    msg = (
        f"wrote {len(rows)} rows ({fetched_ok} ok, "
        f"{len(rows) - fetched_ok} error), max_ledger={max_ledger}, "
        f"toml_inferred={inferred_count}"
    )
    return True, 0, msg


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    log.info("start")
    t0 = time.time()
    try:
        ok, n_findings, message = run()
    except Exception as e:
        log.exception("unhandled exception")
        db.write_walker_health_end(WALKER_NAME, ok=False,
                                   message=f"unhandled: {type(e).__name__}: {e}")
        return 1
    elapsed = time.time() - t0
    message = f"{message} · elapsed={elapsed:.1f}s"
    if ok:
        log.info("PASS: %s", message)
    else:
        log.error("FAIL: %s", message)
    db.write_walker_health_end(
        WALKER_NAME,
        ok=ok,
        message=message,
        findings_count=n_findings if ok else None,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
