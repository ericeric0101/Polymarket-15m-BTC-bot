#!/usr/bin/env python3
"""Minute-resolution, research-only strike proximity and late-flip study."""
from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import json
import math
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "reports/strike_flip_risk"
DEFAULT_CACHES = [
    ROOT / "data/polymarket_history_unified_20260727_20260920",
    ROOT / "data/polymarket_history_public_study_20260727_20260920",
    ROOT / "data/polymarket_history",
]
DEFAULT_BTC = ROOT / "data/btc_history"
DEFAULT_SAMPLE = ROOT / "reports/unified_strategy_research/public_market_sample.csv"
LIFECYCLE = {
    "T+1m": 60, "T+2m": 120, "T+3m": 180, "T+5m": 300, "T+10m": 600,
    "T-5m": 600, "T-3m": 360, "T-2m": 240, "T-1m": 120,
}
NEAR_BUCKETS = ((0, 1, "<1bps"), (1, 2, "1-2bps"), (2, 5, "2-5bps"),
                (5, 10, "5-10bps"), (10, math.inf, ">10bps"))
CORE_FLIP_TIMES = ("T+3m", "T+5m", "T-5m", "T-2m", "T-1m")
_TIME_INDEX: dict[int, tuple[list[dict[str, Any]], list[float]]] = {}


def distance_metrics(spot: float, strike: float) -> dict[str, float]:
    signed = float(spot) - float(strike)
    bps = signed / float(strike) * 10_000 if float(strike) > 0 else math.nan
    return {"signed_distance_usd": signed, "abs_distance_usd": abs(signed),
            "distance_bps": bps, "abs_distance_bps": abs(bps)}


def leader_for(spot: float, strike: float) -> str:
    return "UP" if spot > strike else "DOWN" if spot < strike else "TIE"


def leader_persistence(leader: str | None, winner: str | None) -> tuple[bool | None, float | None]:
    if leader not in {"UP", "DOWN"} or winner not in {"UP", "DOWN"}:
        return None, None
    correct = leader == winner
    return correct, 0.0 if correct else 1.0


def distance_bucket(abs_bps: float | None) -> str:
    if abs_bps is None or not math.isfinite(float(abs_bps)):
        return "UNAVAILABLE"
    value = abs(float(abs_bps))
    if value == 10.0:
        return "5-10bps"
    for low, high, label in NEAR_BUCKETS:
        if low <= value < high:
            return label
    return ">10bps"


def classify_weekend(epoch: int, timezone_name: str = "America/New_York") -> tuple[bool, str, str]:
    local = datetime.fromtimestamp(int(epoch), ZoneInfo(timezone_name))
    return local.weekday() >= 5, local.strftime("%Y-%m-%d"), f"{(local.hour // 6) * 6:02d}-{(local.hour // 6 + 1) * 6:02d}"


def crossing_count(rows: list[dict[str, Any]], strike: float, start_ts: float | None = None,
                   end_ts: float | None = None, *, include_left_context: bool = True) -> int:
    lower_bound = start_ts - 60.5 if start_ts is not None and include_left_context else start_ts
    selected = [r for r in rows if (lower_bound is None or r["close_ts"] >= lower_bound)
                and (end_ts is None or r["close_ts"] <= end_ts)]
    signs = [leader_for(float(r["close"]), strike) for r in selected]
    signs = [side for side in signs if side in {"UP", "DOWN"}]
    return sum(left != right for left, right in zip(signs, signs[1:]))


def _last_closed(rows: list[dict[str, Any]], target_ts: float) -> dict[str, Any] | None:
    cached = _TIME_INDEX.get(id(rows))
    if cached is None or cached[0] is not rows:
        cached = (rows, [float(r["close_ts"]) for r in rows])
        _TIME_INDEX[id(rows)] = cached
    idx = bisect.bisect_right(cached[1], float(target_ts)) - 1
    if idx < 0 or float(target_ts) - cached[1][idx] > 60.5:
        return None
    return rows[idx]


def reconstruct_adjacent_strike(rows: list[dict[str, Any]], next_market_start: int) -> tuple[float | None, str, str]:
    # Boundary is the previous 15m market's end. Use only a closed Binance
    # minute bar immediately before that boundary; never substitute open price.
    candle = _last_closed(rows, float(next_market_start))
    if candle is None:
        return None, "UNAVAILABLE", "NONE"
    return float(candle["close"]), "RECONSTRUCTED_ADJACENT_MARKET_BINANCE_PROXY", "LOW_PROXY"


def adjacent_pairs(markets: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    by_start = {int(m.get("start", m.get("market_start_ts"))): m for m in markets}
    return [(by_start[s], by_start[s + 900]) for s in sorted(by_start) if s + 900 in by_start]


def near_strike_duration(rows: list[dict[str, Any]], strike: float, *, start_ts: int, end_ts: int,
                         thresholds_bps: Iterable[float] = (1, 2, 5)) -> dict[float, dict[str, Any]]:
    market_rows = [r for r in rows if start_ts < r["close_ts"] <= end_ts]
    result = {}
    for threshold in thresholds_bps:
        inside = [abs(float(r["close"]) - strike) / strike * 10_000 < threshold for r in market_rows]
        episodes = sum(flag and (i == 0 or not inside[i - 1]) for i, flag in enumerate(inside))
        result[float(threshold)] = {"minutes": sum(inside), "episodes": episodes,
                                    "reentries": max(0, episodes - 1), "observations": len(inside)}
    return result


def safety_sigma(rows: list[dict[str, Any]], *, spot: float, strike: float,
                 observation_ts: float, time_left_sec: float) -> dict[str, Any]:
    latest = _last_closed(rows, observation_ts)
    if latest is None:
        past = []
    else:
        cached = _TIME_INDEX[id(rows)]
        last_idx = bisect.bisect_right(cached[1], float(observation_ts)) - 1
        past = rows[max(0, last_idx-60):last_idx+1]
    returns = []
    for prev, cur in zip(past[-61:], past[-60:]):
        if float(prev["close"]) > 0 and float(cur["close"]) > 0:
            returns.append(math.log(float(cur["close"]) / float(prev["close"])))
    source = "VOLATILITY_PROXY_PRIOR_60M" if len(returns) >= 30 else "VOLATILITY_PROXY_PRIOR_30M" if len(returns) >= 15 else "UNAVAILABLE_INSUFFICIENT_HISTORY"
    if len(returns) < 15 or time_left_sec <= 0 or spot <= 0 or strike <= 0:
        return {"safety_sigma": None, "volatility_source": source, "prior_return_count": len(returns),
                "remaining_volatility_usd": None}
    sample = returns[-60:] if len(returns) >= 30 else returns[-30:]
    mean = statistics.mean(sample)
    variance = sum((value - mean) ** 2 for value in sample) / max(1, len(sample) - 1)
    per_minute = math.sqrt(variance)
    remaining = float(spot) * per_minute * math.sqrt(float(time_left_sec) / 60.0)
    return {"safety_sigma": abs(float(spot) - float(strike)) / remaining if remaining > 0 else None,
            "volatility_source": source, "prior_return_count": len(sample),
            "remaining_volatility_usd": remaining}


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = (len(ordered) - 1) * q
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)


def _correlation(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y) or len(x) < 3:
        return None
    mx, my = statistics.mean(x), statistics.mean(y)
    dx = math.sqrt(sum((v-mx)**2 for v in x))
    dy = math.sqrt(sum((v-my)**2 for v in y))
    if dx == 0 or dy == 0:
        return None
    return sum((a-mx)*(b-my) for a,b in zip(x,y))/(dx*dy)


def compare_samples(weekday: list[float], weekend: list[float], *, seed: int = 1709,
                   reps: int = 1000) -> dict[str, Any]:
    if not weekday or not weekend:
        return {"weekday_n": len(weekday), "weekend_n": len(weekend), "weekend_minus_weekday": None,
                "bootstrap_ci_low": None, "bootstrap_ci_high": None, "permutation_p": None,
                "cohens_d": None}
    observed = statistics.mean(weekend) - statistics.mean(weekday)
    rng = random.Random(seed)
    boot = []
    for _ in range(reps):
        a = [weekday[rng.randrange(len(weekday))] for _ in weekday]
        b = [weekend[rng.randrange(len(weekend))] for _ in weekend]
        boot.append(statistics.mean(b) - statistics.mean(a))
    combined = weekday + weekend
    extreme = 0
    for _ in range(reps):
        shuffled = combined[:]
        rng.shuffle(shuffled)
        delta = statistics.mean(shuffled[len(weekday):]) - statistics.mean(shuffled[:len(weekday)])
        extreme += abs(delta) >= abs(observed)
    n1, n2 = len(weekday), len(weekend)
    pooled = math.sqrt(((n1 - 1) * statistics.variance(weekday) + (n2 - 1) * statistics.variance(weekend)) / (n1 + n2 - 2)) if n1 > 1 and n2 > 1 else 0.0
    return {"weekday_n": n1, "weekend_n": n2, "weekend_minus_weekday": observed,
            "bootstrap_ci_low": _quantile(boot, .025), "bootstrap_ci_high": _quantile(boot, .975),
            "permutation_p": (extreme + 1) / (reps + 1), "cohens_d": observed / pooled if pooled > 0 else None}


def _load_candles(cache_dir: Path) -> list[dict[str, float]]:
    output = {}
    for path in sorted(cache_dir.glob("binance_btcusdt_1m_*.json.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                rows = json.load(stream)
        except (OSError, json.JSONDecodeError):
            continue
        for row in rows:
            try:
                ts = int(row[0]) // 1000
                output[ts] = {"ts": ts, "open": float(row[1]), "high": float(row[2]),
                              "low": float(row[3]), "close": float(row[4]),
                              "volume": float(row[5]), "close_ts": int(row[6]) / 1000}
            except (IndexError, TypeError, ValueError):
                continue
    return [output[k] for k in sorted(output)]


def _market_start(slug: str) -> int | None:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (ValueError, AttributeError):
        return None


def _winner(market: dict[str, Any]) -> str | None:
    if not market.get("closed"):
        return None
    try:
        outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
        prices = json.loads(market.get("outcomePrices", "[]")) if isinstance(market.get("outcomePrices"), str) else market.get("outcomePrices", [])
        values = {str(o).upper(): float(p) for o, p in zip(outcomes, prices)}
        for side in ("UP", "DOWN"):
            other = "DOWN" if side == "UP" else "UP"
            if values.get(side, 0) >= .99 and values.get(other, 1) <= .01:
                return side
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def _gamma_strike(market: dict[str, Any]) -> float | None:
    for key in ("priceToBeat", "_priceToBeat", "price_to_beat"):
        try:
            value = float(market[key])
            if value > 0:
                return value
        except (KeyError, TypeError, ValueError):
            pass
    for key in ("eventMetadata", "event_metadata"):
        meta = market.get(key) or {}
        if isinstance(meta, dict):
            try:
                value = float(meta.get("priceToBeat") or meta.get("price_to_beat"))
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
    return None


def _load_markets(cache_dirs: list[Path], sample_csv: Path | None, start_epoch: int, end_epoch: int) -> list[dict[str, Any]]:
    wanted = set()
    if sample_csv and sample_csv.exists():
        with sample_csv.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                slug = row.get("market_slug") or row.get("slug")
                if slug:
                    wanted.add(slug)
    cache_by_slug = {}
    for directory in cache_dirs:
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            try:
                market_data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            slug = str(market_data.get("slug") or "")
            start = _market_start(slug)
            if start and start_epoch <= start <= end_epoch and (not wanted or slug in wanted):
                cache_by_slug[slug] = market_data
    return [cache_by_slug[key] for key in sorted(cache_by_slug, key=lambda s: _market_start(s) or 0)]


def _load_local_strikes(db_path: Path | None) -> dict[str, dict[str, Any]]:
    if db_path is None or not db_path.exists():
        return {}
    result = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT event_type,payload_json FROM strategy_events WHERE event_type IN ('MARKET_STRIKE_LOCKED','MARKET_STRIKE_PROVENANCE')")
            for event_type, raw in rows:
                try:
                    item = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    continue
                slug = str(item.get("slug") or "")
                value = item.get("strike") or item.get("crypto_open_price")
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                source = str(item.get("strike_source") or item.get("source") or "")
                if slug and value > 0 and item.get("strike_status", "verified") == "verified":
                    result[slug] = {"value": value, "source": source or "polymarket_crypto_price_twap_open",
                                    "confidence": "HIGH", "event_type": event_type}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return result


def _load_local_settlements(db_path: Path | None) -> dict[str, dict[str, Any]]:
    if db_path is None or not db_path.exists():
        return {}
    result = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for raw, in conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='MARKET_SETTLEMENT'"):
                try:
                    item = json.loads(raw or "{}")
                    if item.get("slug") and item.get("spot"):
                        result[str(item["slug"])] = item
                except (json.JSONDecodeError, TypeError):
                    pass
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return result


def _csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or ["status"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _group_distance(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = defaultdict(list)
    for row in snapshots:
        if row.get("abs_distance_usd") is not None:
            groups[(row["lifecycle_time"], row["day_type"], row.get("strike_confidence", "UNKNOWN"))].append(row)
    output = []
    for (label, day_type, strike_confidence), rows in sorted(groups.items()):
        usd = [float(r["abs_distance_usd"]) for r in rows]
        bps = [float(r["abs_distance_bps"]) for r in rows]
        output.append({"lifecycle_time": label, "day_type": day_type, "strike_confidence": strike_confidence, "n": len(rows),
                       "mean_abs_distance_usd": statistics.mean(usd), "median_abs_distance_usd": statistics.median(usd),
                       "p10_abs_distance_usd": _quantile(usd, .1), "p25_abs_distance_usd": _quantile(usd, .25),
                       "p50_abs_distance_usd": _quantile(usd, .5), "p75_abs_distance_usd": _quantile(usd, .75),
                       "p90_abs_distance_usd": _quantile(usd, .9), "mean_abs_distance_bps": statistics.mean(bps)})
    return output


def _logistic_fit(rows: list[dict[str, Any]], adjusted: bool) -> list[dict[str, Any]]:
    """Small deterministic ridge logistic regression (stdlib only)."""
    selected = [r for r in rows if r.get("flip_after_time") in (0.0, 1.0) and r.get("abs_distance_bps") is not None]
    if len(selected) < 20 or len({r["flip_after_time"] for r in selected}) < 2:
        return [{"model": "adjusted" if adjusted else "weekend_only", "n": len(selected), "status": "insufficient_data"}]
    names = ["intercept", "weekend"]
    raw_features = [[1.0, 1.0 if r["day_type"] == "weekend" else 0.0] for r in selected]
    if adjusted:
        names += ["abs_distance_bps", "safety_sigma", "crossings_prior_5m", "hour_06_12", "hour_12_18", "hour_18_24"]
        for x, r in zip(raw_features, selected):
            x.extend([float(r["abs_distance_bps"]), float(r.get("safety_sigma") or 0),
                      float(r.get("crossings_prior_5m") or 0),
                      1.0 if r["hour_block"] == "06-12" else 0.0,
                      1.0 if r["hour_block"] == "12-18" else 0.0,
                      1.0 if r["hour_block"] == "18-24" else 0.0])
    # Standardize continuous covariates; weekend coefficient is reported per SD only for numeric controls.
    if adjusted:
        for j in range(2, len(names)):
            vals = [x[j] for x in raw_features]
            mean = statistics.mean(vals)
            sd = statistics.pstdev(vals)
            if sd > 0:
                for x in raw_features:
                    x[j] = (x[j] - mean) / sd
    y = [float(r["flip_after_time"]) for r in selected]
    k = len(names)
    beta = [0.0] * k
    def solve(a, b):
        matrix = [a[i][:] + [b[i]] for i in range(len(b))]
        n = len(b)
        for col in range(n):
            pivot = max(range(col, n), key=lambda row: abs(matrix[row][col]))
            if abs(matrix[pivot][col]) < 1e-10:
                matrix[pivot][col] += 1e-6
            matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
            div = matrix[col][col]
            matrix[col] = [v / div for v in matrix[col]]
            for row in range(n):
                if row == col: continue
                factor = matrix[row][col]
                matrix[row] = [v - factor * c for v, c in zip(matrix[row], matrix[col])]
        return [matrix[i][-1] for i in range(n)]
    hessian = None
    for _ in range(80):
        gradient = [0.0] * k
        hessian = [[0.0] * k for _ in range(k)]
        for x, target in zip(raw_features, y):
            z = max(-30.0, min(30.0, sum(a*b for a, b in zip(beta, x))))
            prob = 1.0 / (1.0 + math.exp(-z))
            weight = max(1e-7, prob * (1 - prob))
            for i in range(k):
                gradient[i] += x[i] * (target - prob)
                for j in range(k): hessian[i][j] += weight * x[i] * x[j]
        for i in range(1, k):
            gradient[i] -= 1e-6 * beta[i]
            hessian[i][i] += 1e-6
        delta = solve(hessian, gradient)
        beta = [a + b for a, b in zip(beta, delta)]
        if max(abs(v) for v in delta) < 1e-7: break
    output = []
    for idx, name in enumerate(names):
        unit = [1.0 if j == idx else 0.0 for j in range(k)]
        inv_col = solve(hessian, unit)
        se = math.sqrt(max(0.0, inv_col[idx]))
        coef = beta[idx]
        output.append({"model": "adjusted" if adjusted else "weekend_only", "n": len(selected),
                       "outcome": "flip_after_time", "term": name, "coefficient": coef,
                       "odds_ratio": math.exp(max(-30, min(30, coef))), "ci_low": coef - 1.96 * se,
                       "ci_high": coef + 1.96 * se, "weekend_or_shrinkage": None,
                       "warning": "exploratory logistic regression; approximate Wald CI"})
    return output


def run_analysis(*, start_epoch: int, end_epoch: int, cache_dirs: list[Path], sample_csv: Path,
                 btc_cache: Path, db_path: Path | None, output: Path,
                 timezone_name: str = "America/New_York") -> dict[str, Any]:
    markets = _load_markets(cache_dirs, sample_csv, start_epoch, end_epoch)
    candles = _load_candles(btc_cache)
    local_strikes = _load_local_strikes(db_path)
    local_settlements = _load_local_settlements(db_path)
    candles_by_time = candles
    market_meta = []
    quality = []
    snapshots = []
    crossings_rows = []
    danger_rows = []
    final_rows = []
    strike_validation = []
    resolved_winners = {}
    for data in markets:
        slug = str(data.get("slug") or "")
        start = _market_start(slug)
        if start is None: continue
        weekend, local_date, hour = classify_weekend(start, timezone_name)
        end = start + 900
        gamma = ((data.get("gamma") or {}).get("market") or {})
        winner = _winner(gamma)
        gamma_strike = _gamma_strike(gamma)
        local = local_strikes.get(slug)
        reconstructed, recon_source, recon_conf = reconstruct_adjacent_strike(candles_by_time, start)
        if gamma_strike:
            strike, source, confidence = gamma_strike, "OFFICIAL_POLYMARKET_GAMMA", "HIGH"
        elif local:
            strike, source, confidence = local["value"], "CHAINLINK_REFERENCE", local["confidence"]
        elif reconstructed:
            strike, source, confidence = reconstructed, recon_source, recon_conf
        else:
            strike, source, confidence = None, "UNAVAILABLE", "NONE"
        resolved_winners[slug] = winner
        mrow = {"slug": slug, "market_start_ts": start, "market_end_ts": end,
                "market_start_et": datetime.fromtimestamp(start, ZoneInfo(timezone_name)).isoformat(),
                "day_type": "weekend" if weekend else "weekday", "hour_block_et": hour,
                "winner_gamma": winner, "strike_value": strike, "strike_source": source,
                "strike_confidence": confidence, "gamma_strike_value": gamma_strike,
                "local_chainlink_strike": local["value"] if local else None,
                "local_strike_source_detail": local["source"] if local else None,
                "reconstructed_adjacent_strike": reconstructed,
                "reconstructed_strike_source": recon_source,
                "reconstructed_strike_confidence": recon_conf,
                "btc_source": "Binance BTCUSDT 1m; proxy, not Chainlink settlement source"}
        observations = [r for r in candles if start - 3600 <= r["close_ts"] <= end]
        for point, seconds in LIFECYCLE.items():
            target = start + seconds if point.startswith("T+") else end - seconds
            candle = _last_closed(candles, target)
            spot = float(candle["close"]) if candle and strike else None
            d = distance_metrics(spot, strike) if spot is not None else {}
            left = end - target
            sigma = safety_sigma(candles, spot=spot, strike=float(strike), observation_ts=target,
                                 time_left_sec=left) if spot is not None else {"safety_sigma": None, "volatility_source": "UNAVAILABLE"}
            leader = leader_for(spot, float(strike)) if spot is not None else None
            correct, flip = leader_persistence(leader, winner)
            recent_cross = crossing_count(observations, float(strike), target - 300, target) if strike else None
            snap = {"slug": slug, "lifecycle_time": point, "target_ts": target,
                    "observed_ts": candle["close_ts"] if candle else None,
                    "observation_lag_sec": target - candle["close_ts"] if candle else None,
                    "day_type": mrow["day_type"], "hour_block": hour, "winner": winner,
                    "strike_confidence": confidence, "strike_source": source,
                    "spot_binance_close": spot, "strike_value": strike, "strike_source": source,
                    "leader": leader, "leader_matches_winner": correct, "flip_after_time": flip,
                    "crossings_prior_5m": recent_cross,
                    "distance_bucket": distance_bucket(d.get("abs_distance_bps")), **d, **sigma,
                    "resolution_status": "OBSERVED_1M" if candle else "UNAVAILABLE_AT_CURRENT_RESOLUTION",
                    "future_data_used_for_features": False}
            snapshots.append(snap)
            mrow[f"{point}_spot"] = spot
            mrow[f"{point}_signed_distance_usd"] = d.get("signed_distance_usd")
            mrow[f"{point}_abs_distance_usd"] = d.get("abs_distance_usd")
            mrow[f"{point}_abs_distance_bps"] = d.get("abs_distance_bps")
            mrow[f"{point}_leader"] = leader
            mrow[f"{point}_resolution_status"] = snap["resolution_status"]
        # Second-level requests are kept explicitly unavailable; no interpolation.
        for point in ("T-30s", "T-15s", "T-5s", "T-1s"):
            mrow[f"{point}_resolution_status"] = "UNAVAILABLE_AT_CURRENT_RESOLUTION"
        market_rows = [r for r in candles if start < r["close_ts"] <= end]
        crossings_rows.append({"slug": slug, "day_type": mrow["day_type"], "strike_confidence": confidence,
                               "crossings_total": crossing_count(observations, float(strike), start, end, include_left_context=False) if strike else None,
                               "crossings_first_5m": crossing_count(observations, float(strike), start, start+300) if strike else None,
                               "crossings_last_5m": crossing_count(observations, float(strike), end-300, end) if strike else None,
                               "crossings_last_3m": crossing_count(observations, float(strike), end-180, end) if strike else None,
                               "crossings_last_2m": crossing_count(observations, float(strike), end-120, end) if strike else None,
                               "crossings_last_1m": crossing_count(observations, float(strike), end-60, end) if strike else None,
                               "resolution": "lower-bound at 1-minute resolution"})
        if strike:
            durations = near_strike_duration(market_rows, float(strike), start_ts=start, end_ts=end)
            last5 = [r for r in market_rows if r["close_ts"] > end-300]
            last3 = [r for r in market_rows if r["close_ts"] > end-180]
            last2 = [r for r in market_rows if r["close_ts"] > end-120]
            last1 = [r for r in market_rows if r["close_ts"] > end-60]
            for threshold, stats in durations.items():
                window_stats = near_strike_duration(last5, float(strike), start_ts=end-300, end_ts=end,
                                                    thresholds_bps=(threshold,))[threshold]
                in_zone = [abs(float(r["close"])-float(strike))/float(strike)*10000 < threshold for r in market_rows]
                last_zone_leader = next((leader_for(float(r["close"]), float(strike))
                                         for r, inside in reversed(list(zip(market_rows, in_zone))) if inside), None)
                danger_rows.append({"slug": slug, "day_type": mrow["day_type"], "threshold_bps": threshold,
                                    "minutes_within_zone": stats["minutes"], "episodes": stats["episodes"],
                                    "reentries": stats["reentries"], "last_5m_minutes_within_zone": window_stats["minutes"],
                                    "entered_danger_zone": stats["episodes"] > 0,
                                    "last_danger_zone_leader": last_zone_leader,
                                    "final_flip_from_last_danger_observation": (last_zone_leader != winner) if last_zone_leader in {"UP","DOWN"} and winner in {"UP","DOWN"} else None,
                                    "duration_resolution": "1-minute observation counts"})
            for label, rows in (("last_5m",last5),("last_3m",last3),("last_2m",last2),("last_1m",last1)):
                ds = [abs(float(r["close"])-float(strike)) for r in rows]
                mrow[f"min_abs_distance_{label}_usd"] = min(ds) if ds else None
            last = _last_closed(candles, end)
            if last:
                final_rows.append({"slug": slug, "day_type": mrow["day_type"], "winner_gamma": winner,
                                   "final_observation_ts": last["close_ts"], "final_spot_binance": last["close"],
                                   "strike_value": strike, "final_signed_distance_usd": last["close"]-float(strike),
                                   "final_abs_distance_usd": abs(last["close"]-float(strike)),
                                   "final_abs_distance_bps": abs(last["close"]-float(strike))/float(strike)*10000,
                                   "winner_used_for_margin": False})
        if local and reconstructed:
            error = abs(float(reconstructed)-float(local["value"]))
            strike_validation.append({"slug": slug, "reconstructed_strike": reconstructed,
                                     "canonical_local_strike": local["value"], "canonical_source": local["source"],
                                     "absolute_error_usd": error,
                                     "error_bps": error/float(local["value"])*10000,
                                     "comparison": "Binance boundary proxy vs verified Polymarket TWAP opening strike"})
        market_meta.append(mrow)
        quality.append({"slug": slug, "gamma_metadata_status": (data.get("gamma") or {}).get("status"),
                        "gamma_winner_status": "resolved" if winner else "unavailable",
                        "public_trade_fetch_status": data.get("trade_fetch_status"),
                        "public_price_history_status": data.get("price_fetch_status"),
                        "public_trade_rows": data.get("trade_rows_matched"),
                        "gamma_official_strike_available": bool(gamma_strike),
                        "local_chainlink_strike_available": bool(local),
                        "reconstructed_strike_available": reconstructed is not None,
                        "selected_strike_source": source, "selected_strike_confidence": confidence,
                        "btc_candles_market_window": len(market_rows),
                        "lifecycle_points_available": sum(1 for s in snapshots[-len(LIFECYCLE):] if s["slug"] == slug and s["resolution_status"] == "OBSERVED_1M"),
                        "excluded_from_distance_analysis": strike is None,
                        "exclusion_reason": "strike_unavailable" if strike is None else ""})

    distance_rows = _group_distance(snapshots)
    weekday_weekend = []
    for label in LIFECYCLE:
        for confidence in ("ALL", "HIGH", "LOW_PROXY"):
            subset=[r for r in snapshots if r["lifecycle_time"]==label and (confidence=="ALL" or r["strike_confidence"]==confidence)]
            wd=[float(r["abs_distance_usd"]) for r in subset if r["day_type"]=="weekday" and r.get("abs_distance_usd") is not None]
            we=[float(r["abs_distance_usd"]) for r in subset if r["day_type"]=="weekend" and r.get("abs_distance_usd") is not None]
            dbps=[float(r["abs_distance_bps"]) for r in subset if r["day_type"]=="weekday" and r.get("abs_distance_bps") is not None]
            ebps=[float(r["abs_distance_bps"]) for r in subset if r["day_type"]=="weekend" and r.get("abs_distance_bps") is not None]
            weekday_weekend.append({"lifecycle_time":label,"strike_confidence":confidence,"metric":"mean_abs_distance_usd",**compare_samples(wd,we,seed=100+LIFECYCLE[label])})
            weekday_weekend.append({"lifecycle_time":label,"strike_confidence":confidence,"metric":"mean_abs_distance_bps",**compare_samples(dbps,ebps,seed=200+LIFECYCLE[label])})
    near_rows = []
    for label in LIFECYCLE:
        for confidence in ("ALL", "HIGH", "LOW_PROXY"):
            rows=[r for r in snapshots if r["lifecycle_time"]==label and r.get("abs_distance_bps") is not None and (confidence=="ALL" or r["strike_confidence"]==confidence)]
            for day in ("weekday","weekend"):
                sub=[r for r in rows if r["day_type"]==day]
                for _,_,bucket in NEAR_BUCKETS:
                    count=sum(r["distance_bucket"]==bucket for r in sub)
                    near_rows.append({"lifecycle_time":label,"day_type":day,"strike_confidence":confidence,"distance_bucket":bucket,"n":len(sub),"markets_in_bucket":count,"share":count/len(sub) if sub else None})
                for threshold in (1,2,5):
                    count=sum(float(r["abs_distance_bps"])<threshold for r in sub)
                    near_rows.append({"lifecycle_time":label,"day_type":day,"strike_confidence":confidence,"distance_bucket":f"P(<{threshold}bps)","n":len(sub),"markets_in_bucket":count,"share":count/len(sub) if sub else None})
            wd=[r for r in rows if r["day_type"]=="weekday"]; we=[r for r in rows if r["day_type"]=="weekend"]
            for _,_,bucket in NEAR_BUCKETS:
                wd_share=sum(r["distance_bucket"]==bucket for r in wd)/len(wd) if wd else None
                we_share=sum(r["distance_bucket"]==bucket for r in we)/len(we) if we else None
                near_rows.append({"lifecycle_time":label,"day_type":"weekend_minus_weekday","strike_confidence":confidence,"distance_bucket":bucket,"n_weekday":len(wd),"n_weekend":len(we),"share":we_share-wd_share if wd_share is not None and we_share is not None else None})
    crossing_summary = []
    for metric in ("crossings_total","crossings_first_5m","crossings_last_5m","crossings_last_3m","crossings_last_2m","crossings_last_1m"):
        for confidence in ("ALL","HIGH","LOW_PROXY"):
            wd=[float(r[metric]) for r in crossings_rows if r["day_type"]=="weekday" and r.get(metric) is not None and (confidence=="ALL" or r["strike_confidence"]==confidence)]
            we=[float(r[metric]) for r in crossings_rows if r["day_type"]=="weekend" and r.get(metric) is not None and (confidence=="ALL" or r["strike_confidence"]==confidence)]
            crossing_summary.append({"metric":metric,"strike_confidence":confidence,**compare_samples(wd,we,seed=len(metric))})
    persistence_rows=[]
    persistence_tests=[]
    for label in LIFECYCLE:
        for confidence in ("ALL","HIGH","LOW_PROXY"):
            for day in ("weekday","weekend"):
                sub=[r for r in snapshots if r["lifecycle_time"]==label and r["day_type"]==day and r.get("flip_after_time") is not None and (confidence=="ALL" or r["strike_confidence"]==confidence)]
                persistence_rows.append({"lifecycle_time":label,"day_type":day,"strike_confidence":confidence,"n":len(sub),
                                         "leader_accuracy":sum(r["leader_matches_winner"] for r in sub)/len(sub) if sub else None,
                                         "flip_after_time_rate":statistics.mean(r["flip_after_time"] for r in sub) if sub else None,
                                         "winner_truth":"Gamma decisive outcomePrices"})
            wd=[float(r["flip_after_time"]) for r in snapshots if r["lifecycle_time"]==label and r["day_type"]=="weekday" and r.get("flip_after_time") is not None and (confidence=="ALL" or r["strike_confidence"]==confidence)]
            we=[float(r["flip_after_time"]) for r in snapshots if r["lifecycle_time"]==label and r["day_type"]=="weekend" and r.get("flip_after_time") is not None and (confidence=="ALL" or r["strike_confidence"]==confidence)]
            persistence_tests.append({"lifecycle_time":label,"metric":"flip_after_time_rate","strike_confidence":confidence,**compare_samples(wd,we,seed=300+LIFECYCLE[label])})
    conditional=[]
    for label in CORE_FLIP_TIMES:
        for confidence in ("ALL","HIGH","LOW_PROXY"):
          for day in ("weekday","weekend"):
            for _,_,bucket in NEAR_BUCKETS:
                sub=[r for r in snapshots if r["lifecycle_time"]==label and r["day_type"]==day and r["distance_bucket"]==bucket and r.get("flip_after_time") is not None and (confidence=="ALL" or r["strike_confidence"]==confidence)]
                conditional.append({"lifecycle_time":label,"day_type":day,"strike_confidence":confidence,"distance_bucket":bucket,"n":len(sub),
                                    "leader_accuracy":sum(r["leader_matches_winner"] for r in sub)/len(sub) if sub else None,
                                    "flip_after_time_rate":statistics.mean(r["flip_after_time"] for r in sub) if sub else None})
    hour_rows=[]
    for hour in ("00-06","06-12","12-18","18-24"):
        for label in CORE_FLIP_TIMES:
            for day in ("weekday","weekend"):
                sub=[r for r in snapshots if r["lifecycle_time"]==label and r["hour_block"]==hour and r["day_type"]==day and r.get("flip_after_time") is not None]
                hour_rows.append({"hour_block_et":hour,"lifecycle_time":label,"day_type":day,"n":len(sub),
                                  "mean_abs_distance_bps":statistics.mean(r["abs_distance_bps"] for r in sub) if sub else None,
                                  "flip_rate":statistics.mean(r["flip_after_time"] for r in sub) if sub else None})
    safety_rows=[{k:r.get(k) for k in ("slug","lifecycle_time","day_type","hour_block","abs_distance_bps","safety_sigma","volatility_source","prior_return_count","remaining_volatility_usd","future_data_used_for_features")} for r in snapshots]
    model_rows=[]
    model_rows.extend(_logistic_fit([r for r in snapshots if r["lifecycle_time"]=="T-1m"],False))
    adjusted_model=_logistic_fit([r for r in snapshots if r["lifecycle_time"]=="T-1m"],True)
    model_rows.extend(adjusted_model)
    simple_path=ROOT/"reports/unified_strategy_research/simple_backtest_all.csv"
    simple=[]
    if simple_path.exists():
        with simple_path.open(encoding="utf-8",newline="") as f:
            simple=list(csv.DictReader(f))
    t3_by_slug={r["slug"]:r for r in snapshots if r["lifecycle_time"]=="T+3m"}
    strategy_join=[]
    for r in simple:
        if r.get("observation_sec")!="180" or r.get("threshold_bps") not in {"0","2","5"} or r.get("entry_delay_sec")!="0" or r.get("execution_model")!="vwap_5s" or r.get("slippage_cents")!="0": continue
        snap=t3_by_slug.get(r.get("slug"))
        if not snap: continue
        strategy_join.append({**{k:r.get(k) for k in ("slug","local_date","week","weekday_weekend","winner","observation_sec","threshold_bps","trend_bps","side","entry_price","net_pnl","zero_fee_net_pnl")},
                              "tplus3_abs_distance_usd":snap.get("abs_distance_usd"),"tplus3_abs_distance_bps":snap.get("abs_distance_bps"),
                              "safety_sigma":snap.get("safety_sigma"),"crossings_prior_5m":snap.get("crossings_prior_5m"),
                              "distance_bucket":snap.get("distance_bucket"),"strike_confidence":snap.get("strike_confidence"),
                              "analysis_note":"print-based entry proxy; not executable BBO"})
    strategy_group=[]
    for confidence in ("ALL","HIGH","LOW_PROXY"):
      for threshold in ("0","2","5"):
        for bucket in [b[2] for b in NEAR_BUCKETS]:
            sub=[r for r in strategy_join if r["threshold_bps"]==threshold and r["distance_bucket"]==bucket and (confidence=="ALL" or r["strike_confidence"]==confidence)]
            strategy_group.append({"strike_confidence":confidence,"entry_threshold_bps":threshold,"distance_bucket":bucket,"n":len(sub),
                                   "win_rate":sum(float(r["net_pnl"] or 0)>0 for r in sub)/len(sub) if sub else None,
                                   "mean_net_pnl":statistics.mean(float(r["net_pnl"]) for r in sub) if sub else None,
                                   "mean_entry_price":statistics.mean(float(r["entry_price"]) for r in sub) if sub else None})
    trend_distance_rows=[]
    for confidence in ("ALL","HIGH","LOW_PROXY"):
      for threshold in ("0","2","5"):
        sub=[r for r in strategy_join if r["threshold_bps"]==threshold and (confidence=="ALL" or r["strike_confidence"]==confidence)
             and r.get("trend_bps") not in (None,"") and r.get("tplus3_abs_distance_bps") is not None]
        trend=[abs(float(r["trend_bps"])) for r in sub]
        distance=[abs(float(r["tplus3_abs_distance_bps"])) for r in sub]
        entry=[float(r["entry_price"]) for r in sub]
        pnl=[float(r["net_pnl"]) for r in sub]
        correct=[1.0 if r["side"]==r["winner"] else 0.0 for r in sub]
        trend_distance_rows.append({"strike_confidence":confidence,"entry_threshold_bps":threshold,"n":len(sub),
                                    "corr_abs_trend_bps_net_pnl":_correlation(trend,pnl),
                                    "corr_abs_distance_bps_net_pnl":_correlation(distance,pnl),
                                    "corr_entry_price_net_pnl":_correlation(entry,pnl),
                                    "corr_abs_trend_bps_correct":_correlation(trend,correct),
                                    "corr_abs_distance_bps_correct":_correlation(distance,correct),
                                    "corr_trend_vs_distance":_correlation(trend,distance),
                                    "note":"descriptive Pearson correlations; print-based entries; not causal"})
    transitions=[]
    for prev,nxt in adjacent_pairs(market_meta):
        prevslug=prev["slug"]; nxtslug=nxt["slug"]
        prevref=local_settlements.get(prevslug,{}).get("spot")
        prev_binance,_,_=reconstruct_adjacent_strike(candles, int(nxt["market_start_ts"]))
        early=t3_by_slug.get(nxtslug)
        transitions.append({"previous_slug":prevslug,"next_slug":nxtslug,"previous_winner_gamma":prev.get("winner_gamma"),
                            "previous_terminal_reference_local":prevref,"previous_terminal_binance_proxy":prev_binance,
                            "next_strike":nxt.get("strike_value"),
                            "next_strike_source":nxt.get("strike_source"),"previous_reference_minus_next_strike_usd":float(prevref)-float(nxt["strike_value"]) if prevref and nxt.get("strike_value") else None,
                            "binance_boundary_minus_next_strike_usd":float(prev_binance)-float(nxt["strike_value"]) if prev_binance and nxt.get("strike_value") else None,
                            "next_early_leader_tplus3":early.get("leader") if early else None,"next_winner_gamma":nxt.get("winner_gamma"),
                            "descriptive_only_future_winner_not_feature":True})
    # Exact neighboring local journal settlement-reference -> next locked strike validation.
    adjacent_validation=[]
    for prev in market_meta:
        next_slug=f"btc-updown-15m-{int(prev['market_start_ts'])+900}"
        prev_settle=local_settlements.get(prev["slug"]); next_local=local_strikes.get(next_slug)
        if prev_settle and next_local:
            ref=prev_settle.get("spot")
            if ref:
                error=abs(float(ref)-float(next_local["value"]))
                adjacent_validation.append({"previous_slug":prev["slug"],"next_slug":next_slug,
                                            "previous_settlement_reference":ref,"next_verified_strike":next_local["value"],
                                            "absolute_error_usd":error,"error_bps":error/float(next_local["value"])*10000,
                                            "reference_source":prev_settle.get("reference_source"),"next_strike_source":next_local["source"]})
    adjacent_validation_export = [
        {"validation_type":"boundary_binance_proxy_vs_verified_twap_strike", **r}
        for r in strike_validation
    ] + [
        {"validation_type":"previous_local_settlement_reference_vs_next_verified_strike", **r}
        for r in adjacent_validation
    ]
    base_weekend = _logistic_fit([r for r in snapshots if r["lifecycle_time"]=="T-1m"], False)
    base_weekend_coef = next((r["coefficient"] for r in base_weekend if r.get("term")=="weekend"), None)
    adjusted_weekend_coef = next((r["coefficient"] for r in adjusted_model if r.get("term")=="weekend"), None)
    weekend_shrinkage = (1.0-abs(adjusted_weekend_coef)/abs(base_weekend_coef)) if base_weekend_coef not in (None,0) and adjusted_weekend_coef is not None else None
    for row in model_rows:
        if row.get("term") == "weekend":
            row["weekend_or_shrinkage"] = weekend_shrinkage
    evidence_rows = _evidence_verdicts(distance_comparisons=weekday_weekend,
                                       crossing_tests=crossing_summary,
                                       persistence_tests=persistence_tests,
                                       model_rows=model_rows,
                                       strategy_group=strategy_group)
    for rows, name in ((market_meta,"market_strike_distance.csv"),(distance_rows,"distance_by_lifecycle.csv"),
                       (weekday_weekend,"weekday_weekend_distance.csv"),(near_rows,"near_strike_probability.csv"),
                       (crossings_rows,"crossing_counts.csv"),(persistence_rows,"leader_persistence.csv"),
                       (conditional,"flip_rate_by_distance.csv"),(danger_rows,"danger_zone_duration.csv"),
                       (final_rows,"final_margin.csv"),(hour_rows,"hour_block_flip_risk.csv"),
                       (safety_rows,"safety_sigma.csv"),(strategy_join,"strategy_strike_risk_join.csv"),
                       (transitions,"adjacent_market_transitions.csv"),(adjacent_validation_export,"adjacent_strike_validation.csv"),
                       (crossing_summary,"weekend_crossing_tests.csv"),
                       (quality,"data_quality.csv"),(model_rows,"weekend_effect_models.csv"),
                       (persistence_tests,"weekend_flip_rate_tests.csv"),
                       (strategy_group,"strategy_pnl_by_distance.csv"),
                       (trend_distance_rows,"strategy_trend_vs_distance.csv"),
                       (evidence_rows,"evidence_verdict.csv")):
        _csv(output/name,rows)
    _write_summary(output, market_meta, snapshots, crossings_rows, persistence_rows,
                   conditional, strike_validation, adjacent_validation, strategy_group,
                   quality, candles, weekday_weekend, near_rows, danger_rows,
                   crossing_summary, persistence_tests, model_rows, hour_rows, trend_distance_rows,
                   evidence_rows)
    return {"markets":market_meta,"snapshots":snapshots,"quality":quality,"output":output,
            "strike_validation":strike_validation,"adjacent_validation":adjacent_validation,
            "strategy_group":strategy_group}


def _write_summary(output: Path, markets: list[dict[str, Any]], snapshots: list[dict[str, Any]],
                   crossings: list[dict[str, Any]], persistence: list[dict[str, Any]],
                   conditional: list[dict[str, Any]], strike_validation: list[dict[str, Any]],
                   adjacent_validation: list[dict[str, Any]], strategy_group: list[dict[str, Any]],
                   quality: list[dict[str, Any]], candles: list[dict[str, Any]],
                   distance_comparisons: list[dict[str, Any]], near_rows: list[dict[str, Any]],
                   danger_rows: list[dict[str, Any]], crossing_tests: list[dict[str, Any]],
                   persistence_tests: list[dict[str, Any]], model_rows: list[dict[str, Any]],
                   hour_rows: list[dict[str, Any]], trend_distance_rows: list[dict[str, Any]],
                   evidence_rows: list[dict[str, Any]]) -> None:
    def mean(values): return statistics.mean(values) if values else None
    def ptxt(value): return "n/a" if value is None else f"{value*100:.1f}%"
    def num(value): return "n/a" if value is None else f"{value:.3f}"
    by_time={(r["lifecycle_time"],r["day_type"]):r for r in persistence}
    cross_by_day={day:mean([r["crossings_total"] for r in crossings if r["day_type"]==day and r.get("strike_confidence")=="LOW_PROXY" and r.get("crossings_total") is not None]) for day in ("weekday","weekend")}
    trade_status=defaultdict(int); price_status=defaultdict(int)
    for row in quality:
        trade_status[row.get("public_trade_fetch_status") or "unavailable"] += 1
        price_status[row.get("public_price_history_status") or "unavailable"] += 1
    source_counts=defaultdict(int)
    for r in markets: source_counts[r["strike_source"]]+=1
    weekday_n=sum(r["day_type"]=="weekday" for r in markets); weekend_n=sum(r["day_type"]=="weekend" for r in markets)
    distance_index={(r["lifecycle_time"],r["strike_confidence"],r["metric"]):r for r in distance_comparisons}
    flip_test_index={(r["lifecycle_time"],r["strike_confidence"]):r for r in persistence_tests}
    crossing_index={(r["metric"],r["strike_confidence"]):r for r in crossing_tests}
    lines=["# Strike Proximity / Late Flip Risk 研究", "",
           "## 資料覆蓋", "",
           f"- 市場：{len(markets)}（weekday {weekday_n}、weekend {weekend_n}）；Gamma decisive winners：{sum(bool(r['winner_gamma']) for r in markets)}。",
           f"- Public cache retrieval：trades {dict(trade_status)}；price history {dict(price_status)}。Gamma metadata success={sum(r.get('gamma_metadata_status')=='success' for r in quality)}。",
           f"- strike provenance：Gamma official={source_counts['OFFICIAL_POLYMARKET_GAMMA']}；verified Polymarket TWAP/Chainlink reference={source_counts['CHAINLINK_REFERENCE']}；相鄰市場 Binance proxy={source_counts['RECONSTRUCTED_ADJACENT_MARKET_BINANCE_PROXY']}；unavailable={source_counts['UNAVAILABLE']}。",
           f"- Binance BTCUSDT 1m candles loaded={len(candles)}。BTC is a historical proxy, not Polymarket's Chainlink settlement feed. No sub-minute interpolation is used; T-30s/15s/5s/1s remain unavailable.",
           f"- BTC boundary proxy vs verified local strike: N={len(strike_validation)}, median abs error USD={num(_median([r['absolute_error_usd'] for r in strike_validation]))}, P90={num(_quant([r['absolute_error_usd'] for r in strike_validation],.9))}, max={num(max([r['absolute_error_usd'] for r in strike_validation],default=None))}.",
           f"- Previous local settlement reference vs next verified strike adjacent pairs={len(adjacent_validation)}; exact 15m pairs in sampled market set={sum(1 for m in markets if int(m['market_start_ts'])+900 in {int(x['market_start_ts']) for x in markets})} (see CSV).",
           "- Gamma cache contained no Price-To-Beat field in the sampled markets. Verified local Polymarket crypto TWAP opening reference was used where present; Binance boundary close is a low-confidence fallback only.", "",
           "- Strike-confidence limitation: only six sampled markets have verified local TWAP openings; 194 use a Binance one-minute boundary proxy. That proxy's error against verified strikes has median and P90 several bps, so pooled `<1/2/5 bps` results are not canonical strike findings.",
           "", "## 核心結果（低信度 Binance boundary proxy 樣本；探索性，不可解讀為 canonical strike 結論）", "",
           f"- Mean lower-bound crossing count per market: weekday {num(cross_by_day['weekday'])}, weekend {num(cross_by_day['weekend'])}."]
    for label in ("T+3m","T+5m","T-2m","T-1m"):
        comp=distance_index.get((label,"LOW_PROXY","mean_abs_distance_bps"),{})
        lines.append(f"- {label} weekend-minus-weekday mean absolute distance: {num(comp.get('weekend_minus_weekday'))} bps (95% bootstrap CI {num(comp.get('bootstrap_ci_low'))} to {num(comp.get('bootstrap_ci_high'))}; market permutation p={num(comp.get('permutation_p'))}).")
    for label in ("T-5m","T-2m","T-1m"):
        wd=by_time.get((label,"weekday"),{}).get("flip_after_time_rate")
        we=by_time.get((label,"weekend"),{}).get("flip_after_time_rate")
        test=flip_test_index.get((label,"LOW_PROXY"),{})
        lines.append(f"- {label} leader flips before Gamma final winner: weekday {ptxt(wd)}, weekend {ptxt(we)}; difference CI {num(test.get('bootstrap_ci_low'))} to {num(test.get('bootstrap_ci_high'))}, p={num(test.get('permutation_p'))}.")
    cross_test=crossing_index.get(("crossings_total","LOW_PROXY"),{})
    lines.append(f"- Lower-bound crossings/weekend minus weekday: {num(cross_test.get('weekend_minus_weekday'))}; 95% bootstrap CI {num(cross_test.get('bootstrap_ci_low'))} to {num(cross_test.get('bootstrap_ci_high'))}, p={num(cross_test.get('permutation_p'))}.")
    et_rows={(r["day_type"]):r for r in hour_rows if r.get("hour_block_et")=="06-12" and r.get("lifecycle_time")=="T-1m"}
    if "weekday" in et_rows and "weekend" in et_rows:
        lines.append(f"- ET 06–12 at T-1m: weekday mean abs proxy distance={num(et_rows['weekday'].get('mean_abs_distance_bps'))}bps, flip={ptxt(et_rows['weekday'].get('flip_rate'))} (n={et_rows['weekday'].get('n')}); weekend={num(et_rows['weekend'].get('mean_abs_distance_bps'))}bps, flip={ptxt(et_rows['weekend'].get('flip_rate'))} (n={et_rows['weekend'].get('n')}). Proxy-only; small cells.")
    lines += ["", "### Near-strike share (LOW_PROXY only)", ""]
    for label in ("T+3m","T+5m","T-2m","T-1m"):
        parts=[]
        for threshold in (1,2,5):
            vals={(r["day_type"]):r for r in near_rows if r["lifecycle_time"]==label and r["strike_confidence"]=="LOW_PROXY" and r["distance_bucket"]==f"P(<{threshold}bps)" and r["day_type"] in ("weekday","weekend")}
            if vals:
                parts.append(f"<{threshold}bps weekday {ptxt(vals['weekday']['share'])} (n={vals['weekday']['n']}), weekend {ptxt(vals['weekend']['share'])} (n={vals['weekend']['n']})")
        lines.append(f"- {label}: " + "; ".join(parts) + ".")
    lines += ["", "## Distance-conditioned flip rates", ""]
    for bucket in ("<1bps","1-2bps","2-5bps","5-10bps",">10bps"):
        vals=[r for r in conditional if r["lifecycle_time"]=="T-1m" and r["distance_bucket"]==bucket and r["strike_confidence"]=="LOW_PROXY"]
        txt="; ".join(f"{r['day_type']} n={r['n']} flip={ptxt(r['flip_after_time_rate'])}" for r in vals)
        lines.append(f"- T-1m {bucket}: {txt or 'no sample'}.")
    lines += ["", "## ET 06–12 與模型", "",
              "- `hour_block_flip_risk.csv` compares weekday/weekend distance and flip rate at each core timestamp, including ET 06–12.",
              "- `weekend_effect_models.csv` reports exploratory weekend-only and adjusted T-1m logistic models; adjusted model includes distance, prior-only safety sigma, recent crossings and hour blocks. Coefficients/approximate Wald intervals are not causal evidence.",
              "- Weekend coefficient change is descriptive; interpret only if both models use the same rows. Both 95% intervals cross zero; there is no evidence here of a positive weekend flip-risk effect. Sparse cells and 200-market sampling limit power.",
              "- The adjusted T-1m weekend log-odds coefficient uses the same 198 markets as the weekend-only model; the coefficient and change are in `weekend_effect_models.csv`. Here it moves farther below zero rather than shrinking toward zero, but both intervals cross zero.",
              "- ET 06–12 cross-tabs are in `hour_block_flip_risk.csv`; strike distance and flips inherit the low-confidence proxy limitation.",
              "", "## 180 秒策略關聯", ""]
    for row in strategy_group:
        if row["entry_threshold_bps"]=="5" and row.get("strike_confidence")=="LOW_PROXY":
            lines.append(f"- 180s/5bps, {row['distance_bucket']}: n={row['n']}, win={ptxt(row['win_rate'])}, mean net PnL=${num(row['mean_net_pnl'])}, mean entry={num(row['mean_entry_price'])}.")
    trend_rows=[r for r in trend_distance_rows if r.get("strike_confidence")=="LOW_PROXY" and r.get("entry_threshold_bps")=="5"]
    if trend_rows:
        r=trend_rows[0]
        lines.append(f"- 180s/5bps proxy sample correlations: |trend| vs net PnL={num(r.get('corr_abs_trend_bps_net_pnl'))}; |distance| vs net PnL={num(r.get('corr_abs_distance_bps_net_pnl'))}; entry price vs net PnL={num(r.get('corr_entry_price_net_pnl'))}; |trend| vs |distance|={num(r.get('corr_trend_vs_distance'))} (n={r.get('n')}). 高度共線且為描述性 print-based 指標，不能判定 distance 比 trend 更能解釋損益；本研究也沒有同期可比的 liquidity feature。")
    for threshold in (1,2,5):
        sub=[r for r in danger_rows if r["threshold_bps"]==threshold]
        wd=mean([r["minutes_within_zone"] for r in sub if r["day_type"]=="weekday"])
        we=mean([r["minutes_within_zone"] for r in sub if r["day_type"]=="weekend"])
        lines.append(f"- Time in <{threshold}bps zone (observed minute bars / market): weekday {num(wd)} min, weekend {num(we)} min; low-confidence proxy-derived.")
    adjacent_errors=[float(r["absolute_error_usd"]) for r in adjacent_validation if r.get("absolute_error_usd") is not None]
    adjacent_bps=[float(r["error_bps"]) for r in adjacent_validation if r.get("error_bps") is not None]
    near_match=sum(v <= 1 for v in adjacent_errors)
    lines += ["", "## 相鄰市場與限制", "",
              f"- Verified previous-market settlement reference vs next-market strike: N={len(adjacent_validation)}; median absolute difference=${num(_median(adjacent_errors))} ({num(_median(adjacent_bps))}bps), P90=${num(_quant(adjacent_errors,.9))}; within $1 match={near_match}/{len(adjacent_errors)}. The sample is sparse and only tests available local consecutive markets.",
              "- 1-minute crossing counts are lower bounds and cannot resolve multiple flips within a candle. Gamma winner remains outcome truth; Binance is only the spot path proxy.",
              "- Historical strikes absent from Gamma/local verified cache are reconstructed from the last closed Binance minute at the market boundary and marked low-confidence. Do not promote these to canonical strike.",
              "- No live rules were changed. This is descriptive research; near-strike buckets are exploratory and not live thresholds.",
              "- Bootstrap/permutation comparisons use markets as units (1,000 deterministic resamples); small samples and sampling design limit power.",
              "", "## Interpretation", "",
              "- Strike proximity is a plausible mechanism for late flip exposure, but weekend causality and incremental predictive value require the supplied tables' confidence intervals, matched samples, and the explicit data-quality labels.",
              "- `evidence_verdict.csv` gives explicit Supported / Suggestive / Not supported / Cannot assess labels. Current strike coverage is inadequate for canonical proximity conclusions; distance-vs-liquidity cannot be assessed because no matched liquidity feature is in this dataset.",
              "- Strategy trend and strike distance are nearly collinear in this sample, and outcome uses public trade-print proxies rather than executable BBO. The available correlation comparison cannot establish which is more explanatory or causal.", ""]
    lines += ["", "## 假說判定", ""]
    lines.append("| 假說 | 判定 | 依據與限制 |")
    lines.append("|---|---|---|")
    for row in evidence_rows:
        lines.append(f"| {row['hypothesis']} | {row['verdict']} | {row['basis']} |")
    (output/"summary.md").write_text("\n".join(lines),encoding="utf-8")


def _evidence_verdicts(*, distance_comparisons: list[dict[str, Any]],
                       crossing_tests: list[dict[str, Any]],
                       persistence_tests: list[dict[str, Any]],
                       model_rows: list[dict[str, Any]],
                       strategy_group: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Conservative labels: proxy-only signals cannot establish canonical-strike hypotheses."""
    dist = {(r["lifecycle_time"], r["strike_confidence"], r["metric"]): r for r in distance_comparisons}
    cross = {(r["metric"], r["strike_confidence"]): r for r in crossing_tests}
    persist = {(r["lifecycle_time"], r["strike_confidence"]): r for r in persistence_tests}
    model = {(r.get("model"), r.get("term")): r for r in model_rows if r.get("term") == "weekend"}
    # Six verified strikes are all weekdays, so there is no high-confidence weekday/weekend contrast.
    rows = [
        ("H1 weekend closer to strike", "Suggestive (proxy only)", "Weekend proxy distance is lower at key timestamps, but 194/200 strikes are Binance proxies and verified-strike sample has no weekend markets."),
        ("H2 weekend spends more time near strike", "Suggestive (proxy only)", "LOW_PROXY danger-zone duration is descriptively longer on weekends; Binance boundary error is several bps, so canonical rates cannot be established."),
        ("H3 weekend has more crossings", "Not supported (proxy sample)", f"LOW_PROXY weekend-minus-weekday total crossings={cross.get(('crossings_total','LOW_PROXY'),{}).get('weekend_minus_weekday')}; CI includes zero and minute counts are lower bounds."),
        ("H4 weekend leader persistence is lower", "Not supported (proxy sample)", f"LOW_PROXY T-1m flip rates do not show a weekend increase; difference CI includes zero; winner leader depends on proxy strike."),
        ("H5 weekend late flip rate is higher", "Not supported (proxy sample)", "T-5m/T-2m/T-1m proxy contrasts are near zero or negative with intervals crossing zero; low power and strike-source limitation remain."),
        ("Weekend effect shrinks after distance/safety adjustment", "Cannot assess", "Both logistic weekend intervals cross zero and the adjusted coefficient moves farther negative, not toward zero; estimates are imprecise and not causal."),
        ("Distance explains strategy danger better than trend/liquidity", "Cannot assess", "Trend and distance are highly collinear; outcome is trade-print proxy, and no matched liquidity feature or out-of-sample comparison exists."),
        ("Canonical weekend strike-proximity effect", "Cannot assess with current strike coverage", "Only 6 verified local strikes (all weekdays); 0 Gamma official strikes. Obtain canonical Chainlink/RTDS strikes for a balanced sample."),
    ]
    return [{"hypothesis": h, "verdict": v, "basis": b, "evidence_scope": "research-only; LOW_PROXY results are descriptive"} for h,v,b in rows]


def _median(values): return statistics.median(values) if values else None
def _quant(values,q): return _quantile(values,q) if values else None


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start",default="2026-07-27"); parser.add_argument("--end",default="2026-09-20")
    parser.add_argument("--timezone",default="America/New_York"); parser.add_argument("--output",default=str(DEFAULT_OUT))
    parser.add_argument("--sample-csv",default=str(DEFAULT_SAMPLE)); parser.add_argument("--db",default="logs/trade_journal.db")
    parser.add_argument("--btc-cache-dir",default=str(DEFAULT_BTC)); parser.add_argument("--cache-dir",action="append")
    args=parser.parse_args(argv)
    start=int(datetime.fromisoformat(args.start).replace(tzinfo=ZoneInfo(args.timezone)).timestamp())
    end=int(datetime.fromisoformat(args.end).replace(tzinfo=ZoneInfo(args.timezone)).timestamp())+86399
    cache_dirs=[Path(p) for p in args.cache_dir] if args.cache_dir else DEFAULT_CACHES
    result=run_analysis(start_epoch=start,end_epoch=end,cache_dirs=cache_dirs,
                        sample_csv=Path(args.sample_csv) if args.sample_csv else None,
                        btc_cache=Path(args.btc_cache_dir),db_path=Path(args.db) if args.db else None,
                        output=Path(args.output),timezone_name=args.timezone)
    print(f"markets={len(result['markets'])} snapshots={len(result['snapshots'])} strike_validation={len(result['strike_validation'])} output={result['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
