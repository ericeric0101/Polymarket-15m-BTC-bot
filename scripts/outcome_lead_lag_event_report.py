#!/usr/bin/env python3
"""Read-only quality-gated report for event-driven Outcome shadow markouts.

Only records whose observed time met the configured markout deadline are
eligible.  Historical pre-v2 records lack this field and are intentionally
excluded rather than being relabelled as sub-second evidence.
"""
from __future__ import annotations

import argparse
import json
import sqlite3


def load_quality_gated_markouts(db_path: str) -> list[tuple[int, int, int, int]]:
    query = """
        WITH ranked AS (
            SELECT horizon_ms,
                   json_extract(payload_json, '$.twap_change_cents') AS change_cents,
                   json_extract(payload_json, '$.decision.direction') AS direction,
                   json_extract(payload_json, '$.observed_elapsed_ms') AS observed_elapsed_ms,
                   row_number() OVER (
                     PARTITION BY run_id, slug, coalesce(market_id, -1),
                                  candidate_epoch_ns / 1000000000,
                                  json_extract(payload_json, '$.decision.direction'), horizon_ms
                     ORDER BY candidate_epoch_ns
                   ) AS row_rank
            FROM lead_lag_markouts
            WHERE json_extract(payload_json, '$.timely') = 1
        )
        SELECT horizon_ms, change_cents, direction, observed_elapsed_ms
        FROM ranked WHERE row_rank = 1
    """
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return [tuple(map(int, row)) for row in conn.execute(query) if None not in row]


def summarize(rows: list[tuple[int, int, int, int]]) -> dict[str, dict[str, float | int]]:
    result: dict[str, dict[str, float | int]] = {}
    for horizon in sorted({row[0] for row in rows}):
        group = [row for row in rows if row[0] == horizon]
        hits = [change * direction > 0 for _, change, direction, _ in group]
        result[str(horizon)] = {
            "independent_candidate_seconds": len(group),
            "direction_hit_rate": sum(hits) / len(hits) if hits else 0.0,
            "mean_abs_twap_move_usd": sum(abs(change) for _, change, _, _ in group) / len(group) / 100 if group else 0.0,
            "mean_actual_elapsed_ms": sum(elapsed for *_, elapsed in group) / len(group) if group else 0.0,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="logs/hyperliquid_lead_lag.db")
    args = parser.parse_args()
    print(json.dumps({
        "research_question": "Do calibrated Outcome shocks precede Polymarket TWAP moves?",
        "quality_gate": "timely=true; one candidate per direction per second",
        "horizons": summarize(load_quality_gated_markouts(args.db)),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
