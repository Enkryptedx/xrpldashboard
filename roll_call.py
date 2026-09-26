"""Amendment roll-call — pure logic (no network, no DB).

Mirrors rippled's amendment voting arithmetic so the recorder
(scripts/roll_call_recorder.py) and the tests share one implementation.
Every rule below is lifted from rippled source, read 2026-09-25:

* src/xrpld/app/consensus/RCLConsensus.cpp — validators attach
  `sfAmendments` to a validation only when `ledger.isVotingLedger()`,
  i.e. the ledger BEFORE a flag ledger (index % 256 == 255). The
  EnableAmendment pseudo-transactions are then built into the ledger
  after the flag ledger, from the trusted validations of that voting
  ledger (`getTrustedForLedger(prevLedger.parentHash, prevLedger.seq()-1)`).
* src/xrpld/app/misc/detail/AmendmentTable.cpp — `TrustedVotes` keeps
  the LAST vote seen from every trusted validator and expires it 24h
  after that validation's close time; the denominator (`available`) is
  the number of trusted validators with a live record. `AmendmentSet`:
  `threshold = max(1, trusted * num / den)` (integer division) and
  `passes = votes > threshold`, except `votes >= threshold` when exactly
  one trusted validation is available.
* include/xrpl/protocol/SystemParameters.h —
  `kAmendmentMajorityCalcThreshold = std::ratio<80, 100>`.

Two numbers are deliberately kept side by side (Charlie decision 2,
2026-09-25): `yes_votes_round` counts only validators heard THIS round
(what a human sees on the wire), `yes_votes_carried` applies rippled's
24h carry-forward (what decides the on-ledger RESET). `unl_size` is the
published list size for the display "of 35"; `trusted_available` is
rippled's denominator.
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
from typing import Iterable

from xrpl.core import addresscodec
from xrpl.core.binarycodec import decode as _binary_decode

RECORDER_VERSION = "1.0.0"

MAJORITY_NUM = 80
MAJORITY_DEN = 100
VOTE_EXPIRY_SECONDS = 24 * 3600
VOTING_LEDGER_MOD = 255            # index % 256 == 255 → validators carry votes
RIPPLE_EPOCH_OFFSET = 946684800


# --------------------------------------------------------------------------
# Ledger arithmetic
# --------------------------------------------------------------------------

def is_voting_ledger(ledger_index: int) -> bool:
    return int(ledger_index) % 256 == VOTING_LEDGER_MOD


def flag_ledger_for(voting_ledger_index: int) -> int:
    return int(voting_ledger_index) + 1


def rippled_threshold(trusted_available: int) -> int:
    """AmendmentSet ctor: max(1, trusted * 80 / 100) with C++ integer division."""
    return max(1, (int(trusted_available) * MAJORITY_NUM) // MAJORITY_DEN)


def rippled_passes(yes_votes: int, trusted_available: int) -> bool:
    """AmendmentSet::passes — strict '>' unless exactly one trusted validation."""
    thr = rippled_threshold(trusted_available)
    if int(trusted_available) == 1:
        return int(yes_votes) >= thr
    return int(yes_votes) > thr


# --------------------------------------------------------------------------
# UNL → key maps
# --------------------------------------------------------------------------

def node_key_b58(hex_key: str) -> str:
    return addresscodec.encode_node_public_key(bytes.fromhex(hex_key))


def decode_manifest(manifest_b64: str) -> dict:
    """Decode a validator manifest (base64 STObject) → field dict.
    Fields of interest: PublicKey (master, hex), SigningPubKey (hex),
    Sequence, Domain (hex). Raises on malformed input."""
    raw = base64.b64decode(manifest_b64)
    return _binary_decode(raw.hex())


@dataclasses.dataclass
class UnlKeyMaps:
    source: str
    sequence: int | None
    master_keys: set[str]                 # base58 nH…
    signing_to_master: dict[str, str]     # base58 n9… → base58 nH…
    skipped: int = 0                      # validators whose manifest failed to decode

    @property
    def size(self) -> int:
        return len(self.master_keys)


def unl_key_maps(blob: dict, source: str) -> UnlKeyMaps:
    """Build master/signing key maps from a decoded UNL blob
    ({"sequence":…, "validators":[{"validation_public_key": hex,
    "manifest": b64}, …]}). A validator whose manifest fails to decode
    still counts as a UNL member by master key (it just can't be matched
    by its wire signing key) and is tallied in `skipped`."""
    masters: set[str] = set()
    s2m: dict[str, str] = {}
    skipped = 0
    for v in blob.get("validators") or []:
        mk_hex = (v.get("validation_public_key") or "").strip()
        if not mk_hex:
            continue
        try:
            master_b58 = node_key_b58(mk_hex)
        except (ValueError, binascii.Error):
            skipped += 1
            continue
        masters.add(master_b58)
        try:
            man = decode_manifest(v.get("manifest") or "")
            sk_hex = man.get("SigningPubKey") or ""
            if sk_hex:
                s2m[node_key_b58(sk_hex)] = master_b58
        except Exception:  # malformed manifest — member stays, mapping skipped
            skipped += 1
    return UnlKeyMaps(source=source, sequence=blob.get("sequence"),
                      master_keys=masters, signing_to_master=s2m, skipped=skipped)


# --------------------------------------------------------------------------
# Wire → vote
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Vote:
    master_key: str
    signing_key: str | None
    ledger_index: int
    signing_time: int | None
    server_version: str | None
    amendments: tuple[str, ...]          # yes-votes; () = voting yes on nothing


def resolve_master(msg: dict, maps: UnlKeyMaps) -> str | None:
    """Identify the UNL validator behind a validation message. Prefer the
    wire `master_key` (present when our node knows the manifest); fall
    back to mapping `validation_public_key` through UNL manifests. None →
    not a UNL validator (or unknown key)."""
    mk = msg.get("master_key")
    if mk and mk in maps.master_keys:
        return mk
    sk = msg.get("validation_public_key")
    if sk:
        return maps.signing_to_master.get(sk)
    return None


def vote_from_message(msg: dict, maps: UnlKeyMaps) -> Vote | None:
    """Turn one `validationReceived` message into a Vote, or None when it
    is not a FULL validation from a UNL validator (rippled only counts
    trusted full validations — partial ones never reach doVoting)."""
    if msg.get("type") != "validationReceived":
        return None
    if not msg.get("full", False):
        return None
    try:
        idx = int(msg.get("ledger_index"))
    except (TypeError, ValueError):
        return None
    master = resolve_master(msg, maps)
    if master is None:
        return None
    ams = msg.get("amendments") or []
    st = msg.get("signing_time")
    sv = msg.get("server_version")
    return Vote(master_key=master,
                signing_key=msg.get("validation_public_key"),
                ledger_index=idx,
                signing_time=int(st) if st is not None else None,
                server_version=str(sv) if sv is not None else None,
                amendments=tuple(sorted(str(a).upper() for a in ams)))


# --------------------------------------------------------------------------
# rippled TrustedVotes emulation
# --------------------------------------------------------------------------

class TrustedVotes:
    """Port of AmendmentTable.cpp::TrustedVotes. One record per trusted
    validator: (upvotes, timeout). `record_votes` replaces a validator's
    upvotes with the newest validation's and sets timeout = close_time +
    24h; validators not heard from keep their last vote until the timeout
    passes, then drop to "no on everything" with no timeout (not counted
    in `available`)."""

    def __init__(self) -> None:
        self._rec: dict[str, tuple[set[str], int | None]] = {}

    def trust_changed(self, trusted: Iterable[str]) -> None:
        trusted = set(trusted)
        new: dict[str, tuple[set[str], int | None]] = {}
        for mk in trusted:
            new[mk] = self._rec.get(mk, (set(), None))
        self._rec = new

    def record_votes(self, votes: Iterable[Vote], close_time: int) -> None:
        new_timeout = int(close_time) + VOTE_EXPIRY_SECONDS
        for v in votes:
            if v.master_key not in self._rec:
                continue  # untrusted → ignored, exactly as rippled does
            self._rec[v.master_key] = (set(v.amendments), new_timeout)
        for mk, (ups, timeout) in list(self._rec.items()):
            if timeout is not None and int(close_time) > timeout:
                self._rec[mk] = (set(), None)

    def seed(self, master_key: str, amendments: Iterable[str], close_time: int) -> None:
        """Restart re-seed from persisted votes (only for trusted keys)."""
        if master_key in self._rec:
            self._rec[master_key] = (set(amendments), int(close_time) + VOTE_EXPIRY_SECONDS)

    def get_votes(self) -> tuple[int, dict[str, int]]:
        available = 0
        counts: dict[str, int] = {}
        for ups, timeout in self._rec.values():
            if timeout is not None:
                available += 1
            for a in ups:
                counts[a] = counts.get(a, 0) + 1
        return available, counts

    def snapshot(self) -> dict[str, tuple[set[str], int | None]]:
        return {k: (set(v[0]), v[1]) for k, v in self._rec.items()}


# --------------------------------------------------------------------------
# Round tally
# --------------------------------------------------------------------------

@dataclasses.dataclass
class RoundResult:
    round_row: dict
    vote_rows: list[dict]
    tally_rows: list[dict]


def tally_round(voting_ledger_index: int, votes: Iterable[Vote], maps: UnlKeyMaps,
                trusted: TrustedVotes, observed_iso: str,
                voting_ledger_hash: str | None = None) -> RoundResult:
    """Finalize one voting ledger. Applies the round's votes to the
    TrustedVotes state (carry-forward), then emits the three row sets.
    `votes` must all be for `voting_ledger_index`; one per master key
    (the caller de-duplicates — rippled keeps one validation per
    validator per ledger)."""
    votes = [v for v in votes if v.ledger_index == int(voting_ledger_index)]
    by_master: dict[str, Vote] = {}
    for v in votes:
        by_master[v.master_key] = v  # last one wins; caller normally sends one
    round_votes = list(by_master.values())

    signing_times = [v.signing_time for v in round_votes if v.signing_time is not None]
    close_time = max(signing_times) if signing_times else 0

    trusted.trust_changed(maps.master_keys)
    trusted.record_votes(round_votes, close_time)
    available, carried = trusted.get_votes()

    round_counts: dict[str, int] = {}
    for v in round_votes:
        for a in v.amendments:
            round_counts[a] = round_counts.get(a, 0) + 1

    thr = rippled_threshold(available)
    round_row = {
        "voting_ledger_index": int(voting_ledger_index),
        "flag_ledger_index": flag_ledger_for(voting_ledger_index),
        "voting_ledger_hash": voting_ledger_hash,
        "signing_time_max": close_time or None,
        "observed_iso": observed_iso,
        "unl_source": maps.source,
        "unl_sequence": maps.sequence,
        "unl_size": maps.size,
        "validations_seen": len(round_votes),
        "trusted_available": available,
        "threshold_rippled": thr,
        "recorder_version": RECORDER_VERSION,
    }
    vote_rows = [{
        "voting_ledger_index": int(voting_ledger_index),
        "master_key": v.master_key,
        "signing_key": v.signing_key,
        "signing_time": v.signing_time,
        "server_version": v.server_version,
        "amendments": list(v.amendments),
    } for v in sorted(round_votes, key=lambda x: x.master_key)]
    hashes = sorted(set(round_counts) | set(carried))
    tally_rows = [{
        "voting_ledger_index": int(voting_ledger_index),
        "amendment_hash": h,
        "yes_votes_round": round_counts.get(h, 0),
        "yes_votes_carried": carried.get(h, 0),
        "passes_rippled": rippled_passes(carried.get(h, 0), available),
    } for h in hashes]
    return RoundResult(round_row=round_row, vote_rows=vote_rows, tally_rows=tally_rows)
