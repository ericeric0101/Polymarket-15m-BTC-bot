"""Evidence-backed economics for one measured BTC 15m entry regime.

The normal maker path uses market midpoint as fair, so its expected value is
only spread capture.  A historical 10-second adverse markout cannot be a
universal hard veto against that quantity.  This module does *not* reinstate
the rejected raw fair model; it applies a settled, score-conditioned outcome
frequency only inside measured 300-600s directional-distance bins.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, Optional

from execution.rebate_model import QuoteEconomics


MIN_SCORE_ABS = Decimal("0.35")
MIN_TIME_LEFT_SEC = 300.0
MAX_TIME_LEFT_SEC = 600.0
DISTANCE_BUCKETS = (
    ("10_30", Decimal("10"), Decimal("30")),
    ("30_60", Decimal("30"), Decimal("60")),
    ("60_plus", Decimal("60"), None),
)


def apply_strong_directional_regime_economics(
    quote_data: tuple[Any, ...],
    *,
    active_side: str,
    outcome_side: str,
    side_locked: bool,
    side_score: Decimal,
    time_left_sec: Optional[float],
    spot: Optional[Decimal],
    strike: Optional[Decimal],
    calibrations: Optional[dict[str, dict[str, Any]]],
    markout_calibrations: Optional[dict[str, dict[str, Any]]],
    min_expected_net_usdc: Decimal,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Replace only BUY economics when a fully measured regime is eligible.

    The quote target stays unchanged.  Markout remains fully deducted.  This
    function only changes the expected-value basis from passive half-spread to
    a settled, score-conditioned resolution probability.
    """
    details: dict[str, Any] = {"applied": False, "reason": "not_eligible"}
    if not side_locked or str(active_side).upper() != str(outcome_side).upper():
        details["reason"] = "side_not_locked_or_mismatched"
        return quote_data, details
    if abs(side_score) < MIN_SCORE_ABS:
        details["reason"] = "score_below_measured_regime"
        return quote_data, details
    if time_left_sec is None or not (MIN_TIME_LEFT_SEC <= float(time_left_sec) < MAX_TIME_LEFT_SEC):
        details["reason"] = "time_outside_measured_regime"
        return quote_data, details
    if spot is None or strike is None or strike <= 0:
        details["reason"] = "spot_or_strike_unavailable"
        return quote_data, details

    try:
        spot_value = Decimal(str(spot))
        strike_value = Decimal(str(strike))
    except Exception:
        details["reason"] = "spot_or_strike_invalid"
        return quote_data, details
    signed_distance = spot_value - strike_value
    if str(outcome_side).upper() == "DOWN":
        signed_distance = -signed_distance
    bucket = next(
        (
            name
            for name, lower, upper in DISTANCE_BUCKETS
            if lower <= signed_distance and (upper is None or signed_distance < upper)
        ),
        None,
    )
    if bucket is None:
        details["reason"] = "spot_distance_outside_measured_regime"
        return quote_data, details
    calibration = (calibrations or {}).get(bucket)
    if not isinstance(calibration, dict):
        details["reason"] = "distance_bucket_calibration_unavailable"
        return quote_data, details
    try:
        calibrated_win_probability = Decimal(str(calibration["win_probability"]))
    except Exception:
        details["reason"] = "distance_bucket_calibration_invalid"
        return quote_data, details

    price, econ, _should_quote, _robust_net, exec_penalty, _edge_ps, _edge_usdc, fair, fee_ps, _other_cost_ps, components = quote_data
    if not isinstance(econ, QuoteEconomics) or not isinstance(components, dict):
        details["reason"] = "quote_economics_unavailable"
        return quote_data, details
    if components.get("cost_model_available", Decimal("0")) <= 0:
        details["reason"] = "markout_unavailable"
        return quote_data, details

    probability = max(Decimal("0.01"), min(Decimal("0.99"), calibrated_win_probability))
    markout_calibration = (markout_calibrations or {}).get(bucket)
    if not isinstance(markout_calibration, dict):
        markout_calibration = (markout_calibrations or {}).get("global")
    if isinstance(markout_calibration, dict):
        try:
            markout_ps = Decimal(str(markout_calibration["adverse_markout_per_share"]))
            exec_penalty = econ.shares * max(Decimal("0"), markout_ps)
        except Exception:
            markout_calibration = None
    resolution_ev = econ.shares * (probability - price) - econ.fee_equivalent_usdc
    robust_net = resolution_ev - exec_penalty
    penalty_ps = exec_penalty / econ.shares if econ.shares > 0 else Decimal("0")
    directional_edge_ps = probability - price - fee_ps - penalty_ps
    updated_econ = replace(
        econ,
        probability=probability,
        expected_spread_capture_usdc=resolution_ev + econ.fee_equivalent_usdc,
        expected_net_usdc=resolution_ev,
    )
    updated_components = dict(components)
    updated_components.update(
        {
            "regime_resolution_probability": probability,
            "regime_resolution_ev_usdc": resolution_ev,
            "regime_economics_applied": Decimal("1"),
            "empirical_markout_usdc": exec_penalty,
        }
    )
    updated = (
        price,
        updated_econ,
        robust_net >= min_expected_net_usdc,
        robust_net,
        exec_penalty,
        directional_edge_ps,
        directional_edge_ps * econ.shares,
        fair,
        fee_ps,
        penalty_ps,
        updated_components,
    )
    details.update(
        {
            "applied": True,
            "reason": "settled_score_regime",
            "signed_spot_distance": signed_distance,
            "distance_bucket": bucket,
            "calibration_sample_count": int(calibration.get("sample_count") or 0),
            "resolution_probability": probability,
            "resolution_ev_usdc": resolution_ev,
            "robust_net_usdc": robust_net,
            "markout_source": str(markout_calibration.get("source")) if markout_calibration else "engine_global",
            "markout_sample_count": int(markout_calibration.get("sample_count") or 0) if markout_calibration else 0,
            "markout_per_share": exec_penalty / econ.shares if econ.shares > 0 else Decimal("0"),
        }
    )
    return updated, details
