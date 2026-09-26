"""new_accounts_walker — forward-only ingest of XRPL account creations.

Design: docs/NEW_ACCOUNTS_BLOCK_DESIGN_2026-09-25.md (APPROVED with edits;
build gate = Option B first milestone, cleared 2026-09-26). v1 = COUNT
only; funder kept in the row for v2 concentration, not surfaced.

Detection primitive: a validated transaction whose metadata carries a
`CreatedNode` with `LedgerEntryType: AccountRoot` created that account.
Almost always a `Payment` funding a never-before-seen address; the walker
records ANY tx type that creates an AccountRoot so the count is the
ledger's truth, not a tx-type guess.

Provenance boundary (Charlie ruling): FORWARD-ONLY from the walker's
tracking-start. The PG `events` table written by xrpl_stream is continuous
BY DATE since 2026-08-31 but its capture SCOPE narrowed on 2026-09-09
(~100k → ~4k rows/day, watchlist + large transfers), so it is not a
complete record of network-wide account creation and is NOT backfilled
from. Both public surfaces state "tracking since <TRACKING_SINCE>".

Mirrors nft_activity_walker --mode activity: cursor in nft_walker_state
(generic walker-keyed table), 200-ledger batches toward HEAD-3, own node
first via xrpl_client.get_client (sourcing + one walker_node_fallback row
per run on cascade), walker_health 'new_accounts' at 300 s cadence. A
one-ledger fetch failure stops the batch; the cursor only advances past
ledgers that were fully read, so the record has no silent holes.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db  # noqa: E402
from xrpl_client import get_client, RunFallbackSink  # noqa: E402
from xrpl.models.requests import Ledger  # noqa: E402

WALKER_NAME = "new_accounts"
WALKER_CADENCE_SECONDS = 300
BATCH_LEDGERS = int(os.environ.get("NEW_ACCOUNTS_BATCH_LEDGERS", "200"))
HEAD_SAFETY_LEDGERS = 3
# Public-surface fact ("tracking since"): the day the walker first seeded
# its cursor on the own node. Never backdated.
TRACKING_SINCE = "2026-09-26"
SOURCE = "own_node_stream"
RIPPLE_EPOCH_OFFSET = 946684800

logger = logging.getLogger("new_accounts_walker")


def _ripple_to_iso(rt) -> str | None:
    if rt is None:
        return None
    return dt.datetime.fromtimestamp(int(rt) + RIPPLE_EPOCH_OFFSET, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _drops(v) -> int | None:
    """XRP amounts are drops strings; issued-currency objects → None."""
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return None


def rows_from_ledger(ledger_seq: int, close_time_ripple, txs) -> list[dict]:
    """Pure: one row per AccountRoot CreatedNode in a validated ledger.
    Handles the API-v2 envelope ({hash, meta, tx_json, …}) and the legacy
    v1 shape (fields at top level with metaData)."""
    rows = []
    iso = _ripple_to_iso(close_time_ripple)
    for tx in txs or []:
        body = tx.get("tx_json") or tx
        meta = tx.get("meta") or tx.get("metaData") or {}
        if not isinstance(meta, dict) or meta.get("TransactionResult") != "tesSUCCESS":
            continue
        nodes = meta.get("AffectedNodes")
        if not isinstance(nodes, list):
            continue
        tx_hash = tx.get("hash") or body.get("hash")
        funder = body.get("Account")
        amount = _drops(meta.get("delivered_amount"))
        if amount is None:
            amount = _drops(body.get("Amount"))
        for n in nodes:
            if not isinstance(n, dict):
                continue
            created = n.get("CreatedNode")
            if not isinstance(created, dict) or created.get("LedgerEntryType") != "AccountRoot":
                continue
            addr = (created.get("NewFields") or {}).get("Account")
            if not addr or not tx_hash:
                continue
            rows.append({
                "address": addr,
                "funding_tx": tx_hash,
                "funder": funder,
                "amount_drops": amount,
                "ledger_index": int(ledger_seq),
                "close_time": int(close_time_ripple) if close_time_ripple is not None else 0,
                "first_seen_iso": iso or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": SOURCE,
            })
    return rows


def _fetch_validated_ledger_index(client) -> int:
    resp = client.request(Ledger(ledger_index="validated"))
    result = resp.result or {}
    if result.get("error"):
        raise RuntimeError(f"ledger validated rpc-error: {result.get('error')}")
    idx = result.get("ledger_index") or (result.get("ledger") or {}).get("ledger_index")
    if idx is None:
        raise RuntimeError("ledger validated: no ledger_index in response")
    return int(idx)


def _fetch_ledger_txs(client, ledger_seq: int):
    """(close_time_ripple, txs) or (None, None) on any failure."""
    try:
        resp = client.request(Ledger(ledger_index=ledger_seq, transactions=True, expand=True))
    except Exception as e:  # noqa: BLE001
        logger.warning("ledger fetch failed seq=%s err=%s", ledger_seq, e)
        return None, None
    result = resp.result or {}
    if result.get("error"):
        logger.warning("ledger rpc-error seq=%s err=%s", ledger_seq, result.get("error"))
        return None, None
    ledger = result.get("ledger") or {}
    close_time = ledger.get("close_time")
    if close_time is None:
        logger.warning("ledger missing close_time seq=%s", ledger_seq)
        return None, None
    return close_time, ledger.get("transactions") or []


def run() -> tuple[bool, str]:
    sink = RunFallbackSink()
    client = get_client(WALKER_NAME, fallback_sink=sink)
    validated = _fetch_validated_ledger_index(client)
    target_tip = validated - HEAD_SAFETY_LEDGERS

    state = db.read_nft_walker_state(WALKER_NAME)
    if state is None:
        db.seed_nft_walker_state(walker_name=WALKER_NAME, cursor_ledger=target_tip, backfill_target=None)
        return True, (f"seeded cursor_ledger={target_tip} (validated={validated}) "
                      f"tracking_since={TRACKING_SINCE} sourcing={sink.sourcing}")

    cursor = int(state["cursor_ledger"])
    if cursor > target_tip:
        return True, f"caught_up cursor={cursor} target_tip={target_tip} validated={validated} sourcing={sink.sourcing}"

    batch_end = min(cursor + BATCH_LEDGERS - 1, target_tip)
    all_rows: list[dict] = []
    ledgers_ok = 0
    ledgers_err = 0
    for seq in range(cursor, batch_end + 1):
        close_time, txs = _fetch_ledger_txs(client, seq)
        if txs is None:
            ledgers_err += 1
            break  # never advance past a ledger we could not read
        ledgers_ok += 1
        all_rows.extend(rows_from_ledger(seq, close_time, txs))

    inserted = db.insert_new_accounts_batch(all_rows) if all_rows else 0
    if ledgers_ok == 0:
        return False, f"no_ledgers_fetched cursor={cursor} validated={validated} sourcing={sink.sourcing}"
    new_cursor = cursor + ledgers_ok
    db.write_nft_walker_cursor(WALKER_NAME, new_cursor, success=True)
    return True, (
        f"ledgers_ok={ledgers_ok} ledgers_err={ledgers_err} created_seen={len(all_rows)} "
        f"inserted={inserted} cursor={cursor}->{new_cursor} tip={target_tip} "
        f"sourcing={sink.sourcing}" + (f" fallback_reason={sink.reason}" if sink.reason else "")
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    db.ensure_new_accounts_table()
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok, message = False, "not_started"
    try:
        ok, message = run()
        logger.info("ok=%s %s", ok, message)
    except Exception as e:  # noqa: BLE001
        message = f"exception: {type(e).__name__}: {str(e)[:200]}"
        logger.exception("run failed")
    finally:
        db.write_walker_health_end(WALKER_NAME, ok=ok, message=message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
