"""Mainnet WebSocket-only, quality-gated HIP-4 Outcome observation."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from loguru import logger
import websockets


HYPERLIQUID_MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"
MAX_STREAM_AGE_SEC = 5.0
DEFAULT_AUTHORITY_PATH = "/Users/cheng-kaihuang/hyperliquid_prediction_bot/logs/outcome_market_authority.json"


def outcome_coins(outcome_id: int) -> tuple[str, str]:
    return f"#{int(outcome_id)}0", f"#{int(outcome_id)}1"


def _number(value: Any) -> Optional[float]:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return float(parsed) if parsed.is_finite() else None


def _book_top(book: Any) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    try:
        bid, ask = book["levels"][0][0], book["levels"][1][0]
        return _number(bid["px"]), _number(ask["px"]), _number(bid.get("sz")), _number(ask.get("sz"))
    except (IndexError, KeyError, TypeError):
        return None, None, None, None


class HyperliquidOutcomeObserver:
    """Observe one configured daily market; never make a network REST request."""

    def __init__(self, *, market_id: Optional[int] = None, ws_url: Optional[str] = None, authority_path: Optional[str] = None, tick_listener=None) -> None:
        self.market_id = int(market_id if market_id is not None else os.getenv("HYPERLIQUID_OUTCOME_DAILY_MARKET_ID", "1313"))
        self.ws_url = os.getenv("HYPERLIQUID_OUTCOME_WS_URL") or ws_url or HYPERLIQUID_MAINNET_WS_URL
        self.authority_path = authority_path or os.getenv("HYPERLIQUID_OUTCOME_AUTHORITY_PATH", DEFAULT_AUTHORITY_PATH)
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._authority_thread: Optional[threading.Thread] = None
        self._pending_market_id: Optional[int] = None
        self._last_error_log_ts = 0.0
        self._tick_listener = tick_listener
        self._set_market(self.market_id, reason="not_connected")

    def _set_market(self, market_id: int, *, reason: str) -> None:
        self.market_id = int(market_id)
        side0, side1 = outcome_coins(self.market_id)
        with self._lock:
            self._snapshot: dict[str, Any] = {
                "available": False, "analysis_available": False, "stream_connected": False,
                "source": "hyperliquid_outcome_mainnet_ws", "market_id": self.market_id,
                "side0_coin": side0, "side1_coin": side1, "reason": reason,
            }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hyperliquid-outcome-ws")
        self._thread.start()
        self._authority_thread = threading.Thread(target=self._authority_loop, daemon=True, name="hyperliquid-outcome-authority")
        self._authority_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._authority_thread is not None:
            self._authority_thread.join(timeout=2.0)

    def snapshot(self) -> dict[str, Any]:
        now_ts = time.time()
        with self._lock:
            result = dict(self._snapshot)
        mids_ts = float(result.get("mids_received_ts", 0.0) or 0.0)
        side0_book_ts = float(result.get("side0_book_received_ts", 0.0) or 0.0)
        side1_book_ts = float(result.get("side1_book_received_ts", 0.0) or 0.0)
        result["mids_age_sec"] = max(0.0, now_ts - mids_ts) if mids_ts else None
        result["side0_book_age_sec"] = max(0.0, now_ts - side0_book_ts) if side0_book_ts else None
        result["side1_book_age_sec"] = max(0.0, now_ts - side1_book_ts) if side1_book_ts else None
        fresh_mids = result["mids_age_sec"] is not None and result["mids_age_sec"] <= MAX_STREAM_AGE_SEC
        fresh_books = all(age is not None and age <= MAX_STREAM_AGE_SEC for age in (result["side0_book_age_sec"], result["side1_book_age_sec"]))
        connected = bool(result.get("stream_connected"))
        side0_bid, side0_ask = _number(result.get("side0_bid")), _number(result.get("side0_ask"))
        side1_bid, side1_ask = _number(result.get("side1_bid")), _number(result.get("side1_ask"))
        bbo_valid = all(value is not None and value > 0 for value in (side0_bid, side0_ask, side1_bid, side1_ask))
        result["available"] = connected and fresh_mids
        result["analysis_available"] = connected and fresh_mids and fresh_books and bbo_valid
        result["side0_bbo_mid"] = (side0_bid + side0_ask) / 2.0 if side0_bid is not None and side0_ask is not None else None
        result["side1_bbo_mid"] = (side1_bid + side1_ask) / 2.0 if side1_bid is not None and side1_ask is not None else None
        return result

    def _merge(self, **changes: Any) -> None:
        with self._lock:
            self._snapshot = {**self._snapshot, **changes}

    def _authority_market_id(self) -> Optional[int]:
        """Read the other bot's atomically published current 1d market."""
        if not self.authority_path or not Path(self.authority_path).is_file():
            return None
        try:
            payload = json.loads(Path(self.authority_path).read_text(encoding="utf-8"))
            updated_at_ms = int(payload["updated_at_ms"])
            if time.time() - (updated_at_ms / 1000.0) > 180.0:
                return None
            if str(payload.get("period") or "").lower() not in {"1d", "daily", "24h"}:
                return None
            market_id = int(payload["market_id"])
            if (str(payload.get("side0_coin")), str(payload.get("side1_coin"))) != outcome_coins(market_id):
                return None
            return market_id
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _authority_loop(self) -> None:
        """Keep slow local journal I/O out of the WebSocket receive loop."""
        while not self._stop.wait(30.0):
            market_id = self._authority_market_id()
            if market_id is None or market_id == self.market_id:
                continue
            with self._lock:
                self._pending_market_id = market_id

    def _maybe_roll_market(self) -> bool:
        with self._lock:
            market_id = self._pending_market_id
            self._pending_market_id = None
        if market_id is None or market_id == self.market_id:
            return False
        logger.info(f"Hyperliquid Outcome market rollover: #{self.market_id} -> #{market_id}")
        self._set_market(market_id, reason="market_rollover_resubscribe")
        return True

    def _on_message(self, payload: dict[str, Any]) -> None:
        channel, data, now_ts = payload.get("channel"), payload.get("data"), time.time()
        with self._lock:
            side0_coin, side1_coin = self._snapshot["side0_coin"], self._snapshot["side1_coin"]
        if channel == "allMids" and isinstance(data, dict):
            mids = data.get("mids", data)
            if isinstance(mids, dict):
                btc_mark = _number(mids.get("BTC"))
                self._merge(
                    mids_received_ts=now_ts, side0_all_mid=_number(mids.get(side0_coin)),
                    side1_all_mid=_number(mids.get(side1_coin)), btc_mark=btc_mark,
                )
                if btc_mark is not None and self._tick_listener is not None:
                    try:
                        self._tick_listener(btc_mark, self.market_id)
                    except Exception:
                        pass
        elif channel == "l2Book" and isinstance(data, dict):
            coin = str(data.get("coin") or "")
            if coin not in {side0_coin, side1_coin}:
                return
            bid, ask, bid_depth, ask_depth = _book_top(data)
            prefix = "side0" if coin == side0_coin else "side1"
            self._merge(**{
                f"{prefix}_bid": bid, f"{prefix}_ask": ask, f"{prefix}_bid_depth": bid_depth,
                f"{prefix}_ask_depth": ask_depth, f"{prefix}_book_received_ts": now_ts,
                f"{prefix}_server_timestamp_ms": data.get("time"),
            })

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        reconnect_attempt = 0
        while not self._stop.is_set():
            try:
                with self._lock:
                    side0_coin, side1_coin = self._snapshot["side0_coin"], self._snapshot["side1_coin"]
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10) as ws:
                    self._merge(stream_connected=True, connected_ts=time.time(), reason=None)
                    reconnect_attempt = 0
                    for subscription in ({"type": "allMids"}, {"type": "l2Book", "coin": side0_coin}, {"type": "l2Book", "coin": side1_coin}):
                        await ws.send(json.dumps({"method": "subscribe", "subscription": subscription}))
                    while not self._stop.is_set():
                        if self._maybe_roll_market():
                            await ws.close()
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            self._on_message(parsed)
            except Exception as exc:
                now_ts = time.time()
                self._merge(stream_connected=False, disconnected_ts=now_ts, reason=type(exc).__name__)
                if now_ts - self._last_error_log_ts >= 60.0:
                    logger.warning(f"Hyperliquid Outcome WebSocket unavailable: {type(exc).__name__}: {exc}")
                    self._last_error_log_ts = now_ts
                reconnect_attempt += 1
                await asyncio.sleep(min(30.0, float(2 ** min(reconnect_attempt - 1, 5))))
