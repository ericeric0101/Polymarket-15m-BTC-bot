"""Pure analysis helpers for weekday/weekend BTC 15-minute market studies."""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable
from zoneinfo import ZoneInfo


def parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def classify_weekend(timestamp: float | datetime | str, timezone_name: str = "America/New_York") -> bool | None:
    parsed = parse_timestamp(timestamp) if isinstance(timestamp, (datetime, str)) else None
    if parsed is None and not isinstance(timestamp, (datetime, str)):
        try:
            parsed = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    if parsed is None:
        return None
    return parsed.astimezone(ZoneInfo(timezone_name)).weekday() >= 5


def market_seconds_to_resolution(slug: str | None, timestamp: float | datetime | str) -> float | None:
    """BTC 15m slugs end in the market start epoch; resolution is +900 seconds."""
    if not slug:
        return None
    try:
        market_start = int(str(slug).rsplit("-", 1)[-1])
    except (ValueError, TypeError):
        return None
    parsed = parse_timestamp(timestamp) if isinstance(timestamp, (datetime, str)) else None
    if parsed is None and not isinstance(timestamp, (datetime, str)):
        try:
            parsed = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    if parsed is None:
        return None
    return max(0.0, market_start + 900 - parsed.timestamp())


def resolution_time_bin(seconds_left: float | None) -> str:
    if seconds_left is None or not math.isfinite(float(seconds_left)):
        return "unknown"
    seconds = max(0.0, float(seconds_left))
    if seconds > 600:
        return "T-15m_to_T-10m"
    if seconds > 300:
        return "T-10m_to_T-5m"
    if seconds > 120:
        return "T-5m_to_T-2m"
    if seconds > 60:
        return "T-2m_to_T-1m"
    if seconds > 30:
        return "T-60s_to_T-30s"
    if seconds > 15:
        return "T-30s_to_T-15s"
    return "T-15s_to_settlement"


def price_slippage(side: str, intended_price: float | None, actual_price: float | None) -> float | None:
    if intended_price is None or actual_price is None:
        return None
    try:
        intended, actual = float(intended_price), float(actual_price)
        if not math.isfinite(intended) or not math.isfinite(actual):
            return None
    except (TypeError, ValueError):
        return None
    normalized_side = str(side or "").upper()
    if normalized_side == "BUY":
        return actual - intended
    if normalized_side == "SELL":
        return intended - actual
    return None


def percentile(values: Iterable[float], quantile: float) -> float | None:
    clean = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not clean:
        return None
    q = min(1.0, max(0.0, float(quantile)))
    point = (len(clean) - 1) * q
    lower, upper = math.floor(point), math.ceil(point)
    if lower == upper:
        return clean[lower]
    return clean[lower] + (clean[upper] - clean[lower]) * (point - lower)


def summarize_pnl(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = []
    for row in rows:
        value = row.get("pnl")
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            values.append(number)
    if not values:
        return {
            "sample_size": 0, "total_pnl": None, "mean_pnl": None, "median_pnl": None,
            "win_rate": None, "profit_factor": None, "max_drawdown": None,
            "average_win": None, "average_loss": None, "pnl_p10": None,
            "pnl_p5": None, "pnl_p1": None, "worst_trade": None,
            "sample_warning": True,
        }
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    equity = peak = max_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "sample_size": len(values),
        "total_pnl": sum(values),
        "mean_pnl": mean(values),
        "median_pnl": median(values),
        "win_rate": len(wins) / len(values),
        "profit_factor": gross_win / gross_loss if gross_loss else None,
        "max_drawdown": max_drawdown,
        "average_win": mean(wins) if wins else None,
        "average_loss": mean(losses) if losses else None,
        "pnl_p10": percentile(values, 0.10),
        "pnl_p5": percentile(values, 0.05),
        "pnl_p1": percentile(values, 0.01),
        "worst_trade": min(values),
        "sample_warning": len(values) < 30,
    }


def _valid_feature(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def compute_liquidity_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach equal-weight percentile scores; never impute missing depth as zero."""
    fields = {
        "spread": (("spread", "spread_ps"), False),
        "depth_1c": (("depth_1c", "depth_within_1c"), True),
        "depth_2c": (("depth_2c", "depth_within_2c"), True),
        "depth_5c": (("depth_5c", "depth_within_5c"), True),
        "quote_age_sec": (("quote_age_sec", "max_quote_age_sec"), False),
        "quote_update_rate": (("quote_update_rate",), True),
        "trade_frequency": (("trade_frequency", "trade_frequency_per_sec"), True),
        "recent_volume": (("recent_volume", "volume", "public_volume_shares"), True),
    }
    output = [dict(row) for row in rows]
    feature_values: dict[str, list[float | None]] = {}
    for output_name, (input_names, _) in fields.items():
        values = []
        for row in output:
            value = next((row.get(name) for name in input_names if row.get(name) is not None), None)
            values.append(_valid_feature(value))
            row[output_name] = values[-1]
        feature_values[output_name] = values

    normalized: dict[str, list[float | None]] = {}
    for name, values in feature_values.items():
        good = [(index, value) for index, value in enumerate(values) if value is not None]
        if len(good) < 2:
            normalized[name] = [None] * len(values)
            continue
        ordered = sorted(good, key=lambda pair: pair[1])
        ranks = [None] * len(values)
        start = 0
        while start < len(ordered):
            end = start + 1
            while end < len(ordered) and ordered[end][1] == ordered[start][1]:
                end += 1
            rank = ((start + end - 1) / 2) / (len(ordered) - 1)
            for index, _ in ordered[start:end]:
                ranks[index] = 1.0 - rank if not fields[name][1] else rank
            start = end
        normalized[name] = ranks
    for index, row in enumerate(output):
        scores = [normalized[name][index] for name in fields if normalized[name][index] is not None]
        row["liquidity_score"] = mean(scores) if len(scores) >= 3 else None
        row["liquidity_score_feature_count"] = len(scores)
    return output


def deduplicate_fills(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    output = []
    for index, row in enumerate(rows):
        payload = row.get("payload") or {}
        run_id = str(row.get("run_id") or "")
        fill_id = row.get("fill_id") or payload.get("fill_id")
        identity = ("fill", str(fill_id)) if fill_id else (
            "row", str(row.get("event_id") or row.get("id") or f"ordinal:{index}")
        )
        key = (run_id, identity[0] + ":" + identity[1])
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def public_data_source_label(source: str) -> str:
    normalized = str(source or "").lower()
    if normalized in {"cache", "public", "api"}:
        return "PUBLIC_HISTORICAL"
    if normalized in {"journal", "local", "db"}:
        return "LOCAL_RECORDED"
    if normalized in {"l2", "forward_l2"}:
        return "FORWARD_L2_ONLY"
    return "UNCLASSIFIED"


def _cache_path(cache_dir: str, market_slug: str) -> Path:
    safe_slug = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in market_slug)
    suffix = hashlib.sha256(market_slug.encode("utf-8")).hexdigest()[:10]
    return Path(cache_dir) / f"{safe_slug[:100]}-{suffix}.json"


def load_public_cache(cache_dir: str, market_slug: str) -> dict[str, Any] | None:
    try:
        with _cache_path(cache_dir, market_slug).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def save_public_cache(cache_dir: str, market_slug: str, payload: dict[str, Any]) -> None:
    path = _cache_path(cache_dir, market_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def compare_samples(
    weekday: list[float],
    weekend: list[float],
    *,
    bootstrap_iterations: int = 1000,
    seed: int = 17,
) -> dict[str, Any]:
    """Mean/median effects with bootstrap CI and a two-sided permutation p-value."""
    left = [float(x) for x in weekday if math.isfinite(float(x))]
    right = [float(x) for x in weekend if math.isfinite(float(x))]
    if not left or not right:
        return {
            "weekday_n": len(left), "weekend_n": len(right), "mean_difference": None,
            "median_difference": None, "mean_difference_ci95": None,
            "permutation_p": None, "cliffs_delta": None,
            "weekday_mean": mean(left) if left else None,
            "weekend_mean": mean(right) if right else None,
            "weekday_median": median(left) if left else None,
            "weekend_median": median(right) if right else None,
            "sample_warning": True,
        }
    observed = mean(right) - mean(left)
    median_difference = median(right) - median(left)
    rng = random.Random(seed)
    boot = []
    for _ in range(max(100, int(bootstrap_iterations))):
        sampled_left = [rng.choice(left) for _ in left]
        sampled_right = [rng.choice(right) for _ in right]
        boot.append(mean(sampled_right) - mean(sampled_left))
    pooled = left + right
    n_left = len(left)
    extreme = 0
    permutations = max(500, min(5000, int(bootstrap_iterations) * 2))
    for _ in range(permutations):
        shuffled = list(pooled)
        rng.shuffle(shuffled)
        diff = mean(shuffled[n_left:]) - mean(shuffled[:n_left])
        if abs(diff) >= abs(observed):
            extreme += 1
    wins = sum(1 for r in right for l in left if r > l)
    losses = sum(1 for r in right for l in left if r < l)
    return {
        "weekday_n": len(left), "weekend_n": len(right),
        "weekday_mean": mean(left), "weekend_mean": mean(right),
        "weekday_median": median(left), "weekend_median": median(right),
        "mean_difference": observed, "median_difference": median_difference,
        "mean_difference_ci95": [percentile(boot, 0.025), percentile(boot, 0.975)],
        "permutation_p": (extreme + 1) / (permutations + 1),
        "cliffs_delta": (wins - losses) / (len(left) * len(right)),
        "sample_warning": min(len(left), len(right)) < 30,
    }
