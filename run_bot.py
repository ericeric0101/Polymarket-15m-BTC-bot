"""
Complete BTC 15-Min Trading Bot - FIXED VERSION
- Uses time-based filtering (proven to work from test)
- $1 per trade maximum
- Reloads instruments every 12 minutes
- Pre-loads price history on startup
- Full P&L tracking in simulation
"""

import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
import random
import httpx
import re
import json
import threading
import uuid
import subprocess

# Add project to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Now import Nautilus
from nautilus_trader.config import (
    InstrumentProviderConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket import (
    PolymarketDataClientConfig,
    PolymarketExecClientConfig,
)
from nautilus_trader.adapters.polymarket.factories import (
    PolymarketLiveDataClientFactory,
    PolymarketLiveExecClientFactory,
)
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.identifiers import InstrumentId, ClientOrderId
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.data import QuoteTick

from dotenv import load_dotenv
from loguru import logger
import redis

# Import our phases
from core.strategy_brain.signal_processors.spike_detector import SpikeDetectionProcessor
from core.strategy_brain.signal_processors.sentiment_processor import SentimentProcessor
from core.strategy_brain.signal_processors.divergence_processor import PriceDivergenceProcessor
from core.strategy_brain.fusion_engine.signal_fusion import get_fusion_engine
from execution.risk_engine import get_risk_engine
from execution.fee_rate_client import FeeRateClient
from execution.parameter_tuner import ParameterTuner
from execution.rebate_model import CRYPTO_FEE_CURVE, bps_to_fee_rate
from execution.rebate_reporter import RebateReporter
from monitoring.performance_tracker import get_performance_tracker
from monitoring.grafana_exporter import get_grafana_exporter
from monitoring.trade_journal_db import TradeJournalDB
from feedback.learning_engine import get_learning_engine
from execution.maker_engine import MakerEngine, MakerEngineConfig
from execution.sim_adapter import SimAdapter, SimAdapterConfig, PaperTrade

load_dotenv()


def auto_apply_local_patches() -> None:
    """
    Auto-apply local compatibility patches (idempotent).
    """
    enabled = os.getenv("AUTO_APPLY_NAUTILUS_PATCH", "1").strip().lower() not in ("0", "false", "no")
    if not enabled:
        return
    scripts = [
        Path(__file__).parent / "scripts" / "patch_nautilus_polymarket_drop_log.py",
        Path(__file__).parent / "scripts" / "patch_nautilus_polymarket_ticksize_log.py",
        Path(__file__).parent / "scripts" / "patch_nautilus_polymarket_trade_log.py",
    ]
    for script in scripts:
        if not script.exists():
            logger.debug(f"Patch script not found: {script}")
            continue
        try:
            proc = subprocess.run(
                [sys.executable, str(script), "--quiet"],
                cwd=str(Path(__file__).parent),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if proc.returncode != 0:
                logger.warning(f"Auto patch script exited with code {proc.returncode}: {proc.stderr.strip()}")
            else:
                logger.debug(f"Auto patch script applied/verified: {script.name}")
        except Exception as e:
            logger.warning(f"Auto patch apply failed ({script.name}): {e}")


def _build_btc_15m_slug_candidates(lookback: int = 1, lookahead: int = 4) -> List[str]:
    """
    Build candidate BTC 15-min market slugs around current UTC interval.
    """
    now = datetime.now(timezone.utc)
    interval_start = int(now.timestamp() // 900) * 900
    slugs = []
    for offset in range(-lookback, lookahead + 1):
        ts = interval_start + (offset * 900)
        if ts > 0:
            slugs.append(f"btc-updown-15m-{ts}")
    return slugs


async def _discover_existing_btc_15m_slugs(candidates: List[str]) -> List[str]:
    """
    Query Gamma API directly and keep only slugs that currently exist.
    """
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
    """
    Resolve BTC 15-min market slugs without monkey patching Nautilus internals.
    """
    lookback = int(os.getenv("BTC_MARKET_LOOKBACK_INTERVALS", "1"))
    lookahead = int(os.getenv("BTC_MARKET_LOOKAHEAD_INTERVALS", "4"))
    candidates = _build_btc_15m_slug_candidates(lookback=lookback, lookahead=lookahead)
    if not candidates:
        return []

    try:
        existing = asyncio.run(_discover_existing_btc_15m_slugs(candidates))
    except Exception as e:
        logger.warning(f"Gamma discovery failed, using deterministic candidates: {e}")
        existing = []

    if existing:
        logger.info(f"Resolved BTC 15-min slugs from Gamma API: {existing}")
        return existing

    # Fallback to current + future intervals if API is unreachable.
    fallback = [s for s in candidates if int(s.rsplit("-", 1)[-1]) >= int(datetime.now(timezone.utc).timestamp()) - 900]
    logger.warning(f"No confirmed slugs from Gamma API; using fallback candidates: {fallback}")
    return fallback


def select_primary_btc_15m_slug(slugs: List[str]) -> Optional[str]:
    """
    Select the nearest current/future 15m BTC slug (single value).
    """
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


async def _fetch_gamma_market_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    # Attempt to fetch full event which contains eventMetadata.priceToBeat
    api_base = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")
    timeout = float(os.getenv("GAMMA_DISCOVERY_TIMEOUT_SEC", "8"))
    
    # Try fetching the event first
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{api_base}/events", params={"slug": slug})
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    event = data[0]
                    # Expose priceToBeat inside the first market for legacy compatibility if available
                    event_meta = event.get("eventMetadata", {}) or {}
                    if not isinstance(event_meta, dict):
                        event_meta = {}
                    if not event_meta and isinstance(event.get("event_metadata"), dict):
                        event_meta = event.get("event_metadata") or {}
                    ptb = event_meta.get("priceToBeat")
                    if ptb is None:
                        ptb = event_meta.get("price_to_beat")
                    if ptb is None:
                        ptb = event.get("priceToBeat")
                    if ptb is None:
                        ptb = event.get("price_to_beat")
                    markets = event.get("markets", [])
                    if markets and ptb is not None:
                        markets[0]["_priceToBeat"] = ptb
                    if markets:
                        return markets[0]
    except Exception as e:
        logger.debug(f"Fetch gamma event failed for slug={slug}: {e}")
        
    # Fallback to fetching market directly
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{api_base}/markets",
                # Keep this broad. 15m BTC markets can transiently fail strict active/closed filters.
                params={"slug": slug, "limit": 5},
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                for m in data:
                    if isinstance(m, dict) and str(m.get("slug", "") or "") == slug:
                        return m
                if isinstance(data[0], dict):
                    return data[0]
    except Exception as e:
        logger.error(f"Failed to fetch market from Gamma API for slug={slug}: {e}")
    return None


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
    return bool(re.fullmatch(r"\d{20,}", str(value or "").strip()))


async def _hydrate_gamma_market_details(market: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fetch /markets/{id} when list payload has incomplete token fields.
    """
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


def _extract_instrument_ids_from_gamma_market(market: Dict[str, Any]) -> List[InstrumentId]:
    """
    Build Nautilus InstrumentIds from one Gamma market payload.
    """
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
    """
    Resolve exact instrument IDs for a target BTC 15m slug.
    """
    try:
        market = asyncio.run(_fetch_gamma_market_by_slug(slug))
    except Exception as e:
        logger.warning(f"Failed to fetch Gamma market for slug {slug}: {e}")
        return []

    if not market:
        logger.warning(f"No Gamma market found for slug: {slug}")
        return []

    instrument_ids = _extract_instrument_ids_from_gamma_market(market)
    if not instrument_ids:
        try:
            hydrated = asyncio.run(_hydrate_gamma_market_details(market))
        except Exception:
            hydrated = market
        instrument_ids = _extract_instrument_ids_from_gamma_market(hydrated)
    if not instrument_ids:
        logger.warning(f"No instrument IDs extracted from slug: {slug}")
        return []
    return instrument_ids


def resolve_best_btc_15m_market(slugs: List[str]) -> tuple[Optional[str], List[InstrumentId]]:
    """
    Try candidate slugs in order and return the first slug with valid instrument IDs.
    """
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


def resolve_polymarket_auth() -> Optional[Dict[str, str]]:
    """
    Resolve credentials for Nautilus Polymarket clients.

    Priority:
    1) Direct L2 creds from environment (API_KEY/SECRET/PASSPHRASE)
    2) Derive/create L2 creds via PK (L1) using py-clob-client
    """
    private_key = os.getenv("POLYMARKET_PK")
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    passphrase = os.getenv("POLYMARKET_PASSPHRASE")
    funder = (
        os.getenv("POLYMARKET_FUNDER")
        or os.getenv("POLYMARKET_WALLET_ADDRESS")
        or os.getenv("WALLET_ADDRESS")
    )
    clob_host = os.getenv("POLYMARKET_CLOB_BASE_URL", "https://clob.polymarket.com")
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))

    # If user already provided full L2 creds, use them directly.
    if private_key and api_key and api_secret and passphrase:
        resolved_funder = funder or ""
        if not resolved_funder:
            try:
                from py_clob_client.client import ClobClient

                tmp_client = ClobClient(
                    host=clob_host,
                    key=private_key,
                    chain_id=chain_id,
                    signature_type=signature_type,
                )
                resolved_funder = tmp_client.get_address() or ""
            except Exception:
                resolved_funder = ""
        return {
            "private_key": private_key,
            "api_key": api_key,
            "api_secret": api_secret,
            "passphrase": passphrase,
            "funder": resolved_funder,
            "signature_type": str(signature_type),
        }

    if not private_key:
        logger.error("POLYMARKET_PK is required.")
        return None

    try:
        from py_clob_client.client import ClobClient
    except Exception as e:
        logger.error(f"py-clob-client not available for API credential derivation: {e}")
        return None

    try:
        kwargs: Dict[str, Any] = {
            "host": clob_host,
            "key": private_key,
            "chain_id": chain_id,
            "signature_type": signature_type,
        }
        if funder:
            kwargs["funder"] = funder
        client = ClobClient(**kwargs)
        derived = client.create_or_derive_api_creds()
        d_key = derived.api_key if hasattr(derived, "api_key") else derived.get("api_key")
        d_secret = derived.api_secret if hasattr(derived, "api_secret") else derived.get("api_secret")
        d_pass = derived.api_passphrase if hasattr(derived, "api_passphrase") else derived.get("api_passphrase")
        # Compatibility with alternative key names
        d_key = d_key or (derived.get("key") if isinstance(derived, dict) else None)
        d_secret = d_secret or (derived.get("secret") if isinstance(derived, dict) else None)
        d_pass = d_pass or (derived.get("passphrase") if isinstance(derived, dict) else None)

        resolved_funder = funder or (client.get_address() or "")
        if not (d_key and d_secret and d_pass):
            logger.error("Failed to derive complete Polymarket API credentials from private key.")
            return None

        logger.info("Polymarket API credentials derived from private key (L1 -> L2).")
        return {
            "private_key": private_key,
            "api_key": d_key,
            "api_secret": d_secret,
            "passphrase": d_pass,
            "funder": resolved_funder,
            "signature_type": str(signature_type),
        }
    except Exception as e:
        logger.error(f"Failed to derive Polymarket API credentials: {e}")
        return None


def run_preflight_checks(simulation: bool) -> bool:
    """
    Run safety checks before starting trading node.
    """
    logger.info("=" * 80)
    logger.info("PREFLIGHT CHECK START")
    logger.info("=" * 80)

    auth = resolve_polymarket_auth()
    if not auth:
        logger.error("Polymarket auth resolution failed.")
        return False

    slugs = resolve_btc_15m_market_slugs()
    if not slugs:
        logger.error("Preflight failed: no BTC 15-min market slugs resolved")
        return False
    logger.info(f"Preflight market slugs: {slugs}")

    primary_slug, instrument_ids = resolve_best_btc_15m_market(slugs)
    if not primary_slug:
        logger.error("Preflight failed: no primary BTC 15-min slug selected")
        return False
    if not instrument_ids:
        logger.error(f"Preflight failed: no instrument IDs resolved for slug {primary_slug}")
        return False
    logger.info(
        f"Preflight primary slug: {primary_slug}, instrument_ids: {[inst.value for inst in instrument_ids]}"
    )

    redis_client = init_redis()
    if redis_client:
        logger.info("Preflight Redis check: OK")
    else:
        logger.warning("Preflight Redis check: skipped/unavailable")

    mode_text = "SIMULATION" if simulation else "LIVE TRADING"
    logger.info(f"Preflight mode target: {mode_text}")
    logger.info("Polymarket auth check: OK")
    logger.info("PREFLIGHT CHECK PASSED")
    logger.info("=" * 80)
    return True

def init_redis():
    """Initialize Redis connection for simulation mode control."""
    try:
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        redis_password = os.getenv("REDIS_PASSWORD")
        redis_username = os.getenv("REDIS_USERNAME")
        redis_client = redis.Redis(
            host=redis_host,
            port=int(os.getenv('REDIS_PORT', 6379)),
            db=int(os.getenv('REDIS_DB', 2)),
            username=redis_username if redis_username else None,
            password=redis_password if redis_password else None,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_keepalive=True
        )
        redis_client.ping()
        if redis_host not in ("localhost", "127.0.0.1") and not redis_password:
            logger.warning("REDIS_HOST is remote and REDIS_PASSWORD is empty. This is unsafe.")
        logger.info("Redis connection established")
        return redis_client
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}")
        logger.warning("Simulation mode will be static (from .env)")
        return None


class MarketPhase(Enum):
    """Market lifecycle phases for BTC 15-min markets."""
    WAITING = "WAITING"           # No active market; searching for next one
    ACTIVE = "ACTIVE"             # Market is live, quoting is allowed
    REDUCE_ONLY = "REDUCE_ONLY"   # Close to market end, BUY blocked (tail SELL block optional)
    SETTLING = "SETTLING"         # Market has ended, all orders cancelled


class IntegratedBTCStrategy(Strategy):
    """
    Integrated BTC Strategy combining:
    - Nautilus trading framework
    - Our 7-phase system
    - Redis simulation control
    - Paper trading tracking
    - Auto-reload instruments every 12 minutes
    - Pre-loaded price history for immediate trading
    """
    
    def __init__(self, redis_client=None, enable_grafana=True, test_mode=False, selected_slug: Optional[str] = None):
        super().__init__()
        
        # Nautilus
        self.instrument_id = None
        self.redis_client = redis_client
        self.current_simulation_mode = True
        self.selected_slug = selected_slug
        
        # Phase 4: Signal Processors
        self.spike_detector = SpikeDetectionProcessor(
            spike_threshold=float(os.getenv('SPIKE_THRESHOLD', 0.15)),
            lookback_periods=20,
        )
        self.sentiment_processor = SentimentProcessor(
            extreme_fear_threshold=25,
            extreme_greed_threshold=75,
        )
        self.divergence_processor = PriceDivergenceProcessor(
            divergence_threshold=0.05,
        )
        
        # Phase 4: Signal Fusion
        self.fusion_engine = get_fusion_engine()
        
        # Phase 5: Risk Management
        self.risk_engine = get_risk_engine()
        
        # Phase 6: Performance Tracking
        self.performance_tracker = get_performance_tracker()
        
        # Phase 7: Learning Engine
        self.learning_engine = get_learning_engine()
        
        # Phase 6: Grafana (optional)
        if enable_grafana:
            self.grafana_exporter = get_grafana_exporter()
        else:
            self.grafana_exporter = None
        
        # Price history for signal processing
        self.price_history = []
        self.max_history = 100
        self.real_price_history: List[Decimal] = []
        self.real_price_history_by_inst: Dict[str, List[Decimal]] = {}
        self.max_real_history = int(os.getenv("MAKER_VOL_REAL_HISTORY_MAX", "300"))
        
        # The PaperTrade list is now maintained inside SimAdapter
        
        # Last trading decision time (to prevent multiple trades per interval)
        self.last_trade_time = 0
        
        # Last instrument reload time
        self.last_reload_time = 0

        self.test_mode = test_mode
        self.maker_mode = os.getenv("MAKER_MODE", "1").strip().lower() not in ("0", "false", "no")
        self.quote_refresh_sec = int(os.getenv("MAKER_QUOTE_REFRESH_SEC", "5"))
        self.maker_half_spread = Decimal(os.getenv("MAKER_HALF_SPREAD", "0.01"))
        self.maker_quote_size_usdc = Decimal(os.getenv("MAKER_QUOTE_SIZE_USDC", "1.0"))
        self.maker_min_shares = Decimal(os.getenv("MAKER_MIN_SHARES", "5"))
        self.maker_exchange_min_shares = Decimal(os.getenv("MAKER_EXCHANGE_MIN_SHARES", "5"))
        self.maker_fixed_shares = Decimal(os.getenv("MAKER_FIXED_SHARES", "0"))
        self.maker_quote_sides = os.getenv("MAKER_QUOTE_SIDES", "both").strip().lower()
        if self.maker_quote_sides not in {"both", "buy", "sell", "both_buy"}:
            self.maker_quote_sides = "both"
        self.maker_min_expected_net_usdc = Decimal(os.getenv("MAKER_MIN_EXPECTED_NET_USDC", "0.0001"))
        self.maker_adverse_selection_buffer = Decimal(os.getenv("MAKER_ADVERSE_SELECTION_BUFFER", "0.0005"))
        self.maker_use_post_only = os.getenv("MAKER_POST_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")
        self.maker_post_only_strict = os.getenv("MAKER_POST_ONLY_STRICT", "1").strip().lower() not in ("0", "false", "no")
        self.maker_max_inventory_shares = Decimal(os.getenv("MAKER_MAX_INVENTORY_SHARES", "25"))
        self.maker_kill_switch_reset_on_rollover = os.getenv("MAKER_KILL_SWITCH_RESET_ON_ROLLOVER", "1").strip().lower() not in ("0", "false", "no")
        self.maker_inventory_skew_max = Decimal(os.getenv("MAKER_INVENTORY_SKEW_MAX", "0.03"))
        self.maker_stale_inventory_sec = int(os.getenv("MAKER_STALE_INVENTORY_SEC", "30"))
        self.maker_stale_inventory_multiplier = Decimal(os.getenv("MAKER_STALE_INVENTORY_MULTIPLIER", "2.0"))
        self.maker_vol_stressed_threshold = Decimal(os.getenv("MAKER_VOL_STRESSED_THRESHOLD", "0.015"))
        self.maker_vol_extreme_threshold = Decimal(os.getenv("MAKER_VOL_EXTREME_THRESHOLD", "0.08"))
        self.maker_vol_stressed_spread_mult = Decimal(os.getenv("MAKER_VOL_STRESSED_SPREAD_MULT", "2.0"))
        self.maker_vol_stressed_size_mult = Decimal(os.getenv("MAKER_VOL_STRESSED_SIZE_MULT", "0.5"))
        self.maker_vol_extreme_spread_mult = Decimal(os.getenv("MAKER_VOL_EXTREME_SPREAD_MULT", "3.0"))
        
        # --- Phase 3: Active Quoting Parameters ---
        self.maker_pennying_enabled = os.getenv("MAKER_PENNYING_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
        self.maker_pennying_min_edge = Decimal(os.getenv("MAKER_PENNYING_MIN_EDGE", "0.005"))
        self.maker_requote_max_per_sec = float(os.getenv("MAX_REQUOTE_PER_SEC", "1.0"))
        self.maker_requote_hysteresis_ticks = Decimal(os.getenv("REQUOTE_HYSTERESIS_TICKS", "1"))
        self.maker_execution_penalty_enable = os.getenv("MAKER_EXECUTION_PENALTY_ENABLE", "1").strip().lower() not in ("0", "false", "no")
        self.maker_execution_penalty_floor_usdc = Decimal(os.getenv("MAKER_EXECUTION_PENALTY_FLOOR_USDC", "0.001"))
        self.maker_execution_slippage_spread_mult = Decimal(os.getenv("MAKER_EXECUTION_SLIPPAGE_SPREAD_MULT", "0.15"))
        self.maker_execution_non_atomic_vol_mult = Decimal(os.getenv("MAKER_EXECUTION_NON_ATOMIC_VOL_MULT", "0.2"))
        self.maker_execution_depth_impact_mult = Decimal(os.getenv("MAKER_EXECUTION_DEPTH_IMPACT_MULT", "1.0"))
        self.maker_execution_vwap_mult = Decimal(os.getenv("MAKER_EXECUTION_VWAP_MULT", "0.5"))
        self.orderbook_fetch_interval_sec = max(1, int(os.getenv("ORDERBOOK_FETCH_INTERVAL_SEC", "5")))
        self.orderbook_levels_limit = max(1, int(os.getenv("ORDERBOOK_LEVELS_LIMIT", "10")))
        self.requote_bucket_tokens = self.maker_requote_max_per_sec
        self.requote_bucket_last_refill = time.time()
        self.maker_vol_warmup_quotes = int(os.getenv("MAKER_VOL_WARMUP_QUOTES", "30"))
        self.maker_vol_return_clip = Decimal(os.getenv("MAKER_VOL_RETURN_CLIP", "0.20"))
        self.maker_vol_rolling_window = int(os.getenv("MAKER_VOL_ROLLING_WINDOW", "30"))
        self.maker_vol_ewma_alpha = float(os.getenv("MAKER_VOL_EWMA_ALPHA", "0.35"))
        self.maker_max_consecutive_denied = int(os.getenv("MAKER_MAX_CONSECUTIVE_DENIED", "5"))
        self.maker_order_ttl_sec = int(os.getenv("MAKER_ORDER_TTL_SEC", "20"))
        self.maker_balance_pause_sec = int(os.getenv("MAKER_BALANCE_PAUSE_SEC", "60"))
        self.maker_error_pause_sec = int(os.getenv("MAKER_ERROR_PAUSE_SEC", "30"))
        
        # Reduce Only Mode Protections
        self.maker_min_minutes_to_close = float(os.getenv("MAKER_MIN_MINUTES_TO_CLOSE", "3.0"))
        self.maker_min_fair_price = Decimal(os.getenv("MAKER_MIN_FAIR_PRICE", "0.05"))
        self.maker_max_fair_price = Decimal(os.getenv("MAKER_MAX_FAIR_PRICE", "0.95"))
        self.maker_reduce_only_no_new_sell_last_sec = max(
            0,
            int(os.getenv("MAKER_REDUCE_ONLY_NO_NEW_SELL_LAST_SEC", "45")),
        )
        self.maker_fee_rate_default_decimal = Decimal(
            os.getenv("MAKER_FEE_RATE_DEFAULT_DECIMAL", str(CRYPTO_FEE_CURVE.fee_rate))
        )
        if self.maker_fee_rate_default_decimal <= 0:
            self.maker_fee_rate_default_decimal = CRYPTO_FEE_CURVE.fee_rate
        self.maker_fee_rate_legacy_bps_default = int(os.getenv("MAKER_FEE_RATE_BPS_DEFAULT", "0"))
        self.maker_fee_rate_bps_default = int(
            (self.maker_fee_rate_default_decimal * Decimal("10000")).quantize(Decimal("1"))
        )
        self.maker_max_order_usdc = Decimal(os.getenv("MAKER_MAX_ORDER_USDC", "1.0"))
        self.maker_auto_tune = os.getenv("MAKER_AUTO_TUNE", "0") == "1"
        self.maker_auto_tune_interval_sec = int(os.getenv("MAKER_AUTO_TUNE_INTERVAL_SEC", "300"))
        
        self.maker_momentum_filter_pct = Decimal(os.getenv("MAKER_MOMENTUM_FILTER_PCT", "0.06"))
        self.maker_momentum_window_ticks = int(os.getenv("MAKER_MOMENTUM_WINDOW_TICKS", "20"))
        self.maker_fair_pricer_mode = os.getenv("MAKER_FAIR_PRICER_MODE", "drift").strip().lower()
        if self.maker_fair_pricer_mode not in {"drift", "digital"}:
            self.maker_fair_pricer_mode = "drift"
        self.maker_digital_vol_window = max(10, int(os.getenv("MAKER_DIGITAL_VOL_WINDOW", "120")))
        self.maker_digital_vol_min_points = max(5, int(os.getenv("MAKER_DIGITAL_VOL_MIN_POINTS", "20")))
        self.maker_digital_sigma_default = Decimal(os.getenv("MAKER_DIGITAL_SIGMA_DEFAULT", "0.60"))
        self.maker_digital_sigma_floor = Decimal(os.getenv("MAKER_DIGITAL_SIGMA_FLOOR", "0.20"))
        self.maker_digital_sigma_ceiling = Decimal(os.getenv("MAKER_DIGITAL_SIGMA_CEILING", "2.00"))
        self.maker_digital_vol_scale = Decimal(os.getenv("MAKER_DIGITAL_VOL_SCALE", "1.00"))
        self.taker_exit_enabled = os.getenv("TAKER_EXIT_ENABLED", "1").strip().lower() not in ("0", "false", "no")
        self.taker_exit_min_net_usdc = Decimal(os.getenv("TAKER_EXIT_MIN_NET_USDC", "0.02"))
        self.taker_exit_stop_loss_usdc = Decimal(os.getenv("TAKER_EXIT_STOP_LOSS_USDC", "0.15"))
        self.taker_exit_max_hold_sec = int(os.getenv("TAKER_EXIT_MAX_HOLD_SEC", "120"))
        self.taker_exit_min_hold_sec = int(os.getenv("TAKER_EXIT_MIN_HOLD_SEC", "20"))
        self.taker_exit_cooldown_sec = int(os.getenv("TAKER_EXIT_COOLDOWN_SEC", "8"))
        self.taker_exit_slippage_buffer_pct = Decimal(os.getenv("TAKER_EXIT_SLIPPAGE_BUFFER_PCT", "0.002"))
        self.taker_exit_only_on_profit = os.getenv("TAKER_EXIT_ONLY_ON_PROFIT", "0").strip().lower() in ("1", "true", "yes", "on")
        self.taker_exit_disable_stop_loss_last_sec = max(
            0,
            int(os.getenv("TAKER_EXIT_DISABLE_STOP_LOSS_LAST_SEC", "45")),
        )
        
        # Performance / Execution
        self.maker_cancel_max_retries = int(os.getenv("MAKER_CANCEL_MAX_RETRIES", "3"))
        self.maker_cancel_cooldown_sec = int(os.getenv("MAKER_CANCEL_COOLDOWN_SEC", "2"))
        self.maker_cancel_ack_timeout_sec = int(os.getenv("MAKER_CANCEL_ACK_TIMEOUT_SEC", "8"))
        self.maker_simulation_shadow = os.getenv("MAKER_SIMULATION_SHADOW", "1").strip().lower() not in ("0", "false", "no")
        self.maker_sim_eval_sec = int(os.getenv("MAKER_SIM_EVAL_SEC", "60"))
        self.sim_ack_latency_ms_min = int(os.getenv("SIM_ACK_LATENCY_MS_MIN", "120"))
        self.sim_ack_latency_ms_max = int(os.getenv("SIM_ACK_LATENCY_MS_MAX", "800"))
        self.sim_cancel_latency_ms_min = int(os.getenv("SIM_CANCEL_LATENCY_MS_MIN", "80"))
        self.sim_cancel_latency_ms_max = int(os.getenv("SIM_CANCEL_LATENCY_MS_MAX", "500"))
        self.sim_fill_base_prob = float(os.getenv("SIM_FILL_BASE_PROB", "0.08"))
        self.sim_fill_edge_boost = float(os.getenv("SIM_FILL_EDGE_BOOST", "0.30"))
        self.sim_fill_queue_penalty = float(os.getenv("SIM_FILL_QUEUE_PENALTY", "0.45"))
        self.sim_fill_age_bonus_max = float(os.getenv("SIM_FILL_AGE_BONUS_MAX", "0.25"))
        self.sim_fill_age_to_max_sec = max(1, int(os.getenv("SIM_FILL_AGE_TO_MAX_SEC", "25")))
        self.sim_partial_fill_min_ratio = float(os.getenv("SIM_PARTIAL_FILL_MIN_RATIO", "0.2"))
        self.sim_partial_fill_max_ratio = float(os.getenv("SIM_PARTIAL_FILL_MAX_RATIO", "1.0"))
        self.quote_healthcheck_interval_sec = int(os.getenv("QUOTE_HEALTHCHECK_INTERVAL_SEC", "10"))
        self.strategy_status_interval_sec = max(10, int(os.getenv("STRATEGY_STATUS_INTERVAL_SEC", "30")))
        self.quote_stale_sec = int(os.getenv("QUOTE_STALE_SEC", "30"))
        self.quote_invalid_tick_reload_threshold = int(os.getenv("QUOTE_INVALID_TICK_RELOAD_THRESHOLD", "80"))
        self.quote_reload_cooldown_sec = int(os.getenv("QUOTE_RELOAD_COOLDOWN_SEC", "60"))
        self.last_quote_update_ts = 0.0
        self.quote_pause_until_ts = 0.0
        self.last_simulation_guard_log_ts = 0.0
        self.last_valid_quote_ts = 0.0
        self.consecutive_invalid_quote_ticks = 0
        self.last_quote_watchdog_check_ts = 0.0
        self.last_quote_watchdog_reload_ts = 0.0

        # --- New Engines Init ---
        maker_config = MakerEngineConfig(
            maker_half_spread=self.maker_half_spread,
            maker_quote_size_usdc=self.maker_quote_size_usdc,
            maker_adverse_selection_buffer=self.maker_adverse_selection_buffer,
            maker_min_expected_net_usdc=self.maker_min_expected_net_usdc,
            maker_quote_sides=self.maker_quote_sides,
            maker_inventory_skew_max=self.maker_inventory_skew_max,
            maker_max_inventory_shares=self.maker_max_inventory_shares,
            maker_stale_inventory_sec=self.maker_stale_inventory_sec,
            maker_stale_inventory_multiplier=self.maker_stale_inventory_multiplier,
            maker_vol_stressed_threshold=self.maker_vol_stressed_threshold,
            maker_vol_extreme_threshold=self.maker_vol_extreme_threshold,
            maker_vol_stressed_spread_mult=self.maker_vol_stressed_spread_mult,
            maker_vol_stressed_size_mult=self.maker_vol_stressed_size_mult,
            maker_vol_extreme_spread_mult=self.maker_vol_extreme_spread_mult,
            maker_pennying_enabled=self.maker_pennying_enabled,
            maker_pennying_min_edge=self.maker_pennying_min_edge,
            maker_execution_penalty_enable=self.maker_execution_penalty_enable,
            maker_execution_penalty_floor_usdc=self.maker_execution_penalty_floor_usdc,
            maker_execution_slippage_spread_mult=self.maker_execution_slippage_spread_mult,
            maker_execution_non_atomic_vol_mult=self.maker_execution_non_atomic_vol_mult,
            maker_execution_depth_impact_mult=self.maker_execution_depth_impact_mult,
            maker_execution_vwap_mult=self.maker_execution_vwap_mult,
        )
        self.maker_engine = MakerEngine(maker_config)

        sim_config = SimAdapterConfig(
            sim_fill_base_prob=self.sim_fill_base_prob,
            sim_fill_edge_boost=self.sim_fill_edge_boost,
            sim_fill_queue_penalty=self.sim_fill_queue_penalty,
            sim_fill_age_bonus_max=self.sim_fill_age_bonus_max,
            sim_fill_age_to_max_sec=self.sim_fill_age_to_max_sec,
            sim_partial_fill_min_ratio=self.sim_partial_fill_min_ratio,
            sim_partial_fill_max_ratio=self.sim_partial_fill_max_ratio,
            maker_sim_eval_sec=self.maker_sim_eval_sec,
            maker_fee_rate_bps_default=self.maker_fee_rate_bps_default,
        )
        self.sim_adapter = SimAdapter(sim_config)
        # Backward-compatible alias: legacy code paths still use self.paper_trades.
        self.paper_trades = self.sim_adapter.paper_trades

        self.last_status_log_ts = 0.0
        self.orderbook_unavailable_until_ts = 0.0
        self.orderbook_unavailable_token: Optional[str] = None
        self.last_external_spot: Optional[Decimal] = None
        self.latest_external_spot: Optional[Decimal] = None
        self.external_spot_history: List[Tuple[float, Decimal]] = []
        self.external_spot_history_max = max(60, int(os.getenv("EXTERNAL_SPOT_HISTORY_MAX", "1200")))
        self.market_strike_cache_by_slug: Dict[str, Decimal] = {}
        self.market_strike_last_fetch_ts_by_slug: Dict[str, float] = {}
        self.market_strike_fetch_cooldown_sec = max(10, int(os.getenv("MARKET_STRIKE_FETCH_COOLDOWN_SEC", "300")))
        self.strike_fallback_log_interval_sec = max(10, int(os.getenv("STRIKE_FALLBACK_LOG_INTERVAL_SEC", "60")))
        self._last_strike_fallback_log_ts = 0.0
        self._last_digital_pricer_log_ts = 0.0
        self.live_inventory_cost: Dict[str, Dict[str, Any]] = {}
        self.last_taker_exit_ts_by_inst: Dict[str, float] = {}
        self.pending_taker_exit_by_inst: Dict[str, str] = {}
        self.taker_exit_tail_attempted_by_inst: Dict[str, float] = {}
        self.fee_log_interval_sec = max(5, int(os.getenv("FEE_LOG_INTERVAL_SEC", "60")))
        self._last_fee_log_state_by_token: Dict[str, Dict[str, Any]] = {}
        self.fee_rate_fetch_interval_sec = max(
            5,
            int(os.getenv("FEE_RATE_FETCH_INTERVAL_SEC", os.getenv("FEE_RATE_CACHE_TTL_SEC", "300"))),
        )
        self._fee_rate_local_cache_by_token: Dict[str, Dict[str, Any]] = {}
        self.latest_market_bid: Optional[Decimal] = None
        self.latest_market_ask: Optional[Decimal] = None
        self.latest_quote_depth_by_inst: Dict[str, Tuple[Optional[Decimal], Optional[Decimal]]] = {}
        self.orderbook_levels_cache_by_token: Dict[str, Dict[str, Any]] = {}
        self._inventory_delta_shares = Decimal("0")
        self.inventory_last_update_ts = 0.0
        self.consecutive_denied_orders = 0
        self.maker_kill_switch = False
        self.active_maker_orders: Dict[str, Any] = {}
        self.sim_maker_positions: List[Dict[str, Any]] = []
        self.sim_maker_closed_total = 0
        self.sim_maker_closed_wins = 0
        self.current_token_id: Optional[str] = None
        self.current_market_instruments: List[InstrumentId] = []
        self.last_observed_fee_rate_bps: Optional[int] = None
        clob_base = os.getenv("POLYMARKET_CLOB_BASE_URL", "https://clob.polymarket.com")
        fee_ttl = int(os.getenv("FEE_RATE_CACHE_TTL_SEC", "300"))
        self.fee_rate_client = FeeRateClient(base_url=clob_base, ttl_sec=fee_ttl)
        self.rebate_reporter = RebateReporter(output_dir=os.getenv("REBATE_REPORT_DIR", "./logs/rebate"))
        self.auto_tune_enabled = os.getenv("MAKER_AUTO_TUNE", "1").strip().lower() not in ("0", "false", "no")
        self.auto_tune_interval_sec = int(os.getenv("MAKER_AUTO_TUNE_INTERVAL_SEC", "300"))
        self.last_auto_tune_ts = 0.0
        self.parameter_tuner = ParameterTuner()
        self._stopping = False
        self._reload_stop_event = threading.Event()
        self._reload_thread: Optional[threading.Thread] = None
        self._quote_watchdog_stop_event = threading.Event()
        self._quote_watchdog_thread: Optional[threading.Thread] = None
        self.auto_redeem_enabled = os.getenv("AUTO_REDEEM_ENABLED", "0").strip().lower() not in ("0", "false", "no")
        self.auto_redeem_apply = os.getenv("AUTO_REDEEM_APPLY", "0").strip().lower() not in ("0", "false", "no")
        self.auto_redeem_interval_sec = max(120, int(os.getenv("AUTO_REDEEM_INTERVAL_SEC", "900")))
        self.auto_redeem_on_rollover = os.getenv("AUTO_REDEEM_ON_ROLLOVER", "1").strip().lower() not in ("0", "false", "no")
        self.auto_redeem_timeout_sec = max(30, int(os.getenv("AUTO_REDEEM_TIMEOUT_SEC", "180")))
        self.auto_redeem_slug_filter = os.getenv("AUTO_REDEEM_SLUG_FILTER", "btc-updown-15m").strip()
        self._redeem_stop_event = threading.Event()
        self._redeem_thread: Optional[threading.Thread] = None
        self._redeem_job_lock = threading.Lock()
        self._last_redeem_run_ts = 0.0
        self.current_market_slug: Optional[str] = None
        # --- Market Lifecycle State Machine ---
        self.market_phase = MarketPhase.WAITING
        self.market_settling_grace_sec = max(1, int(os.getenv("MARKET_SETTLING_GRACE_SEC", "15")))
        self.market_next_poll_sec = max(5, int(os.getenv("MARKET_NEXT_POLL_SEC", "15")))
        self._market_settling_since_ts: float = 0.0
        self.next_market_slug: Optional[str] = None
        self.next_market_start_ts: Optional[float] = None
        self._lifecycle_stop_event = threading.Event()
        self._lifecycle_thread: Optional[threading.Thread] = None
        # --- Balance Pre-check ---
        self._cached_usdc_balance: Optional[Decimal] = None
        self._balance_last_check_ts: float = 0.0
        self.balance_check_interval_sec = max(10, int(os.getenv("MAKER_BALANCE_CHECK_INTERVAL_SEC", "30")))
        self.conditional_balance_check_interval_sec = max(
            2, int(os.getenv("CONDITIONAL_BALANCE_CHECK_INTERVAL_SEC", "8"))
        )
        self.conditional_balance_safety_buffer_pct = max(
            Decimal("0"),
            min(Decimal("0.05"), Decimal(os.getenv("CONDITIONAL_BALANCE_SAFETY_BUFFER_PCT", "0.001"))),
        )
        self._conditional_balance_cache_by_token: Dict[str, Dict[str, Any]] = {}
        self._sell_reject_pause_until_by_inst: Dict[str, float] = {}

        # --- Binance WebSocket for real-time BTC price ---
        self._binance_ws_price: Optional[Decimal] = None
        self._binance_ws_price_ts: float = 0.0
        self._binance_ws_stop_event = threading.Event()
        self._binance_ws_thread: Optional[threading.Thread] = None
        self._maker_worker_lock = threading.Lock()
        self._decision_worker_lock = threading.Lock()
        self._maker_worker_running = False
        self._decision_worker_running = False
        self.run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        self.trade_db_enabled = os.getenv("TRADE_DB_ENABLED", "1").strip().lower() not in ("0", "false", "no")
        self.trade_db = TradeJournalDB(
            db_path=os.getenv("TRADE_DB_PATH", "./logs/trade_journal.db"),
        ) if self.trade_db_enabled else None

        if test_mode:
            logger.info("=" * 80)
            logger.info("⚠️  TEST MODE ACTIVE - Trading every minute!")
            logger.info("=" * 80)
        
        logger.info("=" * 80)
        logger.info("INTEGRATED BTC STRATEGY INITIALIZED")
        logger.info("  Phase 4: Signal processors ready")
        logger.info("  Phase 5: Risk engine ready")
        logger.info("  Phase 6: Performance tracking ready")

    @property
    def inventory_delta_shares(self) -> Decimal:
        return self._inventory_delta_shares

    @inventory_delta_shares.setter
    def inventory_delta_shares(self, value: Decimal):
        old_val = getattr(self, "_inventory_delta_shares", Decimal("0"))
        if value != old_val:
            self.inventory_last_update_ts = time.time()
            self._inventory_delta_shares = value

    def _log_strategy_config_summary(self) -> None:
        logger.info("  Phase 7: Learning engine ready")
        logger.info("  $1 per trade maximum")
        logger.info("  Reloads instruments every 12 minutes")
        logger.info(f"  Maker mode: {'ON' if self.maker_mode else 'OFF'}")
        logger.info(f"  Maker quote sides: {self.maker_quote_sides.upper()}")
        logger.info(f"  Maker post-only flag: {'ON' if self.maker_use_post_only else 'OFF'}")
        logger.info(f"  Maker post-only strict: {'ON' if self.maker_post_only_strict else 'OFF'}")
        logger.info(f"  Maker auto-tune: {'ON' if self.auto_tune_enabled else 'OFF'}")
        logger.info(f"  Maker max order USDC: ${float(self.maker_max_order_usdc):.2f}")
        logger.info(
            f"  Maker fixed shares: "
            f"{float(self.maker_fixed_shares):.6f}" if self.maker_fixed_shares > 0 else "  Maker fixed shares: OFF"
        )
        logger.info(f"  Exchange min shares: {float(self.maker_exchange_min_shares):.6f}")
        logger.info(f"  Maker cancel max retries: {self.maker_cancel_max_retries}")
        logger.info(f"  Maker reduce-only time cutoff (min): {self.maker_min_minutes_to_close}")
        logger.info(f"  Maker reduce-only no-new-SELL tail: {self.maker_reduce_only_no_new_sell_last_sec}s")
        logger.info(f"  Maker min fair price (buy floor): {self.maker_min_fair_price}")
        logger.info(f"  Maker max fair price (buy ceiling): {self.maker_max_fair_price}")
        logger.info(
            "  Conditional balance guard: "
            f"interval={self.conditional_balance_check_interval_sec}s "
            f"buffer={float(self.conditional_balance_safety_buffer_pct)*100:.2f}%"
        )
        logger.info(f"  Maker simulation shadow: {'ON' if self.maker_simulation_shadow else 'OFF'}")
        logger.info(f"  Maker fair pricer mode: {self.maker_fair_pricer_mode}")
        logger.info(f"  Execution penalty model: {'ON' if self.maker_execution_penalty_enable else 'OFF'}")
        logger.info(
            "  Execution penalty params: "
            f"floor={float(self.maker_execution_penalty_floor_usdc):.6f} "
            f"spread_mult={float(self.maker_execution_slippage_spread_mult):.4f} "
            f"non_atomic_vol_mult={float(self.maker_execution_non_atomic_vol_mult):.4f} "
            f"depth_impact_mult={float(self.maker_execution_depth_impact_mult):.4f} "
            f"vwap_mult={float(self.maker_execution_vwap_mult):.4f}"
        )
        logger.info(f"  Taker exit: {'ON' if self.taker_exit_enabled else 'OFF'}")
        logger.info(f"  Fee-rate fetch interval: {self.fee_rate_fetch_interval_sec}s")
        if self.taker_exit_enabled:
            logger.info(
                "  Taker exit config: "
                f"min_net={float(self.taker_exit_min_net_usdc):.4f} "
                f"stop_loss={float(self.taker_exit_stop_loss_usdc):.4f} "
                f"min_hold={self.taker_exit_min_hold_sec}s "
                f"max_hold={self.taker_exit_max_hold_sec}s "
                f"cooldown={self.taker_exit_cooldown_sec}s "
                f"disable_stop_loss_last={self.taker_exit_disable_stop_loss_last_sec}s"
            )
        logger.info(f"  Maker fee default decimal: {float(self.maker_fee_rate_default_decimal):.6f}")
        logger.info(f"  Auto redeem: {'ON' if self.auto_redeem_enabled else 'OFF'}")
        if self.auto_redeem_enabled:
            logger.info(
                "  Auto redeem config: "
                f"interval={self.auto_redeem_interval_sec}s "
                f"apply={'ON' if self.auto_redeem_apply else 'OFF'} "
                f"on_rollover={'ON' if self.auto_redeem_on_rollover else 'OFF'} "
                f"slug_filter={self.auto_redeem_slug_filter or '(none)'}"
            )
        logger.info(
            "  Quote watchdog: "
            f"stale={self.quote_stale_sec}s invalid_threshold={self.quote_invalid_tick_reload_threshold} "
            f"cooldown={self.quote_reload_cooldown_sec}s"
        )
        logger.info(f"  Trade DB: {'ON' if self.trade_db_enabled else 'OFF'}")
        logger.info("=" * 80)

    def _db_strategy_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        if not self.trade_db:
            return
        self.trade_db.log_strategy_event(
            run_id=self.run_id,
            event_type=event_type,
            payload=payload or {},
        )

    def _db_order_event(
        self,
        event_type: str,
        client_order_id: Optional[str] = None,
        venue_order_id: Optional[str] = None,
        side: Optional[str] = None,
        price: Optional[float] = None,
        qty: Optional[float] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        commission_usdc: Optional[float] = None,
        expected_net_usdc: Optional[float] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.trade_db:
            return
        self.trade_db.log_order_event(
            run_id=self.run_id,
            event_type=event_type,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            side=side,
            price=price,
            qty=qty,
            status=status,
            reason=reason,
            instrument_id=str(self.instrument_id) if self.instrument_id else None,
            token_id=self.current_token_id,
            fee_rate_bps=self.last_observed_fee_rate_bps,
            expected_net_usdc=expected_net_usdc,
            commission_usdc=commission_usdc,
            payload=payload or {},
        )

    def _increment_order_metric(self, status: str) -> None:
        """
        Increment order counters on Grafana exporter when available.
        """
        if self.grafana_exporter and hasattr(self.grafana_exporter, "increment_order_counter"):
            try:
                self.grafana_exporter.increment_order_counter(status)
            except Exception as e:
                logger.debug(f"Failed to increment order metric [{status}]: {e}")

    def _init_live_prom_metrics(self) -> None:
        """Initialize Prometheus gauges/counters for live trading metrics."""
        try:
            from prometheus_client import Gauge, Counter
            self._prom_live_pnl = Gauge('trading_live_realized_pnl', 'Cumulative realized PnL from live trades (USDC)')
            self._prom_live_trades = Counter('trading_live_trades_total', 'Total live trades (position round-trips)')
            self._prom_live_wins = Counter('trading_live_winning_trades', 'Live winning trades')
            self._prom_live_losses = Counter('trading_live_losing_trades', 'Live losing trades')
            self._prom_live_win_rate = Gauge('trading_live_win_rate', 'Live win rate percentage')
            self._prom_live_open_pos = Gauge('trading_live_open_positions', 'Number of open positions')
            self._prom_live_inventory = Gauge('trading_live_inventory_shares', 'Current inventory in shares')
            self._live_cumulative_pnl = 0.0
            self._live_total_trades = 0
            self._live_total_wins = 0
            self._prom_live_metrics_ok = True
            logger.info("✓ Live Prometheus trading metrics initialized")
        except Exception as e:
            logger.debug(f"Failed to init live prom metrics: {e}")
            self._prom_live_metrics_ok = False

    def _push_position_closed_to_prometheus(self, realized_pnl: float, duration_ns: int) -> None:
        """Push a completed round-trip trade to Prometheus metrics."""
        if not getattr(self, '_prom_live_metrics_ok', False):
            return
        try:
            self._live_cumulative_pnl += realized_pnl
            self._live_total_trades += 1
            won = realized_pnl > 0
            if won:
                self._live_total_wins += 1
                self._prom_live_wins.inc()
            else:
                self._prom_live_losses.inc()
            self._prom_live_trades.inc()
            self._prom_live_pnl.set(self._live_cumulative_pnl)
            win_rate = (self._live_total_wins / self._live_total_trades * 100) if self._live_total_trades > 0 else 0
            self._prom_live_win_rate.set(win_rate)

            # Also push to grafana_exporter if available
            if self.grafana_exporter:
                try:
                    self.grafana_exporter.increment_trade_counter(won=won)
                    dur_sec = duration_ns / 1e9 if duration_ns else 0
                    if dur_sec > 0:
                        self.grafana_exporter.record_trade_duration(dur_sec)
                    # Update PnL gauge exposed by exporter
                    self.grafana_exporter.total_pnl.set(self._live_cumulative_pnl)
                    self.grafana_exporter.win_rate.set(win_rate)
                except Exception:
                    pass

            logger.info(
                f"📊 Prometheus: trade #{self._live_total_trades} pnl={realized_pnl:+.4f} "
                f"cum_pnl={self._live_cumulative_pnl:+.4f} win_rate={win_rate:.0f}%"
            )
        except Exception as e:
            logger.debug(f"Failed to push position metrics: {e}")

    def _update_inventory_metric(self) -> None:
        """Update the inventory gauge in Prometheus."""
        if not getattr(self, '_prom_live_metrics_ok', False):
            return
        try:
            self._prom_live_inventory.set(float(self.inventory_delta_shares))
        except Exception:
            pass
    
    async def check_simulation_mode(self) -> bool:
        """Check Redis for current simulation mode."""
        # Safety invariant: test mode must never place real orders.
        if self.test_mode:
            self.current_simulation_mode = True
            return True

        if not self.redis_client:
            return self.current_simulation_mode
        
        try:
            sim_mode = self.redis_client.get('btc_trading:simulation_mode')
            if sim_mode is not None:
                redis_simulation = sim_mode == '1'
                
                if redis_simulation != self.current_simulation_mode:
                    self.current_simulation_mode = redis_simulation
                    mode_text = "SIMULATION" if redis_simulation else "LIVE TRADING"
                    logger.warning(f"Trading mode changed to: {mode_text}")
                    
                    if not redis_simulation:
                        logger.warning("LIVE TRADING ACTIVE - Real money at risk!")
                
                return redis_simulation
        except Exception as e:
            logger.warning(f"Failed to check Redis simulation mode: {e}")
        
        return self.current_simulation_mode

    # ------------------------------------------------------------------
    # Binance WebSocket for real-time BTC price
    # ------------------------------------------------------------------

    def _start_binance_ws(self) -> None:
        """Start a background thread that streams BTC price from Binance WebSocket."""
        if self._binance_ws_thread is not None and self._binance_ws_thread.is_alive():
            return
        self._binance_ws_stop_event.clear()
        self._binance_ws_thread = threading.Thread(
            target=self._binance_ws_loop,
            name="binance-ws",
            daemon=True,
        )
        self._binance_ws_thread.start()
        logger.info("Binance WebSocket thread started")

    def _binance_ws_loop(self) -> None:
        """
        Persistent WebSocket connection to Binance Futures for BTC/USDT aggTrade.
        Per Binance docs:
        - Base URL: wss://fstream.binance.com
        - Stream: /ws/btcusdt@aggTrade
        - Connection valid for max 24 hours → reconnect at 23h
        - Server pings every 3 min; must pong within 10 min
        - Max 10 incoming messages/sec
        """
        import websockets.sync.client as ws_sync  # type: ignore

        url = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
        reconnect_delay = 1.0
        max_reconnect_delay = 30.0
        max_connection_sec = 23 * 3600  # Reconnect before 24h limit
        pong_interval_sec = 120  # Send unsolicited pong every 2 min

        while not self._binance_ws_stop_event.is_set():
            try:
                with ws_sync.connect(
                    url,
                    close_timeout=5,
                    ping_interval=None,    # We handle pong manually
                    ping_timeout=None,
                ) as ws:
                    reconnect_delay = 1.0  # reset on success
                    connect_ts = time.time()
                    last_pong_ts = connect_ts
                    logger.info("✓ Binance Futures WS connected (btcusdt@aggTrade)")
                    while not self._binance_ws_stop_event.is_set():
                        # Check 24h reconnect limit
                        now = time.time()
                        if now - connect_ts > max_connection_sec:
                            logger.info("Binance WS: 23h limit reached, reconnecting...")
                            break

                        # Send unsolicited pong every 2 min to keep alive
                        if now - last_pong_ts > pong_interval_sec:
                            try:
                                ws.pong()
                                last_pong_ts = now
                            except Exception:
                                break

                        try:
                            raw = ws.recv(timeout=5)
                        except TimeoutError:
                            continue

                        try:
                            import json as _json
                            data = _json.loads(raw)
                            # aggTrade payload: {"p": "96123.45", "q": "0.1", ...}
                            price_str = data.get("p")
                            if price_str:
                                self._binance_ws_price = Decimal(price_str)
                                self._binance_ws_price_ts = time.time()
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"Binance WS error: {e}; reconnect in {reconnect_delay:.0f}s")
                self._binance_ws_stop_event.wait(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

    async def _fetch_external_spot_price(self) -> Optional[Decimal]:
        """
        Get BTC spot price. Primary: Binance WebSocket (near-zero latency).
        Fallback: Coinbase HTTP (if WS is stale >10s).
        """
        # Use Binance WS price if fresh (within 10 seconds)
        if self._binance_ws_price is not None:
            age = time.time() - self._binance_ws_price_ts
            if age < 10.0:
                price = self._binance_ws_price
                if not getattr(self, "_logged_first_spot", False):
                    logger.info(f"✓ First BTC spot via Binance WS: ${price:,.2f}")
                    self._logged_first_spot = True
                return price
            else:
                logger.debug(f"Binance WS price stale ({age:.1f}s), falling back to HTTP")

        # Fallback: Coinbase HTTP
        return await asyncio.to_thread(self._fetch_coinbase_spot_sync)

    def _fetch_coinbase_spot_sync(self) -> Optional[Decimal]:
        """Coinbase HTTP fallback for BTC spot price."""
        timeout = float(os.getenv("EXTERNAL_SPOT_TIMEOUT_SEC", "2.5"))
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        try:
            with httpx.Client(timeout=timeout, headers=headers) as client:
                resp = client.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
                resp.raise_for_status()
                data = resp.json()
                raw = data.get("price")
                if raw is not None:
                    price = Decimal(str(raw))
                    if not getattr(self, "_logged_first_spot", False):
                        logger.info(f"✓ First BTC spot via Coinbase HTTP: ${price:,.2f}")
                        self._logged_first_spot = True
                    return price
        except Exception as e:
            logger.debug(f"Coinbase spot fetch failed: {e}")
        return None

    def _record_external_spot_observation(self, price: Decimal) -> None:
        now_ts = time.time()
        self.external_spot_history.append((now_ts, price))
        if len(self.external_spot_history) > self.external_spot_history_max:
            self.external_spot_history.pop(0)

    # Removed math.erf wrappers locally
    def _extract_strike_from_question(self, question_text: str) -> Optional[Decimal]:
        text = str(question_text or "")
        if not text:
            return None
        candidates: List[Decimal] = []
        for m in re.finditer(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)", text):
            raw = str(m.group(1) or "").replace(",", "").strip()
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
        ref_spot = self.latest_external_spot
        if ref_spot is None or ref_spot <= 0:
            return candidates[0]
        return min(candidates, key=lambda v: abs(v - ref_spot))

    async def _get_market_strike_for_instrument(self, instrument_id: Any) -> Optional[Decimal]:
        def _coerce_decimal(value: Any) -> Optional[Decimal]:
            if value is None:
                return None
            try:
                out = Decimal(str(value))
            except Exception:
                return None
            return out if out > 0 else None

        inst = self._normalize_instrument_id(instrument_id)
        if inst is None:
            logger.debug(f"[DEBUG_STRIKE] inst is None for {instrument_id}")
            return None
        instrument = self.cache.instrument(inst)
        if instrument is None:
            logger.debug(f"[DEBUG_STRIKE] instrument cache miss for {inst}")
            return None
        slug = self._extract_market_slug_from_instrument(instrument)
        if not slug:
            slug = str(self.current_market_slug or "")
        logger.debug(f"[DEBUG_STRIKE] Extracted slug: '{slug}' for inst: {inst}")
        if slug and slug in self.market_strike_cache_by_slug:
            return self.market_strike_cache_by_slug[slug]
        info = getattr(instrument, "info", None) or {}
        if not isinstance(info, dict):
            info = {}
        question = str(info.get("question", "") or "")
        strike = self._extract_strike_from_question(question)
        if strike is not None and slug:
            self.market_strike_cache_by_slug[slug] = strike
            return strike
        if not slug:
            logger.debug(f"[DEBUG_STRIKE] No slug found. Returning fallback strike={strike}")
            return strike

        # For 15m markets, the title doesn't contain the strike, so we MUST hit the API.
        # Check if we already tried recently and failed to avoid spamming the API on every loop.
        now_ts = time.time()
        last_fetch_ts = float(self.market_strike_last_fetch_ts_by_slug.get(slug, 0.0))
        if last_fetch_ts > 0 and (now_ts - last_fetch_ts) < float(self.market_strike_fetch_cooldown_sec):
            logger.debug(
                f"[DEBUG_STRIKE] cooldown active for slug: {slug} "
                f"({now_ts - last_fetch_ts:.1f}s < {self.market_strike_fetch_cooldown_sec}s). Returning cached/fallback."
            )
            return strike

        self.market_strike_last_fetch_ts_by_slug[slug] = now_ts
        try:
            market = await _fetch_gamma_market_by_slug(slug)
        except Exception as e:
            logger.debug(f"[DEBUG_STRIKE] Pricer strike API fetch failed: {e}")
            market = None

        if not isinstance(market, dict):
            logger.debug(f"[DEBUG_STRIKE] Extracted market from Gamma API is not a dict: type={type(market)}. Returning {strike}")
            return strike

        # First try direct market keys.
        for key in ("_priceToBeat", "priceToBeat", "price_to_beat"):
            ptb = _coerce_decimal(market.get(key))
            if ptb is not None:
                self.market_strike_cache_by_slug[slug] = ptb
                return ptb

        # Then try nested event metadata from markets endpoint shape.
        events = market.get("events")
        if isinstance(events, list):
            for e in events:
                if not isinstance(e, dict):
                    continue
                event_meta = e.get("eventMetadata", {}) or {}
                if not isinstance(event_meta, dict):
                    event_meta = {}
                if not event_meta and isinstance(e.get("event_metadata"), dict):
                    event_meta = e.get("event_metadata") or {}
                for key in ("priceToBeat", "price_to_beat"):
                    ptb = _coerce_decimal(event_meta.get(key))
                    if ptb is not None:
                        self.market_strike_cache_by_slug[slug] = ptb
                        return ptb

        logger.debug(f"[DEBUG_STRIKE] priceToBeat not found in market payload keys: {list(market.keys())}")
            
        text_candidates: List[str] = []
        for key in ("question", "title", "description"):
            val = market.get(key)
            if val:
                text_candidates.append(str(val))
        for txt in text_candidates:
            parsed = self._extract_strike_from_question(txt)
            if parsed is not None:
                self.market_strike_cache_by_slug[slug] = parsed
                return parsed
        return strike

    def _estimate_external_spot_sigma_annualized(self) -> Optional[Decimal]:
        if len(self.external_spot_history) < self.maker_digital_vol_min_points:
            return None
        sample = self.external_spot_history[-self.maker_digital_vol_window :]
        returns: List[float] = []
        dts: List[float] = []
        for i in range(1, len(sample)):
            prev_ts, prev_px = sample[i - 1]
            cur_ts, cur_px = sample[i]
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
        if len(returns) < max(2, self.maker_digital_vol_min_points - 1):
            return None
        mean_r = sum(returns) / len(returns)
        denom = max(1, len(returns) - 1)
        var = sum((r - mean_r) ** 2 for r in returns) / denom
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

    async def _compute_fair_probability(self, market_mid: Decimal, instrument_id: Optional[Any] = None) -> Decimal:
        """
        Build fair probability from external BTC spot.
        Modes:
        - drift: legacy momentum shift on market_mid.
        - digital: short-dated digital option probability using parsed strike + estimated sigma.
        """
        # Delegated mathematical pricing logic to maker_engine
        fair = market_mid
        external = await self._fetch_external_spot_price()
        if external:
            self.latest_external_spot = external
            self._record_external_spot_observation(external)
            
            strike = None
            sigma = self.maker_digital_sigma_default
            time_left_sec = 0.0
            outcome = ""
            
            if self.maker_fair_pricer_mode == "digital":
                strike = await self._get_market_strike_for_instrument(instrument_id)
                end_ts = getattr(self, "current_market_end_timestamp", None)
                time_left_sec = float(end_ts - time.time()) if end_ts is not None else 0.0
                est_sigma = self._estimate_external_spot_sigma_annualized()
                if est_sigma and est_sigma > 0:
                    sigma = est_sigma
                sigma = sigma * self.maker_digital_vol_scale
                sigma = max(self.maker_digital_sigma_floor, min(self.maker_digital_sigma_ceiling, sigma))

                instrument = self.cache.instrument(self._normalize_instrument_id(instrument_id)) if instrument_id is not None else None
                outcome = self._extract_outcome_from_instrument(instrument) if instrument is not None else ""

                if strike is None:
                    if time.time() - self._last_strike_fallback_log_ts >= self.strike_fallback_log_interval_sec:
                        logger.debug("Digital pricer fallback: strike unavailable, using drift mode.")
                        self._last_strike_fallback_log_ts = time.time()
                else:
                    now_ts = time.time()
                    if now_ts - self._last_digital_pricer_log_ts >= 30:
                        logger.info(
                            "Digital pricer inputs: "
                            f"spot={float(external):.2f} strike={float(strike):.2f} "
                            f"sigma={float(sigma):.4f} t_left={time_left_sec:.1f}s "
                            f"outcome={outcome or 'unknown'}"
                        )
                        self._last_digital_pricer_log_ts = now_ts
            
            fair = MakerEngine.calculate_fair_price(
                market_mid=market_mid,
                external_spot=float(external),
                last_external_spot=float(self.last_external_spot or 0.0),
                strike=float(strike) if strike is not None else None,
                sigma=float(sigma),
                time_left_sec=time_left_sec,
                outcome=outcome,
                pricer_mode=self.maker_fair_pricer_mode
            )
            
            self.last_external_spot = external
            
        return fair

    @staticmethod
    def _instrument_key(instrument_id: Any) -> str:
        return str(instrument_id) if instrument_id is not None else ""

    @staticmethod
    def _normalize_side_text(side_val: Any) -> str:
        txt = str(side_val or "").strip().lower()
        if txt in {"buy", "bid"}:
            return "buy"
        if txt in {"sell", "ask"}:
            return "sell"
        if "buy" in txt:
            return "buy"
        if "sell" in txt:
            return "sell"
        if txt == "1":
            return "buy"
        if txt == "2":
            return "sell"
        return ""

    def _update_live_inventory_cost_from_fill(
        self,
        instrument_id: Any,
        side: str,
        fill_price: Decimal,
        fill_qty: Decimal,
        commission: Decimal,
    ) -> None:
        inst_key = self._instrument_key(instrument_id)
        if not inst_key or fill_qty <= 0 or fill_price <= 0:
            return
        state = self.live_inventory_cost.setdefault(
            inst_key,
            {
                "qty": Decimal("0"),
                "avg_entry_price": Decimal("0"),
                "entry_fee_remaining": Decimal("0"),
                "opened_ts": 0.0,
            },
        )
        side_norm = self._normalize_side_text(side)
        now_ts = time.time()
        if side_norm == "buy":
            old_qty = Decimal(str(state.get("qty", "0")))
            old_avg = Decimal(str(state.get("avg_entry_price", "0")))
            new_qty = old_qty + fill_qty
            if new_qty <= 0:
                return
            if old_qty > 0 and old_avg > 0:
                weighted_notional = (old_qty * old_avg) + (fill_qty * fill_price)
            else:
                weighted_notional = fill_qty * fill_price
            state["qty"] = new_qty
            state["avg_entry_price"] = weighted_notional / new_qty
            state["entry_fee_remaining"] = Decimal(str(state.get("entry_fee_remaining", "0"))) + max(Decimal("0"), commission)
            if float(state.get("opened_ts", 0.0)) <= 0:
                state["opened_ts"] = now_ts
            return

        if side_norm != "sell":
            return
        old_qty = Decimal(str(state.get("qty", "0")))
        if old_qty <= 0:
            return
        sell_qty = min(fill_qty, old_qty)
        if sell_qty <= 0:
            return
        avg_entry = Decimal(str(state.get("avg_entry_price", "0")))
        fee_remaining = Decimal(str(state.get("entry_fee_remaining", "0")))
        alloc_ratio = sell_qty / old_qty if old_qty > 0 else Decimal("0")
        entry_fee_alloc = fee_remaining * alloc_ratio
        realized_net = (sell_qty * (fill_price - avg_entry)) - entry_fee_alloc - max(Decimal("0"), commission)

        remaining_qty = old_qty - sell_qty
        remaining_fee = max(Decimal("0"), fee_remaining - entry_fee_alloc)
        if remaining_qty <= 0:
            state["qty"] = Decimal("0")
            state["avg_entry_price"] = Decimal("0")
            state["entry_fee_remaining"] = Decimal("0")
            state["opened_ts"] = 0.0
        else:
            state["qty"] = remaining_qty
            state["entry_fee_remaining"] = remaining_fee
        logger.info(
            f"Inventory realized[{inst_key[:18]}..]: sold={float(sell_qty):.6f} "
            f"entry={float(avg_entry):.4f} exit={float(fill_price):.4f} "
            f"net_pnl={float(realized_net):+.4f} remaining={float(state['qty']):.6f}"
        )

    async def _maybe_taker_exit_positions(self, now_ts: float, is_simulation: bool) -> None:
        if is_simulation or not self.taker_exit_enabled:
            return
        if self.taker_exit_cooldown_sec < 0:
            return
        end_ts = getattr(self, "current_market_end_timestamp", None)
        time_left_sec = (end_ts - now_ts) if end_ts is not None else None
        in_reduce_only_tail = (
            time_left_sec is not None
            and self.maker_reduce_only_no_new_sell_last_sec > 0
            and time_left_sec <= float(self.maker_reduce_only_no_new_sell_last_sec)
        )
        stop_loss_disabled_in_tail = (
            time_left_sec is not None
            and self.taker_exit_disable_stop_loss_last_sec > 0
            and time_left_sec <= float(self.taker_exit_disable_stop_loss_last_sec)
        )
        target_instruments = self._maker_quote_instruments()
        for inst_id in target_instruments:
            inst_key = self._instrument_key(inst_id)
            if not inst_key:
                continue
            if in_reduce_only_tail and inst_key in self.taker_exit_tail_attempted_by_inst:
                continue
            state = self.live_inventory_cost.get(inst_key)
            if not state:
                continue
            qty = Decimal(str(state.get("qty", "0")))
            if qty <= 0:
                continue
            if inst_key in self.pending_taker_exit_by_inst:
                continue
            last_ts = float(self.last_taker_exit_ts_by_inst.get(inst_key, 0.0))
            if now_ts - last_ts < self.taker_exit_cooldown_sec:
                continue
            quote = self._get_quote_for_instrument(inst_id)
            if quote is None:
                continue
            best_bid, _best_ask = quote
            if best_bid <= 0:
                continue

            token_id = self._extract_token_id_from_instrument(inst_key)
            dynamic_fee_rate = await self._get_dynamic_fee_rate(token_id=token_id)
            fee_rate = dynamic_fee_rate if (dynamic_fee_rate is not None and dynamic_fee_rate > 0) else self._infer_market_fee_rate_default()
            if fee_rate is None or fee_rate < 0:
                fee_rate = Decimal("0")

            avg_entry = Decimal(str(state.get("avg_entry_price", "0")))
            entry_fee_remaining = Decimal(str(state.get("entry_fee_remaining", "0")))
            opened_ts = float(state.get("opened_ts", 0.0))
            hold_sec = max(0.0, now_ts - opened_ts) if opened_ts > 0 else 0.0
            slip = max(Decimal("0"), self.taker_exit_slippage_buffer_pct)
            exit_px_effective = best_bid * (Decimal("1") - slip)
            gross = qty * (exit_px_effective - avg_entry)
            exit_fee_est = (qty * exit_px_effective) * fee_rate
            net_if_exit = gross - entry_fee_remaining - exit_fee_est

            trigger = ""
            if net_if_exit >= self.taker_exit_min_net_usdc:
                trigger = "take_profit"
            elif (
                not stop_loss_disabled_in_tail
                and hold_sec >= max(0, self.taker_exit_min_hold_sec)
                and net_if_exit <= -abs(self.taker_exit_stop_loss_usdc)
            ):
                trigger = "stop_loss"
            elif self.taker_exit_max_hold_sec > 0 and hold_sec >= self.taker_exit_max_hold_sec:
                trigger = "max_hold"
            if not trigger:
                continue
            if self.taker_exit_only_on_profit and trigger not in {"stop_loss", "max_hold"} and net_if_exit < self.taker_exit_min_net_usdc:
                continue

            sellable_qty = self._get_effective_sellable_qty(instrument_id=inst_id)
            qty_to_exit = min(qty, sellable_qty)
            if qty_to_exit + Decimal("0.000001") < self.maker_exchange_min_shares:
                continue
            ok = self._submit_taker_exit_order(
                instrument_id=inst_id,
                quantity=qty_to_exit,
                reason=trigger,
                est_net_if_exit=net_if_exit,
                best_bid=best_bid,
                fee_rate=fee_rate,
            )
            if ok:
                self.last_taker_exit_ts_by_inst[inst_key] = now_ts
                if in_reduce_only_tail:
                    self.taker_exit_tail_attempted_by_inst[inst_key] = now_ts

    def _submit_taker_exit_order(
        self,
        instrument_id: Any,
        quantity: Decimal,
        reason: str,
        est_net_if_exit: Decimal,
        best_bid: Decimal,
        fee_rate: Decimal,
    ) -> bool:
        inst = self._normalize_instrument_id(instrument_id)
        if inst is None:
            return False
        instrument = self.cache.instrument(inst)
        if instrument is None:
            return False
        precision = int(getattr(instrument, "size_precision", 6))
        min_lot = Decimal(str(10 ** (-precision)))
        qty_dec = max(min_lot, quantity).quantize(min_lot, rounding=ROUND_FLOOR)
        if qty_dec + Decimal("0.000001") < self.maker_exchange_min_shares:
            return False

        self._cancel_maker_order_side("buy", reason="taker_exit", instrument_id=inst)
        self._cancel_maker_order_side("sell", reason="taker_exit", instrument_id=inst)

        qty = Quantity(float(qty_dec), precision=precision)
        coid = ClientOrderId(f"BTC-15M-TAKER-EXIT-{int(time.time() * 1000)}")
        order = self.order_factory.market(
            instrument_id=inst,
            order_side=OrderSide.SELL,
            quantity=qty,
            client_order_id=coid,
            quote_quantity=False,
            time_in_force=TimeInForce.IOC,
        )
        self.submit_order(order)
        inst_key = self._instrument_key(inst)
        self.pending_taker_exit_by_inst[inst_key] = str(coid)
        logger.warning(
            "TAKER EXIT submit: "
            f"reason={reason} inst={inst_key} qty={float(qty_dec):.6f} "
            f"best_bid={float(best_bid):.4f} est_net={float(est_net_if_exit):+.4f} "
            f"fee_rate={float(fee_rate):.4f}"
        )
        self._db_order_event(
            event_type="ORDER_TAKER_EXIT_SUBMIT",
            client_order_id=str(coid),
            side="SELL",
            price=float(best_bid),
            qty=float(qty_dec),
            status="SUBMITTED",
            reason=reason,
            expected_net_usdc=float(est_net_if_exit),
            payload={
                "instrument_id": inst_key,
                "fee_rate_decimal": float(fee_rate),
                "best_bid": float(best_bid),
            },
        )
        return True

    def _clear_pending_taker_exit_for_order(self, client_order_id: str) -> None:
        target = str(client_order_id or "")
        if not target:
            return
        for inst_key, coid in list(self.pending_taker_exit_by_inst.items()):
            if coid == target:
                self.pending_taker_exit_by_inst.pop(inst_key, None)
                break

    @staticmethod
    def _extract_token_id_from_instrument(instrument_id: str) -> Optional[str]:
        """
        Extract token_id from Nautilus Polymarket instrument ID:
        {condition_id}-{token_id}.POLYMARKET
        """
        m = re.search(r"-([0-9]+)\.POLYMARKET$", instrument_id)
        if not m:
            return None
        return m.group(1)

    @staticmethod
    def _extract_market_slug_from_instrument(instrument: Any) -> str:
        info = getattr(instrument, "info", None) or {}
        if isinstance(info, dict):
            for key in ("market_slug", "slug", "event_slug", "marketSlug", "eventSlug"):
                s = str(info.get(key, "") or "")
                if s:
                    return s
        return ""

    def _extract_outcome_from_instrument(self, instrument: Any) -> str:
        """
        Best-effort outcome mapping (up/down) from instrument metadata.
        """
        try:
            inst_id = str(getattr(instrument, "id", "") or "")
            token_id = self._extract_token_id_from_instrument(inst_id)
            info = getattr(instrument, "info", None) or {}
            if not isinstance(info, dict):
                return ""
            tokens = info.get("tokens")
            if isinstance(tokens, list):
                for t in tokens:
                    if not isinstance(t, dict):
                        continue
                    t_id = str(t.get("token_id", "") or "")
                    if token_id and t_id == token_id:
                        return str(t.get("outcome", "") or "").strip().lower()
        except Exception:
            return ""
        return ""

    @staticmethod
    def _order_key_for(side: str, instrument_id: Any) -> str:
        return f"{side}:{instrument_id}"

    @staticmethod
    def _normalize_instrument_id(instrument_id: Any) -> Optional[InstrumentId]:
        if instrument_id is None:
            return None
        if isinstance(instrument_id, InstrumentId):
            return instrument_id
        try:
            return InstrumentId.from_str(str(instrument_id))
        except Exception:
            return None

    def _active_order_keys(self, side: Optional[str] = None, instrument_id: Optional[Any] = None) -> List[str]:
        keys: List[str] = []
        target_inst = str(instrument_id) if instrument_id is not None else None
        for key, state in self.active_maker_orders.items():
            state_side = str(state.get("side", "") or "")
            state_inst = str(state.get("instrument_id", "") or "")
            if side is not None and state_side != side:
                continue
            if target_inst is not None and state_inst != target_inst:
                continue
            keys.append(key)
        return keys

    def _get_quote_for_instrument(self, instrument_id: Any) -> Optional[Tuple[Decimal, Decimal]]:
        inst = self._normalize_instrument_id(instrument_id)
        if inst is None:
            return None
        quote = self.cache.quote_tick(inst)
        if quote is None:
            return None
        bid_decimal = quote.bid_price.as_decimal() if quote.bid_price is not None else None
        ask_decimal = quote.ask_price.as_decimal() if quote.ask_price is not None else None
        if bid_decimal is None and ask_decimal is not None:
            bid_decimal = max(Decimal("0.01"), ask_decimal - Decimal("0.01"))
        if ask_decimal is None and bid_decimal is not None:
            ask_decimal = min(Decimal("0.99"), bid_decimal + Decimal("0.01"))
        if bid_decimal is None or ask_decimal is None:
            return None
        if bid_decimal > ask_decimal:
            mid_tmp = (bid_decimal + ask_decimal) / 2
            bid_decimal = max(Decimal("0.01"), mid_tmp - Decimal("0.005"))
            ask_decimal = min(Decimal("0.99"), mid_tmp + Decimal("0.005"))
        return bid_decimal, ask_decimal

    def _append_real_mid_price(self, instrument_id: Any, mid_price: Decimal) -> None:
        inst_key = str(instrument_id) if instrument_id is not None else ""
        self.real_price_history.append(mid_price)
        if len(self.real_price_history) > self.max_real_history:
            self.real_price_history.pop(0)
        if not inst_key:
            return
        history = self.real_price_history_by_inst.setdefault(inst_key, [])
        history.append(mid_price)
        if len(history) > self.max_real_history:
            history.pop(0)

    def _momentum_history_for_instrument(self, instrument_id: Any) -> List[Decimal]:
        inst_key = str(instrument_id) if instrument_id is not None else ""
        if inst_key:
            per_inst = self.real_price_history_by_inst.get(inst_key)
            if per_inst:
                return per_inst
        return self.real_price_history

    def _get_total_sellable_qty(self, instrument_ids: Optional[List[Any]] = None) -> Decimal:
        ids = instrument_ids or []
        if not ids and self.instrument_id is not None:
            ids = [self.instrument_id]
        total = Decimal("0")
        seen: set[str] = set()
        for inst_id in ids:
            key = str(inst_id)
            if not key or key in seen:
                continue
            seen.add(key)
            total += self._get_sellable_qty_for_current_instrument(instrument_id=inst_id)
        return total

    def _infer_market_fee_rate_default(self) -> Decimal:
        """
        Infer fee-curve parameter by market type when /fee-rate is unavailable.
        """
        default_rate = self.maker_fee_rate_default_decimal
        try:
            instrument = self.cache.instrument(self.instrument_id) if self.instrument_id else None
            info = getattr(instrument, "info", None) or {}
            if not isinstance(info, dict):
                return default_rate

            gamma_original = info.get("_gamma_original")
            fee_type = ""
            if isinstance(gamma_original, dict):
                fee_type = str(gamma_original.get("feeType", "")).strip().lower()

            text = f"{str(info.get('market_slug', '')).lower()} {str(info.get('question', '')).lower()} {fee_type}"
            if "crypto_15" in fee_type or "crypto_5" in fee_type or "btc-updown" in text:
                return Decimal("0.25")
            if "ncaab" in text or "serie a" in text or "serie_a" in text:
                return Decimal("0.0175")
        except Exception:
            pass
        return default_rate

    async def _get_dynamic_fee_rate(self, token_id: Optional[str] = None) -> Optional[Decimal]:
        """
        Fetch dynamic fee rate from CLOB /fee-rate endpoint using current token_id.
        """
        token = token_id or self.current_token_id
        if not token:
            return None
        now_ts = time.time()
        local_cached = self._fee_rate_local_cache_by_token.get(token)
        if local_cached is not None:
            cached_ts = float(local_cached.get("ts", 0.0))
            cached_rate = local_cached.get("fee_rate")
            if (
                isinstance(cached_rate, Decimal)
                and cached_rate > 0
                and cached_ts > 0
                and (now_ts - cached_ts) < self.fee_rate_fetch_interval_sec
            ):
                return cached_rate

        fee_rate = await self.fee_rate_client.get_fee_rate_decimal(token)
        source = "clob_fee_rate"
        if fee_rate is None or fee_rate <= 0:
            fee_rate = self._infer_market_fee_rate_default()
            source = "market_type_default"
            if (fee_rate is None or fee_rate <= 0) and self.maker_fee_rate_legacy_bps_default > 0:
                fee_rate = bps_to_fee_rate(self.maker_fee_rate_legacy_bps_default)
                source = "legacy_bps_default"
        if fee_rate is None or fee_rate <= 0:
            return None
        self._fee_rate_local_cache_by_token[token] = {"fee_rate": fee_rate, "ts": now_ts}
        fee_bps_value = int((fee_rate * Decimal("10000")).quantize(Decimal("1")))
        prev_state = self._last_fee_log_state_by_token.get(token, {})
        prev_ts = float(prev_state.get("ts", 0.0))
        prev_bps = int(prev_state.get("bps", -1))
        prev_source = str(prev_state.get("source", ""))
        should_log = (
            prev_ts <= 0
            or (now_ts - prev_ts) >= self.fee_log_interval_sec
            or prev_bps != fee_bps_value
            or prev_source != source
        )
        if should_log:
            logger.debug(
                f"Using fee rate source={source} bps={fee_bps_value} "
                f"decimal={float(fee_rate):.6f} token={token}"
            )
            self._last_fee_log_state_by_token[token] = {
                "ts": now_ts,
                "bps": fee_bps_value,
                "source": source,
            }
        if source != "clob_fee_rate":
            health = self.fee_rate_client.get_health_snapshot()
            last_reason = str(health.get("last_error_reason", ""))
            last_status = int(health.get("last_status_code", 0) or 0)
            last_excerpt = str(health.get("last_response_excerpt", ""))
            if last_reason or last_status:
                logger.debug(
                    "fee-rate fallback diagnostics: "
                    f"reason={last_reason} status={last_status} excerpt={last_excerpt}"
                )
        return fee_rate

    @staticmethod
    def _parse_orderbook_levels(raw_levels: Any, limit: int) -> List[Tuple[Decimal, Decimal]]:
        levels: List[Tuple[Decimal, Decimal]] = []
        if not isinstance(raw_levels, list):
            return levels
        for lv in raw_levels:
            if len(levels) >= limit:
                break
            px: Optional[Decimal] = None
            qty: Optional[Decimal] = None
            try:
                if isinstance(lv, dict):
                    px = Decimal(str(lv.get("price")))
                    qty = Decimal(str(lv.get("size") if lv.get("size") is not None else lv.get("quantity")))
                elif isinstance(lv, (list, tuple)) and len(lv) >= 2:
                    px = Decimal(str(lv[0]))
                    qty = Decimal(str(lv[1]))
            except Exception:
                continue
            if px is None or qty is None:
                continue
            if px <= 0 or qty <= 0:
                continue
            levels.append((px, qty))
        return levels

    async def _get_orderbook_levels_for_token(
        self,
        token_id: Optional[str],
    ) -> Tuple[Optional[List[Tuple[Decimal, Decimal]]], Optional[List[Tuple[Decimal, Decimal]]]]:
        token = str(token_id or "").strip()
        if not token:
            return None, None
        now_ts = time.time()
        cached = self.orderbook_levels_cache_by_token.get(token)
        if cached is not None:
            cached_ts = float(cached.get("ts", 0.0))
            if cached_ts > 0 and (now_ts - cached_ts) < self.orderbook_fetch_interval_sec:
                return cached.get("bids"), cached.get("asks")

        client = getattr(self, "_balance_clob_client", None)
        if client is None:
            # Try lazy init from existing balance refresh path.
            self._refresh_balance_cache()
            client = getattr(self, "_balance_clob_client", None)
        if client is None:
            return None, None

        try:
            raw = await asyncio.to_thread(client.get_order_book, token)
            
            # Robust extraction for both dict and object responses from py_clob_client
            raw_bids = raw.get("bids") if isinstance(raw, dict) else getattr(raw, "bids", None)
            raw_asks = raw.get("asks") if isinstance(raw, dict) else getattr(raw, "asks", None)
            
            bids = self._parse_orderbook_levels(raw_bids, self.orderbook_levels_limit)
            asks = self._parse_orderbook_levels(raw_asks, self.orderbook_levels_limit)
            self.orderbook_levels_cache_by_token[token] = {"ts": now_ts, "bids": bids, "asks": asks}
            return bids, asks
        except Exception as e:
            logger.debug(f"Orderbook level fetch failed for token={token}: {e}")
            if cached is not None:
                return cached.get("bids"), cached.get("asks")
            return None, None

    def _activate_maker_kill_switch(self, reason: str) -> None:
        self.maker_kill_switch = True
        self._cancel_active_maker_orders()
        logger.error(f"MAKER KILL SWITCH ACTIVATED: {reason}")

    def _reset_maker_state_for_new_market(self, prev_instrument_id: Optional[str], new_instrument_id: Optional[str]) -> None:
        """
        Per-market maker state reset.
        Inventory and kill-switch are strategy-local controls and should not carry across 15m markets.
        """
        if prev_instrument_id == new_instrument_id:
            return
        self._cancel_active_maker_orders()
        self.inventory_delta_shares = Decimal("0")
        self.live_inventory_cost.clear()
        self.pending_taker_exit_by_inst.clear()
        self.taker_exit_tail_attempted_by_inst.clear()
        self._sell_reject_pause_until_by_inst.clear()
        self._conditional_balance_cache_by_token.clear()
        self.latest_quote_depth_by_inst.clear()
        self.orderbook_levels_cache_by_token.clear()
        if self.maker_kill_switch and self.maker_kill_switch_reset_on_rollover:
            self.maker_kill_switch = False
            logger.warning("Maker kill switch auto-reset on market rollover.")
        self.last_quote_update_ts = 0.0
        logger.info(f"Reset maker per-market state: {prev_instrument_id} -> {new_instrument_id}")

    def _project_inventory_after_fill(self, side: str, qty: Decimal, instrument_id: Optional[Any] = None) -> Decimal:
        inst_id = instrument_id if instrument_id is not None else self.instrument_id
        projected = self.inventory_delta_shares
        
        # Calculate in-flight volume for the SAME side
        target_side_str = "BUY" if side.lower() == "buy" else "SELL"
        in_flight_qty = Decimal("0")
        inst_target = str(inst_id) if inst_id else None
        
        for _, state in self.active_maker_orders.items():
            o_side = str(state.get("side", "")).upper()
            if o_side != target_side_str:
                continue
                
            o_inst = str(state.get("instrument_id", ""))
            if inst_target and o_inst != inst_target:
                continue
                
            # If the order is in our active dict, assume its REMAINING quantity is occupying inventory capacity
            # regardless of whether it lacks a VenueOrderId yet or is pending cancel.
            o_qty = Decimal(str(state.get("quantity", "0")))
            o_filled = Decimal(str(state.get("filled_qty", "0")))
            in_flight_qty += max(Decimal("0"), o_qty - o_filled)
            
        if side.lower() == "buy":
            return projected + in_flight_qty + qty
        return projected - in_flight_qty - qty

    def _get_sellable_qty_for_current_instrument(self, instrument_id: Optional[Any] = None) -> Decimal:
        """
        Get sellable token quantity from cache open positions for current instrument.
        """
        inst = instrument_id if instrument_id is not None else self.instrument_id
        inst = self._normalize_instrument_id(inst)
        if inst is None:
            return Decimal("0")
        total = Decimal("0")
        try:
            positions = self.cache.positions_open(instrument_id=inst)
            for pos in positions or []:
                signed = Decimal(str(getattr(pos, "signed_qty", 0.0) or 0.0))
                if signed > 0:
                    total += signed
        except Exception as e:
            logger.debug(f"Could not read sellable qty from cache positions: {e}")
        return total

    def _get_conditional_balance_for_token(self, token_id: Optional[str], force_refresh: bool = False) -> Optional[Decimal]:
        """
        Query CONDITIONAL token balance from CLOB and cache for a short interval.
        Returns shares (Decimal) or None if unavailable.
        """
        token = str(token_id or "").strip()
        if not token:
            return None
        now_ts = time.time()
        cached = self._conditional_balance_cache_by_token.get(token)
        if (
            not force_refresh
            and cached is not None
            and (now_ts - float(cached.get("ts", 0.0))) < self.conditional_balance_check_interval_sec
        ):
            bal = cached.get("balance")
            return Decimal(str(bal)) if bal is not None else None

        try:
            if not hasattr(self, "_balance_clob_client") or self._balance_clob_client is None:
                self._balance_last_check_ts = 0.0
                self._refresh_balance_cache()
            client = getattr(self, "_balance_clob_client", None)
            if client is None:
                return Decimal(str(cached.get("balance"))) if cached and cached.get("balance") is not None else None

            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token,
                signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
            )
            result = client.get_balance_allowance(params)
            if result and isinstance(result, dict):
                raw = result.get("balance")
                if raw is not None:
                    balance_shares = Decimal(str(raw)) / Decimal("1000000")
                    self._conditional_balance_cache_by_token[token] = {
                        "ts": now_ts,
                        "balance": str(balance_shares),
                    }
                    return balance_shares
        except Exception as e:
            logger.debug(f"Conditional balance fetch failed for token={token}: {e}")

        if cached and cached.get("balance") is not None:
            return Decimal(str(cached.get("balance")))
        return None

    def _get_effective_sellable_qty(self, instrument_id: Optional[Any]) -> Decimal:
        """
        Conservative sellable qty:
        min(cache open positions, on-chain conditional balance with safety buffer).
        """
        local_qty = self._get_sellable_qty_for_current_instrument(instrument_id=instrument_id)
        inst_txt = str(instrument_id or "")
        token_id = self._extract_token_id_from_instrument(inst_txt)
        onchain_qty = self._get_conditional_balance_for_token(token_id=token_id, force_refresh=False)
        if onchain_qty is None:
            return local_qty
        safe_onchain = onchain_qty * (Decimal("1") - self.conditional_balance_safety_buffer_pct)
        safe_onchain = max(Decimal("0"), safe_onchain)
        if local_qty > 0:
            return min(local_qty, safe_onchain)
        return safe_onchain

    def _compute_maker_order_qty(self, limit_price: Decimal, precision: int) -> Decimal:
        """
        Compute order quantity for maker quote.
        Priority:
        1) Fixed shares (MAKER_FIXED_SHARES > 0),
        2) USDC notional / price with min shares floor.
        """
        min_lot = Decimal(str(10 ** (-precision)))
        min_qty = max(min_lot, self.maker_min_shares)
        if self.maker_fixed_shares > 0:
            return max(self.maker_fixed_shares, min_qty)

        quote_notional_usdc = min(self.maker_quote_size_usdc, self.maker_max_order_usdc)
        if quote_notional_usdc < self.maker_quote_size_usdc:
            logger.warning(
                f"Maker quote notional capped by MAKER_MAX_ORDER_USDC: "
                f"{float(self.maker_quote_size_usdc):.4f} -> {float(quote_notional_usdc):.4f}"
            )

        token_qty = Decimal("0")
        if limit_price > 0:
            token_qty = quote_notional_usdc / limit_price
        token_qty = max(token_qty, min_qty)
        return token_qty

    def _compute_recent_volatility(self) -> Optional[Decimal]:
        """
        Compute volatility from REAL quote history only.
        Uses clipped returns + max(rolling_std, ewma_std).
        """
        min_quotes = max(2, self.maker_vol_warmup_quotes)
        if len(self.real_price_history) < min_quotes:
            return None

        window = max(5, self.maker_vol_rolling_window)
        recent = self.real_price_history[-(window + 1):]
        clip = float(abs(self.maker_vol_return_clip))
        returns: List[float] = []
        for i in range(1, len(recent)):
            prev = float(recent[i - 1])
            cur = float(recent[i])
            if prev <= 0:
                continue
            r = (cur - prev) / prev
            r = max(-clip, min(clip, r))
            returns.append(r)
        if len(returns) < 2:
            return None

        # Rolling standard deviation (population) on the clipped returns.
        roll = returns[-window:]
        mean_r = sum(roll) / len(roll)
        var = sum((r - mean_r) ** 2 for r in roll) / len(roll)
        rolling_std = math.sqrt(max(0.0, var))

        # EWMA standard deviation on clipped returns.
        alpha = max(0.01, min(0.99, self.maker_vol_ewma_alpha))
        ewma_var = roll[0] ** 2
        for r in roll[1:]:
            ewma_var = alpha * (r ** 2) + (1.0 - alpha) * ewma_var
        ewma_std = math.sqrt(max(0.0, ewma_var))

        return Decimal(str(max(rolling_std, ewma_std)))

    # _apply_inventory_skew removed (managed by MakerEngine)
    def _cancel_active_maker_orders(self) -> None:
        for order_key in list(self.active_maker_orders.keys()):
            self._cancel_maker_order_side(order_key, reason="risk")

    def _cancel_maker_order_side(self, side: str, reason: str = "risk", instrument_id: Optional[Any] = None) -> None:
        # Backward compatible:
        # - if `side` is an exact order key, cancel that key
        # - else treat `side` as logical side filter (buy/sell)
        target_keys: List[str] = []
        if side in self.active_maker_orders and instrument_id is None:
            target_keys = [side]
        else:
            target_keys = self._active_order_keys(side=side, instrument_id=instrument_id)
        for order_key in target_keys:
            self._cancel_maker_order_key(order_key, reason=reason)

    def _cancel_maker_order_key(self, order_key: str, reason: str = "risk") -> None:
        state = self.active_maker_orders.get(order_key)
        if not state:
            return
        side = str(state.get("side", "") or "")
        order = state.get("order")
        if state.get("simulated"):
            coid = str(state.get("client_order_id", f"SIM-{side}-{int(time.time()*1000)}"))
            now_ts = time.time()
            cancel_latency_ms = random.randint(
                min(self.sim_cancel_latency_ms_min, self.sim_cancel_latency_ms_max),
                max(self.sim_cancel_latency_ms_min, self.sim_cancel_latency_ms_max),
            )
            state["pending_cancel"] = True
            state["cancel_requested_ts"] = now_ts
            state["cancel_effective_at"] = now_ts + (cancel_latency_ms / 1000.0)
            state["cancel_reason"] = reason
            self._db_order_event(
                event_type="ORDER_SIM_CANCEL_REQUESTED",
                client_order_id=coid,
                side=side.upper(),
                price=float(state.get("price", 0.0)),
                qty=float(state.get("quantity", 0.0)),
                status="PENDING_CANCEL",
                reason=reason,
                payload={"cancel_latency_ms": cancel_latency_ms},
            )
            return
        if order is None:
            # The order might just be missing a venue ID or dropped from cache temporarily.
            # Don't pop it! Mark it pending_cancel to be cleaned up by the reconcile loop.
            now_ts = time.time()
            state["pending_cancel"] = True
            state["last_cancel_ts"] = now_ts
            return
        try:
            status_text = str(getattr(order, "status", "")).upper()
            if any(flag in status_text for flag in ("REJECTED", "FILLED", "CANCELED", "CANCELLED")):
                logger.debug(f"Skip cancel [{side}] because order state is terminal: {status_text}")
                self.active_maker_orders.pop(order_key, None)
            else:
                now_ts = time.time()
                if state.get("pending_cancel"):
                    last_cancel_ts = float(state.get("last_cancel_ts", 0.0))
                    if now_ts - last_cancel_ts < self.maker_cancel_cooldown_sec:
                        logger.debug(f"Skip duplicate cancel [{side}] within cooldown")
                        return
                self.cancel_order(order)
                state["pending_cancel"] = True
                state["last_cancel_ts"] = now_ts
                state["cancel_retries"] = int(state.get("cancel_retries", 0))
                logger.info(f"Cancelled maker order [{side}] {order.client_order_id}")
        except Exception as e:
            logger.debug(f"Failed to cancel maker order [{side}]: {e}")
        self.rebate_reporter.record_cancel(reason)

    def _is_order_ttl_expired(self, order_key: str, now_ts: float) -> bool:
        state = self.active_maker_orders.get(order_key)
        if not state:
            return False
        created_ts = float(state.get("created_ts", 0.0))
        if created_ts <= 0:
            return True
        return (now_ts - created_ts) >= self.maker_order_ttl_sec

    def _cleanup_stale_pending_cancels(self, now_ts: float) -> None:
        for order_key, state in list(self.active_maker_orders.items()):
            side = str(state.get("side", "") or "")
            if not state.get("pending_cancel"):
                continue
            last_cancel_ts = float(state.get("last_cancel_ts", 0.0))
            if last_cancel_ts <= 0:
                continue
            if (now_ts - last_cancel_ts) >= self.maker_cancel_ack_timeout_sec:
                order = state.get("order")
                coid = str(order.client_order_id) if order else "unknown"
                is_open = self._is_order_still_open_in_cache(coid)
                retries = int(state.get("cancel_retries", 0))
                if is_open is False:
                    logger.info(f"Cancel reconciled for [{side}] {coid}; removing local pending-cancel state.")
                    self._db_order_event(
                        event_type="ORDER_CANCEL_RECONCILED",
                        client_order_id=coid,
                        side=side.upper(),
                        status="CANCELED_RECONCILED",
                    )
                    self.active_maker_orders.pop(order_key, None)
                    continue

                if is_open is None:
                    unknown_retries = int(state.get("reconcile_unknown_retries", 0)) + 1
                    state["reconcile_unknown_retries"] = unknown_retries
                    
                    if unknown_retries > self.maker_cancel_max_retries * 2:  # Give it a bit more leeway then drop
                        logger.error(f"Cancel reconcile unknown for [{side}] {coid} exceeded max retries. Triggering Maker Kill Switch.")
                        self._db_order_event(
                            event_type="ORDER_CANCEL_RECONCILE_UNKNOWN_KILL",
                            client_order_id=coid,
                            side=side.upper(),
                            status="KILL_SWITCH_UNKNOWN",
                            reason="max_unknown_retries",
                        )
                        self._activate_maker_kill_switch(f"Order {coid} state unknown after {unknown_retries} retries")
                        continue

                    state["last_cancel_ts"] = now_ts
                    pause_sec = max(1, min(self.maker_error_pause_sec, self.maker_cancel_ack_timeout_sec))
                    self.quote_pause_until_ts = max(self.quote_pause_until_ts, now_ts + pause_sec)
                    logger.warning(
                        f"Cancel reconcile unknown for [{side}] {coid}; "
                        f"keeping pending-cancel state and retrying later "
                        f"(unknown_count={unknown_retries}, pause={pause_sec}s)."
                    )
                    self._db_order_event(
                        event_type="ORDER_CANCEL_RECONCILE_UNKNOWN",
                        client_order_id=coid,
                        side=side.upper(),
                        status="PENDING_CANCEL_UNKNOWN",
                        reason="cache_unknown",
                        payload={"unknown_count": unknown_retries},
                    )
                    continue

                if retries < self.maker_cancel_max_retries and order is not None:
                    try:
                        self.cancel_order(order)
                        state["last_cancel_ts"] = now_ts
                        state["cancel_retries"] = retries + 1
                        logger.warning(
                            f"Pending-cancel timeout for [{side}] {coid}; "
                            f"reconcile suggests still open (or unknown), retry cancel "
                            f"{state['cancel_retries']}/{self.maker_cancel_max_retries}."
                        )
                        self._db_order_event(
                            event_type="ORDER_CANCEL_RETRY",
                            client_order_id=coid,
                            side=side.upper(),
                            status="PENDING_CANCEL_RETRY",
                            reason=f"timeout_reconcile_open={is_open}",
                            payload={"retry": state["cancel_retries"]},
                        )
                    except Exception as e:
                        logger.warning(f"Cancel retry failed for [{side}] {coid}: {e}")
                    continue

                logger.error(
                    f"Cancel reconciliation failed for [{side}] {coid} after "
                    f"{retries} retries; activating maker kill switch."
                )
                self._db_order_event(
                    event_type="ORDER_CANCEL_RECONCILE_FAILED",
                    client_order_id=coid,
                    side=side.upper(),
                    status="PENDING_CANCEL_GIVE_UP",
                    reason=f"open_after_retries={retries}",
                )
                self._activate_maker_kill_switch(
                    f"Cancel reconcile failed for {coid} after {retries} retries"
                )

    def _is_order_still_open_in_cache(self, client_order_id: str) -> Optional[bool]:
        """
        Try to reconcile local pending-cancel state against Nautilus cache.
        Returns:
        - True: order still appears open/live
        - False: order not found among open/live orders
        - None: could not determine
        """
        try:
            open_orders = []
            if hasattr(self.cache, "orders_open"):
                oo = self.cache.orders_open()
                if oo:
                    open_orders.extend(list(oo))
            elif hasattr(self.cache, "orders"):
                oo = self.cache.orders()
                if oo:
                    open_orders.extend(list(oo))

            # Empty cache can be a transient sync gap; treat as unknown, not closed.
            if len(open_orders) == 0:
                return None

            target = str(client_order_id)
            for o in open_orders:
                coid = str(getattr(o, "client_order_id", "") or "")
                if coid != target:
                    continue
                status_text = str(getattr(o, "status", "")).upper()
                if any(flag in status_text for flag in ("FILLED", "REJECTED", "CANCELED", "CANCELLED", "EXPIRED")):
                    return False
                return True
            return False
        except Exception as e:
            logger.debug(f"Open-order cache reconciliation failed for {client_order_id}: {e}")
            return None

    # Simulation logic extracted to execution/sim_adapter.py

    def _maybe_auto_tune(self, now_ts: float) -> None:
        if not self.auto_tune_enabled:
            return
        if now_ts - self.last_auto_tune_ts < self.auto_tune_interval_sec:
            return
        metrics = self.rebate_reporter.get_current_metrics()
        suggestion = self.parameter_tuner.suggest(
            current_half_spread=self.maker_half_spread,
            current_min_expected_net=self.maker_min_expected_net_usdc,
            metrics=metrics,
        )
        new_half = suggestion["maker_half_spread"]
        new_min = suggestion["maker_min_expected_net_usdc"]
        if new_half != self.maker_half_spread or new_min != self.maker_min_expected_net_usdc:
            logger.info(
                "Auto-tune update: "
                f"half_spread {float(self.maker_half_spread):.6f}->{float(new_half):.6f}, "
                f"min_net {float(self.maker_min_expected_net_usdc):.6f}->{float(new_min):.6f}"
            )
        self.maker_half_spread = new_half
        self.maker_min_expected_net_usdc = new_min
        # Keep decoupled maker engine config in sync with auto-tuned values.
        self.maker_engine.config.maker_half_spread = new_half
        self.maker_engine.config.maker_min_expected_net_usdc = new_min
        self.last_auto_tune_ts = now_ts

    def _maker_quote_instruments(self) -> List[InstrumentId]:
        if self.maker_quote_sides == "both_buy":
            if self.current_market_instruments:
                return list(self.current_market_instruments)
        if self.instrument_id is None:
            return []
        return [self.instrument_id]

    async def _quote_maker_orders(self, bid_price: Decimal, ask_price: Decimal) -> None:
        """
        Place symmetric maker quotes if expected net economics is positive.
        """
        is_simulation = await self.check_simulation_mode()
        if is_simulation and not self.maker_simulation_shadow:
            self._cancel_active_maker_orders()
            now_ts = time.time()
            if now_ts - self.last_simulation_guard_log_ts >= 30:
                logger.warning(
                    "Maker mode is blocked in SIMULATION mode. "
                    "No real maker orders will be submitted."
                )
                self.last_simulation_guard_log_ts = now_ts
            return

        if self.maker_kill_switch:
            return

        if time.time() < self.quote_pause_until_ts:
            return

        # --- Market Lifecycle Gate ---
        phase = self._update_market_phase()
        if phase in (MarketPhase.WAITING, MarketPhase.SETTLING):
            self._cancel_active_maker_orders()
            return

        # --- Balance Pre-check (non-simulation only) ---
        _balance_forced_sell_only = False
        if not is_simulation:
            balance = self._refresh_balance_cache()
            if balance is not None:
                required = self.maker_quote_size_usdc * Decimal("1.1")  # 10% buffer
                if balance < required:
                    gross_sellable = self._get_total_sellable_qty(self._maker_quote_instruments())
                    if gross_sellable > 0:
                        # Have inventory — switch to SELL-only to free up capital
                        _balance_forced_sell_only = True
                        if time.time() - getattr(self, '_last_balance_warn_ts', 0) >= 60:
                            logger.warning(
                                f"Balance pre-check: low USDC "
                                f"(available={float(balance):.2f}, needed≈{float(required):.2f}). "
                                f"Switching to SELL-only until balance recovers. "
                                f"net_inventory={float(self.inventory_delta_shares):.4f} "
                                f"gross_sellable={float(gross_sellable):.4f}"
                            )
                            self._last_balance_warn_ts = time.time()
                    else:
                        # No inventory and no balance — skip quoting entirely
                        if time.time() - getattr(self, '_last_balance_warn_ts', 0) >= 60:
                            logger.warning(
                                f"Balance pre-check: insufficient USDC and no inventory "
                                f"(available={float(balance):.2f}, needed≈{float(required):.2f}). "
                                f"Skipping maker quotes."
                            )
                            self._last_balance_warn_ts = time.time()
                        return

        # Check whether current inventory should be force-closed via taker orders.
        await self._maybe_taker_exit_positions(time.time(), is_simulation=is_simulation)

        now_ts = time.time()
        if now_ts - self.last_quote_update_ts < self.quote_refresh_sec:
            return
        self.last_quote_update_ts = now_ts
        self._maybe_auto_tune(now_ts)
        self._cleanup_stale_pending_cancels(now_ts)
        if is_simulation and self.maker_simulation_shadow:
            self.inventory_delta_shares = self.sim_adapter.simulate_shadow_maker_fills_and_closes(
                active_maker_orders=self.active_maker_orders,
                inventory_delta_shares=self.inventory_delta_shares,
                bid_price=bid_price,
                ask_price=ask_price,
                now_ts=now_ts,
                get_quote_for_instrument_fn=self._get_quote_for_instrument,
                normalize_instrument_id_fn=self._normalize_instrument_id,
                instrument_cache_fn=self.cache.instrument,
                db_event_fn=self._db_order_event,
                record_cancel_fn=self.rebate_reporter.record_cancel,
                record_trade_fn=self.performance_tracker.record_trade
            )

        # Cancel stale quotes by TTL before computing new target quotes.
        for order_key, state in list(self.active_maker_orders.items()):
            created_ts = float(state.get("created_ts", 0.0))
            if created_ts <= 0 or (now_ts - created_ts) >= self.maker_order_ttl_sec:
                side = str(state.get("side", "") or "")
                logger.info(f"Maker order [{side}] exceeded TTL={self.maker_order_ttl_sec}s, cancel and requote.")
                self._cancel_maker_order_side(order_key, reason="ttl")

        if abs(self.inventory_delta_shares) > self.maker_max_inventory_shares:
            self._activate_maker_kill_switch(
                f"Inventory {self.inventory_delta_shares} exceeds max {self.maker_max_inventory_shares}"
            )
            return

        recent_vol = self._compute_recent_volatility()
        if recent_vol is None:
            logger.debug(
                f"Volatility gate warmup: real_quotes={len(self.real_price_history)}/{self.maker_vol_warmup_quotes}"
            )
        else:
            # We no longer pause the engine on high volatility.
            # Volatility Regimes inside MakerEngine handle STRESSED and EXTREME states.
            pass

        target_instruments = self._maker_quote_instruments()
        if not target_instruments:
            return

        # Refill Requote Token Bucket
        time_passed = max(0.0, now_ts - self.requote_bucket_last_refill)
        self.requote_bucket_tokens = min(
            self.maker_requote_max_per_sec,
            self.requote_bucket_tokens + (time_passed * self.maker_requote_max_per_sec)
        )
        self.requote_bucket_last_refill = now_ts

        desired_quotes: Dict[str, Dict[str, Any]] = {}
        target_inst_set = {str(inst) for inst in target_instruments}

        for inst_id in target_instruments:
            quote = self._get_quote_for_instrument(inst_id)
            if quote is None:
                continue
            inst_bid, inst_ask = quote
            fair = await self._compute_fair_probability((inst_bid + inst_ask) / 2, instrument_id=inst_id)
            
            instrument_for_tick = self._normalize_instrument_id(inst_id)
            instrument = self.cache.instrument(instrument_for_tick) if instrument_for_tick else None
            tick = Decimal("0.01")
            if instrument is not None:
                try:
                    raw_tick = getattr(instrument, "price_increment", None)
                    if raw_tick is not None:
                        tick = Decimal(str(raw_tick))
                    elif hasattr(instrument, "info") and instrument.info:
                        maybe_tick = instrument.info.get("minimum_tick_size")
                        if maybe_tick is not None:
                            tick = Decimal(str(maybe_tick))
                except Exception:
                    pass
            if tick <= 0:
                tick = Decimal("0.01")

            token_id = self._extract_token_id_from_instrument(str(inst_id))
            dynamic_fee_rate = await self._get_dynamic_fee_rate(token_id=token_id)
            bid_levels, ask_levels = await self._get_orderbook_levels_for_token(token_id)
            self.rebate_reporter.record_api_health(self.fee_rate_client.get_health_snapshot())
            if dynamic_fee_rate is not None:
                pass
                # logger.debug(
                #     f"Using dynamic fee rate: {float(dynamic_fee_rate):.6f} for token {token_id}"
                # )
            fee_rate_val = dynamic_fee_rate if dynamic_fee_rate is not None else self.maker_fee_rate_default_decimal
            depth_key = str(inst_id)
            bid_depth, ask_depth = self.latest_quote_depth_by_inst.get(depth_key, (None, None))

            side_plan = self.maker_engine.generate_quote_plan(
                inst_bid=inst_bid,
                inst_ask=inst_ask,
                fair_price=fair,
                fee_rate=fee_rate_val,
                inventory_delta_shares=self.inventory_delta_shares,
                inventory_last_update_ts=self.inventory_last_update_ts,
                current_time_ts=now_ts,
                tick_size=tick,
                recent_vol=recent_vol,
                balance_forced_sell_only=_balance_forced_sell_only,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                bid_levels=bid_levels,
                ask_levels=ask_levels,
            )
            if not side_plan:
                continue

            # --- TREND / MOMENTUM FILTER ---------------------------------------------
            # Prevent "catching a falling knife" (buy) or "stepping in front of a train" (sell).
            momentum_history = self._momentum_history_for_instrument(inst_id)
            if self.maker_momentum_filter_pct > 0 and len(momentum_history) >= self.maker_momentum_window_ticks:
                recent_px = momentum_history[-1]
                old_px = momentum_history[-self.maker_momentum_window_ticks]
                trend_pct = (recent_px - old_px) / old_px if old_px > 0 else Decimal("0")
                
                if trend_pct <= -self.maker_momentum_filter_pct and "buy" in side_plan:
                    reduce_only_reason = f"momentum filter (dropped {float(trend_pct*100):.1f}%)"
                    side_plan["buy"] = (side_plan["buy"][0], side_plan["buy"][1], False)
                    if not getattr(self, "_logged_mom_buy", False) or time.time() - getattr(self, "_last_mom_ts", 0) > 30:
                        logger.warning(f"Trend Protection: {reduce_only_reason}. Blocking BUY orders.")
                        self._logged_mom_buy = True
                        self._last_mom_ts = time.time()
                elif "buy" in side_plan and getattr(self, "_logged_mom_buy", False):
                    self._logged_mom_buy = False
                    logger.info("Trend Protection: BUY blocking cleared.")
                    
                if trend_pct >= self.maker_momentum_filter_pct and "sell" in side_plan:
                    reduce_only_reason = f"momentum filter (pumped {float(trend_pct*100):.1f}%)"
                    side_plan["sell"] = (side_plan["sell"][0], side_plan["sell"][1], False)
                    if not getattr(self, "_logged_mom_sell", False) or time.time() - getattr(self, "_last_mom_ts_s", 0) > 30:
                        logger.warning(f"Trend Protection: {reduce_only_reason}. Blocking SELL orders.")
                        self._logged_mom_sell = True
                        self._last_mom_ts_s = time.time()
                elif "sell" in side_plan and getattr(self, "_logged_mom_sell", False):
                    self._logged_mom_sell = False
                    logger.info("Trend Protection: SELL blocking cleared.")
            # -------------------------------------------------------------------------
            
            # --- REDUCE ONLY MODE (Time & Extreme Price & Lifecycle Protection) ---
            reduce_only_reason = None
            reduce_only_tail_sell_block = False
            reduce_only_tail_sec_left = None
            if phase == MarketPhase.REDUCE_ONLY:
                end_ts = getattr(self, "current_market_end_timestamp", None)
                time_left_min = ((end_ts - now_ts) / 60.0) if end_ts else 0
                reduce_only_reason = f"lifecycle REDUCE_ONLY ({time_left_min:.1f}m left)"
                if end_ts is not None and self.maker_reduce_only_no_new_sell_last_sec > 0:
                    time_left_sec = end_ts - now_ts
                    if time_left_sec <= float(self.maker_reduce_only_no_new_sell_last_sec):
                        reduce_only_tail_sell_block = True
                        reduce_only_tail_sec_left = max(0.0, time_left_sec)
            elif fair < self.maker_min_fair_price:
                reduce_only_reason = f"fair {float(fair):.4f} < min {float(self.maker_min_fair_price):.4f}"
            elif fair > self.maker_max_fair_price:
                reduce_only_reason = f"fair {float(fair):.4f} > max {float(self.maker_max_fair_price):.4f}"
            else:
                end_ts = getattr(self, "current_market_end_timestamp", None)
                if end_ts is not None:
                    time_left_min = (end_ts - now_ts) / 60.0
                    if time_left_min < self.maker_min_minutes_to_close:
                        reduce_only_reason = f"only {time_left_min:.1f}m until close"
            
            if reduce_only_reason:
                if "buy" in side_plan:
                    if not getattr(self, "_logged_reduce_only", False) or time.time() - getattr(self, "_last_ro_log_ts", 0) > 60:
                        logger.warning(f"Maker Reduce-Only active ({reduce_only_reason}). Blocking BUY orders.")
                        self._logged_reduce_only = True
                        self._last_ro_log_ts = time.time()
                    side_plan["buy"] = (side_plan["buy"][0], side_plan["buy"][1], False)
                if "sell" in side_plan and reduce_only_tail_sell_block:
                    if (
                        not getattr(self, "_logged_reduce_only_tail_sell_block", False)
                        or time.time() - getattr(self, "_last_ro_tail_sell_log_ts", 0) > 60
                    ):
                        logger.warning(
                            "Maker Reduce-Only tail guard active "
                            f"({reduce_only_tail_sec_left:.1f}s left <= {self.maker_reduce_only_no_new_sell_last_sec}s). "
                            "Blocking new SELL quotes."
                        )
                        self._logged_reduce_only_tail_sell_block = True
                        self._last_ro_tail_sell_log_ts = time.time()
                    side_plan["sell"] = (side_plan["sell"][0], side_plan["sell"][1], False)

            elif "buy" in side_plan and getattr(self, "_logged_reduce_only", False):
                self._logged_reduce_only = False  # Reset if conditions normalize
                self._logged_extreme_sell_block = False
                self._logged_reduce_only_tail_sell_block = False
            # -----------------------------------------------------------

            # --- Balance-forced SELL-only mode ---
            if _balance_forced_sell_only and "buy" in side_plan:
                side_plan["buy"] = (side_plan["buy"][0], side_plan["buy"][1], False)

            for side, (limit_price, econ, should_quote) in side_plan.items():
                if side == "sell":
                    inst_key = self._instrument_key(inst_id)
                    sell_pause_until = float(self._sell_reject_pause_until_by_inst.get(inst_key, 0.0))
                    if now_ts < sell_pause_until:
                        should_quote = False
                order_key = self._order_key_for(side, inst_id)
                desired_quotes[order_key] = {
                    "side": side,
                    "instrument_id": inst_id,
                    "price": limit_price,
                    "econ": econ,
                    "should_quote": should_quote,
                    "dynamic_fee_rate": dynamic_fee_rate,
                }

        # Cancel orders that are no longer desired for target instruments.
        for order_key, state in list(self.active_maker_orders.items()):
            state_inst = str(state.get("instrument_id", "") or "")
            if state_inst not in target_inst_set:
                continue
            desired = desired_quotes.get(order_key)
            if desired is None or not bool(desired.get("should_quote", False)):
                self._cancel_maker_order_side(order_key, reason="risk")

        # Quote desired sides with selective requote.
        for order_key, desired in desired_quotes.items():
            if not bool(desired.get("should_quote", False)):
                continue
            side = str(desired["side"])
            inst_id = desired["instrument_id"]
            limit_price = Decimal(str(desired["price"]))
            econ = desired["econ"]
            dynamic_fee_rate = desired.get("dynamic_fee_rate")

            current = self.active_maker_orders.get(order_key)
            if current:
                if current.get("pending_cancel"):
                    continue
                current_price = Decimal(str(current.get("price", "0")))
                # Use per-instrument tick size here (not leaked from prior loop iterations).
                inst_for_tick = self._normalize_instrument_id(inst_id)
                inst_obj = self.cache.instrument(inst_for_tick) if inst_for_tick else None
                tick = Decimal("0.01")
                if inst_obj is not None:
                    try:
                        raw_tick = getattr(inst_obj, "price_increment", None)
                        if raw_tick is not None:
                            tick = Decimal(str(raw_tick))
                        elif hasattr(inst_obj, "info") and inst_obj.info:
                            maybe_tick = inst_obj.info.get("minimum_tick_size")
                            if maybe_tick is not None:
                                tick = Decimal(str(maybe_tick))
                    except Exception:
                        pass
                if tick <= 0:
                    tick = Decimal("0.01")
                distance = abs(current_price - limit_price)
                if distance < (self.maker_requote_hysteresis_ticks * tick):
                    continue
                
                # Token Bucket guard against cancel storms
                if self.requote_bucket_tokens < 1.0:
                    now_ts = time.time()
                    if now_ts - getattr(self, "_last_rl_log_ts", 0) > 10:
                        logger.warning(
                            f"Rate Limiter: out of requote tokens ({float(self.requote_bucket_tokens):.2f}/{self.maker_requote_max_per_sec}). "
                            "Skipping requote."
                        )
                        self._last_rl_log_ts = now_ts
                    continue
                
                self.requote_bucket_tokens -= 1.0

                # Safety-first requote: cancel existing order and wait for cancel ack/reconcile
                # before submitting a replacement. This prevents multiple live orders on the same
                # side/instrument when cancel acknowledgements are delayed or duplicated.
                self._cancel_maker_order_side(order_key, reason="requote")
                continue

            await self._submit_maker_quote(inst_id, side, limit_price, econ, dynamic_fee_rate)

    async def _submit_maker_quote(
        self,
        instrument_id: Any,
        side: str,
        limit_price: Decimal,
        econ,
        dynamic_fee_rate: Optional[Decimal] = None,
    ) -> None:
        instrument_id = self._normalize_instrument_id(instrument_id)
        instrument = self.cache.instrument(instrument_id) if instrument_id else None
        limit_price = self._align_price_to_tick(limit_price, side, instrument)
        precision = int(getattr(instrument, "size_precision", 6)) if instrument is not None else 6
        qty_dec = self._compute_maker_order_qty(limit_price, precision)
        projected_inventory = self._project_inventory_after_fill(side, qty_dec, instrument_id=instrument_id)
        if abs(projected_inventory) > self.maker_max_inventory_shares:
            logger.warning(
                "Skip maker quote: projected inventory would exceed max "
                f"(side={side}, qty={float(qty_dec):.6f}, projected={float(projected_inventory):.6f}, "
                f"max={float(self.maker_max_inventory_shares):.6f})"
            )
            self._db_order_event(
                event_type="ORDER_SKIP_INVENTORY_CAP",
                side=side.upper(),
                price=float(limit_price),
                qty=float(qty_dec),
                reason="projected_inventory_exceeds_max",
                payload={
                    "current_inventory": float(self.inventory_delta_shares),
                    "projected_inventory": float(projected_inventory),
                    "max_inventory": float(self.maker_max_inventory_shares),
                },
            )
            return

        # Live-only guard: prevent SELL submissions when we don't actually hold enough tokens.
        # If sellable is less than requested, REDUCE the qty to sellable amount instead of skipping.
        if side == "sell" and not self.current_simulation_mode:
            sellable_qty = self._get_effective_sellable_qty(instrument_id=instrument_id)
            if sellable_qty < Decimal("0.01"):
                # logger.debug(
                #     "Skip maker SELL: no sellable tokens "
                #     f"(sellable={float(sellable_qty):.6f}, instrument={self.instrument_id})"
                # )
                return
            if sellable_qty + Decimal("0.000001") < qty_dec:
                old_qty = qty_dec
                qty_dec = sellable_qty.quantize(Decimal(str(10 ** (-precision))))
                logger.info(
                    f"Maker SELL qty reduced to sellable amount: "
                    f"{float(old_qty):.6f} → {float(qty_dec):.6f} "
                    f"(on-chain tokens after fees)"
                )
            if qty_dec + Decimal("0.000001") < self.maker_exchange_min_shares:
                logger.info(
                    "Skip maker SELL: reduced sellable qty is below minimum trade size "
                    f"(qty={float(qty_dec):.6f}, min={float(self.maker_exchange_min_shares):.6f})"
                )
                return

        if self.current_simulation_mode:
            sim_order_id = f"SIM-MAKER-{side.upper()}-{int(time.time() * 1000)}"
            token_qty_sim = float(qty_dec)
            now_ts = time.time()
            order_key = self._order_key_for(side, instrument_id)
            ack_latency_ms = random.randint(
                min(self.sim_ack_latency_ms_min, self.sim_ack_latency_ms_max),
                max(self.sim_ack_latency_ms_min, self.sim_ack_latency_ms_max),
            )
            effective_fee_rate = dynamic_fee_rate
            if effective_fee_rate is None or effective_fee_rate <= 0:
                effective_fee_rate = self._infer_market_fee_rate_default()
            fee_rate_bps = int((effective_fee_rate * Decimal("10000")).quantize(Decimal("1")))
            self.active_maker_orders[order_key] = {
                "order": None,
                "simulated": True,
                "client_order_id": sim_order_id,
                "econ": econ,
                "price": limit_price,
                "side": side,
                "instrument_id": instrument_id,
                "token_id": self._extract_token_id_from_instrument(str(instrument_id)),
                "quantity": Decimal(str(token_qty_sim)),
                "created_ts": now_ts,
                "status": "PENDING_ACK",
                "ack_at": now_ts + (ack_latency_ms / 1000.0),
                "queue_rank": random.uniform(0.0, 1.0),
                "filled_qty": Decimal("0"),
                "entry_commission_usdc": Decimal("0"),
                "fee_rate_bps": fee_rate_bps,
            }
            self._db_order_event(
                event_type="ORDER_SIM_SUBMIT",
                client_order_id=sim_order_id,
                side=side.upper(),
                price=float(limit_price),
                qty=float(token_qty_sim),
                status="PENDING_ACK",
                expected_net_usdc=float(econ.expected_net_usdc),
                payload={
                    "ack_latency_ms": ack_latency_ms,
                    "queue_rank": self.active_maker_orders[order_key]["queue_rank"],
                    "fee_rate_bps": fee_rate_bps,
                },
            )
            logger.info(
                f"[SIM-MAKER] QUOTE {side.upper()} qty={token_qty_sim:.6f} px={float(limit_price):.4f} "
                f"net={float(econ.expected_net_usdc):.6f}"
            )
            return
        if not instrument_id:
            return
        if not instrument:
            return
        token_qty = float(qty_dec)
        qty = Quantity(token_qty, precision=precision)
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        price_precision = int(getattr(instrument, "price_precision", 3))
        price = Price.from_str(f"{float(limit_price):.{price_precision}f}")
        order_id = ClientOrderId(f"BTC-15M-MAKER-{side.upper()}-{int(time.time() * 1000)}")

        order_kwargs = dict(
            instrument_id=instrument_id,
            order_side=order_side,
            quantity=qty,
            price=price,
            client_order_id=order_id,
            quote_quantity=False,
            time_in_force=TimeInForce.GTC,
        )
        order = None
        if self.maker_use_post_only:
            try:
                order = self.order_factory.limit(**order_kwargs, post_only=True)
            except TypeError:
                if self.maker_post_only_strict:
                    logger.error("Order factory does not support post_only while strict mode is enabled; skip quote.")
                    return
                logger.warning("Order factory post_only unsupported; falling back to normal limit order.")
                self.maker_use_post_only = False
                order = self.order_factory.limit(**order_kwargs)
        else:
            order = self.order_factory.limit(**order_kwargs)

        # Final guard: Asymmetric Taker execution
        # Entry (BUY): Always refuse crossing quote to preserve Maker edge.
        # Exit (SELL): Allow crossing (Taker) to guarantee escape, unless strict post-only is on.
        quote = self._get_quote_for_instrument(instrument_id)
        if quote is not None:
            best_bid, best_ask = quote
            if side == "buy" and limit_price >= best_ask:
                logger.warning(f"Skip crossing BUY quote {float(limit_price):.4f} >= ask {float(best_ask):.4f}")
                return
            if side == "sell" and limit_price <= best_bid:
                if self.maker_use_post_only and getattr(self, "maker_post_only_strict", False):
                    logger.warning(f"Skip crossing SELL quote {float(limit_price):.4f} <= bid {float(best_bid):.4f}")
                    return

        self.submit_order(order)
        self.consecutive_denied_orders = 0
        order_key = self._order_key_for(side, instrument_id)
        self.active_maker_orders[order_key] = {
            "order": order,
            "econ": econ,
            "price": limit_price,
            "side": side,
            "instrument_id": instrument_id,
            "token_id": self._extract_token_id_from_instrument(str(instrument_id)),
            "quantity": Decimal(str(token_qty)),
            "created_ts": time.time(),
        }
        self._db_order_event(
            event_type="ORDER_SUBMIT",
            client_order_id=str(order.client_order_id),
            side=side.upper(),
            price=float(limit_price),
            qty=float(token_qty),
            status="SUBMITTED",
            expected_net_usdc=float(econ.expected_net_usdc),
            payload={
                "maker": True,
                "rebate_estimate_usdc": float(econ.expected_rebate_usdc),
                "spread_capture_estimate_usdc": float(econ.expected_spread_capture_usdc),
            },
        )
        self.rebate_reporter.record_quote(
            fee_equivalent=float(econ.fee_equivalent_usdc),
            rebate=float(econ.expected_rebate_usdc),
            spread_capture=float(econ.expected_spread_capture_usdc),
            expected_net=float(econ.expected_net_usdc),
        )
        logger.info(
            f"MAKER QUOTE {side.upper()} qty={token_qty:.6f} px={float(limit_price):.4f} "
            f"net={float(econ.expected_net_usdc):.6f} rebate={float(econ.expected_rebate_usdc):.6f} "
            f"inventory={float(self.inventory_delta_shares):.4f}"
        )
    
    def on_start(self):
        """Called when strategy starts."""
        logger.info("=" * 80)
        logger.info("INTEGRATED BTC STRATEGY STARTED")
        logger.info("=" * 80)
        self._log_strategy_config_summary()
        self.last_valid_quote_ts = time.time()
        self.consecutive_invalid_quote_ticks = 0
        
        # Find BTC instrument FIRST and wait for it
        if not self._wait_for_btc_instrument(timeout_sec=60, poll_interval_sec=2):
            raise RuntimeError("Startup check failed: no BTC 15-min instrument loaded")

        if self.trade_db:
            self.trade_db.log_run_start(
                run_id=self.run_id,
                mode="SIMULATION" if self.current_simulation_mode else "LIVE",
                test_mode=self.test_mode,
                maker_mode=self.maker_mode,
                instrument_id=str(self.instrument_id) if self.instrument_id else None,
                selected_slug=self.selected_slug,
                notes={
                    "quote_sides": self.maker_quote_sides,
                    "quote_size_usdc": float(self.maker_quote_size_usdc),
                },
            )
        self._db_strategy_event(
            "STRATEGY_START",
            {
                "instrument_id": str(self.instrument_id) if self.instrument_id else None,
                "selected_slug": self.selected_slug,
                "test_mode": self.test_mode,
            },
        )
        
        # Generate synthetic history regardless (for testing)
        # This ensures we have price data even if no BTC instrument found
        logger.info("Generating synthetic price history for testing...")
        
        # Check if we already have price history
        if len(self.price_history) < 20:
            # Generate synthetic history directly (not async since we're in sync context)
            self._generate_synthetic_history(target_count=20, existing_count=len(self.price_history))
        
        # Try to get real price if instrument exists and we have quotes
        if self.instrument_id:
            try:
                # Get the most recent quote from cache
                quote = self.cache.quote_tick(self.instrument_id)
                if quote and quote.bid_price and quote.ask_price:
                    current_price = (quote.bid_price + quote.ask_price) / 2
                    # Replace last synthetic price with real one
                    if self.price_history:
                        self.price_history[-1] = current_price
                    else:
                        self.price_history.append(current_price)
                    self.real_price_history.append(current_price)
                    if len(self.real_price_history) > self.max_real_history:
                        self.real_price_history.pop(0)
                    logger.info(f"Real price from cache: ${float(current_price):.4f}")
            except Exception as e:
                logger.debug(f"Could not get real price: {e}")
                logger.debug("Using synthetic prices until real quotes arrive")
        
        # Start market lifecycle timer (replaces fixed 12-min reload)
        self._lifecycle_stop_event.clear()
        self._lifecycle_thread = threading.Thread(target=self._start_market_lifecycle_timer, daemon=True)
        self._lifecycle_thread.start()
        # Initialize live Prometheus trading metrics
        self._init_live_prom_metrics()
        # Start Binance WebSocket for real-time BTC price
        self._start_binance_ws()
        # Also start the legacy reload timer as a fallback
        self._reload_stop_event.clear()
        self._reload_thread = threading.Thread(target=self._start_reload_timer, daemon=True)
        self._reload_thread.start()
        self._quote_watchdog_stop_event.clear()
        self._quote_watchdog_thread = threading.Thread(target=self._start_quote_watchdog_timer, daemon=True)
        self._quote_watchdog_thread.start()
        # Initialize phase based on current market
        self._update_market_phase()
        if self.auto_redeem_enabled:
            self._redeem_stop_event.clear()
            self._redeem_thread = threading.Thread(target=self._start_auto_redeem_timer, daemon=True)
            self._redeem_thread.start()
            self._schedule_auto_redeem(reason="startup")
        
        # Start Grafana if enabled
        if self.grafana_exporter:
            threading.Thread(target=self._start_grafana_sync, daemon=True).start()
        
        logger.info("=" * 80)
        logger.info("Strategy active - will trade every 15 minutes")
        logger.info(f"Price history: {len(self.price_history)} points")
        if len(self.price_history) >= 20:
            logger.info("✓ READY TO TRADE at next 15-minute mark!")
        else:
            logger.warning(f"⚠ Need more history ({len(self.price_history)}/20)")
        logger.info("=" * 80)
        logger.info("Use Ctrl+C to stop")
                
    def _preload_history_sync(self):
        """Synchronous wrapper for history preload."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._preload_price_history())
        finally:
            loop.close()
    
    async def _preload_price_history(self):
        """Pre-load price history from cache or generate synthetic data for testing."""
        logger.info("=" * 80)
        logger.info("PRE-LOADING PRICE HISTORY")
        logger.info("=" * 80)
        
        # Get current instrument
        if not self.instrument_id:
            logger.warning("No instrument ID, skipping preload")
            return
        
        # Try to get current price from cache first
        quote = self.cache.quote_tick(self.instrument_id)
        if quote:
            current_price = (quote.bid_price + quote.ask_price) / 2
            self.price_history.append(current_price)
            logger.info(f"Current price from cache: ${float(current_price):.4f}")
        
        # Try to get historical quotes from cache
        # Note: This depends on your data provider storing history
        quotes = self.cache.quote_tick(self.instrument_id)
        if quotes and len(quotes) > 0:
            for quote in quotes[-20:]:  # Take last 20 quotes
                mid_price = (quote.bid_price + quote.ask_price) / 2
                self.price_history.append(mid_price)
            logger.info(f"Loaded {len(quotes)} historical quotes from cache")
        
        # Remove duplicates while preserving order
        seen = set()
        unique_history = []
        for price in self.price_history:
            price_str = str(price)
            if price_str not in seen:
                seen.add(price_str)
                unique_history.append(price)
        self.price_history = unique_history
        
        # If still not enough, generate synthetic data
        if len(self.price_history) < 20:
            logger.warning(f"Only {len(self.price_history)} historical quotes found, generating synthetic data to fill")
            self._generate_synthetic_history(existing_count=len(self.price_history))
        
        logger.info(f"Final price history: {len(self.price_history)} points")
        if len(self.price_history) >= 20:
            logger.info("✓ SUFFICIENT HISTORY - Ready to trade!")
        else:
            logger.warning("⚠ Still need more history - will collect from live data")
        
        # Show first few prices
        logger.info("Sample price points:")
        for i, price in enumerate(self.price_history[:5]):
            logger.info(f"  Price {i+1}: ${float(price):.4f}")
        
        logger.info("=" * 80)
    
    def _generate_synthetic_history(self, target_count: int = 20, existing_count: int = 0):
        """Generate synthetic price history for testing/initialization."""
        # Get current price if available
        if self.price_history and len(self.price_history) > 0:
            base_price = self.price_history[-1]
            logger.info(f"Using last real price as base: ${float(base_price):.4f}")
        else:
            # Use a reasonable default for prediction markets
            base_price = Decimal("0.5")
            logger.info(f"No real price available, using default base: ${float(base_price):.4f}")
        
        needed = target_count - existing_count
        if needed <= 0:
            return
        
        logger.info(f"Generating {needed} synthetic price points")
        
        # Generate realistic looking price movement (random walk)
        for i in range(needed):
            # Random walk with small steps (±3% max change)
            change = Decimal(str(random.uniform(-0.03, 0.03)))
            new_price = base_price * (Decimal("1.0") + change)
            
            # Ensure price stays in 0-1 range for prediction markets
            new_price = max(Decimal("0.01"), min(Decimal("0.99"), new_price))
            
            self.price_history.append(new_price)
            base_price = new_price
        
        logger.info(f"Generated {needed} synthetic price points")
        logger.info(f"Now have {len(self.price_history)} total price points")
    
    def _start_reload_timer(self):
        """Start timer to reload instruments every 12 minutes."""
        while not self._reload_stop_event.wait(720):  # 12 minutes
            logger.info("=" * 80)
            logger.info("RELOADING INSTRUMENTS (12-minute interval)")
            logger.info("=" * 80)
            
            try:
                # Request instrument reload from data client
                instruments = self.cache.instruments()
                logger.info(f"Before reload: {len(instruments)} instruments in cache")
                
                # Re-find BTC instrument (this will select the active one)
                previous_slug = self.current_market_slug
                if not self._find_btc_instrument():
                    logger.warning("Reload completed but no BTC 15-min instrument found")
                elif self.auto_redeem_enabled and self.auto_redeem_on_rollover and previous_slug and self.current_market_slug and previous_slug != self.current_market_slug:
                    self._schedule_auto_redeem(reason=f"market_rollover:{previous_slug}->{self.current_market_slug}")
                
                logger.info("Instruments reloaded successfully")
            except Exception as e:
                logger.error(f"Failed to reload instruments: {e}")

    def _schedule_auto_redeem(self, reason: str) -> None:
        """
        Run redeem checker script in a detached worker so trading flow is never blocked.
        """
        if not self.auto_redeem_enabled:
            return

        def _runner() -> None:
            if not self._redeem_job_lock.acquire(blocking=False):
                logger.debug(f"Auto redeem skipped (already running): reason={reason}")
                return
            try:
                now_ts = time.time()
                self._last_redeem_run_ts = now_ts
                script = Path(__file__).parent / "scripts" / "check_positions_and_redeem.py"
                if not script.exists():
                    logger.warning(f"Auto redeem script not found: {script}")
                    return

                cmd = [sys.executable, str(script)]
                if self.auto_redeem_slug_filter:
                    cmd.extend(["--slug", self.auto_redeem_slug_filter])
                if self.auto_redeem_apply:
                    cmd.append("--apply")

                started = time.time()
                proc = subprocess.run(
                    cmd,
                    cwd=str(Path(__file__).parent),
                    capture_output=True,
                    text=True,
                    timeout=self.auto_redeem_timeout_sec,
                    check=False,
                )
                elapsed = time.time() - started
                stdout_tail = "\n".join((proc.stdout or "").strip().splitlines()[-8:])
                stderr_tail = "\n".join((proc.stderr or "").strip().splitlines()[-4:])
                logger.info(
                    f"Auto redeem run done: reason={reason} rc={proc.returncode} elapsed={elapsed:.1f}s "
                    f"apply={'ON' if self.auto_redeem_apply else 'OFF'}"
                )
                if stdout_tail:
                    logger.info(f"Auto redeem output (tail):\n{stdout_tail}")
                if stderr_tail:
                    logger.warning(f"Auto redeem stderr (tail):\n{stderr_tail}")
                self._db_strategy_event(
                    "AUTO_REDEEM_RUN",
                    {
                        "reason": reason,
                        "return_code": proc.returncode,
                        "elapsed_sec": elapsed,
                        "apply": self.auto_redeem_apply,
                    },
                )
            except subprocess.TimeoutExpired:
                logger.warning(f"Auto redeem timeout after {self.auto_redeem_timeout_sec}s (reason={reason})")
            except Exception as e:
                logger.warning(f"Auto redeem failed (reason={reason}): {e}")
            finally:
                self._redeem_job_lock.release()

        threading.Thread(target=_runner, daemon=True).start()

    def _start_auto_redeem_timer(self) -> None:
        """
        Periodic auto redeem timer (default every 15 minutes).
        Also checks for YES/NO merge opportunities.
        """
        while not self._redeem_stop_event.wait(self.auto_redeem_interval_sec):
            if self._stopping:
                return
            self._schedule_auto_redeem(reason="interval")
            # Check for merge opportunities periodically
            self._try_merge_yes_no_positions()

    def _try_merge_yes_no_positions(self) -> None:
        """
        Check if we hold both YES and NO tokens for the same condition.
        If so, merge the minimum overlapping amount back to USDC.
        This recovers locked capital.
        """
        try:
            pk = os.getenv("POLYMARKET_PK", "").strip()
            if not pk or int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")) != 0:
                return  # Can't do direct on-chain tx without EOA

            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            clob_base = os.getenv("POLYMARKET_CLOB_BASE_URL", "https://clob.polymarket.com").rstrip("/")
            rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
            chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))

            # Get current instruments to find YES/NO pairs
            instruments = self.cache.instruments() if hasattr(self, 'cache') else []
            if not instruments:
                return

            # Group instruments by condition_id (market)
            from collections import defaultdict
            condition_pairs: dict[str, list] = defaultdict(list)
            for inst in instruments:
                if hasattr(inst, 'info') and inst.info:
                    condition_id = inst.info.get('condition_id', '')
                    if condition_id:
                        token_id = inst.info.get('token_id', '')
                        if token_id:
                            condition_pairs[condition_id].append({
                                'token_id': token_id,
                                'instrument': inst,
                            })

            # Only check conditions with 2 tokens (YES + NO)
            if not hasattr(self, '_balance_clob_client') or self._balance_clob_client is None:
                return
            client = self._balance_clob_client

            for condition_id, tokens in condition_pairs.items():
                if len(tokens) < 2:
                    continue

                # Query on-chain balance for each token
                balances = []
                for t in tokens:
                    try:
                        params = BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL,
                            token_id=t['token_id'],
                            signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
                        )
                        result = client.get_balance_allowance(params)
                        balance_raw = int(result.get("balance", "0")) if result else 0
                        balances.append(balance_raw)
                    except Exception:
                        balances.append(0)

                # If we hold both tokens, merge the minimum amount
                min_balance = min(balances)
                if min_balance < 100000:  # Less than 0.1 USDC worth, skip
                    continue

                merge_amount_usdc = min_balance / 1_000_000
                logger.info(
                    f"Merge opportunity detected! condition={condition_id[:16]}... "
                    f"overlap={merge_amount_usdc:.4f} USDC — executing merge"
                )

                # Execute on-chain merge
                success = self._execute_merge_on_chain(
                    pk=pk,
                    condition_id=condition_id,
                    amount=min_balance,
                    rpc_url=rpc_url,
                    chain_id=chain_id,
                )
                
                # Deduct from live_inventory_cost if successful to prevent ghost inventory
                if success:
                    deduct_qty = Decimal(str(merge_amount_usdc))
                    for t in tokens:
                        inst_key = self._instrument_key(t['instrument'].id)
                        state = self.live_inventory_cost.get(inst_key)
                        if state:
                            old_qty = Decimal(str(state.get("qty", "0")))
                            if old_qty <= deduct_qty:
                                self.live_inventory_cost.pop(inst_key, None)
                            else:
                                state["qty"] = old_qty - deduct_qty
                                alloc = deduct_qty / old_qty if old_qty > 0 else Decimal("0")
                                state["entry_fee_remaining"] = state.get("entry_fee_remaining", Decimal("0")) * (Decimal("1") - alloc)
                    logger.info(f"Deducted {float(deduct_qty):.6f} from live_inventory_cost after merge.")

        except Exception as e:
            logger.debug(f"Merge check failed: {e}")

    def _execute_merge_on_chain(
        self, pk: str, condition_id: str, amount: int, rpc_url: str, chain_id: int
    ) -> bool:
        """Execute CTF mergePositions on-chain."""
        try:
            from web3 import Web3
            from web3.middleware import ExtraDataToPOAMiddleware

            CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
            USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            CTF_MERGE_ABI = [{
                "inputs": [
                    {"internalType": "address", "name": "collateralToken", "type": "address"},
                    {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
                    {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
                    {"internalType": "uint256[]", "name": "partition", "type": "uint256[]"},
                    {"internalType": "uint256", "name": "amount", "type": "uint256"},
                ],
                "name": "mergePositions",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function",
            }]

            w3 = Web3(Web3.HTTPProvider(rpc_url))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

            from eth_account import Account
            acct = Account.from_key(pk)
            owner = w3.to_checksum_address(acct.address)

            contract = w3.eth.contract(
                address=w3.to_checksum_address(CTF_ADDRESS),
                abi=CTF_MERGE_ABI,
            )

            tx = contract.functions.mergePositions(
                w3.to_checksum_address(USDC_ADDRESS),
                b"\x00" * 32,
                Web3.to_bytes(hexstr=condition_id),
                [1, 2],  # YES=1, NO=2
                amount,
            ).build_transaction({
                "chainId": chain_id,
                "from": owner,
                "nonce": w3.eth.get_transaction_count(owner, "pending"),
            })
            signed = w3.eth.account.sign_transaction(tx, private_key=pk)
            txh = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=120)
            usdc_recovered = amount / 1_000_000
            logger.info(
                f"✓ Merge SUCCESS: condition={condition_id[:16]}... "
                f"recovered={usdc_recovered:.4f} USDC tx={txh.hex()} status={receipt.status}"
            )
            return receipt.status == 1
        except Exception as e:
            logger.warning(f"Merge on-chain failed: {e}")
            return False

    def _trigger_quote_watchdog_reload(self, trigger: str, now_ts: float) -> None:
        """
        Recover quote stream when valid bid/ask updates disappear for too long.
        """
        if now_ts - self.last_quote_watchdog_reload_ts < self.quote_reload_cooldown_sec:
            return
        self.last_quote_watchdog_reload_ts = now_ts

        prev_instrument = str(self.instrument_id) if self.instrument_id else None
        stale_for = (now_ts - self.last_valid_quote_ts) if self.last_valid_quote_ts > 0 else None
        logger.warning(
            "Quote watchdog triggered: "
            f"trigger={trigger} stale_for={stale_for if stale_for is not None else -1:.1f}s "
            f"invalid_ticks={self.consecutive_invalid_quote_ticks}"
        )
        self._db_strategy_event(
            "QUOTE_WATCHDOG_TRIGGERED",
            {
                "trigger": trigger,
                "stale_for_sec": stale_for,
                "invalid_ticks": self.consecutive_invalid_quote_ticks,
                "instrument_before": prev_instrument,
            },
        )

        # Prevent stale maker orders while market data is degraded.
        self._cancel_active_maker_orders()

        selected_ok = self._find_btc_instrument()
        new_instrument = str(self.instrument_id) if self.instrument_id else None
        if selected_ok and self.instrument_id is not None:
            logger.warning(f"Quote watchdog recovery complete: {prev_instrument} -> {new_instrument}")
            self._db_strategy_event(
                "QUOTE_WATCHDOG_RECOVERED",
                {
                    "instrument_before": prev_instrument,
                    "instrument_after": new_instrument,
                },
            )
            self.consecutive_invalid_quote_ticks = 0
            self.last_valid_quote_ts = now_ts
            return

        logger.error("Quote watchdog recovery failed: no BTC 15-min instrument selected")
        self._db_strategy_event(
            "QUOTE_WATCHDOG_FAILED",
            {
                "instrument_before": prev_instrument,
                "instrument_after": new_instrument,
            },
        )

    def _maybe_run_quote_watchdog(self, trigger: str) -> None:
        now_ts = time.time()
        if now_ts - self.last_quote_watchdog_check_ts < self.quote_healthcheck_interval_sec:
            return
        self.last_quote_watchdog_check_ts = now_ts

        stale_for = (now_ts - self.last_valid_quote_ts) if self.last_valid_quote_ts > 0 else 0.0
        stale_hit = self.last_valid_quote_ts > 0 and stale_for >= self.quote_stale_sec
        invalid_hit = self.consecutive_invalid_quote_ticks >= self.quote_invalid_tick_reload_threshold
        if not stale_hit and not invalid_hit:
            return

        reason = trigger
        if stale_hit:
            reason = f"{reason}|stale_quotes"
        if invalid_hit:
            reason = f"{reason}|invalid_ticks"
        self._trigger_quote_watchdog_reload(reason, now_ts)

    def _start_quote_watchdog_timer(self) -> None:
        """
        Background heartbeat for quote health.
        Needed because DataClient can drop incomplete ticks before strategy receives them.
        """
        while not self._quote_watchdog_stop_event.wait(self.quote_healthcheck_interval_sec):
            if self._stopping:
                return
            now_ts = time.time()
            self._emit_strategy_status(now_ts)
            stale_for = (now_ts - self.last_valid_quote_ts) if self.last_valid_quote_ts > 0 else None
            if stale_for is None or stale_for < self.quote_stale_sec:
                continue
            self._trigger_quote_watchdog_reload("timer_stale_quotes", now_ts)

    def _emit_strategy_status(self, now_ts: float) -> None:
        """
        Periodic concise status line to explain why bot is (not) quoting.
        """
        if now_ts - self.last_status_log_ts < self.strategy_status_interval_sec:
            return
        self.last_status_log_ts = now_ts

        reasons: List[str] = []
        if self._stopping:
            reasons.append("stopping")
        if not self.maker_mode:
            reasons.append("maker_mode_off")
        if self.maker_kill_switch:
            reasons.append("kill_switch_on")
        if now_ts < self.quote_pause_until_ts:
            reasons.append(f"paused_{int(self.quote_pause_until_ts - now_ts)}s")
        if now_ts < self.orderbook_unavailable_until_ts:
            reasons.append(f"orderbook_unavailable_{int(self.orderbook_unavailable_until_ts - now_ts)}s")
        if self.latest_market_bid is None or self.latest_market_ask is None:
            reasons.append("no_valid_quote")
        if self.instrument_id is None:
            reasons.append("no_instrument")

        bid_txt = f"{float(self.latest_market_bid):.4f}" if self.latest_market_bid is not None else "None"
        ask_txt = f"{float(self.latest_market_ask):.4f}" if self.latest_market_ask is not None else "None"
        stale_for = (now_ts - self.last_valid_quote_ts) if self.last_valid_quote_ts > 0 else -1.0
        active_orders = list(self.active_maker_orders.keys())
        tradable = "YES" if len(reasons) == 0 else "NO"
        reason_txt = "ok" if len(reasons) == 0 else ",".join(reasons)

        logger.info(
            "STATUS "
            f"tradable={tradable} reason={reason_txt} "
            f"phase={self.market_phase.value} "
            f"slug={self.current_market_slug or '-'} "
            f"instrument={self.instrument_id or '-'} "
            f"bid={bid_txt} ask={ask_txt} "
            f"stale_for={stale_for:.1f}s invalid_ticks={self.consecutive_invalid_quote_ticks} "
            f"inventory={float(self.inventory_delta_shares):.4f}/{float(self.maker_max_inventory_shares):.4f} "
            f"active_orders={active_orders}"
            f"{self._format_time_left()}"
        )

    def _format_time_left(self) -> str:
        """Format remaining time and next-market info for status line."""
        parts: List[str] = []
        end_ts = getattr(self, "current_market_end_timestamp", None)
        if end_ts is not None:
            remaining = end_ts - time.time()
            if remaining > 0:
                parts.append(f" time_left={remaining / 60:.1f}m")
            else:
                parts.append(f" time_left=ENDED({abs(remaining):.0f}s ago)")
        if self.next_market_slug:
            if self.next_market_start_ts:
                until = self.next_market_start_ts - time.time()
                parts.append(f" next={self.next_market_slug}(in {until / 60:.1f}m)")
            else:
                parts.append(f" next={self.next_market_slug}")
        return "".join(parts)

    # ------------------------------------------------------------------
    # Market Lifecycle State Machine
    # ------------------------------------------------------------------

    def _update_market_phase(self) -> MarketPhase:
        """
        Evaluate current time vs market end timestamp and transition
        between lifecycle phases.

        Returns the current phase after evaluation.
        """
        now_ts = time.time()
        prev_phase = self.market_phase
        end_ts = getattr(self, "current_market_end_timestamp", None)

        if end_ts is None:
            # No market end time known — we're waiting.
            if self.market_phase not in (MarketPhase.WAITING, MarketPhase.SETTLING):
                self._transition_market_phase(MarketPhase.WAITING, now_ts)
            return self.market_phase

        time_left_sec = end_ts - now_ts
        time_left_min = time_left_sec / 60.0

        if time_left_sec > self.maker_min_minutes_to_close * 60:
            # Plenty of time — we're active.
            if self.market_phase != MarketPhase.ACTIVE:
                self._transition_market_phase(MarketPhase.ACTIVE, now_ts)

        elif time_left_sec > 0:
            # Close to end — reduce only.
            if self.market_phase not in (MarketPhase.REDUCE_ONLY, MarketPhase.SETTLING):
                self._transition_market_phase(MarketPhase.REDUCE_ONLY, now_ts)

        else:
            # Market has ended.
            if self.market_phase == MarketPhase.SETTLING:
                # Check if grace period is over.
                if now_ts - self._market_settling_since_ts >= self.market_settling_grace_sec:
                    self._transition_market_phase(MarketPhase.WAITING, now_ts)
            elif self.market_phase != MarketPhase.WAITING:
                self._transition_market_phase(MarketPhase.SETTLING, now_ts)
                self._market_settling_since_ts = now_ts

        return self.market_phase

    def _transition_market_phase(self, new_phase: MarketPhase, now_ts: float) -> None:
        """Log and record a market phase transition."""
        old_phase = self.market_phase
        self.market_phase = new_phase

        end_ts = getattr(self, "current_market_end_timestamp", None)
        time_left = (end_ts - now_ts) if end_ts else None

        logger.warning(
            f"MARKET PHASE: {old_phase.value} → {new_phase.value} "
            f"slug={self.current_market_slug or '-'} "
            f"time_left={time_left / 60:.1f}m" if time_left is not None else
            f"MARKET PHASE: {old_phase.value} → {new_phase.value} "
            f"slug={self.current_market_slug or '-'} "
            f"time_left=N/A"
        )
        self._db_strategy_event(
            "MARKET_PHASE_CHANGE",
            {
                "from": old_phase.value,
                "to": new_phase.value,
                "slug": self.current_market_slug,
                "time_left_sec": time_left,
            },
        )

        # Actions on transition
        if new_phase == MarketPhase.SETTLING:
            self._cancel_active_maker_orders()
            logger.info("Settling: all maker orders cancelled. Waiting for grace period.")
        elif new_phase == MarketPhase.WAITING:
            self._cancel_active_maker_orders()
            # Clear stale market end timestamp so we don't re-enter SETTLING.
            self.current_market_end_timestamp = None
            logger.info("Waiting: proactively searching for next market.")
        elif new_phase == MarketPhase.REDUCE_ONLY:
            # Cancel buy-side orders immediately.
            for order_key, state in list(self.active_maker_orders.items()):
                if str(state.get("side", "")) == "buy":
                    self._cancel_maker_order_side(order_key, reason="reduce_only")

    # ------------------------------------------------------------------
    # Proactive Next-Market Detection
    # ------------------------------------------------------------------

    def _search_next_market(self) -> bool:
        """
        Proactively search for the next BTC 15-min market.
        Updates self.next_market_slug and self.next_market_start_ts.
        If a viable market is found, switches to it.

        Returns True if a new market was found and activated.
        """
        try:
            btc_slugs = resolve_btc_15m_market_slugs()
            if not btc_slugs:
                logger.debug("Next market search: no slugs found")
                self.next_market_slug = None
                self.next_market_start_ts = None
                return False

            now_ts = time.time()
            best_slug = None
            best_start_ts = None

            for slug in btc_slugs:
                # Extract timestamp from slug (e.g. btc-updown-15m-1771800300)
                try:
                    ts_str = slug.rsplit("-", 1)[-1]
                    start_ts = int(ts_str)
                except (ValueError, IndexError):
                    continue

                end_ts = start_ts + 900  # 15 minutes

                # We want markets that haven't ended yet.
                if end_ts <= now_ts:
                    continue

                if best_start_ts is None or start_ts < best_start_ts:
                    best_slug = slug
                    best_start_ts = start_ts

            if best_slug:
                self.next_market_slug = best_slug
                self.next_market_start_ts = best_start_ts
                time_until = best_start_ts - now_ts if best_start_ts else 0
                logger.info(
                    f"Next market found: {best_slug} "
                    f"(starts in {time_until / 60:.1f}m)"
                )

                # Try to switch to this market
                previous_slug = self.current_market_slug
                if self._find_btc_instrument():
                    if self.current_market_slug != previous_slug:
                        logger.info(
                            f"Switched to new market: {previous_slug} → {self.current_market_slug}"
                        )
                        if self.auto_redeem_enabled and self.auto_redeem_on_rollover and previous_slug:
                            self._schedule_auto_redeem(
                                reason=f"lifecycle_rollover:{previous_slug}->{self.current_market_slug}"
                            )
                    return True
            else:
                self.next_market_slug = None
                self.next_market_start_ts = None
                logger.debug("Next market search: no future markets found")

        except Exception as e:
            logger.warning(f"Next market search failed: {e}")

        return False

    # ------------------------------------------------------------------
    # Smart Market Lifecycle Timer (replaces fixed 12-min reload)
    # ------------------------------------------------------------------

    def _start_market_lifecycle_timer(self) -> None:
        """
        Market-aware timer that replaces the fixed 12-minute reload.
        Sleeps intelligently based on market end time and transitions
        the lifecycle state machine.
        """
        while not self._lifecycle_stop_event.is_set():
            if self._stopping:
                return

            now_ts = time.time()
            phase = self._update_market_phase()
            end_ts = getattr(self, "current_market_end_timestamp", None)

            if phase == MarketPhase.ACTIVE:
                # Sleep until near market end, checking periodically.
                if end_ts is not None:
                    time_until_reduce = end_ts - now_ts - (self.maker_min_minutes_to_close * 60)
                    if time_until_reduce > 30:
                        # Sleep in chunks to allow stopping
                        sleep_sec = min(time_until_reduce - 10, 60)
                        self._lifecycle_stop_event.wait(max(5, sleep_sec))
                    else:
                        self._lifecycle_stop_event.wait(5)
                else:
                    # No end timestamp — fallback polling.
                    self._lifecycle_stop_event.wait(60)
                    # Try to reload instruments.
                    try:
                        if not self._find_btc_instrument():
                            logger.warning("Lifecycle timer: no BTC instrument found")
                    except Exception as e:
                        logger.error(f"Lifecycle timer reload failed: {e}")

            elif phase == MarketPhase.REDUCE_ONLY:
                # Poll frequently until market ends.
                self._lifecycle_stop_event.wait(5)

            elif phase == MarketPhase.SETTLING:
                # Wait for grace period to expire.
                remaining_grace = self.market_settling_grace_sec - (now_ts - self._market_settling_since_ts)
                if remaining_grace > 0:
                    self._lifecycle_stop_event.wait(min(remaining_grace, 5))
                # Phase will transition on next _update_market_phase call.

            elif phase == MarketPhase.WAITING:
                # Actively search for next market.
                found = self._search_next_market()
                if found:
                    logger.info("Lifecycle timer: new market found, transitioning to ACTIVE")
                    # _find_btc_instrument already called inside _search_next_market
                    self._update_market_phase()
                    self._waiting_miss_count = 0
                else:
                    self._waiting_miss_count = getattr(self, '_waiting_miss_count', 0) + 1
                    max_waiting_misses = int(os.getenv("MARKET_WAITING_MAX_MISSES", "3"))
                    if self._waiting_miss_count >= max_waiting_misses and self.next_market_slug:
                        # We know the next slug but can't find its instrument in Nautilus
                        # → force a node rebuild to load fresh instruments
                        logger.warning(
                            f"Lifecycle timer: {self._waiting_miss_count} consecutive misses for "
                            f"{self.next_market_slug}. Instruments stale — requesting node rollover."
                        )
                        self._waiting_miss_count = 0
                        self._stopping = True
                        self._rollover_requested_flag = True
                        try:
                            import nautilus_trader  # noqa: F811
                            # Signal the trading node to stop, which triggers a rebuild in the outer loop
                            if hasattr(self, '_trader') and hasattr(self._trader, 'node'):
                                self._trader.node.stop()
                            else:
                                # Fallback — raise to break out of the lifecycle loop
                                raise SystemExit("rollover_needed")
                        except SystemExit:
                            raise
                        except Exception:
                            pass
                        return  # Exit lifecycle timer thread
                    logger.info(
                        f"Lifecycle timer: no market yet (miss {self._waiting_miss_count}/"
                        f"{max_waiting_misses}), retry in {self.market_next_poll_sec}s"
                    )
                    self._lifecycle_stop_event.wait(self.market_next_poll_sec)

    # ------------------------------------------------------------------
    # Balance Pre-check
    # ------------------------------------------------------------------

    def _refresh_balance_cache(self) -> Optional[Decimal]:
        """
        Refresh cached USDC balance from CLOB API (get_balance_allowance).
        Only queries if interval has elapsed.
        Returns the cached balance.
        """
        now_ts = time.time()
        if now_ts - self._balance_last_check_ts < self.balance_check_interval_sec:
            return self._cached_usdc_balance

        self._balance_last_check_ts = now_ts
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams

            # Reuse cached client if available
            if not hasattr(self, '_balance_clob_client') or self._balance_clob_client is None:
                clob_base = os.getenv("POLYMARKET_CLOB_BASE_URL", "https://clob.polymarket.com").rstrip("/")
                pk = os.getenv("POLYMARKET_PK")
                if not pk:
                    return self._cached_usdc_balance

                client = ClobClient(
                    host=clob_base,
                    key=pk,
                    chain_id=int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
                    signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
                    funder=os.getenv("POLYMARKET_FUNDER") or None,
                )
                api_key = os.getenv("POLYMARKET_API_KEY")
                api_secret = os.getenv("POLYMARKET_API_SECRET")
                passphrase = os.getenv("POLYMARKET_PASSPHRASE")
                if api_key and api_secret and passphrase:
                    client.set_api_creds(ApiCreds(
                        api_key=api_key,
                        api_secret=api_secret,
                        api_passphrase=passphrase,
                    ))
                else:
                    # Env vars are empty — derive creds from private key (same as Nautilus)
                    try:
                        derived = client.create_or_derive_api_creds()
                        client.set_api_creds(derived)
                        logger.info("Balance cache: derived API creds from private key")
                    except Exception as e:
                        logger.warning(f"Balance cache: failed to derive API creds: {e}")
                        return self._cached_usdc_balance
                self._balance_clob_client = client

            client = self._balance_clob_client

            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
            )
            result = client.get_balance_allowance(params)
            if result and isinstance(result, dict):
                balance_val = result.get("balance")
                if balance_val is not None:
                    self._cached_usdc_balance = Decimal(str(balance_val)) / Decimal("1000000")  # USDC has 6 decimals
                    logger.info(f"Balance cache updated: {float(self._cached_usdc_balance):.4f} USDC")
                    # Export real balance to Prometheus for Grafana
                    try:
                        if not hasattr(self, '_prom_wallet_balance'):
                            from prometheus_client import Gauge
                            self._prom_wallet_balance = Gauge(
                                'trading_wallet_balance_usdc',
                                'Real on-chain wallet USDC balance'
                            )
                        self._prom_wallet_balance.set(float(self._cached_usdc_balance))
                    except Exception:
                        pass
                    return self._cached_usdc_balance

        except Exception as e:
            logger.debug(f"Balance cache refresh failed: {e}")

        return self._cached_usdc_balance

    def _align_price_to_tick(self, price: Decimal, side: str, instrument: Optional[Any]) -> Decimal:
        """
        Align quote price to current instrument tick size and precision.
        """
        aligned = price
        tick = Decimal("0.001")
        try:
            if instrument is not None:
                raw_tick = getattr(instrument, "price_increment", None)
                if raw_tick is not None:
                    if hasattr(raw_tick, "as_decimal"):
                        tick = Decimal(str(raw_tick.as_decimal()))
                    else:
                        tick = Decimal(str(raw_tick))
                elif hasattr(instrument, "info") and instrument.info:
                    min_tick = instrument.info.get("minimum_tick_size")
                    if min_tick:
                        tick = Decimal(str(min_tick))
        except Exception:
            tick = Decimal("0.001")

        if tick <= 0:
            tick = Decimal("0.001")
        units = aligned / tick
        if side == "buy":
            aligned = units.to_integral_value(rounding=ROUND_FLOOR) * tick
        else:
            aligned = units.to_integral_value(rounding=ROUND_CEILING) * tick

        aligned = max(Decimal("0.01"), min(Decimal("0.99"), aligned))
        try:
            if instrument is not None:
                precision = int(getattr(instrument, "price_precision", 3))
                precision = max(0, precision)
                quantum = Decimal("1").scaleb(-precision)
                aligned = aligned.quantize(quantum)
        except Exception:
            pass
        return aligned

    def _start_maker_worker(self, bid_decimal: Decimal, ask_decimal: Decimal) -> None:
        with self._maker_worker_lock:
            if self._maker_worker_running or self._stopping:
                return
            self._maker_worker_running = True

        def _worker():
            try:
                self._maker_quote_sync(float(bid_decimal), float(ask_decimal))
            finally:
                with self._maker_worker_lock:
                    self._maker_worker_running = False

        threading.Thread(target=_worker, daemon=True).start()

    def _start_decision_worker(self, mid_price: Decimal) -> None:
        with self._decision_worker_lock:
            if self._decision_worker_running or self._stopping:
                return
            self._decision_worker_running = True

        def _worker():
            try:
                self._make_trading_decision_sync(float(mid_price))
            finally:
                with self._decision_worker_lock:
                    self._decision_worker_running = False

        threading.Thread(target=_worker, daemon=True).start()
                
    def _start_grafana_sync(self):
        """Start Grafana in separate thread."""
        try:
            self.grafana_exporter.start()
            logger.info("Grafana metrics started on port 8000")
        except Exception as e:
            logger.error(f"Failed to start Grafana: {e}")
    
    def _find_btc_instrument(self):
        """Find the CURRENT active BTC 15-min instrument."""
        instruments = self.cache.instruments()
        logger.info(f"Checking {len(instruments)} loaded instruments...")
        
        if not instruments:
            logger.error("NO INSTRUMENTS LOADED!")
            return False
        
        # Get current UTC time
        now = datetime.now(timezone.utc)
        current_timestamp = int(now.timestamp())
        
        btc_instruments = []
        
        seen_ids = set()
        for instrument in instruments:
            try:
                inst_id = str(getattr(instrument, "id", ""))
                if inst_id in seen_ids:
                    continue
                seen_ids.add(inst_id)
                if hasattr(instrument, 'info') and instrument.info:
                    question = instrument.info.get('question', '').lower()
                    slug = instrument.info.get('market_slug', '').lower()
                    
                    if ('btc' in question or 'btc' in slug) and '15m' in slug:
                        # Extract timestamp from slug
                        try:
                            timestamp_part = slug.split('-')[-1]
                            market_timestamp = int(timestamp_part)
                            
                            # Get end time from instrument
                            end_date = instrument.info.get('end_date_iso')
                            end_timestamp = None
                            if end_date:
                                end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                                end_timestamp = int(end_dt.timestamp())
                            
                            # Calculate time difference
                            time_diff = market_timestamp - current_timestamp
                            
                            btc_instruments.append({
                                'instrument': instrument,
                                'slug': slug,
                                'market_timestamp': market_timestamp,
                                'end_timestamp': end_timestamp,
                                'question': question,
                                'active': instrument.info.get('active', False),
                                'closed': instrument.info.get('closed', True),
                                'time_diff_minutes': time_diff / 60,  # Minutes from now
                            })
                            
                        except (ValueError, IndexError):
                            continue
            
            except Exception:
                continue
        
        if not btc_instruments:
            logger.error("NO BTC 15-MIN INSTRUMENTS FOUND!")
            return False

        # Keep only markets that are still active or not yet ended (small grace for clock skew).
        alive = []
        for item in btc_instruments:
            end_ts = item.get("end_timestamp")
            closed = bool(item.get("closed", False))
            if closed:
                continue
            if end_ts is not None and end_ts < (current_timestamp - 60):
                continue
            alive.append(item)
        if alive:
            btc_instruments = alive
        
        # Sort by how close they are to current time (positive means future)
        # We want the one that started most recently (smallest positive time_diff)
        current_markets = [i for i in btc_instruments if i['time_diff_minutes'] <= 0 and i['time_diff_minutes'] > -15]
        future_markets = [i for i in btc_instruments if i['time_diff_minutes'] > 0]
        
        logger.info("=" * 80)
        logger.info("BTC 15-MIN INSTRUMENTS:")
        for i in btc_instruments:
            status = "CURRENT" if i in current_markets else "FUTURE" if i['time_diff_minutes'] > 0 else "PAST"
            logger.info(f"  {i['slug']}: {status} (starts in {i['time_diff_minutes']:.1f} min)")
        logger.info("=" * 80)
        
        # Select the current market if available
        if current_markets:
            # Sort by closest to now
            current_markets.sort(key=lambda x: abs(x['time_diff_minutes']))
            selected = current_markets[0]
            logger.info(f"✓ SELECTED CURRENT market: {selected['slug']}")
        elif future_markets:
            # Select the next future market
            future_markets.sort(key=lambda x: x['time_diff_minutes'])
            selected = future_markets[0]
            logger.info(f"⚠ No current market, selecting next: {selected['slug']} (starts in {selected['time_diff_minutes']:.1f} min)")
        else:
            logger.warning("No active or future BTC 15-min instruments found in cache. All are PAST.")
            return False
        
        previous_instrument = str(self.instrument_id) if self.instrument_id else None
        self.current_market_slug = str(selected.get("slug") or "")
        
        # The market_timestamp from the slug is the START time of the 15-min market.
        # The true end time is exactly 15 minutes (900 seconds) later.
        start_ts = selected.get("market_timestamp")
        self.current_market_end_timestamp = start_ts + 900 if start_ts else None
        market_instruments = []
        for item in btc_instruments:
            if str(item.get("slug") or "") != self.current_market_slug:
                continue
            inst = item.get("instrument")
            if inst is None:
                continue
            inst_id = getattr(inst, "id", None)
            if inst_id is None:
                continue
            if str(inst_id) not in {str(x) for x in market_instruments}:
                market_instruments.append(inst_id)

        if self.maker_quote_sides == "both_buy" and market_instruments:
            # Prefer deterministic ordering: Up then Down when metadata is available.
            ordered: List[InstrumentId] = []
            up_id: Optional[InstrumentId] = None
            down_id: Optional[InstrumentId] = None
            for item in btc_instruments:
                if str(item.get("slug") or "") != self.current_market_slug:
                    continue
                inst = item.get("instrument")
                if inst is None:
                    continue
                inst_id = getattr(inst, "id", None)
                if inst_id is None:
                    continue
                outcome = self._extract_outcome_from_instrument(inst)
                if outcome == "up":
                    up_id = inst_id
                elif outcome == "down":
                    down_id = inst_id
            if up_id is not None:
                ordered.append(up_id)
            if down_id is not None and str(down_id) != str(up_id):
                ordered.append(down_id)
            for inst_id in market_instruments:
                if str(inst_id) not in {str(x) for x in ordered}:
                    ordered.append(inst_id)
            market_instruments = ordered

        self.current_market_instruments = market_instruments or [selected["instrument"].id]
        self.instrument_id = self.current_market_instruments[0]
        self.current_token_id = self._extract_token_id_from_instrument(str(self.instrument_id))
        self._reset_maker_state_for_new_market(previous_instrument, str(self.instrument_id))
        for inst_id in self.current_market_instruments:
            self.subscribe_quote_ticks(inst_id)
        return True

    def _wait_for_btc_instrument(self, timeout_sec: int = 60, poll_interval_sec: int = 2) -> bool:
        """
        Wait for instruments to arrive in cache during startup.
        """
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self._find_btc_instrument():
                return True
            time.sleep(poll_interval_sec)
        return False
                        
    def on_quote_tick(self, tick: QuoteTick):
        """Handle quote tick updates."""
        if self._stopping:
            return
        try:
            if self.instrument_id is not None and tick.instrument_id != self.instrument_id:
                allowed = {str(i) for i in (self.current_market_instruments or [])}
                if str(tick.instrument_id) not in allowed:
                    return

            # Check if we have valid prices
            if tick.bid_price is None and tick.ask_price is None:
                self.consecutive_invalid_quote_ticks += 1
                logger.debug(f"Skipping empty quote: bid={tick.bid_price}, ask={tick.ask_price}")
                self._maybe_run_quote_watchdog(trigger="empty_quote")
                return

            bid_decimal = tick.bid_price.as_decimal() if tick.bid_price is not None else None
            ask_decimal = tick.ask_price.as_decimal() if tick.ask_price is not None else None
            bid_size_decimal: Optional[Decimal] = None
            ask_size_decimal: Optional[Decimal] = None
            try:
                if getattr(tick, "bid_size", None) is not None:
                    bs = tick.bid_size
                    bid_size_decimal = bs.as_decimal() if hasattr(bs, "as_decimal") else Decimal(str(bs))
            except Exception:
                bid_size_decimal = None
            try:
                if getattr(tick, "ask_size", None) is not None:
                    a_s = tick.ask_size
                    ask_size_decimal = a_s.as_decimal() if hasattr(a_s, "as_decimal") else Decimal(str(a_s))
            except Exception:
                ask_size_decimal = None

            # Tolerate one-sided quotes to avoid long no-quote stalls around market transitions.
            if bid_decimal is None and ask_decimal is not None:
                if self.latest_market_bid is not None:
                    bid_decimal = self.latest_market_bid
                else:
                    bid_decimal = max(Decimal("0.01"), ask_decimal - Decimal("0.01"))
            if ask_decimal is None and bid_decimal is not None:
                if self.latest_market_ask is not None:
                    ask_decimal = self.latest_market_ask
                else:
                    ask_decimal = min(Decimal("0.99"), bid_decimal + Decimal("0.01"))

            if bid_decimal is None or ask_decimal is None:
                self.consecutive_invalid_quote_ticks += 1
                self._maybe_run_quote_watchdog(trigger="incomplete_quote")
                return

            if bid_decimal > ask_decimal:
                # Keep ordering sane in stressed snapshots.
                mid_tmp = (bid_decimal + ask_decimal) / 2
                bid_decimal = max(Decimal("0.01"), mid_tmp - Decimal("0.005"))
                ask_decimal = min(Decimal("0.99"), mid_tmp + Decimal("0.005"))

            self.last_valid_quote_ts = time.time()
            self.consecutive_invalid_quote_ticks = 0
            self.latest_market_bid = bid_decimal
            self.latest_market_ask = ask_decimal
            self.latest_quote_depth_by_inst[str(tick.instrument_id)] = (bid_size_decimal, ask_size_decimal)
            
            # Calculate mid price
            mid_price = (bid_decimal + ask_decimal) / 2
            
            # Update price history
            self.price_history.append(mid_price)
            self._append_real_mid_price(tick.instrument_id, mid_price)
            
            # Limit history size
            if len(self.price_history) > self.max_history:
                self.price_history.pop(0)
            if self.maker_mode:
                self._start_maker_worker(bid_decimal, ask_decimal)
                return
            
            # Check if we should trade
            now = datetime.now(timezone.utc)
            
            if self.test_mode:
                # TEST MODE: Trade every minute at the start of each minute
                current_minute = now.replace(second=0, microsecond=0)
                seconds_since_minute = now.second
                
                if seconds_since_minute < 5:  # Within first 5 seconds of each minute
                    if current_minute != self.last_trade_time:
                        self.last_trade_time = current_minute
                        logger.info("=" * 80)
                        logger.info(f"TEST MODE - MINUTE REACHED: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                        logger.info(f"Current price: ${float(mid_price):,.4f}")
                        logger.info(f"Bid: ${float(bid_decimal):,.4f}, Ask: ${float(ask_decimal):,.4f}")
                        logger.info(f"Price history size: {len(self.price_history)}")
                        logger.info("=" * 80)
                        
                        # Make trading decision
                        self._start_decision_worker(mid_price)
            else:
                # NORMAL MODE: Trade every 15 minutes
                seconds_since_interval = now.timestamp() % 900
                
                if seconds_since_interval < 30:
                    current_interval = int(now.timestamp() // 900)
                    
                    if current_interval != self.last_trade_time:
                        self.last_trade_time = current_interval
                        logger.info("=" * 80)
                        logger.info(f"15-MIN INTERVAL REACHED: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                        logger.info(f"Current price: ${float(mid_price):,.4f}")
                        logger.info(f"Bid: ${float(bid_decimal):,.4f}, Ask: ${float(ask_decimal):,.4f}")
                        logger.info(f"Price history size: {len(self.price_history)}")
                        logger.info("=" * 80)
                        
                        # Make trading decision
                        self._start_decision_worker(mid_price)
        
        except Exception as e:
            logger.error(f"Error processing quote tick: {e}")
            import traceback
            traceback.print_exc()

    def _maker_quote_sync(self, bid_price: float, ask_price: float) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                self._quote_maker_orders(
                    Decimal(str(bid_price)),
                    Decimal(str(ask_price)),
                )
            )
        finally:
            loop.close()
	                                            
    def _make_trading_decision_sync(self, current_price):
        """Synchronous wrapper for trading decision (called from executor)."""
        # Convert float back to Decimal for processing
        from decimal import Decimal
        price_decimal = Decimal(str(current_price))
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._make_trading_decision(price_decimal))
        finally:
            loop.close()
            
    async def _make_trading_decision(self, current_price):
        """Make trading decision using our 7-phase system."""
        
        # Check simulation mode
        is_simulation = await self.check_simulation_mode()
        mode_text = "SIMULATION" if is_simulation else "LIVE TRADING"
        logger.info(f"Mode: {mode_text}")
        self._db_strategy_event(
            "DECISION_TICK",
            {
                "mode": mode_text,
                "price_history_len": len(self.price_history),
                "current_price": float(current_price),
            },
        )
        
        # Need price history
        if len(self.price_history) < 20:
            logger.warning(f"Not enough price history yet ({len(self.price_history)}/20)")
            return
        
        logger.info(f"Current price: ${float(current_price):,.4f}")
        
        # Create test metadata with sentiment and spot price for better signals
        # FIX: Convert everything to float consistently
        current_price_float = float(current_price)
        metadata = {
            "sentiment_score": random.uniform(10, 90),  # Random sentiment for testing
            "spot_price": current_price_float * random.uniform(0.95, 1.05),  # Random divergence as float
        }
        
        # Phase 4: Process signals
        signals = self._process_signals(current_price, metadata)
        
        if not signals:
            logger.info("No signals generated")
            return
        
        logger.info(f"Generated {len(signals)} signals:")
        for sig in signals:
            logger.info(f"  [{sig.source}] {sig.direction.value}: score={sig.score:.1f}")
        
        # Phase 4: Fuse signals
        fused = self.fusion_engine.fuse_signals(signals, min_signals=1, min_score=60.0)
        
        if not fused:
            logger.info("No actionable fused signal")
            return
        
        logger.info(f"FUSED SIGNAL: {fused.direction.value} (score={fused.score:.1f}, confidence={fused.confidence:.2%})")
        
        # Phase 5: Calculate position size (with $1 cap)
        position_size = self.risk_engine.calculate_position_size(
            signal_confidence=fused.confidence,
            signal_score=fused.score,
            current_price=current_price,  # Pass Decimal to risk engine
        )
        
        logger.info(f"Calculated position size: ${float(position_size):.2f}")
        
        # Phase 5: Validate with risk engine
        direction = "long" if "BULLISH" in str(fused.direction) else "short"
        is_valid, error = self.risk_engine.validate_new_position(
            size=position_size,
            direction=direction,
            current_price=current_price,
        )
        
        if not is_valid:
            logger.warning(f"Position rejected by risk engine: {error}")
            return
        
        # Execute trade (simulation or live based on Redis)
        if is_simulation:
            await self._record_paper_trade(fused, position_size, current_price, direction)
        else:
            await self._place_real_order(fused, position_size, current_price, direction)
            
    async def _record_paper_trade(self, signal, position_size, current_price, direction):
        """Record a paper trade for simulation tracking."""
        
        # Simulate exit after 1 minute (for test mode) or 15 minutes (for normal mode)
        if hasattr(self, 'test_mode') and self.test_mode:
            exit_delta = timedelta(minutes=1)
        else:
            exit_delta = timedelta(minutes=15)
        
        exit_time = datetime.now(timezone.utc) + exit_delta
        
        # Simulate price movement based on signal direction
        if "BULLISH" in str(signal.direction):
            movement = random.uniform(-0.02, 0.08)  # -2% to +8%
        else:
            movement = random.uniform(-0.08, 0.02)  # -8% to +2%
        
        exit_price = current_price * (Decimal("1.0") + Decimal(str(movement)))
        exit_price = max(Decimal("0.01"), min(Decimal("0.99"), exit_price))
        
        # Calculate P&L
        if direction == "long":
            pnl = position_size * (exit_price - current_price) / current_price
        else:
            pnl = position_size * (current_price - exit_price) / current_price
        
        # Determine outcome
        outcome = "WIN" if pnl > 0 else "LOSS"
        
        # Create paper trade record with outcome
        paper_trade = PaperTrade(
            timestamp=datetime.now(timezone.utc),
            direction=direction.upper(),
            size_usd=float(position_size),
            price=float(current_price),
            signal_score=signal.score,
            signal_confidence=signal.confidence,
            outcome=outcome,  # ← NOW SETTING OUTCOME!
        )
        
        self.paper_trades.append(paper_trade)
        
        # Record in performance tracker
        self.performance_tracker.record_trade(
            trade_id=f"paper_{int(datetime.now().timestamp())}",
            direction=direction,
            entry_price=current_price,
            exit_price=exit_price,
            size=position_size,
            entry_time=datetime.now(timezone.utc),
            exit_time=exit_time,
            signal_score=signal.score,
            signal_confidence=signal.confidence,
            metadata={
                "simulated": True,
                "num_signals": signal.num_signals if hasattr(signal, 'num_signals') else 1,
                "fusion_score": signal.score,
            }
        )
        
        # Update metrics in grafana exporter
        if hasattr(self, 'grafana_exporter') and self.grafana_exporter:
            self.grafana_exporter.increment_trade_counter(won=(pnl > 0))
            self.grafana_exporter.record_trade_duration(exit_delta.total_seconds())
        
        logger.info("=" * 80)
        logger.info("[SIMULATION] PAPER TRADE RECORDED")
        logger.info(f"  Direction: {direction.upper()}")
        logger.info(f"  Size: ${float(position_size):.2f}")
        logger.info(f"  Entry Price: ${float(current_price):,.4f}")
        logger.info(f"  Simulated Exit: ${float(exit_price):,.4f}")
        logger.info(f"  Simulated P&L: ${float(pnl):+.2f} ({movement*100:+.2f}%)")
        logger.info(f"  Outcome: {outcome}")
        logger.info(f"  Signal Score: {signal.score:.1f}")
        logger.info(f"  Signal Confidence: {signal.confidence:.2%}")
        logger.info(f"  Total Paper Trades: {len(self.paper_trades)}")
        logger.info("=" * 80)
        
        self._save_paper_trades()
            
    def _save_paper_trades(self):
        """Save paper trades to JSON file."""
        import json
        try:
            trades_data = [t.to_dict() for t in self.paper_trades]
            with open('paper_trades.json', 'w') as f:
                json.dump(trades_data, f, indent=2)
            logger.info(f"Saved {len(trades_data)} paper trades to paper_trades.json")
        except Exception as e:
            logger.error(f"Failed to save paper trades: {e}")
    
    async def _place_real_order(self, signal, position_size, current_price, direction):
        """Place REAL order using Nautilus."""
        if not self.instrument_id:
            logger.error("No instrument available")
            return
        
        try:
            # Get instrument
            instrument = self.cache.instrument(self.instrument_id)
            if not instrument:
                logger.error("Instrument not in cache")
                return
            
            logger.info("=" * 80)
            logger.info("LIVE MODE - PLACING REAL ORDER!")
            logger.info("=" * 80)
            
            # Determine side
            side = OrderSide.BUY if direction == "long" else OrderSide.SELL
            
            # Calculate token quantity
            trade_price = float(current_price)
            max_usd_amount = float(position_size)
            
            if trade_price > 0:
                token_qty = max_usd_amount / trade_price
            else:
                token_qty = max_usd_amount * 2
            
            # Round to appropriate precision
            precision = instrument.size_precision
            token_qty = round(token_qty, precision)
            
            # Ensure minimum quantity
            min_qty = 10 ** (-precision)
            if token_qty < min_qty:
                token_qty = min_qty
            
            qty = Quantity(token_qty, precision=precision)
            
            # Create unique order ID
            timestamp_ms = int(time.time() * 1000)
            unique_id = f"BTC-15MIN-${max_usd_amount:.0f}-{timestamp_ms}"
            
            # Create market order
            order = self.order_factory.market(
                instrument_id=self.instrument_id,
                order_side=side,
                quantity=qty,
                client_order_id=ClientOrderId(unique_id),
                quote_quantity=False,
                time_in_force=TimeInForce.IOC,
            )
            
            # Submit order
            self.submit_order(order)
            
            logger.info(f"REAL ORDER SUBMITTED!")
            logger.info(f"  Order ID: {unique_id}")
            logger.info(f"  Side: {side.name}")
            logger.info(f"  Token Quantity: {token_qty:.6f}")
            logger.info(f"  Estimated Cost: ~${max_usd_amount:.2f}")
            logger.info(f"  Price: ${trade_price:.4f}")
            logger.info("=" * 80)
            
            # Track order in performance tracker
            self._increment_order_metric("placed")
            
        except Exception as e:
            logger.error(f"Error placing real order: {e}")
            import traceback
            traceback.print_exc()
            self._increment_order_metric("rejected")
    
    def _process_signals(self, current_price, metadata=None):
        """Process all signal processors."""
        signals = []
        
        if metadata is None:
            metadata = {}
        
        # Convert metadata values to Decimal where needed by processors
        processed_metadata = {}
        for key, value in metadata.items():
            if isinstance(value, float):
                # Convert float to Decimal for processors that expect Decimal
                processed_metadata[key] = Decimal(str(value))
            else:
                processed_metadata[key] = value
        
        # Spike detection
        spike_signal = self.spike_detector.process(
            current_price=current_price,
            historical_prices=self.price_history,
            metadata=processed_metadata,
        )
        if spike_signal:
            signals.append(spike_signal)
        
        # Sentiment processor (if we have sentiment data)
        if 'sentiment_score' in processed_metadata:
            sentiment_signal = self.sentiment_processor.process(
                current_price=current_price,
                historical_prices=self.price_history,
                metadata=processed_metadata,
            )
            if sentiment_signal:
                signals.append(sentiment_signal)
        
        # Divergence processor (if we have spot price)
        if 'spot_price' in processed_metadata:
            divergence_signal = self.divergence_processor.process(
                current_price=current_price,
                historical_prices=self.price_history,
                metadata=processed_metadata,
            )
            if divergence_signal:
                signals.append(divergence_signal)
        
        return signals

    def on_order_filled(self, event):
        """Handle when a REAL order is filled."""
        logger.info("=" * 80)
        logger.info(f"ORDER FILLED!")
        logger.info(f"  Order: {event.client_order_id}")
        logger.info(f"  Fill Price: ${float(event.last_px):.4f}")
        logger.info(f"  Quantity: {float(event.last_qty):.6f}")
        logger.info("=" * 80)

        filled_id = str(event.client_order_id)
        filled_side: Optional[str] = None
        filled_econ = None
        filled_inst: Any = None
        maker_matched = False
        for order_key, state in list(self.active_maker_orders.items()):
            side = str(state.get("side", "") or "")
            order = state.get("order")
            if order and str(order.client_order_id) == filled_id:
                maker_matched = True
                filled_side = side
                filled_econ = state.get("econ")
                filled_inst = state.get("instrument_id")
                fill_qty = Decimal(str(float(getattr(event, "last_qty", 0.0) or 0.0)))
                if fill_qty <= 0:
                    fill_qty = Decimal(str(state.get("quantity", "0")))
                total_qty = Decimal(str(state.get("quantity", "0")))
                accumulated = Decimal(str(state.get("filled_qty", "0"))) + fill_qty
                if accumulated > total_qty and total_qty > 0:
                    fill_qty = max(Decimal("0"), total_qty - Decimal(str(state.get("filled_qty", "0"))))
                    accumulated = total_qty
                if side == "buy":
                    self.inventory_delta_shares += fill_qty
                else:
                    self.inventory_delta_shares -= fill_qty
                state["filled_qty"] = accumulated
                if total_qty <= 0 or accumulated >= total_qty:
                    self.active_maker_orders.pop(order_key, None)
                break
        if filled_inst is None:
            filled_inst = getattr(event, "instrument_id", None) or self.instrument_id

        fill_price_dec = Decimal(str(float(getattr(event, "last_px", 0.0) or 0.0)))
        fill_qty_dec = Decimal(str(float(getattr(event, "last_qty", 0.0) or 0.0)))
        fill_commission_dec = Decimal(str(float(getattr(event, "commission", 0.0) or 0.0)))
        side_for_ledger = filled_side or self._normalize_side_text(getattr(event, "order_side", ""))
        # Non-maker fills (e.g. taker-exit IOC market sells) are not in active_maker_orders.
        # Keep inventory_delta_shares in sync for those fills as well.
        if not maker_matched and fill_qty_dec > 0:
            side_norm = self._normalize_side_text(getattr(event, "order_side", ""))
            if side_norm == "buy":
                self.inventory_delta_shares += fill_qty_dec
            elif side_norm == "sell":
                self.inventory_delta_shares -= fill_qty_dec
        if side_for_ledger:
            self._update_live_inventory_cost_from_fill(
                instrument_id=filled_inst,
                side=side_for_ledger,
                fill_price=fill_price_dec,
                fill_qty=fill_qty_dec,
                commission=fill_commission_dec,
            )
        self._clear_pending_taker_exit_for_order(filled_id)

        self.consecutive_denied_orders = 0
        self.last_quote_update_ts = 0.0

        # Learn effective fee bps from real fills to keep economics aligned with venue reality.
        try:
            notional = Decimal(str(float(event.last_qty))) * Decimal(str(float(event.last_px)))
            commission = Decimal(str(float(event.commission)))
            if notional > 0 and commission >= 0:
                observed_bps = int(round(float((commission / notional) * Decimal("10000"))))
                if observed_bps > 0:
                    self.last_observed_fee_rate_bps = observed_bps
                    logger.info(
                        f"Observed effective fee rate from fill: {observed_bps} bps "
                        f"(commission={float(commission):.6f}, notional={float(notional):.6f})"
                    )
        except Exception as e:
            logger.debug(f"Could not derive observed fee bps from fill: {e}")

        self.rebate_reporter.record_fill(
            econ=filled_econ,
            fill_qty=float(event.last_qty),
            fill_price=float(event.last_px),
        )
        self._db_order_event(
            event_type="ORDER_FILLED",
            client_order_id=str(getattr(event, "client_order_id", "")),
            venue_order_id=str(getattr(event, "venue_order_id", "")) if getattr(event, "venue_order_id", None) else None,
            side=str(getattr(event, "order_side", "")),
            price=float(getattr(event, "last_px", 0.0)),
            qty=float(getattr(event, "last_qty", 0.0)),
            status="FILLED",
            commission_usdc=float(getattr(event, "commission", 0.0)),
            payload={
                "liquidity_side": str(getattr(event, "liquidity_side", "")),
                "inventory_delta_shares": float(self.inventory_delta_shares),
            },
        )
        self.rebate_reporter.flush_daily_report()

        # Immediately replenish missing side after fill when maker mode is active.
        if (
            not self._stopping
            and self.maker_mode
            and not self.maker_kill_switch
            and self.latest_market_bid
            and self.latest_market_ask
        ):
            logger.info(f"Replenishing maker quote after fill on side={filled_side or 'unknown'}")
            self._start_maker_worker(self.latest_market_bid, self.latest_market_ask)
        
        self._increment_order_metric("filled")
        self._update_inventory_metric()

    def on_event(self, event):
        """Handle Nautilus events — catch PositionClosed for PnL tracking."""
        event_type = type(event).__name__
        if event_type == "PositionClosed":
            try:
                realized_pnl = float(getattr(event, 'realized_pnl', 0.0))
                duration_ns = int(getattr(event, 'duration_ns', 0))
                self._push_position_closed_to_prometheus(realized_pnl, duration_ns)
            except Exception as e:
                logger.debug(f"Failed to handle PositionClosed event for metrics: {e}")
        elif event_type == "PositionOpened":
            if getattr(self, '_prom_live_metrics_ok', False):
                try:
                    self._prom_live_open_pos.set(1)
                except Exception:
                    pass
        elif event_type == "PositionChanged":
            pass  # Could track unrealized PnL here

    def on_order_canceled(self, event):
        """Handle cancel acknowledgements to clear pending-cancel state."""
        canceled_id = str(getattr(event, "client_order_id", "") or "")
        self._clear_pending_taker_exit_for_order(canceled_id)
        for order_key, state in list(self.active_maker_orders.items()):
            order = state.get("order")
            if order and str(order.client_order_id) == canceled_id:
                self.active_maker_orders.pop(order_key, None)
                break
        self._db_order_event(
            event_type="ORDER_CANCELED",
            client_order_id=canceled_id,
            venue_order_id=str(getattr(event, "venue_order_id", "")) if getattr(event, "venue_order_id", None) else None,
            side=str(getattr(event, "order_side", "")),
            status="CANCELED",
        )
    
    def on_order_cancel_rejected(self, event):
        """Handle when an order cancellation is rejected by exchange (e.g. already canceled or matched)."""
        rejected_id = str(getattr(event, "client_order_id", "") or "")
        self._clear_pending_taker_exit_for_order(rejected_id)
        reason = str(getattr(event, "reason", "") or "").lower()
        
        logger.warning(f"OrderCancelRejected for {rejected_id}: {reason}")
        
        # If it says it's already canceled or matched, it's safe to clear from our active tracking
        # to prevent stuck states and kill-switch activations.
        if "already canceled or matched" in reason or "order can't be found" in reason:
            for order_key, state in list(self.active_maker_orders.items()):
                order = state.get("order")
                if order and str(order.client_order_id) == rejected_id:
                    logger.info(f"Clearing {rejected_id} from active_maker_orders due to benign CancelReject.")
                    self.active_maker_orders.pop(order_key, None)
                    break
            
            self._db_order_event(
                event_type="ORDER_CANCEL_REJECTED",
                client_order_id=rejected_id,
                venue_order_id=str(getattr(event, "venue_order_id", "")) if getattr(event, "venue_order_id", None) else None,
                side=str(getattr(event, "order_side", "")),
                status="CANCEL_REJECTED_RECONCILED",
                reason=reason,
            )
    
    def on_order_denied(self, event):
        """Handle when an order is denied."""
        self._handle_order_rejection_like_event(event, title="ORDER DENIED")

    def on_order_rejected(self, event):
        """Handle when an order is rejected by exchange."""
        self._handle_order_rejection_like_event(event, title="ORDER REJECTED")

    def _handle_order_rejection_like_event(self, event, title: str = "ORDER REJECTED") -> None:
        """Shared handler for denied/rejected order events."""
        logger.error("=" * 80)
        logger.error(f"{title}!")
        logger.error(f"  Order: {event.client_order_id}")
        logger.error(f"  Reason: {event.reason}")
        logger.error("=" * 80)

        denied_id = str(event.client_order_id)
        self._clear_pending_taker_exit_for_order(denied_id)
        rejected_side = ""
        rejected_inst: Any = None
        for order_key, state in list(self.active_maker_orders.items()):
            order = state.get("order")
            if order and str(order.client_order_id) == denied_id:
                rejected_side = str(state.get("side", "") or "")
                rejected_inst = state.get("instrument_id")
                self.active_maker_orders.pop(order_key, None)
                break
        if not rejected_side:
            rejected_side = self._normalize_side_text(getattr(event, "order_side", ""))
        if rejected_inst is None:
            rejected_inst = getattr(event, "instrument_id", None)
        if not rejected_side and denied_id.startswith("BTC-15M-TAKER-EXIT-"):
            rejected_side = "sell"

        self.consecutive_denied_orders += 1
        reason = str(getattr(event, "reason", "") or "")
        self._db_order_event(
            event_type="ORDER_REJECTED" if "REJECTED" in title else "ORDER_DENIED",
            client_order_id=str(getattr(event, "client_order_id", "")),
            venue_order_id=str(getattr(event, "venue_order_id", "")) if getattr(event, "venue_order_id", None) else None,
            side=str(getattr(event, "order_side", "")),
            status="REJECTED",
            reason=reason,
            payload={"title": title, "consecutive_denied": self.consecutive_denied_orders},
        )
        if "POST_ONLY_NOT_SUPPORTED" in reason:
            if self.maker_use_post_only:
                logger.warning("Exchange rejected post-only orders; disabling post-only and continuing maker mode.")
            self.maker_use_post_only = False
        lowered = reason.lower()
        if ("orderbook" in lowered) and ("does not exist" in lowered):
            pause_sec = max(1, self.maker_error_pause_sec)
            now_ts = time.time()
            self.quote_pause_until_ts = max(self.quote_pause_until_ts, now_ts + pause_sec)
            self.orderbook_unavailable_until_ts = max(self.orderbook_unavailable_until_ts, now_ts + pause_sec)
            inst_id_txt = str(getattr(event, "instrument_id", "") or "")
            self.orderbook_unavailable_token = self._extract_token_id_from_instrument(inst_id_txt)
            self._cancel_active_maker_orders()
            logger.warning(
                f"Orderbook missing rejection detected; pause quoting for {pause_sec}s and reload instrument "
                f"(instrument={inst_id_txt}, token={self.orderbook_unavailable_token})."
            )
            # Infrastructure/data inconsistency; do not count toward deny kill-switch.
            self.consecutive_denied_orders = max(0, self.consecutive_denied_orders - 1)
            self._trigger_quote_watchdog_reload("orderbook_not_exist", now_ts)
            self.rebate_reporter.record_denied()
            self._increment_order_metric("rejected")
            return

        if ("not enough balance" in lowered) or ("allowance" in lowered):
            pause_sec = max(1, self.maker_error_pause_sec)
            now_ts = time.time()
            if rejected_side == "sell":
                inst_key = self._instrument_key(rejected_inst)
                if inst_key:
                    self._sell_reject_pause_until_by_inst[inst_key] = max(
                        float(self._sell_reject_pause_until_by_inst.get(inst_key, 0.0)),
                        now_ts + pause_sec,
                    )
                # Keep BUY quotes alive; block SELL quotes only.
                self._cancel_maker_order_side("sell", reason="sell_balance_reject", instrument_id=rejected_inst)
                # Refresh conditional balance cache immediately to reduce repeated rejects.
                token_id = self._extract_token_id_from_instrument(inst_key) if inst_key else None
                self._get_conditional_balance_for_token(token_id=token_id, force_refresh=True)
                logger.warning(
                    f"SELL balance/allowance rejection detected; block SELL quoting for {pause_sec}s "
                    f"(instrument={inst_key or '-'}) and keep BUY side active."
                )
            else:
                self.quote_pause_until_ts = max(self.quote_pause_until_ts, now_ts + pause_sec)
                self._cancel_active_maker_orders()
                logger.warning(
                    f"Balance/allowance rejection detected; pause quoting for {pause_sec}s. "
                    "Check wallet balance and token allowance."
                )
        self.rebate_reporter.record_denied()
        if self.consecutive_denied_orders >= self.maker_max_consecutive_denied:
            self._activate_maker_kill_switch(
                f"Consecutive denied orders reached {self.consecutive_denied_orders}"
            )
        
        self._increment_order_metric("rejected")
    
    def on_stop(self):
        """Called when strategy stops."""
        self._stopping = True
        self._lifecycle_stop_event.set()
        self._reload_stop_event.set()
        self._quote_watchdog_stop_event.set()
        self._redeem_stop_event.set()
        self._binance_ws_stop_event.set()
        if self._lifecycle_thread and self._lifecycle_thread.is_alive():
            self._lifecycle_thread.join(timeout=2)
        if self._reload_thread and self._reload_thread.is_alive():
            self._reload_thread.join(timeout=2)
        if self._quote_watchdog_thread and self._quote_watchdog_thread.is_alive():
            self._quote_watchdog_thread.join(timeout=2)
        if self._redeem_thread and self._redeem_thread.is_alive():
            self._redeem_thread.join(timeout=2)
        if self._binance_ws_thread and self._binance_ws_thread.is_alive():
            self._binance_ws_thread.join(timeout=2)
        logger.info("Integrated BTC strategy stopped")
        logger.info(f"Total paper trades recorded: {len(self.paper_trades)}")
        self._cancel_active_maker_orders()
        self.rebate_reporter.flush_daily_report()
        self._db_strategy_event(
            "STRATEGY_STOP",
            {
                "paper_trades": len(self.paper_trades),
                "inventory_delta_shares": float(self.inventory_delta_shares),
            },
        )
        if self.trade_db:
            self.trade_db.log_run_stop(
                run_id=self.run_id,
                notes={
                    "paper_trades": len(self.paper_trades),
                    "inventory_delta_shares": float(self.inventory_delta_shares),
                },
            )
        
        if self.grafana_exporter:
            try:
                self.grafana_exporter.stop()
            except:
                pass


def run_integrated_bot(simulation: bool = True, enable_grafana: bool = True, test_mode: bool = False):
    """Run the integrated BTC 15-min trading bot."""
    print("=" * 80)
    print("INTEGRATED POLYMARKET BTC 15-MIN TRADING BOT")
    print("Nautilus + 7-Phase System + Redis Control")
    print("=" * 80)
    
    # Initialize Redis
    redis_client = init_redis()
    
    # Set initial simulation mode in Redis
    if redis_client:
        try:
            redis_client.set('btc_trading:simulation_mode', '1' if simulation else '0')
            logger.info(f"Initial mode set in Redis: {'SIMULATION' if simulation else 'LIVE'}")
        except Exception as e:
            logger.warning(f"Could not set Redis simulation mode: {e}")
    
    print(f"\nConfiguration:")
    print(f"  Initial Mode: {'SIMULATION' if simulation else 'LIVE TRADING'}")
    print(f"  Redis Control: {'Enabled' if redis_client else 'Disabled'}")
    print(f"  Grafana: {'Enabled' if enable_grafana else 'Disabled'}")
    print(f"  Max Trade Size: $1.00")
    print(f"  Instrument Reload: Every 12 minutes")
    print(f"  Price History: Pre-loaded on startup")
    auto_rollover_enabled = os.getenv("AUTO_NODE_ROLLOVER_ENABLED", "1").strip().lower() not in ("0", "false", "no")
    auto_rollover_sec = max(300, int(os.getenv("AUTO_NODE_ROLLOVER_SEC", "1800")))
    auto_rollover_cooldown_sec = max(1, int(os.getenv("AUTO_NODE_ROLLOVER_COOLDOWN_SEC", "3")))
    auto_rollover_max_failures = max(1, int(os.getenv("AUTO_NODE_ROLLOVER_MAX_FAILURES", "5")))
    auto_restart_on_unexpected_exit = os.getenv("AUTO_NODE_RESTART_ON_UNEXPECTED_EXIT", "0").strip().lower() in ("1", "true", "yes", "on")
    print(
        f"  Auto Node Rollover: {'Enabled' if auto_rollover_enabled else 'Disabled'} "
        f"({auto_rollover_sec}s)"
    )
    print(f"  Restart On Unexpected Exit: {'Enabled' if auto_restart_on_unexpected_exit else 'Disabled'}")
    print()

    auth = resolve_polymarket_auth()
    if not auth:
        raise RuntimeError("Cannot resolve Polymarket auth (provide PK or full API credentials).")

    def _build_node_for_cycle(cycle_index: int) -> tuple[TradingNode, str]:
        # Discover exact BTC 15-min slugs, then load a rolling window of nearby markets.
        btc_slugs = resolve_btc_15m_market_slugs()
        if not btc_slugs:
            raise RuntimeError("No BTC 15-min market slugs resolved. Refusing to start.")

        primary_slug, primary_instrument_ids = resolve_best_btc_15m_market(btc_slugs)
        if not primary_slug:
            raise RuntimeError("No primary BTC 15-min slug selected. Refusing to start.")
        if not primary_instrument_ids:
            raise RuntimeError(f"No instrument IDs resolved for slug {primary_slug}. Refusing to start.")

        load_slug_count = max(1, int(os.getenv("BTC_MARKET_LOAD_SLUG_COUNT", "3")))
        ordered_slugs: List[str] = [primary_slug] + [s for s in btc_slugs if s != primary_slug]
        slugs_to_load = ordered_slugs[:load_slug_count]
        seen_ids: Set[str] = set()
        instrument_ids: List[InstrumentId] = []
        for slug in slugs_to_load:
            ids = resolve_primary_btc_15m_instrument_ids(slug)
            if not ids:
                continue
            for inst_id in ids:
                if inst_id.value in seen_ids:
                    continue
                seen_ids.add(inst_id.value)
                instrument_ids.append(inst_id)

        if not instrument_ids:
            instrument_ids = primary_instrument_ids

        now_utc = datetime.now(timezone.utc)
        window_back_minutes = int(os.getenv("BTC_MARKET_END_WINDOW_BACK_MINUTES", "5"))
        window_forward_minutes = int(os.getenv("BTC_MARKET_END_WINDOW_FORWARD_MINUTES", "120"))
        end_date_min = (now_utc - timedelta(minutes=window_back_minutes)).isoformat()
        end_date_max = (now_utc + timedelta(minutes=window_forward_minutes)).isoformat()

        logger.info("=" * 80)
        logger.info(f"Using SAFE slug-based market discovery (cycle={cycle_index})")
        logger.info(f"  Candidate BTC 15-min slugs: {btc_slugs}")
        logger.info(f"  Primary slug: {primary_slug}")
        logger.info(f"  Slugs loaded into provider: {slugs_to_load}")
        logger.info(f"  Instrument IDs: {[inst.value for inst in instrument_ids]}")
        logger.info(
            f"  End-date window: {end_date_min} -> {end_date_max} "
            f"(back={window_back_minutes}m, forward={window_forward_minutes}m)"
        )
        logger.info("=" * 80)

        instrument_cfg = InstrumentProviderConfig(
            load_all=False,
            load_ids=frozenset(instrument_ids),
            filters={
                "active": True,
                "closed": False,
                "archived": False,
                "end_date_min": end_date_min,
                "end_date_max": end_date_max,
                "limit": 25,
            },
            use_gamma_markets=True,
        )

        poly_data_cfg = PolymarketDataClientConfig(
            private_key=auth["private_key"],
            signature_type=int(auth.get("signature_type", "0")),
            funder=auth.get("funder") or None,
            api_key=auth["api_key"],
            api_secret=auth["api_secret"],
            passphrase=auth["passphrase"],
            instrument_provider=instrument_cfg,
            drop_quotes_missing_side=False,
        )

        poly_exec_cfg = PolymarketExecClientConfig(
            private_key=auth["private_key"],
            signature_type=int(auth.get("signature_type", "0")),
            funder=auth.get("funder") or None,
            api_key=auth["api_key"],
            api_secret=auth["api_secret"],
            passphrase=auth["passphrase"],
            instrument_provider=instrument_cfg,
        )

        config = TradingNodeConfig(
            environment="live",
            trader_id="BTC-15MIN-INTEGRATED-001",
            logging=LoggingConfig(
                log_level="INFO",
                log_directory="./logs/nautilus",
            ),
            data_engine=LiveDataEngineConfig(qsize=6000),
            exec_engine=LiveExecEngineConfig(qsize=6000),
            risk_engine=LiveRiskEngineConfig(
                bypass=simulation,
            ),
            data_clients={POLYMARKET: poly_data_cfg},
            exec_clients={POLYMARKET: poly_exec_cfg},
        )

        strategy = IntegratedBTCStrategy(
            redis_client=redis_client,
            enable_grafana=enable_grafana,
            test_mode=test_mode,
            selected_slug=primary_slug,
        )

        print("\nBuilding Nautilus node...")
        print("=" * 80)
        node = TradingNode(config=config)
        node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
        node.add_exec_client_factory(POLYMARKET, PolymarketLiveExecClientFactory)
        node.trader.add_strategy(strategy)
        node.build()
        logger.info("Nautilus node built successfully")
        return node, primary_slug

    cycle_idx = 0
    consecutive_failures = 0
    user_stopped = False

    while True:
        cycle_idx += 1
        cycle_started_at = time.time()
        node: Optional[TradingNode] = None
        rollover_requested = threading.Event()
        rollover_stop = threading.Event()
        rollover_thread: Optional[threading.Thread] = None

        try:
            node, cycle_slug = _build_node_for_cycle(cycle_idx)
            logger.info(f"Cycle {cycle_idx} ready (slug={cycle_slug})")

            if auto_rollover_enabled:
                def _rollover_worker() -> None:
                    if rollover_stop.wait(auto_rollover_sec):
                        return
                    rollover_requested.set()
                    logger.warning(
                        f"Auto node rollover timer reached ({auto_rollover_sec}s). "
                        "Stopping current node for market refresh."
                    )
                    try:
                        if node is not None:
                            node.stop()
                    except Exception as e:
                        logger.error(f"Failed to stop node during auto rollover: {e}")

                rollover_thread = threading.Thread(target=_rollover_worker, daemon=True)
                rollover_thread.start()

            print()
            print("=" * 80)
            print(f"BOT STARTING (cycle {cycle_idx})")
            print("=" * 80)
            node.run()
            consecutive_failures = 0
        except KeyboardInterrupt:
            user_stopped = True
            print("\nShutting down...")
        except Exception as e:
            consecutive_failures += 1
            logger.exception(f"Node cycle {cycle_idx} failed: {e}")
        finally:
            rollover_stop.set()
            if rollover_thread and rollover_thread.is_alive():
                rollover_thread.join(timeout=1)
            if node is not None:
                try:
                    node.dispose()
                except Exception as e:
                    logger.warning(f"Node dispose raised: {e}")
            logger.info(f"Bot cycle {cycle_idx} stopped")

        # Check if the strategy requested rollover due to stale instruments
        try:
            strategies = node.trader.strategies() if node else []
            for strat in strategies:
                if getattr(strat, '_rollover_requested_flag', False):
                    rollover_requested.set()
                    logger.info("Strategy requested rollover (stale instruments)")
                    break
        except Exception:
            pass

        if user_stopped:
            break
        if not auto_rollover_enabled:
            break
        if consecutive_failures >= auto_rollover_max_failures:
            logger.error(
                f"Auto rollover aborted after {consecutive_failures} consecutive failures "
                f"(max={auto_rollover_max_failures})."
            )
            break

        if rollover_requested.is_set():
            logger.info("Starting next cycle after scheduled auto rollover...")
        else:
            run_sec = int(time.time() - cycle_started_at)
            if auto_restart_on_unexpected_exit:
                logger.warning(
                    f"Node cycle exited without rollover request (runtime={run_sec}s). "
                    "Attempting auto rebuild."
                )
            else:
                logger.warning(
                    f"Node cycle exited without rollover request (runtime={run_sec}s). "
                    "Stopping loop to avoid restart churn. "
                    "Set AUTO_NODE_RESTART_ON_UNEXPECTED_EXIT=1 to force auto rebuild."
                )
                break
        time.sleep(auto_rollover_cooldown_sec)


def main():
    """Main entry point."""
    import argparse

    auto_apply_local_patches()
    
    parser = argparse.ArgumentParser(description="Integrated BTC 15-Min Trading Bot")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run in LIVE mode (real money at risk!). Default is simulation."
    )
    parser.add_argument(
        "--no-grafana",
        action="store_true",
        help="Disable Grafana metrics"
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Run in TEST MODE (trade every minute for faster testing)"
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Run safety checks only (no trading node startup)"
    )
    
    args = parser.parse_args()
    
    simulation = not args.live
    enable_grafana = not args.no_grafana
    test_mode = args.test_mode

    if not run_preflight_checks(simulation=simulation):
        print("Preflight check failed. Startup aborted.")
        return

    if args.preflight_only:
        print("Preflight check passed. Exiting without starting bot.")
        return
    
    if not simulation:
        print("WARNING: LIVE TRADING MODE - REAL MONEY AT RISK!")
        confirm = input("Type 'yes' to continue: ")
        if confirm.lower() != 'yes':
            print("Cancelled.")
            return
    
    run_integrated_bot(
        simulation=simulation,
        enable_grafana=enable_grafana,
        test_mode=test_mode
    )


if __name__ == "__main__":
    main()
