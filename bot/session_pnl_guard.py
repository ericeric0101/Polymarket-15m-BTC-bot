"""Persistent, BUY-only daily realized-PnL guard primitives.

This module deliberately has no execution dependency: callers use its decision
at their final new-BUY authority boundary.  SELL and reconciliation paths must
never consult it as an execution veto.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class SessionPnlGuardConfig:
    enabled: bool = True
    profit_arm_usdc: Decimal = Decimal("8")
    profit_drawdown_usdc: Decimal = Decimal("4")
    hard_profit_lock_enabled: bool = False
    hard_profit_lock_usdc: Decimal = Decimal("10")
    max_loss_enabled: bool = True
    max_loss_usdc: Decimal = Decimal("6")


@dataclass
class SessionPnlState:
    session_date_taipei: str
    realized_pnl_usdc: Decimal = Decimal("0")
    realized_high_water_usdc: Decimal = Decimal("0")
    profit_guard_armed: bool = False
    buy_lock_active: bool = False
    buy_lock_reason: str = ""

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "SessionPnlState":
        return cls(
            session_date_taipei=str(value["session_date_taipei"]),
            realized_pnl_usdc=Decimal(str(value.get("realized_pnl_usdc", 0))),
            realized_high_water_usdc=Decimal(str(value.get("realized_high_water_usdc", 0))),
            profit_guard_armed=bool(value.get("profit_guard_armed", False)),
            buy_lock_active=bool(value.get("buy_lock_active", False)),
            buy_lock_reason=str(value.get("buy_lock_reason") or ""),
        )


@dataclass(frozen=True)
class SessionBuyGuardDecision:
    allowed: bool
    reason: str
    session_date_taipei: str
    realized_pnl_usdc: Decimal
    realized_high_water_usdc: Decimal
    drawdown_from_high_usdc: Decimal
    armed: bool


class SessionPnlGuard:
    def __init__(self, config: SessionPnlGuardConfig, *, session_date: str, state: SessionPnlState | None = None) -> None:
        self.config = config
        self.state = state or SessionPnlState(session_date_taipei=session_date)
        self._evaluate()

    def apply_realized_delta(self, delta_usdc: Decimal | float | str) -> bool:
        self.state.realized_pnl_usdc += Decimal(str(delta_usdc))
        return self._evaluate()

    def _evaluate(self) -> bool:
        """Apply sticky state transitions and say whether an event should emit."""
        state = self.state
        changed = False
        if state.realized_pnl_usdc > state.realized_high_water_usdc:
            state.realized_high_water_usdc = state.realized_pnl_usdc
            changed = True
        # On restart reconstruction current PnL can already have retraced
        # below the arm threshold while the durable/reconstructed high-water
        # proves the session did arm. The arm is sticky for the current guard
        # session (the configured Taipei overnight window in live runtime).
        if self.config.enabled and not state.profit_guard_armed and (
            state.realized_pnl_usdc >= self.config.profit_arm_usdc
            or state.realized_high_water_usdc >= self.config.profit_arm_usdc
        ):
            state.profit_guard_armed = True
            changed = True
        if not self.config.enabled or state.buy_lock_active:
            return changed
        reason = ""
        if self.config.max_loss_enabled and state.realized_pnl_usdc <= -self.config.max_loss_usdc:
            reason = "session_max_loss_lock"
        elif self.config.hard_profit_lock_enabled and state.realized_pnl_usdc >= self.config.hard_profit_lock_usdc:
            reason = "session_hard_profit_lock"
        elif state.profit_guard_armed and (
            state.realized_high_water_usdc - state.realized_pnl_usdc >= self.config.profit_drawdown_usdc
        ):
            reason = "session_profit_drawdown_lock"
        if reason:
            state.buy_lock_active = True
            state.buy_lock_reason = reason
            changed = True
        return changed

    def decision(self) -> SessionBuyGuardDecision:
        state = self.state
        drawdown = max(Decimal("0"), state.realized_high_water_usdc - state.realized_pnl_usdc)
        return SessionBuyGuardDecision(
            allowed=not (self.config.enabled and state.buy_lock_active),
            reason=state.buy_lock_reason if self.config.enabled and state.buy_lock_active else "ok",
            session_date_taipei=state.session_date_taipei,
            realized_pnl_usdc=state.realized_pnl_usdc,
            realized_high_water_usdc=state.realized_high_water_usdc,
            drawdown_from_high_usdc=drawdown,
            armed=state.profit_guard_armed,
        )
