#!/usr/bin/env python3
"""
Quick report tool for trade_journal.db.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Show summary from trade_journal.db")
    parser.add_argument("--db", default="./logs/trade_journal.db", help="SQLite DB path")
    parser.add_argument("--run-id", default=None, help="Filter by run_id")
    parser.add_argument("--limit", type=int, default=30, help="Recent rows limit")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    where = "WHERE run_id=?" if args.run_id else ""
    params = (args.run_id,) if args.run_id else ()

    cur.execute(f"SELECT COUNT(*) AS c FROM strategy_runs {where}", params)
    print(f"runs: {cur.fetchone()['c']}")

    cur.execute(f"SELECT COUNT(*) AS c FROM order_events {where}", params)
    print(f"order_events: {cur.fetchone()['c']}")

    cur.execute(
        f"""
        SELECT event_type, COUNT(*) AS c
        FROM order_events
        {where}
        GROUP BY event_type
        ORDER BY c DESC
        """,
        params,
    )
    print("\norder_events_by_type:")
    for r in cur.fetchall():
        print(f"- {r['event_type']}: {r['c']}")

    cur.execute(
        f"""
        SELECT ts, run_id, event_type, client_order_id, side, price, qty, status, reason, commission_usdc
        FROM order_events
        {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, args.limit),
    )
    print(f"\nrecent_order_events(limit={args.limit}):")
    for r in cur.fetchall():
        print(
            f"- {r['ts']} run={r['run_id']} {r['event_type']} "
            f"coid={r['client_order_id']} side={r['side']} px={r['price']} qty={r['qty']} "
            f"status={r['status']} reason={r['reason']} commission={r['commission_usdc']}"
        )

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
