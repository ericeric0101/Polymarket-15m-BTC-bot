from decimal import Decimal

from bot.quoting import apply_quote_plan_guards, set_side_should_quote
from bot.enums import MarketPhase


def test_set_side_should_quote_preserves_execution_penalty_components():
    components = {"vwap_usdc": Decimal("0.24"), "non_atomic_usdc": Decimal("0.03")}
    side_plan = {
        "buy": (
            Decimal("0.50"),
            object(),
            True,
            Decimal("0.10"),
            Decimal("0.30"),
            Decimal("0.02"),
            Decimal("0.12"),
            Decimal("0.52"),
            Decimal("0.01"),
            Decimal("0.02"),
            components,
        )
    }
    reasons: dict[str, str] = {}

    set_side_should_quote(side_plan, reasons, "buy", False, "edge_gate_buy")

    assert len(side_plan["buy"]) == 11
    assert side_plan["buy"][2] is False
    assert side_plan["buy"][10] == components
    assert reasons == {"buy": "edge_gate_buy"}


def test_legacy_directional_edge_gate_is_telemetry_not_a_buy_veto():
    side_plan = {
        "buy": (
            Decimal("0.70"),
            object(),
            False,
            Decimal("-0.20"),
            Decimal("0.30"),
            Decimal("-0.05"),
            Decimal("-0.50"),
            Decimal("0.72"),
            Decimal("0"),
            Decimal("0"),
        ),
    }

    outcome = apply_quote_plan_guards(
        side_plan=side_plan,
        quote_sides_mode="both",
        phase_value=MarketPhase.ACTIVE.value,
        inventory_delta_shares=Decimal("0"),
        early_sell_only_sec=0.0,
        time_left_sec_global=600.0,
        directional_edge_gate_enabled=True,
        regime_guard_active=True,
        min_directional_edge_ps=Decimal("0.01"),
        min_directional_edge_ps_conservative=Decimal("0.02"),
        now_ts=100.0,
        buy_cooldown_until_ts=0.0,
        momentum_buy_filter_pct=Decimal("0"),
        momentum_sell_filter_pct=Decimal("0"),
        momentum_window_ticks=2,
        momentum_history=[],
        fair=Decimal("0.72"),
        min_fair_price=Decimal("0.05"),
        max_fair_price=Decimal("0.95"),
        end_ts=1000.0,
        min_minutes_to_close=3.0,
        reduce_only_no_new_sell_last_sec=45,
        forced_sell_only=False,
        active_side="UP",
        min_directional_edge_ps_down=Decimal("0.01"),
    )

    assert outcome.side_disable_reason_by_side.get("buy") is None
