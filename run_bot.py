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
from typing import Any, Dict, List, Optional, Set
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
from execution.rebate_model import CRYPTO_FEE_CURVE, bps_to_fee_rate, estimate_quote_economics
from execution.rebate_reporter import RebateReporter
from monitoring.performance_tracker import get_performance_tracker
from monitoring.grafana_exporter import get_grafana_exporter
from monitoring.trade_journal_db import TradeJournalDB
from feedback.learning_engine import get_learning_engine

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
    """
    Fetch one Gamma market record by slug.
    """
    api_base = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")
    timeout = float(os.getenv("GAMMA_DISCOVERY_TIMEOUT_SEC", "8"))
    async with httpx.AsyncClient(timeout=timeout) as client:
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
        if isinstance(data, list) and data:
            return data[0]
        # Fallback: some responses are better populated via event-slug endpoint.
        event_resp = await client.get(f"{api_base}/events/slug/{slug}")
        if event_resp.status_code == 200:
            payload = event_resp.json()
            if isinstance(payload, dict):
                markets = payload.get("markets") or []
                if isinstance(markets, list) and markets:
                    return markets[0]
            elif isinstance(payload, list):
                for event in payload:
                    if not isinstance(event, dict):
                        continue
                    markets = event.get("markets") or []
                    if isinstance(markets, list) and markets:
                        return markets[0]
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


@dataclass
class PaperTrade:
    """Track paper/simulation trades"""
    timestamp: datetime
    direction: str
    size_usd: float
    price: float
    signal_score: float
    signal_confidence: float
    outcome: str = "PENDING"
    
    def to_dict(self):
        return {
            'timestamp': self.timestamp.isoformat(),
            'direction': self.direction,
            'size_usd': self.size_usd,
            'price': self.price,
            'signal_score': self.signal_score,
            'signal_confidence': self.signal_confidence,
            'outcome': self.outcome,
        }


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
        self.max_real_history = int(os.getenv("MAKER_VOL_REAL_HISTORY_MAX", "300"))
        
        # Paper trading tracker
        self.paper_trades: List[PaperTrade] = []
        
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
        self.maker_quote_sides = os.getenv("MAKER_QUOTE_SIDES", "both").strip().lower()
        if self.maker_quote_sides not in {"both", "buy", "sell"}:
            self.maker_quote_sides = "both"
        self.maker_min_expected_net_usdc = Decimal(os.getenv("MAKER_MIN_EXPECTED_NET_USDC", "0.0001"))
        self.maker_adverse_selection_buffer = Decimal(os.getenv("MAKER_ADVERSE_SELECTION_BUFFER", "0.0005"))
        self.maker_use_post_only = os.getenv("MAKER_POST_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")
        self.maker_post_only_strict = os.getenv("MAKER_POST_ONLY_STRICT", "1").strip().lower() not in ("0", "false", "no")
        self.maker_max_inventory_shares = Decimal(os.getenv("MAKER_MAX_INVENTORY_SHARES", "25"))
        self.maker_inventory_skew_max = Decimal(os.getenv("MAKER_INVENTORY_SKEW_MAX", "0.03"))
        self.maker_volatility_pause_threshold = Decimal(os.getenv("MAKER_VOL_PAUSE_THRESHOLD", "0.03"))
        self.maker_volatility_pause_sec = int(os.getenv("MAKER_VOL_PAUSE_SEC", "30"))
        self.maker_vol_warmup_quotes = int(os.getenv("MAKER_VOL_WARMUP_QUOTES", "30"))
        self.maker_vol_return_clip = Decimal(os.getenv("MAKER_VOL_RETURN_CLIP", "0.20"))
        self.maker_vol_rolling_window = int(os.getenv("MAKER_VOL_ROLLING_WINDOW", "30"))
        self.maker_vol_ewma_alpha = float(os.getenv("MAKER_VOL_EWMA_ALPHA", "0.35"))
        self.maker_max_consecutive_denied = int(os.getenv("MAKER_MAX_CONSECUTIVE_DENIED", "5"))
        self.maker_order_ttl_sec = int(os.getenv("MAKER_ORDER_TTL_SEC", "20"))
        self.maker_requote_threshold = Decimal(os.getenv("MAKER_REQUOTE_THRESHOLD", "0.002"))
        self.maker_balance_pause_sec = int(os.getenv("MAKER_BALANCE_PAUSE_SEC", "60"))
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
        self.maker_cancel_cooldown_sec = int(os.getenv("MAKER_CANCEL_COOLDOWN_SEC", "2"))
        self.maker_cancel_ack_timeout_sec = int(os.getenv("MAKER_CANCEL_ACK_TIMEOUT_SEC", "8"))
        self.maker_cancel_max_retries = int(os.getenv("MAKER_CANCEL_MAX_RETRIES", "3"))
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
        self.last_external_spot: Optional[Decimal] = None
        self.latest_external_spot: Optional[Decimal] = None
        self.latest_market_bid: Optional[Decimal] = None
        self.latest_market_ask: Optional[Decimal] = None
        self.inventory_delta_shares = Decimal("0")
        self.consecutive_denied_orders = 0
        self.maker_kill_switch = False
        self.active_maker_orders: Dict[str, Any] = {}
        self.sim_maker_positions: List[Dict[str, Any]] = []
        self.sim_maker_closed_total = 0
        self.sim_maker_closed_wins = 0
        self.current_token_id: Optional[str] = None
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
        logger.info("  Phase 7: Learning engine ready")
        logger.info("  $1 per trade maximum")
        logger.info("  Reloads instruments every 12 minutes")
        logger.info(f"  Maker mode: {'ON' if self.maker_mode else 'OFF'}")
        logger.info(f"  Maker quote sides: {self.maker_quote_sides.upper()}")
        logger.info(f"  Maker post-only flag: {'ON' if self.maker_use_post_only else 'OFF'}")
        logger.info(f"  Maker post-only strict: {'ON' if self.maker_post_only_strict else 'OFF'}")
        logger.info(f"  Maker auto-tune: {'ON' if self.auto_tune_enabled else 'OFF'}")
        logger.info(f"  Maker max order USDC: ${float(self.maker_max_order_usdc):.2f}")
        logger.info(f"  Maker cancel max retries: {self.maker_cancel_max_retries}")
        logger.info(f"  Maker simulation shadow: {'ON' if self.maker_simulation_shadow else 'OFF'}")
        logger.info(f"  Maker fee default decimal: {float(self.maker_fee_rate_default_decimal):.6f}")
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

    async def _fetch_external_spot_price(self) -> Optional[Decimal]:
        """
        Fetch BTC spot from Coinbase + Binance and average valid results.
        """
        timeout = float(os.getenv("EXTERNAL_SPOT_TIMEOUT_SEC", "2.5"))
        urls = (
            ("coinbase", "https://api.exchange.coinbase.com/products/BTC-USD/ticker"),
            ("binance", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
        )
        prices: List[Decimal] = []
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                responses = await asyncio.gather(
                    *(client.get(url) for _, url in urls),
                    return_exceptions=True,
                )
            for (source, _), resp in zip(urls, responses):
                if isinstance(resp, Exception):
                    logger.debug(f"{source} spot fetch failed: {resp}")
                    continue
                try:
                    resp.raise_for_status()
                    data = resp.json()
                    raw = data.get("price")
                    if raw is not None:
                        prices.append(Decimal(str(raw)))
                except Exception as e:
                    logger.debug(f"{source} spot parse failed: {e}")
        except Exception as e:
            logger.debug(f"External spot fetch error: {e}")

        if not prices:
            return None
        return sum(prices) / Decimal(len(prices))

    async def _compute_fair_probability(self, market_mid: Decimal) -> Decimal:
        """
        Build fair probability from market mid plus external BTC momentum adjustment.
        """
        fair = market_mid
        external = await self._fetch_external_spot_price()
        if external:
            self.latest_external_spot = external
            if self.last_external_spot and self.last_external_spot > 0:
                drift = (external - self.last_external_spot) / self.last_external_spot
                shift = Decimal(str(max(-0.05, min(0.05, float(drift) * 8.0))))
                fair = market_mid + shift
            self.last_external_spot = external
        return max(Decimal("0.01"), min(Decimal("0.99"), fair))

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

    async def _get_dynamic_fee_rate(self) -> Optional[Decimal]:
        """
        Fetch dynamic fee rate from CLOB /fee-rate endpoint using current token_id.
        """
        if not self.current_token_id:
            return None
        fee_rate = await self.fee_rate_client.get_fee_rate_decimal(self.current_token_id)
        source = "clob_fee_rate"
        if fee_rate is None or fee_rate <= 0:
            fee_rate = self._infer_market_fee_rate_default()
            source = "market_type_default"
            if (fee_rate is None or fee_rate <= 0) and self.maker_fee_rate_legacy_bps_default > 0:
                fee_rate = bps_to_fee_rate(self.maker_fee_rate_legacy_bps_default)
                source = "legacy_bps_default"
        if fee_rate is None or fee_rate <= 0:
            return None
        fee_bps_value = int((fee_rate * Decimal("10000")).quantize(Decimal("1")))
        logger.debug(
            f"Using fee rate source={source} bps={fee_bps_value} "
            f"decimal={float(fee_rate):.6f} token={self.current_token_id}"
        )
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

    def _activate_maker_kill_switch(self, reason: str) -> None:
        self.maker_kill_switch = True
        self._cancel_active_maker_orders()
        logger.error(f"MAKER KILL SWITCH ACTIVATED: {reason}")

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

    def _apply_inventory_skew(self, fair: Decimal) -> Decimal:
        if self.maker_max_inventory_shares <= 0:
            return fair
        ratio = self.inventory_delta_shares / self.maker_max_inventory_shares
        ratio = max(Decimal("-1"), min(Decimal("1"), ratio))
        # Long inventory => lower fair to encourage selling and reduce bid aggressiveness.
        skew = ratio * self.maker_inventory_skew_max
        return max(Decimal("0.01"), min(Decimal("0.99"), fair - skew))

    def _cancel_active_maker_orders(self) -> None:
        for side in list(self.active_maker_orders.keys()):
            self._cancel_maker_order_side(side, reason="risk")

    def _cancel_maker_order_side(self, side: str, reason: str = "risk") -> None:
        state = self.active_maker_orders.get(side)
        if not state:
            return
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
            self.active_maker_orders.pop(side, None)
            return
        try:
            status_text = str(getattr(order, "status", "")).upper()
            if any(flag in status_text for flag in ("REJECTED", "FILLED", "CANCELED", "CANCELLED")):
                logger.debug(f"Skip cancel [{side}] because order state is terminal: {status_text}")
                self.active_maker_orders.pop(side, None)
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

    def _is_order_ttl_expired(self, side: str, now_ts: float) -> bool:
        state = self.active_maker_orders.get(side)
        if not state:
            return False
        created_ts = float(state.get("created_ts", 0.0))
        if created_ts <= 0:
            return True
        return (now_ts - created_ts) >= self.maker_order_ttl_sec

    def _cleanup_stale_pending_cancels(self, now_ts: float) -> None:
        for side, state in list(self.active_maker_orders.items()):
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
                    self.active_maker_orders.pop(side, None)
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
                    reason=f"open_or_unknown_after_retries={retries}",
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

            if len(open_orders) == 0:
                return False

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

    def _simulate_shadow_maker_fills_and_closes(self, bid_price: Decimal, ask_price: Decimal, now_ts: float) -> None:
        """
        In simulation mode, emulate maker order lifecycle:
        - Submit -> ack latency -> resting.
        - Cancel request -> cancel latency (can still fill before effective cancel).
        - Queue-priority + edge + age based probabilistic partial fills.
        - Evaluate PnL after hold period, including entry/exit fees.
        """
        mid_price = (bid_price + ask_price) / 2

        # Advance simulated order states.
        for side, state in list(self.active_maker_orders.items()):
            if not state.get("simulated"):
                continue
            coid = str(state.get("client_order_id", f"SIM-{side}-{int(now_ts*1000)}"))

            # Ack transition.
            if state.get("status", "PENDING_ACK") == "PENDING_ACK":
                ack_at = float(state.get("ack_at", 0.0))
                if now_ts >= ack_at:
                    state["status"] = "RESTING"
                    self._db_order_event(
                        event_type="ORDER_SIM_ACCEPTED",
                        client_order_id=coid,
                        side=side.upper(),
                        price=float(state.get("price", 0.0)),
                        qty=float(state.get("quantity", 0.0)),
                        status="ACCEPTED",
                    )

            # Cancel transition.
            if state.get("pending_cancel"):
                cancel_effective_at = float(state.get("cancel_effective_at", 0.0))
                if now_ts >= cancel_effective_at:
                    self.active_maker_orders.pop(side, None)
                    self.rebate_reporter.record_cancel(str(state.get("cancel_reason", "cancel")))
                    self._db_order_event(
                        event_type="ORDER_SIM_CANCELED",
                        client_order_id=coid,
                        side=side.upper(),
                        price=float(state.get("price", 0.0)),
                        qty=float(state.get("quantity", 0.0)),
                        status="CANCELED",
                        reason=str(state.get("cancel_reason", "cancel")),
                    )
                    continue

            if state.get("status") != "RESTING":
                continue

            limit_price = Decimal(str(state.get("price", "0")))
            qty = Decimal(str(state.get("quantity", "0")))
            if qty <= 0:
                continue
            filled_qty = Decimal(str(state.get("filled_qty", "0")))
            remaining_qty = qty - filled_qty
            if remaining_qty <= 0:
                continue

            instrument = self.cache.instrument(self.instrument_id) if self.instrument_id else None
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

            # Fill trigger:
            # 1) hard cross (always eligible),
            # 2) resting at/near top-of-book (maker can be hit without spread crossing).
            crossed = False
            near_top = False
            if side == "buy":
                crossed = ask_price <= limit_price
                near_top = limit_price >= (bid_price - tick)
            else:
                crossed = bid_price >= limit_price
                near_top = limit_price <= (ask_price + tick)

            if not crossed and not near_top:
                continue

            created_ts = float(state.get("created_ts", now_ts))
            age_sec = max(0.0, now_ts - created_ts)
            queue_rank = float(state.get("queue_rank", 0.5))  # 0=front, 1=back
            edge = float((limit_price - ask_price) if side == "buy" else (bid_price - limit_price))
            edge = max(0.0, edge)

            # If near top but not crossed, reward top-of-book proximity.
            if side == "buy":
                dist_to_best = max(Decimal("0"), bid_price - limit_price)
            else:
                dist_to_best = max(Decimal("0"), limit_price - ask_price)
            proximity_ratio = 0.0
            if tick > 0:
                proximity_ratio = max(0.0, 1.0 - float(dist_to_best / tick))
                proximity_ratio = min(1.0, proximity_ratio)

            age_bonus = min(
                self.sim_fill_age_bonus_max,
                (age_sec / float(self.sim_fill_age_to_max_sec)) * self.sim_fill_age_bonus_max,
            )
            crossed_bonus = 0.45 if crossed else 0.0
            top_book_bonus = 0.20 * proximity_ratio if near_top else 0.0
            fill_prob = (
                self.sim_fill_base_prob
                + min(self.sim_fill_edge_boost, edge * 30.0)
                + crossed_bonus
                + top_book_bonus
                + age_bonus
                - (self.sim_fill_queue_penalty * queue_rank)
            )
            fill_prob = max(0.0, min(0.98, fill_prob))
            if random.random() >= fill_prob:
                continue

            fill_ratio_low = max(0.01, min(self.sim_partial_fill_min_ratio, self.sim_partial_fill_max_ratio))
            fill_ratio_high = max(fill_ratio_low, self.sim_partial_fill_max_ratio)
            fill_ratio = random.uniform(fill_ratio_low, fill_ratio_high)
            this_fill_qty = remaining_qty * Decimal(str(fill_ratio))
            min_lot = Decimal("0.000001")
            if this_fill_qty < min_lot:
                this_fill_qty = min_lot
            if this_fill_qty > remaining_qty:
                this_fill_qty = remaining_qty

            fee_rate_bps = int(state.get("fee_rate_bps", self.maker_fee_rate_bps_default))
            fee_rate_dec = Decimal(str(max(0, fee_rate_bps))) / Decimal("10000")
            entry_commission = (this_fill_qty * limit_price) * fee_rate_dec

            state["filled_qty"] = filled_qty + this_fill_qty
            state["entry_commission_usdc"] = Decimal(str(state.get("entry_commission_usdc", "0"))) + entry_commission
            state["last_fill_ts"] = now_ts

            status = "PARTIALLY_FILLED" if state["filled_qty"] < qty else "FILLED"
            self._db_order_event(
                event_type="ORDER_SIM_FILLED",
                client_order_id=coid,
                side=side.upper(),
                price=float(limit_price),
                qty=float(this_fill_qty),
                status=status,
                commission_usdc=float(entry_commission),
                payload={
                    "fill_prob": fill_prob,
                    "queue_rank": queue_rank,
                    "age_sec": age_sec,
                    "edge": edge,
                    "crossed": crossed,
                    "near_top": near_top,
                    "proximity_ratio": proximity_ratio,
                    "filled_qty_total": float(state["filled_qty"]),
                    "qty_total": float(qty),
                    "fee_rate_bps": fee_rate_bps,
                },
            )

            if state["filled_qty"] < qty:
                continue

            self.active_maker_orders.pop(side, None)
            final_qty = state["filled_qty"]
            if side == "buy":
                self.inventory_delta_shares += final_qty
            else:
                self.inventory_delta_shares -= final_qty

            self.sim_maker_positions.append(
                {
                    "client_order_id": coid,
                    "side": side,
                    "qty": final_qty,
                    "entry_price": limit_price,
                    "entry_commission_usdc": Decimal(str(state.get("entry_commission_usdc", "0"))),
                    "fee_rate_bps": fee_rate_bps,
                    "opened_ts": now_ts,
                }
            )

        # Close simulated fills after hold horizon and compute win-rate.
        remaining_positions: List[Dict[str, Any]] = []
        for pos in self.sim_maker_positions:
            opened_ts = float(pos.get("opened_ts", 0.0))
            if now_ts - opened_ts < self.maker_sim_eval_sec:
                remaining_positions.append(pos)
                continue

            side = str(pos.get("side"))
            qty = Decimal(str(pos.get("qty", "0")))
            entry = Decimal(str(pos.get("entry_price", "0")))
            entry_commission = Decimal(str(pos.get("entry_commission_usdc", "0")))
            fee_rate_bps = int(pos.get("fee_rate_bps", self.maker_fee_rate_bps_default))
            fee_rate_dec = Decimal(str(max(0, fee_rate_bps))) / Decimal("10000")
            if qty <= 0 or entry <= 0:
                continue

            if side == "buy":
                gross_pnl = (mid_price - entry) * qty
                self.inventory_delta_shares -= qty
                direction = "long"
            else:
                gross_pnl = (entry - mid_price) * qty
                self.inventory_delta_shares += qty
                direction = "short"
            exit_commission = (mid_price * qty) * fee_rate_dec
            pnl = gross_pnl - entry_commission - exit_commission

            self.sim_maker_closed_total += 1
            if pnl > 0:
                self.sim_maker_closed_wins += 1
            win_rate = (
                self.sim_maker_closed_wins / self.sim_maker_closed_total
                if self.sim_maker_closed_total > 0
                else 0.0
            )

            # Persist into existing performance tracker as synthetic maker trade.
            try:
                self.performance_tracker.record_trade(
                    trade_id=f"sim_maker_{pos.get('client_order_id')}",
                    direction=direction,
                    entry_price=entry,
                    exit_price=mid_price,
                    size=entry * qty,
                    entry_time=datetime.fromtimestamp(opened_ts, tz=timezone.utc),
                    exit_time=datetime.now(timezone.utc),
                    signal_score=0.0,
                    signal_confidence=0.0,
                    metadata={"sim_maker": True},
                )
            except Exception:
                pass

            self._db_order_event(
                event_type="ORDER_SIM_CLOSED",
                client_order_id=str(pos.get("client_order_id")),
                side=side.upper(),
                price=float(mid_price),
                qty=float(qty),
                status="CLOSED",
                commission_usdc=float(entry_commission + exit_commission),
                payload={
                    "entry_price": float(entry),
                    "exit_price": float(mid_price),
                    "gross_pnl_usdc": float(gross_pnl),
                    "pnl_usdc": float(pnl),
                    "entry_commission_usdc": float(entry_commission),
                    "exit_commission_usdc": float(exit_commission),
                    "fee_rate_bps": fee_rate_bps,
                    "win_rate": win_rate,
                },
            )
            logger.info(
                f"[SIM-MAKER] Closed {side.upper()} qty={float(qty):.6f} "
                f"entry={float(entry):.4f} exit={float(mid_price):.4f} "
                f"pnl={float(pnl):+.4f} win_rate={win_rate:.2%}"
            )

        self.sim_maker_positions = remaining_positions

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
        self.last_auto_tune_ts = now_ts

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

        now_ts = time.time()
        if now_ts - self.last_quote_update_ts < self.quote_refresh_sec:
            return
        self.last_quote_update_ts = now_ts
        self._maybe_auto_tune(now_ts)
        self._cleanup_stale_pending_cancels(now_ts)
        if is_simulation and self.maker_simulation_shadow:
            self._simulate_shadow_maker_fills_and_closes(bid_price, ask_price, now_ts)

        # Cancel stale quotes by TTL before computing new target quotes.
        for side in ("buy", "sell"):
            if self._is_order_ttl_expired(side, now_ts):
                logger.info(f"Maker order [{side}] exceeded TTL={self.maker_order_ttl_sec}s, cancel and requote.")
                self._cancel_maker_order_side(side, reason="ttl")

        if abs(self.inventory_delta_shares) >= self.maker_max_inventory_shares:
            self._activate_maker_kill_switch(
                f"Inventory {self.inventory_delta_shares} exceeds max {self.maker_max_inventory_shares}"
            )
            return

        recent_vol = self._compute_recent_volatility()
        if recent_vol is None:
            logger.debug(
                f"Volatility gate warmup: real_quotes={len(self.real_price_history)}/{self.maker_vol_warmup_quotes}"
            )
        elif recent_vol > self.maker_volatility_pause_threshold:
            self.quote_pause_until_ts = time.time() + self.maker_volatility_pause_sec
            self._cancel_active_maker_orders()
            logger.warning(
                f"Volatility pause triggered: recent_vol={float(recent_vol):.4f} "
                f"threshold={float(self.maker_volatility_pause_threshold):.4f} "
                f"pause_sec={self.maker_volatility_pause_sec}"
            )
            return

        fair = await self._compute_fair_probability((bid_price + ask_price) / 2)
        fair = self._apply_inventory_skew(fair)
        quote_bid = max(Decimal("0.01"), fair - self.maker_half_spread)
        quote_ask = min(Decimal("0.99"), fair + self.maker_half_spread)

        # Keep quotes passive relative to current top of book to reduce taker fills.
        quote_bid = min(quote_bid, bid_price)
        quote_ask = max(quote_ask, ask_price)
        if quote_bid >= quote_ask:
            return

        dynamic_fee_rate = await self._get_dynamic_fee_rate()
        self.rebate_reporter.record_api_health(self.fee_rate_client.get_health_snapshot())
        if dynamic_fee_rate is not None:
            logger.debug(
                f"Using dynamic fee rate: {float(dynamic_fee_rate):.6f} for token {self.current_token_id}"
            )
        bid_econ = estimate_quote_economics(
            quote_size_usdc=self.maker_quote_size_usdc,
            probability=quote_bid,
            half_spread=(fair - quote_bid),
            adverse_selection_buffer=self.maker_adverse_selection_buffer,
            fee_rate_override=dynamic_fee_rate,
        )
        ask_econ = estimate_quote_economics(
            quote_size_usdc=self.maker_quote_size_usdc,
            probability=quote_ask,
            half_spread=(quote_ask - fair),
            adverse_selection_buffer=self.maker_adverse_selection_buffer,
            fee_rate_override=dynamic_fee_rate,
        )

        if bid_econ.expected_net_usdc < self.maker_min_expected_net_usdc and ask_econ.expected_net_usdc < self.maker_min_expected_net_usdc:
            logger.info(
                "Maker quotes skipped: expected net below threshold "
                f"(bid={float(bid_econ.expected_net_usdc):.6f}, ask={float(ask_econ.expected_net_usdc):.6f})"
            )
            self._cancel_active_maker_orders()
            return

        desired_quotes = {
            "buy": (quote_bid, bid_econ, bid_econ.expected_net_usdc >= self.maker_min_expected_net_usdc),
            "sell": (quote_ask, ask_econ, ask_econ.expected_net_usdc >= self.maker_min_expected_net_usdc),
        }
        if self.maker_quote_sides == "buy":
            desired_quotes["sell"] = (quote_ask, ask_econ, False)
        elif self.maker_quote_sides == "sell":
            desired_quotes["buy"] = (quote_bid, bid_econ, False)

        # Cancel sides that are no longer desired.
        for side in ("buy", "sell"):
            _, _, should_quote = desired_quotes[side]
            if not should_quote and side in self.active_maker_orders:
                self._cancel_maker_order_side(side, reason="risk")

        # Quote desired sides with selective requote.
        for side in ("buy", "sell"):
            limit_price, econ, should_quote = desired_quotes[side]
            if not should_quote:
                continue

            current = self.active_maker_orders.get(side)
            if current:
                if current.get("pending_cancel"):
                    continue
                current_price = Decimal(str(current.get("price", "0")))
                if abs(current_price - limit_price) < self.maker_requote_threshold:
                    continue
                self._cancel_maker_order_side(side, reason="requote")

            await self._submit_maker_quote(side, limit_price, econ, dynamic_fee_rate)

    async def _submit_maker_quote(self, side: str, limit_price: Decimal, econ, dynamic_fee_rate: Optional[Decimal] = None) -> None:
        instrument = self.cache.instrument(self.instrument_id) if self.instrument_id else None
        limit_price = self._align_price_to_tick(limit_price, side, instrument)
        if self.current_simulation_mode:
            sim_order_id = f"SIM-MAKER-{side.upper()}-{int(time.time() * 1000)}"
            token_qty_sim = 0.0
            if float(limit_price) > 0:
                token_qty_sim = float(min(self.maker_quote_size_usdc, self.maker_max_order_usdc) / limit_price)
            token_qty_sim = max(token_qty_sim, float(self.maker_min_shares))
            now_ts = time.time()
            ack_latency_ms = random.randint(
                min(self.sim_ack_latency_ms_min, self.sim_ack_latency_ms_max),
                max(self.sim_ack_latency_ms_min, self.sim_ack_latency_ms_max),
            )
            effective_fee_rate = dynamic_fee_rate
            if effective_fee_rate is None or effective_fee_rate <= 0:
                effective_fee_rate = self._infer_market_fee_rate_default()
            fee_rate_bps = int((effective_fee_rate * Decimal("10000")).quantize(Decimal("1")))
            self.active_maker_orders[side] = {
                "order": None,
                "simulated": True,
                "client_order_id": sim_order_id,
                "econ": econ,
                "price": limit_price,
                "side": side,
                "token_id": self.current_token_id,
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
                    "queue_rank": self.active_maker_orders[side]["queue_rank"],
                    "fee_rate_bps": fee_rate_bps,
                },
            )
            logger.info(
                f"[SIM-MAKER] QUOTE {side.upper()} qty={token_qty_sim:.6f} px={float(limit_price):.4f} "
                f"net={float(econ.expected_net_usdc):.6f}"
            )
            return
        if not self.instrument_id:
            return
        if not instrument:
            return

        quote_notional_usdc = min(self.maker_quote_size_usdc, self.maker_max_order_usdc)
        if quote_notional_usdc < self.maker_quote_size_usdc:
            logger.warning(
                f"Maker quote notional capped by MAKER_MAX_ORDER_USDC: "
                f"{float(self.maker_quote_size_usdc):.4f} -> {float(quote_notional_usdc):.4f}"
            )

        token_qty = float(quote_notional_usdc) / float(limit_price) if float(limit_price) > 0 else 0.0
        precision = instrument.size_precision
        token_qty = round(token_qty, precision)
        min_qty = max(Decimal(str(10 ** (-precision))), self.maker_min_shares)
        min_notional = min_qty * limit_price
        if min_notional > self.maker_max_order_usdc:
            logger.warning(
                "Skip maker quote: min shares constraint exceeds maker max order notional "
                f"(min_shares={float(min_qty):.6f}, px={float(limit_price):.4f}, "
                f"min_notional=${float(min_notional):.4f}, cap=${float(self.maker_max_order_usdc):.4f})."
            )
            self._db_order_event(
                event_type="ORDER_SKIP_NOTIONAL_CAP",
                side=side.upper(),
                price=float(limit_price),
                qty=float(min_qty),
                reason="min_shares_exceeds_notional_cap",
            )
            return
        token_qty = max(Decimal(str(token_qty)), min_qty)
        token_qty = float(token_qty)
        qty = Quantity(token_qty, precision=precision)
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        price_precision = int(getattr(instrument, "price_precision", 3))
        price = Price.from_str(f"{float(limit_price):.{price_precision}f}")
        order_id = ClientOrderId(f"BTC-15M-MAKER-{side.upper()}-{int(time.time() * 1000)}")

        order_kwargs = dict(
            instrument_id=self.instrument_id,
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

        # Final guard: refuse quote if it would cross current top of book.
        if self.latest_market_bid is not None and self.latest_market_ask is not None:
            if side == "buy" and limit_price >= self.latest_market_ask:
                logger.warning(f"Skip crossing BUY quote {float(limit_price):.4f} >= ask {float(self.latest_market_ask):.4f}")
                return
            if side == "sell" and limit_price <= self.latest_market_bid:
                logger.warning(f"Skip crossing SELL quote {float(limit_price):.4f} <= bid {float(self.latest_market_bid):.4f}")
                return

        self.submit_order(order)
        self.consecutive_denied_orders = 0
        self.active_maker_orders[side] = {
            "order": order,
            "econ": econ,
            "price": limit_price,
            "side": side,
            "token_id": self.current_token_id,
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
        
        # Start instrument reload timer
        self._reload_stop_event.clear()
        self._reload_thread = threading.Thread(target=self._start_reload_timer, daemon=True)
        self._reload_thread.start()
        self._quote_watchdog_stop_event.clear()
        self._quote_watchdog_thread = threading.Thread(target=self._start_quote_watchdog_timer, daemon=True)
        self._quote_watchdog_thread.start()
        
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
                if not self._find_btc_instrument():
                    logger.warning("Reload completed but no BTC 15-min instrument found")
                
                logger.info("Instruments reloaded successfully")
            except Exception as e:
                logger.error(f"Failed to reload instruments: {e}")

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
            try:
                self.subscribe_quote_ticks(self.instrument_id)
            except Exception as e:
                logger.debug(f"Quote watchdog subscribe failed: {e}")
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
            stale_for = (now_ts - self.last_valid_quote_ts) if self.last_valid_quote_ts > 0 else None
            if stale_for is None or stale_for < self.quote_stale_sec:
                continue
            self._trigger_quote_watchdog_reload("timer_stale_quotes", now_ts)

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
        else:
            # Select the next future market
            future_markets.sort(key=lambda x: x['time_diff_minutes'])
            selected = future_markets[0]
            logger.info(f"⚠ No current market, selecting next: {selected['slug']} (starts in {selected['time_diff_minutes']:.1f} min)")
        
        self.instrument_id = selected['instrument'].id
        self.current_token_id = self._extract_token_id_from_instrument(str(self.instrument_id))
        self.subscribe_quote_ticks(self.instrument_id)
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
            # Check if we have valid prices
            if tick.bid_price is None or tick.ask_price is None:
                self.consecutive_invalid_quote_ticks += 1
                logger.debug(f"Skipping incomplete quote: bid={tick.bid_price}, ask={tick.ask_price}")
                self._maybe_run_quote_watchdog(trigger="incomplete_quote")
                return
            
            # Get decimal values properly
            bid_decimal = tick.bid_price.as_decimal()
            ask_decimal = tick.ask_price.as_decimal()
            self.last_valid_quote_ts = time.time()
            self.consecutive_invalid_quote_ticks = 0
            self.latest_market_bid = bid_decimal
            self.latest_market_ask = ask_decimal
            
            # Calculate mid price
            mid_price = (bid_decimal + ask_decimal) / 2
            
            # Update price history
            self.price_history.append(mid_price)
            self.real_price_history.append(mid_price)
            
            # Limit history size
            if len(self.price_history) > self.max_history:
                self.price_history.pop(0)
            if len(self.real_price_history) > self.max_real_history:
                self.real_price_history.pop(0)

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
        for side, state in list(self.active_maker_orders.items()):
            order = state.get("order")
            if order and str(order.client_order_id) == filled_id:
                filled_side = side
                filled_econ = state.get("econ")
                qty = Decimal(str(state.get("quantity", "0")))
                if side == "buy":
                    self.inventory_delta_shares += qty
                else:
                    self.inventory_delta_shares -= qty
                self.active_maker_orders.pop(side, None)
                break

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

    def on_order_canceled(self, event):
        """Handle cancel acknowledgements to clear pending-cancel state."""
        canceled_id = str(getattr(event, "client_order_id", "") or "")
        for side, state in list(self.active_maker_orders.items()):
            order = state.get("order")
            if order and str(order.client_order_id) == canceled_id:
                self.active_maker_orders.pop(side, None)
                break
        self._db_order_event(
            event_type="ORDER_CANCELED",
            client_order_id=canceled_id,
            venue_order_id=str(getattr(event, "venue_order_id", "")) if getattr(event, "venue_order_id", None) else None,
            side=str(getattr(event, "order_side", "")),
            status="CANCELED",
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
        for side, state in list(self.active_maker_orders.items()):
            order = state.get("order")
            if order and str(order.client_order_id) == denied_id:
                self.active_maker_orders.pop(side, None)
                break

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
        if ("not enough balance" in lowered) or ("allowance" in lowered):
            self.quote_pause_until_ts = max(self.quote_pause_until_ts, time.time() + self.maker_balance_pause_sec)
            self._cancel_active_maker_orders()
            logger.warning(
                f"Balance/allowance rejection detected; pause quoting for {self.maker_balance_pause_sec}s. "
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
        self._reload_stop_event.set()
        self._quote_watchdog_stop_event.set()
        if self._reload_thread and self._reload_thread.is_alive():
            self._reload_thread.join(timeout=2)
        if self._quote_watchdog_thread and self._quote_watchdog_thread.is_alive():
            self._quote_watchdog_thread.join(timeout=2)
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
