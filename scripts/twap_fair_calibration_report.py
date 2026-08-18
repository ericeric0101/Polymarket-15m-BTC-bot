#!/usr/bin/env python3
"""Report out-of-sample calibration of the recorded live forecast state.

Observational only.  It uses one earliest complete forecast per settled market,
so a frequently quoted market cannot dominate the result.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Iterable


BINS = ((0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01))


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def _time_bucket(time_left_sec: float) -> str:
    if time_left_sec <= 300:
        return "0-5m"
    if time_left_sec <= 600:
        return "5-10m"
    return "10-15m"


def _print_summary(label: str, rows: list[tuple[float, float | None, int, str, str, float, float]]) -> None:
    print(f"\n{label}: settled_markets={len(rows)}")
    if not rows:
        return
    model_brier = _mean((model - outcome) ** 2 for model, _market, outcome, *_ in rows)
    market_brier = _mean(
        (market - outcome) ** 2
        for _model, market, outcome, *_ in rows
        if market is not None
    )
    print(
        f"brier_model={model_brier:.5f} "
        f"brier_market={'-' if market_brier is None else f'{market_brier:.5f}'}"
    )
    print("source/model                 markets  brier    avg_model  actual_up  avg_sigma")
    groups: dict[tuple[str, str], list[tuple[float, float | None, int, str, str, float, float]]] = defaultdict(list)
    for row in rows:
        groups[(row[3], row[4])].append(row)
    for (source, model_name), group in sorted(groups.items(), key=lambda item: -len(item[1])):
        avg_model = _mean(row[0] for row in group)
        actual = _mean(float(row[2]) for row in group)
        brier = _mean((row[0] - row[2]) ** 2 for row in group)
        avg_sigma = _mean(row[5] for row in group)
        print(
            f"{source[:16]:16} {model_name[:18]:18} {len(group):>7}"
            f" {brier:>7.5f} {avg_model:>10.4f} {actual:>10.4f} {avg_sigma:>9.4f}"
        )
    print("model_up_bin  markets  actual_up  avg_model_up  calibration_error")
    for low, high in BINS:
        selected = [row for row in rows if low <= row[0] < high]
        if not selected:
            print(f"{low:.1f}-{min(high, 1.0):.1f} {0:>8} {'-':>10} {'-':>13} {'-':>18}")
            continue
        actual = _mean(float(row[2]) for row in selected)
        model = _mean(row[0] for row in selected)
        print(
            f"{low:.1f}-{min(high, 1.0):.1f} {len(selected):>8} {actual:>10.1%}"
            f" {model:>13.3f} {actual - model:>+18.3f}"
        )
    print("time_bucket  markets  brier    avg_abs_model_market_delta")
    time_groups: dict[str, list[tuple[float, float | None, int, str, str, float, float]]] = defaultdict(list)
    for row in rows:
        time_groups[_time_bucket(row[6])].append(row)
    for bucket in ("10-15m", "5-10m", "0-5m"):
        group = time_groups.get(bucket, [])
        if not group:
            continue
        brier = _mean((row[0] - row[2]) ** 2 for row in group)
        delta = _mean(abs(row[0] - row[1]) for row in group if row[1] is not None)
        delta_text = "-" if delta is None else f"{delta:.5f}"
        print(f"{bucket:11} {len(group):>7} {brier:>7.5f} {delta_text:>26}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/trade_journal.db")
    parser.add_argument("--hours", type=float, default=168.0)
    parser.add_argument("--holdout-fraction", type=float, default=0.30)
    args = parser.parse_args()
    if not 0 < args.holdout_fraction < 1:
        raise SystemExit("--holdout-fraction must be between zero and one")
    path = Path(args.db)
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    sql = """
    WITH forecasts AS (
      SELECT
        json_extract(payload_json, '$.slug') AS slug,
        json_extract(payload_json, '$.forecast_reference_source') AS source,
        json_extract(payload_json, '$.forecast_settlement_model') AS settlement_model,
        CAST(json_extract(payload_json, '$.forecast_standard_up_probability') AS REAL) AS standard_up,
        CAST(json_extract(payload_json, '$.forecast_twap_average_up_probability') AS REAL) AS twap_up,
        CAST(json_extract(payload_json, '$.forecast_sigma_final') AS REAL) AS sigma,
        CAST(json_extract(payload_json, '$.time_left_sec') AS REAL) AS time_left_sec,
        CAST(json_extract(payload_json, '$.bid_up') AS REAL) AS bid_up,
        CAST(json_extract(payload_json, '$.ask_up') AS REAL) AS ask_up,
        ROW_NUMBER() OVER (PARTITION BY json_extract(payload_json, '$.slug') ORDER BY id ASC) AS rn
      FROM strategy_events
      WHERE event_type='LIVE_SIGNAL_COMPARE'
        AND julianday(ts) >= julianday('now', ?)
        AND json_extract(payload_json, '$.forecast_settlement_model') IS NOT NULL
    ), settlements AS (
      SELECT json_extract(payload_json, '$.slug') AS slug, ts AS settlement_ts,
             json_extract(payload_json, '$.outcome') AS outcome
      FROM strategy_events
      WHERE event_type='MARKET_SETTLEMENT' AND julianday(ts) >= julianday('now', ?)
    )
    SELECT
      CASE WHEN settlement_model='twap_average_approx' THEN twap_up ELSE standard_up END,
      CASE WHEN bid_up > 0 AND ask_up > 0 THEN (bid_up + ask_up) / 2.0 ELSE NULL END,
      CASE outcome WHEN 'UP' THEN 1 ELSE 0 END,
      coalesce(source, 'unknown'), settlement_model, sigma, time_left_sec, settlement_ts
    FROM forecasts JOIN settlements USING (slug)
    WHERE rn=1 AND outcome IN ('UP', 'DOWN') AND standard_up IS NOT NULL
    ORDER BY settlement_ts ASC
    """
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        raw_rows = conn.execute(sql, (f"-{args.hours:g} hours", f"-{args.hours:g} hours")).fetchall()
    rows = [
        (
            float(model), float(market) if market is not None else None, int(outcome),
            str(source), str(model_name), float(sigma or 0), float(time_left or 0),
        )
        for model, market, outcome, source, model_name, sigma, time_left, _settlement_ts in raw_rows
        if model is not None
    ]
    split_at = max(1, int(len(rows) * (1 - args.holdout_fraction)))
    print(
        f"Forecast calibration report last={args.hours:g}h holdout={args.holdout_fraction:.0%}; "
        "one earliest complete forecast per settled market."
    )
    _print_summary("all observations", rows)
    _print_summary("out-of-sample chronological holdout", rows[split_at:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
