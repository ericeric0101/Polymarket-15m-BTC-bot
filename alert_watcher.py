from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

from dashboard_state import DashboardState, TradeRecord
from telegram_notifier import TelegramNotifier


ALERT_CONSECUTIVE_LOSSES = 3
ALERT_LARGE_LOSS_USD = 7.0
ALERT_LOW_BALANCE_USD = 20.0
ALERT_HEARTBEAT_STALE_SEC = 300


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _trade_pnl(trade: TradeRecord) -> Optional[float]:
    cost = float(trade.entry_price or 0.0) * float(trade.qty or 0.0)
    if trade.redeem_amount is not None:
        return float(trade.redeem_amount) - cost
    if trade.exit_price is not None:
        return (float(trade.exit_price) - float(trade.entry_price or 0.0)) * float(trade.qty or 0.0)
    return None


class AlertWatcher:
    def __init__(self) -> None:
        self.consecutive_losses = ALERT_CONSECUTIVE_LOSSES
        self.large_loss_usd = ALERT_LARGE_LOSS_USD
        self.low_balance_usd = ALERT_LOW_BALANCE_USD
        self.heartbeat_stale_sec = ALERT_HEARTBEAT_STALE_SEC
        self.last_sent: Dict[str, datetime] = {}
        self._alerted_loss_trades: Set[Tuple[int, str]] = set()
        self._seen_errors: Set[Tuple[str, str]] = set()
        self._seen_redeems: Set[Tuple[str, float]] = set()

    def check_and_alert(self, state: DashboardState, bot: TelegramNotifier) -> None:
        snapshot = state.snapshot() if hasattr(state, "snapshot") else state
        now = _utc_now()

        if snapshot.consecutive_losses >= self.consecutive_losses and self._cooldown_ok("consecutive_losses", now, 30 * 60):
            state.update(bot_paused=True) if hasattr(state, "update") else setattr(state, "bot_paused", True)
            bot.send_message(TelegramNotifier.escape_md("🔴 3 consecutive losses. Auto-pausing bot."))
            self.last_sent["consecutive_losses"] = now

        large_loss = self._latest_large_loss(snapshot)
        if large_loss is not None:
            trade, pnl = large_loss
            key = (int(trade.trade_id), str(trade.market_slug))
            if key not in self._alerted_loss_trades and self._cooldown_ok("large_loss", now, 5 * 60):
                self._alerted_loss_trades.add(key)
                bot.send_message(TelegramNotifier.escape_md(f"⚠️ Large loss: -${abs(pnl):.2f} on {trade.market_slug}"))
                self.last_sent["large_loss"] = now

        last_heartbeat = _as_utc(snapshot.last_heartbeat)
        if (now - last_heartbeat).total_seconds() > self.heartbeat_stale_sec and self._cooldown_ok("heartbeat", now, 10 * 60):
            bot.send_message(TelegramNotifier.escape_md(f"🔴 Heartbeat lost. Last: {last_heartbeat:%Y-%m-%d %H:%M:%S UTC}"))
            self.last_sent["heartbeat"] = now

        if snapshot.usdc_balance < self.low_balance_usd and self._cooldown_ok("low_balance", now, 60 * 60):
            bot.send_message(TelegramNotifier.escape_md(f"⚠️ Low balance: ${snapshot.usdc_balance:.2f} USDC"))
            self.last_sent["low_balance"] = now

        new_error = self._latest_new_error(snapshot)
        if new_error is not None and self._cooldown_ok("error", now, 2 * 60):
            ts, message = new_error
            self._seen_errors.add((ts.isoformat(), message))
            bot.send_message(TelegramNotifier.escape_md(f"🔴 Error: {message[:150]}"))
            self.last_sent["error"] = now

        redeem = self._latest_new_redeem(snapshot)
        if redeem is not None:
            trade, pnl = redeem
            self._seen_redeems.add((str(trade.market_slug), float(trade.redeem_amount or 0.0)))
            bot.send_message(TelegramNotifier.escape_md(f"✅ Redeemed {trade.market_slug}: +${max(pnl, 0.0):.2f}"))

    def _cooldown_ok(self, key: str, now: datetime, seconds: int) -> bool:
        last = self.last_sent.get(key)
        return last is None or (now - last).total_seconds() >= seconds

    def _latest_large_loss(self, state: DashboardState) -> Optional[Tuple[TradeRecord, float]]:
        for trade in sorted(state.trades, key=lambda t: int(t.trade_id), reverse=True):
            pnl = _trade_pnl(trade)
            if pnl is not None and pnl < -self.large_loss_usd:
                return trade, pnl
        return None

    def _latest_new_error(self, state: DashboardState) -> Optional[Tuple[datetime, str]]:
        for ts, message in reversed(list(state.recent_errors)):
            key = (ts.isoformat(), str(message))
            if key not in self._seen_errors:
                return ts, str(message)
        return None

    def _latest_new_redeem(self, state: DashboardState) -> Optional[Tuple[TradeRecord, float]]:
        for trade in sorted(state.trades, key=lambda t: int(t.trade_id), reverse=True):
            if trade.redeem_amount is None:
                continue
            key = (str(trade.market_slug), float(trade.redeem_amount))
            if key in self._seen_redeems:
                continue
            pnl = _trade_pnl(trade)
            if pnl is not None:
                return trade, pnl
        return None
