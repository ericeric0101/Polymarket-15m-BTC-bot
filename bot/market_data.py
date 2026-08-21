import os
from decimal import Decimal
from datetime import datetime, timezone
import math
from typing import Any, Optional, Dict

import httpx
import re


def fetch_coinbase_spot_sync(
    timeout_sec: float,
    already_logged_first_spot: bool,
    logger_info_fn,
    logger_debug_fn,
) -> tuple[Optional[Decimal], bool]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        with httpx.Client(timeout=timeout_sec, headers=headers) as client:
            resp = client.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("price")
            if raw is not None:
                price = Decimal(str(raw))
                if not already_logged_first_spot:
                    logger_info_fn(f"✓ First BTC spot via Coinbase HTTP: ${price:,.2f}")
                    already_logged_first_spot = True
                return price, already_logged_first_spot
    except Exception as exc:
        logger_debug_fn(f"Coinbase spot fetch failed: {exc}")
    return None, already_logged_first_spot


def record_external_spot_observation(
    external_spot_history: list[tuple[float, Decimal]],
    external_spot_history_max: int,
    now_ts: float,
    price: Decimal,
) -> None:
    external_spot_history.append((now_ts, price))
    if len(external_spot_history) > external_spot_history_max:
        external_spot_history.pop(0)


def extract_strike_from_question(question_text: str, latest_external_spot: Optional[Decimal]) -> Optional[Decimal]:
    text = str(question_text or "")
    if not text:
        return None
    candidates: list[Decimal] = []
    for match in re.finditer(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)", text):
        raw = str(match.group(1) or "").replace(",", "").strip()
        if not raw:
            continue
        try:
            value = Decimal(raw)
        except Exception:
            continue
        if value < Decimal("1000") or value > Decimal("1000000"):
            continue
        candidates.append(value)
    if not candidates:
        return None
    ref_spot = latest_external_spot
    if ref_spot is None or ref_spot <= 0:
        return candidates[0]
    return min(candidates, key=lambda value: abs(value - ref_spot))


def extract_market_start_ts_from_slug(slug: str) -> Optional[int]:
    txt = str(slug or "").strip()
    if not txt:
        return None
    try:
        ts = int(txt.rsplit("-", 1)[-1])
        return ts if ts > 0 else None
    except Exception:
        return None


def extract_price_to_beat_from_market_payload(market: dict[str, Any]) -> Optional[Decimal]:
    def _coerce_decimal(value: Any) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            out = Decimal(str(value))
        except Exception:
            return None
        return out if out > 0 else None

    for key in ("_priceToBeat", "priceToBeat", "price_to_beat"):
        ptb = _coerce_decimal(market.get(key))
        if ptb is not None:
            return ptb

    event_meta = market.get("eventMetadata", {}) or {}
    if not isinstance(event_meta, dict):
        event_meta = {}
    if not event_meta and isinstance(market.get("event_metadata"), dict):
        event_meta = market.get("event_metadata") or {}
    for key in ("priceToBeat", "price_to_beat"):
        ptb = _coerce_decimal(event_meta.get(key))
        if ptb is not None:
            return ptb
    return None


def _coerce_positive_decimal(value: Any) -> Optional[Decimal]:
    try:
        price = Decimal(str(value))
    except Exception:
        return None
    return price if price > 0 else None


async def fetch_crypto_price_to_beat(
    *,
    start_ts: int,
    end_ts: int,
    symbol: str = "BTC",
    variant: str = "fifteen",
) -> Optional[Decimal]:
    """Fetch Polymarket's published crypto opening Price To Beat."""
    if start_ts <= 0 or end_ts <= start_ts:
        return None
    params = {
        "symbol": str(symbol or "BTC").upper(),
        "eventStartTime": datetime.fromtimestamp(start_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "variant": str(variant or "fifteen"),
        "endDate": datetime.fromtimestamp(end_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        async with httpx.AsyncClient(
            timeout=8.0
        ) as client:
            response = await client.get(
                "https://polymarket.com/api/crypto/crypto-price",
                params=params,
            )
            if response.status_code != 200:
                return None
            return _coerce_positive_decimal((response.json() or {}).get("openPrice"))
    except Exception:
        return None


def resolve_opening_strike_from_history(
    external_spot_history: list[tuple[float, Decimal]],
    start_ts: int,
    max_lag_sec: float,
    near_window_sec: float,
) -> Optional[tuple[float, Decimal]]:
    if not external_spot_history:
        return None
    future_candidates: list[tuple[float, Decimal]] = []
    near_candidates: list[tuple[float, Decimal]] = []
    for ts, px in external_spot_history:
        if ts >= float(start_ts) and ts <= float(start_ts) + max_lag_sec:
            future_candidates.append((ts, px))
        if abs(ts - float(start_ts)) <= near_window_sec:
            near_candidates.append((ts, px))
    if future_candidates:
        future_candidates.sort(key=lambda item: item[0])
        return future_candidates[0]
    if near_candidates:
        near_candidates.sort(key=lambda item: (abs(item[0] - float(start_ts)), item[0]))
        return near_candidates[0]
    return None


def fetch_binance_open_price_sync(
    start_ts: int,
    timeout_sec: float,
    logger_debug_fn,
) -> Optional[Decimal]:
    minute_start_ts = int(start_ts) - (int(start_ts) % 60)
    params = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "startTime": minute_start_ts * 1000,
        "limit": 1,
    }
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.get("https://fapi.binance.com/fapi/v1/klines", params=params)
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, list) and data and isinstance(data[0], list) and len(data[0]) >= 2:
            price = Decimal(str(data[0][1]))
            if price > 0:
                return price
    except Exception as exc:
        logger_debug_fn(f"Binance open-price backfill failed: {exc}")
    return None


def estimate_external_spot_sigma_annualized(
    external_spot_history: list[tuple[float, Decimal]],
    min_points: int,
    digital_vol_window: int,
) -> Optional[Decimal]:
    if len(external_spot_history) < min_points:
        return None
    sample = external_spot_history[-digital_vol_window:]
    returns: list[float] = []
    dts: list[float] = []
    for idx in range(1, len(sample)):
        prev_ts, prev_px = sample[idx - 1]
        cur_ts, cur_px = sample[idx]
        prev_f = float(prev_px)
        cur_f = float(cur_px)
        dt = float(cur_ts - prev_ts)
        if prev_f <= 0 or cur_f <= 0 or dt <= 0:
            continue
        lr = math.log(cur_f / prev_f)
        if not math.isfinite(lr):
            continue
        returns.append(lr)
        dts.append(dt)
    if len(returns) < max(2, min_points - 1):
        return None
    mean_r = sum(returns) / len(returns)
    denom = max(1, len(returns) - 1)
    var = sum((ret - mean_r) ** 2 for ret in returns) / denom
    if var <= 0:
        return None
    std_per_obs = math.sqrt(var)
    avg_dt = sum(dts) / len(dts)
    if avg_dt <= 0:
        return None
    sec_per_year = 365.0 * 24.0 * 3600.0
    sigma_annual = std_per_obs * math.sqrt(sec_per_year / avg_dt)
    if not math.isfinite(sigma_annual) or sigma_annual <= 0:
        return None
    return Decimal(str(sigma_annual))


async def fetch_gamma_market_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    """
    Attempt to fetch full event which contains eventMetadata.priceToBeat.
    Async implementation for use in strategical decision paths.
    """
    api_base = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")
    timeout = 8.0

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{api_base}/events", params={"slug": slug})
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    event = data[0]
                    event_slug = str(event.get("slug") or "")
                    if event_slug != str(slug):
                        return None
                    # Expose priceToBeat inside the first market for legacy compatibility if available
                    event_meta = event.get("eventMetadata", {}) or {}
                    if not isinstance(event_meta, dict):
                        event_meta = {}
                    if not event_meta and isinstance(event.get("event_metadata"), dict):
                        event_meta = event.get("event_metadata") or {}

                    markets = event.get("markets", [])
                    if isinstance(markets, list) and len(markets) > 0:
                        market = dict(markets[0])
                        if event_meta:
                            # Merge event metadata into market for easier extraction
                            market["eventMetadata"] = event_meta
                        market["_gamma_event_slug"] = event_slug
                        market["_gamma_event_id"] = event.get("id")
                        return market
    except Exception:
        pass
    return None
