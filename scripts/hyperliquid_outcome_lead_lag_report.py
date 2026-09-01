#!/usr/bin/env python3
"""Evaluate whether BTC daily Outcome changes precede Polymarket 15m mids.

This is research only: it reads journal snapshots and never imports execution
code or produces a trading decision.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any


def _correlation(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs, ys = zip(*pairs)
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    denominator = math.sqrt(sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys))
    return numerator / denominator if denominator > 0 else None


def load_snapshots(db_path: str | Path) -> list[dict[str, float]]:
    db_uri = f"file:{Path(db_path).resolve()}?mode=ro"
    # The live bot is the sole writer. A report is disposable research, so it
    # must fail quickly rather than wait behind an unexpected writer lock.
    with sqlite3.connect(db_uri, uri=True, timeout=2.0) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT payload_json FROM strategy_events WHERE event_type='EXTERNAL_LEAD_LAG_SNAPSHOT' ORDER BY id"
        ).fetchall()
    snapshots = []
    for (raw,) in rows:
        try:
            payload = json.loads(raw)
            timestamp = float(payload["observed_ts"])
            outcome_yes = float(payload["hyperliquid_outcome_yes_mid"])
            polymarket_up = float(payload["up_mid"])
            if bool(payload.get("hyperliquid_outcome_available")) and outcome_yes > 0 and polymarket_up > 0:
                snapshots.append({"ts": timestamp, "outcome_yes": outcome_yes, "polymarket_up": polymarket_up})
        except (KeyError, TypeError, ValueError):
            continue
    return snapshots


def build_report(snapshots: list[dict[str, float]], *, snapshot_interval_sec: float = 5.0) -> dict[str, Any]:
    report: dict[str, Any] = {"snapshot_count": len(snapshots), "horizons": {}}
    for horizon_sec in (5, 15, 30, 60):
        lag = int(round(horizon_sec / snapshot_interval_sec))
        pairs: list[tuple[float, float]] = []
        for index in range(1, len(snapshots) - lag):
            previous, current, future = snapshots[index - 1], snapshots[index], snapshots[index + lag]
            if current["ts"] - previous["ts"] > snapshot_interval_sec * 1.5:
                continue
            if future["ts"] - current["ts"] > horizon_sec + snapshot_interval_sec * 1.5:
                continue
            outcome_change = current["outcome_yes"] - previous["outcome_yes"]
            polymarket_future_change = future["polymarket_up"] - current["polymarket_up"]
            if outcome_change != 0 and polymarket_future_change != 0:
                pairs.append((outcome_change, polymarket_future_change))
        report["horizons"][str(horizon_sec)] = {
            "sample_count": len(pairs),
            "correlation": _correlation(pairs),
            "sign_agreement_rate": (
                sum(1 for outcome, polymarket in pairs if outcome * polymarket > 0) / len(pairs)
                if pairs else None
            ),
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Hyperliquid Outcome → Polymarket lead/lag report")
    parser.add_argument("--db", default="logs/trade_journal.db")
    args = parser.parse_args()
    print(json.dumps(build_report(load_snapshots(args.db)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
