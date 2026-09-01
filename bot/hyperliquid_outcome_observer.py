"""Mainnet WebSocket-only BTC daily Hyperliquid Outcome observer.

No REST request, wallet, authentication, order, or execution dependency is
permitted here. The configured daily Outcome id is subscribed through the same
``allMids`` and ``l2Book`` channels used by the Hyperliquid Outcome bot.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from decimal import Decimal
from typing import Any, Optional

from loguru import logger
import websockets


HYPERLIQUID_MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"


def outcome_coins(outcome_id: int) -> tuple[str, str]:
    return f"#{int(outcome_id)}0", f"#{int(outcome_id)}1"


def _number(value: Any) -> Optional[float]:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return float(parsed) if parsed.is_finite() else None


def _best_bid_ask(book: Any) -> tuple[Optional[float], Optional[float]]:
    try:
        return _number(book["levels"][0][0]["px"]), _number(book["levels"][1][0]["px"])
    except (IndexError, KeyError, TypeError):
        return None, None


class HyperliquidOutcomeObserver:
    """Cache mainnet Outcome stream state without entering the trade path."""

    def __init__(self, *, market_id: Optional[int] = None, ws_url: Optional[str] = None) -> None:
        self.market_id = int(market_id if market_id is not None else os.getenv("HYPERLIQUID_OUTCOME_DAILY_MARKET_ID", "1313"))
        self.yes_coin, self.no_coin = outcome_coins(self.market_id)
        self.ws_url = os.getenv("HYPERLIQUID_OUTCOME_WS_URL") or ws_url or HYPERLIQUID_MAINNET_WS_URL
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._snapshot: dict[str, Any] = {
            "available": False, "source": "hyperliquid_outcome_mainnet_ws",
            "market_id": self.market_id, "yes_coin": self.yes_coin, "no_coin": self.no_coin,
            "reason": "not_connected",
        }
        self._last_error_log_ts = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hyperliquid-outcome-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._snapshot)
        observed_ts = float(result.get("observed_ts", 0.0) or 0.0)
        result["age_sec"] = max(0.0, time.time() - observed_ts) if observed_ts else None
        return result

    def _update(self, **changes: Any) -> None:
        with self._lock:
            merged = {**self._snapshot, **changes}
            yes_mid, no_mid = _number(merged.get("yes_mid")), _number(merged.get("no_mid"))
            merged["available"] = yes_mid is not None and yes_mid > 0 and no_mid is not None and no_mid > 0
            if merged["available"]:
                merged.pop("reason", None)
            self._snapshot = merged

    def _on_message(self, payload: dict[str, Any]) -> None:
        channel, data = payload.get("channel"), payload.get("data")
        now_ts = time.time()
        if channel == "allMids" and isinstance(data, dict):
            mids = data.get("mids", data)
            if not isinstance(mids, dict):
                return
            self._update(
                observed_ts=now_ts, source="hyperliquid_outcome_mainnet_ws",
                market_id=self.market_id, yes_coin=self.yes_coin, no_coin=self.no_coin,
                yes_mid=_number(mids.get(self.yes_coin)), no_mid=_number(mids.get(self.no_coin)),
                btc_mark=_number(mids.get("BTC")),
            )
        elif channel == "l2Book" and isinstance(data, dict):
            coin = str(data.get("coin") or "")
            if coin not in {self.yes_coin, self.no_coin}:
                return
            bid, ask = _best_bid_ask(data)
            prefix = "yes" if coin == self.yes_coin else "no"
            self._update(
                observed_ts=now_ts, source="hyperliquid_outcome_mainnet_ws",
                **{f"{prefix}_bid": bid, f"{prefix}_ask": ask, "book_observed_ts": now_ts,
                   "server_timestamp_ms": data.get("time")},
            )

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        reconnect_attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10) as ws:
                    reconnect_attempt = 0
                    for subscription in (
                        {"type": "allMids"},
                        {"type": "l2Book", "coin": self.yes_coin},
                        {"type": "l2Book", "coin": self.no_coin},
                    ):
                        await ws.send(json.dumps({"method": "subscribe", "subscription": subscription}))
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            self._on_message(parsed)
            except Exception as exc:
                now_ts = time.time()
                if now_ts - self._last_error_log_ts >= 60.0:
                    logger.warning(f"Hyperliquid Outcome WebSocket unavailable: {type(exc).__name__}: {exc}")
                    self._last_error_log_ts = now_ts
                self._update(observed_ts=now_ts, available=False, reason=type(exc).__name__)
                reconnect_attempt += 1
                await asyncio.sleep(min(30.0, float(2 ** min(reconnect_attempt - 1, 5))))
