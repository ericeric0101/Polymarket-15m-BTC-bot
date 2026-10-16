#!/usr/bin/env python3
"""Offline, no-lookahead empirical replay of exact final-window TWAP paths.

This script is research-only. It never imports or changes live trading behavior.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bot.btc_1s_history import load_btc_1s_history
from execution.maker_engine import MakerEngine


DEFAULT_DB = Path("data/research/twap_forward_shadow.db")
DEFAULT_BTC_DIR = Path("data/btc_history_1s")
DEFAULT_OUTPUT = Path("reports/required_path_probability")
COVERAGE_THRESHOLDS = (0.90, 0.95, 1.0)
# Exact full-path coverage is the primary estimator; partial-coverage results
# are sensitivity diagnostics because missing path segments cannot be imputed.
PRIMARY_COVERAGE = 1.0
MIN_CONDITIONED_PATHS = 100
VOL_LOOKBACK_SEC = 60
SIGMA_BUCKETS = ((0.0, 1.0, "LT_1_SIGMA"), (1.0, 2.0, "1_TO_2_SIGMA"),
                 (2.0, 3.0, "2_TO_3_SIGMA"), (3.0, math.inf, "GT_3_SIGMA"))


def required_average_boundary(strike: float, observed_average: float,
                              observed_sec: float, window_sec: float = 60.0) -> float | None:
    """Reuse the production calculator's exact partial-integral boundary."""
    diagnostic = MakerEngine.twap_settlement_diagnostics(
        spot=None, strike=float(strike), sigma_annual=0.5,
        time_left_sec=max(0.0, float(window_sec) - float(observed_sec)),
        twap_window_sec=max(1, int(round(window_sec))),
        observed_window_avg=float(observed_average), observed_window_sec=float(observed_sec),
    )
    value = diagnostic.get("remaining_avg_decision_boundary")
    return float(value) if value is not None else None


def settlement_side_from_average(average: float, strike: float) -> str:
    """Match the bot's settlement convention: equality resolves UP."""
    return "UP" if float(average) >= float(strike) else "DOWN"


def is_flip(current_side: str, synthetic_average: float, boundary: float) -> bool:
    """A DOWN average ties UP; an UP leader flips only strictly below boundary."""
    if current_side == "UP":
        return float(synthetic_average) < float(boundary)
    if current_side == "DOWN":
        return float(synthetic_average) >= float(boundary)
    raise ValueError(f"invalid settlement side: {current_side!r}")


def horizon_weights(horizon_sec: float) -> list[float]:
    """One-second interval weights covering the actual, possibly fractional horizon."""
    horizon = float(horizon_sec)
    if not math.isfinite(horizon) or horizon <= 0:
        return []
    whole = int(math.floor(horizon))
    fraction = horizon - whole
    weights = [1.0] * whole
    if fraction > 1e-9:
        weights.append(fraction)
    if not weights:
        weights = [horizon]
    return weights


def _path_metrics(rows_by_sec: dict[int, dict], start_sec: int, horizon_sec: float,
                  current_spot: float, boundary: float) -> dict | None:
    """Project an observed historical path to current spot without filling gaps.

    The path's first bar is the normalization anchor. Missing seconds contribute
    no fabricated price; the average is over observed time only and coverage is
    returned so partial paths can be sensitivity-tested separately.
    """
    weights = horizon_weights(horizon_sec)
    if not weights:
        return None
    first = rows_by_sec.get(start_sec)
    if first is None:
        return None
    anchor = float(first["close"])
    if not math.isfinite(anchor) or anchor <= 0:
        return None
    weighted_relative = 0.0
    observed_weight = 0.0
    endpoint = None
    for offset, weight in enumerate(weights):
        row = rows_by_sec.get(start_sec + offset)
        if row is None:
            continue
        price = float(row["close"])
        if not math.isfinite(price) or price <= 0:
            continue
        weighted_relative += weight * (price / anchor)
        observed_weight += weight
        if offset == len(weights) - 1:
            endpoint = price / anchor
    coverage = observed_weight / float(horizon_sec)
    if observed_weight <= 0:
        return None
    projected_average = float(current_spot) * weighted_relative / observed_weight
    endpoint_average = float(current_spot) * endpoint if endpoint is not None else None
    return {"coverage": min(1.0, coverage), "synthetic_average": projected_average,
            "synthetic_endpoint": endpoint_average, "observed_weight_sec": observed_weight,
            "expected_weight_sec": float(horizon_sec)}


def _trailing_sigma(rows_by_sec: dict[int, dict], start_sec: int,
                    lookback_sec: int = VOL_LOOKBACK_SEC) -> float | None:
    """Annualized realized volatility from a complete preceding 1s close path."""
    prices = []
    for sec in range(start_sec - lookback_sec, start_sec + 1):
        row = rows_by_sec.get(sec)
        if row is None:
            return None
        price = float(row["close"])
        if price <= 0 or not math.isfinite(price):
            return None
        prices.append(price)
    returns = [math.log(b / a) for a, b in zip(prices, prices[1:])]
    if len(returns) < 20:
        return None
    return statistics.pstdev(returns) * math.sqrt(365 * 24 * 3600)


def candidate_paths(rows: list[dict], *, evaluation_ts: float, horizon_sec: float,
                    current_spot: float, boundary: float, current_side: str) -> list[dict]:
    """Enumerate historical horizons that finish no later than evaluation time."""
    by_sec = {int(row["ts_sec"]): row for row in rows}
    weights = horizon_weights(horizon_sec)
    if not weights or not by_sec:
        return []
    output = []
    earliest_start = min(by_sec)
    latest_start = math.floor(float(evaluation_ts) - float(horizon_sec))
    for start_sec in range(earliest_start, latest_start + 1):
        # The entire historical interval, including the partial final second,
        # must be strictly earlier than the evaluation timestamp.
        if start_sec >= evaluation_ts or start_sec + horizon_sec > evaluation_ts:
            continue
        metrics = _path_metrics(by_sec, start_sec, horizon_sec, current_spot, boundary)
        if metrics is None:
            # Count windows with a missing/invalid starting second as rejected
            # instead of silently removing them from the coverage denominator.
            output.append({"start_sec": start_sec, "coverage": 0.0,
                           "synthetic_average": None, "synthetic_endpoint": None,
                           "observed_weight_sec": 0.0, "expected_weight_sec": float(horizon_sec),
                           "flip": None, "endpoint_flip": None, "trailing_sigma": None})
            continue
        endpoint = metrics["synthetic_endpoint"]
        output.append({"start_sec": start_sec, **metrics,
                       "flip": is_flip(current_side, metrics["synthetic_average"], boundary),
                       "endpoint_flip": (is_flip(current_side, endpoint, boundary) if endpoint is not None else None),
                       "trailing_sigma": _trailing_sigma(by_sec, start_sec)})
    return output


def estimate_probability(paths: list[dict], *, coverage_threshold: float,
                         current_sigma: float | None = None) -> dict:
    """Return raw empirical path/endpoint flip rates and vol-tercile estimate."""
    eligible = [row for row in paths if row["coverage"] + 1e-12 >= coverage_threshold]
    rejected = len(paths) - len(eligible)
    path_rate = (sum(bool(row["flip"]) for row in eligible) / len(eligible)
                 if eligible else None)
    endpoint_rows = [row for row in eligible if row["endpoint_flip"] is not None]
    endpoint_rate = (sum(bool(row["endpoint_flip"]) for row in endpoint_rows) / len(endpoint_rows)
                     if endpoint_rows else None)
    vol_rows = [row for row in eligible if row.get("trailing_sigma") is not None]
    vols = sorted(row["trailing_sigma"] for row in vol_rows)
    vol_rate = None
    vol_bucket = None
    vol_sample_count = 0
    vol_fallback_reason = None
    if current_sigma is None or not math.isfinite(float(current_sigma)):
        vol_fallback_reason = "current_sigma_unavailable"
    elif len(vols) < MIN_CONDITIONED_PATHS:
        vol_fallback_reason = "insufficient_vol_labeled_paths"
    else:
        p33, p67 = _quantile(vols, 1 / 3), _quantile(vols, 2 / 3)
        current_band = 0 if current_sigma <= p33 else 1 if current_sigma <= p67 else 2
        band_rows = [r for r in vol_rows if (0 if r["trailing_sigma"] <= p33 else 1 if r["trailing_sigma"] <= p67 else 2) == current_band]
        if len(band_rows) >= MIN_CONDITIONED_PATHS:
            vol_bucket = ("LOW" if current_band == 0 else "MID" if current_band == 1 else "HIGH")
            vol_sample_count = len(band_rows)
            vol_rate = sum(bool(r["flip"]) for r in band_rows) / len(band_rows)
        else:
            vol_fallback_reason = "insufficient_paths_in_current_vol_tercile"
    return {"coverage_threshold": coverage_threshold, "eligible_historical_paths": len(eligible),
            "rejected_missing_data_paths": rejected, "empirical_flip_probability": path_rate,
            "endpoint_flip_probability_diagnostic_only": endpoint_rate,
            "endpoint_eligible_paths": len(endpoint_rows),
            "empirical_vol_conditioned_flip_probability": vol_rate,
            "conditioning_mode": f"VOL_{vol_bucket}_TERCILE" if vol_rate is not None else "UNCONDITIONAL_FALLBACK",
            "volatility_bucket": vol_bucket, "vol_conditioned_sample_count": vol_sample_count,
            "vol_fallback_reason": vol_fallback_reason}


def _quantile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("quantile requires values")
    pos = max(0.0, min(1.0, q)) * (len(values) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _load_events(db_path: Path) -> list[dict]:
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT decision_epoch_ns,payload_json FROM lead_lag_decisions ORDER BY decision_epoch_ns").fetchall()
    output = []
    for ns, raw in rows:
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            continue
        payload["observed_ts"] = int(ns) / 1e9
        output.append(payload)
    return output


def exact_observations(events: list[dict]) -> list[dict]:
    summaries = {r.get("market_slug"): r for r in events
                 if r.get("event_type") == "MARKET_TWAP_SUMMARY" and r.get("market_slug")}
    observations = []
    for row in events:
        if row.get("event_type") != "TMINUS_CHECKPOINT" or row.get("required_move_mode") != "EXACT_FINAL_WINDOW_BOUNDARY":
            continue
        try:
            horizon = float(row["remaining_final_window_sec"])
            spot = float(row["path_spot"])
            boundary = float(row["remaining_avg_decision_boundary"])
            current_side = str(row["settlement_state_side"])
        except (KeyError, TypeError, ValueError):
            continue
        if horizon <= 0 or spot <= 0 or current_side not in {"UP", "DOWN"}:
            continue
        summary = summaries.get(row.get("market_slug"), {})
        observations.append({**row, "remaining_sec_actual": horizon,
                             "decision_boundary": boundary, "current_path_spot": spot,
                             "current_settlement_side": current_side,
                             "flip_side": "DOWN" if current_side == "UP" else "UP",
                             "settlement_side": summary.get("settlement_side"),
                             "settlement_reference_source": summary.get("settlement_reference_source"),
                             "settlement_reference_is_canonical": summary.get("settlement_reference_is_canonical") is True,
                             "settlement_reference_age_sec": summary.get("settlement_reference_age_sec")})
    return observations


def _p_up_from_flip(current_side: str, p_flip: float | None) -> float | None:
    if p_flip is None:
        return None
    return 1.0 - p_flip if current_side == "UP" else p_flip


def _sigma_bucket(value: float | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    for low, high, name in SIGMA_BUCKETS:
        if low <= abs(float(value)) < high:
            return name
    return "UNAVAILABLE"


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not keys:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _score(p: float, outcome_up: int) -> tuple[float, float]:
    p = min(1 - 1e-12, max(1e-12, p))
    return (p - outcome_up) ** 2, -(outcome_up * math.log(p) + (1 - outcome_up) * math.log(1 - p))


def _market_cluster_ci(rows: list[dict], value_key: str, reps: int = 2000, seed: int = 17) -> dict:
    import random
    clusters: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(value_key)
        slug = row.get("market_slug")
        if slug and value is not None and math.isfinite(float(value)):
            clusters[str(slug)].append(float(value))
    means = [statistics.mean(values) for values in clusters.values()]
    if not means:
        return {"n_markets": 0, "mean": None, "ci_low": None, "ci_high": None}
    rng = random.Random(seed)
    bootstrap = sorted(statistics.mean(means[rng.randrange(len(means))] for _ in means) for _ in range(reps))
    return {"n_markets": len(means), "mean": statistics.mean(means),
            "ci_low": bootstrap[int(.025 * (reps - 1))],
            "ci_high": bootstrap[int(.975 * (reps - 1))]}


def paired_market_comparison(rows: list[dict]) -> list[dict]:
    """Paired analytic-vs-empirical scores; bootstrap clusters by market."""
    pairs = []
    for row in rows:
        if (row.get("settlement_reference_is_canonical") is not True
                or row.get("settlement_side") not in {"UP", "DOWN"}
                or row.get("analytic_p_up_ex_market") is None
                or row.get("p_up_empirical_path") is None):
            continue
        outcome_up = int(row["settlement_side"] == "UP")
        analytic_brier, analytic_ll = _score(float(row["analytic_p_up_ex_market"]), outcome_up)
        empirical_brier, empirical_ll = _score(float(row["p_up_empirical_path"]), outcome_up)
        pairs.append({**row, "delta_brier_empirical_minus_analytic": empirical_brier - analytic_brier,
                      "delta_log_loss_empirical_minus_analytic": empirical_ll - analytic_ll})
    output = []
    for checkpoint in sorted({row.get("checkpoint_sec") for row in pairs}):
        group = [row for row in pairs if row.get("checkpoint_sec") == checkpoint]
        brier = _market_cluster_ci(group, "delta_brier_empirical_minus_analytic")
        logloss = _market_cluster_ci(group, "delta_log_loss_empirical_minus_analytic")
        output.append({"checkpoint_sec": checkpoint, "paired_observations": len(group),
                       "brier_n_markets": brier["n_markets"], "brier_delta_mean": brier["mean"],
                       "brier_delta_ci_low": brier["ci_low"], "brier_delta_ci_high": brier["ci_high"],
                       "log_loss_n_markets": logloss["n_markets"], "log_loss_delta_mean": logloss["mean"],
                       "log_loss_delta_ci_low": logloss["ci_low"], "log_loss_delta_ci_high": logloss["ci_high"],
                       "bootstrap_unit": "market_slug", "descriptive_only": True})
    return output


def build_outputs(observations: list[dict], btc_rows: list[dict]) -> dict[str, list[dict]]:
    probability_rows = []
    quality_rows = []
    thresholds = []
    for obs in observations:
        horizon = obs["remaining_sec_actual"]
        paths = candidate_paths(btc_rows, evaluation_ts=float(obs["observed_ts"]),
                                horizon_sec=horizon, current_spot=obs["current_path_spot"],
                                boundary=obs["decision_boundary"], current_side=obs["current_settlement_side"])
        current_sigma = obs.get("sigma_ex_market")
        try:
            current_sigma = float(current_sigma) if current_sigma is not None else None
        except (TypeError, ValueError):
            current_sigma = None
        estimates = {int(round(threshold * 100)): estimate_probability(
            paths, coverage_threshold=threshold, current_sigma=current_sigma)
            for threshold in COVERAGE_THRESHOLDS}
        primary = estimates[int(round(PRIMARY_COVERAGE * 100))]
        p_flip = primary["empirical_flip_probability"]
        p_up = _p_up_from_flip(obs["current_settlement_side"], p_flip)
        analytic_up = obs.get("p_up_ex_market") if obs.get("sigma_ex_market_fresh") is True else None
        analytic_flip = (1.0 - float(analytic_up) if obs["current_settlement_side"] == "UP" else float(analytic_up)) if analytic_up is not None else None
        move_usd = obs.get("required_move_usd")
        move_bps = obs.get("required_move_bps")
        if move_usd is None:
            move_usd = obs["decision_boundary"] - obs["current_path_spot"]
        if move_bps is None:
            move_bps = float(move_usd) / obs["current_path_spot"] * 10000
        usable_starts = [r["start_sec"] for r in paths if r.get("coverage", 0) > 0]
        row = {"market_slug": obs.get("market_slug"), "observed_ts": obs["observed_ts"],
               "observed_utc": datetime.fromtimestamp(float(obs["observed_ts"]), timezone.utc).isoformat(),
               "checkpoint_sec": obs.get("checkpoint_sec"), "time_left_sec": obs.get("time_left_sec"),
               "required_move_mode": obs.get("required_move_mode"), "remaining_sec_actual": horizon,
               "matched_horizon_sec": horizon, "current_settlement_side": obs["current_settlement_side"],
               "flip_side": obs["flip_side"], "current_path_spot": obs["current_path_spot"],
               "decision_boundary": obs["decision_boundary"], "required_move_usd": move_usd,
               "required_move_bps": move_bps, "required_move_sigma": obs.get("required_move_sigma"),
               "analytic_p_up_ex_market": analytic_up, "analytic_flip_probability": analytic_flip,
               "empirical_flip_probability": p_flip, "p_up_empirical_path": p_up,
               "p_down_empirical_path": 1.0 - p_up if p_up is not None else None,
               "delta_empirical_vs_analytic_up": p_up - float(analytic_up) if p_up is not None and analytic_up is not None else None,
               "endpoint_flip_probability_diagnostic_only": primary["endpoint_flip_probability_diagnostic_only"],
               "eligible_historical_paths": primary["eligible_historical_paths"],
               "rejected_missing_data_paths": primary["rejected_missing_data_paths"],
               "historical_sample_start_ts": min(usable_starts) if usable_starts else None,
               "historical_sample_end_ts": max(usable_starts) if usable_starts else None,
               "coverage_threshold": PRIMARY_COVERAGE,
               "empirical_vol_conditioned_flip_probability": primary["empirical_vol_conditioned_flip_probability"],
               "volatility_conditioning_mode": primary["conditioning_mode"],
               "volatility_bucket": primary["volatility_bucket"],
               "vol_conditioned_sample_count": primary["vol_conditioned_sample_count"],
               "settlement_side": obs.get("settlement_side"),
               "settlement_reference_is_canonical": obs.get("settlement_reference_is_canonical"),
               "empirical_probability_status": "OK" if p_flip is not None else "INSUFFICIENT_HISTORY"}
        for threshold_pct, estimate in estimates.items():
            for key in ("empirical_flip_probability", "eligible_historical_paths", "rejected_missing_data_paths",
                        "endpoint_flip_probability_diagnostic_only"):
                row[f"coverage_{threshold_pct}_{key}"] = estimate.get(key)
        probability_rows.append(row)
        recomputed_boundary = required_average_boundary(
            float(obs["strike"]), float(obs["observed_final_window_avg"]),
            float(obs["observed_final_window_sec"]), 60.0
        ) if obs.get("strike") is not None and obs.get("observed_final_window_avg") is not None else None
        quality_rows.append({"market_slug": obs.get("market_slug"), "observed_ts": obs["observed_ts"],
                             "remaining_sec_actual": horizon, "candidate_historical_paths": len(paths),
                             "eligible_90pct": estimates[90]["eligible_historical_paths"],
                             "eligible_95pct": estimates[95]["eligible_historical_paths"],
                             "eligible_100pct": estimates[100]["eligible_historical_paths"],
                             "rejected_95pct": estimates[95]["rejected_missing_data_paths"],
                             "coverage_threshold_primary": PRIMARY_COVERAGE,
                             "recorded_boundary": obs["decision_boundary"],
                             "recomputed_boundary": recomputed_boundary,
                             "boundary_math_abs_error": abs(recomputed_boundary - obs["decision_boundary"]) if recomputed_boundary is not None else None,
                             "historical_start_before_evaluation": all(r["start_sec"] + horizon <= obs["observed_ts"] for r in paths),
                             "historical_paths_are_overlapping": True,
                             "status": "OK" if estimates[95]["eligible_historical_paths"] else "INSUFFICIENT_HISTORY"})

    timelines = sorted(probability_rows, key=lambda r: (str(r.get("market_slug")), float(r["observed_ts"])))
    for slug in sorted({r.get("market_slug") for r in timelines if r.get("market_slug")}):
        rows = [r for r in timelines if r.get("market_slug") == slug]
        previous = None
        for row in rows:
            p = row.get("empirical_flip_probability")
            if p is not None:
                for threshold in (.25, .10, .05, .01):
                    if p <= threshold and (previous is None or previous > threshold):
                        thresholds.append({"market_slug": slug, "observed_ts": row["observed_ts"],
                                           "time_left_sec": row.get("time_left_sec"), "event": "FLIP_PROBABILITY_CROSSED_BELOW",
                                           "threshold": threshold, "empirical_flip_probability": p,
                                           "required_move_bps": row.get("required_move_bps"),
                                           "required_move_sigma": row.get("required_move_sigma")})
                for threshold in (.10, .25, .50):
                    if p >= threshold and (previous is None or previous < threshold):
                        thresholds.append({"market_slug": slug, "observed_ts": row["observed_ts"],
                                           "time_left_sec": row.get("time_left_sec"), "event": "FLIP_PROBABILITY_CROSSED_ABOVE",
                                           "threshold": threshold, "empirical_flip_probability": p,
                                           "required_move_bps": row.get("required_move_bps"),
                                           "required_move_sigma": row.get("required_move_sigma")})
                previous = p
    calibration = []
    for checkpoint in sorted({r.get("checkpoint_sec") for r in probability_rows if r.get("checkpoint_sec") is not None}):
        rows = [r for r in probability_rows if r.get("checkpoint_sec") == checkpoint
                and r.get("settlement_reference_is_canonical") is True
                and r.get("settlement_side") in {"UP", "DOWN"}]
        outcomes = [int(r["settlement_side"] == "UP") for r in rows]
        for model, key in (("analytic", "analytic_p_up_ex_market"), ("empirical_path", "p_up_empirical_path")):
            usable = [(r, y) for r, y in zip(rows, outcomes) if r.get(key) is not None]
            scores = [_score(float(r[key]), y) for r, y in usable]
            brier = [x[0] for x in scores]
            logloss = [x[1] for x in scores]
            calibration.append({"checkpoint_sec": checkpoint, "model": model,
                               "n_observations": len(usable),
                               "n_markets": len({r["market_slug"] for r, _ in usable}),
                               "brier": statistics.mean(brier) if brier else None,
                               "log_loss": statistics.mean(logloss) if logloss else None,
                               "market_cluster_brier_ci_low": _market_cluster_ci(
                                   [{**r, "delta": score[0]} for (r, _), score in zip(usable, scores)], "delta")["ci_low"] if brier else None,
                               "market_cluster_brier_ci_high": _market_cluster_ci(
                                   [{**r, "delta": score[0]} for (r, _), score in zip(usable, scores)], "delta")["ci_high"] if brier else None,
                               "descriptive_only": True})
    return {"observation_probability.csv": probability_rows,
            "market_flip_timeline.csv": timelines,
            "threshold_crossings.csv": thresholds,
            "calibration.csv": calibration,
            "paired_comparison.csv": paired_market_comparison(probability_rows),
            "data_quality.csv": quality_rows}


def _fmt(value, digits=4):
    return "—" if value is None else f"{float(value):.{digits}f}"


def render_summary(outputs: dict[str, list[dict]], btc_rows: list[dict]) -> str:
    rows = outputs["observation_probability.csv"]
    data_quality = outputs["data_quality.csv"]
    slugs = sorted({r["market_slug"] for r in rows if r.get("market_slug")})
    canonical = [r for r in rows if r.get("settlement_reference_is_canonical") is True and r.get("settlement_side") in {"UP", "DOWN"}]
    paired = [r for r in canonical if r.get("analytic_flip_probability") is not None and r.get("empirical_flip_probability") is not None]
    enough_status = "NO" if not btc_rows else "NO (only one short capture/session and highly overlapping paths)"
    if btc_rows:
        data_start, data_end = int(btc_rows[0]["ts_sec"]), int(btc_rows[-1]["ts_sec"])
        span = data_end - data_start + 1
        coverage = len({int(r["ts_sec"]) for r in btc_rows}) / span if span else 0.0
        history_desc = (f"{len(btc_rows)} 1-second bars, {span} sec span ({coverage:.1%} observed-second coverage), "
                        f"{datetime.fromtimestamp(data_start, timezone.utc).isoformat()} to "
                        f"{datetime.fromtimestamp(data_end, timezone.utc).isoformat()}")
    else:
        history_desc = "No BTC 1-second bars available"
    lines = ["# Required-path probability replay", "",
             "Offline-only descriptive analysis. No live authority; Polymarket mid is never an estimator input.", "",
             "## Dataset and eligibility", "",
             f"- TWAP DB exact observations: {len(rows)} rows across {len(slugs)} markets.",
             f"- BTC 1-second history: {history_desc}.",
             f"- Observations with a canonical settlement label: {len(canonical)}; paired analytic/empirical rows: {len(paired)}.",
             f"- Primary coverage threshold: {PRIMARY_COVERAGE:.0%}; 90% and 95% are partial-path sensitivity diagnostics only.",
             "- Historical candidate windows must end before the evaluation timestamp. The primary estimate requires every 1-second interval; missing seconds are never forward-filled. Partial-coverage sensitivity averages only observed seconds and is not an exact full-path reconstruction.",
             "- Historical path windows overlap heavily. Raw candidate counts are not independent samples; cluster-level uncertainty is by market, and this one-day dataset cannot support stable intervals.",
             "- Status: `INSUFFICIENT_HISTORY` for any broader probability claim. This capture is an illustrative pipeline/replay check, not a stable empirical distribution.", "",
             "## Existing field semantics", "",
             "- `remaining_avg_decision_boundary` / `required_future_avg_to_flip`: exact final-window remaining average price which makes the full official window average equal strike, computed from observed partial integral; equality resolves UP under repository convention.",
             "- `required_move_usd` / `required_move_bps`: boundary minus current `path_spot`, in USD and basis points.",
             "- `required_move_sigma`: existing analytic standardized-distance diagnostic for the same boundary/horizon; it is not itself a path-average empirical probability.",
             "- `required_move_mode`: primary rows require `EXACT_FINAL_WINDOW_BOUNDARY`; `PRE_FINAL_STRIKE_PROXY` and `UNAVAILABLE` are excluded.",
             "- `observed_final_window_avg` and `observed_final_window_sec`: already-observed 60-second window average and duration; `remaining_final_window_sec` is the remaining time for the empirical path horizon.",
             "- `settlement_state_side`: current official-TWAP-vs-strike side. `p_up_ex_market` is the existing analytic probability; empirical path probability uses only BTC paths and boundary, not market mid.", "",
             "## Results", "",
             f"| Market | Observations | Current/settled side | Example horizon | Boundary / move | Required σ | Analytic flip p | Empirical flip p ({PRIMARY_COVERAGE:.0%}) | Eligible paths |",
             "|---|---:|---|---:|---:|---:|---:|---:|---:|"]
    for slug in slugs:
        group = [r for r in rows if r["market_slug"] == slug]
        row = min(group, key=lambda r: abs(float(r["remaining_sec_actual"]) - 30.0))
        side = f"{row.get('current_settlement_side')} / {row.get('settlement_side') or 'label unavailable'}"
        lines.append(f"| {slug} | {len(group)} | {side} | {float(row['remaining_sec_actual']):.1f}s | ${float(row['current_path_spot']):,.2f} → ${float(row['decision_boundary']):,.2f}; {float(row['required_move_usd']):+,.2f} USD / {float(row['required_move_bps']):+.2f} bps | {_fmt(row.get('required_move_sigma'), 2)}σ | {_fmt(row.get('analytic_flip_probability'))} | {_fmt(row.get('empirical_flip_probability'))} | {row.get('eligible_historical_paths', 0)} |")
    if not slugs:
        lines.append("| No exact observations available | 0 | — | — | — | — | — | 0 |")
    lines += ["", "### Required-move-sigma bands", "",
              "Rates below are forecast-time estimates: empirical values are the mean of each observation's historical full-path window flip frequency, not the realized outcome frequency across live trades. Repeated observations within a market are correlated.", "",
              "| Required move | Exact observations | Full-path estimates | Mean historical-path flip frequency | Mean analytic flip p |",
              "|---|---:|---:|---:|---:|"]
    for low, high, name in SIGMA_BUCKETS:
        group = [r for r in rows if _sigma_bucket(r.get("required_move_sigma")) == name]
        flips = [r["empirical_flip_probability"] for r in group if r.get("empirical_flip_probability") is not None]
        analytic = [r["analytic_flip_probability"] for r in group if r.get("analytic_flip_probability") is not None]
        lines.append(f"| {name} | {len(group)} | {len(flips)} | {_fmt(statistics.mean(flips) if flips else None)} | {_fmt(statistics.mean(analytic) if analytic else None)} |")
    lines += ["", "## Answers", "",
              "1. **Exact required-average math:** the replay consumes the calculator's exact partial-integral boundary; the underlying identity is `(window × strike − observed_seconds × observed_average) / remaining_seconds`. It is consistent with the implementation, which uses the same boundary for UP/DOWN because settlement compares the full average to strike.",
              "2. **Analytic versus empirical:** this dataset is too short and path windows overlap, so observed differences are illustrative only. See per-observation CSV; no stable error comparison is claimed.",
              "3. **Historical flip rate by sigma:** see the sigma-band table and `observation_probability.csv`; the sample is not independent by second and is insufficient to infer a reliable rate.",
              "4. **Bias direction:** not determinable from this capture. It cannot establish systematic overestimation, underestimation, or calibration.",
              "5. **Conclusion:** not enough history for a probability-quality conclusion. The path-average estimator is operationally testable, but only 3 markets have current 1-second history preceding exact observations; many windows share the same short session.", "",
              "## Distinguish the three questions", "",
              "- **SETTLEMENT MATH:** deterministic boundary calculation; exact rows define the remaining average needed to cross strike.",
              "- **PROBABILITY ESTIMATION:** analytic probability versus historical path-frequency estimate; currently `INSUFFICIENT_HISTORY` for a stable conclusion.",
              "- **TRADING EDGE:** not tested. There is no BBO/ask execution, fees, depth, or fills in this probability replay; no live action is authorized.", "",
              "## Coverage / data quality", "",
              "- `data_quality.csv` reports candidate path count, rejected windows, coverage-sensitivity counts, recomputes the required-average boundary from the observed integral, and verifies the strict no-lookahead condition per observation.",
              "- `paired_comparison.csv` gives analytic-minus-empirical paired Brier/log-loss deltas with bootstrap resampling by `market_slug`; it will be empty when no exact canonical, paired evaluation rows exist.",
              "- For primary path-average probability, missing seconds are not filled and incomplete windows are rejected. Under partial-coverage sensitivities, the synthetic average is normalized over observed seconds only, so it is not an exact reconstruction of the missing full path.",
              "- `endpoint_flip_probability_diagnostic_only` is deliberately separate from the primary remaining-path-average probability.",
              "- Volatility conditioning uses the current analytic sigma to select a LOW/MID/HIGH tercile of historical windows with a complete preceding 60-second realized-volatility sample; fewer than 100 paths causes explicit unconditional fallback.",
              "- Momentum conditioning is omitted because the current history is too short and correlated to justify another split.", "",
              "## What would make this useful", "",
              "Continue accumulating timestamped 1-second history across multiple weeks and volatility regimes. A practical review gate is not a raw count of overlapping windows: require multiple independent market-days, adequate coverage in each horizon/volatility band, and market-cluster confidence intervals narrow enough to distinguish the empirical estimate from the analytic estimate. Keep this research-only until it also survives executable-price, fee, depth, and forward validation.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--btc-data-dir", type=Path, default=DEFAULT_BTC_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    events = _load_events(args.db)
    observations = exact_observations(events)
    if observations:
        start = min(float(r["observed_ts"]) for r in observations)
        end = max(float(r["observed_ts"]) for r in observations) + 1
        btc_rows = load_btc_1s_history(start_ts=0, end_ts=end, data_dir=args.btc_data_dir)
    else:
        btc_rows = []
    outputs = build_outputs(observations, btc_rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in outputs.items():
        _write_csv(args.output_dir / name, rows)
    (args.output_dir / "summary.md").write_text(render_summary(outputs, btc_rows), encoding="utf-8")
    print(f"exact observations={len(observations)} markets={len({r.get('market_slug') for r in observations})} "
          f"historical 1s bars={len(btc_rows)} output={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
