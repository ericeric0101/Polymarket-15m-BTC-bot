#!/usr/bin/env python3
"""Summarize submit-time-approved fair-edge counterfactuals by edge bucket.

These are shadow-only observations: they never create live orders. A simulated
fill requires a later ask at or below the original passive limit. Results are
settlement-only, pre-fee and pre-slippage.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


BUCKET_ORDER = (
    "lt_neg_0_15",
    "neg_0_15_to_neg_0_10",
    "neg_0_10_to_neg_0_05",
    "neg_0_05_to_neg_0_02",
    "neg_0_02_to_0",
    "0_to_0_002",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/trade_journal.db")
    parser.add_argument("--hours", type=float, default=0.0, help="0 means all retained data")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")
    cutoff = ""
    params: tuple[object, ...] = ()
    if args.hours > 0:
        cutoff = " AND julianday(ts) >= julianday('now', ?)"
        params = (f"-{args.hours} hours",)

    sql = """
    WITH events AS (
      SELECT event_type, client_order_id, payload_json
      FROM order_events
      WHERE event_type IN (
        'FAIR_EDGE_BUCKET_SHADOW_CANDIDATE',
        'FAIR_EDGE_BUCKET_SHADOW_FILLED',
        'FAIR_EDGE_BUCKET_SHADOW_EXPIRED',
        'FAIR_EDGE_BUCKET_SHADOW_SETTLED'
      )
    """ + cutoff + """
    ), grouped AS (
      SELECT
        json_extract(payload_json, '$.bucket') AS bucket,
        client_order_id AS simulation_id,
        MAX(event_type='FAIR_EDGE_BUCKET_SHADOW_CANDIDATE') AS candidate,
        MAX(event_type='FAIR_EDGE_BUCKET_SHADOW_FILLED') AS filled,
        MAX(event_type='FAIR_EDGE_BUCKET_SHADOW_EXPIRED') AS expired,
        MAX(event_type='FAIR_EDGE_BUCKET_SHADOW_SETTLED') AS settled,
        MAX(CASE WHEN event_type='FAIR_EDGE_BUCKET_SHADOW_SETTLED'
                 THEN CAST(json_extract(payload_json, '$.won') AS INTEGER) END) AS won,
        MAX(CASE WHEN event_type='FAIR_EDGE_BUCKET_SHADOW_SETTLED'
                 THEN CAST(json_extract(payload_json, '$.simulated_gross_pnl_usdc') AS REAL) END) AS gross_pnl
      FROM events
      GROUP BY client_order_id, bucket
    )
    SELECT bucket,
           SUM(candidate), SUM(filled), SUM(expired), SUM(settled),
           COALESCE(SUM(won), 0),
           COALESCE(SUM(CASE WHEN settled=1 THEN 1-won ELSE 0 END), 0),
           COALESCE(SUM(gross_pnl), 0.0)
    FROM grouped
    GROUP BY bucket
    """
    with sqlite3.connect(db_path) as conn:
        rows = {row[0]: row[1:] for row in conn.execute(sql, params).fetchall()}

    print("Fair-edge bucket shadow report")
    print("shadow-only; passive fill requires a later ask <= limit; settlement PnL is pre-fee/pre-slippage")
    print("bucket                       candidates  filled  fill_rate  expired  settled  wins/losses  win_rate  gross_pnl")
    totals = [0, 0, 0, 0, 0, 0, 0.0]
    for bucket in BUCKET_ORDER:
        candidate, filled, expired, settled, wins, losses, gross_pnl = rows.get(bucket, (0, 0, 0, 0, 0, 0, 0.0))
        values = [int(candidate or 0), int(filled or 0), int(expired or 0), int(settled or 0), int(wins or 0), int(losses or 0), float(gross_pnl or 0.0)]
        totals = [left + right for left, right in zip(totals, values)]
        fill_rate = values[1] / values[0] if values[0] else 0.0
        win_rate = values[4] / values[3] if values[3] else 0.0
        print(
            f"{bucket:<28} {values[0]:>10} {values[1]:>7} {fill_rate:>9.1%}"
            f" {values[2]:>8} {values[3]:>8} {values[4]:>4}/{values[5]:<6}"
            f" {win_rate:>8.1%} {values[6]:>+10.4f}"
        )
    fill_rate = totals[1] / totals[0] if totals[0] else 0.0
    win_rate = totals[4] / totals[3] if totals[3] else 0.0
    print(
        f"{'TOTAL':<28} {totals[0]:>10} {totals[1]:>7} {fill_rate:>9.1%}"
        f" {totals[2]:>8} {totals[3]:>8} {totals[4]:>4}/{totals[5]:<6}"
        f" {win_rate:>8.1%} {totals[6]:>+10.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
