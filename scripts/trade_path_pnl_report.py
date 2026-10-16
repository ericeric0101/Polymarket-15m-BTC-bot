#!/usr/bin/env python3
"""Read-only PnL and entry-latency attribution by maker/Outcome path."""
from __future__ import annotations

import argparse
import json
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from urllib.parse import quote


def _payload(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _epoch(ts: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _path(side: str, order_id: str, payload: dict) -> str:
    source = str(payload.get("entry_source") or "").lower()
    if source == "outcome_fast_follow" or str(order_id).startswith("BTC-15M-FAST-FOLLOW-BUY-"):
        return "outcome_fast_follow"
    if str(side).upper() == "BUY" and (
        source == "normal_maker" or str(order_id).startswith("BTC-15M-MAKER-BUY-")
    ):
        return "normal_maker"
    return "unknown"


def _mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def build_report(db_path: str, *, hours: float = 0.0) -> dict:
    """Summarize realized sell PnL and timing for each identified BUY path."""
    path = Path(db_path).expanduser().resolve()
    uri = f"file:{quote(str(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        cutoff = ""
        params: tuple = ()
        if hours > 0:
            cutoff = " AND julianday(ts) >= julianday('now', ?)"
            params = (f"-{float(hours):g} hours",)
        events = conn.execute(
            """SELECT id, ts, event_type, client_order_id, side, price, qty,
                      payload_json, instrument_id
               FROM order_events
               WHERE event_type IN (
                 'ORDER_MAKER_INTENT', 'ORDER_FAST_FOLLOW_INTENT', 'ORDER_SUBMIT',
                 'ORDER_FAST_FOLLOW_SUBMIT', 'ORDER_FILLED'
               )""" + cutoff + " ORDER BY id",
            params,
        ).fetchall()

        allowed_trace_times: dict[tuple[str, str], list[float]] = defaultdict(list)
        try:
            traces = conn.execute(
                "SELECT ts, payload_json FROM strategy_events "
                "WHERE event_type='ENTRY_DECISION_TRACE'" + cutoff + " ORDER BY id",
                params,
            ).fetchall()
        except sqlite3.OperationalError:
            traces = []
        for row in traces:
            payload = _payload(row["payload_json"])
            if payload.get("state") != "ALLOW":
                continue
            ts = _epoch(row["ts"])
            if ts is not None:
                allowed_trace_times[(str(payload.get("slug") or ""), str(payload.get("instrument_id") or ""))].append(ts)

        latest_intent: dict[str, float] = {}
        latest_submit: dict[str, float] = {}
        position_path: dict[tuple[str, str], str] = {}
        totals: dict[str, dict] = defaultdict(lambda: {
            "markets": set(), "filled_buy_events": 0, "filled_buy_qty": 0.0,
            "buy_notional_usdc": 0.0, "sell_fill_events": 0,
            "realized_net_usdc": 0.0, "entry_market_age_sec": [],
            "submit_to_fill_sec": [], "allow_to_intent_sec": [],
            "signal_to_intent_ms": [], "intent_to_submit_sec": [],
        })

        for row in events:
            order_id = str(row["client_order_id"] or "")
            event_type = str(row["event_type"])
            ts = _epoch(row["ts"])
            if ts is None:
                continue
            payload = _payload(row["payload_json"])
            slug = str(payload.get("slug") or payload.get("market_slug") or "")
            instrument = str(row["instrument_id"] or payload.get("instrument_id") or "")
            position_key = (slug, instrument)
            if event_type in {"ORDER_MAKER_INTENT", "ORDER_FAST_FOLLOW_INTENT"}:
                latest_intent[order_id] = ts
                route = "outcome_fast_follow" if event_type == "ORDER_FAST_FOLLOW_INTENT" else "normal_maker"
                if event_type == "ORDER_FAST_FOLLOW_INTENT":
                    try:
                        totals[route]["signal_to_intent_ms"].append(float(payload["signal_age_ms"]))
                    except (KeyError, TypeError, ValueError):
                        pass
                elif slug and instrument:
                    allow_times = allowed_trace_times.get(position_key, [])
                    index = bisect_right(allow_times, ts) - 1
                    if index >= 0:
                        totals[route]["allow_to_intent_sec"].append(max(0.0, ts - allow_times[index]))
                continue
            if event_type in {"ORDER_SUBMIT", "ORDER_FAST_FOLLOW_SUBMIT"}:
                latest_submit[order_id] = ts
                intent_ts = latest_intent.get(order_id)
                if intent_ts is not None:
                    route = "outcome_fast_follow" if event_type == "ORDER_FAST_FOLLOW_SUBMIT" else "normal_maker"
                    totals[route]["intent_to_submit_sec"].append(max(0.0, ts - intent_ts))
                continue
            if event_type != "ORDER_FILLED":
                continue
            side = str(row["side"] or "").upper()
            route = _path(side, order_id, payload)
            if side == "BUY":
                if route == "unknown":
                    continue
                if slug and instrument:
                    position_path[position_key] = route
                data = totals[route]
                data["filled_buy_events"] += 1
                quantity = float(row["qty"] or 0.0)
                price = float(row["price"] or 0.0)
                data["filled_buy_qty"] += quantity
                data["buy_notional_usdc"] += quantity * price
                if slug:
                    data["markets"].add(slug)
                    try:
                        market_start = float(slug.rsplit("-", 1)[-1])
                        data["entry_market_age_sec"].append(max(0.0, ts - market_start))
                    except ValueError:
                        pass
                submit_ts = latest_submit.get(order_id)
                intent_ts = latest_intent.get(order_id)
                handoff_ts = submit_ts if submit_ts is not None else intent_ts
                if handoff_ts is not None:
                    data["submit_to_fill_sec"].append(max(0.0, ts - handoff_ts))
            elif side == "SELL":
                route = position_path.get(position_key, "unknown")
                if route == "unknown":
                    continue
                data = totals[route]
                data["sell_fill_events"] += 1
                data["realized_net_usdc"] += float(
                    _payload(row["payload_json"]).get("realized_net_usdc") or 0.0
                )

        by_path = {}
        for route, data in totals.items():
            by_path[route] = {
                "markets": len(data["markets"]),
                "filled_buy_events": data["filled_buy_events"],
                "filled_buy_qty": data["filled_buy_qty"],
                "buy_notional_usdc": data["buy_notional_usdc"],
                "sell_fill_events": data["sell_fill_events"],
                "realized_net_usdc": data["realized_net_usdc"],
                "mean_entry_market_age_sec": _mean(data["entry_market_age_sec"]),
                "mean_submit_to_fill_sec": _mean(data["submit_to_fill_sec"]),
                "mean_allow_to_intent_sec": _mean(data["allow_to_intent_sec"]),
                "mean_signal_to_intent_ms": _mean(data["signal_to_intent_ms"]),
                "mean_intent_to_submit_sec": _mean(data["intent_to_submit_sec"]),
            }
        return {"hours": float(hours), "by_path": by_path}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/trade_journal.db")
    parser.add_argument("--hours", type=float, default=24.0, help="lookback hours; 0 means all history")
    args = parser.parse_args()
    print(json.dumps(build_report(args.db, hours=args.hours), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
