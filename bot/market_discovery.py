from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger
from nautilus_trader.model.identifiers import InstrumentId

from bot.market_data import fetch_gamma_market_by_slug


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
    timeout = float(os.getenv("GAMMA_DISCOVERY_TIMEOUT_SEC", "8"))
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
    parsed: List[tuple[int, str]] = []
    for slug in slugs:
        try:
            ts = int(slug.rsplit("-", 1)[-1])
            parsed.append((ts, slug))
        except Exception:
            continue
    if not parsed:
        return slugs[0]
    future = [(ts, s) for ts, s in parsed if ts >= now_ts - 900]
    if future:
        future.sort(key=lambda x: x[0])
        return future[0][1]
    parsed.sort(key=lambda x: x[0], reverse=True)
    return parsed[0][1]


def _parse_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            txt = value.strip()
            if txt.startswith("[") and txt.endswith("]"):
                txt = txt[1:-1]
            return [p.strip().strip('"').strip("'") for p in txt.split(",") if p.strip()]
    return []


def _valid_token_id(value: Any) -> bool:
    import re
    return bool(re.fullmatch(r"\d{20,}", str(value or "").strip()))


async def hydrate_gamma_market_details(market: Dict[str, Any]) -> Dict[str, Any]:
    api_base = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")
    timeout = float(os.getenv("GAMMA_DISCOVERY_TIMEOUT_SEC", "8"))
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
    condition_id = str(market.get("conditionId") or market.get("condition_id") or "").strip()
    if not condition_id:
        return []
    token_ids = _parse_json_list(market.get("clobTokenIds") or market.get("clob_token_ids"))
    if len(token_ids) < 2:
        token_ids = _parse_json_list(market.get("clobTokenIDs"))
    if len(token_ids) < 2:
        tokens = market.get("tokens")
        if isinstance(tokens, list):
            token_ids = []
            for token in tokens:
                if not isinstance(token, dict):
                    continue
                token_id = token.get("token_id") or token.get("tokenId")
                if _valid_token_id(token_id):
                    token_ids.append(str(token_id))
    token_ids = [str(token_id).strip() for token_id in token_ids if _valid_token_id(token_id)]
    result: List[InstrumentId] = []
    for token_id in token_ids:
        try:
            result.append(InstrumentId.from_str(f"{condition_id}-{token_id}.POLYMARKET"))
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
