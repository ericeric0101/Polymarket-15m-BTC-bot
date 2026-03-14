from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class HoldToRedeemConfig:
    cost_threshold: Decimal
    min_time_left_sec: int
    max_inventory_shares: Decimal


class HoldToRedeemPolicy:
    def __init__(self, config: HoldToRedeemConfig) -> None:
        self.config = config

    def should_hold(self, avg_entry: Decimal, inventory_shares: Decimal, time_left_sec: Optional[float]) -> bool:
        if self.config.cost_threshold <= 0:
            return False
        if avg_entry < self.config.cost_threshold:
            return False
        if inventory_shares <= 0 or inventory_shares > self.config.max_inventory_shares:
            return False
        if time_left_sec is None:
            return False
        return time_left_sec >= float(self.config.min_time_left_sec)


@dataclass
class RegimeGuardConfig:
    n_markets: int
    trigger_sum_pnl_usdc: Decimal
    min_negative_markets: int


class RegimeGuardPolicy:
    def __init__(self, config: RegimeGuardConfig) -> None:
        self.config = config

    def min_negative_markets(self) -> int:
        return max(1, self.config.min_negative_markets)

    def should_trigger(self, window: list[float]) -> tuple[bool, float, int]:
        if len(window) < self.config.n_markets:
            return False, 0.0, 0
        window_sum = float(sum(window))
        neg_count = sum(1 for value in window if value < 0)
        should = neg_count >= self.min_negative_markets() and Decimal(str(window_sum)) <= self.config.trigger_sum_pnl_usdc
        return should, window_sum, neg_count


@dataclass
class FillCooldownConfig:
    post_fill_buy_cooldown_sec: float
    max_consecutive_losses: int
    loss_pause_sec: float


class FillCooldownPolicy:
    def __init__(self, config: FillCooldownConfig) -> None:
        self.config = config

    def next_buy_cooldown_until(self, now_ts: float) -> float:
        return now_ts + self.config.post_fill_buy_cooldown_sec

    def register_realized_pnl(
        self,
        recent_fill_pnl_results: list[float],
        realized_net_usdc: float,
        now_ts: float,
        current_quote_pause_until_ts: float,
    ) -> tuple[list[float], float, bool, float]:
        updated = list(recent_fill_pnl_results)
        updated.append(realized_net_usdc)
        max_history = max(10, self.config.max_consecutive_losses * 2)
        if len(updated) > max_history:
            updated = updated[-max_history:]
        if self.config.max_consecutive_losses <= 0 or len(updated) < self.config.max_consecutive_losses:
            return updated, current_quote_pause_until_ts, False, 0.0
        tail = updated[-self.config.max_consecutive_losses :]
        if not all(pnl < 0 for pnl in tail):
            return updated, current_quote_pause_until_ts, False, 0.0
        total_loss = float(sum(tail))
        pause_until = max(current_quote_pause_until_ts, now_ts + self.config.loss_pause_sec)
        return [], pause_until, True, total_loss
