#!/usr/bin/env python3
"""
Hourly attribution report for recent performance.

Breakdown per hour:
- Fill realized net
- Settlement pnl
- Combined pnl
- Fees / implied bps
- Taker exits
- Directional edge snapshot averages
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import List


def _fmt(x: float, digits: int = 6) -> str:
    return f"{x:.{digits}f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Hourly attribution report from trade_journal.db")
    ap.add_argument("--db", default="./logs/trade_journal.db", help="SQLite DB path")
    ap.add_argument("--hours", type=int, default=24, help="Lookback hours")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"DB not found: {db}")
        return 1

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute(
        """
        WITH fills AS (
          SELECT
            substr(ts,1,13) AS h,
            COUNT(*) AS fills,
            SUM(COALESCE(json_extract(payload_json,'$.realized_net_usdc'),0)) AS fill_realized_usdc,
            SUM(COALESCE(commission_usdc,0)) AS commissions_usdc,
            SUM(COALESCE(price,0) * COALESCE(qty,0)) AS fill_notional_usdc,
            AVG(COALESCE(json_extract(payload_json,'$.directional_edge_ps_submit'), NULL)) AS avg_directional_edge_ps,
            AVG(COALESCE(json_extract(payload_json,'$.directional_edge_usdc_submit'), NULL)) AS avg_directional_edge_usdc
          FROM order_events
          WHERE event_type='ORDER_FILLED'
            AND ts >= datetime('now', ?)
          GROUP BY h
        ),
        sett AS (
          SELECT
            substr(ts,1,13) AS h,
            COUNT(*) AS settlements,
            SUM(COALESCE(json_extract(payload_json,'$.settlement_pnl_usdc'),0)) AS settlement_pnl_usdc
          FROM strategy_events
          WHERE event_type='MARKET_SETTLEMENT'
            AND ts >= datetime('now', ?)
          GROUP BY h
        ),
        exits AS (
          SELECT
            substr(ts,1,13) AS h,
            COUNT(*) AS taker_exits
          FROM order_events
          WHERE event_type='ORDER_TAKER_EXIT_SUBMIT'
            AND ts >= datetime('now', ?)
          GROUP BY h
        ),
        hs AS (
          SELECT h FROM fills
          UNION
          SELECT h FROM sett
          UNION
          SELECT h FROM exits
        )
        SELECT
          hs.h AS hour_utc,
          COALESCE(f.fills,0) AS fills,
          COALESCE(f.fill_realized_usdc,0) AS fill_realized_usdc,
          COALESCE(f.commissions_usdc,0) AS commissions_usdc,
          COALESCE(f.fill_notional_usdc,0) AS fill_notional_usdc,
          CASE
            WHEN COALESCE(f.fill_notional_usdc,0) > 0
            THEN (COALESCE(f.commissions_usdc,0) / f.fill_notional_usdc) * 10000.0
            ELSE NULL
          END AS implied_fee_bps,
          COALESCE(s.settlements,0) AS settlements,
          COALESCE(s.settlement_pnl_usdc,0) AS settlement_pnl_usdc,
          COALESCE(e.taker_exits,0) AS taker_exits,
          COALESCE(f.avg_directional_edge_ps, NULL) AS avg_directional_edge_ps,
          COALESCE(f.avg_directional_edge_usdc, NULL) AS avg_directional_edge_usdc,
          COALESCE(f.fill_realized_usdc,0) + COALESCE(s.settlement_pnl_usdc,0) AS combined_pnl_usdc
        FROM hs
        LEFT JOIN fills f ON f.h = hs.h
        LEFT JOIN sett s ON s.h = hs.h
        LEFT JOIN exits e ON e.h = hs.h
        ORDER BY hs.h
        """,
        (f"-{args.hours} hours", f"-{args.hours} hours", f"-{args.hours} hours"),
    ).fetchall()

    if not rows:
        print("No rows in selected window.")
        conn.close()
        return 0

    print("=" * 112)
    print("Hourly Attribution Report")
    print("=" * 112)
    print(f"db: {db}")
    print(f"lookback_hours: {args.hours}")
    print("")
    print(
        "hour_utc | fills | fill_realized | settlement | combined | fee_bps | taker_exits | avg_dir_edge_ps"
    )
    print("-" * 112)

    total_combined = 0.0
    total_fill_realized = 0.0
    total_settlement = 0.0
    total_commission = 0.0
    total_notional = 0.0
    total_taker_exits = 0
    neg_combined_hours = 0
    notes: List[str] = []

    for r in rows:
        fill_realized = float(r["fill_realized_usdc"] or 0.0)
        settlement = float(r["settlement_pnl_usdc"] or 0.0)
        combined = float(r["combined_pnl_usdc"] or 0.0)
        fee_bps = r["implied_fee_bps"]
        taker_exits = int(r["taker_exits"] or 0)
        avg_dir_edge_ps = r["avg_directional_edge_ps"]

        total_fill_realized += fill_realized
        total_settlement += settlement
        total_combined += combined
        total_commission += float(r["commissions_usdc"] or 0.0)
        total_notional += float(r["fill_notional_usdc"] or 0.0)
        total_taker_exits += taker_exits
        if combined < 0:
            neg_combined_hours += 1

        print(
            f"{r['hour_utc']} | {int(r['fills'] or 0):>5d} | "
            f"{_fmt(fill_realized):>12} | {_fmt(settlement):>10} | {_fmt(combined):>10} | "
            f"{_fmt(float(fee_bps),2) if fee_bps is not None else 'n/a':>7} | "
            f"{taker_exits:>11d} | "
            f"{_fmt(float(avg_dir_edge_ps),4) if avg_dir_edge_ps is not None else 'n/a':>14}"
        )

    implied_fee_bps_total = (total_commission / total_notional) * 10000.0 if total_notional > 0 else None
    print("-" * 112)
    print(
        f"TOTAL    |       | {_fmt(total_fill_realized):>12} | {_fmt(total_settlement):>10} | "
        f"{_fmt(total_combined):>10} | "
        f"{_fmt(implied_fee_bps_total,2) if implied_fee_bps_total is not None else 'n/a':>7} | "
        f"{total_taker_exits:>11d} | {'-':>14}"
    )

    if implied_fee_bps_total is not None and implied_fee_bps_total > 220:
        notes.append("Fee bps is elevated; reduce taker usage and requote churn.")
    if total_settlement < 0 and abs(total_settlement) > max(1.0, abs(total_fill_realized) * 0.5):
        notes.append("Settlement loss dominates; tighten pre-close inventory target.")
    if total_taker_exits >= max(6, args.hours // 3):
        notes.append("Taker exits are frequent; widen maker exit runway before fail-safe taker.")
    if neg_combined_hours >= max(4, args.hours // 2):
        notes.append("Many negative hours; enable/strengthen regime guard and BUY edge gate.")

    print("")
    print("Suggestions:")
    if notes:
        for n in notes:
            print(f"- {n}")
    else:
        print("- No acute risk signal in this window.")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
