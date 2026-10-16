#!/usr/bin/env python3
"""Read-only reconstruction of prospective stop-timing records per market.

Joins ``STOP_TIMING_*`` strategy events (bot/stop_timing_telemetry.py) with
the existing core journal rows (ORDER_SUBMIT / ORDER_FILLED / ORDER_REJECTED /
ORDER_TAKER_EXIT_SUBMIT / MARKET_SETTLEMENT / MARKET_CYCLE_PNL).  Missing
values stay ``None``; nothing is inferred.

Usage: scripts/stop_timing_report.py --db logs/trade_journal.db --run-id RUN [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ORDER_TYPES = ("ORDER_SUBMIT", "ORDER_FILLED", "ORDER_REJECTED", "ORDER_TAKER_EXIT_SUBMIT", "ORDER_CANCELED")
MARKET_TYPES = ("MARKET_SETTLEMENT", "MARKET_CYCLE_PNL")


def _loads(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _snap(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    keys = ("obs_wall_ts", "best_bid", "net_if_exit", "time_left_sec", "signed_distance_bps",
            "required_move_sigma_legacy", "required_move_z_diffusion", "qty", "cross_state",
            "obs_interval_sec")
    return {key: payload.get(key) for key in keys}


def reconstruct(conn: sqlite3.Connection, run_id: str) -> dict[str, dict[str, Any]]:
    markets: dict[str, dict[str, Any]] = {}

    def market(slug: str) -> dict[str, Any]:
        return markets.setdefault(slug or "UNKNOWN", {
            "slug": slug or "UNKNOWN", "positions": {}, "orders": {}, "settlement": None,
            "cycle_pnl": None, "telemetry_rows": 0,
        })

    inst_to_slug: dict[str, str] = {}
    rows = conn.execute(
        "SELECT id, ts, event_type, payload_json FROM strategy_events WHERE run_id=? AND "
        "(event_type LIKE 'STOP_TIMING_%' OR event_type IN (?, ?)) ORDER BY id", (run_id, *MARKET_TYPES)).fetchall()
    for _id, ts, event_type, raw in rows:
        payload = _loads(raw)
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        m = market(slug)
        if event_type == "MARKET_SETTLEMENT":
            m["settlement"] = {"runtime_outcome": payload.get("outcome"), "source": payload.get("outcome_source"),
                               "margin_bps": payload.get("settlement_reference_margin_bps"), "ts": ts}
            continue
        if event_type == "MARKET_CYCLE_PNL":
            m["cycle_pnl"] = payload
            continue
        m["telemetry_rows"] += 1
        epoch_key = str(payload.get("position_epoch") or "")
        pos = m["positions"].setdefault(epoch_key, {
            "entry": None, "first": {}, "crosses": [], "recrosses": [], "checkpoints": [],
            "degraded": [], "decision_changes": [], "settlement": None})
        if payload.get("instrument_id"):
            inst_to_slug[str(payload["instrument_id"])] = slug
        kind = event_type[len("STOP_TIMING_"):]
        if kind == "POSITION_OPENED":
            pos["entry"] = {k: payload.get(k) for k in (
                "instrument_id", "held_side", "entry_fill_ts", "entry_price", "entry_qty", "entry_cost_usdc",
                "tte_at_entry_sec", "strike", "settlement_reference_twap", "settlement_reference_age_sec",
                "reference_source", "sizing_rule_version_runtime", "first_observation_lag_sec")}
        elif kind == "ADVERSE_CROSS":
            pos["crosses"].append({**_snap(payload), "cross_seq": payload.get("cross_seq"),
                                   "cross_quality": payload.get("cross_quality")})
        elif kind == "FAVORABLE_RECROSS":
            pos["recrosses"].append({**_snap(payload), "adverse_duration_sec": payload.get("adverse_duration_sec")})
        elif kind == "PERSISTENCE_CHECKPOINT":
            pos["checkpoints"].append({"cross_seq": payload.get("cross_seq"), "checkpoint_sec": payload.get("checkpoint_sec"),
                                       "still_adverse": payload.get("still_adverse"),
                                       "capture_lag_sec": payload.get("capture_lag_sec")})
        elif kind in {"COMPONENT_FIRST_TRUE", "BREAKER_FIRST_ELIGIBLE"}:
            pos["first"][str(payload.get("component"))] = _snap(payload)
        elif kind == "DEGRADED_FIRST":
            pos["degraded"].append(payload.get("degraded_reason"))
        elif kind == "DECISION_CHANGE":
            pos["decision_changes"].append({"ts": payload.get("obs_wall_ts"), "from": payload.get("previous_decision_reason"),
                                            "to": payload.get("decision_reason")})
        elif kind == "POSITION_SETTLEMENT":
            pos["settlement"] = {k: payload.get(k) for k in (
                "settlement_outcome_runtime", "counterfactual_hold_gross_pnl", "official_outcome",
                "telemetry_counters", "telemetry_disabled", "cross_count", "settlement_relabel",
                "superseded_outcome_runtime", "settlement_outcome_source")}

    orows = conn.execute(
        f"SELECT ts, event_type, client_order_id, side, price, qty, status, reason, instrument_id, payload_json "
        f"FROM order_events WHERE run_id=? AND event_type IN ({','.join('?' * len(ORDER_TYPES))}) ORDER BY id",
        (run_id, *ORDER_TYPES)).fetchall()
    for ts, event_type, coid, side, price, qty, status, reason, inst, raw in orows:
        payload = _loads(raw)
        slug = str(payload.get("slug") or inst_to_slug.get(str(inst or ""), "") or "")
        m = market(slug)
        order = m["orders"].setdefault(str(coid or ""), {"client_order_id": coid, "events": []})
        order["events"].append({"ts": ts, "type": event_type, "side": side, "price": price, "qty": qty,
                                "status": status, "reason": payload.get("decision_reason") or reason})
    for m in markets.values():
        for order in m["orders"].values():
            types = [e["type"] for e in order["events"]]
            coid = str(order["client_order_id"] or "")
            order["kind"] = ("PROTECTIVE_SELL" if "TAKER-EXIT" in coid else
                             "MAKER_SELL" if "SELL" in coid else "BUY" if "BUY" in coid else "OTHER")
            order["submitted"] = any(t in {"ORDER_SUBMIT", "ORDER_TAKER_EXIT_SUBMIT"} for t in types)
            order["filled_qty"] = sum(float(e["qty"] or 0) for e in order["events"] if e["type"] == "ORDER_FILLED")
            order["rejected"] = "ORDER_REJECTED" in types
            order["submit_count"] = sum(1 for t in types if t in {"ORDER_SUBMIT", "ORDER_TAKER_EXIT_SUBMIT"})
    return markets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    path = Path(args.db).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        print(json.dumps(reconstruct(conn, args.run_id), indent=1, sort_keys=True, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
