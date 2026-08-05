"""
bot/signal_engine.py – Continuous Probabilistic Side Decision Engine

Replaces the legacy 3-signal integer voting system with a continuous
probabilistic approach using:
  Layer 1: Market mid-price consensus  (40%–80% weight)
  Layer 2: BTC spot EMA microstructure (0%–35% weight)
  Layer 3: Strike proximity z-score    (0%–25% weight)

Weights shift dynamically with time-to-expiry: near settlement the market
consensus dominates; at the start BTC spot leads.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Deque, Dict, Optional


# ---------------------------------------------------------------------------
# EMA helper – exponential moving average with wall-clock span
# ---------------------------------------------------------------------------
class EMA:
    """Wall-clock-based EMA tracker.  Feed (price, timestamp) pairs."""

    __slots__ = ("span_sec", "_value", "_last_ts")

    def __init__(self, span_sec: float = 3.0) -> None:
        self.span_sec = max(0.1, span_sec)
        self._value: Optional[float] = None
        self._last_ts: float = 0.0

    @property
    def value(self) -> Optional[float]:
        return self._value

    def update(self, price: float, ts: float) -> float:
        if self._value is None or ts <= self._last_ts:
            self._value = price
            self._last_ts = ts
            return price
        dt = ts - self._last_ts
        alpha = 1.0 - math.exp(-dt / self.span_sec)
        self._value = alpha * price + (1.0 - alpha) * self._value
        self._last_ts = ts
        return self._value

    def reset(self) -> None:
        self._value = None
        self._last_ts = 0.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class SideSignals:
    """Output of a single SignalEngine.compute() call."""

    market_consensus: float = 0.0    # [-1, +1] from mid-price
    market_momentum: float = 0.0     # [-1, +1] from mid-price rate-of-change
    btc_trend: float = 0.0           # [-1, +1] from BTC EMA slope
    strike_proximity: float = 0.0    # [-1, +1] from z-score
    composite_score: float = 0.0     # weighted sum
    confidence: float = 0.0          # 0~1
    time_left_ratio: float = 1.0     # 0~1  (1 = market just opened)

    # Per-layer weights actually used (for logging/diagnostics)
    w_market: float = 0.0
    w_btc: float = 0.0
    w_strike: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_consensus": round(self.market_consensus, 4),
            "market_momentum": round(self.market_momentum, 4),
            "btc_trend": round(self.btc_trend, 4),
            "strike_proximity": round(self.strike_proximity, 4),
            "composite_score": round(self.composite_score, 4),
            "confidence": round(self.confidence, 4),
            "time_left_ratio": round(self.time_left_ratio, 4),
            "w_market": round(self.w_market, 4),
            "w_btc": round(self.w_btc, 4),
            "w_strike": round(self.w_strike, 4),
        }


@dataclass
class SignalEngineConfig:
    """Tuneable parameters for the SignalEngine."""

    # EMA spans (seconds)
    btc_ema_fast_sec: float = 3.0
    btc_ema_slow_sec: float = 10.0

    # Mid-price momentum EMA
    mid_ema_fast_sec: float = 5.0
    mid_ema_slow_sec: float = 20.0

    # Weight anchors
    w_market_base: float = 0.40   # market weight at t_ratio=1.0
    w_market_max: float = 0.80    # market weight at t_ratio=0.0
    w_btc_max: float = 0.35       # btc weight at t_ratio=1.0
    w_strike_max: float = 0.25    # strike weight at t_ratio=1.0

    # Market mid remains a useful implied-probability baseline, but its
    # directional contribution is deliberately capped so the bot does not
    # blindly chase Polymarket's own repricing.
    market_alpha_scale: float = 0.65

    # Market cycle duration (sec) for t_ratio normalisation
    market_duration_sec: float = 900.0

    # Confidence threshold – below this → NONE (don't bet)
    min_confidence: float = 0.15

    # Mid-price velocity threshold for reversal detection
    mid_velocity_reversal_threshold: float = 0.010  # mid/sec

    # BTC trend normalisation: slope is divided by this to get [-1,+1]
    btc_trend_norm_pct: float = 0.0005  # 0.05% per second

    # Maximum history for mid-price deque
    mid_history_maxlen: int = 120


# ---------------------------------------------------------------------------
# SignalEngine
# ---------------------------------------------------------------------------
class SignalEngine:
    """
    Continuous probabilistic side-decision signal engine.

    Call ``update_btc_price()`` from the Binance WS callback.
    Call ``update_market_mid()`` each quote-loop iteration.
    Call ``compute()`` when a side decision is needed.
    """

    def __init__(self, config: Optional[SignalEngineConfig] = None) -> None:
        self.config = config or SignalEngineConfig()

        # Layer 2: BTC spot EMAs
        self._btc_ema_fast = EMA(span_sec=self.config.btc_ema_fast_sec)
        self._btc_ema_slow = EMA(span_sec=self.config.btc_ema_slow_sec)
        self._last_btc_price: Optional[float] = None
        self._last_btc_ts: float = 0.0

        # Layer 1: Market mid-price tracking
        self._mid_history: Deque[tuple[float, float]] = deque(
            maxlen=self.config.mid_history_maxlen
        )
        self._mid_ema_fast = EMA(span_sec=self.config.mid_ema_fast_sec)
        self._mid_ema_slow = EMA(span_sec=self.config.mid_ema_slow_sec)

    # ------------------------------------------------------------------
    # Public update hooks
    # ------------------------------------------------------------------

    def update_btc_price(self, price: Decimal, ts: float) -> None:
        """Called from Binance WS callback with each aggTrade."""
        px = float(price)
        if px <= 0:
            return
        self._btc_ema_fast.update(px, ts)
        self._btc_ema_slow.update(px, ts)
        self._last_btc_price = px
        self._last_btc_ts = ts

    def update_market_mid(self, mid: Decimal, ts: float) -> None:
        """Called from quote loop with current UP token mid-price."""
        mid_f = float(mid)
        if mid_f <= 0 or mid_f >= 1.0:
            return
        self._mid_history.append((ts, mid_f))
        self._mid_ema_fast.update(mid_f, ts)
        self._mid_ema_slow.update(mid_f, ts)

    def reset(self) -> None:
        """Reset all state for a new market cycle."""
        self._btc_ema_fast.reset()
        self._btc_ema_slow.reset()
        self._last_btc_price = None
        self._last_btc_ts = 0.0
        self._mid_history.clear()
        self._mid_ema_fast.reset()
        self._mid_ema_slow.reset()

    # ------------------------------------------------------------------
    # Core computation
    # ------------------------------------------------------------------

    def compute(
        self,
        spot: Optional[Decimal],
        strike: Optional[Decimal],
        sigma: Optional[Decimal],
        time_left_sec: Optional[float],
        market_mid: Optional[Decimal],
    ) -> SideSignals:
        """
        Compute composite side signal.

        Parameters
        ----------
        spot : current BTC spot price
        strike : market strike (price-to-beat)
        sigma : realised annual σ
        time_left_sec : seconds until expiry
        market_mid : UP token mid-price (0.01–0.99)

        Returns
        -------
        SideSignals with composite_score in [-1, +1] range.
        Positive = UP, Negative = DOWN.
        """
        sig = SideSignals()

        # -- Time ratio --
        duration = max(1.0, self.config.market_duration_sec)
        t_left = max(0.0, float(time_left_sec or 0.0))
        sig.time_left_ratio = min(1.0, t_left / duration)

        # -- Dynamic weights --
        t_ratio = sig.time_left_ratio
        sig.w_market = self.config.w_market_base + (
            self.config.w_market_max - self.config.w_market_base
        ) * (1.0 - t_ratio)
        sig.w_btc = self.config.w_btc_max * t_ratio
        sig.w_strike = self.config.w_strike_max * t_ratio

        # ---------------------------------------------------
        # Layer 1: Market Consensus
        # ---------------------------------------------------
        market_available = False
        if market_mid is not None:
            mid_f = float(market_mid)
            if 0.01 < mid_f < 0.99:
                market_available = True
                # Linear mapping: mid=0.50 → 0, mid=0.75 → +0.50, mid=0.25 → -0.50
                sig.market_consensus = (mid_f - 0.50) * 2.0
                sig.market_consensus = max(-1.0, min(1.0, sig.market_consensus))

                # Mid-price momentum: EMA fast vs slow crossover
                fast_v = self._mid_ema_fast.value
                slow_v = self._mid_ema_slow.value
                if fast_v is not None and slow_v is not None and slow_v > 0:
                    # Positive when fast > slow (mid rising)
                    raw_mom = (fast_v - slow_v) / slow_v
                    # Normalise: ±2% diff → ±1.0
                    sig.market_momentum = max(-1.0, min(1.0, raw_mom / 0.02))

        # ---------------------------------------------------
        # Layer 2: BTC Spot Microstructure (EMA slope)
        # ---------------------------------------------------
        fast_v = self._btc_ema_fast.value
        slow_v = self._btc_ema_slow.value
        btc_available = fast_v is not None and slow_v is not None and slow_v > 0
        if btc_available:
            # Positive slope when fast > slow
            slope_pct = (fast_v - slow_v) / slow_v
            norm = self.config.btc_trend_norm_pct
            if norm > 0:
                sig.btc_trend = max(-1.0, min(1.0, slope_pct / norm))

        # ---------------------------------------------------
        # Layer 3: Strike Proximity z-score
        # ---------------------------------------------------
        spot_f = float(spot) if spot is not None else 0.0
        strike_f = float(strike) if strike is not None else 0.0
        sigma_f = float(sigma) if sigma is not None else 0.0
        strike_available = spot_f > 0 and strike_f > 0 and sigma_f > 0 and t_left > 0
        if strike_available:
            # z-score in σ√T units
            t_years = t_left / (365.0 * 24.0 * 3600.0)
            denom = strike_f * sigma_f * math.sqrt(max(1e-12, t_years))
            if denom > 0:
                z = (spot_f - strike_f) / denom
                sig.strike_proximity = math.tanh(z)  # compress to [-1, +1]
            else:
                strike_available = False

        # Only available layers participate in the composite. This prevents
        # an un-warmed EMA or missing strike from retaining weight while
        # contributing a zero signal.
        raw_weights = {
            "market": sig.w_market if market_available else 0.0,
            "btc": sig.w_btc if btc_available else 0.0,
            "strike": sig.w_strike if strike_available else 0.0,
        }
        weight_sum = sum(raw_weights.values())
        if weight_sum <= 0:
            sig.w_market = sig.w_btc = sig.w_strike = 0.0
        else:
            sig.w_market = raw_weights["market"] / weight_sum
            sig.w_btc = raw_weights["btc"] / weight_sum
            sig.w_strike = raw_weights["strike"] / weight_sum

        # ---------------------------------------------------
        # Composite score
        # ---------------------------------------------------
        # Blend market momentum into market signal (30% momentum, 70% level)
        market_alpha_scale = max(0.0, min(1.0, self.config.market_alpha_scale))
        effective_market = market_alpha_scale * (
            0.70 * sig.market_consensus + 0.30 * sig.market_momentum
        )

        sig.composite_score = (
            sig.w_market * effective_market
            + sig.w_btc * sig.btc_trend
            + sig.w_strike * sig.strike_proximity
        )
        sig.composite_score = max(-1.0, min(1.0, sig.composite_score))

        # Confidence reflects the strength of the usable composite. Missing
        # layers have already been excluded and reweighted above, so do not
        # apply a second ``available_layers / 3`` penalty here. A later
        # freshness/data-quality layer can reduce this value when a source is
        # stale; absence alone must not double-penalize the signal.
        has_signal = weight_sum > 0
        sig.confidence = abs(sig.composite_score) if has_signal else 0.0

        return sig

    # ------------------------------------------------------------------
    # Mid-price velocity (for reversal detection)
    # ------------------------------------------------------------------

    def mid_price_velocity(self, lookback_sec: float = 10.0) -> float:
        """
        Compute mid-price change rate (per second) over the last *lookback_sec*.

        Returns positive if mid is rising, negative if falling.
        Useful for detecting rapid reversals near settlement.
        """
        if len(self._mid_history) < 2:
            return 0.0
        now_ts = self._mid_history[-1][0]
        cutoff = now_ts - lookback_sec
        # Find the oldest sample within the lookback window
        oldest_ts, oldest_mid = self._mid_history[-1]
        for ts, mid in self._mid_history:
            if ts >= cutoff:
                oldest_ts, oldest_mid = ts, mid
                break
        latest_ts, latest_mid = self._mid_history[-1]
        dt = latest_ts - oldest_ts
        if dt < 0.5:  # need at least 0.5s of data
            return 0.0
        return (latest_mid - oldest_mid) / dt

    def is_mid_reversal(self, holding_up: bool) -> bool:
        """
        Check if mid-price is rapidly moving against the held position.

        Parameters
        ----------
        holding_up : True if holding UP tokens, False if DOWN.

        Returns
        -------
        True if a reversal is detected.
        """
        velocity = self.mid_price_velocity()
        threshold = self.config.mid_velocity_reversal_threshold
        if holding_up and velocity < -threshold:
            return True
        if not holding_up and velocity > threshold:
            return True
        return False
