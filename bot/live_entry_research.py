"""Research-only live entry annotations; never returns order authority."""
from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


class StrikeCrossingTracker:
    """Thread-safe, per-market crossing windows from observed canonical ticks."""

    WINDOWS_SEC = (300, 120, 60, 30)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observations: dict[str, deque[tuple[float, str]]] = defaultdict(deque)
        self._last_leader: dict[str, str] = {}
        self._crossings: dict[str, deque[float]] = defaultdict(deque)
        self._total_crossings: dict[str, int] = defaultdict(int)

    def observe(self, *, slug: str, spot: float | None, strike: float | None,
                observed_ts: float, canonical: bool) -> dict[str, Any]:
        if not canonical or not slug or spot is None or strike is None:
            return {"available": False, "reason": "canonical_spot_or_strike_unavailable"}
        try:
            spot_value, strike_value, ts = float(spot), float(strike), float(observed_ts)
        except (TypeError, ValueError):
            return {"available": False, "reason": "invalid_spot_strike_or_timestamp"}
        if strike_value <= 0 or spot_value <= 0:
            return {"available": False, "reason": "invalid_spot_or_strike"}
        leader = "UP" if spot_value > strike_value else "DOWN" if spot_value < strike_value else "TIE"
        with self._lock:
            prior_observations = self._observations.get(slug)
            if prior_observations and ts <= prior_observations[-1][0]:
                result = self._counts_locked(slug, prior_observations[-1][0], self._last_leader.get(slug, leader))
                result.update({"available": True, "stale_or_duplicate_tick_ignored": True})
                return result
            prior = self._last_leader.get(slug)
            if leader in {"UP", "DOWN"} and prior in {"UP", "DOWN"} and leader != prior:
                self._crossings[slug].append(ts)
                self._total_crossings[slug] += 1
            if leader in {"UP", "DOWN"}:
                self._last_leader[slug] = leader
            self._observations[slug].append((ts, leader))
            cutoff = ts - max(self.WINDOWS_SEC)
            while self._observations[slug] and self._observations[slug][0][0] < cutoff:
                self._observations[slug].popleft()
            while self._crossings[slug] and self._crossings[slug][0] < cutoff:
                self._crossings[slug].popleft()
            if len(self._observations) > 64:
                stale = sorted(self._observations, key=lambda key: self._observations[key][-1][0])
                for old_slug in stale[:-48]:
                    self._observations.pop(old_slug, None); self._crossings.pop(old_slug, None)
                    self._last_leader.pop(old_slug, None); self._total_crossings.pop(old_slug, None)
            result = self._counts_locked(slug, ts, leader)
            result.update({"available": True, "stale_or_duplicate_tick_ignored": False})
            return result

    def snapshot(self, slug: str, now_ts: float) -> dict[str, Any]:
        with self._lock:
            obs = self._observations.get(slug)
            if not obs:
                return {"crossing_observation_available": False,
                        "crossing_observation_reason": "no_canonical_observations"}
            latest_ts, leader = obs[-1]
            counts = self._counts_locked(slug, float(now_ts), leader)
            counts["crossing_observation_available"] = True
            counts["last_canonical_observation_age_sec"] = max(0.0, float(now_ts) - latest_ts)
            return counts

    def _counts_locked(self, slug: str, now_ts: float, leader: str) -> dict[str, Any]:
        ticks = self._crossings.get(slug, ())
        result: dict[str, Any] = {"current_leader": leader, "crossings_total": self._total_crossings.get(slug, 0)}
        for window in self.WINDOWS_SEC:
            result[f"crossings_last_{window}s"] = sum(ts >= now_ts - window for ts in ticks)
        return result


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def classify_time_et(epoch: float | None) -> tuple[str | None, str | None]:
    ts = _finite_float(epoch)
    if ts is None:
        return None, None
    local = datetime.fromtimestamp(ts, ZoneInfo("America/New_York"))
    return ("weekend" if local.weekday() >= 5 else "weekday", f"{(local.hour // 6) * 6:02d}-{(local.hour // 6 + 1) * 6:02d}")


def build_shadow_labels(*, edge_ps: float | None, robust_net_usdc: float | None,
                        fair: float | None, entry_price: float | None,
                        abs_distance_bps: float | None, safety_sigma: float | None,
                        crossings_last_120s: int | None, weekend: bool | None,
                        depth_adequate: bool | None) -> dict[str, Any]:
    """Transparent observational bins/components; output cannot gate a trade."""
    labels: list[str] = []
    if edge_ps is None:
        edge_label = "SHADOW_EDGE_UNAVAILABLE"
    elif edge_ps <= 0:
        edge_label = "SHADOW_EDGE_NEGATIVE"
    elif edge_ps < 0.01:
        edge_label = "SHADOW_EDGE_THIN"
    else:
        edge_label = "SHADOW_EDGE_OK"
    labels.append(edge_label)
    if abs_distance_bps is not None:
        if abs_distance_bps < 1:
            labels.append("SHADOW_STRIKE_NEAR_1BPS")
        elif abs_distance_bps < 2:
            labels.append("SHADOW_STRIKE_NEAR_2BPS")
        elif abs_distance_bps < 5:
            labels.append("SHADOW_STRIKE_NEAR_5BPS")
    if safety_sigma is not None and safety_sigma < 1:
        labels.append("SHADOW_LOW_SAFETY_SIGMA")
    if crossings_last_120s is not None and crossings_last_120s > 0:
        labels.append("SHADOW_RECENT_CROSSING")
    if crossings_last_120s is not None and crossings_last_120s >= 2:
        labels.append("SHADOW_HIGH_CROSSING_COUNT")
    if weekend is True:
        labels.append("SHADOW_WEEKEND")
    if entry_price is not None and fair is not None and entry_price > fair and edge_ps is not None and edge_ps < 0.01:
        labels.append("SHADOW_HIGH_PRICE_LOW_EDGE")
    if depth_adequate is False:
        labels.append("SHADOW_WEAK_DEPTH")

    risk_components = [
        abs_distance_bps is not None and abs_distance_bps < 2,
        safety_sigma is not None and safety_sigma < 1,
        crossings_last_120s is not None and crossings_last_120s > 0,
        edge_ps is not None and edge_ps < 0.01,
        fair is not None and entry_price is not None and entry_price > fair,
        depth_adequate is False,
        weekend is True,
    ]
    component_count = sum(bool(x) for x in risk_components)
    risk_level = "LOW" if component_count <= 1 else "MEDIUM" if component_count <= 3 else "HIGH"
    # Simple count-based counterfactual tiers, not optimized weights and not used by sizing.
    shadow_multiplier = 1.0 if component_count == 0 else 0.75 if component_count == 1 else 0.5 if component_count == 2 else 0.25
    reject_reasons = []
    if robust_net_usdc is not None and robust_net_usdc < 0:
        reject_reasons.append("negative_robust_net")
    if edge_ps is not None and edge_ps < 0.01:
        reject_reasons.append("thin_edge")
    if abs_distance_bps is not None and abs_distance_bps < 2:
        reject_reasons.append("near_strike")
    if crossings_last_120s is not None and crossings_last_120s > 0:
        reject_reasons.append("recent_crossing")
    if weekend is True and abs_distance_bps is not None and abs_distance_bps < 5:
        reject_reasons.append("weekend_near_strike")
    if entry_price is not None and fair is not None and entry_price > fair and edge_ps is not None and edge_ps < 0.01:
        reject_reasons.append("high_price_low_edge")
    return {"shadow_edge_verdict": edge_label, "shadow_labels": labels,
            "shadow_entry_risk_level": risk_level, "shadow_risk_component_count": component_count,
            "shadow_size_multiplier": shadow_multiplier,
            "shadow_reject": bool(reject_reasons), "shadow_reject_reason": ";".join(reject_reasons) or None,
            "shadow_only": True, "shadow_has_order_authority": False}


def build_safety_sigma(*, spot: float | None, strike: float | None,
                       sigma_annual: float | None, time_left_sec: float | None,
                       sigma_source: str | None) -> dict[str, Any]:
    spot_v, strike_v = _finite_float(spot), _finite_float(strike)
    sigma_v, tleft = _finite_float(sigma_annual), _finite_float(time_left_sec)
    remaining = None
    if spot_v is not None and strike_v is not None and sigma_v is not None and tleft is not None and spot_v > 0 and strike_v > 0 and sigma_v > 0 and tleft > 0:
        remaining = spot_v * sigma_v * (tleft / (365.25 * 24 * 3600)) ** 0.5
    value = abs(spot_v - strike_v) / remaining if remaining is not None and remaining > 0 else None
    return {"safety_sigma": value, "safety_sigma_source": sigma_source if value is not None else None,
            "estimated_remaining_volatility_usd": remaining,
            "safety_sigma_unavailable_reason": None if value is not None else "forecast_sigma_or_canonical_spot_strike_unavailable"}
