#!/usr/bin/env python3
"""Amendment roll-call recorder — Lenovo-side long-running unit.

Subscribes to the `validations` + `ledger` streams on our OWN rippled's
loopback WebSocket (no new LAN port — Charlie decision 1, 2026-09-25),
collects the amendment yes-votes that UNL validators attach to their
validations of every VOTING ledger (index % 256 == 255), and when the
following flag ledger has closed writes one append-only round to
Postgres:

    amendment_roll_call_rounds   1 row  (denominators + threshold)
    amendment_roll_call_votes    1 row per UNL validator heard
    amendment_roll_call_tallies  1 row per amendment with ≥1 yes vote
                                 (this round or carried)

All arithmetic lives in roll_call.py (pure, tested); this file is the
plumbing: websocket, buffering, UNL refresh, DB writes, walker_health.

Sovereignty: reads ONLY the own node. There is no public-node fallback
here on purpose — a roll-call of validations is a thing our node hears
first-hand; if the node is down the recorder simply records nothing and
walker_health goes stale (the pager's job). No silent s1 substitute.

Runtime env (sourced by the systemd unit from ~/.config/xrpldashboard/env):
    XRPL_LOCAL_WS_NODE   ws://127.0.0.1:6007   (public loopback port; admin 6006 also fine)
    ROLL_CALL_WS         optional override of the above
    ROLL_CALL_UNL_URL    default https://vl.ripple.com/  (list-size denominator source)
    ROLL_CALL_UNL_TTL    default 600 s
    DATABASE_URL         Neon (owner env; the Lenovo already carries it for xrpl_stream)

Usage:
    python3 scripts/roll_call_recorder.py                 # run forever
    python3 scripts/roll_call_recorder.py --dry-run       # print rounds, write nothing
    python3 scripts/roll_call_recorder.py --rounds 1      # exit after N finalized rounds
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import roll_call  # noqa: E402
import db  # noqa: E402
import network_state  # noqa: E402

WALKER_NAME = "roll_call_recorder"
CADENCE_SECONDS = 1200          # one voting ledger every ~15 min (256 ledgers × ~3.6 s)
FINALIZE_GRACE_LEDGERS = 2      # finalize voting ledger V once ledger V+2 has closed
FINALIZE_MAX_WAIT = 45.0        # …or 45 s after the first vote seen, whichever first
UNL_FALLBACK_TTL = 600

log = logging.getLogger("roll_call_recorder")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# UNL refresh
# --------------------------------------------------------------------------

class UnlCache:
    def __init__(self, url: str, ttl: int) -> None:
        self.url = url
        self.ttl = ttl
        self.maps: roll_call.UnlKeyMaps | None = None
        self.fetched_at = 0.0

    def source_label(self) -> str:
        return self.url.replace("https://", "").replace("http://", "").strip("/")

    def refresh_if_stale(self) -> roll_call.UnlKeyMaps | None:
        if self.maps is not None and (time.monotonic() - self.fetched_at) < self.ttl:
            return self.maps
        blob, err = network_state._fetch_unl(self.url)
        if blob is None:
            log.warning("UNL fetch failed (%s); keeping previous list (seq=%s)",
                        err, self.maps.sequence if self.maps else None)
            return self.maps
        maps = roll_call.unl_key_maps(blob, source=self.source_label())
        if self.maps is None or maps.sequence != self.maps.sequence:
            log.info("UNL %s seq=%s size=%d signing-map=%d skipped=%d",
                     maps.source, maps.sequence, maps.size, len(maps.signing_to_master), maps.skipped)
        self.maps = maps
        self.fetched_at = time.monotonic()
        return maps


# --------------------------------------------------------------------------
# DB writes
# --------------------------------------------------------------------------

def write_round(result: roll_call.RoundResult) -> bool:
    """Append one finalized round. ON CONFLICT DO NOTHING everywhere —
    a re-run of the same voting ledger is a no-op, never a mutation."""
    if not db.pg_available():
        log.error("DATABASE_URL not configured — refusing to run without a write path")
        return False
    r = result.round_row
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO amendment_roll_call_rounds (voting_ledger_index, flag_ledger_index, "
                " voting_ledger_hash, signing_time_max, observed_iso, unl_source, unl_sequence, "
                " unl_size, validations_seen, trusted_available, threshold_rippled, votes_needed, "
                " recorder_version) "
                "VALUES (%(voting_ledger_index)s, %(flag_ledger_index)s, %(voting_ledger_hash)s, "
                " %(signing_time_max)s, %(observed_iso)s, %(unl_source)s, %(unl_sequence)s, "
                " %(unl_size)s, %(validations_seen)s, %(trusted_available)s, %(threshold_rippled)s, "
                " %(votes_needed)s, %(recorder_version)s) "
                "ON CONFLICT (voting_ledger_index) DO NOTHING", r)
            for v in result.vote_rows:
                cur.execute(
                    "INSERT INTO amendment_roll_call_votes (voting_ledger_index, master_key, "
                    " signing_key, signing_time, server_version, amendments) "
                    "VALUES (%(voting_ledger_index)s, %(master_key)s, %(signing_key)s, "
                    " %(signing_time)s, %(server_version)s, %(amendments)s) "
                    "ON CONFLICT (voting_ledger_index, master_key) DO NOTHING", v)
            for t in result.tally_rows:
                cur.execute(
                    "INSERT INTO amendment_roll_call_tallies (voting_ledger_index, amendment_hash, "
                    " yes_votes_round, yes_votes_carried, passes_rippled) "
                    "VALUES (%(voting_ledger_index)s, %(amendment_hash)s, %(yes_votes_round)s, "
                    " %(yes_votes_carried)s, %(passes_rippled)s) "
                    "ON CONFLICT (voting_ledger_index, amendment_hash) DO NOTHING", t)
        conn.commit()
    return True


def seed_trusted_votes(trusted: roll_call.TrustedVotes, maps: roll_call.UnlKeyMaps) -> int:
    """On (re)start, rebuild the 24h carry-forward state from the latest
    persisted vote per UNL validator so a restart doesn't zero every
    carried tally. Returns number of validators seeded."""
    if not db.pg_available():
        return 0
    trusted.trust_changed(maps.master_keys)
    cutoff = int(time.time()) - roll_call.RIPPLE_EPOCH_OFFSET - roll_call.VOTE_EXPIRY_SECONDS
    n = 0
    with db.pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (master_key) master_key, amendments, signing_time "
            "FROM amendment_roll_call_votes WHERE signing_time >= %s "
            "ORDER BY master_key, voting_ledger_index DESC", (cutoff,))
        for master_key, amendments, signing_time in cur.fetchall():
            if master_key in maps.master_keys:
                trusted.seed(master_key, amendments or [], int(signing_time))
                n += 1
    return n


# --------------------------------------------------------------------------
# Stream loop
# --------------------------------------------------------------------------

class Recorder:
    def __init__(self, ws_url: str, unl: UnlCache, dry_run: bool, max_rounds: int | None) -> None:
        self.ws_url = ws_url
        self.unl = unl
        self.dry_run = dry_run
        self.max_rounds = max_rounds
        self.trusted = roll_call.TrustedVotes()
        self.pending: dict[int, dict[str, roll_call.Vote]] = {}   # voting idx → master → Vote
        self.first_seen: dict[int, float] = {}
        self.ledger_hashes: dict[int, str] = {}
        self.rounds_done = 0
        self.stop = asyncio.Event()

    # -- message handling ---------------------------------------------------
    def on_message(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "ledgerClosed":
            try:
                idx = int(msg.get("ledger_index"))
            except (TypeError, ValueError):
                return
            if msg.get("ledger_hash"):
                self.ledger_hashes[idx] = msg["ledger_hash"]
                # keep the hash map small
                for k in [k for k in self.ledger_hashes if k < idx - 600]:
                    self.ledger_hashes.pop(k, None)
            return
        if t != "validationReceived":
            return
        maps = self.unl.maps
        if maps is None:
            return
        try:
            idx = int(msg.get("ledger_index"))
        except (TypeError, ValueError):
            return
        if not roll_call.is_voting_ledger(idx):
            return
        vote = roll_call.vote_from_message(msg, maps)
        if vote is None:
            return
        bucket = self.pending.setdefault(idx, {})
        bucket.setdefault(vote.master_key, vote)      # first full validation per validator wins
        self.first_seen.setdefault(idx, time.monotonic())

    def due_rounds(self, latest_closed: int | None) -> list[int]:
        now = time.monotonic()
        due = []
        for idx, t0 in self.first_seen.items():
            if (latest_closed is not None and latest_closed >= idx + FINALIZE_GRACE_LEDGERS) \
                    or (now - t0) >= FINALIZE_MAX_WAIT:
                due.append(idx)
        return sorted(due)

    def finalize(self, idx: int) -> None:
        votes = list(self.pending.pop(idx, {}).values())
        self.first_seen.pop(idx, None)
        maps = self.unl.maps
        if maps is None:
            log.warning("round %d dropped: no UNL loaded", idx)
            return
        result = roll_call.tally_round(idx, votes, maps, self.trusted, _now_iso(),
                                       voting_ledger_hash=self.ledger_hashes.get(idx))
        r = result.round_row
        passing = sum(1 for t in result.tally_rows if t["passes_rippled"])
        summary = (f"voting_ledger={idx} flag={r['flag_ledger_index']} seen={r['validations_seen']}/"
                   f"{r['unl_size']} trusted_available={r['trusted_available']} "
                   f"threshold={r['threshold_rippled']} amendments={len(result.tally_rows)} passing={passing}")
        if self.dry_run:
            print(json.dumps({"round": r, "tallies": result.tally_rows,
                              "votes": len(result.vote_rows)}, indent=1))
            log.info("DRY-RUN %s", summary)
        else:
            db.write_walker_health_start(WALKER_NAME, cadence_seconds=CADENCE_SECONDS)
            try:
                write_round(result)
            except Exception as exc:  # loud, then keep streaming
                log.exception("round %d write failed", idx)
                db.write_walker_health_end(WALKER_NAME, ok=False, message=f"write failed: {exc}"[:400])
            else:
                # findings_count is the L1 pager's "this walker found a
                # problem" signal (check_walker_findings pages on >0). A
                # clean round has NO findings — `passing` is a tally, not
                # an anomaly, and it stays in the message. (2026-09-26:
                # passing=3 had been paging as 3 findings every round.)
                db.write_walker_health_end(WALKER_NAME, ok=True, message=summary[:400],
                                           findings_count=0)
                log.info("WROTE %s", summary)
        self.rounds_done += 1
        if self.max_rounds is not None and self.rounds_done >= self.max_rounds:
            self.stop.set()

    # -- main loop ------------------------------------------------------------
    async def run(self) -> None:
        import websockets  # local import so the pure tests never need it
        ssl_ctx = None
        if self.ws_url.startswith("wss://"):
            # Production is plain ws:// on the Lenovo loopback; wss:// is only
            # for Mac-side dry-runs against a public node. python.org macOS
            # builds ship without a CA bundle wired into ssl, so use certifi's.
            import ssl
            try:
                import certifi
                ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ssl_ctx = ssl.create_default_context()
        backoff = 5
        while not self.stop.is_set():
            latest_closed: int | None = None
            try:
                async with websockets.connect(self.ws_url, max_size=2 ** 22, ping_interval=20,
                                              ssl=ssl_ctx) as ws:
                    await ws.send(json.dumps({"id": 1, "command": "subscribe",
                                              "streams": ["validations", "ledger"]}))
                    log.info("subscribed validations+ledger on %s", self.ws_url)
                    backoff = 5
                    while not self.stop.is_set():
                        self.unl.refresh_if_stale()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        except asyncio.TimeoutError:
                            raw = None
                        if raw is not None:
                            try:
                                msg = json.loads(raw)
                            except ValueError:
                                continue
                            if msg.get("type") == "ledgerClosed":
                                try:
                                    latest_closed = int(msg.get("ledger_index"))
                                except (TypeError, ValueError):
                                    pass
                            self.on_message(msg)
                        for idx in self.due_rounds(latest_closed):
                            self.finalize(idx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("stream error (%s); reconnect in %ds", str(exc)[:160], backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true", help="print rounds, write nothing")
    p.add_argument("--rounds", type=int, default=None, help="exit after N finalized rounds")
    p.add_argument("--ws", default=None, help="override websocket URL")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ws_url = args.ws or os.environ.get("ROLL_CALL_WS") or os.environ.get("XRPL_LOCAL_WS_NODE") \
        or "ws://127.0.0.1:6007"
    unl = UnlCache(os.environ.get("ROLL_CALL_UNL_URL", "https://vl.ripple.com/"),
                   int(os.environ.get("ROLL_CALL_UNL_TTL", str(UNL_FALLBACK_TTL))))
    maps = unl.refresh_if_stale()
    if maps is None:
        log.error("no UNL available at start; exiting non-zero so systemd restarts us")
        return 2

    rec = Recorder(ws_url, unl, dry_run=args.dry_run, max_rounds=args.rounds)
    if not args.dry_run:
        seeded = seed_trusted_votes(rec.trusted, maps)
        log.info("seeded carry-forward state for %d validators from persisted votes", seeded)
        db.write_walker_health_start(WALKER_NAME, cadence_seconds=CADENCE_SECONDS)
        db.write_walker_health_end(WALKER_NAME, ok=True,
                                   message=f"recorder started ws={ws_url} unl={maps.source} seq={maps.sequence}")
    else:
        rec.trusted.trust_changed(maps.master_keys)

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, rec.stop.set)
    try:
        loop.run_until_complete(rec.run())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
