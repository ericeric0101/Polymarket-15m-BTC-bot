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
    catastrophic_stop_loss_enabled: bool = True
    catastrophic_stop_loss_usdc: Decimal = Decimal("0.40")
    catastrophic_stop_loss_min_score_abs: Decimal = Decimal("0.50")
    catastrophic_stop_loss_confirmations: int = 2


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
        if signal.locked and spot_strike_supports:
            return (
                "continue",
                "unified_winner_continuation",
                {
                    **shared_meta,
                    "exit_intent": "continue",
                },
            )
        return (
            "continue",
            "profitable_hold_simple",
            {
                **shared_meta,
                "exit_intent": "continue",
                "score_abs": str(score_abs),
                "peak_profit_ps": str(peak_profit_ps),
            },
        )

    def _held_side_strong_opposite(
        self,
        *,
        position: PositionState,
        signal: SignalDecision,
        min_score_abs: Decimal,
    ) -> bool:
        held_side = str(position.held_side or "NONE").upper()
        if held_side == "UP":
            return signal.score <= -abs(min_score_abs)
        if held_side == "DOWN":
            return signal.score >= abs(min_score_abs)
        return False

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        position: PositionState,
        signal: SignalDecision,
        *,
        external_thesis_weakened: bool | None = None,
        external_offside_confirmed: bool | None = None,
    ) -> ExitDecision:
        """Evaluate exit decision for a held position.

        Parameters
        ----------
        external_thesis_weakened : bool | None
            When provided, overrides the internal instant thesis-weakened
            check with the strategy-layer multi-confirmation result.
            This ensures the ExitPolicyEngine is synchronized with the
            strategy-level ``_assess_thesis_weakened`` + ``confirmed_adverse_exit``
            pipeline, which uses higher score thresholds and requires consecutive
            confirmations before declaring the thesis broken.
        external_offside_confirmed : bool | None
            When provided, overrides the internal offside check.
        """
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
        strong_opposite = explicit_offside and abs(signal.score) >= self.config.stop_loss_thesis_min_score_abs
        price_adverse = (
            position.avg_entry_price > 0
            and snapshot.best_bid < position.avg_entry_price
            and gross_if_exit < 0
        )
        # Internal instant computation (used as fallback and for logging).
        _instant_thesis_weakened = strong_opposite
        if signal_is_none and price_adverse:
            _instant_thesis_weakened = True
        # Prefer the externally-confirmed thesis state when available:
        # the strategy layer applies multi-confirmation + higher score
        # thresholds, so its signal is more reliable.
        thesis_weakened = (
            external_thesis_weakened
            if external_thesis_weakened is not None
            else _instant_thesis_weakened
        )
        offside_confirmed = (
            external_offside_confirmed
            if external_offside_confirmed is not None
            else strong_opposite
        )

        profitable_intent, profitable_reason, profitable_meta = self._classify_profitable_exit_intent(
            snapshot=snapshot,
            position=position,
            signal=signal,
            thesis_weakened=thesis_weakened,
            offside_confirmed=offside_confirmed,
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
        catastrophic_stop_loss_candidate = (
            self.config.catastrophic_stop_loss_enabled
            and not snapshot.stop_loss_disabled_in_tail
            and position.hold_sec >= max(0, self.config.min_hold_sec)
            and price_adverse
            and net_if_exit <= -abs(self.config.catastrophic_stop_loss_usdc)
            and self._held_side_strong_opposite(
                position=position,
                signal=signal,
                min_score_abs=self.config.catastrophic_stop_loss_min_score_abs,
            )
        )
        if catastrophic_stop_loss_candidate:
            stop_loss_threshold = abs(self.config.catastrophic_stop_loss_usdc)
            required_confirmations = max(1, self.config.catastrophic_stop_loss_confirmations)
        stop_loss_candidate = (
            catastrophic_stop_loss_candidate
            or (
                not snapshot.stop_loss_disabled_in_tail
                and position.hold_sec >= max(0, self.config.min_hold_sec)
                and price_adverse
                and net_if_exit <= -stop_loss_threshold
                and (
                    not self.config.stop_loss_requires_thesis_weakening
                    or thesis_weakened
                )
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
                    "catastrophic_stop_loss_candidate": "1" if catastrophic_stop_loss_candidate else "0",
                    "stop_loss_threshold": str(stop_loss_threshold),
                    "required_confirmations": str(required_confirmations),
                },
            )

        confirm_hits = position.stop_loss_confirm_hits + 1
        pending_reason = (
            "catastrophic_stop_loss_confirming"
            if catastrophic_stop_loss_candidate
            else "stop_loss_confirming"
        )
        if confirm_hits < required_confirmations:
            return ExitDecision(
                decision_type=ExitDecisionType.STOP_LOSS_PENDING_CONFIRMATION,
                reason=pending_reason,
                net_if_exit=net_if_exit,
                gross_if_exit=gross_if_exit,
                exit_fee_est=exit_fee_est,
                exit_px_effective=exit_px_effective,
                confirm_hits=confirm_hits,
                metadata={
                    **base_metadata,
                    "signal_is_none": "1" if signal_is_none else "0",
                    "thesis_weakened": "1" if thesis_weakened else "0",
                    "catastrophic_stop_loss_candidate": "1" if catastrophic_stop_loss_candidate else "0",
                    "stop_loss_threshold": str(stop_loss_threshold),
                    "required_confirmations": str(required_confirmations),
                },
            )

        confirmed_reason = (
            "catastrophic_stop_loss_confirmed"
            if catastrophic_stop_loss_candidate
            else "stop_loss_confirmed"
        )
        return ExitDecision(
            decision_type=ExitDecisionType.TAKER_STOP_LOSS,
            reason=confirmed_reason,
            net_if_exit=net_if_exit,
            gross_if_exit=gross_if_exit,
            exit_fee_est=exit_fee_est,
            exit_px_effective=exit_px_effective,
            confirm_hits=confirm_hits,
            metadata={
                **base_metadata,
                "signal_is_none": "1" if signal_is_none else "0",
                "thesis_weakened": "1" if thesis_weakened else "0",
                "catastrophic_stop_loss_candidate": "1" if catastrophic_stop_loss_candidate else "0",
                "stop_loss_threshold": str(stop_loss_threshold),
                "required_confirmations": str(required_confirmations),
            },
        )
