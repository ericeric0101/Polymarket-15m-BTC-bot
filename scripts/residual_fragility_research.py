#!/usr/bin/env python3
"""Offline-only residual-repricing and entry-fragility research.

Reuses prediction_forensics' parsers, freshness definitions, entry replay, and
the repository BTC 1-second loader. Nothing in this script has live authority.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path

from bot.btc_1s_history import load_btc_1s_history
from scripts import prediction_forensics as pf

HORIZONS = (5, 10, 30, 60)
FRESH_SEC = pf.FRESH_SEC
RESIDUAL_BINS = ((-math.inf, -.10, "<=-0.10"), (-.10, -.05, "-0.10_to_-0.05"),
                 (-.05, -.02, "-0.05_to_-0.02"), (-.02, .02, "-0.02_to_+0.02"),
                 (.02, .05, "+0.02_to_+0.05"), (.05, .10, "+0.05_to_+0.10"),
                 (.10, math.inf, ">=+0.10"))
SIGMA_BINS = ((-math.inf, .5, "<0.5"), (.5, 1, "0.5-1"), (1, 2, "1-2"),
              (2, 3, "2-3"), (3, math.inf, ">3"))
EDGE_BINS = ((-math.inf, 0, "<=0"), (0, .02, "0-0.02"), (.02, .05, "0.02-0.05"),
             (.05, .10, "0.05-0.10"), (.10, math.inf, ">0.10"))


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _bin(value, bins):
    if value is None:
        return "UNAVAILABLE"
    for low, high, name in bins:
        if low <= value < high:
            return name
    return bins[-1][2]


def _csv(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    fields = list(dict.fromkeys(key for row in rows for key in row)) or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _load_twap(path: Path, slugs: set[str]):
    by_slug = defaultdict(list)
    with pf._db(path) as conn:
        rows = conn.execute("SELECT decision_epoch_ns,slug,payload_json FROM lead_lag_decisions ORDER BY decision_epoch_ns")
        for ns, slug, raw in rows:
            if slug not in slugs:
                continue
            try:
                import json
                payload = json.loads(raw or "{}")
            except (TypeError, ValueError):
                continue
            payload["observed_ts"] = _number(payload.get("observed_ts")) or int(ns) / 1e9
            by_slug[slug].append(payload)
    return by_slug


def _valid_joint(row):
    return (pf._valid_model(row) and pf._fresh_mid(row)
            and _number(row.get("market_mid_probability_up")) is not None)


def _mid(row, side="UP"):
    value = _number(row.get("market_mid_probability_up"))
    return value if side == "UP" else (1 - value if value is not None else None)


def _future_match(rows, target_ts, current_ts, tolerance=1.5):
    candidates = [row for row in rows if row.get("fresh", True) is not False
                  and row["observed_ts"] > current_ts
                  and abs(row["observed_ts"] - target_ts) <= tolerance]
    if not candidates:
        return None
    return min(candidates, key=lambda row: (abs(row["observed_ts"] - target_ts), row["observed_ts"]))


def _cluster_mean_ci(rows, value_key, *, reps=1000, seed=83):
    """Market-cluster bootstrap CI for the observation-weighted mean."""
    groups = defaultdict(list)
    for row in rows:
        value = _number(row.get(value_key))
        if value is not None:
            groups[row["market_slug"]].append(value)
    if not groups:
        return None, None, None, 0
    values = [value for group in groups.values() for value in group]
    estimate = statistics.mean(values)
    if len(groups) < 2:
        return estimate, None, None, len(groups)
    import random
    rng = random.Random(seed)
    names = list(groups)
    samples = []
    for _ in range(reps):
        draw = [value for name in (rng.choice(names) for _ in names) for value in groups[name]]
        if draw:
            samples.append(statistics.mean(draw))
    samples.sort()
    return estimate, samples[int(.025 * (len(samples) - 1))], samples[int(.975 * (len(samples) - 1))], len(groups)


def _solve(matrix, vector):
    n = len(vector)
    augmented = [list(map(float, row)) + [float(vector[i])] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(augmented[r][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            return None
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        augmented[col] = [v / scale for v in augmented[col]]
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col]
            augmented[row] = [a - factor * b for a, b in zip(augmented[row], augmented[col])]
    return [augmented[i][-1] for i in range(n)]


def _fit_metrics(rows, features):
    complete = [row for row in rows if _number(row.get("future_mid_change")) is not None
                and all(_number(row.get(feature)) is not None for feature in features)]
    if not complete:
        return {"n_observations": 0, "n_markets": 0, "r_squared": None, "mae": None,
                "directional_accuracy": None}
    y = [float(row["future_mid_change"]) for row in complete]
    if not features:
        pred = [0.0] * len(y)
    else:
        x = [[1.0] + [float(row[key]) for key in features] for row in complete]
        k = len(x[0])
        xtx = [[sum(r[i] * r[j] for r in x) for j in range(k)] for i in range(k)]
        xty = [sum(r[i] * target for r, target in zip(x, y)) for i in range(k)]
        beta = _solve(xtx, xty)
        if beta is None:
            return {"n_observations": len(complete), "n_markets": len({r['market_slug'] for r in complete}),
                    "r_squared": None, "mae": None, "directional_accuracy": None}
        pred = [sum(a * b for a, b in zip(r, beta)) for r in x]
    mean_y = statistics.mean(y)
    ss_total = sum((value - mean_y) ** 2 for value in y)
    directional = [(p > 0) == (actual > 0) for p, actual in zip(pred, y) if p != 0 and actual != 0]
    return {"n_observations": len(complete), "n_markets": len({r["market_slug"] for r in complete}),
            "r_squared": 1 - sum((a - b) ** 2 for a, b in zip(y, pred)) / ss_total if ss_total else None,
            "mae": statistics.mean(abs(a - b) for a, b in zip(y, pred)),
            "directional_accuracy": statistics.mean(directional) if directional else None,
            "_complete": complete, "_prediction": pred}


def _repricing_models(rows):
    models = {"M0_zero": [], "M1_mid_only": ["market_mid_up"],
              "M2_residual_only": ["residual_up"],
              "M3_mid_plus_residual": ["market_mid_up", "residual_up"],
              "M4_mid_plus_btc10": ["market_mid_up", "btc_return_10s_bps"],
              "M5_mid_residual_btc10": ["market_mid_up", "residual_up", "btc_return_10s_bps"]}
    out = []
    for horizon in HORIZONS:
        group = [row for row in rows if row["horizon_sec"] == horizon]
        fitted = {}
        for name, features in models.items():
            metrics = _fit_metrics(group, features)
            fitted[name] = metrics
            out.append({"horizon_sec": horizon, "model": name, "features": "+".join(features) or "constant_zero",
                        **{k: v for k, v in metrics.items() if not k.startswith("_")},
                        "evaluation": "in-sample descriptive OLS; observation rows are clustered by market for uncertainty"})
        base = fitted["M1_mid_only"]
        for name in ("M2_residual_only", "M3_mid_plus_residual", "M4_mid_plus_btc10", "M5_mid_residual_btc10"):
            metrics = fitted[name]
            common = set(r["market_slug"] for r in base.get("_complete", [])) & set(r["market_slug"] for r in metrics.get("_complete", []))
            # Paired loss differences on common observations, bootstrapped by market.
            bmap = {id(row): abs(float(row["future_mid_change"]) - pred)
                    for row, pred in zip(base.get("_complete", []), base.get("_prediction", []))}
            mmap = {id(row): abs(float(row["future_mid_change"]) - pred)
                    for row, pred in zip(metrics.get("_complete", []), metrics.get("_prediction", []))}
            by_slug = defaultdict(list)
            b_rows = base.get("_complete", [])
            m_rows = metrics.get("_complete", [])
            b_by_key = {(r["market_slug"], r["observed_ts"]): abs(float(r["future_mid_change"]) - p)
                        for r, p in zip(b_rows, base.get("_prediction", []))}
            m_by_key = {(r["market_slug"], r["observed_ts"]): abs(float(r["future_mid_change"]) - p)
                        for r, p in zip(m_rows, metrics.get("_prediction", []))}
            for key in b_by_key.keys() & m_by_key.keys():
                if key[0] in common:
                    by_slug[key[0]].append(m_by_key[key] - b_by_key[key])
            deltas = [v for values in by_slug.values() for v in values]
            ci = _cluster_mean_ci([{"market_slug": slug, "delta": delta}
                                   for slug, values in by_slug.items() for delta in values], "delta")
            out.append({"horizon_sec": horizon, "model": name + "_vs_M1_paired_mae_delta",
                        "features": "paired common observations", "n_observations": len(deltas),
                        "n_markets": len(by_slug), "mae_delta_vs_mid_only": statistics.mean(deltas) if deltas else None,
                        "mae_delta_ci_low": ci[1], "mae_delta_ci_high": ci[2],
                        "evaluation": "negative favors added predictor; market-cluster bootstrap; descriptive only"})
    return out


def _residual_bins(rows):
    result = []
    for low, high, name in RESIDUAL_BINS:
        subset = [row for row in rows if low <= float(row["residual_up"]) < high]
        for horizon in HORIZONS:
            group = [row for row in subset if row.get(f"mid_change_{horizon}s") is not None]
            changes = [float(row[f"mid_change_{horizon}s"]) for row in group]
            directional = [((float(row["residual_up"]) > 0) == (float(row[f"mid_change_{horizon}s"]) > 0))
                           for row in group if float(row["residual_up"]) != 0 and float(row[f"mid_change_{horizon}s"]) != 0]
            mean, lo, hi, n_markets = _cluster_mean_ci(
                [{"market_slug": r["market_slug"], "change": r[f"mid_change_{horizon}s"]} for r in group], "change")
            result.append({"residual_bin": name, "horizon_sec": horizon, "n_observations": len(group),
                           "n_unique_markets": n_markets, "mean_future_mid_change": mean,
                           "median_future_mid_change": statistics.median(changes) if changes else None,
                           "directional_hit_rate": statistics.mean(directional) if directional else None,
                           "mean_cluster_bootstrap_ci_low": lo, "mean_cluster_bootstrap_ci_high": hi,
                           "bootstrap_unit": "market_slug"})
    return result


def _track_a(twap_by_slug, btc):
    observations, data_quality = [], []
    for slug, raw_rows in twap_by_slug.items():
        ordered = sorted(raw_rows, key=lambda r: r["observed_ts"])
        joint = [row for row in ordered if _valid_joint(row)]
        pex = [row for row in ordered if pf._valid_model(row)]
        mids = [row for row in ordered if pf._fresh_mid(row)]
        intervals = [b["observed_ts"] - a["observed_ts"] for a, b in zip(pex, pex[1:])
                     if 0 < b["observed_ts"] - a["observed_ts"] < 300]
        for row in joint:
            p = float(row["p_up_ex_market"])
            mid = float(pf._market_mid(row))
            ts = float(row["observed_ts"])
            returns = pf._btc_returns(btc, ts)
            current = {"market_slug": slug, "timestamp": ts, "observed_ts": ts,
                       "time_left_sec": row.get("time_left_sec"),
                       "p_up_ex_market": p, "p_down_ex_market": 1 - p,
                       "market_mid_up": mid, "market_mid_down": 1 - mid,
                       "residual_up": p - mid, "residual_down": (1 - p) - (1 - mid),
                       "best_bid_up": row.get("best_bid_up"), "best_ask_up": row.get("best_ask_up"),
                       "spread_up": (float(row["best_ask_up"]) - float(row["best_bid_up"])
                                     if row.get("best_ask_up") is not None and row.get("best_bid_up") is not None else None),
                       "btc_spot": row.get("path_spot"), "strike": row.get("strike"),
                       "spot_minus_strike_bps": (10000 * (float(row["path_spot"]) / float(row["strike"]) - 1)
                                                  if row.get("path_spot") is not None and row.get("strike") else None),
                       **returns, "required_move_sigma": row.get("required_move_sigma"),
                       "required_move_bps": row.get("required_move_bps"),
                       "settlement_state_side": row.get("settlement_state_side"),
                       "p_ex_sigma_age_sec": row.get("sigma_ex_market_age_sec"),
                       "market_quote_source_age_sec": row.get("market_bbo_up_source_age_sec"),
                       "current_quote_age_sec": row.get("market_age_source_sec")}
            observations.append(current)
        data_quality.append({"market_slug": slug, "twap_rows": len(ordered), "valid_p_ex_observations": len(pex),
                             "fresh_market_quote_observations": len(mids), "fresh_joint_observations": len(joint),
                             "median_p_ex_interval_sec": statistics.median(intervals) if intervals else None,
                             "p95_p_ex_interval_sec": (sorted(intervals)[min(len(intervals)-1, int(.95*(len(intervals)-1)))] if intervals else None),
                             "entry_nearest_fresh_p_ex_age_sec": None,
                             "strict_fresh_p_ex_within_2s_at_entry": None,
                             "note": "joint rows require p_ex sigma/source validity and fresh market mid; stale/missing rows are excluded observation-by-observation"})
    observations.sort(key=lambda r: (r["market_slug"], r["timestamp"]))
    by_slug = defaultdict(list)
    for row in observations:
        by_slug[row["market_slug"]].append(row)
    for current in observations:
        rows = by_slug[current["market_slug"]]
        for horizon in HORIZONS:
            future = _future_match(rows, current["timestamp"] + horizon, current["timestamp"])
            current[f"mid_change_{horizon}s"] = (float(future["market_mid_up"]) - current["market_mid_up"] if future else None)
            current[f"actual_horizon_{horizon}s"] = (future["timestamp"] - current["timestamp"] if future else None)
            current[f"future_quote_age_{horizon}s"] = future.get("market_quote_source_age_sec") if future else None
            for field in ("best_bid_up", "best_ask_up"):
                    current[f"{field}_change_{horizon}s"] = (float(future[field]) - float(current[field])
                                                            if future and future.get(field) is not None and current.get(field) is not None else None)
    model_rows = [{**row, "horizon_sec": horizon, "future_mid_change": row.get(f"mid_change_{horizon}s")}
                  for row in observations for horizon in HORIZONS
                  if row.get(f"mid_change_{horizon}s") is not None]
    return observations, _residual_bins(observations), _repricing_models(model_rows), data_quality


def _load_entries(journal, twap_db, btc_dir, output, slugs, entries_csv=None):
    if entries_csv:
        with Path(entries_csv).open(encoding="utf-8", newline="") as handle:
            entries = list(csv.DictReader(handle))
        return [row for row in entries if row.get("slug") in set(slugs)]
    with tempfile.TemporaryDirectory(prefix="residual-fragility-base-") as temp:
        pf.build_analysis(journal, twap_db, btc_dir, Path(temp), tuple(slugs))
        with (Path(temp) / "entries.csv").open(encoding="utf-8", newline="") as handle:
            entries = list(csv.DictReader(handle))
    return entries


def _entry_fresh_context(entry, twap_rows, empirical_rows):
    ts = float(entry["entry_ts"])
    side = entry.get("side")
    p_candidates = [row for row in twap_rows if row["observed_ts"] <= ts
                    and ts - row["observed_ts"] <= FRESH_SEC and pf._valid_model(row)]
    quote_age_key = "market_bbo_up_source_age_sec" if side == "UP" else "market_bbo_down_source_age_sec"
    quote_candidates = [row for row in twap_rows if row["observed_ts"] <= ts
                        and ts - row["observed_ts"] <= FRESH_SEC
                        and _number(row.get(quote_age_key)) is not None
                        and 0 <= float(row[quote_age_key]) <= FRESH_SEC
                        and _number(row.get("market_mid_probability_up")) is not None]
    p_row = p_candidates[-1] if p_candidates else None
    quote_row = quote_candidates[-1] if quote_candidates else None
    p_up = float(p_row["p_up_ex_market"]) if p_row else None
    p_side = p_up if side == "UP" else (1 - p_up if p_up is not None else None)
    mid_up = float(pf._market_mid(quote_row)) if quote_row else None
    mid_side = mid_up if side == "UP" else (1 - mid_up if mid_up is not None else None)
    ask_key = "best_ask_up" if side == "UP" else "best_ask_down"
    ask = _number(quote_row.get(ask_key)) if quote_row else None
    edge = p_side - ask if p_side is not None and ask is not None else None
    edge_source = "p_ex_minus_fresh_ask" if edge is not None else None
    if edge is None and p_side is not None and mid_side is not None:
        edge, edge_source = p_side - mid_side, "p_ex_minus_fresh_mid_fallback"
    p_age = ts - float(p_row["observed_ts"]) if p_row else None
    quote_age = ts - float(quote_row["observed_ts"]) if quote_row else None
    synchronized = (p_side is not None and mid_side is not None and p_age is not None and quote_age is not None
                    and max(p_age, quote_age) <= FRESH_SEC)
    empirical = None
    if empirical_rows:
        eligible = [r for r in empirical_rows if r.get("market_slug") == entry["slug"]
                    and abs((_number(r.get("observed_ts")) or 0) - ts) <= FRESH_SEC
                    and r.get("settlement_reference_is_canonical") == "True"
                    and r.get("empirical_flip_probability") not in (None, "")]
        if eligible:
            empirical = float(min(eligible, key=lambda r: abs(float(r["observed_ts"]) - ts))["empirical_flip_probability"])
    return {"fresh_p_ex_side": p_side, "fresh_p_ex_up": p_up,
            "fresh_market_mid_side": mid_side, "fresh_market_mid_up": mid_up,
            "fresh_ask_side": ask, "entry_edge": edge, "entry_edge_source": edge_source,
            "entry_p_ex_ts": p_row.get("observed_ts") if p_row else None,
            "entry_quote_ts": quote_row.get("observed_ts") if quote_row else None,
            "entry_p_ex_age_sec": p_age, "entry_quote_observation_age_sec": quote_age,
            "entry_p_ex_quote_synchronized": synchronized,
            "entry_feature_ts": p_row.get("observed_ts") if p_row else None,
            "entry_feature_age_sec": p_age,
            "analytic_opposite_flip_probability": 1 - p_side if p_side is not None else None,
            "empirical_opposite_flip_probability": empirical,
            "opposite_probability_source": "canonical exact-path replay" if empirical is not None else
                ("fresh p_ex complement" if p_side is not None else "unavailable")}


def _fragility(entries, twap_by_slug, empirical_rows):
    result, matrix = [], []
    for entry in entries:
        slug, side = entry["slug"], entry.get("side")
        twap_rows = twap_by_slug.get(slug, [])
        context = _entry_fresh_context(entry, twap_rows, empirical_rows)
        sigma = _number(entry.get("required_move_sigma"))
        sigma_abs = abs(sigma) if sigma is not None else None
        returns = {h: _number(entry.get(f"btc_return_{h}s_bps")) for h in (5, 10, 30, 60)}
        flips = _number(entry.get("active_side_flips_last_60s"))
        edge = context["entry_edge"]
        flags = {"low_flip_sigma": sigma_abs < 1.0 if sigma_abs is not None else None,
                 "btc_disagrees_10s": ((returns[10] < 0) if side == "UP" else (returns[10] > 0)) if returns[10] is not None else None,
                 "side_unstable_60s": flips > 0 if flips is not None else None,
                 "thin_fair_value_edge": edge <= .02 if edge is not None else None}
        flags_available = sum(value is not None for value in flags.values())
        flag_count = sum(value is True for value in flags.values())
        fill_mid_side = _number(entry.get("market_side_probability")) if entry.get("market_mid_fresh") == "True" else None
        quote_rows = [{**row, "fresh": True,
                       "held_mid": (float(pf._market_mid(row)) if side == "UP" else 1-float(pf._market_mid(row)))}
                      for row in twap_rows if pf._fresh_mid(row)]
        adverse_labels = {}
        for horizon in (5, 10, 30, 60):
            future = _future_match(quote_rows, float(entry["entry_ts"]) + horizon,
                                   float(entry["entry_ts"]), tolerance=1.5)
            change = float(future["held_mid"]) - fill_mid_side if future and fill_mid_side is not None else None
            adverse_labels[f"future_adverse_mid_change_{horizon}s"] = min(0.0, change) if change is not None else None
            adverse_labels[f"actual_horizon_{horizon}s"] = float(future["observed_ts"]) - float(entry["entry_ts"]) if future else None
        outcome = entry.get("outcome")
        item = {"market_slug": slug, "entry_ts": entry.get("entry_ts"), "side": side,
                "entry_price": _number(entry.get("entry_price")), "settlement": outcome,
                "result": "WIN" if entry.get("won") == "True" else "LOSS",
                "simulated_pnl_usdc": _number(entry.get("simulated_pnl_usdc")),
                "time_left_sec": _number(entry.get("time_left_sec")),
                "market_mid_side_at_fill": _number(entry.get("market_side_probability")),
                "fresh_p_ex_side": context["fresh_p_ex_side"],
                "p_ex_freshness_status": "FRESH" if context["fresh_p_ex_side"] is not None else "MISSING_OR_STALE",
                "entry_edge_status": "VERIFIED" if context["entry_edge"] is not None else "UNVERIFIED_EDGE_AT_ENTRY",
                "entry_p_ex_quote_synchronized": context["entry_p_ex_quote_synchronized"],
                "nearest_prior_p_ex_side_within_12s": _number(entry.get("p_ex_side_within_12s")),
                "nearest_prior_p_ex_side_age_sec": _number(entry.get("p_ex_last_observation_age_sec")),
                "internal_fair_at_entry": _number(entry.get("fair")),
                "internal_fair_minus_entry_price": (_number(entry.get("fair")) - _number(entry.get("entry_price"))
                                                     if _number(entry.get("fair")) is not None and _number(entry.get("entry_price")) is not None else None),
                "fresh_market_mid_side": context["fresh_market_mid_side"],
                "fresh_ask_side": context["fresh_ask_side"],
                "p_ex_minus_ask_or_mid": context["entry_edge"],
                "edge_source": context["entry_edge_source"],
                "entry_feature_age_sec": context["entry_feature_age_sec"],
                "required_move_sigma": sigma, "opposite_flip_sigma_abs": sigma_abs,
                "opposite_flip_sigma_bin": _bin(sigma_abs, SIGMA_BINS),
                "analytic_opposite_flip_probability": context["analytic_opposite_flip_probability"],
                "empirical_opposite_flip_probability": context["empirical_opposite_flip_probability"],
                "btc_return_5s_bps": returns[5], "btc_return_10s_bps": returns[10],
                "btc_return_30s_bps": returns[30], "btc_return_60s_bps": returns[60],
                "btc_disagreement_5s": flags["btc_disagrees_10s"] if returns[5] is None else ((returns[5] < 0) if side == "UP" else (returns[5] > 0)),
                "btc_disagreement_10s": flags["btc_disagrees_10s"],
                "btc_disagreement_30s": (((returns[30] < 0) if side == "UP" else (returns[30] > 0)) if returns[30] is not None else None),
                "side_flips_30s": _number(entry.get("active_side_flips_last_30s")),
                "side_flips_60s": flips, "side_flips_120s": _number(entry.get("active_side_flips_last_120s")),
                "signal_confidence": _number(entry.get("side_score")),
                **flags, "fragility_flags_available": flags_available,
                "number_of_fragility_flags": flag_count,
                "flag_count_bin": str(flag_count) if flag_count < 3 else "3+",
                **adverse_labels,
                "diagnostic_only_not_live_score": True}
        result.append(item)
        matrix.append({"market_slug": slug, "entry_side": side, **flags,
                       "fragility_flags_available": flags_available, "number_of_fragility_flags": flag_count,
                       "result": item["result"], "simulated_pnl_usdc": item["simulated_pnl_usdc"],
                       **adverse_labels,
                       "diagnostic_only_not_live_score": True})
    return result, matrix


def _group_rows(rows, feature, bins=None):
    groups = defaultdict(list)
    for row in rows:
        value = row.get(feature)
        label = ("UNAVAILABLE" if value is None else
                 _bin(abs(float(value)), bins) if bins is SIGMA_BINS else
                 _bin(float(value), bins) if bins is not None else str(value))
        groups[label].append(row)
    output = []
    for label, group in groups.items():
        pnl = [_number(r.get("simulated_pnl_usdc")) for r in group]
        pnl = [x for x in pnl if x is not None]
        output.append({"feature": feature, "bin": label, "entries": len(group),
                       "wins": sum(r["result"] == "WIN" for r in group),
                       "losses": sum(r["result"] == "LOSS" for r in group),
                       "win_rate": sum(r["result"] == "WIN" for r in group) / len(group) if group else None,
                       "mean_simulated_pnl_usdc": statistics.mean(pnl) if pnl else None,
                       "mean_future_adverse_mid_move_5s": _mean_available(group, "future_adverse_mid_change_5s"),
                       "mean_future_adverse_mid_move_10s": _mean_available(group, "future_adverse_mid_change_10s"),
                       "mean_future_adverse_mid_move_30s": _mean_available(group, "future_adverse_mid_change_30s"),
                       "mean_future_adverse_mid_move_60s": _mean_available(group, "future_adverse_mid_change_60s"),
                       "note": "descriptive cohort counts; repeated markets/candidate observations are not independent"})
    return output


def _mean_available(rows, key):
    values = [_number(row.get(key)) for row in rows]
    values = [value for value in values if value is not None]
    return statistics.mean(values) if values else None


def _known_loser_timeline(slug, entry, twap_rows, btc):
    entry_ts = float(entry["entry_ts"])
    side = entry["side"]
    rows = []
    for item in twap_rows:
        ts = float(item["observed_ts"])
        if ts < entry_ts:
            continue
        p_up = _number(item.get("p_up_ex_market")) if pf._valid_model(item) else None
        mid_up = _number(pf._market_mid(item)) if pf._fresh_mid(item) else None
        p_side = p_up if side == "UP" else (1 - p_up if p_up is not None else None)
        mid_side = mid_up if side == "UP" else (1 - mid_up if mid_up is not None else None)
        rows.append({"market_slug": slug, "seconds_from_entry": ts - entry_ts,
                     "observed_ts": ts, "held_side": side,
                     "fresh_p_ex_side": p_side, "fresh_market_mid_side": mid_side,
                     "p_ex_sigma_age_sec": item.get("sigma_ex_market_age_sec"),
                     "market_quote_source_age_sec": item.get("market_bbo_up_source_age_sec"),
                     "settlement_state_side": item.get("settlement_state_side"),
                     "path_spot": item.get("path_spot"), "strike": item.get("strike"),
                     "required_move_sigma": item.get("required_move_sigma"),
                     **pf._btc_returns(btc, ts)})
    return rows


def _telemetry_audit(entries, twap_by_slug):
    rows = []
    for entry in entries:
        slug = entry["slug"]
        ts = float(entry["entry_ts"])
        raw = sorted(twap_by_slug.get(slug, []), key=lambda r: r["observed_ts"])
        model = [r for r in raw if pf._valid_model(r) and r["observed_ts"] <= ts]
        nearest_age = ts - float(model[-1]["observed_ts"]) if model else None
        intervals = [b["observed_ts"] - a["observed_ts"] for a, b in zip(model, model[1:])
                     if 0 < b["observed_ts"] - a["observed_ts"] < 300]
        quote_fresh = bool(raw and any(r["observed_ts"] <= ts and ts-r["observed_ts"] <= FRESH_SEC and pf._fresh_mid(r) for r in raw))
        rows.append({"market_slug": slug, "entry_ts": ts, "twap_rows": len(raw),
                     "valid_p_ex_rows_before_entry": len(model),
                     "nearest_valid_p_ex_age_sec": nearest_age,
                     "p_ex_within_2s": nearest_age is not None and nearest_age <= FRESH_SEC,
                     "median_valid_p_ex_interval_sec": statistics.median(intervals) if intervals else None,
                     "p95_valid_p_ex_interval_sec": sorted(intervals)[min(len(intervals)-1, int(.95*(len(intervals)-1)))] if intervals else None,
                       "fresh_twap_market_mid_within_2s": quote_fresh,
                       "simulated_fill_quote_fresh": entry.get("quote_freshness_tier") == "FRESH"
                           and (_number(entry.get("quote_age_sec")) is not None
                                and 0 <= float(entry["quote_age_sec"]) <= FRESH_SEC),
                     "entry_snapshot_quote_age_sec": _number(entry.get("quote_age_sec")),
                     "entry_snapshot_freshness_tier": entry.get("quote_freshness_tier"),
                     "likely_reason_for_missing_strict_p_ex": (
                         "NO_VALID_P_EX_BEFORE_ENTRY" if not model else
                         "OBSERVATION_CADENCE_OR_EVENT_GAP" if nearest_age > FRESH_SEC else
                         "STRICT_MATCH_AVAILABLE"),
                     "diagnosis_caveat": "event log alone cannot distinguish calculator cadence from selective persistence when no evaluation event was emitted"})
    return rows


def _summary(track_a, bins, models, entries, fragility, matrix, telemetry, loser_timelines, quality):
    horizons = []
    for horizon in HORIZONS:
        rows = [r for r in track_a if r.get(f"mid_change_{horizon}s") is not None]
        corr = pf._corr([float(r["residual_up"]) for r in rows], [float(r[f"mid_change_{horizon}s"]) for r in rows])
        count = len(rows); markets = len({r["market_slug"] for r in rows})
        horizons.append({"horizon": horizon, "observations": count, "markets": markets, "correlation": corr})
    fresh_entry_n = sum(row["fresh_p_ex_side"] is not None for row in fragility)
    synchronized_entry_n = sum(bool(row.get("entry_p_ex_quote_synchronized")) for row in fragility)
    strict_entry_count = sum(bool(row["p_ex_within_2s"]) for row in telemetry)
    def verdict(h):
        x = next(r for r in horizons if r["horizon"] == h)
        return "NOT MEASURABLE" if x["markets"] < 5 or x["correlation"] is None else "MIXED"
    loss = [r for r in fragility if r["result"] == "LOSS"]
    win = [r for r in fragility if r["result"] == "WIN"]
    def flag_count(group, key):
        vals = [r[key] for r in group if r.get(key) is not None]
        return f"{sum(value is True for value in vals)}/{len(vals)}" if vals else "0/0 unavailable"
    a_lines = "\n".join(f"| {h['horizon']}s | {h['observations']} | {h['markets']} | {h['correlation'] if h['correlation'] is not None else 'NA'} | {verdict(h['horizon'])} |" for h in horizons)
    track_b_lines = "\n".join(f"| {label} | {flag_count(loss,key)} | {flag_count(win,key)} |" for label,key in (("Low flip sigma <1σ", "low_flip_sigma"),("10s BTC disagreement","btc_disagrees_10s"),("Any side flip in 60s","side_unstable_60s"),("Thin edge <=0.02","thin_fair_value_edge")))
    positive30 = next((r for r in bins if r["residual_bin"] == ">=+0.10" and r["horizon_sec"] == 30), None)
    adjacent30 = next((r for r in bins if r["residual_bin"] == "+0.05_to_+0.10" and r["horizon_sec"] == 30), None)
    model30 = next((r for r in models if r.get("horizon_sec") == 30 and r.get("model") == "M3_mid_plus_residual_vs_M1_paired_mae_delta"), None)
    available_sigma = sorted((r["opposite_flip_sigma_abs"], r["market_slug"]) for r in fragility
                             if r.get("opposite_flip_sigma_abs") is not None)
    loser_details = []
    for slug in ("btc-updown-15m-1790920800", "btc-updown-15m-1790939700"):
        entry = next((r for r in fragility if r["market_slug"] == slug), None)
        if not entry:
            loser_details.append(f"- `{slug}`: 未在目前資料中找到 entry。")
            continue
        timeline = [r for r in loser_timelines if r["market_slug"] == slug]
        def first(predicate):
            return next((r for r in timeline if predicate(r)), None)
        p_cross = first(lambda r: r.get("fresh_p_ex_side") is not None and r["fresh_p_ex_side"] < .5)
        m_cross = first(lambda r: r.get("fresh_market_mid_side") is not None and r["fresh_market_mid_side"] < .5)
        first_post_p = next((r.get("fresh_p_ex_side") for r in timeline if r.get("fresh_p_ex_side") is not None), None)
        p_drop = first(lambda r: first_post_p is not None and r.get("fresh_p_ex_side") is not None
                        and r["fresh_p_ex_side"] <= first_post_p - .05)
        rank = next((i + 1 for i, (_, market) in enumerate(available_sigma) if market == slug), None)
        details = (f"- `{slug}`: 嚴格 entry 前 fresh p_ex={entry.get('fresh_p_ex_side')}，edge={entry.get('p_ex_minus_ask_or_mid')} ({entry.get('edge_source') or '不可驗證'}); "
                   f"最近 prior p_ex={entry.get('nearest_prior_p_ex_side_within_12s')}、距 fill {entry.get('nearest_prior_p_ex_side_age_sec')}s; "
                   f"internal fair={entry.get('internal_fair_at_entry')} vs entry price {entry.get('entry_price')} "
                   f"(fair-price={entry.get('internal_fair_minus_entry_price')}); opposite sigma={entry.get('opposite_flip_sigma_abs')} "
                   f"(available sigma rank {rank}/{len(available_sigma) if rank else len(available_sigma)}).")
        if p_drop:
            details += f" First ≥5pp post-entry p_ex decline at +{p_drop['seconds_from_entry']:.1f}s."
        else:
            details += " No observed ≥5pp post-entry p_ex decline."
        if p_cross:
            details += f" Held-side p_ex <0.5 at +{p_cross['seconds_from_entry']:.1f}s."
        if m_cross:
            details += f" Fresh mid <0.5 at +{m_cross['seconds_from_entry']:.1f}s."
        loser_details.append(details)
    summary = f"""# Residual repricing and entry fragility — offline research

TRACK A verdict: **{verdict(30)}** (30s residual→mid repricing; descriptive only)
TRACK B verdict: **{'MIXED' if loss and win else 'NOT MEASURABLE'}** (loser-vs-winner fragility in the selected settled cohort)
Best candidate signal: **fresh p_ex − fresh market mid residual**, only as a research hypothesis; no live use justified.
Main instrumentation gap: **only {strict_entry_count}/{len(entries)} entries have a valid p_ex observation within 2.0s, and {synchronized_entry_n}/{len(entries)} have both p_ex and fresh market mid available at entry; executable ask/p_ex edge is unverified for {len(entries)-sum(r['entry_edge_status']=='VERIFIED' for r in fragility)}/{len(entries)}.**
Live change justified? **NO**

這份報告是離線、描述性分析，不是可交易 edge/PnL，也不是獨立同分布樣本。每秒觀測高度重疊，信賴區間與 bootstrap 以 market_slug 分群。資料不足不等於「沒有趨勢」；以下同時列方向性觀察與可辨識限制。

## Track A — residual repricing

`residual_up = p_up_ex_market - fresh_market_mid_up`。Current 與 future 都要求 p_ex/source 有效且 market quote source age 在 0–{FRESH_SEC:.1f}s；每個不合格 observation 單獨排除，不會丟掉整場。Future label 取目標 horizon ±1.5s 內最近的 fresh observation，實際 horizon 與 future quote age 均輸出。Mid 是市場 mid，不是可成交價格。

| Horizon | 有效配對觀測 | markets | corr(residual, future Δmid) | 判讀 |
|---|---:|---:|---:|---|
{a_lines}

具體趨勢：30s 的 `>=+0.10` residual bin 平均 mid 重估 **{positive30['mean_future_mid_change'] if positive30 else 'NA'}**（N={positive30['n_observations'] if positive30 else 0}, markets={positive30['n_unique_markets'] if positive30 else 0}, market-cluster CI {positive30['mean_cluster_bootstrap_ci_low'] if positive30 else 'NA'} to {positive30['mean_cluster_bootstrap_ci_high'] if positive30 else 'NA'}）；相鄰 `+0.05..+0.10` bin 平均 **{adjacent30['mean_future_mid_change'] if adjacent30 else 'NA'}**。這是大正 residual 對 30 秒重估可能有訊息的具體線索，但 bins 不呈全域單調，極端 bin 僅 {positive30['n_unique_markets'] if positive30 else 0} 個市場。mid-only 加 residual 的 30s paired MAE delta={model30.get('mae_delta_vs_mid_only') if model30 else 'NA'}（95% market-cluster CI {model30.get('mae_delta_ci_low') if model30 else 'NA'} to {model30.get('mae_delta_ci_high') if model30 else 'NA'}）；區間跨零，增量尚未證實。完整 bins／CI 在 `residual_bins.csv`；模型比較在 `residual_model_comparison.csv`。OLS 是 in-sample 描述，不可聲稱因果或泛化。

## Track B — entry fragility

| Fragility flag | Losses：命中／可用 | Winners：命中／可用 |
|---|---:|---:|
{track_b_lines}

判讀：**structural fragility=MIXED**（1790920800 的 0.558σ 是可用值中最低，但另一輸家缺值；而低於 1σ 的可用樣本也包含贏家）；**BTC 10s disagreement=YES 作為風險標記但非決策規則**（2/2 輸家、4/12 贏家）；**side instability=NOT MEASURABLE**（本 cohort 的 30/60/120s active-side flip 記錄缺失）；**thin fair-value edge=NOT MEASURABLE**（沒有任何 entry 同時取得 strict fresh p_ex 與可用 fresh ask/mid）。

`entry_fragility.csv` 保留固定 sigma bins、BTC 5/10/30/60s disagreement、30/60/120s side flips、entry fair margin 與可用性；`fragility_matrix.csv` 的 flag count 是診斷，不是 live score。低 sigma 使用既定固定 bins 的 `<1σ`（合併 `<0.5` 與 `0.5–1`）；thin margin 按固定 ≤0.02 描述，不做門檻搜尋。由於模擬 fill 沒有實際成交保證，edge 優先用 entry 前 2 秒內 p_ex 與 side-specific fresh ask；缺 ask 才用同步 fresh mid 並標記 fallback。舊 p_ex 不冒充 entry edge。Analytic opposite probability 是 fresh p_ex 的持倉側補數；empirical probability 僅接受 canonical exact-path replay 的時間匹配值。

### Losers versus winners

{track_b_lines}

上述比例只描述這 14 個 settled shadow fills；兩筆輸家不能支撐任何規則。`winner_loser_fragility.csv` 提供 fixed bins 分組；沒有充分樣本的 bins 保留小 N，不合併、不調參。

## Known loser cases

{chr(10).join(loser_details)}

兩案逐次 post-entry p_ex、mid、TWAP/spot 與 BTC returns 見 `known_loser_case_1790920800.csv`、`known_loser_case_1790939700.csv`。第一個已知 case 的 entry snapshot可能比 event feature 更同步；本報告不把 entry後首次穿越 0.5 解讀成可執行止損或最早可預測時點，只列第一筆觀測時間。

## Telemetry gap audit

嚴格 entry 時點缺 p_ex 的主因可由「最近 valid event 距 entry」與 p_ex 事件間隔（`telemetry_gap_audit.csv`）支持：1/14 在 2 秒內有 p_ex event，但同期 fresh market mid/ask 未形成 join；其餘 entries 的最近 p_ex 已超過 2 秒，其中多場 event 間隔中位數約 3–13 秒、p95 約 13–67 秒。這強烈指向資料事件 cadence/gap 與同步採樣限制；但只靠未寫入的 evaluation event，**不能再區分計算未觸發還是計算結果未被持久化**，所以不能完全歸因於 feed stale。模擬 fill quote 本身另有 freshness 記錄，多數是 fresh；不代表 p_ex 同時 fresh。整場 p_ex/market 覆蓋見 `data_quality.csv`。

## Verdict and next experiment

**CURRENT BEST PATH: E. insufficient telemetry** for a fair incremental-prediction claim. Track A contains visible descriptive relationship(s), especially at longer horizons if bins are directionally ordered, but only 14 markets and repeated observations make the cluster uncertainty decisive. Track B has risk-marker contrasts worth following, but only two losers and strict entry p_ex scarcity prevent fair winner/loser comparison. No live filter or stop change is justified.

Next experiments (maximum two):
1. Prospectively freeze the joined fresh residual → 5/10/30/60s fresh-mid repricing table and evaluate by whole-market/day blocks. Residual hypothesis is falsified if bins are not directionally monotonic or market-cluster CI stays centered around zero after independent market-days accrue.
2. Keep a shadow-only entry-fragility snapshot at simulated/real entry (p_ex, executable ask, required sigma, BTC returns, side flips) and compare future adverse repricing by fixed bins. Fragility hypothesis is falsified if the low-sigma/disagreement/thin-edge markers are equally common in winners and losers and show no association with subsequent adverse repricing.

Telemetry sufficiency: Track A **{'PARTIALLY' if any(r['observations'] for r in horizons) else 'NO'}** (some valid synchronized rows, small clustered market count); Track B **PARTIALLY** (entry/fill/shadow and BTC data exist, but only {fresh_entry_n}/{len(entries)} strict synchronized p_ex entry features and two losers).
"""
    return summary


def run(journal: Path, twap_db: Path, btc_dir: Path, output: Path, empirical_csv: Path | None = None,
        entries_csv: Path | None = None):
    if entries_csv:
        with Path(entries_csv).open(encoding="utf-8", newline="") as handle:
            cached_entries = list(csv.DictReader(handle))
        slugs = tuple(dict.fromkeys(row.get("slug") for row in cached_entries if row.get("slug")))
    else:
        slugs = pf._all_settled_slugs(journal)
        if not slugs:
            slugs = pf.DEFAULT_TARGET_SLUGS
    entries = _load_entries(journal, twap_db, btc_dir, output, slugs, entries_csv)
    if not entries:
        raise RuntimeError("No settled shadow entries found in the selected trade journal")
    slugs = {row["slug"] for row in entries}
    twap_by_slug = _load_twap(twap_db, slugs)
    start = min(float(pf._slug_start(slug)) for slug in slugs)
    end = max(float(pf._slug_start(slug)) + 900 for slug in slugs)
    btc_rows = load_btc_1s_history(start - 120, end + 1, btc_dir)
    btc = {int(row["ts_sec"]): row for row in btc_rows}
    empirical_rows = []
    if empirical_csv and empirical_csv.exists():
        with empirical_csv.open(encoding="utf-8", newline="") as handle:
            empirical_rows = list(csv.DictReader(handle))
    observations, bins, models, quality = _track_a(twap_by_slug, btc)
    fragility, matrix = _fragility(entries, twap_by_slug, empirical_rows)
    telemetry = _telemetry_audit(entries, twap_by_slug)
    for q in quality:
        item = next((r for r in telemetry if r["market_slug"] == q["market_slug"]), None)
        if item:
            q["entry_nearest_fresh_p_ex_age_sec"] = item["nearest_valid_p_ex_age_sec"]
            q["strict_fresh_p_ex_within_2s_at_entry"] = item["p_ex_within_2s"]
    winner_loser = []
    for feature in ("opposite_flip_sigma_abs", "btc_disagreement_5s", "btc_disagreement_10s", "btc_disagreement_30s",
                    "side_unstable_60s", "thin_fair_value_edge", "number_of_fragility_flags"):
        group_rows = _group_rows(fragility, feature, SIGMA_BINS if feature == "opposite_flip_sigma_abs" else None)
        winner_loser.extend(group_rows)
    loser_timeline_by_slug = {}
    for slug in ("btc-updown-15m-1790920800", "btc-updown-15m-1790939700"):
        entry = next((row for row in entries if row["slug"] == slug), None)
        if entry:
            loser_timeline_by_slug[slug] = _known_loser_timeline(slug, entry, twap_by_slug.get(slug, []), btc)
    all_loser_timeline = [row for rows in loser_timeline_by_slug.values() for row in rows]
    output.mkdir(parents=True, exist_ok=True)
    _csv(output / "residual_observations.csv", observations)
    future_rows = []
    for row in observations:
        for horizon in HORIZONS:
            if row.get(f"mid_change_{horizon}s") is not None:
                future_rows.append({"market_slug": row["market_slug"], "timestamp": row["timestamp"],
                                    "horizon_sec": horizon, "actual_horizon_sec": row[f"actual_horizon_{horizon}s"],
                                    "future_quote_age_sec": row[f"future_quote_age_{horizon}s"],
                                    "current_mid_up": row["market_mid_up"],
                                    "future_mid_change": row[f"mid_change_{horizon}s"],
                                    "best_bid_up_change": row.get(f"best_bid_up_change_{horizon}s"),
                                    "best_ask_up_change": row.get(f"best_ask_up_change_{horizon}s"),
                                    "residual_up": row["residual_up"], "btc_return_10s_bps": row.get("btc_return_10s_bps")})
    _csv(output / "future_repricing.csv", future_rows)
    _csv(output / "residual_bins.csv", bins)
    _csv(output / "residual_model_comparison.csv", models)
    _csv(output / "entry_fragility.csv", fragility)
    _csv(output / "fragility_matrix.csv", matrix)
    _csv(output / "winner_loser_fragility.csv", winner_loser)
    for slug, filename in (("btc-updown-15m-1790920800", "known_loser_case_1790920800.csv"),
                           ("btc-updown-15m-1790939700", "known_loser_case_1790939700.csv")):
        _csv(output / filename, loser_timeline_by_slug.get(slug, []))
    _csv(output / "telemetry_gap_audit.csv", telemetry)
    _csv(output / "data_quality.csv", quality)
    (output / "summary.md").write_text(_summary(observations, bins, models, entries, fragility, matrix,
                                                    telemetry, all_loser_timeline, quality), encoding="utf-8")
    return {"entries": len(entries), "markets": len(slugs), "observations": len(observations),
            "future_labels": len(future_rows), "strict_entry_pex": sum(r["p_ex_within_2s"] for r in telemetry)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=Path("logs/trade_journal.db"))
    parser.add_argument("--twap-db", type=Path, default=Path("data/research/twap_forward_shadow.db"))
    parser.add_argument("--btc-dir", type=Path, default=Path("data/btc_history_1s"))
    parser.add_argument("--empirical-csv", type=Path, default=Path("reports/required_path_probability/observation_probability.csv"))
    parser.add_argument("--entries-csv", type=Path, help="reuse an existing prediction_forensics entries.csv instead of rebuilding the journal replay")
    parser.add_argument("--output", type=Path, default=Path("reports/residual_fragility"))
    args = parser.parse_args()
    result = run(args.journal, args.twap_db, args.btc_dir, args.output, args.empirical_csv, args.entries_csv)
    print(f"residual/fragility research: {result} output={args.output}")


if __name__ == "__main__":
    main()
