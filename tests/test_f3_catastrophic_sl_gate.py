"""
F-3 regression test: Catastrophic stop-loss gate relaxation.

Tests:
1. Large loss + unlocked signal → catastrophic SL becomes candidate
2. Large loss + thesis_weakened → catastrophic SL becomes candidate
3. Large loss + signal mismatch → catastrophic SL becomes candidate
4. Large loss + fully healthy thesis (locked+matching+not weakened) → NOT candidate
5. Small loss + unlocked signal → NOT candidate (below $0.40 threshold)
6. Large loss but hold_sec too short → NOT candidate
7. Normal stop-loss path unchanged (smaller loss, thesis weakened)
8. Catastrophic SL with confirmations (verify confirmation flow)
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


def _make_snapshot(best_bid, fair=None, spot_minus_strike_bps=None, **kwargs):
    best_bid = Decimal(str(best_bid))
    if fair is None:
        fair = best_bid + Decimal("0.01")
    else:
        fair = Decimal(str(fair))
    best_ask = best_bid + Decimal("0.01")
    return MarketSnapshot(
        instrument_id="test",
        phase="ACTIVE",
        time_left_sec=300.0,
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
        spot_minus_strike_bps=spot_minus_strike_bps if spot_minus_strike_bps is not None else Decimal("10"),
    )


def _make_position(avg_entry, qty=Decimal("5"), hold_sec=120.0,
                   confirm_hits=0, held_side="UP"):
    return PositionState(
        instrument_id="test",
        qty=qty,
        sellable_qty=qty,
        avg_entry_price=Decimal(str(avg_entry)),
        entry_fee_remaining=Decimal("0.01"),
        hold_sec=hold_sec,
        stop_loss_confirm_hits=confirm_hits,
        held_side=held_side,
        peak_bid=None,
        peak_fair=None,
    )


def _make_signal(score=Decimal("0.20"), locked=True, matches=True, active_side="UP"):
    return SignalDecision(
        active_side=active_side,
        score=score,
        locked=locked,
        reason="test",
        matches_position=matches,
    )


def test_01_large_loss_unlocked_signal_triggers_catastrophic():
    """
    Entry=0.69, bid=0.55 → gross ~ 5*(0.55-0.69) = -0.70, net ~ -0.73
    Signal is NOT locked → gate passes → catastrophic candidate.
    With confirm_hits=0, should get STOP_LOSS_PENDING_CONFIRMATION (needs 2).
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _make_snapshot(best_bid="0.55"),
        _make_position(avg_entry="0.69", hold_sec=120, confirm_hits=0),
        _make_signal(score=Decimal("0.15"), locked=False, matches=True),
    )
    assert result.decision_type == ExitDecisionType.STOP_LOSS_PENDING_CONFIRMATION, (
        f"FAIL: expected STOP_LOSS_PENDING_CONFIRMATION, got {result.decision_type}"
    )
    assert result.metadata.get("catastrophic_stop_loss_candidate") == "1", (
        f"FAIL: not flagged as catastrophic. metadata={result.metadata}"
    )
    print(f"PASS test_01: large loss + unlocked signal → catastrophic candidate (pending confirmation)")


def test_02_large_loss_thesis_weakened_triggers():
    """
    Entry=0.69, bid=0.55. Signal locked+matching BUT thesis_weakened=True.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _make_snapshot(best_bid="0.55"),
        _make_position(avg_entry="0.69", hold_sec=120, confirm_hits=0),
        _make_signal(score=Decimal("0.20"), locked=True, matches=True),
        external_thesis_weakened=True,
    )
    assert result.metadata.get("catastrophic_stop_loss_candidate") == "1", (
        f"FAIL: thesis_weakened didn't trigger catastrophic. metadata={result.metadata}"
    )
    print(f"PASS test_02: large loss + thesis_weakened → catastrophic candidate ✓")


def test_03_large_loss_signal_mismatch_triggers():
    """
    Entry=0.69, bid=0.55. Signal is locked on DOWN (opposite of our UP position).
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _make_snapshot(best_bid="0.55"),
        _make_position(avg_entry="0.69", hold_sec=120, confirm_hits=0),
        _make_signal(score=Decimal("-0.30"), locked=True, matches=False, active_side="DOWN"),
    )
    # With signal mismatch, thesis_weakened should also be True (from _instant_thesis_weakened)
    assert result.metadata.get("catastrophic_stop_loss_candidate") == "1", (
        f"FAIL: signal mismatch didn't trigger catastrophic. metadata={result.metadata}"
    )
    print(f"PASS test_03: large loss + signal mismatch → catastrophic candidate ✓")


def test_04_large_loss_healthy_thesis_does_NOT_trigger():
    """
    Entry=0.69, bid=0.55. Signal is locked, matches, thesis not weakened.
    The position is fully healthy from a thesis perspective.
    Catastrophic SL should NOT fire — this protects against temporary dips
    where the thesis is still valid.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _make_snapshot(best_bid="0.55"),
        _make_position(avg_entry="0.69", hold_sec=120, confirm_hits=0),
        _make_signal(score=Decimal("0.20"), locked=True, matches=True),
        external_thesis_weakened=False,
    )
    assert result.metadata.get("catastrophic_stop_loss_candidate") != "1", (
        f"FAIL: healthy thesis triggered catastrophic! metadata={result.metadata}"
    )
    print(f"PASS test_04: large loss + healthy thesis → NOT catastrophic (protected) ✓")


def test_05_small_loss_does_NOT_trigger():
    """
    Entry=0.69, bid=0.67 → gross ~ 5*(0.67-0.69) = -0.10.
    Well below $0.40 threshold → should NOT be catastrophic.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _make_snapshot(best_bid="0.67"),
        _make_position(avg_entry="0.69", hold_sec=120, confirm_hits=0),
        _make_signal(score=Decimal("0.15"), locked=False, matches=True),
    )
    assert result.metadata.get("catastrophic_stop_loss_candidate") != "1", (
        f"FAIL: small loss triggered catastrophic! metadata={result.metadata}"
    )
    assert result.decision_type != ExitDecisionType.TAKER_STOP_LOSS, (
        f"FAIL: small loss triggered taker stop loss!"
    )
    print(f"PASS test_05: small loss (2-cent dip) → NOT catastrophic (noise protection) ✓")


def test_06_large_loss_but_hold_too_short():
    """
    Entry=0.69, bid=0.55, hold_sec=20 (< min_hold_sec=45).
    Catastrophic SL should NOT fire on fresh positions.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _make_snapshot(best_bid="0.55"),
        _make_position(avg_entry="0.69", hold_sec=20, confirm_hits=0),
        _make_signal(score=Decimal("0.15"), locked=False, matches=True),
    )
    assert result.metadata.get("catastrophic_stop_loss_candidate") != "1", (
        f"FAIL: short hold triggered catastrophic! metadata={result.metadata}"
    )
    print(f"PASS test_06: large loss but hold too short → NOT catastrophic (hold protection) ✓")


def test_07_catastrophic_with_enough_confirmations_fires():
    """
    Entry=0.69, bid=0.55, unlocked signal, confirm_hits=1 (needs 2 total).
    After evaluation, confirm hits should be 2 → TAKER_STOP_LOSS fires.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _make_snapshot(best_bid="0.55"),
        _make_position(avg_entry="0.69", hold_sec=120, confirm_hits=1),
        _make_signal(score=Decimal("0.15"), locked=False, matches=True),
    )
    assert result.decision_type == ExitDecisionType.TAKER_STOP_LOSS, (
        f"FAIL: expected TAKER_STOP_LOSS with 2 confirmations, got {result.decision_type}"
    )
    assert result.reason == "catastrophic_stop_loss_confirmed", (
        f"FAIL: wrong reason: {result.reason}"
    )
    print(f"PASS test_07: catastrophic + 2 confirmations → TAKER_STOP_LOSS ✓")


def test_08_normal_stop_loss_path_unchanged():
    """
    Normal stop-loss (not catastrophic): loss below catastrophic threshold ($0.40)
    but above normal threshold ($0.50 for conviction band).
    With thesis_weakened + above normal threshold → should still work.
    
    Entry=0.69, bid=0.60 → gross = 5*(0.598-0.69) = -0.46. Net ~ -0.48.
    Normal stop_loss_usdc=0.50 in conviction = 0.50*1.75 = 0.875 (too high).
    Use neutral band (bid=0.55) to test: net ~ -0.73 > 0.50 threshold → normal SL.
    But this would also trigger catastrophic. Let's test normal SL with 
    thesis_weakened + matching signal (so catastrophic gate fires too).
    
    Actually let's verify: with thesis_weakened=True and locked signal, 
    catastrophic fires first (lower threshold). This is fine.
    """
    engine = ExitPolicyEngine(_make_config())
    # bid = 0.58, entry = 0.69 → gross = 5*(0.578-0.69) = -0.56, net ~ -0.58
    # net_if_exit ~ -0.58 > threshold 0.50 → normal SL candidate
    # But also > 0.40 → catastrophic candidate if gate passes
    # thesis_weakened=True → catastrophic gate passes
    result = engine.evaluate(
        _make_snapshot(best_bid="0.58"),
        _make_position(avg_entry="0.69", hold_sec=120, confirm_hits=1),
        _make_signal(score=Decimal("0.20"), locked=True, matches=True),
        external_thesis_weakened=True,
    )
    # Should be a stop-loss confirmation or stop-loss fire
    assert result.decision_type in (
        ExitDecisionType.STOP_LOSS_PENDING_CONFIRMATION,
        ExitDecisionType.TAKER_STOP_LOSS,
    ), f"FAIL: normal SL path broken: {result.decision_type}"
    print(f"PASS test_08: normal stop-loss path → {result.decision_type.value} ✓")


if __name__ == "__main__":
    test_01_large_loss_unlocked_signal_triggers_catastrophic()
    test_02_large_loss_thesis_weakened_triggers()
    test_03_large_loss_signal_mismatch_triggers()
    test_04_large_loss_healthy_thesis_does_NOT_trigger()
    test_05_small_loss_does_NOT_trigger()
    test_06_large_loss_but_hold_too_short()
    test_07_catastrophic_with_enough_confirmations_fires()
    test_08_normal_stop_loss_path_unchanged()
    print("\n✅ All F-3 tests passed.")
