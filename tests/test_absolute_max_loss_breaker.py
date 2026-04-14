"""
Unconditional Loss Circuit Breaker — targeted regression tests.

Tests the exact Test 4 gap scenario and the feature flag disable case.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal
from bot.exit_engine import ExitPolicyEngine, ExitEngineConfig, ExitDecisionType
from bot.models import MarketSnapshot, PositionState, SignalDecision
from execution.exit_policy import ExitStage


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
        absolute_max_loss_usdc=Decimal("1.50"),
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


def _signal(score=Decimal("0.30"), locked=True, matches=True):
    return SignalDecision(
        active_side="UP",
        score=score,
        locked=locked,
        reason="test",
        matches_position=matches,
    )


# =========================================================================
# TEST 1: Exact Test 4 scenario — the gap that prompted this feature.
#
# Entry=0.69, bid=0.41, signal locked+matching, hold_sec=90.
# Net loss: 5.3 * (0.41*0.998 - 0.69) - 0.05 - fee ≈ -$1.59
#
# Before this fix: ALL 5 exit paths were blocked (thesis healthy).
# After this fix: absolute_max_loss_breaker fires IMMEDIATELY.
# =========================================================================
def test_exact_gap_scenario_fires_breaker():
    """
    The REAL failure from trade 1776024900:
    Signal locked UP + matching + thesis healthy, but bid collapsed to 0.41.
    The circuit breaker must fire unconditionally.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.41", fair="0.42"),
        _position(avg_entry="0.69", hold_sec=90),
        _signal(score=Decimal("0.30"), locked=True, matches=True),
        # NOTE: no external_thesis_weakened passed — thesis is HEALTHY
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
        _snapshot(best_bid="0.41", fair="0.42"),
        _position(avg_entry="0.69", hold_sec=90),
        _signal(score=Decimal("0.30"), locked=True, matches=True),
    )
    assert result.reason != "absolute_max_loss_breaker", (
        f"FAIL: breaker fired despite being disabled! reason={result.reason}"
    )
    print(f"PASS: feature disabled → {result.decision_type.value}/{result.reason} (no breaker)")


# =========================================================================
# TEST 3: Loss below threshold — must NOT fire.
# Entry=0.69, bid=0.55 → net ≈ -$0.82 (below $1.50 threshold).
# =========================================================================
def test_loss_below_threshold_does_not_fire():
    """
    Moderate loss (-$0.82) is below the $1.50 absolute threshold.
    Normal exit logic should handle this, not the circuit breaker.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.55", fair="0.56"),
        _position(avg_entry="0.69", hold_sec=90),
        _signal(score=Decimal("0.30"), locked=True, matches=True),
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
        _signal(score=Decimal("0.30"), locked=True, matches=True),
    )
    assert result.reason != "absolute_max_loss_breaker", (
        f"FAIL: breaker fired on fresh position! reason={result.reason}"
    )
    print(f"PASS: short hold → {result.decision_type.value}/{result.reason} (no breaker)")


# =========================================================================
# TEST 5: Confirm breaker fires BEFORE band/thesis logic.
# Verify it bypasses HOLD_IN_BAND completely.
# =========================================================================
def test_breaker_bypasses_hold_in_band():
    """
    Setup a scenario where band="hold" (bid >= 0.68, signal high+locked).
    Normally this would HOLD_IN_BAND. But with loss > $1.50, the breaker
    fires first.

    Entry=0.95 (extremely high entry), bid=0.68 (still in hold band).
    Gross = 5.3 * (0.68*0.998 - 0.95) = 5.3 * (0.6786 - 0.95) = 5.3 * -0.2714 = -1.438
    Net = -1.438 - 0.05 - fee ≈ -1.56
    Net > -1.50 → breaker fires.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.68", fair="0.72"),
        _position(avg_entry="0.95", hold_sec=120),
        _signal(score=Decimal("0.30"), locked=True, matches=True),
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
# TEST 7: No confirmations needed — fires immediately without waiting.
# Unlike catastrophic SL (which needs 2 cycles), the breaker fires in 1 cycle.
# =========================================================================
def test_breaker_fires_immediately_no_confirmations():
    """
    Verify the breaker returns confirm_hits=0 and fires on the FIRST cycle.
    This is critical: at -$1.50, there's no time to wait for confirmations.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.30", fair="0.31"),
        _position(avg_entry="0.69", hold_sec=120, confirm_hits=0),
        _signal(score=Decimal("0.30"), locked=True, matches=True),
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
    test_breaker_fires_immediately_no_confirmations()
    print("\n✅ All circuit breaker tests passed.")
