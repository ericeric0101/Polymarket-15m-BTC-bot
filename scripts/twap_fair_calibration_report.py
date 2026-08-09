#!/usr/bin/env python3
"""Compare recorded TWAP-model UP probabilities with settled UP outcomes.

Observational only: this does not estimate executable PnL or justify changing
live probability calibration weights without a sufficiently large sample.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


BINS = ((0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/trade_journal.db")
    parser.add_argument("--hours", type=float, default=168.0)
    args = parser.parse_args()
    path = Path(args.db)
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    sql = """
    WITH observations AS (
      SELECT json_extract(payload_json, '$.slug') AS slug,
             CAST(json_extract(payload_json, '$.model_probability_up') AS REAL) AS model_up,
             CAST(json_extract(payload_json, '$.market_probability_up') AS REAL) AS market_up,
             ROW_NUMBER() OVER (
               PARTITION BY json_extract(payload_json, '$.slug')
               ORDER BY id ASC
             ) AS rn
      FROM order_events
      WHERE event_type='ENTRY_EDGE_OBSERVATION'
        AND julianday(ts) >= julianday('now', ?)
    ), settlements AS (
      SELECT json_extract(payload_json, '$.slug') AS slug,
             json_extract(payload_json, '$.outcome') AS outcome
      FROM strategy_events
      WHERE event_type='MARKET_SETTLEMENT'
        AND julianday(ts) >= julianday('now', ?)
    )
    SELECT model_up, market_up, outcome
    FROM observations JOIN settlements USING (slug)
    WHERE rn=1 AND model_up IS NOT NULL AND outcome IN ('UP', 'DOWN')
    """
    with sqlite3.connect(path) as conn:
        rows = conn.execute(sql, (f"-{args.hours} hours", f"-{args.hours} hours")).fetchall()
    print(f"TWAP fair calibration report: last {args.hours:g}h, settled_markets={len(rows)}")
    print("model_up_bin  markets  actual_up  avg_model_up  avg_market_up  calibration_error")
    for low, high in BINS:
        selected = [row for row in rows if low <= float(row[0]) < high]
        if not selected:
            print(f"{low:.1f}-{min(high, 1.0):.1f} {0:>8} {'-':>10} {'-':>13} {'-':>14} {'-':>18}")
            continue
        actual_up = sum(str(row[2]).upper() == "UP" for row in selected) / len(selected)
        avg_model = sum(float(row[0]) for row in selected) / len(selected)
        avg_market = sum(float(row[1]) for row in selected) / len(selected)
        print(
            f"{low:.1f}-{min(high, 1.0):.1f} {len(selected):>8} {actual_up:>10.1%}"
            f" {avg_model:>13.3f} {avg_market:>14.3f} {actual_up - avg_model:>+18.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
