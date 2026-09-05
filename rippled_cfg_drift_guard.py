"""Guard walker: verify Lenovo's /etc/rippled/rippled.cfg port stanzas remain
sovereign-safe.

Watches these three stanzas' `ip` and `admin` lines:
- [port_rpc_admin_local]  → ip=127.0.0.1, admin=127.0.0.1
- [port_rpc_public_lan]   → ip=192.168.40.95, admin-key ABSENT
- [port_rpc_public]       → ip=127.0.0.1,     admin-key ABSENT
                              (stanza itself may be ABSENT until tunnel Step 1
                              ships — that's OK, only present-and-drifted is a
                              finding)

Any deviation → walker_health.findings_count > 0 → BetterStack page. Standard
guard posture — findings-based alerting, walker itself always exits ok=True as
long as it ran to completion.

Cadence: hourly.

Reads the live cfg via `ssh rippled-node cat /etc/rippled/rippled.cfg`. File is
world-readable (per Aug 2026 perms check), no sudo required.

**Hard prerequisite for tunnel Step 1** per
`triage/TUNNEL_DESIGN_PACK_2026-08-31.md`. Codified same day as Wave-0's
admin-port drift closure — same lie-shape, same alerting axis.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys

import db

WALKER_NAME = "rippled_cfg_drift_guard"
WALKER_CADENCE_SECONDS = 3600  # hourly

SSH_HOST = "rippled-node"
REMOTE_CFG_PATH = "/etc/rippled/rippled.cfg"
SSH_TIMEOUT_SECONDS = 8
SSH_COMMAND_TIMEOUT_SECONDS = 25

# stanza → {key: expected value | None (must be absent)}
# Sentinel: stanza-name in _OPTIONAL_STANZAS means "not a finding if entire
# stanza is missing". Empty since 2026-09-02: [port_rpc_public] shipped
# live in the tunnel restart, so its absence is now a real drift signal.
EXPECTED: dict[str, dict[str, str | None]] = {
    "port_rpc_admin_local": {
        "ip": "127.0.0.1",
        "admin": "127.0.0.1",
    },
    "port_rpc_public_lan": {
        "ip": "192.168.40.95",
        "admin": None,  # must be absent
    },
    "port_rpc_public": {
        "ip": "127.0.0.1",
        "admin": None,  # must be absent
    },
    # 2026-09-02: sovereign WS for the on-box xrpl_stream service (runs on the
    # Lenovo itself, systemd xrpld-xrpl-stream). Loopback-only, non-admin:
    # ip=127.0.0.1 so nothing off-box can reach it, admin key ABSENT so the
    # transactions-stream subscriber cannot invoke admin methods (e.g. `stop`).
    # Exact WS-side mirror of [port_rpc_public] on 5007. The earlier LAN
    # variant (192.168.40.95:6007) was scrapped once we confirmed the stream
    # is same-box, not Mac→Lenovo.
    "port_ws_public": {
        "ip": "127.0.0.1",
        "admin": None,  # must be absent
        # 2026-09-05: raised from rippled's default 100 after the on-box
        # xrpl_stream subscriber (transactions stream on this port) was
        # being kicked by rippled ~400/hr with "Policy error: client is
        # too slow" — same shape as the Mac-era `send_queue_limit = 500`
        # workaround from [port_ws_admin_local] that got dropped in the
        # Lenovo repoint. Guard this key so it never silently reverts.
        "send_queue_limit": "500",
    },
}
# _OPTIONAL_STANZAS handling: name a stanza here to make its absence a
# non-finding during pre-ship windows (stanza added to EXPECTED first,
# then cfg edit lands + rippled restart, then the entry is cleared).
# Empty since 2026-09-02: [port_ws_public_lan] shipped live and is now
# a required stanza; absence is a real drift signal.
_OPTIONAL_STANZAS: frozenset[str] = frozenset()


def _fetch_cfg() -> str | None:
    """Read the live cfg via SSH. World-readable per file perms; no sudo needed."""
    try:
        r = subprocess.run(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={SSH_TIMEOUT_SECONDS}",
                SSH_HOST,
                "cat", REMOTE_CFG_PATH,
            ],
            capture_output=True,
            text=True,
            timeout=SSH_COMMAND_TIMEOUT_SECONDS,
        )
        if r.returncode != 0:
            return None
        return r.stdout
    except (subprocess.TimeoutExpired, OSError):
        return None


def _stanza_body(cfg: str, stanza_name: str) -> str | None:
    """Return the body of [stanza_name] (up to but not including the next
    [header]). Returns None if the stanza doesn't appear in the cfg. If a
    stanza header appears multiple times (rippled tolerates duplicates), we
    take the FIRST occurrence — same behavior rippled's parser uses."""
    header_re = re.compile(rf"^\[{re.escape(stanza_name)}\][ \t]*$")
    lines = cfg.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if header_re.match(ln):
            start = i + 1
            break
    if start is None:
        return None
    body_lines: list[str] = []
    any_header_re = re.compile(r"^\[.+\][ \t]*$")
    for ln in lines[start:]:
        if any_header_re.match(ln):
            break
        body_lines.append(ln)
    return "\n".join(body_lines)


def _extract_key(body: str, key: str) -> str | None:
    """Return `key = VALUE` value from stanza body; None if absent. Handles
    inline comments (`key = value  # ...`), quoted values, and comments."""
    kv_re = re.compile(
        rf"^[ \t]*{re.escape(key)}[ \t]*=[ \t]*(?P<val>[^#\n]*?)[ \t]*(?:#.*)?$"
    )
    for ln in body.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        m = kv_re.match(ln)
        if m:
            v = m.group("val").strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            return v
    return None


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)

    cfg = _fetch_cfg()
    if cfg is None:
        db.write_walker_health_end(
            WALKER_NAME,
            ok=False,
            message=f"SSH fetch of {SSH_HOST}:{REMOTE_CFG_PATH} failed",
        )
        sys.exit(1)

    drift_msgs: list[str] = []
    for stanza, expected in EXPECTED.items():
        body = _stanza_body(cfg, stanza)
        if body is None:
            if stanza in _OPTIONAL_STANZAS:
                # Absent stanza is OK for optional-until-shipped items.
                continue
            drift_msgs.append(f"[{stanza}]=<MISSING STANZA>")
            continue
        for key, expected_val in expected.items():
            actual = _extract_key(body, key)
            if expected_val is None:
                # Key must be ABSENT.
                if actual is not None:
                    drift_msgs.append(
                        f"[{stanza}].{key}={actual!r} (expected ABSENT)"
                    )
            else:
                if actual is None:
                    drift_msgs.append(
                        f"[{stanza}].{key}=<MISSING> (expected {expected_val!r})"
                    )
                elif actual != expected_val:
                    drift_msgs.append(
                        f"[{stanza}].{key}={actual!r} (expected {expected_val!r})"
                    )

    findings = len(drift_msgs)
    if findings == 0:
        message = (
            f"clean: {len(EXPECTED)} rippled.cfg port stanzas verified"
        )
    else:
        message = f"DRIFT[{findings}]: {'; '.join(drift_msgs)}"
    logging.info(message)
    db.write_walker_health_end(
        WALKER_NAME,
        ok=True,
        message=message,
        findings_count=findings,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
