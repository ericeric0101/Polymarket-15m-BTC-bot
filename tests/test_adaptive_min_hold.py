from decimal import Decimal

from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.models import MarketSnapshot


def _engine() -> ExitPolicyEngine:
    return ExitPolicyEngine(ExitEngineConfig(
        hold_to_redeem_enabled=False, min_hold_sec=60,
        stop_loss_usdc=Decimal("0.5"), stop_loss_confirmations=2,
        stop_loss_requires_thesis_weakening=True, stop_loss_thesis_min_score_abs=Decimal("0.18"),
        stop_loss_hold_on_none_signal=True, conviction_band_min_price=Decimal("0.6"),
        hold_band_min_price=Decimal("0.68"), conviction_band_min_score_abs=Decimal("0.15"),
        hold_band_min_score_abs=Decimal("0.15"), hold_band_release_min_roi=Decimal("0"),
        conviction_stop_loss_multiplier=Decimal("1.75"), conviction_extra_confirmations=1,
        hold_band_requires_locked=True,
    ))


def test_adaptive_minimum_hold_has_floor_and_relaxes_as_settlement_approaches():
    engine = _engine()
    assert engine._effective_min_hold_sec(900) == 60.0
    assert engine._effective_min_hold_sec(300) == 30.0
    assert engine._effective_min_hold_sec(10) == 30.0
