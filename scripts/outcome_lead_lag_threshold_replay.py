#!/usr/bin/env python3
"""Replay Outcome/TWAP threshold grids from existing reference_1s history."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitoring.outcome_lead_lag_replay import ReplayConfig, replay_rows


def _ints(raw: str) -> list[int]:
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="logs/hyperliquid_lead_lag.db")
    parser.add_argument("--shock", default="300,400,500,700,1000")
    parser.add_argument("--residual", default="200,250,300,400,500")
    parser.add_argument("--debounce", default="1,2")
    parser.add_argument("--horizon-ms", type=int, default=5_000)
    parser.add_argument("--limit-pairs", type=int, default=0,
                        help="Optional cap for a quick sample; zero replays all references.")
    args = parser.parse_args()
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as conn:
        query = """
            SELECT run_id, slug, source, price_cents, received_epoch_ns
            FROM reference_1s
            WHERE source IN ('outcome_btc_mark', 'polymarket_twap')
            ORDER BY run_id, slug, received_epoch_ns, source
        """
        rows = conn.execute(query).fetchall()
    if args.limit_pairs:
        rows = rows[:args.limit_pairs]
    results = []
    for shock in _ints(args.shock):
        for residual in _ints(args.residual):
            for debounce in _ints(args.debounce):
                metrics = replay_rows(rows, ReplayConfig(
                    shock_cents=shock, residual_cents=residual,
                    debounce_ticks=debounce, horizon_ms=args.horizon_ms,
                ))
                results.append({"shock_cents": shock, "residual_cents": residual,
                                "debounce_ticks": debounce, **metrics})
    print(json.dumps({"db": args.db, "reference_rows": len(rows),
                      "horizon_ms": args.horizon_ms, "results": results}, indent=2))


if __name__ == "__main__":
    main()
