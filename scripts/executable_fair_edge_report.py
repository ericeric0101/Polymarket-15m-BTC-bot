#!/usr/bin/env python3
"""Report cost-adjusted results for shadow-only negative fair-edge buckets.

The report includes only counterfactual candidates that later received a
simulated passive fill. ``model_cost_adjusted_pnl`` subtracts the submit-time
fee buffer, other-cost buffer, and execution penalty recorded by the strategy.
It is not realized PnL: fill simulation, fee estimates, and settlement are all
model inputs, and live exits are deliberately excluded.
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


def _rows(connection: sqlite3.Connection, hours: float) -> dict[str, tuple[object, ...]]:
    cutoff = ""
    params: tuple[object, ...] = ()
    if hours > 0:
        cutoff = " AND julianday(ts) >= julianday('now', ?)"
        params = (f"-{hours} hours",)
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
                 THEN CAST(json_extract(payload_json, '$.simulated_gross_pnl_usdc') AS REAL) END) AS gross_pnl,
        MAX(CAST(json_extract(payload_json, '$.fair_minus_entry') AS REAL)) AS fair_minus_entry,
        MAX(CAST(json_extract(payload_json, '$.qty') AS REAL)) AS qty,
        MAX(CAST(json_extract(payload_json, '$.fee_ps') AS REAL)) AS fee_ps,
        MAX(CAST(json_extract(payload_json, '$.other_cost_ps') AS REAL)) AS other_cost_ps,
        MAX(CAST(json_extract(payload_json, '$.exec_penalty_usdc') AS REAL)) AS exec_penalty_usdc
      FROM events
      GROUP BY client_order_id, bucket
    )
    SELECT
      bucket,
      COALESCE(SUM(candidate), 0),
      COALESCE(SUM(filled), 0),
      COALESCE(SUM(expired), 0),
      COALESCE(SUM(settled), 0),
      COALESCE(SUM(won), 0),
      COALESCE(SUM(CASE WHEN settled=1 THEN 1-won ELSE 0 END), 0),
      COALESCE(SUM(gross_pnl), 0.0),
      AVG(fair_minus_entry),
      COALESCE(SUM(CASE WHEN settled=1 AND fee_ps IS NOT NULL
                          AND other_cost_ps IS NOT NULL AND exec_penalty_usdc IS NOT NULL
                        THEN 1 ELSE 0 END), 0) AS cost_covered,
      COALESCE(SUM(CASE WHEN settled=1 AND fee_ps IS NOT NULL
                          AND other_cost_ps IS NOT NULL AND exec_penalty_usdc IS NOT NULL
                        THEN (qty * (fee_ps + other_cost_ps)) + exec_penalty_usdc
                        ELSE 0 END), 0.0) AS model_cost,
      COALESCE(SUM(CASE WHEN settled=1 AND fee_ps IS NOT NULL
                          AND other_cost_ps IS NOT NULL AND exec_penalty_usdc IS NOT NULL
                        THEN gross_pnl - ((qty * (fee_ps + other_cost_ps)) + exec_penalty_usdc)
                        ELSE 0 END), 0.0) AS model_cost_adjusted_pnl
    FROM grouped
    GROUP BY bucket
    """
    return {row[0]: row[1:] for row in connection.execute(sql, params).fetchall()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/trade_journal.db")
    parser.add_argument("--hours", type=float, default=0.0, help="0 means all retained data")
    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")

    with sqlite3.connect(db_path) as connection:
        rows = _rows(connection, args.hours)

    print("Executable fair-edge shadow report")
    print("shadow-only; simulated passive fills; settlement PnL excludes live exits")
    print("model-cost adjusted = gross settlement PnL - recorded submit-time fee/other-cost/exec-penalty")
    print("bucket                       cand fill exp set W/L    fill%   win%  avg_edge  covered  model_cost  adjusted_pnl")
    totals = [0, 0, 0, 0, 0, 0, 0.0, 0, 0.0, 0.0]
    weighted_edge_sum = 0.0
    weighted_edge_count = 0
    for bucket in BUCKET_ORDER:
        row = rows.get(bucket, (0, 0, 0, 0, 0, 0, 0.0, None, 0, 0.0, 0.0))
        candidates, filled, expired, settled, wins, losses, gross, avg_edge, covered, cost, adjusted = row
        values = [int(candidates), int(filled), int(expired), int(settled), int(wins), int(losses), float(gross), int(covered), float(cost), float(adjusted)]
        totals = [left + right for left, right in zip(totals, values)]
        if avg_edge is not None and values[0]:
            weighted_edge_sum += float(avg_edge) * values[0]
            weighted_edge_count += values[0]
        fill_rate = values[1] / values[0] if values[0] else 0.0
        win_rate = values[4] / values[3] if values[3] else 0.0
        edge_display = f"{float(avg_edge):+.4f}" if avg_edge is not None else "     NA"
        print(
            f"{bucket:<28} {values[0]:>4} {values[1]:>4} {values[2]:>3} {values[3]:>3} "
            f"{values[4]:>2}/{values[5]:<2} {fill_rate:>7.1%} {win_rate:>6.1%} {edge_display:>9} "
            f"{values[7]:>7}/{values[3]:<3} {values[8]:>+11.4f} {values[9]:>+13.4f}"
        )
    fill_rate = totals[1] / totals[0] if totals[0] else 0.0
    win_rate = totals[4] / totals[3] if totals[3] else 0.0
    avg_edge = weighted_edge_sum / weighted_edge_count if weighted_edge_count else None
    edge_display = f"{avg_edge:+.4f}" if avg_edge is not None else "     NA"
    print(
        f"{'TOTAL':<28} {totals[0]:>4} {totals[1]:>4} {totals[2]:>3} {totals[3]:>3} "
        f"{totals[4]:>2}/{totals[5]:<2} {fill_rate:>7.1%} {win_rate:>6.1%} {edge_display:>9} "
        f"{totals[7]:>7}/{totals[3]:<3} {totals[8]:>+11.4f} {totals[9]:>+13.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
