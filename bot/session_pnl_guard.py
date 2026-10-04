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
    # ``legacy`` preserves the original fixed-threshold guard byte-for-byte in
    # its decision semantics.  V2 is deliberately opt-in at process start.
    mode: str = "legacy"
    enabled: bool = True
    profit_arm_usdc: Decimal = Decimal("8")
    profit_drawdown_usdc: Decimal = Decimal("4")
    hard_profit_lock_enabled: bool = False
    hard_profit_lock_usdc: Decimal = Decimal("10")
    max_loss_enabled: bool = True
    max_loss_usdc: Decimal = Decimal("6")
    monthly_net_target_usdc: Decimal | None = None
    per_trade_risk_usdc: Decimal | None = None

    @property
    def is_target_scaled_v2(self) -> bool:
        return self.mode in {"target_scaled_v2", "shadow_target_scaled_v2"}

    def target_scaled_thresholds(self) -> dict[str, Decimal]:
        """Derive V2 thresholds with Decimal-only accounting.

        This remains a pure function so offline replay and the live guard share
        exactly the same risk arithmetic.
        """
        if not self.is_target_scaled_v2:
            raise ValueError("target-scaled thresholds requested outside V2")
        monthly = self.monthly_net_target_usdc
        risk = self.per_trade_risk_usdc
        if (monthly is None or risk is None or not monthly.is_finite() or not risk.is_finite()
                or monthly <= 0 or risk <= 0):
            raise ValueError("target_scaled_v2 requires positive monthly target and per-trade risk")
        daily = monthly / Decimal("30")
        return {
            "normalized_target_usdc": daily,
            "profit_arm_usdc": max(Decimal("0.35") * daily, risk),
            "normal_profit_drawdown_usdc": max(Decimal("0.35") * daily, risk),
            "target_profit_drawdown_usdc": max(Decimal("0.25") * daily, Decimal("0.75") * risk),
            "hard_profit_lock_usdc": Decimal("1.35") * daily,
            "max_loss_usdc": min(Decimal("2") * risk, max(Decimal("1.5") * risk, Decimal("0.45") * daily)),
        }


@dataclass
class SessionPnlState:
    session_date_taipei: str
    realized_pnl_usdc: Decimal = Decimal("0")
    realized_high_water_usdc: Decimal = Decimal("0")
    profit_guard_armed: bool = False
    buy_lock_active: bool = False
    buy_lock_reason: str = ""
    guard_mode: str = "legacy"

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "SessionPnlState":
        return cls(
            session_date_taipei=str(value["session_date_taipei"]),
            realized_pnl_usdc=Decimal(str(value.get("realized_pnl_usdc", 0))),
            realized_high_water_usdc=Decimal(str(value.get("realized_high_water_usdc", 0))),
            profit_guard_armed=bool(value.get("profit_guard_armed", False)),
            buy_lock_active=bool(value.get("buy_lock_active", False)),
            buy_lock_reason=str(value.get("buy_lock_reason") or ""),
            guard_mode=str(value.get("guard_mode") or "legacy"),
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
    mode: str = "legacy"
    state: str = "NORMAL"
    target_protection_active: bool = False
    normalized_target_usdc: Decimal | None = None
    monthly_target_usdc: Decimal | None = None
    current_allowed_drawdown_usdc: Decimal | None = None
    shadow_would_block: bool = False


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
        profit_arm = (
            self.config.target_scaled_thresholds()["profit_arm_usdc"]
            if self.config.is_target_scaled_v2 else self.config.profit_arm_usdc
        )
        if self.config.enabled and not state.profit_guard_armed and (
            state.realized_pnl_usdc >= profit_arm
            or state.realized_high_water_usdc >= profit_arm
        ):
            state.profit_guard_armed = True
            changed = True
        if not self.config.enabled or state.buy_lock_active:
            return changed
        if self.config.is_target_scaled_v2:
            thresholds = self.config.target_scaled_thresholds()
            target_protected = state.realized_high_water_usdc >= thresholds["normalized_target_usdc"]
            allowed_drawdown = (
                thresholds["target_profit_drawdown_usdc"]
                if target_protected else thresholds["normal_profit_drawdown_usdc"]
            )
            if state.realized_pnl_usdc <= -thresholds["max_loss_usdc"]:
                reason = "session_max_loss_lock"
            elif state.realized_pnl_usdc >= thresholds["hard_profit_lock_usdc"]:
                reason = "session_hard_profit_lock"
            elif state.profit_guard_armed and (
                state.realized_high_water_usdc - state.realized_pnl_usdc >= allowed_drawdown
            ):
                reason = "session_profit_drawdown_lock"
            else:
                reason = ""
            if reason:
                state.buy_lock_active = True
                state.buy_lock_reason = reason
                changed = True
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
        thresholds = self.config.target_scaled_thresholds() if self.config.is_target_scaled_v2 else {}
        target_protected = bool(
            thresholds and state.realized_high_water_usdc >= thresholds["normalized_target_usdc"]
        )
        if state.buy_lock_active:
            guard_state = "LOSS_LOCKED" if state.buy_lock_reason == "session_max_loss_lock" else "PROFIT_LOCKED"
        elif target_protected:
            guard_state = "TARGET_PROTECTION"
        elif state.profit_guard_armed:
            guard_state = "PROFIT_GUARD_ARMED"
        else:
            guard_state = "NORMAL"
        would_block = bool(self.config.enabled and state.buy_lock_active)
        allowed = not would_block or self.config.mode == "shadow_target_scaled_v2"
        dd_limit = (
            thresholds["target_profit_drawdown_usdc"] if target_protected else thresholds.get("normal_profit_drawdown_usdc")
        ) if thresholds else self.config.profit_drawdown_usdc
        return SessionBuyGuardDecision(
            allowed=allowed,
            reason=state.buy_lock_reason if self.config.enabled and state.buy_lock_active else "ok",
            session_date_taipei=state.session_date_taipei,
            realized_pnl_usdc=state.realized_pnl_usdc,
            realized_high_water_usdc=state.realized_high_water_usdc,
            drawdown_from_high_usdc=drawdown,
            armed=state.profit_guard_armed,
            mode=self.config.mode,
            state=guard_state,
            target_protection_active=target_protected,
            normalized_target_usdc=thresholds.get("normalized_target_usdc"),
            monthly_target_usdc=self.config.monthly_net_target_usdc,
            current_allowed_drawdown_usdc=dd_limit,
            shadow_would_block=bool(self.config.mode == "shadow_target_scaled_v2" and would_block),
        )
