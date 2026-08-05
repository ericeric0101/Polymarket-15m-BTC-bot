from decimal import Decimal

from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.models import MarketSnapshot


def test_trailing_threshold_is_ratio_based_but_not_micro_sensitive():
    engine = ExitPolicyEngine(
        ExitEngineConfig(
            hold_to_redeem_enabled=False,
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
            hold_band_release_min_roi=Decimal("0"),
            conviction_stop_loss_multiplier=Decimal("1.75"),
            conviction_extra_confirmations=1,
            hold_band_requires_locked=True,
        )
    )

    assert engine._effective_trailing_drawdown(
        peak_profit_ps=Decimal("0.07"),
        snapshot=MarketSnapshot(
            instrument_id="x", phase="ACTIVE", time_left_sec=600,
            best_bid=Decimal("0.60"), best_ask=Decimal("0.61"),
            fee_rate=Decimal("0"), spread=Decimal("0.01"), spread_pct=Decimal("0.016"),
            slippage_buffer_pct=Decimal("0"), exit_stage="PASSIVE",
            in_reduce_only_tail=False, stop_loss_disabled_in_tail=False,
            fair=Decimal("0.62"), fair_edge_ps=Decimal("0.02"),
            spot_minus_strike_bps=Decimal("10"),
        ),
    ) == Decimal("0.05")

    assert engine._effective_trailing_drawdown(
        peak_profit_ps=Decimal("0.18"),
        snapshot=MarketSnapshot(
            instrument_id="x", phase="ACTIVE", time_left_sec=600,
            best_bid=Decimal("0.70"), best_ask=Decimal("0.71"),
            fee_rate=Decimal("0"), spread=Decimal("0.01"), spread_pct=Decimal("0.014"),
            slippage_buffer_pct=Decimal("0"), exit_stage="PASSIVE",
            in_reduce_only_tail=False, stop_loss_disabled_in_tail=False,
            fair=Decimal("0.72"), fair_edge_ps=Decimal("0.02"),
            spot_minus_strike_bps=Decimal("10"),
        ),
    ) == Decimal("0.05")


def test_trailing_threshold_widens_near_settlement():
    engine = ExitPolicyEngine(
        ExitEngineConfig(
            hold_to_redeem_enabled=False,
            min_hold_sec=0,
            stop_loss_usdc=Decimal("0.50"), stop_loss_confirmations=2,
            stop_loss_requires_thesis_weakening=True,
            stop_loss_thesis_min_score_abs=Decimal("0.18"),
            stop_loss_hold_on_none_signal=True,
            conviction_band_min_price=Decimal("0.60"), hold_band_min_price=Decimal("0.68"),
            conviction_band_min_score_abs=Decimal("0.15"), hold_band_min_score_abs=Decimal("0.15"),
            hold_band_release_min_roi=Decimal("0"), conviction_stop_loss_multiplier=Decimal("1.75"),
            conviction_extra_confirmations=1, hold_band_requires_locked=True,
        )
    )
    snapshot = MarketSnapshot(
        instrument_id="x", phase="ACTIVE", time_left_sec=60,
        best_bid=Decimal("0.70"), best_ask=Decimal("0.71"), fee_rate=Decimal("0"),
        spread=Decimal("0.01"), spread_pct=Decimal("0.014"), slippage_buffer_pct=Decimal("0"),
        exit_stage="PASSIVE", in_reduce_only_tail=False, stop_loss_disabled_in_tail=False,
        fair=Decimal("0.72"), fair_edge_ps=Decimal("0.02"), spot_minus_strike_bps=Decimal("10"),
    )
    assert engine._effective_trailing_drawdown(Decimal("0.18"), snapshot) == Decimal("0.0625")
