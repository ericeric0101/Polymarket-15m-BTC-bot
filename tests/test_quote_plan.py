from decimal import Decimal

from types import SimpleNamespace

from bot.quote_service import (
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
