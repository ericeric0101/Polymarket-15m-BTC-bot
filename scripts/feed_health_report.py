#!/usr/bin/env python3
"""Read-only TWAP and Outcome WebSocket lifecycle-health report."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict


def _payload(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "max": None}
    return {"count": len(values), "mean": sum(values) / len(values), "max": max(values)}


def build_report(db_path: str) -> dict:
    twap_stalls: list[float] = []
    twap_unavailable: list[float] = []
    twap_first_tick: list[float] = []
    outcome_disconnects: list[float] = []
    errors: Counter[str] = Counter()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT event_type, payload_json FROM strategy_events
               WHERE event_type LIKE 'POLYMARKET_TWAP_%'
                  OR event_type LIKE 'HYPERLIQUID_OUTCOME_OBSERVER_%'"""
        )
        for event_type, raw in rows:
            payload = _payload(raw)
            if event_type == "POLYMARKET_TWAP_SILENT_STALL":
                try:
                    twap_stalls.append(float(payload["silence_sec"]))
                except (KeyError, TypeError, ValueError):
                    pass
            elif event_type == "POLYMARKET_TWAP_WS_RECOVERED":
                for field, target in (("feed_unavailable_sec", twap_unavailable), ("first_valid_twap_after_connect_sec", twap_first_tick)):
                    try:
                        if payload.get(field) is not None:
                            target.append(float(payload[field]))
                    except (TypeError, ValueError):
                        pass
            elif event_type == "HYPERLIQUID_OUTCOME_OBSERVER_DISCONNECTED":
                try:
                    if payload.get("retry_delay_sec") is not None:
                        outcome_disconnects.append(float(payload["retry_delay_sec"]))
                except (TypeError, ValueError):
                    pass
                errors[str(payload.get("error_type") or "unknown")] += 1
    return {
        "twap_silent_stall_sec": _summary(twap_stalls),
        "twap_feed_unavailable_sec": _summary(twap_unavailable),
        "twap_first_valid_tick_after_connect_sec": _summary(twap_first_tick),
        "outcome_reconnect_backoff_sec": _summary(outcome_disconnects),
        "outcome_disconnect_errors": dict(errors),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="logs/trade_journal.db")
    args = parser.parse_args()
    print(json.dumps(build_report(args.db), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
