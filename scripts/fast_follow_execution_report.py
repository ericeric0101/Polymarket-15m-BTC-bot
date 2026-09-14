#!/usr/bin/env python3
"""Read-only fast-follow FOK outcome, price, and latency report."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime


PREFIX = "BTC-15M-FAST-FOLLOW-BUY-"


def _payload(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _ts(value: str) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def rejection_class(reason: str) -> str:
    lowered = str(reason or "").lower()
    if "fully filled or killed" in lowered or "fok" in lowered or "couldn't be fully filled" in lowered:
        return "fok_unfilled"
    if any(word in lowered for word in ("precision", "decimal", "maker amount", "taker amount", "invalid amount")):
        return "amount_precision"
    return "other_rejection"


def percentile(values: list[float], point: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * point))]


def build_report(db_path: str) -> dict[str, dict]:
    submissions: dict[str, dict] = {}
    outcomes: dict[str, list[dict]] = defaultdict(list)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT ts, event_type, client_order_id, price, qty, reason, payload_json
               FROM order_events
               WHERE client_order_id LIKE ?
                 AND event_type IN ('ORDER_FAST_FOLLOW_SUBMIT', 'ORDER_FILLED', 'ORDER_REJECTED', 'ORDER_DENIED')
               ORDER BY id""",
            (f"{PREFIX}%",),
        )
        for ts, event_type, order_id, price, qty, reason, raw in rows:
            order_id = str(order_id or "")
            if event_type == "ORDER_FAST_FOLLOW_SUBMIT":
                submissions[order_id] = {"ts": _ts(ts), "limit": float(price or 0), "qty": float(qty or 0), **_payload(raw)}
                continue
            submit = submissions.get(order_id, {})
            latency_ms = None
            if submit.get("ts") is not None and _ts(ts) is not None:
                latency_ms = max(0.0, (_ts(ts) - submit["ts"]) * 1000)
            if event_type == "ORDER_FILLED":
                outcomes["filled"].append({
                    "latency_ms": latency_ms, "fill_price": float(price or 0),
                    "limit_price": float(submit.get("limit") or 0), "qty": float(qty or 0),
                })
            else:
                outcomes[rejection_class(str(reason or ""))].append({"latency_ms": latency_ms})
    result: dict[str, dict] = {}
    for name, rows in sorted(outcomes.items()):
        latencies = [row["latency_ms"] for row in rows if row.get("latency_ms") is not None]
        fills = [row for row in rows if row.get("fill_price") is not None]
        result[name] = {
            "count": len(rows),
            "latency_p50_ms": percentile(latencies, 0.50),
            "latency_p95_ms": percentile(latencies, 0.95),
            "mean_fill_price": (sum(row["fill_price"] for row in fills) / len(fills)) if fills else None,
            "mean_fill_minus_limit": (
                sum(row["fill_price"] - row["limit_price"] for row in fills) / len(fills)
            ) if fills else None,
            "mean_fill_qty": (sum(row["qty"] for row in fills) / len(fills)) if fills else None,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="logs/trade_journal.db")
    args = parser.parse_args()
    print(json.dumps(build_report(args.db), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
