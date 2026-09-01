#!/usr/bin/env python3
"""Quality-gated, market-scoped Outcome → Polymarket lead/lag research."""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


def _correlation(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs, ys = zip(*pairs)
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = math.sqrt(sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys))
    return sum((x - x_mean) * (y - y_mean) for x, y in pairs) / denominator if denominator else None


def load_snapshots(db_path: str | Path) -> list[dict[str, Any]]:
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2.0) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT run_id, payload_json FROM strategy_events WHERE event_type='EXTERNAL_LEAD_LAG_SNAPSHOT' ORDER BY id"
        ).fetchall()
    snapshots = []
    for run_id, raw in rows:
        try:
            payload = json.loads(raw)
            if not bool(payload.get("hyperliquid_outcome_analysis_available")):
                continue
            snapshots.append({
                "run_id": str(run_id), "slug": str(payload["slug"]),
                "market_id": int(payload["hyperliquid_outcome_market_id"]),
                "ts": float(payload["observed_ts"]),
                "outcome_side0": float(payload["hyperliquid_outcome_side0_bbo_mid"]),
                "polymarket_up": float(payload["up_mid"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return snapshots


def build_report(snapshots: list[dict[str, Any]], *, snapshot_interval_sec: float = 5.0) -> dict[str, Any]:
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for snapshot in snapshots:
        groups[(snapshot["run_id"], snapshot["slug"], snapshot["market_id"])].append(snapshot)
    report: dict[str, Any] = {"snapshot_count": len(snapshots), "group_count": len(groups), "horizons": {}}
    for horizon_sec in (5, 15, 30, 60):
        pairs: list[tuple[float, float]] = []
        for group in groups.values():
            group.sort(key=lambda row: row["ts"])
            timestamps = [row["ts"] for row in group]
            for index in range(1, len(group)):
                previous, current = group[index - 1], group[index]
                if current["ts"] - previous["ts"] > snapshot_interval_sec * 1.5:
                    continue
                future_index = bisect.bisect_left(timestamps, current["ts"] + horizon_sec)
                if future_index >= len(group) or group[future_index]["ts"] > current["ts"] + horizon_sec + snapshot_interval_sec * 1.5:
                    continue
                future = group[future_index]
                pairs.append((current["outcome_side0"] - previous["outcome_side0"], future["polymarket_up"] - current["polymarket_up"]))
        directional = [pair for pair in pairs if pair[0] != 0]
        report["horizons"][str(horizon_sec)] = {
            "sample_count": len(pairs),
            "outcome_move_sample_count": len(directional),
            "correlation": _correlation(pairs),
            # A zero future Polymarket move is a non-follow, not an excluded row.
            "sign_agreement_rate": (sum(1 for outcome, polymarket in directional if outcome * polymarket > 0) / len(directional)) if directional else None,
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only quality-gated Outcome → Polymarket lead/lag report")
    parser.add_argument("--db", default="logs/trade_journal.db")
    print(json.dumps(build_report(load_snapshots(parser.parse_args().db)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
