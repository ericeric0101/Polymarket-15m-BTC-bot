#!/usr/bin/env python3
"""Summarise counterfactual large-order L2 samples from the trade journal."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def build_report(db_path: str) -> list[dict[str, float | int | None]]:
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT event_type, payload_json FROM order_events "
                "WHERE event_type IN ('DEPTH_RISK_SHADOW_CANDIDATE', 'DEPTH_RISK_SHADOW_MARKOUT')"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()
    grouped: dict[tuple[float, int], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    candidate_ids: dict[str, float] = {}
    for event_type, raw_payload in rows:
        payload = json.loads(raw_payload or "{}")
        requested = payload.get("requested_quantity")
        if requested is None:
            continue
        requested = float(requested)
        if event_type == "DEPTH_RISK_SHADOW_CANDIDATE":
            candidate_ids[str(payload.get("simulation_id"))] = requested
            entry = payload.get("entry") or {}
            exit_ = payload.get("exit") or {}
            key = (requested, -1)
            grouped[key]["fill_rate"].append(float(entry.get("fill_rate") or 0))
            if entry.get("slippage_per_share") is not None:
                grouped[key]["slippage"].append(float(entry["slippage_per_share"]))
            grouped[key]["exit_fill_rate"].append(float(exit_.get("fill_rate") or 0))
            if payload.get("immediate_round_trip_markout_usdc") is not None:
                grouped[key]["immediate_markout"].append(float(payload["immediate_round_trip_markout_usdc"]))
        else:
            horizon = int(payload.get("markout_horizon_sec") or 0)
            key = (requested, horizon)
            if payload.get("markout_usdc") is not None:
                grouped[key]["markout"].append(float(payload["markout_usdc"]))
    report: list[dict[str, float | int | None]] = []
    for (requested, horizon), metrics in sorted(grouped.items()):
        report.append({
            "shares": requested,
            "horizon_sec": None if horizon < 0 else horizon,
            "samples": len(metrics.get("fill_rate", metrics.get("markout", []))),
            "mean_entry_fill_rate": (
                sum(metrics["fill_rate"]) / len(metrics["fill_rate"])
                if metrics.get("fill_rate") else None
            ),
            "median_entry_slippage_ps": _median(metrics.get("slippage", [])),
            "mean_exit_fill_rate": (
                sum(metrics["exit_fill_rate"]) / len(metrics["exit_fill_rate"])
                if metrics.get("exit_fill_rate") else None
            ),
            "mean_immediate_round_trip_markout_usdc": (
                sum(metrics["immediate_markout"]) / len(metrics["immediate_markout"])
                if metrics.get("immediate_markout") else None
            ),
            "mean_bbo_markout_usdc": (
                sum(metrics["markout"]) / len(metrics["markout"])
                if metrics.get("markout") else None
            ),
        })
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="logs/trade_journal.db")
    args = parser.parse_args()
    print(json.dumps(build_report(args.db), indent=2))


if __name__ == "__main__":
    main()
