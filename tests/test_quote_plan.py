from decimal import Decimal

from bot.quote_service import apply_entry_vwap_risk_weight, parse_quote_plan


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
