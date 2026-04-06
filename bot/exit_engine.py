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
    stop_loss_hold_on_none_signal: bool
    conviction_band_min_price: Decimal
    hold_band_min_price: Decimal
    conviction_band_min_score_abs: Decimal
    hold_band_min_score_abs: Decimal
    hold_band_release_min_roi: Decimal
    conviction_stop_loss_multiplier: Decimal
    conviction_extra_confirmations: int
    hold_band_requires_locked: bool
    early_profit_hold_enabled: bool = True
    early_profit_hold_min_hold_sec: int = 60
    early_profit_hold_max_profit_ps: Decimal = Decimal("0.08")
    profit_run_enabled: bool = True
    profit_run_min_hold_sec: int = 20
    profit_run_min_profit_ps: Decimal = Decimal("0.04")
    profit_run_min_score_abs: Decimal = Decimal("0.12")
    profit_run_trailing_drawdown_ps: Decimal = Decimal("0.05")
    profit_run_unlock_profit_ps: Decimal = Decimal("0.18")
    profit_run_unlock_trailing_drawdown_ps: Decimal = Decimal("0.02")
    winner_continuation_min_fair_edge_ps: Decimal = Decimal("0.04")


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

    @staticmethod
    def _spot_strike_supports(snapshot: MarketSnapshot, position: PositionState) -> bool:
        if snapshot.spot_minus_strike_bps is None:
            return True
        held_side = str(position.held_side or "NONE").upper()
        if held_side == "UP":
            return snapshot.spot_minus_strike_bps > 0
        if held_side == "DOWN":
            return snapshot.spot_minus_strike_bps < 0
        return True

    def _classify_profitable_exit_intent(
        self,
        *,
        snapshot: MarketSnapshot,
        position: PositionState,
        signal: SignalDecision,
        thesis_weakened: bool,
        offside_confirmed: bool,
    ) -> tuple[str, str, dict[str, str]]:
        if position.avg_entry_price <= 0 or snapshot.best_bid <= position.avg_entry_price:
            return "neutral", "", {}
        if snapshot.fair is None or snapshot.fair <= 0:
            return "neutral", "", {}
        if snapshot.exit_stage.value != "PASSIVE":
            return "de_risk", "profitable_non_passive_stage", {"exit_intent": "de_risk"}

        peak_bid = position.peak_bid if position.peak_bid is not None else snapshot.best_bid
        peak_fair = position.peak_fair if position.peak_fair is not None else (snapshot.fair or snapshot.best_bid)
        peak_profit_ps = max(peak_bid - position.avg_entry_price, peak_fair - position.avg_entry_price)
        fair_edge_ps = (
            snapshot.fair_edge_ps
            if snapshot.fair_edge_ps is not None
            else max(Decimal("0"), (snapshot.fair or snapshot.best_bid) - snapshot.best_bid)
        )
        spot_strike_supports = self._spot_strike_supports(snapshot, position)
        score_abs = abs(signal.score)
        shared_meta = {
            "exit_intent": "de_risk",
            "fair_edge_ps": str(fair_edge_ps),
            "peak_profit_ps": str(peak_profit_ps),
            "spot_strike_supports": "1" if spot_strike_supports else "0",
        }
        if offside_confirmed or thesis_weakened or not signal.matches_position:
            return "de_risk", "profitable_thesis_weakened", shared_meta
        if (
            self.config.early_profit_hold_enabled
            and position.hold_sec < float(self.config.early_profit_hold_min_hold_sec)
            and peak_profit_ps < self.config.early_profit_hold_max_profit_ps
        ):
            return (
                "continue",
                "unified_early_profit_hold",
                {
                    **shared_meta,
                    "exit_intent": "continue",
                },
            )
        if (
            signal.locked
            and score_abs >= self.config.hold_band_min_score_abs
            and fair_edge_ps >= self.config.winner_continuation_min_fair_edge_ps
            and spot_strike_supports
        ):
            return (
                "continue",
                "unified_winner_continuation",
                {
                    **shared_meta,
                    "exit_intent": "continue",
                },
            )
        if not self.config.profit_run_enabled:
            return "de_risk", "profitable_no_profit_run", shared_meta
        if score_abs < self.config.profit_run_min_score_abs:
            return "de_risk", "profitable_score_softening", shared_meta
        if peak_profit_ps < self.config.profit_run_min_profit_ps:
            return "de_risk", "profitable_below_profit_run_floor", shared_meta

        unlock_active = (
            self.config.profit_run_unlock_profit_ps > 0
            and peak_profit_ps >= self.config.profit_run_unlock_profit_ps
        )
        trailing_drawdown_ps = self.config.profit_run_trailing_drawdown_ps
        if unlock_active and self.config.profit_run_unlock_trailing_drawdown_ps > 0:
            trailing_drawdown_ps = min(
                trailing_drawdown_ps,
                self.config.profit_run_unlock_trailing_drawdown_ps,
            )
        fair_now = snapshot.fair if snapshot.fair is not None else peak_fair
        drawdown_bid = max(Decimal("0"), peak_bid - snapshot.best_bid)
        drawdown_fair = max(Decimal("0"), peak_fair - fair_now)
        if position.hold_sec < float(self.config.profit_run_min_hold_sec) and not unlock_active:
            return (
                "continue",
                "unified_profit_run_min_hold",
                {
                    **shared_meta,
                    "exit_intent": "continue",
                    "drawdown_bid": str(drawdown_bid),
                    "drawdown_fair": str(drawdown_fair),
                },
            )
        if drawdown_bid < trailing_drawdown_ps and drawdown_fair < trailing_drawdown_ps:
            return (
                "continue",
                "unified_profit_run_trailing",
                {
                    **shared_meta,
                    "exit_intent": "continue",
                    "drawdown_bid": str(drawdown_bid),
                    "drawdown_fair": str(drawdown_fair),
                },
            )
        return (
            "de_risk",
            "profitable_drawdown_break",
            {
                **shared_meta,
                "drawdown_bid": str(drawdown_bid),
                "drawdown_fair": str(drawdown_fair),
            },
        )

    def evaluate(self, snapshot: MarketSnapshot, position: PositionState, signal: SignalDecision) -> ExitDecision:
        exit_px_effective = snapshot.best_bid * (Decimal("1") - snapshot.slippage_buffer_pct)
        gross_if_exit = position.qty * (exit_px_effective - position.avg_entry_price)
        exit_fee_est = estimate_taker_fee_usdc(
            shares=position.qty,
            probability=exit_px_effective,
        )
        net_if_exit = gross_if_exit - position.entry_fee_remaining - exit_fee_est
        entry_cost_usdc = (position.qty * position.avg_entry_price) + position.entry_fee_remaining
        net_exit_roi = (
            net_if_exit / entry_cost_usdc
            if entry_cost_usdc > 0
            else Decimal("0")
        )
        band = self._classify_band(snapshot, signal)
        hold_band_released = (
            band == "hold"
            and self.config.hold_band_release_min_roi > 0
            and net_exit_roi >= self.config.hold_band_release_min_roi
        )
        base_metadata = {
            "band": band,
            "signal_matches_position": "1" if signal.matches_position else "0",
            "signal_score": str(signal.score),
            "signal_locked": "1" if signal.locked else "0",
            "entry_cost_usdc": str(entry_cost_usdc),
            "net_exit_roi": str(net_exit_roi),
            "hold_band_released": "1" if hold_band_released else "0",
            "hold_band_release_min_roi": str(self.config.hold_band_release_min_roi),
        }
        signal_side = str(signal.active_side).upper()
        signal_is_none = signal_side == "NONE"
        explicit_offside = (not signal.matches_position) and not signal_is_none
        price_adverse = (
            position.avg_entry_price > 0
            and snapshot.best_bid < position.avg_entry_price
            and gross_if_exit < 0
        )
        thesis_weakened = explicit_offside or (
            not signal_is_none and abs(signal.score) < self.config.stop_loss_thesis_min_score_abs
        )
        if signal_is_none and price_adverse:
            thesis_weakened = True

        profitable_intent, profitable_reason, profitable_meta = self._classify_profitable_exit_intent(
            snapshot=snapshot,
            position=position,
            signal=signal,
            thesis_weakened=thesis_weakened,
            offside_confirmed=explicit_offside,
        )

        if self.config.stop_loss_hold_on_none_signal and signal_is_none and not price_adverse:
            return ExitDecision(
                decision_type=ExitDecisionType.NONE,
                reason="signal_none_hold",
                net_if_exit=net_if_exit,
                gross_if_exit=gross_if_exit,
                exit_fee_est=exit_fee_est,
                exit_px_effective=exit_px_effective,
                metadata={
                    **base_metadata,
                    "signal_is_none": "1",
                    "thesis_weakened": "0",
                },
            )

        if profitable_intent == "continue":
            return ExitDecision(
                decision_type=ExitDecisionType.HOLD_IN_BAND,
                reason=profitable_reason,
                net_if_exit=net_if_exit,
                gross_if_exit=gross_if_exit,
                exit_fee_est=exit_fee_est,
                exit_px_effective=exit_px_effective,
                metadata={
                    **base_metadata,
                    **profitable_meta,
                },
            )

        if profitable_intent == "de_risk":
            return ExitDecision(
                decision_type=ExitDecisionType.DE_RISK,
                reason=profitable_reason,
                net_if_exit=net_if_exit,
                gross_if_exit=gross_if_exit,
                exit_fee_est=exit_fee_est,
                exit_px_effective=exit_px_effective,
                metadata={
                    **base_metadata,
                    **profitable_meta,
                },
            )

        if band == "hold" and not hold_band_released:
            return ExitDecision(
                decision_type=ExitDecisionType.HOLD_IN_BAND,
                reason="hold_band_in_thesis",
                net_if_exit=net_if_exit,
                gross_if_exit=gross_if_exit,
                exit_fee_est=exit_fee_est,
                exit_px_effective=exit_px_effective,
                metadata=base_metadata,
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
                    "signal_is_none": "1" if signal_is_none else "0",
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
                    "signal_is_none": "1" if signal_is_none else "0",
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
                "signal_is_none": "1" if signal_is_none else "0",
                "thesis_weakened": "1" if thesis_weakened else "0",
                "stop_loss_threshold": str(stop_loss_threshold),
                "required_confirmations": str(required_confirmations),
            },
        )
