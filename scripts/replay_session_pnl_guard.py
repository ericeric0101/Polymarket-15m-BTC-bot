#!/usr/bin/env python3
"""Replay completed-cycle PnL under the persistent Taipei-session BUY guard.

This is descriptive research.  A lock does not prove that subsequent markets
would have been traded unchanged, so the reported avoided PnL is not causal.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.session_pnl_guard import SessionPnlGuard, SessionPnlGuardConfig


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/trading/trade_journal.db")
    parser.add_argument("--output", default="reports/stop_forensics/session_pnl_guard_replay.csv")
    args = parser.parse_args()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.db)
    if not db_path.is_file() and args.db == "data/trading/trade_journal.db" and Path("logs/trade_journal.db").is_file():
        db_path = Path("logs/trade_journal.db")
    if not db_path.is_file():
        parser.error(f"journal database not found: {args.db}")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT ts, payload_json FROM strategy_events WHERE event_type='MARKET_CYCLE_PNL' ORDER BY id").fetchall()
    guards: dict[str, SessionPnlGuard] = {}
    out = []
    for ts, raw in rows:
        payload = json.loads(raw or "{}")
        pnl = Decimal(str(payload.get("cycle_combined_pnl_usdc", 0)))
        local_date = datetime.fromisoformat(ts).astimezone(ZoneInfo("Asia/Taipei")).date().isoformat()
        # Replay includes the requested +$10 optional cap even though the
        # live default remains disabled; this makes its counterfactual visible.
        guard = guards.setdefault(local_date, SessionPnlGuard(
            SessionPnlGuardConfig(hard_profit_lock_enabled=True), session_date=local_date
        ))
        was_allowed = guard.decision().allowed
        guard.apply_realized_delta(pnl)
        decision = guard.decision()
        out.append({"ts": ts, "session_date_taipei": local_date, "slug": payload.get("slug"), "cycle_pnl_usdc": float(pnl),
                    "buy_was_allowed_before_cycle": was_allowed, "guard_allowed_after_cycle": decision.allowed,
                    "lock_reason": decision.reason, "realized_pnl_usdc": float(decision.realized_pnl_usdc),
                    "high_water_usdc": float(decision.realized_high_water_usdc),
                    "drawdown_from_high_usdc": float(decision.drawdown_from_high_usdc)})
    fields = list(out[0]) if out else ["ts", "session_date_taipei", "slug", "cycle_pnl_usdc"]
    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(out)
    print(f"wrote {len(out)} rows: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
