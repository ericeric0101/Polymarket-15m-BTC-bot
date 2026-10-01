#!/usr/bin/env python3
"""Offline empirical BTC probability research; never imported by live runtime."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
import sqlite3
from collections import deque
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


CHECKPOINTS = (120, 60, 30, 15, 10, 5)
HORIZON_SECONDS = {120: 120, 60: 60, 30: None, 15: None, 10: None, 5: None}
BUCKETS = ((0, .10), (.10, .25), (.25, .40), (.40, .60), (.60, .75),
           (.75, .90), (.90, .95), (.95, .975), (.975, 1.0000001))
MIN_EMPIRICAL_SAMPLES = 100
MIN_VOL_BUCKET_SAMPLES = 100
ROLLING_DAYS = 56
EPS = 1e-12


def market_quote_is_fresh(row: dict) -> bool:
    """Require explicit source/receive ages within the recorded quote limit."""
    try:
        max_age = float(row["market_bbo_max_age_sec"])
        source_age = float(row["market_bbo_up_source_age_sec"])
        received_age = float(row["market_bbo_up_received_age_sec"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0.0 <= source_age <= max_age and 0.0 <= received_age <= max_age


def empirical_cdf_probability(returns_bps: Iterable[float], required_move_bps: float,
                              current_side: str) -> float | None:
    """Estimate terminal-UP proxy probability from a signed required move.

    A positive move is the UP boundary crossing tail; a negative move is the
    DOWN boundary crossing tail. ``current_side`` disambiguates which tail is
    the winning side. This is a strike-proxy approximation, not an official
    60-second settlement-path simulator.
    """
    values = list(returns_bps)
    if not values or current_side not in {"UP", "DOWN"}:
        return None
    if required_move_bps > 0:
        p_up = sum(value >= required_move_bps for value in values) / len(values)
        return p_up
    if required_move_bps < 0:
        p_down = sum(value <= required_move_bps for value in values) / len(values)
        return 1.0 - p_down
    return 1.0 if current_side == "UP" else 0.0


def horizon_seconds(checkpoint_sec: int) -> int | None:
    return HORIZON_SECONDS.get(int(checkpoint_sec))


def rolling_history(samples: list[dict], evaluation_ts: float, days: int = ROLLING_DAYS) -> list[dict]:
    lower = evaluation_ts - days * 86400
    return [sample for sample in samples if lower <= sample["ts"] < evaluation_ts]


def market_cluster_bootstrap(paired_rows: list[dict], *, reps: int = 2000,
                             seed: int = 17) -> dict:
    """Bootstrap paired mean deltas by market slug, not individual checkpoint."""
    import random
    clusters: dict[str, list[float]] = defaultdict(list)
    for row in paired_rows:
        value = row.get("delta")
        if row.get("market_slug") and value is not None and math.isfinite(float(value)):
            clusters[str(row["market_slug"])].append(float(value))
    if not clusters:
        return {"n_markets": 0, "mean_delta": None, "median_market_delta": None,
                "ci_low": None, "ci_high": None}
    per_market = {slug: statistics.mean(values) for slug, values in clusters.items()}
    vals = list(per_market.values())
    rng = random.Random(seed)
    boots = [statistics.mean(vals[rng.randrange(len(vals))] for _ in vals) for _ in range(reps)]
    boots.sort()
    return {"n_markets": len(vals), "mean_delta": statistics.mean(vals),
            "median_market_delta": statistics.median(vals),
            "ci_low": boots[int(.025 * (reps - 1))], "ci_high": boots[int(.975 * (reps - 1))]}


def score_probability(p: float, outcome_up: int) -> tuple[float, float, int]:
    p = min(1 - EPS, max(EPS, float(p)))
    return (p - outcome_up) ** 2, -(outcome_up * math.log(p) + (1 - outcome_up) * math.log(1 - p)), int((p >= .5) == bool(outcome_up))


def calibration_error(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    total = len(pairs)
    error = 0.0
    # Equal-width 10-bin ECE, computed independently for each horizon/model.
    for idx in range(10):
        low, high = idx / 10, (idx + 1) / 10
        group = [(p, y) for p, y in pairs if low <= p < high or (idx == 9 and p == 1)]
        if group:
            error += len(group) / total * abs(statistics.mean(p for p, _ in group) - statistics.mean(y for _, y in group))
    return error


def load_candles(cache_dir: Path) -> list[dict]:
    rows: dict[int, dict] = {}
    for path in sorted(cache_dir.glob("binance_btcusdt_1m_*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for row in json.load(f):
                try:
                    ts = int(row[0]) / 1000
                    rows[int(ts)] = {"ts": ts, "close_ts": int(row[6]) / 1000,
                                     "close": float(row[4]), "high": float(row[2]),
                                     "low": float(row[3])}
                except (IndexError, TypeError, ValueError):
                    continue
    return [rows[t] for t in sorted(rows)]


def make_forward_samples(candles: list[dict], horizon: int) -> list[dict]:
    """Minute-close returns; source and target are exactly N minute closes apart."""
    closes = {int(round(row["close_ts"])): row["close"] for row in candles}
    times = sorted(closes)
    output = []
    trailing = deque(maxlen=30)
    previous_ts = None
    for ts in times:
        if previous_ts is not None and ts - previous_ts == 60 and closes[previous_ts] > 0:
            trailing.append(math.log(closes[ts] / closes[previous_ts]))
        else:
            trailing.clear()
        future = ts + horizon
        if future not in closes or closes[ts] <= 0:
            previous_ts = ts
            continue
        move = (closes[future] / closes[ts] - 1) * 10000
        vol = statistics.pstdev(trailing) * math.sqrt(365 * 24 * 60) if len(trailing) >= 20 else None
        output.append({"ts": float(ts), "return_bps": move, "vol_proxy": vol})
        previous_ts = ts
    return output


def load_events(db_path: Path) -> list[dict]:
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT decision_epoch_ns,payload_json FROM lead_lag_decisions ORDER BY id").fetchall()
    events = []
    for ns, raw in rows:
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            continue
        if payload.get("event_type") in {"TMINUS_CHECKPOINT", "MARKET_TWAP_SUMMARY"}:
            payload["observed_ts"] = int(ns) / 1e9
            events.append(payload)
    return events


def selected_checkpoint_rows(events: list[dict]) -> tuple[list[dict], dict]:
    summaries = {row.get("market_slug"): row for row in events if row.get("event_type") == "MARKET_TWAP_SUMMARY"}
    grouped: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in events:
        if row.get("event_type") != "TMINUS_CHECKPOINT":
            continue
        try:
            cp = int(row["checkpoint_sec"])
            left = float(row["time_left_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if cp in CHECKPOINTS and 0 < left <= cp and row.get("market_slug"):
            grouped[(str(row["market_slug"]), cp)].append(row)
    selected = []
    for (slug, cp), rows in grouped.items():
        # One independent market observation per configured checkpoint.
        selected.append(min(rows, key=lambda r: (abs(float(r["time_left_sec"]) - cp), -float(r["observed_ts"]))))
    return selected, summaries


def _vol_thresholds(historical: list[dict]) -> tuple[float, float] | None:
    vals = sorted(float(row["vol_proxy"]) for row in historical if row.get("vol_proxy") is not None)
    if len(vals) < 3:
        return None
    return vals[int(.33 * (len(vals) - 1))], vals[int(.67 * (len(vals) - 1))]


def _vol_regime(value: float | None, thresholds: tuple[float, float] | None) -> str | None:
    if value is None or thresholds is None:
        return None
    lo, hi = thresholds
    return "LOW" if value <= lo else "MID" if value <= hi else "HIGH"


def estimate_row(row: dict, summary: dict, candles: list[dict], samples_by_h: dict[int, list[dict]],
                 window_days: int = ROLLING_DAYS) -> dict:
    cp = int(row["checkpoint_sec"])
    mode = str(row.get("required_move_mode") or "UNAVAILABLE")
    horizon = horizon_seconds(cp)
    output = {**row, "settlement_side": summary.get("settlement_side"),
              "settlement_reference_is_canonical": summary.get("settlement_reference_is_canonical"),
              "market_quote_fresh_for_research": market_quote_is_fresh(row),
              "required_move_mode_group": "EXACT_FINAL_WINDOW" if mode == "EXACT_FINAL_WINDOW_BOUNDARY" else "PRE_FINAL_PROXY" if mode == "PRE_FINAL_STRIKE_PROXY" else "UNAVAILABLE",
              "historical_horizon_sec": horizon, "empirical_sample_count": 0,
              "empirical_window_start": None, "empirical_window_end": None,
              "conditioning_mode": "UNAVAILABLE", "p_up_empirical": None,
              "p_up_empirical_vol_conditioned": None, "vol_conditioning_fallback": None,
              "empirical_resolution_note": ""}
    if summary.get("settlement_reference_is_canonical") is not True or summary.get("settlement_side") not in {"UP", "DOWN"}:
        output["empirical_resolution_note"] = "noncanonical_or_missing_settlement_label"
        return output
    if mode == "EXACT_FINAL_WINDOW_BOUNDARY":
        output["empirical_resolution_note"] = "1m_close_data_cannot_reconstruct_remaining_average_path_boundary"
        return output
    if mode != "PRE_FINAL_STRIKE_PROXY":
        output["empirical_resolution_note"] = "required_move_unavailable"
        return output
    if horizon is None:
        output["empirical_resolution_note"] = "1m_ohlcv_cannot_resolve_subminute_forward_horizon"
        return output
    try:
        required = float(row["required_move_bps"])
        current_side = str(row["settlement_state_side"])
        eval_ts = float(row["observed_ts"])
    except (KeyError, TypeError, ValueError):
        output["empirical_resolution_note"] = "required_move_or_observation_missing"
        return output
    if current_side not in {"UP", "DOWN"}:
        output["empirical_resolution_note"] = "settlement_state_side_unavailable"
        return output
    historical = rolling_history(samples_by_h[horizon], eval_ts, window_days)
    output["empirical_sample_count"] = len(historical)
    if historical:
        output["empirical_window_start"] = datetime.fromtimestamp(historical[0]["ts"], timezone.utc).isoformat()
        output["empirical_window_end"] = datetime.fromtimestamp(historical[-1]["ts"], timezone.utc).isoformat()
    if len(historical) < MIN_EMPIRICAL_SAMPLES:
        output["empirical_resolution_note"] = "insufficient_historical_sample_count"
        return output
    required_returns = [float(item["return_bps"]) for item in historical]
    output["p_up_empirical"] = empirical_cdf_probability(required_returns, required, current_side)
    output["conditioning_mode"] = "UNCONDITIONAL_EMPIRICAL_CDF"
    output["empirical_resolution_note"] = "minute_close_coarse; market_mid_not_executable; endpoint_return_is_proxy_not_official_twap_path"
    # Current volatility must be available; choose the nearest past-only 30-minute
    # historical volatility rank and fall back explicitly when regime is sparse.
    try:
        current_vol = float(row["sigma_ex_market"])
    except (KeyError, TypeError, ValueError):
        current_vol = None
    thresholds = _vol_thresholds(historical)
    regime = _vol_regime(current_vol, thresholds)
    output["volatility_regime"] = regime
    matching = [item for item in historical if _vol_regime(item.get("vol_proxy"), thresholds) == regime] if regime else []
    if regime and len(matching) >= MIN_VOL_BUCKET_SAMPLES:
        output["p_up_empirical_vol_conditioned"] = empirical_cdf_probability(
            [float(item["return_bps"]) for item in matching], required, current_side)
        output["conditioning_mode"] = "VOLATILITY_CONDITIONED_EMPIRICAL_CDF"
        output["vol_conditioning_fallback"] = False
        output["vol_empirical_sample_count"] = len(matching)
    else:
        output["p_up_empirical_vol_conditioned"] = output["p_up_empirical"]
        output["vol_conditioning_fallback"] = True
        output["vol_empirical_sample_count"] = len(matching)
    return output


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = sorted({key for row in rows for key in row}) or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _metric_rows(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    sources = ("analytic_p_up", "p_up_empirical", "p_up_empirical_vol_conditioned", "market_mid_probability_up")
    metrics, calibrations, paired = [], [], []
    for cp in CHECKPOINTS:
        # Keep strike-proxy and exact remaining-average boundaries separate.
        for mode in ("PRE_FINAL_PROXY", "EXACT_FINAL_WINDOW"):
            group = [r for r in rows if int(r["checkpoint_sec"]) == cp
                     and r.get("required_move_mode_group") == mode
                     and r.get("settlement_side") in {"UP", "DOWN"}]
            for source in sources:
                pairs = [(float(r[source]), int(r["settlement_side"] == "UP"), r) for r in group
                         if r.get(source) is not None and math.isfinite(float(r[source]))
                         and (source != "market_mid_probability_up" or market_quote_is_fresh(r))]
                scores = [score_probability(p, y) for p, y, _ in pairs]
                metrics.append({"checkpoint_sec": cp, "required_move_mode_group": mode,
                                "source": source, "n_markets": len({r["market_slug"] for _, _, r in pairs}),
                                "n_observations": len(pairs), "brier": statistics.mean(x[0] for x in scores) if scores else None,
                                "log_loss": statistics.mean(x[1] for x in scores) if scores else None,
                                "calibration_error_10bin": calibration_error([(p, y) for p, y, _ in pairs]),
                                "direction_accuracy": statistics.mean(x[2] for x in scores) if scores else None})
                for low, high in BUCKETS:
                    bucket = [(p, y) for p, y, _ in pairs if low <= p < high]
                    calibrations.append({"checkpoint_sec": cp, "mode": mode, "source": source,
                                         "probability_low": low, "probability_high": high, "n": len(bucket),
                                         "mean_predicted": statistics.mean(p for p, _ in bucket) if bucket else None,
                                         "actual_up_rate": statistics.mean(y for _, y in bucket) if bucket else None})
            if mode != "PRE_FINAL_PROXY":
                continue
            for estimator in ("p_up_empirical", "p_up_empirical_vol_conditioned"):
                estimator_rows = {r["market_slug"]: r for r in group if r.get(estimator) is not None}
                for baseline in ("market_mid_probability_up", "analytic_p_up"):
                    deltas = []
                    for slug, erow in estimator_rows.items():
                        brow = next((r for r in group if r["market_slug"] == slug and r.get(baseline) is not None), None)
                        if baseline == "market_mid_probability_up" and brow is not None and not market_quote_is_fresh(brow):
                            brow = None
                        if brow is None:
                            continue
                        y = int(erow["settlement_side"] == "UP")
                        delta = score_probability(float(erow[estimator]), y)[0] - score_probability(float(brow[baseline]), y)[0]
                        deltas.append({"market_slug": slug, "delta": delta, "checkpoint_sec": cp, "baseline": baseline})
                    boot = market_cluster_bootstrap(deltas)
                    paired.append({"checkpoint_sec": cp, "mode": mode, "estimator": estimator,
                                   "empirical_minus_baseline": baseline,
                                   "n_paired_markets": boot["n_markets"], "mean_paired_brier_delta": boot["mean_delta"],
                                   "median_market_paired_delta": boot["median_market_delta"],
                                   "bootstrap_ci_low": boot["ci_low"], "bootstrap_ci_high": boot["ci_high"],
                                   "positive_favors_baseline": True})
    return metrics, calibrations, paired


def _regime_metric_rows(rows: list[dict]) -> list[dict]:
    output = []
    move_bins = ((0, 2, "<2"), (2, 5, "2-5"), (5, 10, "5-10"),
                 (10, 20, "10-20"), (20, math.inf, ">20"))
    time_bins = ((0, 15, "0-15"), (15, 30, "15-30"), (30, 60, "30-60"),
                 (60, 120, "60-120"), (120, math.inf, ">120"))
    def bucket_for(row, bins, key, absolute=False):
        value = row.get(key)
        if value is None:
            return None
        value = abs(float(value)) if absolute else float(value)
        return next((label for low, high, label in bins if low <= value < high), None)
    dimensions = {
        "required_move_abs_bps": lambda row: bucket_for(row, move_bins, "required_move_bps", True),
        "time_left_sec": lambda row: bucket_for(row, time_bins, "time_left_sec"),
        "volatility_regime": lambda row: row.get("volatility_regime"),
    }
    for dimension, bucket_func in dimensions.items():
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            bucket = bucket_func(row)
            if bucket and row.get("required_move_mode_group") == "PRE_FINAL_PROXY":
                groups[str(bucket)].append(row)
        for bucket, group in groups.items():
            for source in ("p_up_empirical", "p_up_empirical_vol_conditioned", "analytic_p_up", "market_mid_probability_up"):
                pairs = [(float(r[source]), int(r["settlement_side"] == "UP")) for r in group
                         if r.get(source) is not None and r.get("settlement_side") in {"UP", "DOWN"}
                         and (source != "market_mid_probability_up" or market_quote_is_fresh(r))]
                scores = [score_probability(p, y) for p, y in pairs]
                output.append({"checkpoint_sec": 120, "mode": "PRE_FINAL_PROXY",
                               "regime_dimension": dimension, "regime": bucket, "source": source,
                               "n_markets": len(pairs),
                               "brier": statistics.mean(s[0] for s in scores) if scores else None,
                               "log_loss": statistics.mean(s[1] for s in scores) if scores else None,
                               "direction_accuracy": statistics.mean(s[2] for s in scores) if scores else None})
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/research/twap_forward_shadow.db"))
    parser.add_argument("--btc-cache", type=Path, default=Path("data/btc_history"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports/empirical_probability"))
    parser.add_argument("--window-days", type=int, default=ROLLING_DAYS)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error(f"TWAP research DB not found: {args.db}")
    candles = load_candles(args.btc_cache)
    events = load_events(args.db)
    selected, summaries = selected_checkpoint_rows(events)
    samples_by_h = {h: make_forward_samples(candles, h) for h in (60, 120)}
    estimated = [estimate_row(row, summaries.get(row.get("market_slug"), {}), candles, samples_by_h, args.window_days)
                for row in selected]
    eligible = []
    for row in estimated:
        if (row.get("settlement_reference_is_canonical") is True and row.get("settlement_side") in {"UP", "DOWN"}
                and row.get("sigma_ex_market_fresh") is True and row.get("p_up_ex_market") is not None):
            row["analytic_p_up"] = float(row["p_up_ex_market"])
            eligible.append(row)
    metrics, calibration, paired = _metric_rows(eligible)
    summary_rows = []
    for metric in metrics:
        key = (metric["checkpoint_sec"], metric["required_move_mode_group"])
        row = next((item for item in summary_rows if (item["checkpoint_sec"], item["mode"]) == key), None)
        if row is None:
            row = {"checkpoint_sec": key[0], "mode": key[1]}
            summary_rows.append(row)
        row["n_" + metric["source"]] = metric["n_markets"]
        row["brier_" + metric["source"]] = metric["brier"]
    for row in summary_rows:
        mode = row["mode"]
        for comparison in paired:
            if comparison["checkpoint_sec"] == row["checkpoint_sec"] and comparison["mode"] == mode:
                suffix = comparison["estimator"] + "_vs_" + comparison["empirical_minus_baseline"]
                row["paired_delta_" + suffix] = comparison["mean_paired_brier_delta"]
                row["paired_ci_low_" + suffix] = comparison["bootstrap_ci_low"]
                row["paired_ci_high_" + suffix] = comparison["bootstrap_ci_high"]
    regime_rows = _regime_metric_rows(eligible)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "empirical_probability_checkpoint.csv", estimated)
    write_csv(args.output_dir / "empirical_probability_checkpoint_summary.csv", summary_rows)
    write_csv(args.output_dir / "empirical_probability_calibration.csv", calibration)
    write_csv(args.output_dir / "empirical_probability_paired_comparison.csv", paired)
    write_csv(args.output_dir / "empirical_probability_regimes.csv", regime_rows)
    mode_counts = defaultdict(set)
    for row in estimated:
        mode_counts[row["required_move_mode_group"]].add(row["market_slug"])
    lines = ["# Offline empirical probability research", "",
             "No live runtime, strategy, or authority is changed by this analysis.", "",
             f"- TWAP DB: `{args.db}` (read-only)", f"- BTC source: `{args.btc_cache}`; cached Binance BTCUSDT 1m OHLCV only",
             f"- Historical window: rolling {args.window_days} days, strictly earlier than each evaluation timestamp",
             f"- Data coverage: {datetime.fromtimestamp(candles[0]['ts'], timezone.utc).date() if candles else 'none'} to {datetime.fromtimestamp(candles[-1]['ts'], timezone.utc).date() if candles else 'none'}",
             f"- Evaluation mode markets: exact={len(mode_counts['EXACT_FINAL_WINDOW'])}, pre-final proxy={len(mode_counts['PRE_FINAL_PROXY'])}",
             "- Minute-close data supports only coarse 60s/120s forward returns; 5/10/15/30s are unavailable.",
             "- Exact final-window remaining-average boundaries are not estimable from minute closes; those rows are excluded from empirical scoring.",
             "- Polymarket mid is not an executable price; these are probability scores, not PnL or edge estimates.",
             "- Vol-conditioned estimates use low/mid/high trailing-vol regimes; sparse regimes fall back to unconditional and are flagged.",
             "- Bootstrap resampling unit is market slug. Small canonical evaluation sample means intervals may be very wide.", "",
             "## Metric summary — modes reported separately", "", "| Checkpoint | Required-move mode | Source | Markets | Brier | Log loss | ECE | Direction accuracy |", "|---:|---|---|---:|---:|---:|---:|---:|"]
    for row in metrics:
        def fmt(key): return "—" if row[key] is None else f"{row[key]:.4f}"
        lines.append(f"| T−{row['checkpoint_sec']} | {row['required_move_mode_group']} | {row['source']} | {row['n_markets']} | {fmt('brier')} | {fmt('log_loss')} | {fmt('calibration_error_10bin')} | {fmt('direction_accuracy')} |")
    summary_lookup = {(row["checkpoint_sec"], row["mode"]): row for row in summary_rows}
    lines += ["", "## Requested checkpoint comparison", "",
              "| Checkpoint | Mode | N markets | Analytic Brier | Empirical Brier | Vol-conditioned Brier | Market-mid Brier |",
              "|---:|---|---:|---:|---:|---:|---:|"]
    for cp in CHECKPOINTS:
        for mode in ("PRE_FINAL_PROXY", "EXACT_FINAL_WINDOW"):
            row = summary_lookup.get((cp, mode), {})
            def show(key): return "—" if row.get(key) is None else f"{row[key]:.4f}"
            n = row.get("n_analytic_p_up", 0) or 0
            lines.append(f"| T−{cp} | {mode} | {n} | {show('brier_analytic_p_up')} | {show('brier_p_up_empirical')} | {show('brier_p_up_empirical_vol_conditioned')} | {show('brier_market_mid_probability_up')} |")
    valid_emp = sum(row.get("p_up_empirical") is not None for row in estimated)
    fresh_market_mid_rows = sum(
        row.get("market_mid_probability_up") is not None and market_quote_is_fresh(row)
        for row in estimated
    )
    unproven_market_mid_rows = sum(
        row.get("market_mid_probability_up") is not None and not market_quote_is_fresh(row)
        for row in estimated
    )
    exact_rows = [row for row in estimated if row["required_move_mode_group"] == "EXACT_FINAL_WINDOW"]
    proxy_rows = [row for row in estimated if row["required_move_mode_group"] == "PRE_FINAL_PROXY"]
    up_count = sum(row.get("settlement_side") == "UP" for row in proxy_rows if row.get("p_up_empirical") is not None)
    down_count = sum(row.get("settlement_side") == "DOWN" for row in proxy_rows if row.get("p_up_empirical") is not None)
    paired120 = {(row["estimator"], row["empirical_minus_baseline"]): row for row in paired if row["checkpoint_sec"] == 120}
    def paired_text(estimator: str, baseline: str) -> str:
        result = paired120.get((estimator, baseline), {})
        if not result.get("n_paired_markets"):
            return f"No paired sample at T−120 for {estimator} versus {baseline}."
        return (f"T−120 {estimator} Brier − {baseline} = {result['mean_paired_brier_delta']:.4f}; "
                f"market-cluster bootstrap 95% CI [{result['bootstrap_ci_low']:.4f}, {result['bootstrap_ci_high']:.4f}], "
                f"N={result['n_paired_markets']} markets.")
    lines += ["", "## Interpretation", "",
              f"Usable empirical predictions: {valid_emp} of {len(estimated)} selected checkpoint rows; {sum(r.get('p_up_empirical') is not None for r in proxy_rows)} are pre-final proxy rows and {sum(r.get('p_up_empirical') is not None for r in exact_rows)} exact-boundary rows.",
              f"Selected mode rows: PRE_FINAL_PROXY={len(proxy_rows)}, EXACT_FINAL_WINDOW={len(exact_rows)}, UNAVAILABLE={len(estimated)-len(proxy_rows)-len(exact_rows)}.",
              f"Freshness-proven market-mid checkpoint rows: {fresh_market_mid_rows}; market-mid rows excluded as stale or missing explicit source/receive ages: {unproven_market_mid_rows}.",
              f"Empirical-evaluable outcomes: UP={up_count}, DOWN={down_count}; hit rate is not stable evidence at this sample size.",
              paired_text("p_up_empirical", "analytic_p_up"), paired_text("p_up_empirical", "market_mid_probability_up"),
              paired_text("p_up_empirical_vol_conditioned", "analytic_p_up"),
              paired_text("p_up_empirical_vol_conditioned", "market_mid_probability_up"),
              "Q1 — Empirical vs analytic: not established; only eight paired T−120 proxy markets.",
              "Q2 — Empirical vs market mid: not established; mid is non-executable and this sample is too small.",
              "Q3 — Effective checkpoint/regime: cannot be established; sub-minute and exact final-window rows are not estimable from 1m OHLCV.",
              "Q4 — Stable residual structure for ML: not demonstrated.",
              "Classification: probability-model comparison is underpowered; settlement math alpha is undetermined; no standalone executable edge is shown.",
              "ML_JUSTIFIED = INSUFFICIENT_DATA", "", "Momentum-conditioned model was skipped: current evaluation panel and checkpoint labels are too sparse to justify another conditioning split."]
    (args.output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(estimated)} checkpoint rows; empirical-eligible={valid_emp}; output={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
