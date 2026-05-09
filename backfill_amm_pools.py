"""One-shot: load amm_ranked.json + amm_index.json + amm_rank_state.json
into Postgres so prod (Render) sees real /pools data immediately, without
waiting for the next scheduled rank_amms.py run.

After this, future runs of rank_amms.py keep Postgres fresh on every
SAVE_EVERY checkpoint via db.replace_amm_ranked_pools.

Usage:
    DATABASE_URL=postgres://... python backfill_amm_pools.py
"""

import json
import os
import sys

import db


HERE = os.path.dirname(os.path.abspath(__file__))
RANKED_PATH = os.path.join(HERE, "amm_ranked.json")
INDEX_PATH = os.path.join(HERE, "amm_index.json")
STATE_PATH = os.path.join(HERE, "amm_rank_state.json")


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def main():
    if not db.pg_available():
        print("DATABASE_URL not set or psycopg missing — nothing to do.")
        return 1

    ranked = _load_json(RANKED_PATH, [])
    index = _load_json(INDEX_PATH, [])
    state = _load_json(STATE_PATH, {})

    if not isinstance(ranked, list) or not ranked:
        print(f"FATAL: {RANKED_PATH} is empty or invalid — refusing to wipe table.")
        return 2

    print(f"loaded ranked={len(ranked)} indexed={len(index)} from disk")
    print("ensuring schema...")
    db.init_schema()

    print(f"writing {len(ranked)} pools to amm_ranked_pools...")
    db.replace_amm_ranked_pools(ranked)

    db.write_heartbeat(
        "amm_ranker",
        txns_seen=len(ranked),
        extra={
            "indexed_count": len(index),
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "cursor": state.get("cursor"),
            "errors": state.get("errors"),
            "skipped": state.get("skipped"),
            "source": "backfill",
        },
    )

    written = db.read_amm_ranked_pools()
    snap_ts = db.read_amm_snapshot_ts()
    hb = db.read_heartbeat("amm_ranker")
    print(f"verify: rows in PG={len(written)} snapshot_ts={snap_ts}")
    print(f"verify: heartbeat={hb}")
    if len(written) != len(ranked):
        print(f"WARN: row count mismatch ({len(written)} vs {len(ranked)})")
        return 3
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
