from decimal import Decimal

from bot.strong_directional_regime import apply_strong_directional_regime_economics
from execution.rebate_model import QuoteEconomics


def _quote_data():
    econ = QuoteEconomics(
        shares=Decimal("10"),
        probability=Decimal("0.69"),
        fee_equivalent_usdc=Decimal("0"),
        expected_rebate_usdc=Decimal("0"),
        expected_spread_capture_usdc=Decimal("0.05"),
        expected_net_usdc=Decimal("0.05"),
    )
    return (
        Decimal("0.69"), econ, False, Decimal("-0.48"), Decimal("0.53"),
        Decimal("-0.048"), Decimal("-0.48"), Decimal("0.695"), Decimal("0"),
        Decimal("0.053"), {"cost_model_available": Decimal("1")},
    )


def test_strong_directional_regime_keeps_markout_but_uses_settled_probability():
    updated, details = apply_strong_directional_regime_economics(
        _quote_data(),
        active_side="UP",
        outcome_side="UP",
        side_locked=True,
        side_score=Decimal("0.40"),
        time_left_sec=480,
        spot=Decimal("10020"),
        strike=Decimal("10000"),
        calibrations={"10_30": {"win_probability": 0.78, "sample_count": 100}},
        min_expected_net_usdc=Decimal("0.001"),
    )

    assert details["applied"] is True
    assert updated[2] is True
    assert updated[4] == Decimal("0.53")  # Markout is still fully deducted.
    assert updated[3] == Decimal("0.37")  # 10 * (0.78 - 0.69) - 0.53
    assert updated[10]["regime_resolution_probability"] == Decimal("0.78")


def test_strong_directional_regime_refuses_unmeasured_time_window():
    updated, details = apply_strong_directional_regime_economics(
        _quote_data(),
        active_side="UP",
        outcome_side="UP",
        side_locked=True,
        side_score=Decimal("0.40"),
        time_left_sec=700,
        spot=Decimal("10020"),
        strike=Decimal("10000"),
        calibrations={"10_30": {"win_probability": 0.78, "sample_count": 100}},
        min_expected_net_usdc=Decimal("0.001"),
    )

    assert details["applied"] is False
    assert details["reason"] == "time_outside_measured_regime"
    assert updated == _quote_data()
