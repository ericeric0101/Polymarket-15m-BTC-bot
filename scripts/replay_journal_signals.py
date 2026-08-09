#!/usr/bin/env python3
"""Replay recorded candidates against recorded settlements without live API calls."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.journal_replay import (
    candidate_from_payload,
    dry_run_fill_from_payload,
    replay_candidates,
    select_one_candidate_per_market,
)


def _load_payload(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/trade_journal.db")
    parser.add_argument("--hours", type=float, default=0.0, help="0 means all retained data")
    parser.add_argument("--selection", choices=("first", "last"), default="first")
    parser.add_argument(
        "--shares",
        type=float,
        default=1.0,
        help="fallback quantity for signal-baseline candidates; dry-run-fill uses each recorded fill quantity",
    )
    parser.add_argument(
        "--mode",
        choices=("dry-run-fill", "signal-baseline"),
        default="dry-run-fill",
        help="dry-run-fill matches live gates plus simulated maker fills; signal-baseline is diagnostic only",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")
    where = ""
    params: tuple[object, ...] = ()
    if args.hours > 0:
        where = " AND julianday(ts) >= julianday('now', ?)"
        params = (f"-{args.hours} hours",)

    with sqlite3.connect(db_path) as conn:
        if args.mode == "dry-run-fill":
            simulation_rows = conn.execute(
                "SELECT event_type, ts, payload_json, side, price, qty FROM order_events "
                "WHERE event_type IN ('SHADOW_SIM_ENTRY_FILLED', 'SHADOW_SIM_SETTLED')"
                + where
                + " ORDER BY ts",
                params,
            ).fetchall()
        else:
            candidate_rows = conn.execute(
                "SELECT ts, payload_json FROM strategy_events "
                "WHERE event_type='SHADOW_SIGNAL_CANDIDATE_LIVE'" + where + " ORDER BY ts",
                params,
            ).fetchall()
        settlement_rows = conn.execute(
            "SELECT payload_json FROM strategy_events "
            "WHERE event_type='MARKET_SETTLEMENT'" + where,
            params,
        ).fetchall()

    if args.mode == "dry-run-fill":
        candidates = []
        outcomes = {}
        for event_type, ts, raw, side, price, qty in simulation_rows:
            payload = _load_payload(raw)
            candidate = dry_run_fill_from_payload(
                ts,
                payload,
                side=side,
                price=price,
                qty=qty,
            )
            if candidate is None:
                continue
            if event_type == "SHADOW_SIM_ENTRY_FILLED":
                candidates.append(candidate)
            else:
                outcome = str(payload.get("outcome") or "").upper()
                if outcome in {"UP", "DOWN"}:
                    outcomes[candidate.slug] = outcome
        label = "Dry-run executable replay (live gates, live sizing + simulated passive fills; settlement only; excludes fees/exits)"
    else:
        candidates = [
            candidate
            for ts, raw in candidate_rows
            if (candidate := candidate_from_payload(ts, _load_payload(raw)))
        ]
        outcomes = {
            str(payload.get("slug")): str(payload.get("outcome"))
            for (raw,) in settlement_rows
            if (payload := _load_payload(raw)).get("slug") and payload.get("outcome")
        }
        label = "Signal baseline replay (not live-gated; settlement only; excludes fees/fills/slippage)"
    selected = select_one_candidate_per_market(candidates, selection=args.selection)
    results = replay_candidates(selected, outcomes, default_qty=args.shares)
    wins = sum(result.won for result in results)
    gross_pnl = sum(result.pnl for result in results)

    print(label)
    print(f"mode={args.mode}")
    if args.mode == "dry-run-fill":
        unscored = len({candidate.slug for candidate in selected} - set(outcomes))
        print(
            f"filled_candidates={len(candidates)} selected_markets={len(selected)} "
            f"settled={len(results)} unscored={unscored}"
        )
    else:
        print(f"candidates={len(candidates)} selected_markets={len(selected)} settled={len(results)}")
    print(f"wins={wins} losses={len(results) - wins} win_rate={(wins / len(results)) if results else 0:.2%}")
    if args.mode == "dry-run-fill":
        print(f"gross_pnl_using_recorded_fill_quantities={gross_pnl:+.4f}")
    else:
        print(f"gross_pnl_for_{args.shares:g}_shares_per_market={gross_pnl:+.4f}")
    for result in results:
        print(
            f"{result.ts} {result.slug} {result.side} entry={result.entry_price:.3f} "
            f"qty={result.qty:.3f} outcome={result.outcome} pnl={result.pnl:+.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
