from decimal import Decimal
from types import SimpleNamespace

from execution.maker_engine import MakerEngine


def test_empirical_markout_caps_full_book_vwap_stress_for_maker_entry():
    engine = MakerEngine.__new__(MakerEngine)
    engine.config = SimpleNamespace(
        maker_execution_penalty_enable=True,
        maker_execution_depth_impact_mult=Decimal("0"),
        maker_execution_slippage_spread_mult=Decimal("0"),
        maker_execution_vwap_mult=Decimal("1"),
        maker_execution_non_atomic_vol_mult=Decimal("0"),
        maker_execution_penalty_floor_usdc=Decimal("0"),
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

    assert components["book_vwap_usdc"].quantize(Decimal("0.0001")) == Decimal("0.4000")
    assert components["empirical_markout_usdc"] == Decimal("0.12")
    assert components["vwap_usdc"] == Decimal("0")
    assert components["total_usdc"] == Decimal("0.12")
    # The convergence telemetry exposes the old forced-exit proxy alongside
    # the empirical model without changing the production total.
    assert components["legacy_proxy_vwap_usdc"].quantize(Decimal("0.0001")) == Decimal("0.4000")
    assert components["legacy_proxy_penalty_usdc"].quantize(Decimal("0.0001")) == Decimal("0.4000")
    assert components["single_empirical_penalty_usdc"] == Decimal("0.12")
    assert components["single_empirical_available"] == Decimal("1")
