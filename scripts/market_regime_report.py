"""Reproducible D.4 evidence report; it never changes live policy."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime


WINDOWS = (12, 24, 36, 48, 168)


def _adverse(payload: dict) -> float:
    return max(0.0, -float(payload.get("signed_markout_ps") or 0.0))


def _summary(values: list[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    ordered = sorted(values)
    cap = ordered[max(0, math.ceil(len(ordered) * 0.90) - 1)]
    return {
        "sample_count": len(values),
        "adverse_markout_per_share": sum(min(value, cap) for value in values) / len(values),
        "raw_mean_adverse_markout_per_share": sum(values) / len(values),
        "winsor_cap_per_share": cap,
    }


def _first_per_market(observations, *, cutoff: float, horizon: int):
    """Prevent several fills/ticks in one 15-minute market becoming samples."""
    first = {}
    for ts, payload in observations:
        slug = str(payload.get("slug") or "")
        if not slug or ts.timestamp() < cutoff or int(payload.get("horizon_sec") or 0) != horizon:
            continue
        first.setdefault(slug, (ts, payload))
    return list(first.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="./logs/trade_journal.db")
    parser.add_argument("--min-samples", type=int, default=30)
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute(
        "select ts, payload_json from order_events where event_type='FILL_MARKOUT' and side='BUY'"
    ).fetchall()
    observations = []
    for ts, raw in rows:
        payload = json.loads(raw or "{}")
        if payload.get("liquidity_class") != "maker" or int(payload.get("horizon_sec") or 0) not in (10, 30):
            continue
        observations.append((datetime.fromisoformat(ts), payload))
    if not observations:
        print(json.dumps({"status": "no_maker_buy_markouts"}, indent=2))
        return 1
    latest = max(ts for ts, _ in observations)
    report = {"latest_observation": latest.isoformat(), "candidate_windows": {}, "weekday_weekend": {}}
    for hours in WINDOWS:
        cutoff = latest.timestamp() - hours * 3600
        report["candidate_windows"][str(hours)] = {
            str(horizon): _summary([_adverse(p) for _ts, p in _first_per_market(observations, cutoff=cutoff, horizon=horizon)])
            for horizon in (10, 30)
        }
    for weekend in (False, True):
        values = [
            _adverse(p)
            for ts, p in _first_per_market(observations, cutoff=float("-inf"), horizon=10)
            if (ts.weekday() >= 5) == weekend
        ]
        report["weekday_weekend"]["weekend" if weekend else "weekday"] = _summary(values)
    viable = [hours for hours in (12, 24, 36, 48) if (report["candidate_windows"][str(hours)]["10"] or {}).get("sample_count", 0) >= args.min_samples]
    report["selection"] = {
        "selected_window_hours": None,
        "reason": "insufficient_out_of_sample_samples" if not viable else "requires_out_of_sample_review",
        "minimum_samples": args.min_samples,
        "eligible_candidates": viable,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
