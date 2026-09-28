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
    parsed = []
    for ts, raw in rows:
        payload = json.loads(raw or "{}")
        parsed.append((ts, payload, Decimal(str(payload.get("cycle_combined_pnl_usdc", 0))),
                       datetime.fromisoformat(ts).astimezone(ZoneInfo("Asia/Taipei")).date().isoformat()))
    scenarios = {
        "NO_GUARD": SessionPnlGuardConfig(enabled=False),
        "LIVE_DEFAULT": SessionPnlGuardConfig(),
        "HARD_CAP_10": SessionPnlGuardConfig(hard_profit_lock_enabled=True),
    }
    out = []
    for scenario, config in scenarios.items():
        guards: dict[str, SessionPnlGuard] = {}
        locked: dict[str, dict] = {}
        final: dict[str, Decimal] = {}
        avoided: dict[str, Decimal] = {}
        counts: dict[str, list[int]] = {}
        for ts, payload, pnl, local_date in parsed:
            guard = guards.setdefault(local_date, SessionPnlGuard(config, session_date=local_date))
            final[local_date] = final.get(local_date, Decimal("0")) + pnl
            counts.setdefault(local_date, [0, 0])
            if guard.decision().allowed:
                counts[local_date][0] += 1
                guard.apply_realized_delta(pnl)
                decision = guard.decision()
                if not decision.allowed and local_date not in locked:
                    locked[local_date] = {"lock_ts": ts, "lock_reason": decision.reason,
                                          "pnl_at_lock": decision.realized_pnl_usdc,
                                          "high_water_at_lock": decision.realized_high_water_usdc}
            else:
                counts[local_date][1] += 1; avoided[local_date] = avoided.get(local_date, Decimal("0")) + pnl
        for local_date in sorted(final):
            details = locked.get(local_date, {})
            retained = final[local_date] - avoided.get(local_date, Decimal("0"))
            out.append({"scenario": scenario, "session_date_taipei": local_date, **details,
                        "markets_before_lock": counts[local_date][0], "markets_after_lock": counts[local_date][1],
                        "actual_final_pnl": float(final[local_date]), "counterfactual_avoided_market_pnl": float(avoided.get(local_date, Decimal("0"))),
                        "counterfactual_retained_pnl": float(retained),
                        "caveat": "descriptive_counterfactual_not_causal"})
    fields = sorted({key for row in out for key in row}) if out else ["scenario", "session_date_taipei"]
    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(out)
    print(f"wrote {len(out)} rows: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
