from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional


@dataclass
class TradeRecord:
    trade_id: int
    market_slug: str
    side: str
    entry_price: float
    qty: float
    exit_price: Optional[float]
    redeem_amount: Optional[float]
    is_settled: bool


@dataclass
class DashboardState:
    strike_price: float
    spot_price: float
    position_side: Optional[str]
    position_entry: Optional[float]
    position_qty: Optional[float]
    position_ask: Optional[float]
    current_market_price: float
    trades: List[TradeRecord]
    cumulative_pnl: float
    usdc_balance: float
    pol_balance: float
    account_last_updated: datetime
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _callbacks: List[Callable[[], None]] = field(default_factory=list, init=False, repr=False)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if not hasattr(self, key):
                    raise AttributeError(f"DashboardState has no field '{key}'")
                setattr(self, key, value)
            self.updated_at = datetime.now(timezone.utc)
            callbacks = list(self._callbacks)
        self._notify(callbacks)

    def snapshot(self) -> "DashboardState":
        with self._lock:
            return DashboardState(
                strike_price=self.strike_price,
                spot_price=self.spot_price,
                position_side=self.position_side,
                position_entry=self.position_entry,
                position_qty=self.position_qty,
                position_ask=self.position_ask,
                current_market_price=self.current_market_price,
                trades=list(self.trades),
                cumulative_pnl=self.cumulative_pnl,
                usdc_balance=self.usdc_balance,
                pol_balance=self.pol_balance,
                account_last_updated=self.account_last_updated,
                updated_at=self.updated_at,
            )

    def upsert_redeem(self, market_slug: str, redeem_amount: float) -> bool:
        with self._lock:
            for trade in self.trades:
                if trade.market_slug == market_slug:
                    trade.redeem_amount = float(redeem_amount)
                    trade.is_settled = True
                    self.updated_at = datetime.now(timezone.utc)
                    callbacks = list(self._callbacks)
                    break
            else:
                return False
        self._notify(callbacks)
        return True

    def add_listener(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)

    @staticmethod
    def _notify(callbacks: List[Callable[[], None]]) -> None:
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass
