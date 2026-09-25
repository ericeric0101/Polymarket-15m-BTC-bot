"""
Unconditional Loss Circuit Breaker — targeted regression tests.

Tests the exact Test 4 gap scenario and the feature flag disable case.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal
from types import SimpleNamespace
import asyncio
from bot.exit_engine import ExitPolicyEngine, ExitEngineConfig, ExitDecisionType
from bot.models import MarketSnapshot, PositionState, SignalDecision
from execution.exit_policy import ExitStage
from bot.enums import ActiveSide
from bot.taker_exit import TakerExitMixin


def _make_config(**overrides):
    defaults = dict(
        min_hold_sec=45,
        stop_loss_usdc=Decimal("0.50"),
        stop_loss_confirmations=2,
        stop_loss_requires_thesis_weakening=True,
        stop_loss_thesis_min_score_abs=Decimal("0.05"),
        stop_loss_hold_on_none_signal=True,
        conviction_band_min_price=Decimal("0.60"),
        hold_band_min_price=Decimal("0.68"),
        conviction_band_min_score_abs=Decimal("0.12"),
        hold_band_min_score_abs=Decimal("0.12"),
        hold_band_release_min_roi=Decimal("0.15"),
        conviction_stop_loss_multiplier=Decimal("1.75"),
        conviction_extra_confirmations=1,
        hold_band_requires_locked=True,
        early_profit_hold_enabled=True,
        early_profit_hold_min_hold_sec=60,
        early_profit_hold_max_profit_ps=Decimal("0.08"),
        profit_run_enabled=True,
        profit_run_min_hold_sec=20,
        profit_run_min_profit_ps=Decimal("0.06"),
        profit_run_min_score_abs=Decimal("0.12"),
        profit_run_trailing_drawdown_ps=Decimal("0.06"),
        profit_run_unlock_profit_ps=Decimal("0.18"),
        profit_run_unlock_trailing_drawdown_ps=Decimal("0.02"),
        recycle_locked_side_min_fair_edge_ps=Decimal("0.04"),
        catastrophic_stop_loss_enabled=True,
        catastrophic_stop_loss_usdc=Decimal("0.40"),
        catastrophic_stop_loss_min_score_abs=Decimal("0.50"),
        catastrophic_stop_loss_confirmations=2,
        absolute_max_loss_enabled=True,
        absolute_max_loss_usdc=Decimal("2.00"),
        absolute_max_loss_min_hold_sec=60,
    )
    defaults.update(overrides)
    return ExitEngineConfig(**defaults)


def _snapshot(best_bid, fair=None):
    best_bid = Decimal(str(best_bid))
    fair = Decimal(str(fair)) if fair is not None else best_bid + Decimal("0.01")
    best_ask = best_bid + Decimal("0.01")
    return MarketSnapshot(
        instrument_id="test",
        phase="ACTIVE",
        time_left_sec=120.0,
        best_bid=best_bid,
        best_ask=best_ask,
        fee_rate=Decimal("0.02"),
        spread=Decimal("0.01"),
        spread_pct=Decimal("0.01"),
        slippage_buffer_pct=Decimal("0.002"),
        exit_stage=ExitStage.PASSIVE,
        in_reduce_only_tail=False,
        stop_loss_disabled_in_tail=False,
        fair=fair,
        fair_edge_ps=max(Decimal("0"), fair - best_bid),
        spot_minus_strike_bps=Decimal("10"),
    )


def _position(avg_entry, hold_sec=90.0, confirm_hits=0, qty=Decimal("5.3")):
    return PositionState(
        instrument_id="test",
        qty=qty,
        sellable_qty=qty,
        avg_entry_price=Decimal(str(avg_entry)),
        entry_fee_remaining=Decimal("0.05"),
        hold_sec=hold_sec,
        stop_loss_confirm_hits=confirm_hits,
        held_side="UP",
        peak_bid=None,
        peak_fair=None,
    )


def _signal(score=Decimal("0.30"), locked=True, matches=True, active_side="UP"):
    return SignalDecision(
        active_side=active_side,
        score=score,
        locked=locked,
        reason="test",
        matches_position=matches,
    )


# =========================================================================
# TEST 1: Exact Test 4 scenario — the gap that prompted this feature.
#
# Entry=0.69, bid=0.20, confirmed opposite direction, hold_sec=90.
# Net loss is beyond the new $2.00 threshold.
#
# Before this fix: ALL 5 exit paths were blocked (thesis healthy).
# After this fix: absolute_max_loss_breaker requires trend confirmation.
# =========================================================================
def test_exact_gap_scenario_fires_breaker():
    """
    A strong, locked opposite-side signal and loss beyond $2.00 fire the breaker.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.20", fair="0.21"),
        _position(avg_entry="0.69", hold_sec=90),
        _signal(score=Decimal("-0.30"), locked=True, matches=False, active_side="DOWN"),
    )
    assert result.decision_type == ExitDecisionType.TAKER_STOP_LOSS, (
        f"FAIL: breaker did not fire! Got {result.decision_type.value}/{result.reason}"
    )
    assert result.reason == "absolute_max_loss_breaker", (
        f"FAIL: wrong reason. Expected absolute_max_loss_breaker, got {result.reason}"
    )
    assert "absolute_max_loss_usdc" in result.metadata, (
        "FAIL: metadata missing absolute_max_loss_usdc"
    )
    print(f"PASS: exact gap scenario → {result.decision_type.value}/{result.reason}")
    print(f"  net_if_exit={result.net_if_exit:.4f}")
    print(f"  metadata: absolute_max_loss_usdc={result.metadata['absolute_max_loss_usdc']}, "
          f"best_bid={result.metadata['best_bid']}")


def test_matching_locked_trend_suppresses_absolute_breaker_even_beyond_two_dollars():
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.20", fair="0.21"),
        _position(avg_entry="0.69", hold_sec=90),
        _signal(score=Decimal("0.30"), locked=True, matches=True),
    )
    assert result.reason != "absolute_max_loss_breaker"


def test_matching_locked_trend_does_not_cancel_resting_tail_tp():
    host = _BreakerExitHost()

    asyncio.run(host._maybe_taker_exit_positions(10_000.0, is_simulation=False))

    assert host.submissions == []
    assert "sell:up" in host.active_maker_orders


def test_strong_opposite_locked_trend_fires_absolute_breaker_at_two_dollars():
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.20", fair="0.21"),
        _position(avg_entry="0.69", hold_sec=90),
        _signal(score=Decimal("-0.30"), locked=True, matches=False, active_side="DOWN"),
    )
    assert result.reason == "absolute_max_loss_breaker"
    assert result.metadata["absolute_max_loss_usdc"] == "2.00"


def test_hold_to_redeem_keeps_position_when_trend_still_matches():
    engine = ExitPolicyEngine(_make_config(hold_to_redeem_enabled=True))
    result = engine.evaluate(
        _snapshot(best_bid="0.20", fair="0.21"),
        _position(avg_entry="0.69", hold_sec=90),
        _signal(score=Decimal("0.30"), locked=True, matches=True),
    )
    assert result.decision_type == ExitDecisionType.HOLD_TO_REDEEM


def test_unlocked_or_none_signal_does_not_confirm_adverse_trend():
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.20", fair="0.21"),
        _position(avg_entry="0.69", hold_sec=90),
        _signal(score=Decimal("0"), locked=False, matches=False, active_side="NONE"),
    )
    assert result.reason != "absolute_max_loss_breaker"


class _BreakerExitHost(TakerExitMixin):
    def __init__(self):
        now = 10_000.0
        self.taker_exit_enabled = True
        self.hold_to_redeem_enabled = False
        self.taker_exit_only_after_invalidation = False
        self.taker_exit_cooldown_sec = 0
        self.taker_exit_eval_interval_sec = 0
        self.taker_exit_last_eval_ts_by_inst = {}
        self.taker_exit_reject_cooldown_until_by_inst = {}
        self.taker_exit_tail_attempted_by_inst = {}
        self.pending_taker_exit_by_inst = {}
        self.last_taker_exit_ts_by_inst = {}
        self.taker_exit_stop_loss_hits_by_inst = {}
        self._stop_loss_execution_priority_by_inst = {}
        self.current_market_end_timestamp = now + 300
        self.current_market_slug = "breaker-test"
        self.market_strike_cache_by_slug = {}
        self.maker_reduce_only_no_new_sell_last_sec = 0
        self.taker_exit_disable_stop_loss_last_sec = 0
        self.endgame_twap_exit_enabled = False
        self.live_inventory_cost = {"up": {"qty": Decimal("5.3"), "avg_entry_price": Decimal("0.69"), "opened_ts": now - 90}}
        self.active_maker_orders = {"sell:up": {"created_ts": now - 1, "pending_cancel": False}}
        self.maker_exchange_min_shares = Decimal("5")
        self.taker_exit_slippage_buffer_pct = Decimal("0.002")
        self.taker_exit_stop_loss_max_spread_pct = Decimal("0.03")
        self.taker_exit_stop_loss_confirmations = 2
        self.taker_exit_stop_loss_usdc = Decimal("0.50")
        self.taker_exit_wait_for_sell_quote_sec = 20
        self.taker_exit_max_hold_near_close_sec = 0
        self.maker_high_cost_exit_cooldown_enabled = False
        self.high_cost_exit_cooldown_until_by_inst = {}
        self.market_phase = "ACTIVE"
        self.active_side = ActiveSide.UP
        self.side_decision_score = Decimal("0.30")
        self.active_side_locked = True
        self.side_decision_reason = "healthy"
        self.maker_profit_run_peak_bid_by_inst = {}
        self.maker_profit_run_peak_fair_by_inst = {}
        self.exit_policy = SimpleNamespace(stage=lambda _time_left: ExitStage.PASSIVE)
        self.exit_policy_engine = ExitPolicyEngine(_make_config())
        self.submissions = []

    def _maker_quote_instruments(self): return ["up"]
    def _instrument_key(self, inst): return str(inst)
    def _order_key_for(self, side, inst): return f"{side}:{inst}"
    def _normalize_instrument_id(self, inst): return inst
    def _get_quote_for_instrument(self, _inst): return Decimal("0.20"), Decimal("0.25")
    def _side_for_instrument_id(self, _inst): return ActiveSide.UP
    def _instrument_for_side(self, side): return "up" if side == ActiveSide.UP else None
    def _extract_token_id_from_instrument(self, _inst): return None
    async def _get_dynamic_fee_rate(self, **_kwargs): return Decimal("0")
    def _infer_market_fee_rate_default(self): return Decimal("0")
    def _is_emergency_exit_window(self, _time_left): return False
    def _get_effective_sellable_qty(self, **_kwargs): return Decimal("5.3")
    def _submit_taker_exit_order(self, **kwargs): self.submissions.append(kwargs); return True


def test_absolute_breaker_bypasses_wide_spread_and_fresh_existing_sell():
    host = _BreakerExitHost()
    host.active_side = ActiveSide.DOWN
    host.side_decision_score = Decimal("-0.30")

    asyncio.run(host._maybe_taker_exit_positions(10_000.0, is_simulation=False))

    assert len(host.submissions) == 1
    assert host.submissions[0]["reason"] == "stop_loss"
    assert host.submissions[0]["decision_payload"]["decision_reason"] == "absolute_max_loss_breaker"
    assert host.submissions[0]["decision_payload"]["stop_loss_threshold"] == "2.00"
    assert host.submissions[0]["decision_payload"]["required_confirmations"] == 0


# =========================================================================
# TEST 2: Feature flag disabled — same scenario, must NOT fire.
# =========================================================================
def test_feature_flag_disabled_does_not_fire():
    """
    Same catastrophic scenario but absolute_max_loss_enabled=False.
    The breaker must NOT fire. The position proceeds to normal evaluation.
    """
    engine = ExitPolicyEngine(_make_config(absolute_max_loss_enabled=False))
    result = engine.evaluate(
        _snapshot(best_bid="0.20", fair="0.21"),
        _position(avg_entry="0.69", hold_sec=90),
        _signal(score=Decimal("-0.30"), locked=True, matches=False, active_side="DOWN"),
    )
    assert result.reason != "absolute_max_loss_breaker", (
        f"FAIL: breaker fired despite being disabled! reason={result.reason}"
    )
    print(f"PASS: feature disabled → {result.decision_type.value}/{result.reason} (no breaker)")


# =========================================================================
# TEST 3: Loss below threshold — must NOT fire.
# Entry=0.69, bid=0.55 → net ≈ -$0.82 (below $2.00 threshold).
# =========================================================================
def test_loss_below_threshold_does_not_fire():
    """
    Moderate loss (-$0.82) is below the $2.00 absolute threshold.
    Normal exit logic should handle this, not the circuit breaker.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.55", fair="0.56"),
        _position(avg_entry="0.69", hold_sec=90),
        _signal(score=Decimal("-0.30"), locked=True, matches=False, active_side="DOWN"),
    )
    assert result.reason != "absolute_max_loss_breaker", (
        f"FAIL: breaker fired on moderate loss! reason={result.reason}"
    )
    print(f"PASS: moderate loss ($0.82) → {result.decision_type.value}/{result.reason} (no breaker)")


# =========================================================================
# TEST 4: Hold too short — must NOT fire.
# Entry=0.69, bid=0.20, hold_sec=30 (below 60s min hold).
# =========================================================================
def test_hold_too_short_does_not_fire():
    """
    Massive loss but position held only 30s.
    Fresh positions should not be cut — may be volatile entry.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.20", fair="0.21"),
        _position(avg_entry="0.69", hold_sec=30),
        _signal(score=Decimal("-0.30"), locked=True, matches=False, active_side="DOWN"),
    )
    assert result.reason != "absolute_max_loss_breaker", (
        f"FAIL: breaker fired on fresh position! reason={result.reason}"
    )
    print(f"PASS: short hold → {result.decision_type.value}/{result.reason} (no breaker)")


# =========================================================================
# TEST 5: Confirm adverse trend lets breaker bypass HOLD_IN_BAND.
# =========================================================================
def test_breaker_bypasses_hold_in_band():
    """
    Strong opposite-side confirmation and loss > $2.00 should bypass HOLD_IN_BAND.

    Entry=0.95, bid=0.68, qty=10; the net loss exceeds $2.00.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.68", fair="0.72"),
        _position(avg_entry="0.95", hold_sec=120, qty=Decimal("10")),
        _signal(score=Decimal("-0.30"), locked=True, matches=False, active_side="DOWN"),
    )
    assert result.decision_type == ExitDecisionType.TAKER_STOP_LOSS, (
        f"FAIL: breaker didn't bypass hold band! Got {result.decision_type.value}/{result.reason}"
    )
    assert result.reason == "absolute_max_loss_breaker", (
        f"FAIL: wrong reason. Expected absolute_max_loss_breaker, got {result.reason}"
    )
    print(f"PASS: breaker bypasses HOLD_IN_BAND → {result.decision_type.value}/{result.reason}")
    print(f"  net_if_exit={result.net_if_exit:.4f}")


# =========================================================================
# TEST 6: Profitable position — must NEVER fire.
# =========================================================================
def test_profitable_position_never_fires():
    """
    Profitable position: entry=0.50, bid=0.80.
    Breaker requires price_adverse (bid < entry), so it must not fire.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.80", fair="0.81"),
        _position(avg_entry="0.50", hold_sec=120),
        _signal(score=Decimal("0.30"), locked=True, matches=True),
    )
    assert result.reason != "absolute_max_loss_breaker", (
        f"FAIL: breaker fired on profitable position! reason={result.reason}"
    )
    print(f"PASS: profitable position → {result.decision_type.value}/{result.reason}")


# =========================================================================
# TEST 7: Strong opposite-side confirmation can fire immediately.
# =========================================================================
def test_breaker_fires_immediately_no_confirmations():
    """
    A confirmed opposite trend can still fire without extra confirmation cycles.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.30", fair="0.31"),
        _position(avg_entry="0.69", hold_sec=120, confirm_hits=0),
        _signal(score=Decimal("-0.30"), locked=True, matches=False, active_side="DOWN"),
    )
    assert result.decision_type == ExitDecisionType.TAKER_STOP_LOSS, (
        f"FAIL: expected immediate TAKER_STOP_LOSS, got {result.decision_type.value}"
    )
    assert result.reason == "absolute_max_loss_breaker"
    assert result.confirm_hits == 0, (
        f"FAIL: confirm_hits should be 0 (immediate), got {result.confirm_hits}"
    )
    print(f"PASS: breaker fires immediately (confirm_hits=0) → {result.decision_type.value}")


if __name__ == "__main__":
    test_exact_gap_scenario_fires_breaker()
    test_feature_flag_disabled_does_not_fire()
    test_loss_below_threshold_does_not_fire()
    test_hold_too_short_does_not_fire()
    test_breaker_bypasses_hold_in_band()
    test_profitable_position_never_fires()
    test_matching_locked_trend_suppresses_absolute_breaker_even_beyond_two_dollars()
    test_matching_locked_trend_does_not_cancel_resting_tail_tp()
    test_strong_opposite_locked_trend_fires_absolute_breaker_at_two_dollars()
    test_hold_to_redeem_keeps_position_when_trend_still_matches()
    test_unlocked_or_none_signal_does_not_confirm_adverse_trend()
    test_breaker_fires_immediately_no_confirmations()
    print("\n✅ All circuit breaker tests passed.")
