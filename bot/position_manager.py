from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional, Tuple

from bot.models import DecisionPhase, DecisionRegime, DecisionState


# ---------------------------------------------------------------------------
# Pressure model normalisation constants
# ---------------------------------------------------------------------------
_SPOT_PRESSURE_NORM_BPS = Decimal("50")       # BPS range to normalise spot-vs-strike to [-1, +1]
_EDGE_NORM_PS = Decimal("0.08")               # Fair-vs-executable gap normalisation
_PERSISTENCE_NORM = Decimal("0.60")            # Score normalisation for persistence component
_SPREAD_NORM_MULT = Decimal("8")              # Spread-pct multiplier for book_pressure
_DRAWDOWN_NORM_PS = Decimal("0.12")           # Peak-to-bid drawdown normalisation
_PNL_NORM_PS = Decimal("0.10")                # Bid-vs-entry PnL normalisation
_HARD_PRESSURE_EXIT_THRESHOLD = Decimal("-0.45")   # EMA threshold for forced EXIT phase
_SPOT_PRESSURE_EXIT_THRESHOLD = Decimal("-0.20")   # Spot pressure threshold for confirming EXIT


class PositionLifecycle(str, Enum):
    FLAT = "flat"
    ENTRY_PROTECTED = "entry_protected"
    LONG = "long"
    EXIT_ARMED = "exit_armed"


@dataclass
class PositionRuntimeState:
    lifecycle: PositionLifecycle = PositionLifecycle.FLAT
    thesis_side: str = "NONE"
    entry_mode: str = "value"
    entered_ts: float = 0.0
    entry_protection_until_ts: float = 0.0
    adverse_since_ts: float = 0.0
    adverse_hits: int = 0
    recycle_hold_until_ts: float = 0.0
    last_spot_sign: int = 0
    recent_flip_count: int = 0
    pressure_ema: Decimal = Decimal("0")
    regime: str = DecisionRegime.CHOP.value
    phase: str = DecisionPhase.FLAT.value
    last_state_update_ts: float = 0.0


@dataclass(frozen=True)
class PositionManagerConfig:
    early_profit_hold_enabled: bool
    early_profit_hold_min_hold_sec: int
    early_profit_hold_max_profit_ps: Decimal
    early_profit_hold_min_score_abs: Decimal
    profit_run_enabled: bool
    profit_run_min_hold_sec: int
    profit_run_min_profit_ps: Decimal
    profit_run_min_score_abs: Decimal
    profit_run_trailing_drawdown_ps: Decimal
    profit_run_unlock_profit_ps: Decimal
    profit_run_unlock_trailing_drawdown_ps: Decimal
    stop_loss_entry_protection_sec: int
    continuation_entry_protection_sec: int
    stop_loss_regime_min_sec: int
    stop_loss_regime_confirmations: int
    stop_loss_min_opposite_score_abs: Decimal
    recycle_locked_side_min_fair_edge_ps: Decimal = Decimal("0")


@dataclass(frozen=True)
class StopLossRegimeDecision:
    status: str
    reason: str
    adverse_sec: float = 0.0
    adverse_hits: int = 0


class PositionManager:
    def __init__(self, config: PositionManagerConfig) -> None:
        self.config = config
        self.states: Dict[str, PositionRuntimeState] = {}

    @staticmethod
    def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
        return max(low, min(high, value))

    def clear_all(self) -> None:
        self.states.clear()

    def sync_position(
        self,
        *,
        inst_key: str,
        qty: Decimal,
        opened_ts: float,
        thesis_side: str,
        now_ts: Optional[float] = None,
    ) -> PositionRuntimeState:
        now_ts = now_ts or time.time()
        if qty <= 0:
            self.states.pop(inst_key, None)
            return PositionRuntimeState()
        state = self.states.get(inst_key)
        if state is None:
            entered_ts = opened_ts if opened_ts > 0 else now_ts
            entry_protection_until_ts = entered_ts + float(self.config.stop_loss_entry_protection_sec)
            lifecycle = (
                PositionLifecycle.ENTRY_PROTECTED
                if now_ts < entry_protection_until_ts
                else PositionLifecycle.LONG
            )
            state = PositionRuntimeState(
                lifecycle=lifecycle,
                thesis_side=str(thesis_side or "NONE").upper(),
                entry_mode="value",
                entered_ts=entered_ts,
                entry_protection_until_ts=entry_protection_until_ts,
            )
            self.states[inst_key] = state
            return state
        if opened_ts > 0 and state.entered_ts <= 0:
            state.entered_ts = opened_ts
        if state.entry_protection_until_ts <= 0 and state.entered_ts > 0:
            state.entry_protection_until_ts = state.entered_ts + float(self.config.stop_loss_entry_protection_sec)
        if thesis_side:
            state.thesis_side = str(thesis_side).upper()
        if state.entry_protection_until_ts > 0 and now_ts >= state.entry_protection_until_ts:
            if state.lifecycle == PositionLifecycle.ENTRY_PROTECTED:
                state.lifecycle = PositionLifecycle.LONG
        return state

    def on_fill(
        self,
        *,
        inst_key: str,
        side: str,
        remaining_qty: Decimal,
        thesis_side: str,
        entry_mode: str = "value",
        now_ts: Optional[float] = None,
    ) -> None:
        now_ts = now_ts or time.time()
        side_norm = str(side or "").lower()
        if side_norm == "buy":
            normalized_entry_mode = str(entry_mode or "value").lower()
            protection_sec = (
                self.config.continuation_entry_protection_sec
                if normalized_entry_mode == "continuation"
                else self.config.stop_loss_entry_protection_sec
            )
            self.states[inst_key] = PositionRuntimeState(
                lifecycle=PositionLifecycle.ENTRY_PROTECTED,
                thesis_side=str(thesis_side or "NONE").upper(),
                entry_mode=normalized_entry_mode,
                entered_ts=now_ts,
                entry_protection_until_ts=now_ts + float(protection_sec),
                recycle_hold_until_ts=0.0,
            )
            return
        if remaining_qty <= 0:
            self.states.pop(inst_key, None)
            return
        state = self.states.get(inst_key)
        if state is None:
            state = PositionRuntimeState(
                lifecycle=PositionLifecycle.LONG,
                thesis_side=str(thesis_side or "NONE").upper(),
                entry_mode=str(entry_mode or "value").lower(),
                entered_ts=now_ts,
                entry_protection_until_ts=now_ts,
                recycle_hold_until_ts=0.0,
            )
            self.states[inst_key] = state
        else:
            state.thesis_side = str(thesis_side or state.thesis_side or "NONE").upper()
            state.adverse_since_ts = 0.0
            state.adverse_hits = 0
            if state.lifecycle == PositionLifecycle.EXIT_ARMED:
                state.lifecycle = PositionLifecycle.LONG
            if remaining_qty <= 0:
                state.recycle_hold_until_ts = 0.0

    def should_hold_profitable_position(
        self,
        *,
        inst_key: str,
        qty: Decimal,
        best_bid: Decimal,
        fair: Optional[Decimal],
        avg_entry: Decimal,
        active_side_locked: bool,
        active_side: str,
        instrument_matches_active_side: bool,
        side_decision_score: Decimal,
        exit_stage_value: str,
        thesis_weakened: bool,
        offside_confirmed: bool,
        opened_ts: float,
        peak_bid: Decimal,
        peak_fair: Decimal,
        now_ts: Optional[float] = None,
    ) -> Tuple[bool, str]:
        if not self.config.profit_run_enabled:
            return False, ""
        if not inst_key or qty <= 0 or avg_entry <= 0 or best_bid <= 0:
            return False, ""
        if offside_confirmed or thesis_weakened:
            return False, ""
        if not active_side_locked or str(active_side or "NONE").upper() == "NONE":
            return False, ""
        if not instrument_matches_active_side:
            return False, ""
        if exit_stage_value != "PASSIVE":
            return False, ""

        now_ts = now_ts or time.time()
        state = self.sync_position(
            inst_key=inst_key,
            qty=qty,
            opened_ts=opened_ts,
            thesis_side=active_side,
            now_ts=now_ts,
        )
        if state.recycle_hold_until_ts > now_ts:
            return True, (
                f"recycle_hold_latch remaining={state.recycle_hold_until_ts - now_ts:.1f}s"
            )
        hold_sec = max(0.0, now_ts - opened_ts) if opened_ts > 0 else 0.0
        peak_profit_ps = max(peak_bid - avg_entry, peak_fair - avg_entry)
        if (
            self.config.early_profit_hold_enabled
            and hold_sec < float(self.config.early_profit_hold_min_hold_sec)
            and peak_profit_ps < self.config.early_profit_hold_max_profit_ps
        ):
            return True, (
                f"early_profit_hold hold={hold_sec:.1f}s<{self.config.early_profit_hold_min_hold_sec}s "
                f"peak_profit={float(peak_profit_ps):.4f}<{float(self.config.early_profit_hold_max_profit_ps):.4f}"
            )
        fair_edge_ps = Decimal("0")
        if fair is not None and fair > 0:
            fair_edge_ps = max(Decimal("0"), fair - best_bid)
        if (
            self.is_recycle_locked_side_hold_candidate(
                best_bid=best_bid,
                fair=fair,
                avg_entry=avg_entry,
            )
        ):
            state.recycle_hold_until_ts = max(
                state.recycle_hold_until_ts,
                now_ts + float(self.config.profit_run_min_hold_sec),
            )
            return True, (
                f"recycle_locked_side_hold fair_edge={float(fair_edge_ps):.4f}"
                f">={float(self.config.recycle_locked_side_min_fair_edge_ps):.4f} "
                f"best_bid={float(best_bid):.4f} fair={float(fair or best_bid):.4f}"
            )
        if abs(side_decision_score) < self.config.profit_run_min_score_abs:
            return False, ""
        if peak_profit_ps < self.config.profit_run_min_profit_ps:
            return False, ""

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
        fair_now = fair if fair is not None else peak_fair
        drawdown_bid = max(Decimal("0"), peak_bid - best_bid)
        drawdown_fair = max(Decimal("0"), peak_fair - fair_now)
        if hold_sec < float(self.config.profit_run_min_hold_sec) and not unlock_active:
            return True, (
                f"profit_run_hold hold={hold_sec:.1f}s<{self.config.profit_run_min_hold_sec}s "
                f"peak_profit={float(peak_profit_ps):.4f}"
            )
        if drawdown_bid < trailing_drawdown_ps and drawdown_fair < trailing_drawdown_ps:
            reason_prefix = "profit_run_hold_unlocked" if unlock_active else "profit_run_hold"
            return True, (
                f"{reason_prefix} drawdown_bid={float(drawdown_bid):.4f} "
                f"drawdown_fair={float(drawdown_fair):.4f} "
                f"< trail={float(trailing_drawdown_ps):.4f} "
                f"peak_profit={float(peak_profit_ps):.4f}"
            )
        return False, ""

    def is_recycle_locked_side_hold_candidate(
        self,
        *,
        best_bid: Decimal,
        fair: Optional[Decimal],
        avg_entry: Decimal,
    ) -> bool:
        if self.config.recycle_locked_side_min_fair_edge_ps <= 0:
            return False
        if avg_entry <= 0 or best_bid <= avg_entry:
            return False
        if fair is None or fair <= 0:
            return False
        fair_edge_ps = max(Decimal("0"), fair - best_bid)
        return fair_edge_ps >= self.config.recycle_locked_side_min_fair_edge_ps

    def compute_decision_state(
        self,
        *,
        inst_key: str,
        now_ts: float,
        qty: Decimal,
        opened_ts: float,
        held_side: str,
        active_side: str,
        signal_score: Decimal,
        signal_matches_position: bool,
        current_price: Optional[Decimal],
        price_to_beat: Optional[Decimal],
        best_bid: Decimal,
        best_ask: Decimal,
        fair: Optional[Decimal],
        time_left_sec: Optional[float],
        avg_entry: Decimal,
        peak_bid: Optional[Decimal] = None,
        peak_fair: Optional[Decimal] = None,
        entry_protection_sec: Optional[float] = None,
    ) -> DecisionState:
        state = self.sync_position(
            inst_key=inst_key,
            qty=qty,
            opened_ts=opened_ts,
            thesis_side=held_side if qty > 0 else active_side,
            now_ts=now_ts,
        )
        side = str(held_side if qty > 0 else active_side or "NONE").upper()
        signed_score = Decimal("0")
        if side == "UP":
            signed_score = signal_score
        elif side == "DOWN":
            signed_score = -signal_score

        spot_minus_strike_bps: Optional[Decimal] = None
        if (
            current_price is not None
            and current_price > 0
            and price_to_beat is not None
            and price_to_beat > 0
        ):
            try:
                spot_minus_strike_bps = ((current_price / price_to_beat) - Decimal("1")) * Decimal("10000")
            except Exception:
                spot_minus_strike_bps = None

        spot_sign = 0
        if spot_minus_strike_bps is not None:
            if spot_minus_strike_bps > Decimal("8"):
                spot_sign = 1
            elif spot_minus_strike_bps < Decimal("-8"):
                spot_sign = -1
        if state.last_state_update_ts > 0 and now_ts - state.last_state_update_ts > 20.0:
            state.recent_flip_count = max(0, state.recent_flip_count - 1)
        if spot_sign != 0:
            if (
                state.last_spot_sign != 0
                and spot_sign != state.last_spot_sign
                and (state.last_state_update_ts <= 0 or now_ts - state.last_state_update_ts <= 45.0)
            ):
                state.recent_flip_count += 1
            state.last_spot_sign = spot_sign
            state.last_state_update_ts = now_ts

        spot_pressure = Decimal("0")
        if side == "UP" and spot_minus_strike_bps is not None:
            spot_pressure = self._clamp(spot_minus_strike_bps / _SPOT_PRESSURE_NORM_BPS, Decimal("-1"), Decimal("1"))
        elif side == "DOWN" and spot_minus_strike_bps is not None:
            spot_pressure = self._clamp((-spot_minus_strike_bps) / _SPOT_PRESSURE_NORM_BPS, Decimal("-1"), Decimal("1"))

        executable_price = best_ask if qty <= 0 else best_bid
        edge = Decimal("0")
        if fair is not None and fair > 0 and executable_price > 0:
            edge = self._clamp((fair - executable_price) / _EDGE_NORM_PS, Decimal("-1"), Decimal("1"))

        spread = max(Decimal("0"), best_ask - best_bid)
        mid = (best_bid + best_ask) / Decimal("2") if (best_bid + best_ask) > 0 else Decimal("0")
        spread_pct = (spread / mid) if mid > 0 else Decimal("0")
        book_pressure = self._clamp(Decimal("0.20") - (spread_pct * _SPREAD_NORM_MULT), Decimal("-0.5"), Decimal("0.2"))

        drawdown_pressure = Decimal("0")
        peak_ref = best_bid
        if peak_bid is not None:
            peak_ref = max(peak_ref, peak_bid)
        if qty > 0 and avg_entry > 0:
            if peak_ref > best_bid:
                drawdown_pressure -= self._clamp((peak_ref - best_bid) / _DRAWDOWN_NORM_PS, Decimal("0"), Decimal("1"))
            drawdown_pressure += self._clamp((best_bid - avg_entry) / _PNL_NORM_PS, Decimal("-1"), Decimal("0.5"))

        time_pressure = Decimal("0")
        if time_left_sec is not None and time_left_sec > 0:
            if qty <= 0 and time_left_sec < 240.0:
                time_pressure -= self._clamp(
                    Decimal(str((240.0 - time_left_sec) / 240.0)) * Decimal("0.35"),
                    Decimal("0"),
                    Decimal("0.35"),
                )
            elif qty > 0 and time_left_sec < 180.0:
                time_pressure -= self._clamp(
                    Decimal(str((180.0 - time_left_sec) / 180.0)) * Decimal("0.45"),
                    Decimal("0"),
                    Decimal("0.45"),
                )

        persistence = self._clamp(signed_score / _PERSISTENCE_NORM, Decimal("-1"), Decimal("1"))
        instant_pressure = self._clamp(
            (spot_pressure * Decimal("0.40"))
            + (edge * Decimal("0.25"))
            + (persistence * Decimal("0.25"))
            + book_pressure
            + drawdown_pressure
            + time_pressure,
            Decimal("-1"),
            Decimal("1"),
        )
        # Guard against double-blending: if the EMA was already updated at
        # this exact timestamp for this position, skip the blend to prevent
        # callers that invoke compute_decision_state + assess_stop_loss_regime
        # in the same cycle from double-smoothing the pressure signal.
        _ema_key = (inst_key, now_ts)
        if getattr(state, "_ema_updated_key", None) != _ema_key:
            if state.pressure_ema == Decimal("0"):
                state.pressure_ema = instant_pressure
            else:
                state.pressure_ema = self._clamp(
                    (state.pressure_ema * Decimal("0.60")) + (instant_pressure * Decimal("0.40")),
                    Decimal("-1"),
                    Decimal("1"),
                )
            state._ema_updated_key = _ema_key  # type: ignore[attr-defined]

        regime = DecisionRegime.TREND
        if (
            qty > 0
            and not signal_matches_position
            and signed_score <= -self.config.stop_loss_min_opposite_score_abs
            and spot_pressure < Decimal("-0.20")
        ):
            regime = DecisionRegime.BROKEN
        elif state.recent_flip_count >= 2 or abs(signed_score) < Decimal("0.18") or abs(spot_pressure) < Decimal("0.12"):
            regime = DecisionRegime.CHOP

        protection_sec = float(entry_protection_sec if entry_protection_sec is not None else self.config.stop_loss_entry_protection_sec)
        hold_sec = max(0.0, now_ts - opened_ts) if opened_ts > 0 else 0.0
        would_hold_entry_protection = bool(qty > 0 and hold_sec < protection_sec)
        signal_side = str(active_side or "NONE").upper()
        signal_is_none = signal_side == "NONE"
        explicit_opposite = (not signal_matches_position) and not signal_is_none
        strong_opposite = explicit_opposite and abs(signal_score) >= self.config.stop_loss_min_opposite_score_abs
        would_hold_thesis_not_opposite = bool(qty > 0 and not strong_opposite)
        prospective_adverse_hits = state.adverse_hits + 1 if strong_opposite else 0
        prospective_adverse_sec = (
            max(0.0, now_ts - state.adverse_since_ts)
            if state.adverse_since_ts > 0 and strong_opposite
            else 0.0
        )
        would_pending_confirmation = bool(
            qty > 0
            and strong_opposite
            and (
                prospective_adverse_sec < float(self.config.stop_loss_regime_min_sec)
                or prospective_adverse_hits < self.config.stop_loss_regime_confirmations
            )
        )
        hard_pressure_exit = (
            state.pressure_ema <= _HARD_PRESSURE_EXIT_THRESHOLD
            and (
                not signal_matches_position
                or spot_pressure < _SPOT_PRESSURE_EXIT_THRESHOLD
            )
        )
        if qty <= 0:
            phase = DecisionPhase.PROBE if state.pressure_ema >= Decimal("0.20") and regime != DecisionRegime.CHOP else DecisionPhase.FLAT
        else:
            phase = DecisionPhase.EXIT if (regime == DecisionRegime.BROKEN or hard_pressure_exit) else DecisionPhase.HOLD

        state.regime = regime.value
        state.phase = phase.value
        return DecisionState(
            regime=regime,
            phase=phase,
            pressure=state.pressure_ema,
            edge=edge,
            persistence=persistence,
            book_pressure=book_pressure,
            drawdown_pressure=drawdown_pressure,
            time_pressure=time_pressure,
            current_price=current_price,
            price_to_beat=price_to_beat,
            spot_minus_strike_bps=spot_minus_strike_bps,
            metadata={
                "recent_flip_count": str(state.recent_flip_count),
                "hold_sec": f"{hold_sec:.1f}",
                "instant_pressure": str(instant_pressure),
                "spot_pressure": str(spot_pressure),
                "side": side,
                "would_hold_entry_protection": "1" if would_hold_entry_protection else "0",
                "would_hold_thesis_not_opposite": "1" if would_hold_thesis_not_opposite else "0",
                "would_pending_confirmation": "1" if would_pending_confirmation else "0",
                "legacy_signal_side": signal_side,
            },
        )

    def reset_stop_loss_regime(self, inst_key: str) -> None:
        state = self.states.get(inst_key)
        if state is None:
            return
        state.adverse_since_ts = 0.0
        state.adverse_hits = 0
        if state.lifecycle == PositionLifecycle.EXIT_ARMED:
            state.lifecycle = PositionLifecycle.LONG
        state.recycle_hold_until_ts = 0.0

    def assess_stop_loss_regime(
        self,
        *,
        inst_key: str,
        now_ts: float,
        qty: Decimal,
        opened_ts: float,
        held_side: str,
        signal_active_side: str,
        signal_score: Decimal,
        signal_matches_position: bool,
        force_exit: bool,
        current_price: Optional[Decimal] = None,
        price_to_beat: Optional[Decimal] = None,
        best_bid: Decimal = Decimal("0"),
        best_ask: Decimal = Decimal("0"),
        fair: Optional[Decimal] = None,
        time_left_sec: Optional[float] = None,
        avg_entry: Decimal = Decimal("0"),
        peak_bid: Optional[Decimal] = None,
        peak_fair: Optional[Decimal] = None,
        precomputed_decision_state: Optional[DecisionState] = None,
    ) -> StopLossRegimeDecision:
        if qty <= 0:
            self.states.pop(inst_key, None)
            return StopLossRegimeDecision(status="flat", reason="flat_position")
        state = self.sync_position(
            inst_key=inst_key,
            qty=qty,
            opened_ts=opened_ts,
            thesis_side=held_side,
            now_ts=now_ts,
        )
        if force_exit:
            state.lifecycle = PositionLifecycle.EXIT_ARMED
            return StopLossRegimeDecision(status="armed", reason="force_exit_near_close")

        # If caller already computed DecisionState this cycle, reuse it to
        # avoid double-blending the pressure EMA (0.6/0.4 smoothing).
        if precomputed_decision_state is not None:
            decision_state = precomputed_decision_state
        else:
            decision_state = self.compute_decision_state(
                inst_key=inst_key,
                now_ts=now_ts,
                qty=qty,
                opened_ts=opened_ts,
                held_side=held_side,
                active_side=signal_active_side,
                signal_score=signal_score,
                signal_matches_position=signal_matches_position,
                current_price=current_price,
                price_to_beat=price_to_beat,
                best_bid=best_bid,
                best_ask=best_ask,
                fair=fair,
                time_left_sec=time_left_sec,
                avg_entry=avg_entry,
                peak_bid=peak_bid,
                peak_fair=peak_fair,
            )
        if decision_state.phase == DecisionPhase.EXIT:
            state.lifecycle = PositionLifecycle.EXIT_ARMED
            state.adverse_since_ts = now_ts if state.adverse_since_ts <= 0 else state.adverse_since_ts
            state.adverse_hits = max(state.adverse_hits, self.config.stop_loss_regime_confirmations)
            return StopLossRegimeDecision(
                status="armed",
                reason=(
                    "state_machine_exit "
                    f"pressure={float(decision_state.pressure):+.4f} "
                    f"regime={decision_state.regime.value}"
                ),
                adverse_sec=max(0.0, now_ts - state.adverse_since_ts),
                adverse_hits=state.adverse_hits,
            )

        state.lifecycle = PositionLifecycle.LONG
        self.reset_stop_loss_regime(inst_key)
        return StopLossRegimeDecision(
            status="hold",
            reason=(
                "state_machine_hold "
                f"phase={decision_state.phase.value} "
                f"regime={decision_state.regime.value} "
                f"pressure={float(decision_state.pressure):+.4f} "
                f"legacy(entry={decision_state.metadata.get('would_hold_entry_protection')},"
                f"thesis={decision_state.metadata.get('would_hold_thesis_not_opposite')},"
                f"pending={decision_state.metadata.get('would_pending_confirmation')})"
            ),
        )
