"""Mainnet WebSocket-only, quality-gated HIP-4 Outcome observation."""
from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger
import websockets


HYPERLIQUID_MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"
MAX_STREAM_AGE_SEC = 5.0
APPLICATION_HEARTBEAT_INTERVAL_SEC = 20.0
APPLICATION_PONG_TIMEOUT_SEC = 45.0
WS_OPEN_TIMEOUT_SEC = 10.0
STABLE_STREAM_RESET_SEC = 30.0
MAX_RECONNECT_DELAY_SEC = 30.0
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


def reconnect_delay_sec(attempt: int) -> float:
    """Bound reconnects so a bad endpoint cannot create a connection storm."""
    return min(MAX_RECONNECT_DELAY_SEC, float(2 ** min(max(0, int(attempt) - 1), 5)))


def stream_is_stable_for_backoff_reset(*, connected_ts: float, now_ts: float, stream_ready: bool) -> bool:
    """A handshake is insufficient: require a sustained valid market stream."""
    return bool(stream_ready) and now_ts - float(connected_ts) >= STABLE_STREAM_RESET_SEC


class HyperliquidOutcomeObserver:
    """Observe one configured daily market; never make a network REST request."""

    def __init__(
        self,
        *,
        market_id: Optional[int] = None,
        ws_url: Optional[str] = None,
        authority_path: Optional[str] = None,
        tick_listener: Optional[Callable[[float, int, int], None]] = None,
        lifecycle_listener: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ) -> None:
        self.market_id = int(market_id if market_id is not None else os.getenv("HYPERLIQUID_OUTCOME_DAILY_MARKET_ID", "1313"))
        self.ws_url = os.getenv("HYPERLIQUID_OUTCOME_WS_URL") or ws_url or HYPERLIQUID_MAINNET_WS_URL
        self.authority_path = authority_path or os.getenv("HYPERLIQUID_OUTCOME_AUTHORITY_PATH", DEFAULT_AUTHORITY_PATH)
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._authority_thread: Optional[threading.Thread] = None
        self._pending_market_id: Optional[int] = None
        self._last_error_log_ts = 0.0
        self._tick_listener = tick_listener
        self._lifecycle_listener = lifecycle_listener
        self._set_market(self.market_id, reason="not_connected")

    def _set_market(self, market_id: int, *, reason: str) -> None:
        self.market_id = int(market_id)
        side0, side1 = outcome_coins(self.market_id)
        with self._lock:
            self._snapshot: dict[str, Any] = {
                "available": False, "analysis_available": False, "stream_connected": False,
                "source": "hyperliquid_outcome_mainnet_ws", "market_id": self.market_id,
                "side0_coin": side0, "side1_coin": side1, "reason": reason,
                "stream_ready": False, "connection_attempt_count": 0, "connection_epoch": 0,
                "disconnect_count": 0, "consecutive_disconnects": 0,
                "reconnect_delay_sec": None,
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
        last_message_ts = float(result.get("last_message_ts", 0.0) or 0.0)
        last_valid_data_ts = float(result.get("last_valid_data_ts", 0.0) or 0.0)
        result["last_message_age_sec"] = max(0.0, now_ts - last_message_ts) if last_message_ts else None
        result["last_valid_data_age_sec"] = max(0.0, now_ts - last_valid_data_ts) if last_valid_data_ts else None
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

    def _emit_lifecycle(self, event: str, **details: Any) -> None:
        """Best-effort, low-frequency audit hook; never block or kill the feed."""
        if self._lifecycle_listener is None:
            return
        payload = {"observed_ts": time.time(), "market_id": self.market_id, **details}
        try:
            self._lifecycle_listener(event, payload)
        except Exception:
            pass

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
        self._merge(last_message_ts=now_ts)
        if channel == "pong":
            self._merge(last_app_pong_ts=now_ts)
            return
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
                if btc_mark is not None:
                    self._merge(last_valid_data_ts=now_ts, stream_ready=True, reason=None)
                if btc_mark is not None and self._tick_listener is not None:
                    try:
                        self._tick_listener(btc_mark, self.market_id, int(self._snapshot.get("connection_epoch", 0)))
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
            connected_ts = 0.0
            try:
                with self._lock:
                    side0_coin, side1_coin = self._snapshot["side0_coin"], self._snapshot["side1_coin"]
                    connection_attempt_count = int(self._snapshot.get("connection_attempt_count", 0)) + 1
                self._merge(
                    connection_attempt_count=connection_attempt_count,
                    stream_ready=False,
                    reason="connecting",
                )
                self._emit_lifecycle("connect_attempt", attempt=connection_attempt_count)
                async with websockets.connect(
                    self.ws_url,
                    open_timeout=WS_OPEN_TIMEOUT_SEC,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    connected_ts = time.time()
                    with self._lock:
                        connection_epoch = int(self._snapshot.get("connection_epoch", 0)) + 1
                    self._merge(
                        stream_connected=True, connected_ts=connected_ts, reason=None,
                        last_app_ping_ts=None, last_app_pong_ts=None, reconnect_delay_sec=None,
                        connection_epoch=connection_epoch,
                    )
                    self._emit_lifecycle("connected", attempt=connection_attempt_count)
                    for subscription in ({"type": "allMids"}, {"type": "l2Book", "coin": side0_coin}, {"type": "l2Book", "coin": side1_coin}):
                        await ws.send(json.dumps({"method": "subscribe", "subscription": subscription}))
                    self._emit_lifecycle("subscribed", side0_coin=side0_coin, side1_coin=side1_coin)
                    stable_reset = False
                    while not self._stop.is_set():
                        if self._maybe_roll_market():
                            await ws.close()
                            break
                        heartbeat_now = time.time()
                        with self._lock:
                            last_ping_ts = self._snapshot.get("last_app_ping_ts") or 0.0
                            last_pong_ts = self._snapshot.get("last_app_pong_ts") or connected_ts
                        if heartbeat_now - float(last_ping_ts) >= APPLICATION_HEARTBEAT_INTERVAL_SEC:
                            await ws.send(json.dumps({"method": "ping"}))
                            self._merge(last_app_ping_ts=heartbeat_now)
                        if heartbeat_now - float(last_pong_ts) >= APPLICATION_PONG_TIMEOUT_SEC:
                            raise TimeoutError("Hyperliquid application pong timeout")
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            self._on_message(parsed)
                        snapshot = self.snapshot()
                        if not stable_reset and stream_is_stable_for_backoff_reset(
                            connected_ts=connected_ts,
                            now_ts=time.time(),
                            stream_ready=bool(snapshot.get("stream_ready")),
                        ):
                            reconnect_attempt = 0
                            stable_reset = True
                            self._merge(consecutive_disconnects=0, reconnect_delay_sec=None)
                            self._emit_lifecycle("stable", connected_for_sec=time.time() - connected_ts)
            except Exception as exc:
                now_ts = time.time()
                with self._lock:
                    disconnect_count = int(self._snapshot.get("disconnect_count", 0)) + 1
                    consecutive_disconnects = int(self._snapshot.get("consecutive_disconnects", 0)) + 1
                self._merge(
                    stream_connected=False,
                    stream_ready=False,
                    disconnected_ts=now_ts,
                    reason=type(exc).__name__,
                    last_disconnect_error=type(exc).__name__,
                    last_disconnect_detail=str(exc)[:300],
                    last_connected_duration_sec=(now_ts - connected_ts) if connected_ts else None,
                    disconnect_count=disconnect_count,
                    consecutive_disconnects=consecutive_disconnects,
                )
                if now_ts - self._last_error_log_ts >= 60.0:
                    logger.warning(f"Hyperliquid Outcome WebSocket unavailable: {type(exc).__name__}: {exc}")
                    self._last_error_log_ts = now_ts
                if self._stop.is_set():
                    break
                reconnect_attempt += 1
                delay = reconnect_delay_sec(reconnect_attempt)
                self._merge(reconnect_delay_sec=delay)
                self._emit_lifecycle(
                    "disconnected",
                    error_type=type(exc).__name__,
                    error_detail=str(exc)[:300],
                    consecutive_disconnects=consecutive_disconnects,
                    retry_delay_sec=delay,
                )
                await asyncio.sleep(delay + random.uniform(0.0, min(0.5, delay * 0.1)))
