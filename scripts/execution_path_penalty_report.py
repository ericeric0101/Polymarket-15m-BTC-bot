#!/usr/bin/env python3
"""Read-only 10-second markout report by execution path and filled size."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path


def _payload(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def execution_path(client_order_id: str, side: str, liquidity_class: str) -> str:
    order_id = str(client_order_id or "")
    if order_id.startswith("BTC-15M-FAST-FOLLOW-BUY-"):
        return "fast_follow_fok_buy"
    if str(side).upper() == "SELL" and order_id.startswith("BTC-15M-TAKER-EXIT-"):
        return "taker_exit_sell"
    if str(side).upper() == "BUY" and str(liquidity_class).lower() == "maker":
        return "maker_buy"
    return f"{str(liquidity_class or 'unknown').lower()}_{str(side or 'unknown').lower()}"


def size_bucket(quantity: float) -> str:
    if quantity <= 0:
        return "unknown"
    if quantity <= 6.0:
        return "5_5_shares"
    if quantity <= 11.0:
        return "10_shares"
    return "other"


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, math.ceil(len(ordered) * 0.90) - 1)
    return ordered[index]


def summarize(values: list[float]) -> dict[str, float | int]:
    adverse = [max(0.0, -value) for value in values]
    cap = _p90(adverse)
    return {
        "n": len(values),
        "mean_signed_ps": sum(values) / len(values),
        "raw_adverse_ps": sum(adverse) / len(adverse),
        "winsorized_adverse_ps": sum(min(value, cap) for value in adverse) / len(adverse),
        "p90_adverse_ps": cap,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/trade_journal.db")
    parser.add_argument("--hours", type=float, default=168.0)
    parser.add_argument("--horizon-sec", type=int, default=10)
    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")

    fills: dict[str, tuple[str, str, float]] = {}
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    markets: dict[tuple[str, str], set[str]] = defaultdict(set)
    with sqlite3.connect(db_path) as conn:
        for order_id, side, qty, raw in conn.execute(
            """SELECT client_order_id, side, qty, payload_json FROM order_events
               WHERE event_type='ORDER_FILLED'"""
        ):
            payload = _payload(raw)
            fills[str(order_id or "")] = (
                str(side or ""), str(payload.get("liquidity_class") or ""), float(qty or 0.0),
            )
        rows = conn.execute(
            """SELECT ts, client_order_id, side, payload_json FROM order_events
               WHERE event_type='FILL_MARKOUT'
                 AND CAST(json_extract(payload_json, '$.horizon_sec') AS INTEGER)=?
                 AND julianday(ts) >= julianday('now', ?)""",
            (args.horizon_sec, f"-{args.hours:g} hours"),
        )
        for _ts, order_id, side, raw in rows:
            payload = _payload(raw)
            try:
                signed = float(payload["signed_markout_ps"])
            except (KeyError, TypeError, ValueError):
                continue
            fill_side, liquidity, quantity = fills.get(str(order_id or ""), (str(side or ""), str(payload.get("liquidity_class") or ""), 0.0))
            key = (execution_path(str(order_id or ""), fill_side, liquidity), size_bucket(quantity))
            groups[key].append(signed)
            markets[key].add(str(payload.get("slug") or ""))

    print(f"execution path penalty report: horizon={args.horizon_sec}s lookback={args.hours:g}h")
    print("path size_bucket n independent_markets mean_signed_ps raw_adverse_ps winsor_adverse_ps p90_adverse_ps")
    for key in sorted(groups):
        stats = summarize(groups[key])
        print(
            f"{key[0]} {key[1]} {stats['n']} {len(markets[key] - {''})} "
            f"{stats['mean_signed_ps']:.6f} {stats['raw_adverse_ps']:.6f} "
            f"{stats['winsorized_adverse_ps']:.6f} {stats['p90_adverse_ps']:.6f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
