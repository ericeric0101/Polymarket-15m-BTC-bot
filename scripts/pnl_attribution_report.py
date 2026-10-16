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

from monitoring.pnl_attribution import load_effective_market_pnl, load_market_pnl_attributions


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
    ledger = load_market_pnl_attributions(db_path, set(slugs))
    effective = load_effective_market_pnl(db_path, slugs=set(slugs))
    # MARKET_CYCLE_PNL is also written for idle cycles.  Keep the operational
    # report focused on markets with cash or a journaled fill; explicitly
    # requested slugs remain visible for diagnosis.
    if not args.slug:
        slugs = [
            slug for slug in slugs
            if (item := ledger.get(slug, {})).get("fill_count", 0)
            or abs(float(item.get("redeem_value_usdc", 0.0))) > 0
        ]
    if not slugs:
        print("No markets with journaled fills or redemption cash found.")
        return 0
    print("slug status buy maker_sell taker_sell redeem attributable computed reported reconciliation_delta "
          "| effective_basis effective_pnl")
    attributable_total = 0.0
    effective_final_total = 0.0
    pre_journal_count = 0
    for slug in slugs:
        item = ledger.get(slug, {})
        eff = effective.get(slug, {})
        reported = item["reported_cycle_pnl_usdc"]
        delta = item["reconciliation_adjustment_usdc"]
        attributable = item.get("attributable_pnl_usdc")
        if attributable is not None:
            attributable_total += float(attributable)
        if item.get("accounting_status") == "pre_journal_inventory":
            pre_journal_count += 1
        print(
            f"{slug} {item.get('accounting_status', 'unknown')} {item['buy_notional_usdc']:+.4f} "
            f"{item['maker_sell_proceeds_usdc']:+.4f} {item['taker_exit_proceeds_usdc']:+.4f} "
            f"{item['redeem_value_usdc']:+.4f} "
            f"{'n/a' if attributable is None else f'{attributable:+.4f}'} {item['computed_pnl_usdc']:+.4f} "
            f"{'n/a' if reported is None else f'{reported:+.4f}'} "
            f"{'n/a' if delta is None else f'{delta:+.4f}'} "
            f"| {eff.get('pnl_basis', 'n/a')} "
            f"{'n/a' if eff.get('effective_pnl_usdc') is None else format(eff['effective_pnl_usdc'], '+.4f')}"
        )
        if eff.get("is_final"):
            effective_final_total += float(eff["effective_pnl_usdc"])
    print(f"attributable_total_usdc={attributable_total:+.4f} pre_journal_inventory_markets={pre_journal_count}")
    # The authoritative number: same projection as the dashboards (fills + journaled evidence).
    print(f"effective_final_total_usdc={effective_final_total:+.4f} (monitoring.pnl_attribution.load_effective_market_pnl)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
