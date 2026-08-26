"""Reproducible D.4 evidence report; it never changes live policy."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime


WINDOWS = (12, 24, 36, 48, 168)
MARKOUT_CONTEXT_SCHEMA_VERSION = 2
SETTLEMENT_EMBARGO_SEC = 15 * 60


def _adverse(payload: dict) -> float:
    return max(0.0, -float(payload.get("signed_markout_ps") or 0.0))


def _summary(observations) -> dict[str, float | int] | None:
    """Summarize one first-maker-fill markout observation per market.

    A short markout is available before settlement, but D.4 cannot select a
    live policy until the corresponding market is settled.  Keep both counts
    visible rather than treating legacy or still-open markets as OOS evidence.
    """
    if not observations:
        return None
    values = [_adverse(payload) for _ts, payload in observations]
    ordered = sorted(values)
    cap = ordered[max(0, math.ceil(len(ordered) * 0.90) - 1)]
    return {
        "markout_sample_count": len(values),
        "settled_sample_count": sum(1 for _ts, payload in observations if payload["settled"]),
        "adverse_markout_per_share": sum(min(value, cap) for value in values) / len(values),
        "raw_mean_adverse_markout_per_share": sum(values) / len(values),
        "winsor_cap_per_share": cap,
    }


def _walk_forward_oos(
    observations,
    *,
    window_hours: int,
    min_train_samples: int,
) -> dict[str, object]:
    """Score a penalty estimate using only settled, embargoed prior markets.

    The target is the first 10-second maker-BUY markout for a later settled
    market.  The 15-minute embargo prevents a market's own outcome or an
    adjacent still-open market from entering its calibration history.
    """
    eligible = sorted(
        [(ts, payload) for ts, payload in observations if payload.get("settled")],
        key=lambda item: item[0],
    )
    evaluations = []
    for target_ts, target_payload in eligible:
        history_start = target_ts.timestamp() - (window_hours * 3600)
        history_end = target_ts.timestamp() - SETTLEMENT_EMBARGO_SEC
        history = [
            (ts, payload)
            for ts, payload in eligible
            if history_start <= ts.timestamp() < history_end
        ]
        if len(history) < min_train_samples:
            continue
        estimate = _summary(history)
        if estimate is None:
            continue
        actual = _adverse(target_payload)
        evaluations.append(
            {
                "target_ts": target_ts.isoformat(),
                "actual_adverse_markout_per_share": actual,
                "estimated_penalty_per_share": estimate["adverse_markout_per_share"],
                "training_sample_count": len(history),
                "target_is_weekend_utc": bool(target_payload.get("entry_is_weekend_utc")),
            }
        )
    if not evaluations:
        return {
            "evaluation_count": 0,
            "reason": "insufficient_embargoed_training_samples",
        }
    actuals = [float(item["actual_adverse_markout_per_share"]) for item in evaluations]
    estimates = [float(item["estimated_penalty_per_share"]) for item in evaluations]
    by_regime = {}
    for weekend in (False, True):
        items = [item for item in evaluations if item["target_is_weekend_utc"] == weekend]
        if not items:
            continue
        regime_actuals = [float(item["actual_adverse_markout_per_share"]) for item in items]
        regime_estimates = [float(item["estimated_penalty_per_share"]) for item in items]
        by_regime["weekend" if weekend else "weekday"] = {
            "evaluation_count": len(items),
            "mean_actual_adverse_markout_per_share": sum(regime_actuals) / len(items),
            "mean_estimated_penalty_per_share": sum(regime_estimates) / len(items),
        }
    return {
        "evaluation_count": len(evaluations),
        "first_target_ts": evaluations[0]["target_ts"],
        "last_target_ts": evaluations[-1]["target_ts"],
        "mean_actual_adverse_markout_per_share": sum(actuals) / len(actuals),
        "mean_estimated_penalty_per_share": sum(estimates) / len(estimates),
        "mean_signed_error_per_share": sum(
            actual - estimate for actual, estimate in zip(actuals, estimates)
        ) / len(actuals),
        "mean_absolute_error_per_share": sum(
            abs(actual - estimate) for actual, estimate in zip(actuals, estimates)
        ) / len(actuals),
        "underestimate_rate": sum(
            actual > estimate for actual, estimate in zip(actuals, estimates)
        ) / len(actuals),
        "training_sample_count_min": min(int(item["training_sample_count"]) for item in evaluations),
        "training_sample_count_max": max(int(item["training_sample_count"]) for item in evaluations),
        "by_target_regime": by_regime,
    }


def _first_per_market(observations, *, cutoff: float, horizon: int):
    """Prevent several fills/ticks in one 15-minute market becoming samples."""
    first = {}
    for ts, payload in sorted(observations, key=lambda item: item[0]):
        slug = str(payload.get("slug") or "")
        if not slug or ts.timestamp() < cutoff or int(payload.get("horizon_sec") or 0) != horizon:
            continue
        first.setdefault(slug, (ts, payload))
    return list(first.values())


def _settled_slugs(conn: sqlite3.Connection, slugs: set[str]) -> set[str]:
    """Return markets with a journaled settlement after a D.4 candidate fill.

    Settlement payloads are JSON and the historical journal has no slug index,
    so filter the small result set in Python.  This remains read-only and avoids
    relying on an undocumented payload shape beyond the existing ``slug`` key.
    """
    if not slugs:
        return set()
    rows = conn.execute(
        "select payload_json from strategy_events where event_type='MARKET_SETTLEMENT'"
    ).fetchall()
    settled = set()
    for (raw,) in rows:
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            continue
        slug = str(payload.get("slug") or "") if isinstance(payload, dict) else ""
        if slug in slugs:
            settled.add(slug)
    return settled


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="./logs/trade_journal.db")
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument(
        "--oos-min-samples",
        type=int,
        default=30,
        help="Minimum independent walk-forward evaluations before a policy can be reviewed.",
    )
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute(
        "select ts, payload_json from order_events where event_type='FILL_MARKOUT' and side='BUY'"
    ).fetchall()
    observations = []
    for ts, raw in rows:
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            continue
        if (
            payload.get("liquidity_class") != "maker"
            or int(payload.get("markout_context_schema_version") or 0) != MARKOUT_CONTEXT_SCHEMA_VERSION
            or int(payload.get("horizon_sec") or 0) not in (10, 30)
        ):
            continue
        observations.append((datetime.fromisoformat(ts), payload))
    if not observations:
        print(json.dumps({"status": "no_maker_buy_markouts"}, indent=2))
        return 1
    settled_slugs = _settled_slugs(conn, {str(payload.get("slug") or "") for _ts, payload in observations})
    observations = [
        (ts, {**payload, "settled": str(payload.get("slug") or "") in settled_slugs})
        for ts, payload in observations
    ]
    latest = max(ts for ts, _ in observations)
    report = {
        "markout_context_schema_version": MARKOUT_CONTEXT_SCHEMA_VERSION,
        "latest_observation": latest.isoformat(),
        "candidate_windows": {},
        "weekday_weekend": {},
        "walk_forward_oos": {},
    }
    for hours in WINDOWS:
        cutoff = latest.timestamp() - hours * 3600
        report["candidate_windows"][str(hours)] = {
            str(horizon): _summary(_first_per_market(observations, cutoff=cutoff, horizon=horizon))
            for horizon in (10, 30)
        }
    for weekend in (False, True):
        regime_observations = [
            (ts, payload)
            for ts, payload in _first_per_market(observations, cutoff=float("-inf"), horizon=10)
            if bool(payload.get("entry_is_weekend_utc")) == weekend
        ]
        report["weekday_weekend"]["weekend" if weekend else "weekday"] = _summary(regime_observations)
    primary_observations = _first_per_market(
        observations,
        cutoff=float("-inf"),
        horizon=10,
    )
    for hours in WINDOWS:
        report["walk_forward_oos"][str(hours)] = _walk_forward_oos(
            primary_observations,
            window_hours=hours,
            min_train_samples=args.min_samples,
        )
    viable = [
        hours
        for hours in (12, 24, 36, 48)
        if (report["candidate_windows"][str(hours)]["10"] or {}).get("settled_sample_count", 0)
        >= args.min_samples
    ]
    oos_viable = [
        hours
        for hours in viable
        if int(report["walk_forward_oos"][str(hours)].get("evaluation_count", 0))
        >= args.oos_min_samples
    ]
    if not viable:
        reason = "insufficient_schema_v2_settled_samples"
    elif not oos_viable:
        reason = "insufficient_out_of_sample_evaluations"
    else:
        reason = "requires_operator_review"
    report["selection"] = {
        "selected_window_hours": None,
        "reason": reason,
        "minimum_samples": args.min_samples,
        "minimum_oos_evaluations": args.oos_min_samples,
        "eligible_candidates": viable,
        "oos_eligible_candidates": oos_viable,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
