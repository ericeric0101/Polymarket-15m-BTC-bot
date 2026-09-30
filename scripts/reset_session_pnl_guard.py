#!/usr/bin/env python3
"""Explicit, audited reset of one Taipei session BUY-only guard.

This does not alter orders, fills, inventory, or historical strategy events.
It only establishes a fresh guard baseline after an operator explicitly opts in.
The live process must be stopped before applying it and restarted afterwards.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Direct execution places ``scripts/`` rather than the repository root on
# ``sys.path``.  Keep this standalone operational tool usable without an
# installation step.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from monitoring.trade_journal_db import TradeJournalDB


def _current_session_key() -> str:
    local = datetime.now(ZoneInfo("Asia/Taipei"))
    minute_of_day = local.hour * 60 + local.minute
    if minute_of_day >= 19 * 60 + 30:
        return local.date().isoformat()
    if minute_of_day < 7 * 60 + 30:
        return (local.date() - timedelta(days=1)).isoformat()
    return f"{local.date().isoformat()}-day"


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset one Taipei session PnL BUY guard with an audit event.")
    parser.add_argument("--db", default="logs/trade_journal.db", help="Trade-journal SQLite path")
    parser.add_argument("--date", default=_current_session_key(), help="Taipei session key (night: YYYY-MM-DD; day: YYYY-MM-DD-day)")
    parser.add_argument(
        "--reason",
        required=True,
        help="Operator-provided audit reason; historical fills and events are retained.",
    )
    parser.add_argument("--apply", action="store_true", help="Perform the reset; otherwise print the planned change only.")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_file():
        parser.error(f"journal does not exist: {db_path}")

    db = TradeJournalDB(str(db_path), backup_interval_sec=3600)
    try:
        previous = db.load_session_pnl_state(args.date)
        plan = {
            "session_date_taipei": args.date,
            "reason": args.reason,
            "previous": previous,
            "replacement": {
                "realized_pnl_usdc": 0.0,
                "realized_high_water_usdc": 0.0,
                "profit_guard_armed": False,
                "buy_lock_active": False,
                "buy_lock_reason": "",
            },
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
        if not args.apply:
            print("Dry run only. Re-run with --apply after stopping the live process.")
            return 0

        now = time.time()
        replacement = {
            "session_date_taipei": args.date,
            "realized_pnl_usdc": 0.0,
            "realized_high_water_usdc": 0.0,
            "profit_guard_armed": False,
            "buy_lock_active": False,
            "buy_lock_reason": "",
            "created_ts": now,
        }
        if not db.save_session_pnl_state(replacement):
            print("Session guard reset failed while writing state.", file=sys.stderr)
            return 2
        if not db.log_strategy_event(
            "manual_session_guard_reset",
            "SESSION_GUARD_MANUAL_RESET",
            {
                "session_date_taipei": args.date,
                "reason": args.reason,
                "previous_state": previous,
                "replacement_state": {key: value for key, value in replacement.items() if key != "created_ts"},
                "historical_trade_events_retained": True,
                "applied_ts": now,
            },
        ):
            print("Reset state was written but audit event could not be recorded; do not restart until investigated.", file=sys.stderr)
            return 3
        db.flush_backup()
        print("Applied audited session-guard reset and flushed the journal backup.")
        return 0
    finally:
        db.stop()


if __name__ == "__main__":
    raise SystemExit(main())
