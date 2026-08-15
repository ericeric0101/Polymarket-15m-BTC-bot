from decimal import Decimal
from types import SimpleNamespace

from execution.maker_engine import MakerEngine


def test_empirical_markout_is_the_only_maker_entry_execution_cost():
    engine = MakerEngine.__new__(MakerEngine)
    engine.config = SimpleNamespace(
        maker_execution_empirical_adverse_markout_per_share=Decimal("0.02"),
    )

    components = engine._execution_penalty_components(
        side="buy",
        quote_price=Decimal("0.50"),
        quote_shares=Decimal("6"),
        effective_quote_size=Decimal("3"),
        inst_bid=Decimal("0.50"),
        inst_ask=Decimal("0.51"),
        bid_depth=Decimal("6"),
        ask_depth=Decimal("6"),
        bid_levels=[(Decimal("0.50"), Decimal("2")), (Decimal("0.40"), Decimal("4"))],
        ask_levels=None,
        recent_vol=Decimal("0"),
    )

    assert components["empirical_markout_usdc"] == Decimal("0.12")
    assert components["total_usdc"] == Decimal("0.12")
    assert components["cost_model_available"] == Decimal("1")


def test_missing_empirical_markout_does_not_fall_back_to_book_proxies():
    engine = MakerEngine.__new__(MakerEngine)
    engine.config = SimpleNamespace(
        maker_execution_empirical_adverse_markout_per_share=None,
    )

    components = engine._execution_penalty_components(
        side="buy",
        quote_price=Decimal("0.50"),
        quote_shares=Decimal("6"),
        effective_quote_size=Decimal("3"),
        inst_bid=Decimal("0.50"),
        inst_ask=Decimal("0.51"),
        bid_depth=Decimal("1"),
        ask_depth=Decimal("1"),
        bid_levels=[(Decimal("0.50"), Decimal("1"))],
        ask_levels=None,
        recent_vol=Decimal("0.20"),
    )

    assert components["total_usdc"] == Decimal("0")
    assert components["cost_model_available"] == Decimal("0")


def test_quote_plan_generates_both_sides_without_removed_proxy_fields():
    engine = MakerEngine.__new__(MakerEngine)
    engine.config = SimpleNamespace(
        maker_half_spread=Decimal("0.01"),
        maker_quote_size_usdc=Decimal("5"),
        maker_min_shares=Decimal("5"),
        maker_fixed_shares=Decimal("10"),
        maker_max_order_usdc=Decimal("12"),
        maker_min_expected_net_usdc=Decimal("0"),
        maker_quote_sides="both",
        maker_inventory_skew_max=Decimal("0"),
        maker_max_inventory_shares=Decimal("10"),
        maker_stale_inventory_sec=60,
        maker_stale_inventory_multiplier=Decimal("1"),
        maker_vol_stressed_threshold=Decimal("1"),
        maker_vol_extreme_threshold=Decimal("2"),
        maker_vol_stressed_spread_mult=Decimal("1"),
        maker_vol_stressed_size_mult=Decimal("1"),
        maker_vol_extreme_spread_mult=Decimal("1"),
        maker_pennying_enabled=False,
        maker_pennying_min_edge=Decimal("0"),
        maker_execution_empirical_adverse_markout_per_share=Decimal("0.02"),
    )

    plan = engine.generate_quote_plan(
        inst_bid=Decimal("0.49"),
        inst_ask=Decimal("0.51"),
        fair_price=Decimal("0.50"),
        fee_rate=Decimal("0"),
        inventory_delta_shares=Decimal("0"),
        inventory_last_update_ts=0,
        current_time_ts=0,
        tick_size=Decimal("0.01"),
    )

    assert set(plan) == {"buy", "sell"}
    assert plan["buy"][9] == Decimal("0.02")
    assert plan["sell"][9] == Decimal("0")
