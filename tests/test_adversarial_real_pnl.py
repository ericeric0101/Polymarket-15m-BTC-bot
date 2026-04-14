"""
High-value adversarial tests derived from ACTUAL PnL loss patterns
observed in trade_journal.db (Phase 1 analysis).

These tests simulate real trading sequences, not just isolated gate checks.
Each test is annotated with the specific trade ID or loss pattern it targets.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal
from bot.exit_engine import ExitPolicyEngine, ExitEngineConfig, ExitDecisionType
from bot.models import (
    MarketSnapshot, PositionState, SignalDecision, ExitDecisionType,
)
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


def _snapshot(best_bid, fair=None, spot_minus_strike_bps=None, time_left_sec=300.0,
              stop_loss_disabled_in_tail=False):
    best_bid = Decimal(str(best_bid))
    fair = Decimal(str(fair)) if fair is not None else best_bid + Decimal("0.01")
    best_ask = best_bid + Decimal("0.01")
    return MarketSnapshot(
        instrument_id="test",
        phase="ACTIVE",
        time_left_sec=time_left_sec,
        best_bid=best_bid,
        best_ask=best_ask,
        fee_rate=Decimal("0.02"),
        spread=Decimal("0.01"),
        spread_pct=Decimal("0.01"),
        slippage_buffer_pct=Decimal("0.002"),
        exit_stage=ExitStage.PASSIVE,
        in_reduce_only_tail=False,
        stop_loss_disabled_in_tail=stop_loss_disabled_in_tail,
        fair=fair,
        fair_edge_ps=max(Decimal("0"), fair - best_bid),
        spot_minus_strike_bps=spot_minus_strike_bps if spot_minus_strike_bps is not None else Decimal("10"),
    )


def _position(avg_entry, hold_sec=120.0, confirm_hits=0,
              peak_bid=None, peak_fair=None, held_side="UP", qty=Decimal("5.3")):
    return PositionState(
        instrument_id="test",
        qty=qty,
        sellable_qty=qty,
        avg_entry_price=Decimal(str(avg_entry)),
        entry_fee_remaining=Decimal("0.05"),
        hold_sec=hold_sec,
        stop_loss_confirm_hits=confirm_hits,
        held_side=held_side,
        peak_bid=Decimal(str(peak_bid)) if peak_bid is not None else None,
        peak_fair=Decimal(str(peak_fair)) if peak_fair is not None else None,
    )


def _signal(score=Decimal("0.20"), locked=True, matches=True, active_side="UP"):
    return SignalDecision(
        active_side=active_side,
        score=score,
        locked=locked,
        reason="test",
        matches_position=matches,
    )


# =========================================================================
# TEST 1: Full lifecycle of trade 1776086100
# Entry=0.69, peak=0.93, position retraces to 0.86.
# QUESTION: Does evaluate() actually release this position via F-2?
#
# This is the test the original F-2 suite MISSED — it only tested the
# internal helper, not the full evaluate() path. The critical question is:
# does _classify_profitable_exit_intent returning "neutral" actually
# survive the subsequent HOLD_IN_BAND check at L320?
# =========================================================================
def test_real_trade_1776086100_full_evaluate_lifecycle():
    """
    Simulates the real failure: entry 0.69, peak 0.93, bid retraces to 0.86.
    Signal is locked UP, matches position, thesis not weakened.

    The F-2 trailing gate should return "neutral" from _classify_profitable_exit_intent.
    Then evaluate() should NOT re-block it with HOLD_IN_BAND at L320.

    This tests the FULL evaluate() path, not just the helper.
    """
    engine = ExitPolicyEngine(_make_config())

    # Phase A: position at peak — should still HOLD_IN_BAND
    result_peak = engine.evaluate(
        _snapshot(best_bid="0.93", fair="0.94"),
        _position(avg_entry="0.69", peak_bid="0.93", peak_fair="0.94", hold_sec=300),
        _signal(score=Decimal("0.25"), locked=True, matches=True),
    )
    assert result_peak.decision_type == ExitDecisionType.HOLD_IN_BAND, (
        f"Phase A FAIL: at peak, should HOLD_IN_BAND, got {result_peak.decision_type}"
    )

    # Phase B: position retraces to 0.86 (drawdown = 0.93 - 0.86 = 0.07 >= 0.06)
    # Peak profit = 0.93 - 0.69 = 0.24 >= 0.06
    # Hold time = 300s >= 60s
    # ALL trailing conditions met — should release.
    result_retrace = engine.evaluate(
        _snapshot(best_bid="0.86", fair="0.87"),
        _position(avg_entry="0.69", peak_bid="0.93", peak_fair="0.94", hold_sec=300),
        _signal(score=Decimal("0.25"), locked=True, matches=True),
    )

    # Critical check: after trailing_profit_release returns "neutral",
    # does evaluate() re-block with the band == "hold" check at L320?
    # band classification: bid=0.86 >= hold_band_min_price=0.68, score 0.25 >= 0.12,
    #   signal locked + matches → band = "hold"
    # L320: if band == "hold" and not hold_band_released → HOLD_IN_BAND
    #
    # BUT: L306 happens FIRST: profitable_intent is checked, and if it returns
    # "neutral" (from trailing release), then L306 does NOT short-circuit.
    # The code at L306-318 only short-circuits when profitable_intent == "continue".
    # When it's "neutral", we fall through to L320 (band check).
    #
    # L320: band == "hold" → True, hold_band_released? → need ROI >= 0.15
    # ROI = net_if_exit / entry_cost_usdc
    # gross = 5.3 * (0.86*0.998 - 0.69) = 5.3 * (0.85828 - 0.69) = 5.3 * 0.16828 = 0.89188
    # net = 0.89188 - 0.05 (entry_fee) - exit_fee ≈ 0.89188 - 0.05 - 0.114 ≈ 0.728
    # entry_cost = 5.3 * 0.69 + 0.05 = 3.707
    # ROI = 0.728 / 3.707 ≈ 0.196 → 19.6% >= 15% → hold_band_released = True!
    #
    # So the band check PASSES because ROI >= 15%. The position is released.
    # BUT: what if the ROI was lower (smaller profit)?

    if result_retrace.decision_type == ExitDecisionType.HOLD_IN_BAND:
        # If it still holds, check if hold_band_released was involved
        released = result_retrace.metadata.get("hold_band_released")
        reason = result_retrace.reason
        print(f"⚠️  Phase B: still HOLD_IN_BAND, reason={reason}, "
              f"hold_band_released={released}")
        print(f"   metadata: {result_retrace.metadata}")
        # This is the failure case we need to understand
        assert False, (
            f"Phase B FAIL: trailing release should have freed this position. "
            f"reason={reason}, type={result_retrace.decision_type}"
        )
    else:
        print(f"PASS Phase B: position released after retrace → "
              f"{result_retrace.decision_type.value} / {result_retrace.reason}")
        print(f"  net_if_exit={result_retrace.net_if_exit:.4f}")


# =========================================================================
# TEST 2: Trailing release fires but band re-catches — smaller profit case
# Entry=0.65, peak=0.73, bid retraces to 0.66.
# Peak profit = 0.08, drawdown = 0.07. Trailing fires.
# But ROI is small → hold_band_released might be False → HOLD_IN_BAND re-catches.
# This tests the interaction gap between F-2 and L320.
# =========================================================================
def test_trailing_release_vs_hold_band_reblock_small_winner():
    """
    Entry=0.65, peak=0.73, bid=0.66.
    Peak profit = 0.08 >= 0.06 ✓
    Drawdown = 0.07 >= 0.06 ✓
    Hold = 120s >= 60s ✓
    → _classify_profitable_exit_intent returns "neutral"

    But then in evaluate():
    band = "hold"? bid=0.66 < hold_band_min_price=0.68 → band may be "neutral" or "conviction"
    bid=0.66 >= conviction_band_min_price=0.60 → band = "conviction" (if score high enough)

    Even if band="conviction", L320 only checks band=="hold", so conviction
    doesn't block. The position should be released.

    This tests whether a modest winner (not a huge one) actually gets released.
    """
    engine = ExitPolicyEngine(_make_config())
    result = engine.evaluate(
        _snapshot(best_bid="0.66", fair="0.67"),
        _position(avg_entry="0.65", peak_bid="0.73", peak_fair="0.74", hold_sec=120),
        _signal(score=Decimal("0.20"), locked=True, matches=True),
    )
    # The position is barely profitable (bid 0.66 > entry 0.65).
    # Trailing release should return "neutral".
    # Band at bid=0.66: below hold_band (0.68) but above conviction_band (0.60)
    # → band = "conviction" (not "hold"), so L320 doesn't block.
    # → falls through to stop-loss section (L331+), but position is profitable
    #   so price_adverse=False → stop_loss_candidate=False → returns NONE.
    if result.decision_type == ExitDecisionType.HOLD_IN_BAND:
        print(f"⚠️  FAIL: modest winner re-blocked by HOLD_IN_BAND. "
              f"reason={result.reason}")
        print(f"   metadata: {result.metadata}")
        assert False, "Modest winner should be released after trailing drawdown"
    else:
        print(f"PASS: modest winner released → {result.decision_type.value}/{result.reason}")
        print(f"  net_if_exit={result.net_if_exit:.4f}")


# =========================================================================
# TEST 3: Noise oscillation — bid bounces ±0.02 repeatedly
# Entry=0.69. Bid oscillates: 0.69 → 0.67 → 0.69 → 0.67 → 0.69
# Signal stays locked UP, matches position.
# NO exit should trigger at any point. This verifies the system is noise-stable.
# =========================================================================
def test_noise_oscillation_no_exit():
    """
    Simulates BTC 15m noise: ±0.02 oscillation around entry price.
    The bot must NOT exit on any cycle. Tests all three fixes together:
    - F-2: peak profit at most 0.02 (far below 0.06) → no trailing release
    - F-3: net loss at most ~$0.10 (far below $0.40) → no catastrophic SL
    - Normal SL: thesis not weakened → no normal SL
    """
    engine = ExitPolicyEngine(_make_config())
    oscillation_bids = ["0.69", "0.67", "0.69", "0.67", "0.69", "0.71", "0.69"]
    peak_so_far = Decimal("0.69")

    for cycle_idx, bid_str in enumerate(oscillation_bids):
        bid = Decimal(bid_str)
        peak_so_far = max(peak_so_far, bid)
        hold_sec = 60 + cycle_idx * 5  # 60s base + 5s per cycle

        result = engine.evaluate(
            _snapshot(best_bid=bid_str, fair=str(bid + Decimal("0.01"))),
            _position(
                avg_entry="0.69",
                peak_bid=str(peak_so_far),
                peak_fair=str(peak_so_far + Decimal("0.01")),
                hold_sec=hold_sec,
            ),
            _signal(score=Decimal("0.20"), locked=True, matches=True),
        )

        is_exit = result.decision_type in (
            ExitDecisionType.TAKER_STOP_LOSS,
            ExitDecisionType.DE_RISK,
        )
        assert not is_exit, (
            f"Cycle {cycle_idx} FAIL: noise oscillation triggered exit! "
            f"bid={bid_str}, type={result.decision_type}, reason={result.reason}"
        )

    print(f"PASS: 7-cycle noise oscillation (±0.02) → no exits triggered")


# =========================================================================
# TEST 4: Signal locked while price collapses — NOW COVERED by circuit breaker
# Entry=0.69, signal locked UP, matches=True, thesis NOT weakened.
# Bid drops to 0.20. Net loss ≈ -$2.65 (exceeds $1.50 threshold).
#
# PREVIOUSLY: This was a KNOWN LIMITATION — no exit path fired.
# NOW: The absolute_max_loss_breaker fires BEFORE band/thesis logic.
# =========================================================================
def test_signal_locked_price_collapse_breaker_catches():
    """
    The REAL failure pattern from trade 1776024900:
    Signal locked UP, matches, thesis good. Price collapses from 0.69 to 0.20.

    Previously this was a documented gap — no exit path fired.
    Now the absolute_max_loss_breaker catches it:
    - net_if_exit ≈ -$2.65 ≤ -$1.50 ✓
    - hold_sec = 300 ≥ 60 ✓
    - price_adverse = True ✓
    → TAKER_STOP_LOSS / absolute_max_loss_breaker
    """
    engine = ExitPolicyEngine(_make_config())

    # Step 1: Signal locked + matching + not weakened, bid=0.20
    # The circuit breaker fires regardless of thesis state.
    result_locked = engine.evaluate(
        _snapshot(best_bid="0.20", fair="0.21"),
        _position(avg_entry="0.69", hold_sec=300, confirm_hits=0),
        _signal(score=Decimal("0.20"), locked=True, matches=True),
        external_thesis_weakened=False,
    )
    assert result_locked.decision_type == ExitDecisionType.TAKER_STOP_LOSS, (
        f"Step 1 FAIL: breaker should fire at loss ≈ -$2.65. "
        f"Got {result_locked.decision_type.value}/{result_locked.reason}"
    )
    assert result_locked.reason == "absolute_max_loss_breaker", (
        f"Step 1 FAIL: wrong reason. Expected absolute_max_loss_breaker, "
        f"got {result_locked.reason}"
    )
    print(f"Step 1: bid=0.20, signal locked+matching+healthy → "
          f"{result_locked.decision_type.value}/{result_locked.reason} ✓")
    print(f"  net_if_exit={result_locked.net_if_exit:.4f}")
    print(f"  ✅ GAP CLOSED: unconditional circuit breaker caught this position.")

    # Step 2: Verify that at moderate loss (bid=0.55), breaker does NOT fire
    # (net ≈ -$0.82, below $1.50 threshold). F-3 thesis gate is the backstop here.
    result_moderate = engine.evaluate(
        _snapshot(best_bid="0.55", fair="0.56"),
        _position(avg_entry="0.69", hold_sec=300, confirm_hits=0),
        _signal(score=Decimal("0.20"), locked=True, matches=True),
        external_thesis_weakened=False,
    )
    assert result_moderate.reason != "absolute_max_loss_breaker", (
        f"Step 2 FAIL: breaker should NOT fire at moderate loss. "
        f"Got {result_moderate.reason}"
    )
    print(f"Step 2: bid=0.55 (moderate loss) → "
          f"{result_moderate.decision_type.value}/{result_moderate.reason} (no breaker) ✓")


# =========================================================================
# TEST 5: Profitable → unprofitable transition while HOLD_IN_BAND active
# Entry=0.69, bid rises to 0.74 (HOLD_IN_BAND active), then drops to 0.65.
# Peak profit = 0.05 (below 0.06 trailing threshold).
# Position goes underwater. How fast does the system switch from
# HOLD_IN_BAND to stop-loss candidate?
#
# This tests the "almost profitable" leak: 7/9 losing trades were once
# profitable but peak was too small to trigger the trailing release.
# =========================================================================
def test_profitable_to_underwater_transition():
    """
    Entry=0.69, peak=0.74 (peak_profit=0.05, below trailing threshold 0.06).
    Bid drops to 0.65 → position is now underwater.

    The trailing release gate does NOT fire (peak < 0.06).
    The position must transition from HOLD_IN_BAND to a loss-candidate state.

    Question: does the system handle this cleanly, or does it get stuck?
    """
    engine = ExitPolicyEngine(_make_config())

    # Phase A: at peak, still profitable → should be HOLD_IN_BAND or NONE
    result_peak = engine.evaluate(
        _snapshot(best_bid="0.74", fair="0.75"),
        _position(avg_entry="0.69", peak_bid="0.74", peak_fair="0.75", hold_sec=120),
        _signal(score=Decimal("0.20"), locked=True, matches=True),
    )
    print(f"Phase A (peak 0.74): {result_peak.decision_type.value}/{result_peak.reason}")

    # Phase B: bid drops below entry → no longer profitable
    # _classify_profitable_exit_intent L80: bid <= avg_entry → returns "neutral"
    # So profitable_intent = "neutral", falls through L306-318.
    # Band check at L320: bid=0.65 < hold_band 0.68 but >= conviction_band 0.60
    # → band = "conviction" (if score >= conviction_min). conviction doesn't block at L320.
    # Falls through to stop-loss at L331+.
    # price_adverse = True (0.65 < 0.69), thesis_weakened = False
    # → stop_loss_requires_thesis_weakening = True AND thesis_weakened = False
    #   → stop_loss_candidate = False (L358-361)
    # → returns NONE with reason "thesis_still_supported"
    result_underwater = engine.evaluate(
        _snapshot(best_bid="0.65", fair="0.66"),
        _position(avg_entry="0.69", peak_bid="0.74", peak_fair="0.75", hold_sec=180),
        _signal(score=Decimal("0.20"), locked=True, matches=True),
        external_thesis_weakened=False,
    )
    print(f"Phase B (bid 0.65, underwater): {result_underwater.decision_type.value}"
          f"/{result_underwater.reason}")
    print(f"  net_if_exit={result_underwater.net_if_exit:.4f}")

    # Phase C: thesis finally weakens → stop-loss should now be candidate
    result_thesis_weak = engine.evaluate(
        _snapshot(best_bid="0.60", fair="0.61"),
        _position(avg_entry="0.69", peak_bid="0.74", peak_fair="0.75",
                  hold_sec=240, confirm_hits=1),
        _signal(score=Decimal("-0.10"), locked=False, matches=False, active_side="DOWN"),
        external_thesis_weakened=True,
    )
    print(f"Phase C (bid 0.60, thesis weakened): {result_thesis_weak.decision_type.value}"
          f"/{result_thesis_weak.reason}")

    # With thesis_weakened=True, the stop-loss path should activate
    assert result_thesis_weak.decision_type in (
        ExitDecisionType.STOP_LOSS_PENDING_CONFIRMATION,
        ExitDecisionType.TAKER_STOP_LOSS,
    ), (
        f"Phase C FAIL: thesis weakened but no stop-loss. "
        f"type={result_thesis_weak.decision_type}"
    )
    print(f"PASS: profitable→underwater transition handled correctly")
    print(f"  ⚠️  NOTE: the gap between Phase A (peak) and Phase C (exit) "
          f"is where PnL leaks — the bot held from 0.74 to 0.60 waiting for "
          f"thesis to weaken, losing ~$0.48 per share.")


if __name__ == "__main__":
    print("=" * 70)
    print("TEST 1: Real trade 1776086100 full evaluate() lifecycle")
    print("=" * 70)
    test_real_trade_1776086100_full_evaluate_lifecycle()

    print("\n" + "=" * 70)
    print("TEST 2: Trailing release vs hold_band re-catch (modest winner)")
    print("=" * 70)
    test_trailing_release_vs_hold_band_reblock_small_winner()

    print("\n" + "=" * 70)
    print("TEST 3: Noise oscillation stability (±0.02)")
    print("=" * 70)
    test_noise_oscillation_no_exit()

    print("\n" + "=" * 70)
    print("TEST 4: Signal locked while price collapses (BREAKER CATCHES)")
    print("=" * 70)
    test_signal_locked_price_collapse_breaker_catches()

    print("\n" + "=" * 70)
    print("TEST 5: Profitable → underwater transition")
    print("=" * 70)
    test_profitable_to_underwater_transition()

    print("\n" + "=" * 70)
    print("✅ All adversarial tests completed.")
    print("=" * 70)
