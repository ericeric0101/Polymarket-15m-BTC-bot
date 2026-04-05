from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional, Tuple


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
    winner_continuation_min_fair_edge_ps: Decimal = Decimal("0")


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
            )
            self.states[inst_key] = state
        else:
            state.thesis_side = str(thesis_side or state.thesis_side or "NONE").upper()
            state.adverse_since_ts = 0.0
            state.adverse_hits = 0
            if state.lifecycle == PositionLifecycle.EXIT_ARMED:
                state.lifecycle = PositionLifecycle.LONG

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
            self.config.winner_continuation_min_fair_edge_ps > 0
            and best_bid > avg_entry
            and fair_edge_ps >= self.config.winner_continuation_min_fair_edge_ps
        ):
            return True, (
                f"winner_continuation fair_edge={float(fair_edge_ps):.4f}"
                f">={float(self.config.winner_continuation_min_fair_edge_ps):.4f} "
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

    def reset_stop_loss_regime(self, inst_key: str) -> None:
        state = self.states.get(inst_key)
        if state is None:
            return
        state.adverse_since_ts = 0.0
        state.adverse_hits = 0
        if state.lifecycle == PositionLifecycle.EXIT_ARMED:
            state.lifecycle = PositionLifecycle.LONG

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

        hold_sec = max(0.0, now_ts - state.entered_ts) if state.entered_ts > 0 else 0.0
        protection_until_ts = state.entry_protection_until_ts
        if protection_until_ts <= 0 and state.entered_ts > 0:
            protection_until_ts = state.entered_ts + float(self.config.stop_loss_entry_protection_sec)
        if protection_until_ts > now_ts:
            state.lifecycle = PositionLifecycle.ENTRY_PROTECTED
            state.adverse_since_ts = 0.0
            state.adverse_hits = 0
            protection_left = max(0.0, protection_until_ts - now_ts)
            return StopLossRegimeDecision(
                status="hold",
                reason=(
                    f"entry_protection hold={hold_sec:.1f}s "
                    f"remaining={protection_left:.1f}s "
                    f"mode={state.entry_mode}"
                ),
            )

        signal_side = str(signal_active_side or "NONE").upper()
        signal_is_none = signal_side == "NONE"
        explicit_opposite = (not signal_matches_position) and not signal_is_none
        strong_opposite = explicit_opposite and abs(signal_score) >= self.config.stop_loss_min_opposite_score_abs
        if not strong_opposite:
            self.reset_stop_loss_regime(inst_key)
            return StopLossRegimeDecision(
                status="hold",
                reason=(
                    "held_thesis_not_opposite "
                    f"signal={signal_side.lower()} score={float(signal_score):+.4f}"
                ),
            )

        if state.adverse_since_ts <= 0:
            state.adverse_since_ts = now_ts
            state.adverse_hits = 1
        else:
            state.adverse_hits += 1
        adverse_sec = max(0.0, now_ts - state.adverse_since_ts)
        state.lifecycle = PositionLifecycle.EXIT_ARMED
        if (
            adverse_sec < float(self.config.stop_loss_regime_min_sec)
            or state.adverse_hits < self.config.stop_loss_regime_confirmations
        ):
            return StopLossRegimeDecision(
                status="pending",
                reason=(
                    "stop_loss_regime_pending "
                    f"hits={state.adverse_hits}/{self.config.stop_loss_regime_confirmations} "
                    f"adverse={adverse_sec:.1f}s<{self.config.stop_loss_regime_min_sec}s"
                ),
                adverse_sec=adverse_sec,
                adverse_hits=state.adverse_hits,
            )
        return StopLossRegimeDecision(
            status="armed",
            reason=(
                "stop_loss_regime_armed "
                f"hits={state.adverse_hits} adverse={adverse_sec:.1f}s"
            ),
            adverse_sec=adverse_sec,
            adverse_hits=state.adverse_hits,
        )
