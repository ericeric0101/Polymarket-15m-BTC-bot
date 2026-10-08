"""Research-only live entry annotations; never returns order authority."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import datetime
import math
from math import floor
from typing import Any
from zoneinfo import ZoneInfo


class ResearchCandidateLifecycle:
    """Bounded in-memory identity and snapshot-rate control for research only."""

    TTL_SEC = 900.0
    HEARTBEAT_SEC = 7.0
    MATERIAL_MIN_INTERVAL_SEC = 0.5
    SUMMARY_INTERVAL_SEC = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._completed: dict[str, dict[str, Any]] = {}
        self._last_summary_ts = 0.0
        self.counters = {
            "research_snapshots_written": 0,
            "research_snapshots_suppressed": 0,
            "research_snapshot_bytes_estimate": 0,
            "research_candidates_started": 0,
            "research_candidates_completed": 0,
            "research_snapshot_write_failures": 0,
        }

    @staticmethod
    def _material_signature(snapshot: dict[str, Any], *, should_quote: bool,
                            tick_size: float | None = None) -> tuple[Any, ...]:
        price = _finite_float(snapshot.get("entry_price"))
        tick = _finite_float(tick_size) or 0.01
        return (
            floor(price / tick + 1e-9) if price is not None and tick > 0 else None,
            snapshot.get("gross_edge_bucket"), snapshot.get("net_edge_bucket"),
            snapshot.get("strike_distance_bucket"), snapshot.get("shadow_entry_risk_level"),
            snapshot.get("current_leader"), snapshot.get("crossings_last_120s"),
            bool(should_quote), snapshot.get("actual_size_multiplier"),
        )

    def candidate(self, *, slug: str, instrument_id: str, intended_side: str,
                  now_ts: float) -> tuple[str, dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        key = (str(slug), str(instrument_id), str(intended_side).upper())
        expired: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            for old_key, old_state in tuple(self._active.items()):
                if old_key == key:
                    continue
                status = None
                if old_key[0] != key[0]:
                    status = "market_rolled"
                elif old_key[0] == key[0] and old_key[2] != key[2]:
                    status = "candidate_invalidated"
                elif now_ts - old_state["last_seen_ts"] > self.TTL_SEC:
                    status = "candidate_expired"
                if status:
                    old_state["status"] = status
                    expired.append((old_state["candidate_id"], dict(old_state)))
                    self._active.pop(old_key, None)
                    self.counters["research_candidates_completed"] += 1
            state = self._active.get(key)
            if state is not None and now_ts - state["last_seen_ts"] > self.TTL_SEC:
                state["status"] = "candidate_expired"
                expired.append((state["candidate_id"], dict(state)))
                self._active.pop(key, None)
                state = None
                self.counters["research_candidates_completed"] += 1
            if state is None or state.get("status") in {"candidate_submitted", "candidate_filled", "candidate_invalidated", "candidate_expired", "market_rolled"}:
                candidate_id = f"{key[0]}|{key[1]}|{key[2]}|{time.time_ns()}"
                state = {
                    "candidate_id": candidate_id, "slug": key[0], "instrument_id": key[1],
                    "intended_side": key[2], "episode_start_ts": float(now_ts),
                    "last_seen_ts": float(now_ts), "status": "candidate_active",
                    "update_count": 0, "first_eligible_ts": None, "last_signature": None,
                    "last_emit_ts": 0.0,
                }
                self._active[key] = state
                self.counters["research_candidates_started"] += 1
                while len(self._active) > 512:
                    oldest_key = min(self._active, key=lambda k: self._active[k]["last_seen_ts"])
                    old = self._active.pop(oldest_key)
                    old["status"] = "candidate_expired"
                    expired.append((old["candidate_id"], dict(old)))
                    self.counters["research_candidates_completed"] += 1
            else:
                state["last_seen_ts"] = float(now_ts)
            return state["candidate_id"], dict(state), expired

    def should_emit(self, candidate_id: str, snapshot: dict[str, Any], *, now_ts: float,
                    should_quote: bool, tick_size: float | None = None) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            state = next((s for s in self._active.values() if s["candidate_id"] == candidate_id), None)
            if state is None:
                return True, {"candidate_id": candidate_id, "status": "candidate_active", "update_count": 0}
            signature = self._material_signature(snapshot, should_quote=should_quote, tick_size=tick_size)
            first = state["last_signature"] is None
            previously_eligible = bool(state["last_signature"] and state["last_signature"][7])
            material = signature != state["last_signature"]
            eligibility_changed = bool(state["last_signature"] and signature[7] != state["last_signature"][7])
            heartbeat = now_ts - state["last_emit_ts"] >= self.HEARTBEAT_SEC
            material_due = material and now_ts - state["last_emit_ts"] >= self.MATERIAL_MIN_INTERVAL_SEC
            emit = first or eligibility_changed or material_due or heartbeat
            if emit:
                state["last_signature"] = signature
                state["last_emit_ts"] = float(now_ts)
                state["update_count"] += 1
                if should_quote and state["first_eligible_ts"] is None:
                    state["first_eligible_ts"] = float(now_ts)
                if previously_eligible and not should_quote:
                    state["status"] = "candidate_invalidated"
            else:
                self.counters["research_snapshots_suppressed"] += 1
            enriched = dict(state)
            enriched["last_seen_ts"] = float(now_ts)
            return emit, enriched

    def record_write_result(self, snapshot_bytes: int, *, success: bool) -> None:
        with self._lock:
            if success:
                self.counters["research_snapshots_written"] += 1
                self.counters["research_snapshot_bytes_estimate"] += max(0, int(snapshot_bytes))
            else:
                self.counters["research_snapshot_write_failures"] += 1

    def complete(self, candidate_id: str, status: str) -> dict[str, Any] | None:
        with self._lock:
            for key, state in tuple(self._active.items()):
                if state["candidate_id"] == candidate_id:
                    if state.get("status") != "candidate_invalidated":
                        state["status"] = str(status)
                    self.counters["research_candidates_completed"] += 1
                    self._active.pop(key, None)
                    self._completed[candidate_id] = dict(state)
                    while len(self._completed) > 512:
                        self._completed.pop(next(iter(self._completed)))
                    return dict(state)
            prior = self._completed.get(candidate_id)
            if prior is not None and status == "candidate_filled":
                prior["status"] = status
                return dict(prior)
        return None

    def mark_eligible(self, candidate_id: str, now_ts: float) -> None:
        with self._lock:
            for state in self._active.values():
                if state["candidate_id"] == candidate_id:
                    if state["first_eligible_ts"] is None:
                        state["first_eligible_ts"] = float(now_ts)
                    return

    def summary_due(self, now_ts: float) -> dict[str, Any] | None:
        with self._lock:
            if now_ts - self._last_summary_ts < self.SUMMARY_INTERVAL_SEC:
                return None
            self._last_summary_ts = float(now_ts)
            return dict(self.counters)


def edge_semantics(*, probability: float | None, entry_price: float | None,
                   fee_per_share: float | None, execution_penalty_per_share: float | None,
                   method: str) -> dict[str, Any]:
    """Calculate gross edge independently; never substitute missing costs with zero."""
    gross = probability - entry_price if probability is not None and entry_price is not None else None
    # Historical evidence parser only; current maker emits maker_quote_economics.
    comparable = str(method) == "fast_follow_resolution_ev_minus_fee_minus_markout"
    complete = bool(comparable and gross is not None and fee_per_share is not None
                    and execution_penalty_per_share is not None)
    net = gross - fee_per_share - execution_penalty_per_share if complete else None
    gross_bucket = "unavailable" if gross is None else "negative" if gross <= 0 else "thin" if gross < .01 else "positive"
    net_bucket = "unavailable" if net is None else "negative" if net <= 0 else "thin" if net < .01 else "positive"
    return {
        "gross_probability_edge_ps": gross,
        "net_directional_edge_ps": net,
        "edge_cost_complete": complete,
        "net_edge_method": method if complete or str(method).startswith("maker_") else "incomplete",
        "gross_edge_bucket": gross_bucket,
        "net_edge_bucket": net_bucket,
    }


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
                        depth_adequate: bool | None,
                        gross_edge_ps: float | None = None,
                        edge_cost_complete: bool | None = None) -> dict[str, Any]:
    """Transparent observational bins/components; output cannot gate a trade."""
    labels: list[str] = []
    net_available = edge_ps is not None and edge_cost_complete is not False
    if not net_available:
        edge_label = "SHADOW_EDGE_UNAVAILABLE"
    elif edge_ps <= 0:
        edge_label = "SHADOW_EDGE_NEGATIVE"
    elif edge_ps < 0.01:
        edge_label = "SHADOW_EDGE_THIN"
    else:
        edge_label = "SHADOW_EDGE_OK"
    labels.append(edge_label)
    if gross_edge_ps is not None:
        labels.append("SHADOW_GROSS_EDGE_POSITIVE" if gross_edge_ps > 0 else "SHADOW_GROSS_EDGE_NONPOSITIVE")
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
        net_available and edge_ps < 0.01,
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
    if net_available and edge_ps < 0.01:
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


def build_diffusion_flip_z(*, spot: float | None, target: float | None, sigma_annual: float | None,
                           time_left_sec: float | None, twap_window_sec: float | None,
                           remaining_window_sec: float | None) -> dict[str, Any]:
    """Driftless-diffusion z of the settlement average versus its flip boundary.

    Settlement is the arithmetic average of the final ``W`` seconds. For a
    Brownian log-price the variance of that average seen from ``T`` seconds
    before expiry is ``sigma^2 * ((T - W) + W / 3)``; inside the final window,
    only the unobserved ``tau`` seconds remain random, with variance
    ``sigma^2 * tau / 3`` (the target is then the exact remaining-average
    boundary). ``sigma_annual`` must be the raw past-only realized estimate:
    no TTE decay, floor or implied-volatility adjustment. Under this model
    P(terminal flip) = Phi(-z). It is a terminal-settlement quantity, not a
    probability of touching the strike before expiry.
    """
    unavailable = {"required_move_z_diffusion": None, "p_terminal_flip_diffusion": None,
                   "required_move_z_variance_horizon_sec": None,
                   "required_move_z_sigma_source": "raw_realized_chainlink_no_decay",
                   "required_move_z_model": "driftless_log_bm_final_average_v1"}
    spot_v, target_v = _finite_float(spot), _finite_float(target)
    sigma_v, tleft, window = _finite_float(sigma_annual), _finite_float(time_left_sec), _finite_float(twap_window_sec)
    if None in (spot_v, target_v, sigma_v, tleft, window) or min(spot_v, target_v, sigma_v) <= 0 or window <= 0:
        return unavailable
    remaining = _finite_float(remaining_window_sec)
    if remaining is not None:
        horizon = max(0.0, remaining) / 3.0
    elif tleft > window:
        horizon = (tleft - window) + window / 3.0
    else:
        return unavailable  # final window without an observed partial integral
    if horizon <= 0:
        return unavailable
    z = abs(math.log(target_v / spot_v)) / (sigma_v * math.sqrt(horizon / (365.25 * 24 * 3600)))
    return {**unavailable, "required_move_z_diffusion": z,
            "p_terminal_flip_diffusion": 0.5 * math.erfc(z / math.sqrt(2.0)),
            "required_move_z_variance_horizon_sec": horizon}


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
