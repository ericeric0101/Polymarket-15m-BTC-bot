from decimal import Decimal
from pathlib import Path

import pytest
from dotenv import dotenv_values

from bot.app_config import AppConfig
from bot.entry_sizing import (
    CANONICAL_ENTRY_SIZING_RULE,
    HIGH_PRICE_TARGET_SHARES,
    HIGH_PRICE_THRESHOLD,
    LOW_PRICE_TARGET_SHARES,
    MIN_ENTRY_SHARES,
    SKIP_BELOW_MIN_ENTRY_SHARES,
    log_entry_sizing_skip_once,
    round_down_to_venue_size,
    size_entry,
    target_entry_shares,
)
from bot.launcher import enforce_entry_sizing_startup_policy
from bot.quote_service import (
    apply_high_entry_price_size_adjustment,
    apply_sellable_inventory_guard,
    apply_share_entry_sizing,
)

SIZING_KEYS = ("MARKET_TARGET_SHARES", "HIGH_PRICE_TARGET_SHARES", "HIGH_PRICE_THRESHOLD", "MARKET_MAX_POSITION_SHARES")


@pytest.mark.parametrize("price", ["0.30", "0.50", "0.69", "0.70"])
def test_price_at_or_below_boundary_targets_10_shares(price):
    assert target_entry_shares(Decimal(price)) == Decimal("10.0")


def test_exactly_070_is_10_shares_about_7_usdc():
    decision = size_entry(entry_price="0.70", cap_quantity=Decimal("100"))
    assert decision.final_quantity == Decimal("10.00")
    assert decision.expected_notional_usdc == Decimal("7.0000")


@pytest.mark.parametrize("price", ["0.71", "0.85", "0.99"])
def test_price_above_boundary_targets_5_5_shares(price):
    assert size_entry(entry_price=price, cap_quantity=Decimal("100")).final_quantity == Decimal("5.50")


@pytest.mark.parametrize("price", [0.7000000000000001, 0.6999999999999999, "0.70", 0.1 + 0.6])
def test_float_boundary_inputs_land_in_low_bucket(price):
    assert target_entry_shares(price) == Decimal("10.0")


def test_requote_across_boundary_recomputes_without_stale_target():
    desired = {"should_quote": True, "price": Decimal("0.69")}
    seen = []
    for price in ("0.69", "0.71", "0.69"):
        desired["price"] = Decimal(price)
        apply_high_entry_price_size_adjustment(desired_entry=desired, side="buy")
        seen.append((desired["base_target_shares"], "high_entry_price_size_adjustment" in desired))
    assert seen == [(Decimal("10.0"), False), (Decimal("5.5"), True), (Decimal("10.0"), False)]


def test_cap_below_min_entry_skips_side_effect_free_and_logs_once():
    strategy_state = {"market_buy_count_by_slug": {}, "buy_cooldown_until_ts": 0.0, "traded": set()}
    logged, lines = set(), []
    for _ in range(3):
        desired = {"should_quote": True, "price": Decimal("0.60"), "size_multiplier": Decimal("1")}
        desired, decision = apply_share_entry_sizing(desired_entry=desired, side="buy", cap_quantity=Decimal("5.49"))
        log_entry_sizing_skip_once(logged, market="m1", decision=decision, limiting_factor="l2_depth",
                                   log_fn=lines.append)
    assert decision.skip_reason == SKIP_BELOW_MIN_ENTRY_SHARES
    assert desired["should_quote"] is False and desired["target_qty_override"] is None
    assert strategy_state == {"market_buy_count_by_slug": {}, "buy_cooldown_until_ts": 0.0, "traded": set()}
    assert len(lines) == 1 and "base_target=10.0" in lines[0] and "final=5.49" in lines[0]


@pytest.mark.parametrize("multiplier", ["0.55", "0.5"])
def test_multiplier_below_min_entry_is_skipped_never_rounded_up(multiplier):
    decision = size_entry(entry_price="0.80", size_multiplier=Decimal(multiplier))
    assert decision.skipped and decision.final_quantity < MIN_ENTRY_SHARES


def test_full_5_5_position_remains_sellable_after_haircut_and_round_down():
    cfg = AppConfig.from_env(enable_terminal_dashboard=False).operations
    steady = Decimal("5.5") * (Decimal("1") - cfg.conditional_balance_safety_buffer_pct)
    after_buy = Decimal("5.5") - cfg.sellable_after_buy_buffer_shares
    for sellable in (steady, after_buy):
        assert round_down_to_venue_size(sellable) >= Decimal("5")


def test_sub_5_partial_fill_is_not_sellable_and_is_held_to_settlement():
    # Accepted residual risk: behavior unchanged, no SELL path is possible.
    qty, reason = apply_sellable_inventory_guard(Decimal("3"), 6, Decimal("3"), Decimal("5"))
    assert qty is None and reason == "sellable_below_min_after_reduce"


def test_missing_env_keys_yield_canonical_share_rule(monkeypatch):
    for key in SIZING_KEYS:
        monkeypatch.delenv(key, raising=False)
    maker = AppConfig.from_env(enable_terminal_dashboard=False).maker
    assert maker.entry_sizing_rule == CANONICAL_ENTRY_SIZING_RULE
    assert maker.entry_sizing_violations == ()
    assert maker.fixed_shares == Decimal("10.0") and maker.max_inventory_shares == Decimal("10.0")


def test_code_defaults_match_versioned_profile():
    profile = dotenv_values(Path(__file__).parents[1] / "config" / "profiles" / "btc15_twap_v3.env")
    assert Decimal(profile["MARKET_TARGET_SHARES"]) == LOW_PRICE_TARGET_SHARES
    assert Decimal(profile["HIGH_PRICE_TARGET_SHARES"]) == HIGH_PRICE_TARGET_SHARES
    assert Decimal(profile["HIGH_PRICE_THRESHOLD"]) == HIGH_PRICE_THRESHOLD
    assert Decimal(profile["MARKET_MAX_POSITION_SHARES"]) == Decimal("10.0")


@pytest.mark.parametrize("key,value", [("MARKET_TARGET_SHARES", "25"), ("HIGH_PRICE_TARGET_SHARES", "5"),
                                       ("HIGH_PRICE_THRESHOLD", "0.80"), ("MARKET_TARGET_SHARES", "")])
def test_out_of_bounds_override_refuses_live_and_is_fail_safe_in_dry_run(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    cfg = AppConfig.from_env(enable_terminal_dashboard=False)
    assert cfg.maker.entry_sizing_violations
    assert cfg.maker.entry_sizing_rule == CANONICAL_ENTRY_SIZING_RULE
    assert enforce_entry_sizing_startup_policy(cfg, live=True) is False
    assert enforce_entry_sizing_startup_policy(cfg, live=False) is True


def test_in_bounds_override_is_not_rejected(monkeypatch):
    monkeypatch.setenv("MARKET_TARGET_SHARES", "8")
    cfg = AppConfig.from_env(enable_terminal_dashboard=False)
    assert cfg.maker.entry_sizing_violations == ()
    assert enforce_entry_sizing_startup_policy(cfg, live=True) is True


# --- submit boundary: balance, parity, protective exits -------------------------

import asyncio
import sys
from types import SimpleNamespace

from bot import launcher
from bot.enums import ActiveSide
from bot.exit_engine import ExitPolicyEngine
from bot.order_submission import submit_maker_quote
from test_absolute_max_loss_breaker import _BreakerExitHost, _make_config
from test_live_path_regressions import DummyTrendSubmitStrategy

ECON = SimpleNamespace(expected_net_usdc=Decimal("0.01"), expected_rebate_usdc=Decimal("0"),
                       expected_spread_capture_usdc=Decimal("0"), fee_equivalent_usdc=Decimal("0"))


def _buy(strategy, price="0.70", qty=None):
    submit_maker_quote(strategy, instrument_id="inst-up", side="buy", limit_price=Decimal(price), econ=ECON,
                       directional_snapshot={"size_multiplier": Decimal("1")},
                       target_qty_override=Decimal(qty) if qty is not None else None)


@pytest.mark.parametrize("available,submitted", [("7.69", False), ("7.70", True)])
def test_balance_check_uses_exact_entry_notional_with_buffer(available, submitted):
    strategy = DummyTrendSubmitStrategy()
    strategy.available_usdc = Decimal(available)
    _buy(strategy)  # 10 x 0.70 x 1.1 = 7.70
    assert bool(strategy.submitted_orders) is submitted
    if submitted:
        assert strategy.submitted_orders[0].quantity.as_decimal() == Decimal("10.000000")
    else:
        event = strategy.order_events[-1]
        assert event["event_type"] == "ORDER_SKIP_INSUFFICIENT_BALANCE"
        assert event["qty"] == 10.0  # never shrunk to fit
        assert event["payload"]["required_usdc"] == pytest.approx(7.70)


def test_unknown_balance_fails_closed():
    strategy = DummyTrendSubmitStrategy()
    strategy.available_usdc = None
    _buy(strategy)
    assert not strategy.submitted_orders
    assert strategy.order_events[-1]["reason"] == "balance_unavailable"


def test_insufficient_balance_never_blocks_sell_of_existing_position():
    strategy = DummyTrendSubmitStrategy()
    strategy.available_usdc = Decimal("0")
    strategy.inventory_delta_shares = Decimal("10")
    submit_maker_quote(strategy, instrument_id="inst-up", side="sell", limit_price=Decimal("0.97"), econ=ECON,
                       directional_snapshot={"tail_protect_tp": True}, target_qty_override=Decimal("10"))
    assert strategy.submitted_orders and strategy.submitted_orders[0].quantity.as_decimal() == Decimal("10.000000")
    assert not any(e["event_type"] == "ORDER_SKIP_INSUFFICIENT_BALANCE" for e in strategy.order_events)


def test_insufficient_balance_never_blocks_protective_breaker_exit():
    host = _BreakerExitHost()
    host._cached_usdc_balance = Decimal("0")
    host._refresh_balance_cache = lambda: Decimal("0")
    host.stop_loss_enabled = False
    host.active_side = ActiveSide.DOWN
    host.side_decision_score = Decimal("-.30")
    host.exit_policy_engine = ExitPolicyEngine(_make_config(stop_loss_enabled=False))
    asyncio.run(host._maybe_taker_exit_positions(10_000, is_simulation=False))
    assert len(host.submissions) == 1
    assert host.submissions[0]["decision_payload"]["decision_reason"] == "absolute_max_loss_breaker"


class _DryRunSubmitStrategy(DummyTrendSubmitStrategy):
    def __init__(self):
        super().__init__()
        self.shadow_qty = []
        self.current_market_slug = "m1"

    def _is_dry_run_mode(self):
        return True

    def _record_shadow_simulated_entry(self, *, qty, **_kwargs):
        self.shadow_qty.append(qty)
        return True

    def _load_shadow_simulation_for_slug(self, _slug):
        return {"simulation_id": "sim-1"}


@pytest.mark.parametrize("price,override", [("0.69", "10"), ("0.71", "5.5"), ("0.80", None)])
def test_live_and_dry_run_submit_identical_quantity(price, override):
    live, dry = DummyTrendSubmitStrategy(), _DryRunSubmitStrategy()
    _buy(live, price, override)
    _buy(dry, price, override)
    assert live.submitted_orders[0].quantity.as_decimal() == dry.shadow_qty[0]
    assert not dry.submitted_orders


def test_high_price_target_above_canonical_refuses_live(monkeypatch):
    monkeypatch.setenv("HIGH_PRICE_TARGET_SHARES", "10")
    cfg = AppConfig.from_env(enable_terminal_dashboard=False)
    assert cfg.maker.entry_sizing_rule == CANONICAL_ENTRY_SIZING_RULE
    assert enforce_entry_sizing_startup_policy(cfg, live=True) is False


def test_live_main_refuses_before_preflight_on_invalid_sizing(monkeypatch):
    monkeypatch.setenv("MARKET_TARGET_SHARES", "25")
    monkeypatch.setattr(sys, "argv", ["run_bot.py", "--live"])

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("startup continued past the sizing gate")

    for name in ("apply_compatibility_patches", "run_preflight_checks", "acquire_live_process_lock",
                 "run_integrated_bot"):
        monkeypatch.setattr(launcher, name, must_not_run)
    monkeypatch.setattr("builtins.input", must_not_run)
    assert launcher.main() is None


def test_sdk_order_normalization_preserves_share_quantities():
    from py_clob_client_v2.order_builder.builder import ROUNDING_CONFIG, OrderBuilder
    from py_clob_client_v2.order_builder.constants import BUY

    for size, price in ((5.5, 0.99), (10.0, 0.70), (5.5, 0.71)):
        _side, _maker, taker = OrderBuilder.get_order_amounts(None, BUY, size, price, ROUNDING_CONFIG["0.01"])
        assert Decimal(int(taker)) / Decimal(10**6) == Decimal(str(size))
