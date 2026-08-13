from decimal import Decimal

from types import SimpleNamespace

from bot.quote_service import (
    apply_entry_vwap_risk_weight,
    parse_quote_plan,
    scale_buy_economics_to_order_size,
    synchronize_desired_buy_economics_to_quantity,
)


def test_parse_quote_plan_names_tuple_fields_and_units():
    plan = parse_quote_plan(("0.42", "5", True, "0.10", "0.01", "0.02", "0.10", "0.44", "0.001", "0.002"))

    assert plan.price == Decimal("0.42")
    assert plan.quantity == Decimal("5")
    assert plan.fee_per_share == Decimal("0.001")
    assert plan.other_cost_per_share == Decimal("0.002")


def test_parse_quote_plan_rejects_short_tuple():
    assert parse_quote_plan(("0.42",)) is None


def test_entry_vwap_risk_weight_only_discounts_for_confirmed_flat_entry():
    robust, penalty, components, applied = apply_entry_vwap_risk_weight(
        expected_net=Decimal("0.10"),
        raw_robust_net=Decimal("-0.25"),
        raw_execution_penalty=Decimal("0.30"),
        execution_penalty_components={
            "slippage_usdc": Decimal("0.02"),
            "vwap_usdc": Decimal("0.24"),
            "non_atomic_usdc": Decimal("0.04"),
        },
        entry_is_flat=True,
        entry_signal_confirmed=True,
        time_left_sec=600.0,
        vwap_entry_risk_weight=Decimal("0.25"),
        vwap_full_risk_last_sec=180.0,
    )

    assert applied is True
    assert penalty == Decimal("0.12")
    assert robust == Decimal("-0.07")
    assert components["vwap_raw_usdc"] == Decimal("0.24")
    assert components["vwap_effective_usdc"] == Decimal("0.0600")


def test_entry_vwap_risk_weight_keeps_full_penalty_near_settlement_or_with_inventory():
    kwargs = {
        "expected_net": Decimal("0.10"),
        "raw_robust_net": Decimal("-0.25"),
        "raw_execution_penalty": Decimal("0.30"),
        "execution_penalty_components": {"vwap_usdc": Decimal("0.24")},
        "entry_signal_confirmed": True,
        "vwap_entry_risk_weight": Decimal("0.25"),
        "vwap_full_risk_last_sec": 180.0,
    }

    near_close = apply_entry_vwap_risk_weight(
        **kwargs,
        entry_is_flat=True,
        time_left_sec=180.0,
    )
    held_inventory = apply_entry_vwap_risk_weight(
        **kwargs,
        entry_is_flat=False,
        time_left_sec=600.0,
    )

    assert near_close[0] == Decimal("-0.25")
    assert near_close[1] == Decimal("0.30")
    assert near_close[3] is False
    assert held_inventory[0] == Decimal("-0.25")
    assert held_inventory[1] == Decimal("0.30")
    assert held_inventory[3] is False


def test_hold_to_redeem_flat_entry_excludes_forced_exit_vwap_but_keeps_other_costs():
    robust, penalty, components, applied = apply_entry_vwap_risk_weight(
        expected_net=Decimal("0.10"),
        raw_robust_net=Decimal("-0.25"),
        raw_execution_penalty=Decimal("0.30"),
        execution_penalty_components={
            "slippage_usdc": Decimal("0.02"),
            "vwap_usdc": Decimal("0.24"),
            "non_atomic_usdc": Decimal("0.04"),
        },
        entry_is_flat=True,
        entry_signal_confirmed=True,
        time_left_sec=600.0,
        vwap_entry_risk_weight=Decimal("0.25"),
        vwap_full_risk_last_sec=180.0,
        hold_to_redeem_enabled=True,
    )

    assert applied is True
    assert penalty == Decimal("0.06")
    assert robust == Decimal("-0.01")
    assert components["vwap_raw_usdc"] == Decimal("0.24")
    assert components["vwap_effective_usdc"] == Decimal("0")
    assert components["non_atomic_usdc"] == Decimal("0.04")


def test_post_only_hold_entry_excludes_hypothetical_exit_and_taker_costs():
    robust, penalty, components, applied = apply_entry_vwap_risk_weight(
        expected_net=Decimal("0.10"),
        raw_robust_net=Decimal("-0.25"),
        raw_execution_penalty=Decimal("0.30"),
        execution_penalty_components={
            "slippage_usdc": Decimal("0.02"),
            "vwap_usdc": Decimal("0.24"),
            "non_atomic_usdc": Decimal("0.04"),
        },
        entry_is_flat=True,
        entry_signal_confirmed=True,
        time_left_sec=600.0,
        vwap_entry_risk_weight=Decimal("0.25"),
        vwap_full_risk_last_sec=180.0,
        hold_to_redeem_enabled=True,
        post_only_enabled=True,
    )

    assert applied is True
    assert penalty == Decimal("0")
    assert robust == Decimal("0.10")
    assert components["passive_hold_entry_cost_policy"] == Decimal("1")
    assert components["vwap_raw_usdc"] == Decimal("0.24")
    assert components["non_atomic_effective_usdc"] == Decimal("0")
    assert components["taker_leakage_effective_usdc"] == Decimal("0")


def test_size_down_scales_buy_economics_and_usdc_penalties_before_gating():
    econ = SimpleNamespace(
        shares=Decimal("10"),
        fee_equivalent_usdc=Decimal("0.01"),
        expected_rebate_usdc=Decimal("0"),
        expected_spread_capture_usdc=Decimal("0.05"),
        expected_net_usdc=Decimal("0.04"),
    )
    scaled, robust, penalty, edge, components = scale_buy_economics_to_order_size(
        econ=econ,
        robust_net=Decimal("-0.40"),
        exec_penalty=Decimal("0.45"),
        directional_edge_usdc=Decimal("-0.41"),
        execution_penalty_components={
            "vwap_usdc": Decimal("0.36"),
            "non_atomic_usdc": Decimal("0.08"),
            "recent_vol": Decimal("0.18"),
        },
        size_multiplier=Decimal("0.5"),
    )

    assert scaled.shares == Decimal("5")
    assert scaled.expected_net_usdc == Decimal("0.02")
    assert robust == Decimal("-0.20")
    assert penalty == Decimal("0.225")
    assert edge == Decimal("-0.205")
    assert components["vwap_usdc"] == Decimal("0.18")
    assert components["non_atomic_usdc"] == Decimal("0.04")
    assert components["recent_vol"] == Decimal("0.18")


def test_final_quantity_sync_scales_all_usdc_economics_after_late_size_caps():
    desired = {
        "econ": SimpleNamespace(
            shares=Decimal("10"),
            fee_equivalent_usdc=Decimal("0.01"),
            expected_rebate_usdc=Decimal("0"),
            expected_spread_capture_usdc=Decimal("0.05"),
            expected_net_usdc=Decimal("0.04"),
        ),
        "robust_net": Decimal("0.04"),
        "exec_penalty": Decimal("0.00"),
        "directional_edge_usdc": Decimal("0.06"),
        "execution_penalty_components": {"vwap_usdc": Decimal("0.20")},
    }

    synced = synchronize_desired_buy_economics_to_quantity(
        desired_entry=desired,
        requested_quantity=Decimal("5"),
    )

    assert synced["planned_quantity"] == Decimal("5")
    assert synced["econ"].shares == Decimal("5")
    assert synced["econ"].expected_net_usdc == Decimal("0.020")
    assert synced["robust_net"] == Decimal("0.020")
    assert synced["directional_edge_usdc"] == Decimal("0.030")
    assert synced["execution_penalty_components"]["vwap_usdc"] == Decimal("0.100")


def test_size_scaling_preserves_an_explicit_larger_trend_quantity():
    econ = SimpleNamespace(
        shares=Decimal("5.4"),
        fee_equivalent_usdc=Decimal("0.01"),
        expected_rebate_usdc=Decimal("0"),
        expected_spread_capture_usdc=Decimal("0.05"),
        expected_net_usdc=Decimal("0.04"),
    )

    scaled, robust, penalty, edge, _ = scale_buy_economics_to_order_size(
        econ=econ,
        robust_net=Decimal("0.04"),
        exec_penalty=Decimal("0.01"),
        directional_edge_usdc=Decimal("0.05"),
        execution_penalty_components={},
        size_multiplier=Decimal("1.5"),
    )

    assert scaled.shares == Decimal("8.10")
    assert scaled.expected_net_usdc == Decimal("0.060")
    assert robust == Decimal("0.060")
    assert penalty == Decimal("0.015")
    assert edge == Decimal("0.075")
