"""Weekly watch: has our own node started serving `mpt_holders` yet?

We read MPT holder counts from PUBLIC Clio endpoints. That is a disclosed
third-party dependency and the only reason it exists is a capability gap:
`mpt_holders` is a Clio method, and rippled 3.4.1 answers
{"error": "unknownCmd", "error_code": 32}. Measured 2026-11-27 against our
own node AND xrplcluster.com, both rippled 3.4.1, while Clio 2.8.0 answers
it. Two independent servers of the same build rejecting it is what made it
a per-BUILD finding rather than a host problem.

The risk this watch addresses is NOT that Clio breaks. It is that the gap
closes and nobody notices. rippled gains methods between releases; the day
it implements `mpt_holders`, our Clio dependency becomes unnecessary and
every holder figure could be sovereign again. Without a watch, the note in
tools/anchor_nodes.py saying "re-check after a rippled upgrade so the note
cannot harden into a permanent refusal" is a comment nobody re-reads, and
we would keep sending someone else's servers 533 requests a cycle for data
our own node could answer.

So the ALERT HERE IS GOOD NEWS. findings_count > 0 means "capability
gained, go delete the dependency", not "something is broken". That
inversion is deliberate and is why the message says so explicitly -- an
operator seeing this fire should not go hunting for a fault.

WHAT COUNTS AS THE CAPABILITY. Any response that is not an
unknown-method error proves the method is implemented, including
`invalidParams` or an error about the issuance id: to produce those, the
server had to recognise `mpt_holders`, accept it as a command and get as
far as reading its parameters. We therefore probe with a REAL issuance id
but treat parameter-level errors as a positive signal too, so the watch
cannot be defeated by the probe id going stale.

Cadence weekly (StartInterval 604800). A capability gain is not urgent and
cannot be acted on quickly -- it means scheduling a code change. Checking
hourly would spend 8,760 requests a year to learn something that changes at
most a few times a decade. Charlie ruling 2026-11-27: no hourly capability
probe, the holders job already fails loudly if the endpoint list is wrong.

Three outcomes, exhaustive:
  0 findings  our own node still answers unknownCmd. The Clio dependency
              remains justified. This is the expected weekly result.
  1 finding   our own node answered. Revisit the dependency.
  1 finding   our own node could not be reached at all after retries.
              Deliberately NOT silence: a watch that cannot see is not a
              watch that is happy. Other canaries will fire first on a
              genuinely down node, so this corroborates rather than
              duplicates.
"""

from __future__ import annotations

import logging
import os
import sys
import time

import httpx

import db

WALKER_NAME = "mpt_holders_capability_watch"
WALKER_CADENCE_SECONDS = 604800  # weekly; keep equal to the plist StartInterval

# Our OWN node. Deliberately not MPT_HOLDERS_NODES -- that is the Clio list,
# and asking Clio whether Clio implements a Clio method answers nothing.
OWN_NODE = os.environ.get(
    "XRPL_NODE", os.environ.get("XRPL_LOCAL_NODE", "http://192.168.40.95:5006")
)

# Errors that mean "this build does not implement the method".
UNSUPPORTED_ERRORS = {"unknownCmd", "unknownCommand", "notImpl"}

# A real mainnet MPT issuance id. Only needs to be well-FORMED: a parameter
# error still proves the method exists, which is the whole point.
PROBE_ISSUANCE_ID = os.environ.get(
    "MPT_CAPABILITY_PROBE_ID",
    "0649B40ACA10011D695BFE3939CB59EDEB4E1EE73FC9F0B3",
)

# The node is on the LAN. Three tries with a short gap distinguishes a blip
# from a week-long outage without turning a transient into a finding.
ATTEMPTS = int(os.environ.get("MPT_CAPABILITY_ATTEMPTS", "3"))
ATTEMPT_PAUSE_SECS = float(os.environ.get("MPT_CAPABILITY_PAUSE", "2"))
TIMEOUT_SECS = float(os.environ.get("MPT_CAPABILITY_TIMEOUT", "20"))


def probe(node_url: str, issuance_id: str, attempts: int = ATTEMPTS,
          pause: float = ATTEMPT_PAUSE_SECS, timeout: float = TIMEOUT_SECS,
          sleep=time.sleep) -> dict:
    """Ask one node for `mpt_holders` once (with retries on transport only).

    Returns a verdict dict:
      supported     True / False / None (None = could not determine)
      error         the XRPL error code string, if any
      detail        short human string for the walker_health message

    Transport failures retry; an ANSWER of any kind is final, because a
    server that replied has already told us whether it knows the method.
    """
    last_exc = None
    for i in range(max(1, attempts)):
        try:
            resp = httpx.post(
                node_url,
                json={"method": "mpt_holders",
                      "params": [{"mpt_issuance_id": issuance_id, "limit": 1}]},
                timeout=timeout,
            )
            body = resp.json() or {}
            result = body.get("result")
            if not isinstance(result, dict):
                # A reply we cannot parse is still a reply, but it tells us
                # nothing about the method. Treat as indeterminate.
                return {"supported": None, "error": None,
                        "detail": "unparseable result body"}
            err = result.get("error")
            if err in UNSUPPORTED_ERRORS:
                return {"supported": False, "error": err,
                        "detail": f"{err} (method not implemented)"}
            if err:
                # Recognised the command, then objected to the parameters.
                # That is still proof of implementation.
                return {"supported": True, "error": err,
                        "detail": f"answered with {err!r} -- command "
                                  f"recognised, so the method exists"}
            return {"supported": True, "error": None,
                    "detail": "answered successfully"}
        except Exception as e:  # transport only
            last_exc = e
            if i < attempts - 1:
                sleep(pause)
    return {"supported": None, "error": None,
            "detail": f"unreachable after {attempts} attempts: "
                      f"{type(last_exc).__name__}"}


def verdict_to_findings(v: dict, node_url: str) -> tuple[int, str]:
    """Map a probe verdict to (findings_count, message).

    findings_count is the alert axis, as everywhere else. Note the
    inversion: a finding here is GOOD NEWS, and the message says so, so
    nobody reads it as a fault to chase.
    """
    if v["supported"] is False:
        return 0, (
            f"clean: {node_url} still answers mpt_holders with "
            f"{v['error']} -- the public-Clio dependency for MPT holder "
            f"counts remains justified"
        )
    if v["supported"] is True:
        return 1, (
            f"CAPABILITY GAINED (this is good news, not a fault): "
            f"{node_url} now serves mpt_holders -- {v['detail']}. "
            f"Revisit the Clio dependency: mpt_snapshot.py and "
            f"lending_snapshot.py can likely read holders from our own node "
            f"and report sovereign=True. See tools/anchor_nodes.py "
            f"DEFAULT_MPT_HOLDERS_NODES and MPT_HOLDERS_NODES."
        )
    return 1, (
        f"INDETERMINATE: could not establish whether {node_url} serves "
        f"mpt_holders -- {v['detail']}. Reported rather than passed over "
        f"silently; a watch that cannot see is not a watch that is happy."
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start(WALKER_NAME,
                                 cadence_seconds=WALKER_CADENCE_SECONDS)
    v = probe(OWN_NODE, PROBE_ISSUANCE_ID)
    findings, message = verdict_to_findings(v, OWN_NODE)

    logging.info(message)
    print(f"[{WALKER_NAME}] {message}", flush=True)

    # ok=True whenever the watch RAN to completion -- findings_count is the
    # alert axis, same discipline as xrpl_env_drift_guard. The run completing
    # and the thing it watches being noteworthy are different questions.
    db.write_walker_health_end(
        WALKER_NAME, ok=True, message=message, findings_count=findings,
    )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
