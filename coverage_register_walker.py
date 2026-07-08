"""Phase 1b Coverage Register history writer.

Dedicated walker (own walker_health row, own launchd plist) that reads
the four sources — Phase 0 (ledger_definitions), Phase 1a (tx_type_seen +
ledger_entry_type_seen), coverage_labels, walker_scope_declarations, and
walker_health — computes the three-way diff, and appends to
coverage_register_history when state differs from the most recent row OR
>24h has elapsed (guarantee floor for the record in quiescent weeks).

Why separate from the on-read register: computed-on-read gives a live
view for /internal/coverage, but history rows would only appear when
someone loads the page — silent gaps, no artifact of the week nobody
visited. And piggybacking on ledger_definitions_walker would give that
walker two jobs its walker_health row conflates. Per the HIGH-RISK-writer
discipline: named owner, named cadence, watermark. Not an implication.

Cadence: 6h (matches ledger_definitions_walker so the "definitions_hash
still current" invariant holds at write time). If the Phase 0 walker
lags, this walker still records the state honestly against the last-
known-good definitions.
"""

from __future__ import annotations

import logging
import sys
import time

import db


WALKER_NAME = "coverage_register_walker"
WALKER_CADENCE_SECONDS = 21600  # 6h — mirrors ledger_definitions_walker
MIN_INTERVAL_SECONDS = 86400    # guarantee floor: append at least daily

logger = logging.getLogger(WALKER_NAME)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.write_walker_health_start(
        WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS
    )
    ok = False
    message = None
    try:
        state = db.read_coverage_register_state()
        if state is None:
            raise RuntimeError(
                "read_coverage_register_state returned None — Phase 0 "
                "singleton missing or PG unreachable"
            )

        # Loud-line the three-way diff shape at the top of every run so
        # the launchd log doubles as a state trace. Loud once per run,
        # not per row — the register itself is the row-level surface.
        defs = state["definitions"]
        undef_tx = state["seen_but_undefined_tx"]
        undef_entry = state["seen_but_undefined_entry"]
        unlabeled_tx = state["unlabeled_tx"]
        unlabeled_entry = state["unlabeled_entry"]
        stale_walkers = sorted(
            w for w, h in state["walker_health"].items() if h["is_stale"]
        )
        undeclared_walkers = sorted(
            w for w, h in state["walker_health"].items() if h["undeclared"]
        )

        # SCREAM state — a seen-but-undefined type is the loudest alarm
        # this system can render; log at ERROR so it surfaces past any
        # log-filter that drops INFO.
        if undef_tx or undef_entry:
            logger.error(
                "!!! SEEN-BUT-UNDEFINED persisted "
                "tx=%s entry=%s",
                undef_tx, undef_entry,
            )
        # UNDECLARED walkers — same escrow lesson, structural version.
        if undeclared_walkers:
            logger.error(
                "!!! UNDECLARED WALKERS (missing walker_scope_declarations "
                "rows): %s",
                undeclared_walkers,
            )

        if stale_walkers:
            logger.warning("stale walkers: %s", stale_walkers)
        if unlabeled_tx or unlabeled_entry:
            logger.info(
                "unlabeled (curation debt): tx=%s entry=%s",
                unlabeled_tx, unlabeled_entry,
            )

        result = db.append_coverage_register_history(
            state, min_interval_seconds=MIN_INTERVAL_SECONDS
        )

        ok = True
        message = (
            f"defs_hash={(defs.get('hash') or '')[:16]}.. "
            f"defined tx={defs['defined_tx_count']} "
            f"entry={defs['defined_entry_count']} "
            f"seen tx={len(state['seen_tx'])} "
            f"entry={len(state['seen_entry'])} "
            f"undefined tx={len(undef_tx)} entry={len(undef_entry)} "
            f"unlabeled tx={len(unlabeled_tx)} entry={len(unlabeled_entry)} "
            f"stale_walkers={len(stale_walkers)} "
            f"undeclared_walkers={len(undeclared_walkers)} "
            f"history_appended={result['appended']} "
            f"reason={result['reason']}"
        )
        logger.info(message)
    except Exception as exc:
        message = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        db.write_walker_health_end(WALKER_NAME, ok=ok, message=message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
