#!/usr/bin/env python3
"""Evaluate raw, market-midpoint, and learned probability weights offline.

This is Phase D research only. It uses schema-v2, authoritative-strike
``LIVE_SIGNAL_COMPARE`` events and never changes the live calibration weight.
Candidate settlement PnL assumes a one-share filled entry and excludes fees,
fill probability, execution cost, exits, and slippage.
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ForecastRow:
    slug: str
    raw_up: float
    market_up: float
    outcome_up: int
    active_side: str
    ask_up: float | None
    ask_down: float | None


def calibrated_up(raw_up: float, market_up: float, weight: float) -> float:
    return max(0.001, min(0.999, market_up + weight * (raw_up - market_up)))


def fit_brier_weight(rows: Iterable[ForecastRow]) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        delta = row.raw_up - row.market_up
        numerator += delta * (row.outcome_up - row.market_up)
        denominator += delta * delta
    if denominator <= 0.0:
        return None
    return max(0.0, min(1.0, numerator / denominator))


def brier_score(rows: Iterable[ForecastRow], weight: float) -> float | None:
    values = [
        (calibrated_up(row.raw_up, row.market_up, weight) - row.outcome_up) ** 2
        for row in rows
    ]
    return sum(values) / len(values) if values else None


def probability_shadow_settlement(rows: Iterable[ForecastRow], weight: float) -> tuple[int, float]:
    """Return direction-selected, positive settlement-EV candidates and realized PnL."""
    candidates = 0
    pnl = 0.0
    for row in rows:
        side = row.active_side.upper()
        probability_up = calibrated_up(row.raw_up, row.market_up, weight)
        if side == "UP" and row.ask_up is not None and probability_up > row.ask_up:
            candidates += 1
            pnl += (1.0 - row.ask_up) if row.outcome_up else -row.ask_up
        elif side == "DOWN" and row.ask_down is not None and (1.0 - probability_up) > row.ask_down:
            candidates += 1
            pnl += -row.ask_down if row.outcome_up else (1.0 - row.ask_down)
    return candidates, pnl


def _load_rows(path: Path, hours: float, min_time_left: float, max_time_left: float) -> list[ForecastRow]:
    sql = """
    WITH forecast_events AS (
      SELECT
        id,
        json_extract(payload_json, '$.slug') AS slug,
        CAST(CASE
          WHEN json_extract(payload_json, '$.forecast_settlement_model')='twap_average_approx'
          THEN json_extract(payload_json, '$.forecast_twap_average_up_probability')
          ELSE json_extract(payload_json, '$.forecast_standard_up_probability')
        END AS REAL) AS raw_up,
        CAST(json_extract(payload_json, '$.bid_up') AS REAL) AS bid_up,
        CAST(json_extract(payload_json, '$.ask_up') AS REAL) AS ask_up,
        CAST(json_extract(payload_json, '$.ask_down') AS REAL) AS ask_down,
        json_extract(payload_json, '$.active_side') AS active_side,
        CAST(json_extract(payload_json, '$.time_left_sec') AS REAL) AS time_left_sec
      FROM strategy_events
      WHERE event_type='LIVE_SIGNAL_COMPARE'
        AND julianday(ts) >= julianday('now', ?)
        AND json_extract(payload_json, '$.forecast_schema_version')=2
        AND json_extract(payload_json, '$.forecast_strike_authoritative')=1
    ), forecasts AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY slug ORDER BY id ASC) AS market_rn
      FROM forecast_events
      WHERE time_left_sec BETWEEN ? AND ?
    ), settlements AS (
      SELECT json_extract(payload_json, '$.slug') AS slug,
             json_extract(payload_json, '$.outcome') AS outcome,
             ts AS settlement_ts
      FROM strategy_events
      WHERE event_type='MARKET_SETTLEMENT'
        AND julianday(ts) >= julianday('now', ?)
    )
    SELECT slug, raw_up, (bid_up + ask_up) / 2.0, outcome, active_side, ask_up, ask_down
    FROM forecasts JOIN settlements USING (slug)
    WHERE market_rn=1
      AND raw_up IS NOT NULL
      AND bid_up > 0 AND ask_up > 0
      AND outcome IN ('UP', 'DOWN')
    ORDER BY settlement_ts ASC
    """
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        records = conn.execute(
            sql,
            (
                f"-{hours:g} hours",
                min_time_left,
                max_time_left,
                f"-{hours:g} hours",
            ),
        ).fetchall()
    return [
        ForecastRow(
            slug=str(slug), raw_up=float(raw_up), market_up=float(market_up),
            outcome_up=1 if outcome == "UP" else 0, active_side=str(active_side or ""),
            ask_up=float(ask_up) if ask_up is not None else None,
            ask_down=float(ask_down) if ask_down is not None else None,
        )
        for slug, raw_up, market_up, outcome, active_side, ask_up, ask_down in records
    ]


def _format_score(value: float | None) -> str:
    return "-" if value is None else f"{value:.5f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/trade_journal.db")
    parser.add_argument("--hours", type=float, default=168.0)
    parser.add_argument("--holdout-fraction", type=float, default=0.30)
    parser.add_argument("--min-train-markets", type=int, default=30)
    parser.add_argument("--min-time-left-sec", type=float, default=60.0)
    parser.add_argument("--max-time-left-sec", type=float, default=720.0)
    args = parser.parse_args()
    if not 0.0 < args.holdout_fraction < 1.0:
        raise SystemExit("--holdout-fraction must be between zero and one")
    path = Path(args.db)
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    rows = _load_rows(path, args.hours, args.min_time_left_sec, args.max_time_left_sec)
    split_at = max(1, int(len(rows) * (1.0 - args.holdout_fraction)))
    train, holdout = rows[:split_at], rows[split_at:]
    print(
        "Calibration shadow report "
        f"last={args.hours:g}h authoritative_schema_v2_only "
        f"time_window={args.min_time_left_sec:g}-{args.max_time_left_sec:g}s"
    )
    print(f"settled={len(rows)} train={len(train)} holdout={len(holdout)}")
    if len(train) < args.min_train_markets:
        print(
            f"INSUFFICIENT_SAMPLE: need train>={args.min_train_markets}; "
            "no learned weight is proposed."
        )
        return 0

    learned = fit_brier_weight(train)
    if learned is None:
        print("INSUFFICIENT_VARIATION: no learned weight is proposed.")
        return 0
    print("model                 weight  train_brier  oos_brier  candidates  settlement_pnl_1share")
    for label, weight in (("market_mid", 0.0), ("raw_model", 1.0), ("learned_oos", learned)):
        candidate_count, settlement_pnl = probability_shadow_settlement(holdout, weight)
        print(
            f"{label:20} {weight:>6.3f} {_format_score(brier_score(train, weight)):>11}"
            f" {_format_score(brier_score(holdout, weight)):>10} {candidate_count:>11}"
            f" {settlement_pnl:>24.4f}"
        )
    print("Settlement PnL is observational: one-share, assumed fills, pre-fee/pre-slippage/pre-exit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
