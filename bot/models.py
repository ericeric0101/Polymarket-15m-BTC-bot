from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional

from execution.exit_policy import ExitStage


@dataclass(frozen=True)
class MarketSnapshot:
    instrument_id: str
    phase: str
    time_left_sec: Optional[float]
    best_bid: Decimal
    best_ask: Decimal
    fee_rate: Decimal
    spread: Decimal
    spread_pct: Decimal
    slippage_buffer_pct: Decimal
    exit_stage: ExitStage
    in_reduce_only_tail: bool
    stop_loss_disabled_in_tail: bool


@dataclass(frozen=True)
class PositionState:
    instrument_id: str
    qty: Decimal
    sellable_qty: Decimal
    avg_entry_price: Decimal
    entry_fee_remaining: Decimal
    hold_sec: float
    stop_loss_confirm_hits: int


@dataclass(frozen=True)
class SignalDecision:
    active_side: str
    score: Decimal
    locked: bool
    reason: str
    matches_position: bool


class ExitDecisionType(Enum):
    NONE = "NONE"
    HOLD_TO_REDEEM = "HOLD_TO_REDEEM"
    HOLD_IN_BAND = "HOLD_IN_BAND"
    STOP_LOSS_PENDING_CONFIRMATION = "STOP_LOSS_PENDING_CONFIRMATION"
    TAKER_STOP_LOSS = "TAKER_STOP_LOSS"


@dataclass(frozen=True)
class ExitDecision:
    decision_type: ExitDecisionType
    reason: str
    net_if_exit: Decimal
    gross_if_exit: Decimal
    exit_fee_est: Decimal
    exit_px_effective: Decimal
    confirm_hits: int = 0
    metadata: Dict[str, str] = field(default_factory=dict)
