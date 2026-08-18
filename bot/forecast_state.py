"""Shared, immutable forecast inputs for BTC binary-market pricing.

Both quote pricing and side selection must apply the same sigma transforms and
settlement model.  This module deliberately contains no environment reads: the
caller supplies the already-configured policy values.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from execution.maker_engine import MakerEngine


@dataclass(frozen=True)
class ForecastState:
    spot: Decimal
    strike: Decimal
    time_left_sec: float
    reference_source: str
    sigma_default: Decimal
    sigma_raw_realized: Optional[Decimal]
    sigma_input_source: str
    sigma_after_scale: Decimal
    sigma_after_bounds: Decimal
    sigma_time_decay_factor: Optional[Decimal]
    sigma_after_time_decay: Decimal
    sigma_final: Decimal
    implied_sigma: Optional[Decimal]
    implied_sigma_floor: Optional[Decimal]
    implied_sigma_floor_applied: bool
    standard_up_probability: Decimal
    twap_average_up_probability: Optional[Decimal]
    settlement_model: str

    @property
    def selected_up_probability(self) -> Decimal:
        if self.settlement_model == "twap_average_approx" and self.twap_average_up_probability is not None:
            return self.twap_average_up_probability
        return self.standard_up_probability

    def probability_for_outcome(self, outcome: str) -> Decimal:
        return (
            Decimal("1") - self.selected_up_probability
            if str(outcome or "").strip().lower() == "down"
            else self.selected_up_probability
        )

    def diagnostics(self, *, market_mid: Decimal, outcome: str) -> dict[str, object]:
        return {
            "sigma_default": self.sigma_default,
            "sigma_raw_realized": self.sigma_raw_realized,
            "sigma_input_source": self.sigma_input_source,
            "sigma_before_scale": (
                self.sigma_raw_realized
                if self.sigma_raw_realized is not None
                else self.sigma_default
            ),
            "sigma_after_scale": self.sigma_after_scale,
            "sigma_after_bounds": self.sigma_after_bounds,
            "sigma_time_decay_enabled": self.sigma_time_decay_factor is not None,
            "sigma_time_decay_factor": self.sigma_time_decay_factor,
            "sigma_after_time_decay": self.sigma_after_time_decay,
            "sigma": self.sigma_final,
            "implied_sigma": self.implied_sigma,
            "sigma_before_implied_floor": self.sigma_after_time_decay,
            "implied_sigma_floor": self.implied_sigma_floor,
            "implied_sigma_floor_applied": self.implied_sigma_floor_applied,
            "strike": self.strike,
            "time_left_sec": self.time_left_sec,
            "outcome": outcome,
            "market_mid": market_mid,
            "spot": self.spot,
            "reference_source": self.reference_source,
            "standard_up_probability": self.standard_up_probability,
            "twap_average_up_probability": self.twap_average_up_probability,
            "settlement_model": self.settlement_model,
        }


def build_forecast_state(
    *,
    spot: Decimal,
    strike: Decimal,
    time_left_sec: float,
    reference_source: str,
    market_mid: Decimal,
    outcome: str,
    sigma_default: Decimal,
    sigma_raw_realized: Optional[Decimal],
    sigma_scale: Decimal,
    sigma_floor: Decimal,
    sigma_ceiling: Decimal,
    time_decay_enabled: bool,
    time_decay_ref_sec: float,
    time_decay_min: float,
    implied_sigma_enabled: bool,
    twap_window_sec: int,
    observed_twap_average: Optional[Decimal],
    observed_twap_seconds: float,
) -> ForecastState:
    """Apply the existing forecast policy once for either runtime consumer."""
    raw_sigma = sigma_raw_realized if sigma_raw_realized is not None and sigma_raw_realized > 0 else None
    sigma_input = raw_sigma if raw_sigma is not None else sigma_default
    sigma_input_source = "realized_external_spot" if raw_sigma is not None else "default"
    sigma_after_scale = sigma_input * sigma_scale
    sigma_after_bounds = max(sigma_floor, min(sigma_ceiling, sigma_after_scale))

    time_decay_factor: Optional[Decimal] = None
    sigma_after_time_decay = sigma_after_bounds
    if time_decay_enabled and time_left_sec > 0:
        ref = max(1.0, float(time_decay_ref_sec))
        factor = max(float(time_decay_min), min(1.0, float(time_left_sec) / ref))
        time_decay_factor = Decimal(str(round(factor, 4)))
        sigma_after_time_decay = max(sigma_floor, sigma_after_bounds * time_decay_factor)

    sigma_final = sigma_after_time_decay
    implied_sigma: Optional[Decimal] = None
    implied_sigma_floor: Optional[Decimal] = None
    implied_sigma_floor_applied = False
    if (
        implied_sigma_enabled
        and time_left_sec > 30
        and Decimal("0.05") < market_mid < Decimal("0.95")
    ):
        implied_sigma = MakerEngine.implied_sigma_from_market_mid(
            market_mid=float(market_mid),
            spot=float(spot),
            strike=float(strike),
            time_left_sec=time_left_sec,
            outcome=outcome or "up",
        )
        if implied_sigma is not None and implied_sigma > 0:
            implied_sigma_floor = implied_sigma * Decimal("0.6")
            if sigma_final < implied_sigma_floor:
                sigma_final = max(sigma_floor, min(sigma_ceiling, implied_sigma_floor))
                implied_sigma_floor_applied = sigma_final > sigma_after_time_decay

    standard_up_probability = MakerEngine.digital_up_probability(
        spot=float(spot),
        strike=float(strike),
        sigma_annual=float(sigma_final),
        time_left_sec=time_left_sec,
    )
    is_native_twap = str(reference_source or "").startswith("polymarket_chainlink_twap_")
    twap_average_up_probability: Optional[Decimal] = None
    settlement_model = "instantaneous_digital"
    if is_native_twap:
        twap_average_up_probability = MakerEngine.twap_settlement_up_probability(
            spot=float(spot),
            strike=float(strike),
            sigma_annual=float(sigma_final),
            time_left_sec=time_left_sec,
            twap_window_sec=max(1, int(twap_window_sec)),
            observed_window_avg=(
                float(observed_twap_average) if observed_twap_average is not None else None
            ),
            observed_window_sec=float(observed_twap_seconds),
        )
        settlement_model = "twap_average_approx"

    return ForecastState(
        spot=spot,
        strike=strike,
        time_left_sec=time_left_sec,
        reference_source=str(reference_source or ""),
        sigma_default=sigma_default,
        sigma_raw_realized=raw_sigma,
        sigma_input_source=sigma_input_source,
        sigma_after_scale=sigma_after_scale,
        sigma_after_bounds=sigma_after_bounds,
        sigma_time_decay_factor=time_decay_factor,
        sigma_after_time_decay=sigma_after_time_decay,
        sigma_final=sigma_final,
        implied_sigma=implied_sigma,
        implied_sigma_floor=implied_sigma_floor,
        implied_sigma_floor_applied=implied_sigma_floor_applied,
        standard_up_probability=standard_up_probability,
        twap_average_up_probability=twap_average_up_probability,
        settlement_model=settlement_model,
    )
