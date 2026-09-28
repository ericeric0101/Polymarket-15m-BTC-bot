"""Bounded, event-driven research model for the official Polymarket TWAP.

It intentionally has no strategy/order imports.  Returned features and emitted
events are research observations only; callers decide where to persist them.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from typing import Any
from pathlib import Path
import shutil
import time


def _f(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def _side(value: Decimal | None, strike: Decimal | None) -> str:
    if value is None or strike is None:
        return "UNKNOWN"
    return "UP" if value > strike else "DOWN" if value < strike else "TIE"


@dataclass(frozen=True)
class TwapSample:
    observed_ts: float
    source_ts: float
    fast_spot: Decimal | None
    official_twap: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    strike: Decimal | None
    time_left_sec: float | None


class TwapForwardShadow:
    """Per-market fixed-memory official-TWAP projection experiment."""

    def __init__(self, *, max_samples: int = 180, trend_cap_bps: float = 25.0,
                 db: Any = None, run_id: str = "", max_db_mb: float = 500.0,
                 min_free_disk_gb: float = 10.0, storage_check_interval_sec: float = 60.0) -> None:
        # Production constructs this with 180 samples; accepting a smaller
        # bound is useful for deterministic unit tests and does not alter the
        # production default.
        self.max_samples = max(1, int(max_samples))
        self.trend_cap_bps = max(0.0, float(trend_cap_bps))
        self._samples: dict[str, deque[TwapSample]] = defaultdict(lambda: deque(maxlen=self.max_samples))
        self._summary: dict[str, dict[str, Any]] = {}
        self._latest: dict[str, dict[str, Any]] = {}
        self.db, self.run_id, self.max_db_mb = db, str(run_id), float(max_db_mb)
        self.min_free_disk_gb = max(0.0, float(min_free_disk_gb))
        self.storage_check_interval_sec = max(1.0, float(storage_check_interval_sec))
        self._checkpoint_done: dict[str, set[int]] = defaultdict(set)
        self._last_emitted_sign: dict[str, int] = {}
        self._last_emitted_projected_side: dict[str, str] = {}
        self._storage_guard_triggered = False
        self._storage_guard_reason = ""
        self._last_storage_check_ts = 0.0
        self._lock = Lock()

    def sample_count(self, slug: str) -> int:
        with self._lock:
            return len(self._samples[str(slug)])

    @staticmethod
    def _slope(samples: list[TwapSample], now: TwapSample, horizon: float, min_span: float) -> float | None:
        eligible = [item for item in samples if item.official_twap is not None and now.observed_ts - item.observed_ts >= min_span]
        if now.official_twap is None or not eligible:
            return None
        prior = min(eligible, key=lambda item: abs((now.observed_ts - item.observed_ts) - horizon))
        span = now.observed_ts - prior.observed_ts
        if span < min_span or prior.official_twap is None or prior.official_twap <= 0:
            return None
        return float((now.official_twap - prior.official_twap) / prior.official_twap * Decimal("10000") / Decimal(str(span)))

    @staticmethod
    def _spot_slope(samples: list[TwapSample], now: TwapSample) -> float | None:
        eligible = [item for item in samples if item.fast_spot is not None and now.observed_ts - item.observed_ts >= 4.0]
        if now.fast_spot is None or not eligible:
            return None
        prior = min(eligible, key=lambda item: abs((now.observed_ts - item.observed_ts) - 10.0))
        span = now.observed_ts - prior.observed_ts
        if prior.fast_spot is None or prior.fast_spot <= 0 or span < 4:
            return None
        return float((now.fast_spot - prior.fast_spot) / prior.fast_spot * Decimal("10000") / Decimal(str(span)))

    def observe(self, *, slug: str, now_ts: float, source_ts: float, fast_spot: Decimal | None,
                official_twap: Decimal | None, strike: Decimal | None, time_left_sec: float | None,
                best_bid: Decimal | None = None, best_ask: Decimal | None = None) -> dict[str, Any]:
        sample = TwapSample(float(now_ts), float(source_ts), fast_spot, official_twap, best_bid, best_ask, strike, time_left_sec)
        with self._lock:
            samples = self._samples[str(slug)]
            history = list(samples)
            samples.append(sample)
        twap_strike = ((official_twap - strike) / strike * Decimal("10000")) if official_twap and strike and strike > 0 else None
        spot_twap = ((fast_spot - official_twap) / official_twap * Decimal("10000")) if fast_spot and official_twap and official_twap > 0 else None
        slope5 = self._slope(history, sample, 5.0, 4.0)
        slope10 = self._slope(history, sample, 10.0, 8.0)
        spot_slope = self._spot_slope(history, sample)
        remaining = max(0.0, float(time_left_sec or 0.0))
        # This is deliberately a pressure approximation, not a claim about
        # Chainlink's outgoing observation weights.
        flat = fast_spot if fast_spot is not None else official_twap
        trend_spot = flat
        if flat is not None and spot_slope is not None:
            cap = Decimal(str(self.trend_cap_bps)) / Decimal("10000")
            movement = Decimal(str(spot_slope)) / Decimal("10000") * Decimal(str(remaining))
            movement = max(-cap, min(cap, movement))
            trend_spot = flat * (Decimal("1") + movement)
        # A conservative first-order blend: each remaining second replaces
        # approximately 1/60 of a 60-second rolling window.  It is labelled
        # projection, never used as the official settlement value.
        replace_fraction = min(Decimal("1"), Decimal(str(remaining)) / Decimal("60"))
        projected_flat = official_twap + (flat - official_twap) * replace_fraction if official_twap and flat else None
        projected_trend = official_twap + (trend_spot - official_twap) * replace_fraction if official_twap and trend_spot else None
        eta = None
        if twap_strike is not None and slope10 is not None and abs(slope10) > 1e-9:
            # eta is valid only when the slope points toward the strike.
            eta_value = -float(twap_strike) / slope10
            if eta_value > 0 and eta_value <= remaining:
                eta = eta_value
        agreement = spot_twap is not None and slope10 is not None and ((spot_twap > 0) == (slope10 > 0))
        confidence = "HIGH" if agreement and slope10 is not None and len(history) >= 10 else "MEDIUM" if slope10 is not None else "UNAVAILABLE"
        result = {
            "market_slug": str(slug), "observed_ts": float(now_ts), "source_ts": float(source_ts),
            "fast_spot": _f(fast_spot), "official_current_twap": _f(official_twap),
            "fast_spot_source": "binance_ws", "best_bid": _f(best_bid), "best_ask": _f(best_ask),
            "strike": _f(strike), "time_left_sec": time_left_sec,
            "twap_minus_strike_bps": _f(twap_strike), "spot_minus_twap_bps": _f(spot_twap),
            "twap_slope_5s_bps_per_sec": slope5, "twap_slope_10s_bps_per_sec": slope10,
            "approx_replacement_pressure_bps_per_sec": spot_slope,
            "projected_settlement_twap_flat": _f(projected_flat),
            "projected_settlement_twap_flat_bps_vs_strike": _f((projected_flat - strike) / strike * Decimal("10000")) if projected_flat and strike else None,
            "projected_settlement_twap_trend": _f(projected_trend),
            "projected_settlement_twap_trend_bps_vs_strike": _f((projected_trend - strike) / strike * Decimal("10000")) if projected_trend and strike else None,
            "projected_settlement_side_flat": _side(projected_flat, strike),
            "projected_settlement_side_trend": _side(projected_trend, strike),
            "projected_crossing_eta_sec": eta, "crossing_eta_confidence": confidence,
        }
        self._update_summary(str(slug), result)
        with self._lock:
            self._latest[str(slug)] = dict(result)
        self._persist_material(result)
        return result

    def latest(self, slug: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._latest.get(str(slug))
            return dict(value) if value is not None else None

    def _persist(self, slug: str, ts: float, event_type: str, payload: dict[str, Any]) -> None:
        if self.db is None:
            return
        # Compact settlement summaries remain permitted after a storage guard;
        # all optional material/checkpoint traffic is suppressed.
        if self._storage_guard_triggered and event_type != "MARKET_TWAP_SUMMARY":
            return
        try:
            self.db.enqueue_decision(run_id=self.run_id, slug=slug, market_id=None,
                                     decision_epoch_ns=int(ts * 1_000_000_000),
                                     payload={"event_type": event_type, **payload})
        except Exception:
            pass

    def _storage_health(self, now_ts: float) -> dict[str, Any]:
        """Throttle filesystem checks; storage pressure is sticky to restart."""
        if self.db is None:
            return {"checked": False, "triggered": False}
        if now_ts - self._last_storage_check_ts < self.storage_check_interval_sec:
            return {"checked": False, "triggered": self._storage_guard_triggered}
        self._last_storage_check_ts = now_ts
        try:
            path = Path(str(getattr(self.db, "db_path", "")))
            size_mb = path.stat().st_size / 1024 / 1024 if path.is_file() else 0.0
            free_gb = shutil.disk_usage(path.parent if path.parent.exists() else Path(".")).free / 1024 / 1024 / 1024
            reason = "db_size_cap" if size_mb >= self.max_db_mb else "free_disk_low" if free_gb < self.min_free_disk_gb else ""
            if reason and not self._storage_guard_triggered:
                self._storage_guard_triggered, self._storage_guard_reason = True, reason
                # This is a single compact state event, intentionally allowed
                # before optional event suppression begins.
                self._persist_raw("", now_ts, "RESEARCH_STORAGE_GUARD_TRIGGERED", {
                    "db_size_mb": size_mb, "configured_max_db_mb": self.max_db_mb,
                    "disk_free_gb": free_gb, "configured_min_free_disk_gb": self.min_free_disk_gb,
                    "trigger_reason": reason, "timestamp": now_ts,
                })
            return {"checked": True, "triggered": self._storage_guard_triggered, "reason": self._storage_guard_reason,
                    "db_size_mb": size_mb, "disk_free_gb": free_gb}
        except Exception:
            return {"checked": True, "triggered": self._storage_guard_triggered}

    def _persist_raw(self, slug: str, ts: float, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self.db.enqueue_decision(run_id=self.run_id, slug=slug, market_id=None,
                                     decision_epoch_ns=int(max(0.0, ts) * 1_000_000_000),
                                     payload={"event_type": event_type, **payload})
        except Exception:
            pass

    def _persist_material(self, result: dict[str, Any]) -> None:
        slug, ts = str(result["market_slug"]), float(result["observed_ts"])
        self._storage_health(ts)
        value = result.get("twap_minus_strike_bps")
        sign = 1 if value is not None and value > 0 else -1 if value is not None and value < 0 else 0
        if sign and slug in self._last_emitted_sign and sign != self._last_emitted_sign[slug]:
            self._persist(slug, ts, "TWAP_STRIKE_CROSS", result)
        if sign:
            self._last_emitted_sign[slug] = sign
        side = str(result.get("projected_settlement_side_trend") or "UNKNOWN")
        previous = self._last_emitted_projected_side.get(slug)
        if side not in {"UNKNOWN", "TIE"} and previous is not None and side != previous:
            self._persist(slug, ts, "TWAP_PROJECTED_SIDE_CHANGE", result)
        if side not in {"UNKNOWN", "TIE"}:
            self._last_emitted_projected_side[slug] = side
        left = result.get("time_left_sec")
        if left is not None:
            for checkpoint in (120, 60, 30, 15, 10, 5):
                if float(left) <= checkpoint and checkpoint not in self._checkpoint_done[slug]:
                    self._checkpoint_done[slug].add(checkpoint)
                    self._persist(slug, ts, "TMINUS_CHECKPOINT", {**result, "checkpoint_sec": checkpoint})

    def _update_summary(self, slug: str, result: dict[str, Any]) -> None:
        summary = self._summary.setdefault(slug, {"market_slug": slug, "twap_cross_count": 0, "projected_cross_count": 0,
                                                  "min_twap_minus_strike_bps": None, "max_twap_minus_strike_bps": None,
                                                  "max_positive_twap_slope": None, "max_negative_twap_slope": None,
                                                  "min_crossing_eta_sec": None, "last_twap_sign": None, "last_projected_side": None})
        value = result.get("twap_minus_strike_bps")
        if value is not None:
            summary["min_twap_minus_strike_bps"] = value if summary["min_twap_minus_strike_bps"] is None else min(summary["min_twap_minus_strike_bps"], value)
            summary["max_twap_minus_strike_bps"] = value if summary["max_twap_minus_strike_bps"] is None else max(summary["max_twap_minus_strike_bps"], value)
            sign = 1 if value > 0 else -1 if value < 0 else 0
            if summary["last_twap_sign"] is not None and sign and sign != summary["last_twap_sign"]:
                summary["twap_cross_count"] += 1
            if sign: summary["last_twap_sign"] = sign
        slope = result.get("twap_slope_10s_bps_per_sec")
        if slope is not None:
            if slope > 0: summary["max_positive_twap_slope"] = max(summary["max_positive_twap_slope"] or slope, slope)
            if slope < 0: summary["max_negative_twap_slope"] = min(summary["max_negative_twap_slope"] or slope, slope)
        eta = result.get("projected_crossing_eta_sec")
        if eta is not None: summary["min_crossing_eta_sec"] = eta if summary["min_crossing_eta_sec"] is None else min(summary["min_crossing_eta_sec"], eta)
        side = result.get("projected_settlement_side_trend")
        if summary["last_projected_side"] not in (None, side) and side not in ("UNKNOWN", "TIE"):
            summary["projected_cross_count"] += 1
        if side not in ("UNKNOWN", "TIE"): summary["last_projected_side"] = side

    def finalize_market(self, slug: str, *, settlement_side: str, settlement_ts: float | None = None) -> dict[str, Any]:
        with self._lock:
            summary = dict(self._summary.pop(str(slug), {"market_slug": str(slug)}))
            summary["settlement_side"] = str(settlement_side)
            last_sample = self._samples.get(str(slug), deque())
            fallback_ts = float(last_sample[-1].observed_ts) if last_sample else time.time()
            summary["raw_buffer_samples"] = len(self._samples.pop(str(slug), ()))
            self._latest.pop(str(slug), None)
            self._checkpoint_done.pop(str(slug), None)
            self._last_emitted_sign.pop(str(slug), None)
            self._last_emitted_projected_side.pop(str(slug), None)
        summary_ts = float(settlement_ts) if settlement_ts is not None else fallback_ts
        summary["summary_ts"] = summary_ts
        self._persist(str(slug), summary_ts, "MARKET_TWAP_SUMMARY", summary)
        return summary

    def storage_guard_status(self) -> dict[str, Any]:
        return {"triggered": self._storage_guard_triggered, "reason": self._storage_guard_reason,
                "optional_research_writes_enabled": not self._storage_guard_triggered}
