from decimal import Decimal

from bot.edge_state import build_edge_state


def test_edge_state_compares_model_probability_to_market_mid_and_prices():
    state = build_edge_state(
        model_probability_up=Decimal("0.64"),
        market_mid=Decimal("0.58"),
        up_bid=Decimal("0.57"),
        up_ask=Decimal("0.60"),
        down_bid=Decimal("0.39"),
        down_ask=Decimal("0.30"),
        total_cost_buffer=Decimal("0.01"),
    )

    assert state.model_probability_up == Decimal("0.64")
    assert state.market_probability_up == Decimal("0.58")
    assert state.up_edge_vs_mid == Decimal("0.06")
    assert state.up_edge_vs_ask == Decimal("0.04")
    assert state.down_edge_vs_ask == Decimal("0.06")
    assert state.up_net_edge_vs_ask == Decimal("0.03")
    assert state.down_net_edge_vs_ask == Decimal("0.05")


def test_edge_state_is_shadow_safe_when_model_or_market_is_missing():
    state = build_edge_state(
        model_probability_up=None,
        market_mid=None,
        up_bid=None,
        up_ask=None,
        down_bid=None,
        down_ask=None,
        total_cost_buffer=Decimal("0.01"),
    )

    assert state.up_edge_vs_mid is None
    assert state.up_edge_vs_ask is None
    assert state.down_edge_vs_ask is None
    assert state.to_dict()["edge_available"] is False
