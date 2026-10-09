from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger
from nautilus_trader.model.identifiers import InstrumentId

from bot.market_data import fetch_gamma_market_by_slug
from bot.protocol_v2 import UnsupportedMarketProtocol, register_market_assets, select_market_assets


def build_btc_15m_slug_candidates(lookback: int = 1, lookahead: int = 4) -> List[str]:
    now = datetime.now(timezone.utc)
    interval_start = int(now.timestamp() // 900) * 900
    slugs: List[str] = []
    for offset in range(-lookback, lookahead + 1):
        ts = interval_start + (offset * 900)
        if ts > 0:
            slugs.append(f"btc-updown-15m-{ts}")
    return slugs


async def discover_existing_btc_15m_slugs(candidates: List[str]) -> List[str]:
    api_base = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")
    existing: List[str] = []
    timeout = 8.0
    async with httpx.AsyncClient(timeout=timeout) as client:
        for slug in candidates:
            try:
                response = await client.get(
                    f"{api_base}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "archived": "false",
                        "slug": slug,
                        "limit": 1,
                    },
                )
                response.raise_for_status()
                data = response.json()
                if isinstance(data, list) and len(data) > 0:
                    existing.append(slug)
            except Exception as e:
                logger.debug(f"Slug discovery failed for {slug}: {e}")
    return existing


def resolve_btc_15m_market_slugs() -> List[str]:
    lookback = int(os.getenv("BTC_MARKET_LOOKBACK_INTERVALS", "1"))
    lookahead = int(os.getenv("BTC_MARKET_LOOKAHEAD_INTERVALS", "4"))
    candidates = build_btc_15m_slug_candidates(lookback=lookback, lookahead=lookahead)
    if not candidates:
        return []
    try:
        existing = asyncio.run(discover_existing_btc_15m_slugs(candidates))
    except Exception as e:
        logger.warning(f"Gamma discovery failed, using deterministic candidates: {e}")
        existing = []
    if existing:
        logger.info(f"Resolved BTC 15-min slugs from Gamma API: {existing}")
        return existing
    fallback = [
        s for s in candidates
        if int(s.rsplit("-", 1)[-1]) >= int(datetime.now(timezone.utc).timestamp()) - 900
    ]
    logger.warning(f"No confirmed slugs from Gamma API; using fallback candidates: {fallback}")
    return fallback


def select_primary_btc_15m_slug(slugs: List[str]) -> Optional[str]:
    if not slugs:
        return None
    now_ts = int(datetime.now(timezone.utc).timestamp())
    min_remaining_sec = 90
    parsed: List[tuple[int, str]] = []
    for slug in slugs:
        try:
            ts = int(slug.rsplit("-", 1)[-1])
            parsed.append((ts, slug))
        except Exception:
            continue
    if not parsed:
        return slugs[0]
    viable_current = [
        (ts, s)
        for ts, s in parsed
        if ts <= now_ts and (ts + 900 - now_ts) > min_remaining_sec
    ]
    if viable_current:
        viable_current.sort(key=lambda x: x[0], reverse=True)
        return viable_current[0][1]
    future = [(ts, s) for ts, s in parsed if ts > now_ts]
    if future:
        future.sort(key=lambda x: x[0])
        return future[0][1]
    parsed.sort(key=lambda x: x[0], reverse=True)
    return parsed[0][1]


async def hydrate_gamma_market_details(market: Dict[str, Any]) -> Dict[str, Any]:
    api_base = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")
    timeout = 8.0
    market_id = market.get("id") or market.get("marketId") or market.get("conditionId") or market.get("condition_id")
    if not market_id:
        return market
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(f"{api_base}/markets/{market_id}")
            if response.status_code != 200:
                return market
            payload = response.json()
            if isinstance(payload, dict):
                merged = dict(market)
                merged.update(payload)
                return merged
        except Exception:
            return market
    return market


def extract_instrument_ids_from_gamma_market(market: Dict[str, Any]) -> List[InstrumentId]:
    """Instrument ids for the market's protocol: clobTokenIds (v1) or positionIds (v2).

    The ids are chosen by Gamma's ``version`` field, never by which id field is
    present; unknown or inconsistent markets yield no instruments (not traded).
    Each asset's protocol is registered for signing, balances, fees and redeem.
    """
    try:
        assets = select_market_assets(market)
    except UnsupportedMarketProtocol as exc:
        logger.warning(f"Market not tradable by this bot: slug={market.get('slug')} reason={exc}")
        return []
    register_market_assets(assets)
    result: List[InstrumentId] = []
    for asset_id in assets.asset_ids:
        try:
            result.append(InstrumentId.from_str(f"{assets.condition_id}-{asset_id}.POLYMARKET"))
        except Exception:
            continue
    return result


def resolve_primary_btc_15m_instrument_ids(slug: str) -> List[InstrumentId]:
    try:
        market = asyncio.run(fetch_gamma_market_by_slug(slug))
    except Exception as e:
        logger.warning(f"Failed to fetch Gamma market for slug {slug}: {e}")
        return []
    if not market:
        logger.warning(f"No Gamma market found for slug: {slug}")
        return []
    instrument_ids = extract_instrument_ids_from_gamma_market(market)
    if not instrument_ids:
        try:
            hydrated = asyncio.run(hydrate_gamma_market_details(market))
        except Exception:
            hydrated = market
        instrument_ids = extract_instrument_ids_from_gamma_market(hydrated)
    if not instrument_ids:
        logger.warning(f"No instrument IDs extracted from slug: {slug}")
        return []
    return instrument_ids


def resolve_best_btc_15m_market(slugs: List[str]) -> tuple[Optional[str], List[InstrumentId]]:
    if not slugs:
        return None, []
    primary = select_primary_btc_15m_slug(slugs)
    ordered: List[str] = []
    if primary:
        ordered.append(primary)
    ordered.extend([s for s in slugs if s not in ordered])
    for slug in ordered:
        instrument_ids = resolve_primary_btc_15m_instrument_ids(slug)
        if instrument_ids:
            return slug, instrument_ids
    return primary, []
