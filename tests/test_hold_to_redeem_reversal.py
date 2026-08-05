from decimal import Decimal

from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.models import ExitDecisionType, MarketSnapshot, PositionState, SignalDecision


def _config() -> ExitEngineConfig:
    return ExitEngineConfig(
        hold_to_redeem_enabled=True,
        min_hold_sec=0,
        stop_loss_usdc=Decimal("0.50"),
        stop_loss_confirmations=2,
        stop_loss_requires_thesis_weakening=True,
        stop_loss_thesis_min_score_abs=Decimal("0.18"),
        stop_loss_hold_on_none_signal=True,
        conviction_band_min_price=Decimal("0.60"),
        hold_band_min_price=Decimal("0.68"),
        conviction_band_min_score_abs=Decimal("0.15"),
        hold_band_min_score_abs=Decimal("0.15"),
        hold_band_release_min_roi=Decimal("0.15"),
        conviction_stop_loss_multiplier=Decimal("1.75"),
        conviction_extra_confirmations=1,
        hold_band_requires_locked=True,
    )


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        instrument_id="inst-up",
        phase="ACTIVE",
        time_left_sec=300.0,
        best_bid=Decimal("0.70"),
        best_ask=Decimal("0.71"),
        fee_rate=Decimal("0"),
        spread=Decimal("0.01"),
        spread_pct=Decimal("0.014"),
        slippage_buffer_pct=Decimal("0"),
        exit_stage="PASSIVE",
        in_reduce_only_tail=False,
        stop_loss_disabled_in_tail=False,
        fair=Decimal("0.72"),
        fair_edge_ps=Decimal("0.02"),
        spot_minus_strike_bps=Decimal("5"),
    )


def _position() -> PositionState:
    return PositionState(
        instrument_id="inst-up",
        qty=Decimal("5"),
        sellable_qty=Decimal("5"),
        avg_entry_price=Decimal("0.55"),
        entry_fee_remaining=Decimal("0"),
        hold_sec=120.0,
        stop_loss_confirm_hits=0,
        held_side="UP",
        peak_bid=Decimal("0.72"),
        peak_fair=Decimal("0.72"),
    )


def test_hold_to_redeem_survives_normal_profit_but_confirmed_reversal_can_exit():
    engine = ExitPolicyEngine(_config())
    signal = SignalDecision(
        active_side="DOWN",
        score=Decimal("-0.45"),
        locked=True,
        reason="confirmed reversal",
        matches_position=False,
    )

    normal_signal = SignalDecision(
        active_side="UP",
        score=Decimal("0.45"),
        locked=True,
        reason="supported",
        matches_position=True,
    )
    normal = engine.evaluate(_snapshot(), _position(), normal_signal)
    assert normal.decision_type == ExitDecisionType.HOLD_TO_REDEEM

    reversal = engine.evaluate(
        _snapshot(),
        _position(),
        signal,
        external_thesis_weakened=True,
        external_offside_confirmed=True,
        locked_side_invalidated=True,
        confirmed_adverse_exit_active=True,
    )
    assert reversal.decision_type != ExitDecisionType.HOLD_TO_REDEEM
