#!/usr/bin/env python3
"""Read-only (jj_ro) database path canary.

WHY THIS EXISTS (2026-10-30/31 incident)
----------------------------------------
The `jj_ro` read-only path broke with `SASL authentication failed` some time
after 06:10 ET on 2026-10-30 and had recovered by 06:06 ET on 2026-10-31.
Nobody could say when it broke or when it came back, because **nothing
observed it**: `scripts/jj_query.sh` writes no log, and no scheduled job
uses the read-only credential at all. The fault window could only be
narrowed to "somewhere in 14 hours" from chat history.

This canary closes that gap. It performs one `SELECT 1` as `jj_ro` and
records a timestamped ok/fail into `walker_health` under the walker name
`jj_ro_canary`, so the read-only path has the same observability every
other walker already has.

DESIGN CONSTRAINTS (owner, 2026-10-31)
--------------------------------------
* **No new schedule.** This rides an existing walker tick so it does not
  wake the database any more often than today. It is invoked from
  `mac_tunnel_health_canary` (cadence 300s), which already opens a
  connection on every tick.
* **Never log the credential.** The connection string is read into memory,
  used, and discarded. Only an *error class* is ever recorded — never the
  value, never the host, never the password.

WHY IT PARSES BY HAND INSTEAD OF USING A URI PARSER
---------------------------------------------------
This is the load-bearing part, and it is counter-intuitive.

`scripts/jj_query.sh` does NOT hand its connection string to libpq. It
splits it with shell parameter expansion: userinfo at the **first** `@`,
and the host truncated at the **first** `/`. A standard URI parser splits
userinfo at the **last** `@` and treats the first `/` after the authority
as the start of the path.

Those two algorithms disagree whenever the password contains a `/`. The
real read-only credential appears to contain exactly that. Consequence,
verified on 2026-10-31: `urllib.parse.urlparse` AND libpq's own
`conninfo_to_dict` both parse the host as a short non-resolving fragment
of the password, producing a bogus `gaierror` that looks exactly like a
real DNS outage. Two separate diagnostic rounds were lost to that false
signal before the cause was found.

So a canary built on a URI parser would report the read-only path as
BROKEN while the wrapper it is supposed to be monitoring works fine — a
false alarm, which is worse than no canary. `parse_ro_conninfo` therefore
replicates the wrapper's shell semantics exactly and connects with
explicit libpq keyword parameters rather than a URI string.

Fidelity note (reported, deliberately NOT fixed here): the wrapper always
forces port 5432 and discards any `:port` present in the connection
string. This canary replicates that, so it passes and fails for the same
reasons the wrapper does. If the wrapper is ever taught to honour an
explicit port, change both together.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Callable

WALKER_NAME = "jj_ro_canary"
CADENCE_SECONDS = 300
CONNECT_TIMEOUT = 15

# Default location of the read-only env file, same path the wrapper uses.
JJ_ENV_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "xrpldashboard", "jj_env"
)

# The variable the wrapper requires to be present after sourcing.
RO_VAR = "DATABASE_URL_RO"


def parse_ro_conninfo(raw: str) -> dict[str, Any]:
    """Split a read-only connection string the way ``jj_query.sh`` does.

    Replicates these shell expansions exactly::

        URI="${DATABASE_URL_RO#*://}"
        USERPASS="${URI%%@*}"      # before the FIRST @
        HOSTPATH="${URI#*@}"       # after  the FIRST @
        PGUSER="${USERPASS%%:*}"
        PGPASSWORD="${USERPASS#*:}"
        HOSTPORT="${HOSTPATH%%/*}" # before the FIRST /
        PGHOST="${HOSTPORT%%:*}"
        DB="${HOSTPATH#*/}"
        PGDATABASE="${DB%%[?]*}"

    Returns libpq keyword parameters. Raises ``ValueError`` when the string
    has no userinfo separator, since that is a malformed value rather than
    a connectivity problem and must not be reported as a DB outage.
    """
    if not raw or not raw.strip():
        raise ValueError("empty connection string")

    s = raw.strip()

    # Strip the scheme, if any. "#*://" is non-greedy in shell, so the
    # first "://" wins.
    idx = s.find("://")
    uri = s[idx + 3:] if idx != -1 else s

    if "@" not in uri:
        raise ValueError("no userinfo separator")

    # FIRST @ -- deliberately not the last, matching the wrapper.
    userpass, _, hostpath = uri.partition("@")

    user, sep, password = userpass.partition(":")
    if not sep:
        password = ""

    # FIRST / -- host portion ends there.
    hostport = hostpath.split("/", 1)[0]
    # Wrapper takes everything before the first ":" and forces 5432.
    host = hostport.split(":", 1)[0]

    dbname = ""
    if "/" in hostpath:
        dbname = hostpath.split("/", 1)[1]
        # Drop any query string.
        dbname = dbname.split("?", 1)[0]

    if not host:
        raise ValueError("no host in connection string")

    return {
        "host": host,
        "port": 5432,
        "user": user,
        "password": password,
        "dbname": dbname,
        "sslmode": "require",
        "connect_timeout": CONNECT_TIMEOUT,
    }


def classify_error(msg: str) -> str:
    """Map a connection/query error to a short, credential-free class.

    The returned string is safe to persist: it never contains the host,
    the user, the password, or any part of the connection string.
    """
    m = (msg or "").lower()
    table = [
        ("sasl", "sasl_auth_failure"),
        ("password authentication failed", "password_auth_failure"),
        ("channel binding", "channel_binding_failure"),
        ("no pg_hba", "pg_hba_rejection"),
        ("does not exist", "role_or_db_missing"),
        ("nodename nor servname", "dns_resolution_failure"),
        ("could not translate host", "dns_resolution_failure"),
        ("timeout expired", "connect_timeout"),
        ("timed out", "network_timeout"),
        ("connection refused", "connection_refused"),
        ("server closed the connection", "connection_closed"),
        ("certificate", "tls_certificate_error"),
        ("permission denied", "permission_denied"),
        ("too many connections", "connection_limit"),
    ]
    for needle, label in table:
        if needle in m:
            return label
    return "unclassified_error"


def read_ro_raw(
    environ: dict[str, str] | None = None,
    env_path: str = JJ_ENV_PATH,
    opener: Callable[[str], Any] | None = None,
) -> str | None:
    """Obtain the read-only connection string.

    Order: the process environment first, then the env file the wrapper
    sources. Returns ``None`` when neither supplies it -- that is a
    *skip*, not a failure, because a checkout without the read-only file
    (CI, a fresh clone, another host) must not report a false outage.

    The value is returned for immediate use and never logged.
    """
    env = os.environ if environ is None else environ
    val = env.get(RO_VAR)
    if val and val.strip():
        return val.strip()

    try:
        open_fn = opener or open
        with open_fn(env_path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                key, sep, raw = line.partition("=")
                if not sep or key.strip() != RO_VAR:
                    continue
                raw = raw.strip()
                if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                    raw = raw[1:-1]
                return raw.strip() or None
    except (OSError, UnicodeDecodeError):
        return None
    return None


def run_check(
    connect: Callable[..., Any],
    raw: str | None,
) -> tuple[bool | None, dict[str, Any]]:
    """Run ``SELECT 1`` over the read-only path.

    ``connect`` is injected so this is testable without a database.

    Returns ``(ok, detail)`` where ``ok`` is ``True`` (path healthy),
    ``False`` (path broken) or ``None`` (skipped -- nothing to test).
    ``detail`` is a credential-free dict safe to persist.
    """
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    if raw is None:
        return None, {
            "status": "skipped",
            "reason": "no_readonly_url_available",
            "checked_at_utc": now,
        }

    try:
        params = parse_ro_conninfo(raw)
    except ValueError as e:
        return False, {
            "status": "fail",
            "error_class": "malformed_connection_string",
            "detail": str(e),
            "checked_at_utc": now,
        }

    try:
        conn = connect(**params)
    except Exception as e:  # noqa: BLE001 -- class is reported, not raised
        return False, {
            "status": "fail",
            "error_class": classify_error(str(e)),
            "exception_type": type(e).__name__,
            "checked_at_utc": now,
        }

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
        if not row or row[0] != 1:
            return False, {
                "status": "fail",
                "error_class": "unexpected_select_result",
                "checked_at_utc": now,
            }
    except Exception as e:  # noqa: BLE001
        return False, {
            "status": "fail",
            "error_class": classify_error(str(e)),
            "exception_type": type(e).__name__,
            "checked_at_utc": now,
        }

    return True, {
        "status": "ok",
        "select_1": True,
        "checked_at_utc": now,
    }


def _default_connect(**params: Any) -> Any:  # pragma: no cover -- needs a DB
    import psycopg

    return psycopg.connect(**params)


def record(ok: bool | None, detail: dict[str, Any]) -> None:
    """Persist the result to ``walker_health`` under ``jj_ro_canary``.

    A *skip* is recorded as a success with ``status=skipped`` so the row's
    timestamp still advances -- it proves the canary ran, without
    inventing an outage. Never raises: a monitoring write must not break
    the walker hosting it.
    """
    try:
        import db

        if not db.pg_available():
            return
        message = json.dumps(detail, sort_keys=True)
        db.write_walker_health_start(WALKER_NAME, cadence_seconds=CADENCE_SECONDS)
        db.write_walker_health_end(
            WALKER_NAME,
            ok is not False,
            message=message,
            findings_count=0 if ok is not False else 1,
        )
    except Exception as e:  # noqa: BLE001
        print(
            f"[{WALKER_NAME}] walker_health write failed: {type(e).__name__}",
            flush=True,
        )


def run(connect: Callable[..., Any] | None = None) -> bool | None:
    """Entry point for a host walker tick. Returns ok/False/None."""
    raw = read_ro_raw()
    ok, detail = run_check(connect or _default_connect, raw)
    # Credential-free one-liner so the result is visible in the host
    # walker's stdout as well as in walker_health.
    print(
        f"[{WALKER_NAME}] {detail.get('status')} "
        f"{detail.get('error_class', '')}".rstrip(),
        flush=True,
    )
    record(ok, detail)
    return ok


if __name__ == "__main__":  # pragma: no cover
    result = run()
    raise SystemExit(0 if result is not False else 1)
