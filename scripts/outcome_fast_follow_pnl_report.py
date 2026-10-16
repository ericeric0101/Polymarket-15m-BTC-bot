#!/usr/bin/env python3
"""Print conservative, source-attributed Outcome fast-follow realised PnL."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Scripts are invoked by path in operations, so add the repository root before
# importing the journal module (the test runner already has it on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitoring.pnl_attribution import load_fast_follow_pnl_summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/trading/trade_journal.db")
    args = parser.parse_args()
    summary = load_fast_follow_pnl_summary(Path(args.db))
    print(f"completed_trade_count={summary['completed_trade_count']}")
    print(f"completed_pnl_usdc={summary['completed_pnl_usdc']:.6f}")
    print(f"excluded_open_or_unreconciled_count={summary['excluded_open_or_unreconciled_count']}")
    print(f"excluded_mixed_source_count={summary['excluded_mixed_source_count']}")
    for row in summary["markets"]:
        print(
            f"{row['slug']} pnl={float(row['attributable_pnl_usdc']):.6f} "
            f"buy={row['buy_notional_usdc']:.6f} "
            f"exit_cash={row['maker_sell_proceeds_usdc'] + row['taker_exit_proceeds_usdc'] + row['redeem_value_usdc']:.6f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
