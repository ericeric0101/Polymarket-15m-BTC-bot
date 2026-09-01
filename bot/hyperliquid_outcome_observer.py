"""Read-only BTC daily Outcome observer for Polymarket lead/lag research.

It has no wallet, authentication, order, or execution dependency.  The
observer is intentionally isolated on a daemon thread so an unavailable HIP-4
endpoint cannot delay a Polymarket quote or exit path.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

import httpx
from loguru import logger


HYPERLIQUID_OUTCOME_TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


@dataclass(frozen=True)
class OutcomeDailyMarket:
    outcome_id: int
    yes_coin: str
    no_coin: str
    expiry_utc: str
    target_price: Decimal
    description: str


def discover_btc_daily_outcome(payload: dict[str, Any]) -> Optional[OutcomeDailyMarket]:
    """Select the nearest-expiring discoverable BTC `period:1d` outcome."""
    candidates: list[OutcomeDailyMarket] = []
    for raw in payload.get("outcomes", []):
        if not isinstance(raw, dict):
            continue
        description = str(raw.get("description") or "")
        fields = {
            key.strip(): value.strip()
            for part in description.split("|") if ":" in part
            for key, value in (part.split(":", 1),)
        }
        if fields.get("class") != "priceBinary" or fields.get("underlying", "").upper() != "BTC":
            continue
        if fields.get("period", "").lower() not in {"1d", "daily", "24h"}:
            continue
        try:
            outcome_id = int(raw.get("outcome", raw.get("outcomeId")))
        except (TypeError, ValueError):
            continue
        target_price = _decimal(fields.get("targetPrice"))
        expiry = str(fields.get("expiry") or "")
        if target_price is None or not expiry:
            continue
        candidates.append(OutcomeDailyMarket(
            outcome_id=outcome_id,
            yes_coin=f"#{outcome_id}0",
            no_coin=f"#{outcome_id}1",
            expiry_utc=expiry,
            target_price=target_price,
            description=description,
        ))
    return min(candidates, key=lambda item: item.expiry_utc) if candidates else None


def _best_bid_ask(book: Any) -> tuple[Optional[Decimal], Optional[Decimal]]:
    try:
        levels = book["levels"]
        bid = _decimal(levels[0][0]["px"])
        ask = _decimal(levels[1][0]["px"])
        return bid, ask
    except (IndexError, KeyError, TypeError):
        return None, None


class HyperliquidOutcomeObserver:
    """Continuously cache a read-only BTC daily Outcome snapshot."""

    def __init__(self, *, info_url: Optional[str] = None) -> None:
        self.info_url = (info_url or os.getenv("HYPERLIQUID_OUTCOME_INFO_URL") or HYPERLIQUID_OUTCOME_TESTNET_INFO_URL).rstrip("/")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._market: Optional[OutcomeDailyMarket] = None
        self._snapshot: dict[str, Any] = {"available": False, "source": "hyperliquid_outcome_rest"}
        self._last_meta_ts = 0.0
        self._last_book_ts = 0.0
        self._last_error_log_ts = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hyperliquid-outcome-observer")
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

    def _post_info(self, client: httpx.Client, payload: dict[str, Any]) -> Any:
        response = client.post(self.info_url, json=payload)
        response.raise_for_status()
        return response.json()

    def _refresh_once(self, client: httpx.Client) -> None:
        now_ts = time.time()
        if self._market is None or now_ts - self._last_meta_ts >= 60.0:
            meta = self._post_info(client, {"type": "outcomeMeta"})
            self._market = discover_btc_daily_outcome(meta if isinstance(meta, dict) else {})
            self._last_meta_ts = now_ts
        market = self._market
        if market is None:
            with self._lock:
                self._snapshot = {
                    "available": False, "source": "hyperliquid_outcome_rest",
                    "observed_ts": now_ts, "reason": "no_discoverable_btc_daily_outcome",
                }
            return

        mids = self._post_info(client, {"type": "allMids"})
        yes_mid = _decimal((mids or {}).get(market.yes_coin))
        no_mid = _decimal((mids or {}).get(market.no_coin))
        if yes_mid is None or no_mid is None:
            raise ValueError(f"daily Outcome mids missing for {market.yes_coin}/{market.no_coin}")

        snapshot: dict[str, Any] = {
            "available": True, "source": "hyperliquid_outcome_rest",
            "observed_ts": time.time(), "market_id": market.outcome_id,
            "yes_coin": market.yes_coin, "no_coin": market.no_coin,
            "yes_mid": float(yes_mid), "no_mid": float(no_mid),
            "btc_mark": float(_decimal((mids or {}).get("BTC")) or Decimal("0")),
            "target_price": float(market.target_price), "expiry_utc": market.expiry_utc,
        }
        if now_ts - self._last_book_ts >= 15.0:
            yes_book = self._post_info(client, {"type": "l2Book", "coin": market.yes_coin})
            no_book = self._post_info(client, {"type": "l2Book", "coin": market.no_coin})
            yes_bid, yes_ask = _best_bid_ask(yes_book)
            no_bid, no_ask = _best_bid_ask(no_book)
            snapshot.update({
                "yes_bid": float(yes_bid) if yes_bid is not None else None,
                "yes_ask": float(yes_ask) if yes_ask is not None else None,
                "no_bid": float(no_bid) if no_bid is not None else None,
                "no_ask": float(no_ask) if no_ask is not None else None,
                "book_observed_ts": time.time(),
            })
            self._last_book_ts = now_ts
        else:
            with self._lock:
                prior = dict(self._snapshot)
            for key in ("yes_bid", "yes_ask", "no_bid", "no_ask", "book_observed_ts"):
                snapshot[key] = prior.get(key)
        with self._lock:
            self._snapshot = snapshot

    def _run(self) -> None:
        # A short timeout and bounded retry cadence make this strictly
        # observational: network trouble never enters the trading path.
        with httpx.Client(timeout=httpx.Timeout(3.0, connect=2.0), headers={"Content-Type": "application/json"}) as client:
            while not self._stop.is_set():
                try:
                    self._refresh_once(client)
                except Exception as exc:
                    now_ts = time.time()
                    if now_ts - self._last_error_log_ts >= 60.0:
                        logger.warning(f"Hyperliquid Outcome observer unavailable: {type(exc).__name__}: {exc}")
                        self._last_error_log_ts = now_ts
                    with self._lock:
                        self._snapshot = {
                            "available": False, "source": "hyperliquid_outcome_rest",
                            "observed_ts": now_ts, "reason": f"{type(exc).__name__}",
                        }
                self._stop.wait(2.0)
