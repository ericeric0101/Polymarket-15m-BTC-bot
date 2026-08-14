#!/usr/bin/env python3
"""Print fill-versus-settlement PnL attribution without changing strategy state."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitoring.pnl_attribution import load_market_pnl_attribution


def main() -> int:
    parser = argparse.ArgumentParser(description="Trade journal per-market PnL attribution")
    parser.add_argument("--db", default="./logs/trade_journal.db")
    parser.add_argument("--slug", action="append", default=[])
    parser.add_argument("--hours", type=float, default=0, help="Use settled markets in this window")
    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    slugs = list(args.slug)
    if not slugs:
        conn = sqlite3.connect(str(db_path))
        try:
            sql = """
                SELECT DISTINCT json_extract(payload_json, '$.slug')
                FROM strategy_events
                WHERE event_type='MARKET_CYCLE_PNL'
                  AND json_extract(payload_json, '$.slug') IS NOT NULL
            """
            params: tuple[object, ...] = ()
            if args.hours > 0:
                sql += " AND julianday(ts) >= julianday('now', ?)"
                params = (f"-{args.hours:g} hours",)
            sql += " ORDER BY MAX(id) DESC"
            # GROUP BY permits stable market order without relying on JSON text order.
            sql = sql.replace(" ORDER BY MAX(id) DESC", " GROUP BY json_extract(payload_json, '$.slug') ORDER BY MAX(id) DESC")
            slugs = [str(row[0]) for row in conn.execute(sql, params) if row[0]]
        finally:
            conn.close()

    if not slugs:
        print("No settled markets found.")
        return 0
    print("slug buy maker_sell taker_sell redeem computed reported reconciliation_delta")
    for slug in slugs:
        item = load_market_pnl_attribution(db_path, slug)
        reported = item["reported_cycle_pnl_usdc"]
        delta = item["reconciliation_adjustment_usdc"]
        print(
            f"{slug} {item['buy_notional_usdc']:+.4f} "
            f"{item['maker_sell_proceeds_usdc']:+.4f} {item['taker_exit_proceeds_usdc']:+.4f} "
            f"{item['redeem_value_usdc']:+.4f} {item['computed_pnl_usdc']:+.4f} "
            f"{'n/a' if reported is None else f'{reported:+.4f}'} "
            f"{'n/a' if delta is None else f'{delta:+.4f}'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
