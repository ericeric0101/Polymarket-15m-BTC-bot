#!/usr/bin/env python3
"""Report evidence-backed invalidation exit counterfactuals from the journal."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitoring.invalidation_counterfactual import load_invalidation_counterfactual


def main() -> int:
    parser = argparse.ArgumentParser(description="Confirmed invalidation exit counterfactual report")
    parser.add_argument("--db", default="./logs/trade_journal.db")
    parser.add_argument("--hours", type=float, default=0)
    parser.add_argument("--slug", action="append", default=[])
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
                SELECT json_extract(payload_json, '$.slug')
                FROM strategy_events
                WHERE event_type='MARKET_CYCLE_PNL'
                  AND json_extract(payload_json, '$.slug') IS NOT NULL
            """
            params: tuple[object, ...] = ()
            if args.hours > 0:
                sql += " AND julianday(ts) >= julianday('now', ?)"
                params = (f"-{args.hours:g} hours",)
            sql += " GROUP BY json_extract(payload_json, '$.slug') ORDER BY MAX(id) DESC"
            slugs = [str(row[0]) for row in conn.execute(sql, params) if row[0]]
        finally:
            conn.close()

    print("slug source time_left bid qty counterfactual_gross actual gross_improvement")
    count = 0
    for slug in slugs:
        item = load_invalidation_counterfactual(db_path, slug)
        if item is None:
            continue
        count += 1
        actual = item["actual_cycle_pnl_usdc"]
        improvement = item["gross_improvement_vs_actual_usdc"]
        print(
            f"{slug} {item['evidence_source']} {item['time_left_sec']} "
            f"{item['best_bid']:.4f} {item['quantity']:.4f} "
            f"{item['counterfactual_gross_pnl_usdc']:+.4f} "
            f"{'n/a' if actual is None else f'{actual:+.4f}'} "
            f"{'n/a' if improvement is None else f'{improvement:+.4f}'}"
        )
    if not count:
        print("No evidence-backed invalidation exits in this window.")
    print("Note: counterfactual is gross, excludes exit fees and fill probability; it is not an executable PnL claim.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
