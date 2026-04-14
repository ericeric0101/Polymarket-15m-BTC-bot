"""
F-2 regression test: Trailing profit release gate in _classify_profitable_exit_intent.

Tests:
1. Tiny profit (0.02) → must NOT trigger (prevents 0.58→0.60 churn)
2. Meaningful peak + meaningful drawdown → MUST trigger release
3. Meaningful peak but NO drawdown (still at peak) → must NOT trigger
4. Meaningful peak + drawdown but hold_sec too short → must NOT trigger
5. profit_run_enabled=False → feature disabled, must NOT trigger
6. Peak just below threshold → must NOT trigger
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal
from bot.exit_engine import ExitPolicyEngine, ExitEngineConfig
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
        profit_run_trailing_drawdown_ps=Decimal("0.07"),
        profit_run_unlock_profit_ps=Decimal("0.18"),
        profit_run_unlock_trailing_drawdown_ps=Decimal("0.02"),
        recycle_locked_side_min_fair_edge_ps=Decimal("0.04"),
        catastrophic_stop_loss_enabled=True,
        catastrophic_stop_loss_usdc=Decimal("0.40"),
        catastrophic_stop_loss_min_score_abs=Decimal("0.50"),
        catastrophic_stop_loss_confirmations=2,
    )
    defaults.update(overrides)
    return ExitEngineConfig(**defaults)


def _make_snapshot(best_bid, fair=None, best_ask=None, spot_minus_strike_bps=None):
    best_bid = Decimal(str(best_bid))
    if fair is None:
        fair = best_bid + Decimal("0.01")
    else:
        fair = Decimal(str(fair))
    if best_ask is None:
        best_ask = best_bid + Decimal("0.01")
    else:
        best_ask = Decimal(str(best_ask))
    return MarketSnapshot(
        instrument_id="test",
        phase="ACTIVE",
        time_left_sec=300.0,
        best_bid=Decimal(str(best_bid)),
        best_ask=Decimal(str(best_ask)),
        fee_rate=Decimal("0.02"),
        spread=best_ask - Decimal(str(best_bid)),
        spread_pct=Decimal("0.01"),
        slippage_buffer_pct=Decimal("0.002"),
        exit_stage=ExitStage.PASSIVE,
        in_reduce_only_tail=False,
        stop_loss_disabled_in_tail=False,
        fair=Decimal(str(fair)),
        fair_edge_ps=max(Decimal("0"), Decimal(str(fair)) - Decimal(str(best_bid))),
        spot_minus_strike_bps=spot_minus_strike_bps if spot_minus_strike_bps is not None else Decimal("10"),
    )


def _make_position(avg_entry, qty=Decimal("5"), hold_sec=120.0,
                   peak_bid=None, peak_fair=None, held_side="UP"):
    return PositionState(
        instrument_id="test",
        qty=qty,
        sellable_qty=qty,
        avg_entry_price=Decimal(str(avg_entry)),
        entry_fee_remaining=Decimal("0.01"),
        hold_sec=hold_sec,
        stop_loss_confirm_hits=0,
        held_side=held_side,
        peak_bid=Decimal(str(peak_bid)) if peak_bid is not None else None,
        peak_fair=Decimal(str(peak_fair)) if peak_fair is not None else None,
    )


def _make_signal(score=Decimal("0.20"), locked=True, matches=True):
    return SignalDecision(
        active_side="UP",
        score=score,
        locked=locked,
        reason="test",
        matches_position=matches,
    )


def test_01_tiny_profit_does_NOT_trigger():
    """
    Entry=0.58, bid=0.60, peak=0.60 → peak_profit=0.02
    Below min threshold (0.06) → must NOT trigger trailing release.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine._classify_profitable_exit_intent(
        snapshot=_make_snapshot(best_bid="0.60", fair="0.61"),
        position=_make_position(avg_entry="0.58", peak_bid="0.60", peak_fair="0.61", hold_sec=120),
        signal=_make_signal(),
        thesis_weakened=False,
        offside_confirmed=False,
        locked_side_invalidated=False,
    )
    intent, reason, meta = result
    assert intent == "continue", (
        f"FAIL: tiny profit triggered release! intent={intent}, reason={reason}"
    )
    assert reason != "trailing_profit_release", (
        f"FAIL: trailing_profit_release fired on tiny profit!"
    )
    print(f"PASS test_01: tiny profit → {intent}/{reason} (no trailing release)")


def test_02_meaningful_peak_and_drawdown_TRIGGERS():
    """
    Entry=0.58, peak=0.72, current bid=0.64 → peak_profit=0.14, drawdown=0.08
    Peak (0.14) >= 0.06 ✓, drawdown (0.08) >= 0.07 ✓, hold_sec=120 >= 60 ✓
    Must trigger trailing_profit_release.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine._classify_profitable_exit_intent(
        snapshot=_make_snapshot(best_bid="0.64", fair="0.65"),
        position=_make_position(avg_entry="0.58", peak_bid="0.72", peak_fair="0.73", hold_sec=120),
        signal=_make_signal(),
        thesis_weakened=False,
        offside_confirmed=False,
        locked_side_invalidated=False,
    )
    intent, reason, meta = result
    assert intent == "neutral", (
        f"FAIL: meaningful peak+drawdown did NOT trigger! intent={intent}, reason={reason}"
    )
    assert reason == "trailing_profit_release", (
        f"FAIL: wrong reason. expected trailing_profit_release, got {reason}"
    )
    assert "drawdown_from_peak" in meta, "FAIL: missing drawdown_from_peak in metadata"
    print(f"PASS test_02: meaningful peak+drawdown → {intent}/{reason} ✓")
    print(f"  metadata: peak_profit_ps={meta.get('peak_profit_ps')}, "
          f"drawdown_from_peak={meta.get('drawdown_from_peak')}")


def test_03_meaningful_peak_but_no_drawdown_does_NOT_trigger():
    """
    Entry=0.58, peak=0.72, current bid=0.71 → peak_profit=0.14, drawdown=0.01
    Drawdown (0.01) < 0.07 → must NOT trigger. Position is still near peak.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine._classify_profitable_exit_intent(
        snapshot=_make_snapshot(best_bid="0.71", fair="0.72"),
        position=_make_position(avg_entry="0.58", peak_bid="0.72", peak_fair="0.73", hold_sec=120),
        signal=_make_signal(),
        thesis_weakened=False,
        offside_confirmed=False,
        locked_side_invalidated=False,
    )
    intent, reason, meta = result
    assert intent == "continue", (
        f"FAIL: peak without drawdown triggered release! intent={intent}, reason={reason}"
    )
    print(f"PASS test_03: peak but no drawdown → {intent}/{reason} (held correctly)")


def test_04_meaningful_peak_but_hold_too_short_does_NOT_trigger():
    """
    Entry=0.58, peak=0.72, bid=0.64, hold_sec=30 (< 60)
    Hold time is below early_profit_hold_min_hold_sec → must NOT trigger.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine._classify_profitable_exit_intent(
        snapshot=_make_snapshot(best_bid="0.64", fair="0.65"),
        position=_make_position(avg_entry="0.58", peak_bid="0.72", peak_fair="0.73", hold_sec=30),
        signal=_make_signal(),
        thesis_weakened=False,
        offside_confirmed=False,
        locked_side_invalidated=False,
    )
    intent, reason, meta = result
    assert intent == "continue", (
        f"FAIL: short hold triggered release! intent={intent}, reason={reason}"
    )
    # Should be held by early_profit_hold (hold_sec < 60 AND peak < 0.08)
    # OR by recycle_locked_side_hold (signal locked + spot supports)
    print(f"PASS test_04: short hold → {intent}/{reason} (protected correctly)")


def test_05_profit_run_disabled_does_NOT_trigger():
    """
    profit_run_enabled=False → trailing gate is disabled entirely.
    """
    engine = ExitPolicyEngine(_make_config(profit_run_enabled=False))
    result = engine._classify_profitable_exit_intent(
        snapshot=_make_snapshot(best_bid="0.64", fair="0.65"),
        position=_make_position(avg_entry="0.58", peak_bid="0.72", peak_fair="0.73", hold_sec=120),
        signal=_make_signal(),
        thesis_weakened=False,
        offside_confirmed=False,
        locked_side_invalidated=False,
    )
    intent, reason, meta = result
    assert intent == "continue", (
        f"FAIL: disabled profit_run triggered release! intent={intent}, reason={reason}"
    )
    print(f"PASS test_05: profit_run_disabled → {intent}/{reason}")


def test_06_peak_just_below_threshold_does_NOT_trigger():
    """
    Entry=0.58, peak=0.6399, bid=0.56 (below entry, so neutral from L80)
    Actually: bid must be > entry for this function to proceed past L80.
    Let's use entry=0.58, peak=0.6399 (peak_profit=0.0599 < 0.06), bid=0.59
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine._classify_profitable_exit_intent(
        snapshot=_make_snapshot(best_bid="0.59", fair="0.60"),
        position=_make_position(avg_entry="0.58", peak_bid="0.6399", peak_fair="0.6499", hold_sec=120),
        signal=_make_signal(),
        thesis_weakened=False,
        offside_confirmed=False,
        locked_side_invalidated=False,
    )
    intent, reason, meta = result
    assert intent == "continue", (
        f"FAIL: sub-threshold peak triggered release! intent={intent}, reason={reason}"
    )
    print(f"PASS test_06: peak just below threshold → {intent}/{reason}")


def test_07_large_peak_large_drawdown_still_profitable():
    """
    Entry=0.50, peak=0.93, bid=0.82 → peak_profit=0.43, drawdown=0.11
    Large winner that retraced significantly. Must trigger release.
    This is the critical case: entry 0.50, peak 0.93, now at 0.82
    would have continued holding to expiry in the old code.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine._classify_profitable_exit_intent(
        snapshot=_make_snapshot(best_bid="0.82", fair="0.83"),
        position=_make_position(avg_entry="0.50", peak_bid="0.93", peak_fair="0.94", hold_sec=200),
        signal=_make_signal(),
        thesis_weakened=False,
        offside_confirmed=False,
        locked_side_invalidated=False,
    )
    intent, reason, meta = result
    assert intent == "neutral", (
        f"FAIL: large peak + large drawdown did NOT trigger! intent={intent}, reason={reason}"
    )
    assert reason == "trailing_profit_release"
    print(f"PASS test_07: large peak (0.43) + large drawdown (0.11) → released ✓")


def test_08_thesis_weakened_bypasses_all_holds():
    """
    Even without trailing gate, thesis_weakened should return neutral at L101.
    This verifies existing behavior is not broken.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine._classify_profitable_exit_intent(
        snapshot=_make_snapshot(best_bid="0.70", fair="0.71"),
        position=_make_position(avg_entry="0.58", peak_bid="0.72", peak_fair="0.73", hold_sec=120),
        signal=_make_signal(),
        thesis_weakened=True,  # thesis broken
        offside_confirmed=False,
        locked_side_invalidated=False,
    )
    intent, reason, meta = result
    assert intent == "neutral", (
        f"FAIL: thesis_weakened didn't return neutral! intent={intent}"
    )
    print(f"PASS test_08: thesis_weakened → neutral (existing behavior preserved)")


if __name__ == "__main__":
    test_01_tiny_profit_does_NOT_trigger()
    test_02_meaningful_peak_and_drawdown_TRIGGERS()
    test_03_meaningful_peak_but_no_drawdown_does_NOT_trigger()
    test_04_meaningful_peak_but_hold_too_short_does_NOT_trigger()
    test_05_profit_run_disabled_does_NOT_trigger()
    test_06_peak_just_below_threshold_does_NOT_trigger()
    test_07_large_peak_large_drawdown_still_profitable()
    test_08_thesis_weakened_bypasses_all_holds()
    print("\n✅ All F-2 tests passed.")
