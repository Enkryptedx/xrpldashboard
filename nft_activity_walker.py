"""Standalone walker for the /nfts funnel + active/quiet + churn badge.

Four modes, dispatched via --mode:

  --mode activity          Forward-only ingest from cursor_ledger up to
                           validated_ledger - 3 (safety margin), in batches
                           of BATCH_LEDGERS. Idempotent (unique tx_hash).
                           Local rippled primary via xrpl_client.get_client;
                           public s1/s2 fallback.
  --mode backfill          Walks backward from backfill_ledger toward
                           backfill_target (2026-04-01 cutoff). Uses public
                           Clio (s2-clio) because local node ledger_history
                           is only ~10k. Populated in D2.
  --mode rollup            Recompute nft_collection_stats from nft_activity
                           (per issuer/taxon). Populated in D3.
  --mode existing-snapshot One-time full-state count via Clio
                           ledger_data(NFTokenPage). HARD-FAILS on Clio
                           unreachable (walker_health.last_error) — no
                           silent fallback to a node that can't answer.
                           Cadence stays MANUAL until change-rate observed.

The /nfts route reads through nft_snapshot.py (planned) — same PG-first
pattern as escrow/oracle/hero. Walker never serves the browser directly.

STANDING RULE: churn stays intra-collection. compute_churn_metrics() reads
ONLY nft_activity rows for one (issuer, taxon). Never JOIN across
collections. Never look up wallets outside this collection's own sale
records. If a future change tempts a cross-collection wallet map to
"improve" the badge — STOP and flag Charlie. This is the exact place
NFTs could drift into the strangers-graph product Charlie killed.
See: memory/feedback_nft_churn_intra_collection_only.md
"""

import argparse
import datetime
import logging
import sys
import time

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import Ledger
from xrpl.utils import parse_nftoken_id

import db
from xrpl_client import get_client

# Mirrors launchd/com.charliebruce.xrpldashboard.nft_activity_walker.plist
# StartInterval (activity mode). /walker_health uses it for per-row
# staleness thresholds. Backfill mode runs on a separate plist at 900s.
WALKER_CADENCE_SECONDS = 300

# Ledgers processed per invocation. Ledger interval ~4s → 200 ledgers ≈ 13
# minutes of activity per activity-mode run. At the current market rate
# (~540 mints/day, ~1080 sales/day) that's ~5 mints + ~10 sales + offers.
BATCH_LEDGERS = 200

# Safety margin from validated tip — avoid racing consensus.
HEAD_SAFETY_LEDGERS = 3

# Backfill mode targets a public Clio node with full history. Local
# rippled's ledger_history is only ~10k so it cannot answer for the
# 2026-04-01 window we need. Kept as a module constant so an operator
# can override via env if s2-clio needs to be swapped.
import os as _os  # noqa: E402
BACKFILL_CLIO_URL = _os.environ.get(
    "XRPL_BACKFILL_CLIO", "https://s2-clio.ripple.com:51234/"
)

# Backfill runs on a 900s (15 min) launchd cadence. Each invocation
# processes ledgers in BATCH_LEDGERS-sized state-write chunks and exits
# when the runtime budget is exhausted, so a killed process at most
# loses <BATCH_LEDGERS of unwritten progress. Budget is deliberately
# shorter than the cadence so the walker never overlaps itself.
BACKFILL_MAX_RUNTIME_SECONDS = 600

# 2026-04-01 00:00:00 UTC cutoff ledger, verified 2026-07-04 via
# s2-clio.ripple.com binary search (close_time exactly 2026-04-01T00:00:00Z;
# ledger 103252852 closes at 2026-03-31T23:59:51Z). Used only as a fallback
# when backfill_target is not yet persisted in nft_walker_state; the DB
# value is authoritative once written.
BACKFILL_CUTOFF_LEDGER_2026_04_01 = 103252853

# XRPL close_time is seconds since ripple epoch (2000-01-01).
RIPPLE_EPOCH_UNIX = 946684800

NFT_TX_TYPES = frozenset({
    "NFTokenMint",
    "NFTokenBurn",
    "NFTokenCreateOffer",
    "NFTokenAcceptOffer",
    "NFTokenCancelOffer",
})

# Short label surfaced in nft_activity.tx_type — strips the NFToken prefix
# so downstream queries stay readable.
TX_TYPE_LABEL = {
    "NFTokenMint":        "Mint",
    "NFTokenBurn":        "Burn",
    "NFTokenCreateOffer": "CreateOffer",
    "NFTokenAcceptOffer": "AcceptOffer",
    "NFTokenCancelOffer": "CancelOffer",
}

WALKER_NAME = "nft_activity"

logger = logging.getLogger("nft_activity_walker")


def _ripple_time_to_datetime(rt):
    """Convert XRPL close_time (ripple-epoch seconds) to a tz-aware UTC
    datetime. Returns None if input is missing."""
    if rt is None:
        return None
    return datetime.datetime.fromtimestamp(
        int(rt) + RIPPLE_EPOCH_UNIX,
        tz=datetime.timezone.utc,
    )


def _decode_nftoken_id(nftoken_id):
    """Return (issuer_r_address, taxon_int) or (None, None) if input is
    absent or malformed. Uses xrpl-py's XLS-20-aware parser, which handles
    the taxon-XOR unscramble step correctly."""
    if not nftoken_id:
        return None, None
    try:
        parsed = parse_nftoken_id(nftoken_id)
    except Exception:
        return None, None
    return parsed.get("issuer"), parsed.get("taxon")


def _extract_nftoken_id(tx, meta):
    """Best-effort NFTokenID extraction from a transaction.

    Burn / CreateOffer / AcceptOffer often carry NFTokenID on the tx.
    Mint carries it in meta.AffectedNodes (the new NFTokenPage entry) —
    that's a full node walk we defer to D3. When we can't extract now,
    we still ingest the row (raw JSON preserved for later re-derivation).
    """
    nid = tx.get("NFTokenID")
    if nid:
        return nid
    # AcceptOffer: meta.nftoken_id (Clio) is the reliable path when
    # available; fall back to None.
    if meta and isinstance(meta, dict):
        m_nid = meta.get("nftoken_id")
        if m_nid:
            return m_nid
    return None


def _extract_sale_details(tx, meta):
    """AcceptOffer only — pull (buyer, seller, price_drops, currency,
    currency_issuer, is_broker). For D1 this is best-effort; D3 rollup
    hardens the derivations. Non-AcceptOffer returns all-None so the
    columns are uniformly null-safe."""
    if tx.get("TransactionType") != "NFTokenAcceptOffer":
        return None, None, None, None, None, None
    # A brokered accept sets both NFTokenSellOffer and NFTokenBuyOffer;
    # otherwise exactly one is present.
    is_broker = bool(tx.get("NFTokenSellOffer") and tx.get("NFTokenBuyOffer"))
    # buyer / seller / price live in the referenced offers, not the accept
    # tx itself. Full derivation requires walking meta.AffectedNodes for
    # the deleted NFTokenOffer nodes — deferred to D3. D1 leaves nulls.
    return None, None, None, None, None, is_broker


def _fetch_validated_ledger_index(client):
    """Request ledger(index='validated'), return the ledger_index. Fails
    hard on RPC error — the walker cannot proceed without a valid tip."""
    resp = client.request(Ledger(ledger_index="validated"))
    result = resp.result or {}
    if result.get("error"):
        raise RuntimeError(f"ledger validated rpc-error: {result.get('error')}")
    idx = result.get("ledger_index") or (result.get("ledger") or {}).get("ledger_index")
    if idx is None:
        raise RuntimeError("ledger validated: no ledger_index in response")
    return int(idx)


def _fetch_ledger_txs(client, ledger_seq):
    """Fetch one closed ledger with expanded transactions + metadata.
    Returns (close_time_ripple, list_of_tx_dicts). Each tx dict carries
    'metaData' when the node returns metadata inline (rippled convention).
    Returns (None, None) on RPC error so the caller can decide fail-vs-skip."""
    req = Ledger(
        ledger_index=ledger_seq,
        transactions=True,
        expand=True,
    )
    try:
        resp = client.request(req)
    except Exception as e:
        logger.warning("ledger fetch failed seq=%s err=%s", ledger_seq, e)
        return None, None
    result = resp.result or {}
    if result.get("error"):
        logger.warning("ledger rpc-error seq=%s err=%s", ledger_seq, result.get("error"))
        return None, None
    ledger = result.get("ledger") or {}
    close_time = ledger.get("close_time")
    txs = ledger.get("transactions") or []
    return close_time, txs


def _nft_rows_from_ledger(ledger_seq, close_time_ripple, txs):
    """Filter a ledger's transactions to NFT tx types and shape them for
    insert_nft_activity_batch. Non-NFT transactions are skipped. Rows
    with no NFTokenID still ingest — raw JSON preserved for later
    re-derivation (Mint especially).

    Handles the API-v2 tx envelope: each transaction is
      {hash, ledger_hash, ledger_index, meta, tx_json, close_time_iso}
    where the actual tx fields live under tx_json. Legacy v1 shape (fields
    at top level with metaData) is still handled as a fallback so a mixed
    fleet doesn't silently drop rows."""
    close_dt = _ripple_time_to_datetime(close_time_ripple)
    rows = []
    for tx in txs:
        tx_body = tx.get("tx_json") or tx
        tx_type_raw = tx_body.get("TransactionType")
        if tx_type_raw not in NFT_TX_TYPES:
            continue
        meta = tx.get("meta") or tx.get("metaData") or {}
        # Only ingest successful txs. TransactionResult 'tesSUCCESS' means
        # the tx applied to the ledger. tec-* results also apply but with
        # a caveat — for NFT actions they still consume the fee but don't
        # execute the intent, so we skip them for funnel-truth.
        tx_result = None
        if isinstance(meta, dict):
            tx_result = meta.get("TransactionResult")
        if tx_result and tx_result != "tesSUCCESS":
            continue
        tx_hash = tx.get("hash") or tx_body.get("hash") or tx_body.get("Hash")
        if not tx_hash:
            logger.warning("nft tx missing hash seq=%s type=%s",
                           ledger_seq, tx_type_raw)
            continue
        nftoken_id = _extract_nftoken_id(tx_body, meta)
        issuer, taxon = _decode_nftoken_id(nftoken_id)
        buyer, seller, price_drops, currency, currency_issuer, is_broker = \
            _extract_sale_details(tx_body, meta)
        rows.append({
            "ledger_index":     ledger_seq,
            "close_time":       close_dt,
            "tx_hash":          tx_hash,
            "tx_type":          TX_TYPE_LABEL[tx_type_raw],
            "nftoken_id":       nftoken_id,
            "issuer":           issuer,
            "taxon":            taxon,
            "buyer":            buyer,
            "seller":           seller,
            "price_drops":      price_drops,
            "currency":         currency,
            "currency_issuer":  currency_issuer,
            "is_broker":        is_broker,
            "raw":              tx,
        })
    return rows


# ─────────────────────────────────────────────────────────────────────
# Mode: activity — forward-only ingest from cursor toward HEAD-3.
# ─────────────────────────────────────────────────────────────────────

def run_activity():
    """Forward ingest one batch. Seeds walker state on first run.
    Returns (ok: bool, message: str)."""
    client = get_client(WALKER_NAME)
    validated = _fetch_validated_ledger_index(client)
    target_tip = validated - HEAD_SAFETY_LEDGERS

    state = db.read_nft_walker_state(WALKER_NAME)
    if state is None:
        # First-ever run: seed cursor at target_tip and exit cleanly.
        # backfill_target stays NULL for D1; D2 fills it in with the
        # 2026-04-01 cutoff ledger and starts the backfill mode.
        db.seed_nft_walker_state(
            walker_name=WALKER_NAME,
            cursor_ledger=target_tip,
            backfill_target=None,
        )
        return True, f"seeded cursor_ledger={target_tip} (validated={validated})"

    cursor = int(state["cursor_ledger"])
    if cursor > target_tip:
        return True, (
            f"caught_up cursor={cursor} target_tip={target_tip} "
            f"validated={validated}"
        )

    batch_end = min(cursor + BATCH_LEDGERS - 1, target_tip)
    all_rows = []
    ledgers_ok = 0
    ledgers_err = 0
    for ledger_seq in range(cursor, batch_end + 1):
        close_time, txs = _fetch_ledger_txs(client, ledger_seq)
        if txs is None:
            ledgers_err += 1
            # One-ledger failure interrupts the batch — advancing past a
            # missing ledger would leave a permanent gap in nft_activity.
            # We advance cursor only up to the last successfully fetched
            # ledger; next run retries the failed one.
            break
        ledgers_ok += 1
        rows = _nft_rows_from_ledger(ledger_seq, close_time, txs)
        all_rows.extend(rows)

    inserted = db.insert_nft_activity_batch(all_rows) if all_rows else 0

    if ledgers_ok == 0:
        return False, (
            f"no_ledgers_fetched cursor={cursor} first_fail={cursor} "
            f"validated={validated}"
        )

    new_cursor = cursor + ledgers_ok
    db.write_nft_walker_cursor(WALKER_NAME, new_cursor, success=True)

    return True, (
        f"ledgers_ok={ledgers_ok} ledgers_err={ledgers_err} "
        f"nft_rows_seen={len(all_rows)} inserted={inserted} "
        f"cursor={cursor}->{new_cursor} tip={target_tip}"
    )


# ─────────────────────────────────────────────────────────────────────
# Mode: backfill — walks backward toward the 2026-04-01 cutoff.
# Populated in D2. Skeleton here so walker_health picks it up if
# invoked prematurely, rather than crashing silently.
# ─────────────────────────────────────────────────────────────────────

def _fetch_ledger_txs_from(client, ledger_seq):
    """Same shape as _fetch_ledger_txs but takes a pre-built client so
    backfill mode can bypass the local-first cascade. Local rippled would
    return lgrNotFound for every backfill request (~10k history vs ~2M
    ledgers back), and while the xrpl_client cascade handles that
    correctly, forcing local + cascade for every request wastes RTT and
    spams walker_node_fallback rows."""
    req = Ledger(
        ledger_index=ledger_seq,
        transactions=True,
        expand=True,
    )
    try:
        resp = client.request(req)
    except Exception as e:
        logger.warning("backfill ledger fetch failed seq=%s err=%s",
                       ledger_seq, e)
        return None, None
    result = resp.result or {}
    if result.get("error"):
        logger.warning("backfill ledger rpc-error seq=%s err=%s",
                       ledger_seq, result.get("error"))
        return None, None
    ledger = result.get("ledger") or {}
    return ledger.get("close_time"), ledger.get("transactions") or []


def run_backfill():
    """Walk backfill_ledger DOWN toward backfill_target using public
    Clio. Same row shape and ON CONFLICT (tx_hash) dedup as activity
    mode, so overlap with the forward ingest is safe.

    Returns (ok: bool, message: str). One invocation processes as many
    BATCH_LEDGERS-sized chunks as fit within BACKFILL_MAX_RUNTIME_SECONDS,
    persisting backfill_ledger at every chunk boundary — a killed
    process loses at most one chunk of progress."""
    state = db.read_nft_walker_state(WALKER_NAME)
    if state is None:
        return False, "walker not seeded yet — activity mode must run first"

    target = state.get("backfill_target")
    if target is None:
        # First-time backfill: persist the cutoff so /nfts callouts have
        # an immutable label. Retry on next run if the write races.
        target = BACKFILL_CUTOFF_LEDGER_2026_04_01
        wrote = db.set_nft_walker_backfill_target(WALKER_NAME, target)
        if not wrote:
            # Either the write raced with another invocation, or PG is
            # unavailable. Re-read to see which.
            state = db.read_nft_walker_state(WALKER_NAME)
            target = state.get("backfill_target") if state else None
            if target is None:
                return False, "could not persist backfill_target"

    backfill_ledger = state.get("backfill_ledger")
    if backfill_ledger is None:
        return False, "backfill_ledger is NULL (walker seed missing)"

    backfill_ledger = int(backfill_ledger)
    target = int(target)

    if backfill_ledger <= target:
        return True, (
            f"caught_up backfill_ledger={backfill_ledger} "
            f"target={target}"
        )

    client = JsonRpcClient(BACKFILL_CLIO_URL)

    started = time.monotonic()
    total_ledgers_ok = 0
    total_ledgers_err = 0
    total_rows_inserted = 0
    chunk_count = 0
    reason = "runtime_budget"

    while backfill_ledger > target:
        elapsed = time.monotonic() - started
        if elapsed >= BACKFILL_MAX_RUNTIME_SECONDS:
            reason = "runtime_budget"
            break

        # Walk downward: this chunk covers [chunk_lo .. chunk_hi]
        # inclusive. Stop at target on the boundary chunk.
        chunk_hi = backfill_ledger
        chunk_lo = max(chunk_hi - BATCH_LEDGERS + 1, target)

        chunk_rows = []
        chunk_ok = 0
        chunk_err = 0
        # Iterate high-to-low so a mid-chunk failure still leaves the
        # already-fetched high end intact when we truncate backfill_ledger.
        # Truncation on partial failure: we advance only past ledgers we
        # actually fetched, which for a top-down walk means the LOWEST
        # ledger we hit successfully. Tracked in lowest_ok.
        lowest_ok = None
        for seq in range(chunk_hi, chunk_lo - 1, -1):
            close_time, txs = _fetch_ledger_txs_from(client, seq)
            if txs is None:
                chunk_err += 1
                # Failure in the middle of a chunk: bail out, persist
                # progress only for the successfully-fetched suffix.
                break
            chunk_ok += 1
            lowest_ok = seq
            chunk_rows.extend(_nft_rows_from_ledger(seq, close_time, txs))

        inserted = db.insert_nft_activity_batch(chunk_rows) if chunk_rows else 0
        total_rows_inserted += inserted
        total_ledgers_ok += chunk_ok
        total_ledgers_err += chunk_err
        chunk_count += 1

        if lowest_ok is None:
            # Zero ledgers fetched this chunk — public Clio is unhappy;
            # bail out entirely so we don't spin.
            reason = "clio_failed"
            break

        # Advance backfill_ledger past the lowest successfully-fetched
        # ledger. Next chunk will start at (lowest_ok - 1).
        backfill_ledger = lowest_ok - 1
        db.write_nft_walker_backfill_cursor(
            WALKER_NAME, backfill_ledger, success=True,
        )

        if backfill_ledger < target:
            reason = "reached_target"
            break

    remaining = max(0, backfill_ledger - target)
    return (total_ledgers_ok > 0 or backfill_ledger <= target), (
        f"reason={reason} chunks={chunk_count} "
        f"ledgers_ok={total_ledgers_ok} ledgers_err={total_ledgers_err} "
        f"inserted={total_rows_inserted} "
        f"backfill_ledger={backfill_ledger} target={target} "
        f"remaining={remaining}"
    )


# ─────────────────────────────────────────────────────────────────────
# Mode: rollup — recompute nft_collection_stats from nft_activity.
# Populated in D3. compute_churn_metrics stub lives here for the
# STANDING RULE comment (guardrail placed at the temptation site).
# ─────────────────────────────────────────────────────────────────────

def compute_churn_metrics(conn, issuer, taxon):
    # ─────────────────────────────────────────────────────────────────
    # HARD LINE — CHURN STAYS INTRA-COLLECTION.
    # This function reads ONLY nft_activity rows for one (issuer, taxon).
    # Never JOIN across collections. Never look up wallets outside this
    # collection's own sale records. If a future change tempts a cross-
    # collection wallet map to "improve" the badge — STOP and flag
    # Charlie. This is the exact place NFTs could drift into the
    # strangers-graph product he killed.
    # See: memory/feedback_nft_churn_intra_collection_only.md
    # ─────────────────────────────────────────────────────────────────
    raise NotImplementedError("rollup mode arrives in D3")


def run_rollup():
    """Not implemented until D3. Recomputes nft_collection_stats
    (mints_total, burns_total, net_minted_since_cutoff, sales_30d,
    distinct_buyers_30d, is_active, churn_metrics_json) per (issuer,
    taxon) from nft_activity."""
    raise NotImplementedError("rollup mode arrives in D3")


# ─────────────────────────────────────────────────────────────────────
# Mode: existing-snapshot — one-time full-state count via Clio.
# Populated in D5-ish (post-page-shell) so we can surface the true
# STOCK number alongside the funnel FLOW. Cadence stays MANUAL until
# change-rate observed for ~2 weeks (verify-before-automating).
# ─────────────────────────────────────────────────────────────────────

def run_existing_snapshot():
    """Not implemented until later. Walks Clio ledger_data(type=NFTokenPage)
    to count all existing NFT ledger entries. HARD-FAILS on Clio unreachable
    — no silent fallback to a node that can't answer this."""
    raise NotImplementedError(
        "existing-snapshot mode arrives after the funnel ships"
    )


# ─────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────

MODE_DISPATCH = {
    "activity":          run_activity,
    "backfill":          run_backfill,
    "rollup":            run_rollup,
    "existing-snapshot": run_existing_snapshot,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=sorted(MODE_DISPATCH.keys()),
        default="activity",
        help="which walker mode to run (default: activity)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Walker health from the first line — same discipline as escrow_walker.
    # Uncaught crash before the end-of-run write still shows as failure
    # because start-of-run defaults last_run_ok=False.
    walker_health_name = f"{WALKER_NAME}_{args.mode}"
    db.write_walker_health_start(
        walker_health_name,
        cadence_seconds=WALKER_CADENCE_SECONDS if args.mode == "activity" else None,
    )

    ok = False
    message = None
    try:
        ok, message = MODE_DISPATCH[args.mode]()
        logger.info("mode=%s ok=%s %s", args.mode, ok, message)
    except NotImplementedError as e:
        message = f"not_implemented: {e}"
        logger.warning("mode=%s %s", args.mode, message)
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        logger.exception("mode=%s failed", args.mode)
    finally:
        db.write_walker_health_end(
            walker_health_name,
            ok=ok,
            message=message,
        )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
