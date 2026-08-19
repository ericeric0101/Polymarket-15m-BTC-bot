import math
from decimal import Decimal

from bot.probability_calibration import (
    calibrate_probability,
    fractional_kelly_stake_fraction,
)
from bot.quote_service import apply_fractional_kelly_sizing, evaluate_buy_entry_controls
from execution.maker_engine import MakerEngine
from scripts.calibration_shadow_report import (
    ForecastRow,
    brier_score,
    fit_brier_weight,
    probability_shadow_settlement,
)


def test_calibration_shrinks_down_probability_more_than_up_probability():
    raw = Decimal("0.80")
    mid = Decimal("0.60")
    up = calibrate_probability(
        raw_probability=raw,
        market_mid=mid,
        side="UP",
        enabled=True,
        up_model_weight=Decimal("0.65"),
        down_model_weight=Decimal("0.35"),
    )
    down = calibrate_probability(
        raw_probability=raw,
        market_mid=mid,
        side="DOWN",
        enabled=True,
        up_model_weight=Decimal("0.65"),
        down_model_weight=Decimal("0.35"),
    )
    assert up == Decimal("0.73000")
    assert down == Decimal("0.67000")


def test_calibration_uses_market_mid_when_model_weights_are_unverified():
    for side in ("UP", "DOWN"):
        assert calibrate_probability(
            raw_probability=Decimal("0.99"),
            market_mid=Decimal("0.61"),
            side=side,
            enabled=True,
            up_model_weight=Decimal("0"),
            down_model_weight=Decimal("0"),
        ) == Decimal("0.61000")


def test_fractional_kelly_is_zero_without_positive_probability_edge():
    assert fractional_kelly_stake_fraction(
        probability=Decimal("0.60"), entry_price=Decimal("0.61"), fraction=Decimal("0.25")
    ) == 0


def test_calibration_shadow_fits_train_weight_and_keeps_holdout_observational():
    rows = [
        ForecastRow("a", 0.90, 0.50, 1, "UP", 0.60, 0.40),
        ForecastRow("b", 0.10, 0.50, 0, "DOWN", 0.40, 0.60),
    ]
    learned = fit_brier_weight(rows)
    assert learned == 1.0
    assert math.isclose(brier_score(rows, learned) or 0.0, 0.01)
    candidates, settlement_pnl = probability_shadow_settlement(rows, learned)
    assert candidates == 2
    assert math.isclose(settlement_pnl, 0.8)


def test_first_entry_time_window_blocks_only_a_new_market_entry():
    kwargs = dict(
        side="buy",
        bi_side_enabled=True,
        active_side_locked=True,
        active_side_value="UP",
        side_score=Decimal("0.30"),
        directional_entry_min_score_abs_new=Decimal("0.20"),
        directional_first_entry_min_score_abs_new=Decimal("0.20"),
        first_entry_max_time_left_sec=600,
        maker_min_expected_net_usdc=Decimal("0.01"),
        maker_reload_min_expected_net_multiplier=Decimal("1"),
        maker_reload_inventory_threshold_shares=Decimal("5"),
        current_slug="test",
        inst_id="up",
        time_left_sec=601.0,
    )
    blocked = evaluate_buy_entry_controls(current_inst_inventory_qty=Decimal("0"), market_buy_count=0, **kwargs)
    allowed = evaluate_buy_entry_controls(current_inst_inventory_qty=Decimal("1"), market_buy_count=1, **kwargs)
    assert blocked.event_type == "ORDER_SKIP_FIRST_ENTRY_TIME_WINDOW"
    assert allowed.skip is False


def test_twap_degraded_reference_blocks_new_buy_entries():
    out = evaluate_buy_entry_controls(
        side="buy",
        bi_side_enabled=True,
        active_side_locked=True,
        active_side_value="UP",
        side_score=Decimal("0.30"),
        directional_entry_min_score_abs_new=Decimal("0.20"),
        directional_first_entry_min_score_abs_new=Decimal("0.20"),
        maker_min_expected_net_usdc=Decimal("0.01"),
        maker_reload_min_expected_net_multiplier=Decimal("1"),
        current_inst_inventory_qty=Decimal("0"),
        maker_reload_inventory_threshold_shares=Decimal("5"),
        current_slug="test",
        inst_id="up",
        twap_reference_degraded=True,
    )

    assert out.skip is True
    assert out.event_type == "ORDER_SKIP_TWAP_REFERENCE_DEGRADED"
    assert out.reason == "twap_reference_degraded"


def test_twap_probability_uses_shorter_variance_horizon_before_final_window():
    terminal = MakerEngine.digital_up_probability(101, 100, 0.50, 600)
    twap = MakerEngine.twap_settlement_up_probability(101, 100, 0.50, 600, 60)
    assert twap > terminal


def test_twap_probability_uses_observed_window_average_near_settlement():
    probability = MakerEngine.twap_settlement_up_probability(
        100, 100, 0.50, 20, 60, observed_window_avg=101, observed_window_sec=40
    )
    assert probability > Decimal("0.5")


def test_fractional_kelly_sizing_preserves_an_existing_high_price_reduction():
    entry = apply_fractional_kelly_sizing(
        desired_entry={
            "should_quote": True,
            "price": Decimal("0.70"),
            "p_fair": Decimal("0.80"),
            "size_multiplier": Decimal("0.5"),
            "target_qty_override": Decimal("6"),
        },
        side="buy",
        enabled=True,
        available_collateral_usdc=Decimal("100"),
        fraction=Decimal("0.25"),
        max_collateral_fraction=Decimal("0.10"),
        base_quantity=Decimal("6"),
    )
    assert entry["target_qty_override"] <= Decimal("3")
    assert entry["size_multiplier"] == 1
