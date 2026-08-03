from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


BINANCE_AGGTRADE_WS_URL = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
POLYMARKET_LIVE_WS_URL = "wss://ws-live-data.polymarket.com"
POLYMARKET_CHAINLINK_TWAP_TOPIC_BY_WINDOW = {
    30: "crypto_prices_twap_thirty",
    60: "crypto_prices_twap_sixty",
}
POLYMARKET_CHAINLINK_SUBSCRIBE_PAYLOAD = {
    "action": "subscribe",
    "subscriptions": [
        {
            "topic": "crypto_prices_twap_sixty",
            "type": "update",
            "filters": "{\"symbol\":\"btc/usd\"}",
        }
    ],
}


@dataclass
class PriceTick:
    source: str
    price: Decimal
    updated_at_ms: Optional[int]
    received_at_ts: float
    window_seconds: Optional[int] = None
    raw_summary: str = ""


def safe_json_loads(raw: Any) -> Optional[Any]:
    try:
        return json.loads(raw)
    except Exception:
        return None


def to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not dec.is_finite():
        return None
    return dec


def to_epoch_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        num = float(value)
    except Exception:
        return None
    if num <= 0:
        return None
    if num > 1_000_000_000_000:
        return int(num)
    if num > 1_000_000_000:
        return int(num * 1000)
    return None


def from_e18_decimal(value: Any) -> Optional[Decimal]:
    dec = to_decimal(value)
    if dec is None:
        return None
    return dec / Decimal("1000000000000000000")


def build_polymarket_chainlink_subscribe_payload(
    *,
    use_twap: bool = True,
    window_seconds: int = 60,
    symbol: str = "btc/usd",
) -> dict[str, Any]:
    if use_twap:
        topic = POLYMARKET_CHAINLINK_TWAP_TOPIC_BY_WINDOW.get(int(window_seconds))
        if topic is None:
            raise ValueError("Polymarket Chainlink TWAP window must be 30 or 60 seconds")
        filters = json.dumps({"symbol": symbol.strip().lower()}, separators=(",", ":"))
    else:
        topic = "crypto_prices_chainlink"
        filters = ""
    return {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": topic,
                "type": "update" if use_twap else "*",
                "filters": filters,
            }
        ],
    }


def extract_binance_aggtrade_tick(raw: Any) -> Optional[PriceTick]:
    payload = safe_json_loads(raw)
    if not isinstance(payload, dict):
        return None
    px = to_decimal(payload.get("p"))
    if px is None or px <= 0:
        return None
    updated_ms = to_epoch_ms(payload.get("E"))
    return PriceTick(
        source="binance_ws",
        price=px,
        updated_at_ms=updated_ms,
        received_at_ts=time.time(),
        raw_summary=str(payload)[:180],
    )


def _string_contains_btc(obj: Any) -> bool:
    try:
        txt = json.dumps(obj, ensure_ascii=False).lower()
    except Exception:
        txt = str(obj).lower()
    return "btc" in txt


def extract_polymarket_chainlink_tick(payload: Any) -> Optional[PriceTick]:
    candidate_price_keys = (
        "price",
        "currentPrice",
        "current_price",
        "value",
        "answer",
        "mark",
        "px",
    )
    candidate_time_keys = (
        "updatedAt",
        "updated_at",
        "timestamp",
        "ts",
        "time",
    )

    if isinstance(payload, str):
        payload = safe_json_loads(payload)
    if not isinstance(payload, (dict, list)):
        return None

    if isinstance(payload, dict):
        topic = str(payload.get("topic", "")).lower()
        inner = payload.get("payload")
        if topic in {"crypto_prices_twap_thirty", "crypto_prices_twap_sixty", "prices.crypto.chainlink.twap"} and isinstance(inner, dict):
            symbol = str(inner.get("symbol", "")).lower()
            if symbol in {"btc/usd", "btcusd", "btc"} or "btc" in symbol:
                window_seconds = (
                    int(inner.get("window_s") or inner.get("windowSeconds") or inner.get("window_seconds") or 0)
                    or (30 if topic == "crypto_prices_twap_thirty" else 60 if topic == "crypto_prices_twap_sixty" else 0)
                )
                price = from_e18_decimal(inner.get("full_accuracy_value"))
                if price is None or price <= 0:
                    price = to_decimal(inner.get("value"))
                updated_ms = to_epoch_ms(inner.get("timestamp")) or to_epoch_ms(payload.get("timestamp"))
                if price is not None and price > 0:
                    return PriceTick(
                        source=f"polymarket_chainlink_twap_{window_seconds}s_ws" if window_seconds else "polymarket_chainlink_twap_ws",
                        price=price,
                        updated_at_ms=updated_ms,
                        received_at_ts=time.time(),
                        window_seconds=window_seconds or None,
                        raw_summary=str(payload)[:240],
                    )
        if topic == "crypto_prices_chainlink" and isinstance(inner, dict):
            symbol = str(inner.get("symbol", "")).lower()
            if "btc" in symbol:
                price = None
                updated_ms = None
                for key in candidate_price_keys:
                    if key in inner:
                        price = to_decimal(inner.get(key))
                        if price is not None and price > 0:
                            break
                for key in candidate_time_keys:
                    if key in inner:
                        updated_ms = to_epoch_ms(inner.get(key))
                        if updated_ms is not None:
                            break
                if updated_ms is None:
                    for key in candidate_time_keys:
                        if key in payload:
                            updated_ms = to_epoch_ms(payload.get(key))
                            if updated_ms is not None:
                                break
                if price is not None and price > 0:
                    return PriceTick(
                        source="polymarket_chainlink_ws",
                        price=price,
                        updated_at_ms=updated_ms,
                        received_at_ts=time.time(),
                        raw_summary=str(payload)[:240],
                    )

    def walk(node: Any) -> Optional[PriceTick]:
        if isinstance(node, dict):
            if _string_contains_btc(node):
                price: Optional[Decimal] = None
                updated_ms: Optional[int] = None
                for key in candidate_price_keys:
                    if key in node:
                        price = to_decimal(node.get(key))
                        if price is not None and price > 0:
                            break
                for key in candidate_time_keys:
                    if key in node:
                        updated_ms = to_epoch_ms(node.get(key))
                        if updated_ms is not None:
                            break
                if price is not None and price > 0:
                    return PriceTick(
                        source="polymarket_chainlink_ws",
                        price=price,
                        updated_at_ms=updated_ms,
                        received_at_ts=time.time(),
                        raw_summary=str(node)[:240],
                    )
            for value in node.values():
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found is not None:
                    return found
        return None

    return walk(payload)
