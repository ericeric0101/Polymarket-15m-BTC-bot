#!/usr/bin/env python3
"""Report execution-penalty components captured in ENTRY_EDGE_OBSERVATION."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/trade_journal.db")
    parser.add_argument("--hours", type=float, default=168.0)
    args = parser.parse_args()
    path = Path(args.db)
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*),
              AVG(CAST(json_extract(payload_json, '$.execution_penalty_components.slippage_usdc') AS REAL)),
              AVG(CAST(json_extract(payload_json, '$.execution_penalty_components.vwap_usdc') AS REAL)),
              AVG(CAST(json_extract(payload_json, '$.execution_penalty_components.non_atomic_usdc') AS REAL)),
              AVG(CAST(json_extract(payload_json, '$.execution_penalty_components.floor_usdc') AS REAL)),
              AVG(CAST(json_extract(payload_json, '$.execution_penalty_components.total_usdc') AS REAL)),
              AVG(CAST(json_extract(payload_json, '$.execution_penalty_components.recent_vol') AS REAL))
            FROM order_events
            WHERE event_type='ENTRY_EDGE_OBSERVATION'
              AND julianday(ts) >= julianday('now', ?)
            """,
            (f"-{args.hours} hours",),
        ).fetchone()
    labels = ("slippage", "vwap", "non_atomic", "floor", "total", "recent_vol")
    print(f"execution penalty report: last {args.hours:g}h, observations={int(row[0] or 0)}")
    for label, value in zip(labels, row[1:]):
        print(f"{label}_avg={float(value or 0):.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
