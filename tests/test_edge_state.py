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


def test_edge_state_separates_diagnostic_and_executable_availability():
    state = build_edge_state(
        model_probability_up=Decimal("0.60"),
        market_mid=Decimal("0.55"),
        up_bid=None,
        up_ask=None,
        down_bid=None,
        down_ask=None,
        total_cost_buffer=Decimal("0.01"),
    )

    assert state.diagnostic_edge_available is True
    assert state.executable_edge_available is False
    assert state.to_dict()["diagnostic_edge_available"] is True
    assert state.to_dict()["executable_edge_available"] is False


def test_edge_state_accepts_probability_zero_and_tracks_cost_breakdown_and_quote_age():
    state = build_edge_state(
        model_probability_up=Decimal("0"),
        market_mid=Decimal("0.01"),
        up_bid=Decimal("0.01"),
        up_ask=Decimal("0.02"),
        down_bid=Decimal("0.98"),
        down_ask=Decimal("0.99"),
        fee_buffer=Decimal("0.001"),
        slippage_buffer=Decimal("0.002"),
        adverse_selection_buffer=Decimal("0.003"),
        model_error_buffer=Decimal("0.004"),
        quote_age_sec=Decimal("0.8"),
        max_quote_age_sec=Decimal("2"),
    )

    assert state.model_probability_up == Decimal("0")
    assert state.total_cost_buffer == Decimal("0.010")
    assert state.executable_edge_available is True
    assert state.fresh_executable_edge_available is True
    assert state.to_dict()["quote_age_sec"] == 0.8


def test_edge_state_rejects_invalid_binary_prices():
    state = build_edge_state(
        model_probability_up=Decimal("0.6"),
        market_mid=Decimal("0.5"),
        up_bid=Decimal("1.2"),
        up_ask=Decimal("-0.1"),
        down_bid=None,
        down_ask=None,
    )

    assert state.up_bid is None
    assert state.up_ask is None


def test_edge_state_rejects_negative_quote_age_as_not_fresh():
    state = build_edge_state(
        model_probability_up=Decimal("0.6"),
        market_mid=Decimal("0.5"),
        up_bid=Decimal("0.49"),
        up_ask=Decimal("0.51"),
        down_bid=None,
        down_ask=None,
        quote_age_sec=Decimal("-1"),
        max_quote_age_sec=Decimal("2"),
    )

    assert state.quote_age_sec is None
    assert state.fresh_executable_edge_available is False


def test_edge_state_exposes_side_specific_freshness():
    state = build_edge_state(
        model_probability_up=Decimal("0.6"),
        market_mid=Decimal("0.5"),
        up_bid=Decimal("0.49"),
        up_ask=Decimal("0.51"),
        down_bid=Decimal("0.40"),
        down_ask=Decimal("0.42"),
        up_quote_age_sec=Decimal("0.5"),
        down_quote_age_sec=Decimal("3"),
        max_quote_age_sec=Decimal("2"),
    )

    assert state.up_executable_edge_available is True
    assert state.down_executable_edge_available is True
    assert state.up_fresh_executable_edge_available is True
    assert state.down_fresh_executable_edge_available is False
    assert state.to_dict()["observed_quote_age_sec"] is None


def test_edge_state_exact_stale_boundary_is_still_fresh():
    state = build_edge_state(
        model_probability_up=Decimal("0.6"),
        market_mid=Decimal("0.5"),
        up_bid=Decimal("0.49"),
        up_ask=Decimal("0.51"),
        down_bid=None,
        down_ask=None,
        quote_age_sec=Decimal("2"),
        max_quote_age_sec=Decimal("2"),
    )

    assert state.fresh_executable_edge_available is True


def test_edge_state_keeps_down_book_as_down_executable_quote():
    state = build_edge_state(
        model_probability_up=Decimal("0.6"),
        market_mid=Decimal("0.55"),
        up_bid=None,
        up_ask=None,
        down_bid=Decimal("0.39"),
        down_ask=Decimal("0.40"),
        down_quote_age_sec=Decimal("0.2"),
        max_quote_age_sec=Decimal("2"),
    )

    assert state.up_executable_edge_available is False
    assert state.down_executable_edge_available is True
    assert state.down_fresh_executable_edge_available is True
    assert state.down_ask == Decimal("0.40")
