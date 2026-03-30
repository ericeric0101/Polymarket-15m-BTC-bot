from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bot.models import ExitDecision, ExitDecisionType, MarketSnapshot, PositionState, SignalDecision
from execution.rebate_model import estimate_taker_fee_usdc


@dataclass(frozen=True)
class ExitEngineConfig:
    min_hold_sec: int
    stop_loss_usdc: Decimal
    stop_loss_confirmations: int
    stop_loss_requires_thesis_weakening: bool
    stop_loss_thesis_min_score_abs: Decimal
    conviction_band_min_price: Decimal
    hold_band_min_price: Decimal
    conviction_band_min_score_abs: Decimal
    hold_band_min_score_abs: Decimal
    conviction_stop_loss_multiplier: Decimal
    conviction_extra_confirmations: int
    hold_band_requires_locked: bool


class ExitPolicyEngine:
    def __init__(self, config: ExitEngineConfig, **kwargs) -> None:
        self.config = config

    def _classify_band(self, snapshot: MarketSnapshot, signal: SignalDecision) -> str:
        score_abs = abs(signal.score)
        if not signal.matches_position:
            return "neutral"
        if self.config.hold_band_requires_locked and not signal.locked:
            return "neutral"
        if snapshot.best_bid >= self.config.hold_band_min_price and score_abs >= self.config.hold_band_min_score_abs:
            return "hold"
        if snapshot.best_bid >= self.config.conviction_band_min_price and score_abs >= self.config.conviction_band_min_score_abs:
            return "conviction"
        return "neutral"

    def evaluate(self, snapshot: MarketSnapshot, position: PositionState, signal: SignalDecision) -> ExitDecision:
        exit_px_effective = snapshot.best_bid * (Decimal("1") - snapshot.slippage_buffer_pct)
        gross_if_exit = position.qty * (exit_px_effective - position.avg_entry_price)
        exit_fee_est = estimate_taker_fee_usdc(
            shares=position.qty,
            probability=exit_px_effective,
        )
        net_if_exit = gross_if_exit - position.entry_fee_remaining - exit_fee_est
        band = self._classify_band(snapshot, signal)
        base_metadata = {
            "band": band,
            "signal_matches_position": "1" if signal.matches_position else "0",
            "signal_score": str(signal.score),
            "signal_locked": "1" if signal.locked else "0",
        }
        thesis_weakened = (not signal.matches_position) or (
            abs(signal.score) < self.config.stop_loss_thesis_min_score_abs
        )

        if band == "hold":
            return ExitDecision(
                decision_type=ExitDecisionType.HOLD_IN_BAND,
                reason="hold_band_in_thesis",
                net_if_exit=net_if_exit,
                gross_if_exit=gross_if_exit,
                exit_fee_est=exit_fee_est,
                exit_px_effective=exit_px_effective,
                metadata=base_metadata,
            )

        price_adverse = (
            position.avg_entry_price > 0
            and snapshot.best_bid < position.avg_entry_price
            and gross_if_exit < 0
        )
        stop_loss_threshold = abs(self.config.stop_loss_usdc)
        required_confirmations = self.config.stop_loss_confirmations
        if band == "conviction":
            stop_loss_threshold *= max(Decimal("1"), self.config.conviction_stop_loss_multiplier)
            required_confirmations += max(0, self.config.conviction_extra_confirmations)
        stop_loss_candidate = (
            not snapshot.stop_loss_disabled_in_tail
            and position.hold_sec >= max(0, self.config.min_hold_sec)
            and price_adverse
            and net_if_exit <= -stop_loss_threshold
            and (
                not self.config.stop_loss_requires_thesis_weakening
                or thesis_weakened
            )
        )
        if not stop_loss_candidate:
            return ExitDecision(
                decision_type=ExitDecisionType.NONE,
                reason="thesis_still_supported" if price_adverse and not thesis_weakened else "no_exit_signal",
                net_if_exit=net_if_exit,
                gross_if_exit=gross_if_exit,
                exit_fee_est=exit_fee_est,
                exit_px_effective=exit_px_effective,
                metadata={
                    **base_metadata,
                    "thesis_weakened": "1" if thesis_weakened else "0",
                    "stop_loss_threshold": str(stop_loss_threshold),
                    "required_confirmations": str(required_confirmations),
                },
            )

        confirm_hits = position.stop_loss_confirm_hits + 1
        if confirm_hits < required_confirmations:
            return ExitDecision(
                decision_type=ExitDecisionType.STOP_LOSS_PENDING_CONFIRMATION,
                reason="stop_loss_confirming",
                net_if_exit=net_if_exit,
                gross_if_exit=gross_if_exit,
                exit_fee_est=exit_fee_est,
                exit_px_effective=exit_px_effective,
                confirm_hits=confirm_hits,
                metadata={
                    **base_metadata,
                    "thesis_weakened": "1" if thesis_weakened else "0",
                    "stop_loss_threshold": str(stop_loss_threshold),
                    "required_confirmations": str(required_confirmations),
                },
            )

        return ExitDecision(
            decision_type=ExitDecisionType.TAKER_STOP_LOSS,
            reason="stop_loss_confirmed",
            net_if_exit=net_if_exit,
            gross_if_exit=gross_if_exit,
            exit_fee_est=exit_fee_est,
            exit_px_effective=exit_px_effective,
            confirm_hits=confirm_hits,
            metadata={
                **base_metadata,
                "thesis_weakened": "1" if thesis_weakened else "0",
                "stop_loss_threshold": str(stop_loss_threshold),
                "required_confirmations": str(required_confirmations),
            },
        )
