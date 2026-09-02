#!/usr/bin/env python3
"""Read-only test of Outcome BTC mark leading Polymarket's BTC TWAP.

This intentionally does *not* infer an edge from the Outcome contract BBO.
The research question is whether the BTC mark received from Hyperliquid's
mainnet ``allMids`` stream moves before Polymarket's frontend Chainlink TWAP.
Binance-to-that-same-TWAP is reported as a benchmark, not a trading signal.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


HORIZONS_SEC = (5, 10, 15, 30, 60)
MAX_SOURCE_AGE_SEC = 5.0
MIN_LEADER_MOVE_USD = 5.0


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _correlation(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs, ys = zip(*pairs)
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = math.sqrt(sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys))
    return sum((x - x_mean) * (y - y_mean) for x, y in pairs) / denominator if denominator else None


def _fresh_age(payload: dict[str, Any], key: str) -> bool:
    age = _number(payload.get(key))
    return age is not None and 0.0 <= age <= MAX_SOURCE_AGE_SEC


def load_snapshots(db_path: str | Path) -> list[dict[str, Any]]:
    """Load only time-aligned, fresh Outcome-mark and Polymarket-TWAP rows."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2.0) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT run_id, polymarket_slug, hyperliquid_market_id, observed_ts_ms, payload_json FROM snapshots ORDER BY id"
        ).fetchall()
    snapshots = []
    for run_id, slug, market_id, observed_ts_ms, raw in rows:
        try:
            payload = json.loads(raw)
            outcome_mark = _number(payload.get("hyperliquid_outcome_btc_mark"))
            polymarket_twap = _number(payload.get("twap_price"))
            if (
                not bool(payload.get("hyperliquid_outcome_available"))
                or not _fresh_age(payload, "hyperliquid_outcome_mids_age_sec")
                or not _fresh_age(payload, "twap_age_sec")
                or outcome_mark is None
                or polymarket_twap is None
            ):
                continue
            snapshots.append({
                "run_id": str(run_id), "slug": str(slug), "market_id": int(market_id),
                "ts": float(observed_ts_ms) / 1000.0, "outcome_btc_mark": outcome_mark,
                "polymarket_twap": polymarket_twap,
                "binance_price": _number(payload.get("binance_price")) if _fresh_age(payload, "binance_age_sec") else None,
            })
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return snapshots


def _horizon_pairs(
    groups: dict[tuple[str, str, int], list[dict[str, Any]]], *, leader_key: str, follower_key: str,
    snapshot_interval_sec: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon_sec in HORIZONS_SEC:
        pairs: list[tuple[float, float]] = []
        for group in groups.values():
            group.sort(key=lambda row: row["ts"])
            timestamps = [row["ts"] for row in group]
            for index in range(1, len(group)):
                previous, current = group[index - 1], group[index]
                leader_previous, leader_current = _number(previous.get(leader_key)), _number(current.get(leader_key))
                follower_current = _number(current.get(follower_key))
                if leader_previous is None or leader_current is None or follower_current is None:
                    continue
                if current["ts"] - previous["ts"] > snapshot_interval_sec * 1.5:
                    continue
                future_index = bisect.bisect_left(timestamps, current["ts"] + horizon_sec)
                if future_index >= len(group) or group[future_index]["ts"] > current["ts"] + horizon_sec + snapshot_interval_sec * 1.5:
                    continue
                follower_future = _number(group[future_index].get(follower_key))
                if follower_future is None:
                    continue
                leader_move = leader_current - leader_previous
                if abs(leader_move) < MIN_LEADER_MOVE_USD:
                    continue
                pairs.append((leader_move, follower_future - follower_current))
        result[str(horizon_sec)] = {
            "sample_count": len(pairs), "correlation": _correlation(pairs),
            "follow_through_rate": (
                sum(1 for leader, follower in pairs if leader * follower > 0) / len(pairs)
                if pairs else None
            ),
            "mean_follower_move_usd": (sum(follower for _, follower in pairs) / len(pairs)) if pairs else None,
        }
    return result


def build_report(snapshots: list[dict[str, Any]], *, snapshot_interval_sec: float = 5.0) -> dict[str, Any]:
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for snapshot in snapshots:
        groups[(snapshot["run_id"], snapshot["slug"], snapshot["market_id"])].append(snapshot)
    return {
        "research_question": "Does the Outcome BTC mark lead Polymarket Chainlink TWAP?",
        "snapshot_count": len(snapshots), "group_count": len(groups),
        "minimum_leader_move_usd": MIN_LEADER_MOVE_USD,
        "outcome_btc_mark_to_polymarket_twap": _horizon_pairs(
            groups, leader_key="outcome_btc_mark", follower_key="polymarket_twap", snapshot_interval_sec=snapshot_interval_sec,
        ),
        "binance_to_polymarket_twap_benchmark": _horizon_pairs(
            groups, leader_key="binance_price", follower_key="polymarket_twap", snapshot_interval_sec=snapshot_interval_sec,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome BTC mark → Polymarket TWAP lead/lag report")
    parser.add_argument("--db", default="logs/hyperliquid_lead_lag.db")
    print(json.dumps(build_report(load_snapshots(parser.parse_args().db)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
