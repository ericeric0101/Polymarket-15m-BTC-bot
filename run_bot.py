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
from collections import deque
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
from bot.inventory import InventoryLedger
from bot.execution_events import (
    reconcile_benign_cancel_reject,
    reconcile_cancel_ack,
    reconcile_rejected_order,
)
from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.enums import ActiveSide, MarketPhase
from bot.side_decision import SideDecisionMixin
from bot.spot_pricer import SpotPricerMixin
from bot.taker_exit import TakerExitMixin
from bot.fill_ledger import FillLedgerMixin
from bot.order_runtime import OrderRuntimeMixin
from bot.pricing_runtime import PricingRuntimeMixin
from bot.recovery import StrategyRecoveryMixin
from bot.lifecycle_runtime import StrategyLifecycleMixin
from bot.lifecycle import (
    collect_btc_market_candidates,
    evaluate_market_phase,
    resolve_bi_side_market_selection,
)
from bot.ops import (
    adjust_inventory_after_merge,
    dedupe_price_history,
    extend_synthetic_history,
    handle_quote_watchdog_recovery,
    log_strategy_run_start,
    log_strategy_run_stop,
    run_auto_redeem_script,
    start_background_thread,
    stop_event_threads,
    should_run_quote_watchdog,
    should_skip_auto_redeem_run,
)
from bot.market_data import (
    estimate_external_spot_sigma_annualized,
    extract_market_start_ts_from_slug,
    extract_price_to_beat_from_market_payload,
    extract_strike_from_question,
    fetch_binance_open_price_sync,
    fetch_coinbase_spot_sync,
    record_external_spot_observation,
    resolve_opening_strike_from_history,
    fetch_gamma_market_by_slug,
)
from bot.post_trade import (
    build_fill_order_event_payload,
)
from bot.models import ExitDecisionType, MarketSnapshot, PositionState, SignalDecision
from bot.quoting import (
    apply_quote_plan_guards,
    normalize_quote_mode,
)
from bot.quote_service import (
    apply_sellable_inventory_guard,
    build_active_maker_order_state,
    build_desired_quote_entry,
    build_directional_snapshot,
    build_limit_order,
    build_quote_instrument_context,
    compute_requote_target_version,
    extract_instrument_tick,
    log_no_quote_diagnostics,
    reconcile_unwanted_quotes,
    retreat_crossing_buy_quote,
    should_requote_existing_order,
    violates_final_crossing_guard,
)
from bot.risk_policy import (
    FillCooldownConfig,
    FillCooldownPolicy,
    RegimeGuardConfig,
    RegimeGuardPolicy,
)
from execution.fee_rate_client import FeeRateClient
from execution.parameter_tuner import ParameterTuner
from execution.rebate_model import (
    CRYPTO_FEE_CURVE,
    estimate_taker_buy_fee_shares,
    estimate_taker_fee_usdc,
)
from execution.rebate_reporter import RebateReporter
from monitoring.performance_tracker import get_performance_tracker
from monitoring.grafana_exporter import get_grafana_exporter
from monitoring.terminal_dashboard import TerminalDashboard
from monitoring.trade_journal_db import TradeJournalDB
from execution.maker_engine import MakerEngine, MakerEngineConfig
from execution.exit_policy import ExitPolicy, ExitPolicyConfig, ExitStage

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
        Path(__file__).parent / "scripts" / "patch_nautilus_polymarket_execution.py",
        Path(__file__).parent / "scripts" / "patch_py_clob_http_helpers.py",
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


def detect_runtime_git_revision(repo_root: Path) -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if head.returncode != 0:
            return "unknown"
        commit = head.stdout.strip() or "unknown"
        dirty = subprocess.run(
            ["git", "diff", "--quiet", "--exit-code"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if dirty.returncode == 1:
            return f"{commit}-dirty"
        return commit
    except Exception:
        return "unknown"


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


# _fetch_gamma_market_by_slug was moved to bot.market_data.fetch_gamma_market_by_slug


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
        market = asyncio.run(fetch_gamma_market_by_slug(slug))
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
    logger.info("Preflight check started.")

    auth = resolve_polymarket_auth()
    if not auth:
        logger.error("Polymarket auth resolution failed.")
        return False

    slugs = resolve_btc_15m_market_slugs()
    if not slugs:
        logger.error("Preflight failed: no BTC 15-min market slugs resolved")
        return False
    startup_verbose = os.getenv("STARTUP_VERBOSE", "0").strip().lower() in ("1", "true", "yes", "on")
    if startup_verbose:
        logger.info(f"Preflight market slugs: {slugs}")

    primary_slug, instrument_ids = resolve_best_btc_15m_market(slugs)
    if not primary_slug:
        logger.error("Preflight failed: no primary BTC 15-min slug selected")
        return False
    if not instrument_ids:
        logger.error(f"Preflight failed: no instrument IDs resolved for slug {primary_slug}")
        return False
    logger.info(f"Preflight market: primary_slug={primary_slug} instruments={len(instrument_ids)}")
    if startup_verbose:
        logger.info(f"Preflight instrument_ids: {[inst.value for inst in instrument_ids]}")

    redis_client = init_redis()
    if redis_client:
        logger.info("Preflight Redis check: OK")
    else:
        logger.warning("Preflight Redis check: skipped/unavailable")

    mode_text = "SIMULATION" if simulation else "LIVE TRADING"
    logger.info(f"Preflight mode target: {mode_text}")
    logger.info("Polymarket auth check: OK")
    logger.info("PREFLIGHT CHECK PASSED")
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


class IntegratedBTCStrategy(
    SideDecisionMixin,
    SpotPricerMixin,
    TakerExitMixin,
    FillLedgerMixin,
    OrderRuntimeMixin,
    PricingRuntimeMixin,
    StrategyRecoveryMixin,
    StrategyLifecycleMixin,
    Strategy,
):
    """
    Integrated BTC Strategy combining:
    - Nautilus trading framework
    - Our 7-phase system
    - Redis simulation control
    - Paper trading tracking
    - Auto-reload instruments every 12 minutes
    - Pre-loaded price history for immediate trading
    """
    
    def __init__(
        self,
        redis_client=None,
        enable_grafana=True,
        test_mode=False,
        selected_slug: Optional[str] = None,
        enable_terminal_dashboard: bool = False,
    ):
        super().__init__()
        
        # Nautilus
        self.instrument_id = None
        self.redis_client = redis_client
        self.selected_slug = selected_slug
        self.startup_verbose = os.getenv("STARTUP_VERBOSE", "0").strip().lower() in ("1", "true", "yes", "on")
        
        # Phase 6: Performance Tracking
        self.performance_tracker = get_performance_tracker()
        
        # Phase 6: Grafana (optional)
        if enable_grafana:
            self.grafana_exporter = get_grafana_exporter()
        else:
            self.grafana_exporter = None
        self.terminal_dashboard_enabled = enable_terminal_dashboard or (
            os.getenv("TERMINAL_DASHBOARD", "0").strip().lower() in ("1", "true", "yes", "on")
        )
        self.terminal_dashboard_refresh_sec = max(
            0.5, float(os.getenv("TERMINAL_DASHBOARD_REFRESH_SEC", "1"))
        )
        self.terminal_dashboard = (
            TerminalDashboard(
                title="BTC 15M Terminal Dashboard",
                refresh_interval_sec=self.terminal_dashboard_refresh_sec,
            )
            if self.terminal_dashboard_enabled
            else None
        )
        
        # Price history for signal processing
        self.price_history = []
        self.max_history = 100
        self.real_price_history: List[Decimal] = []
        self.real_price_history_by_inst: Dict[str, List[Decimal]] = {}
        self.max_real_history = int(os.getenv("MAKER_VOL_REAL_HISTORY_MAX", "300"))
        
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
        raw_quote_mode = os.getenv("MAKER_QUOTE_SIDES", "both").strip().lower()
        self.maker_quote_sides = normalize_quote_mode(raw_quote_mode)
        if raw_quote_mode in {"sell", "both_buy"}:
            logger.warning(
                f"Deprecated maker quote mode '{raw_quote_mode}' detected; coercing to UP-only 'both'."
            )
        self.maker_directional_edge_gate_enabled = os.getenv(
            "MAKER_DIRECTIONAL_EDGE_GATE_ENABLED", "0"
        ).strip().lower() in ("1", "true", "yes", "on")
        self.maker_min_directional_edge_ps = Decimal(os.getenv("MAKER_MIN_DIRECTIONAL_EDGE_PS", "0.02"))
        self.maker_min_directional_edge_ps_down = Decimal(
            os.getenv("MAKER_MIN_DIRECTIONAL_EDGE_PS_DOWN", os.getenv("MAKER_MIN_DIRECTIONAL_EDGE_PS", "0.02"))
        )
        self.maker_min_directional_edge_ps_conservative = Decimal(
            os.getenv("MAKER_MIN_DIRECTIONAL_EDGE_PS_CONSERVATIVE", "0.03")
        )
        self.maker_min_expected_net_usdc = Decimal(os.getenv("MAKER_MIN_EXPECTED_NET_USDC", "0.0001"))
        self.maker_reload_inventory_threshold_shares = Decimal(
            os.getenv(
                "MAKER_RELOAD_INVENTORY_THRESHOLD_SHARES",
                str(self.maker_fixed_shares if self.maker_fixed_shares > 0 else self.maker_min_shares),
            )
        )
        self.maker_reload_min_expected_net_multiplier = Decimal(
            os.getenv("MAKER_RELOAD_MIN_EXPECTED_NET_MULTIPLIER", "2.0")
        )
        self.maker_reload_min_directional_edge_ps = Decimal(
            os.getenv(
                "MAKER_RELOAD_MIN_DIRECTIONAL_EDGE_PS",
                str(self.maker_min_directional_edge_ps_conservative),
            )
        )
        self.directional_entry_min_score_abs = Decimal(
            os.getenv("DIRECTIONAL_ENTRY_MIN_SCORE_ABS", "1")
        )
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
        self.maker_buy_taker_leakage_prob = max(
            Decimal("0"),
            min(Decimal("1"), Decimal(os.getenv("MAKER_BUY_TAKER_LEAKAGE_PROB", "0.15"))),
        )
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
        # Strategy-side economics calibration: maker fee is treated as 0 by default.
        self.maker_econ_fee_rate_decimal = Decimal(os.getenv("MAKER_ECON_FEE_RATE_DECIMAL", "0"))
        if self.maker_econ_fee_rate_decimal < 0:
            self.maker_econ_fee_rate_decimal = Decimal("0")
        self.maker_max_order_usdc = Decimal(os.getenv("MAKER_MAX_ORDER_USDC", "1.0"))
        self.maker_auto_tune = os.getenv("MAKER_AUTO_TUNE", "0") == "1"
        self.maker_auto_tune_interval_sec = int(os.getenv("MAKER_AUTO_TUNE_INTERVAL_SEC", "300"))
        
        self.maker_momentum_filter_pct = Decimal(os.getenv("MAKER_MOMENTUM_FILTER_PCT", "0.06"))
        self.maker_momentum_window_ticks = int(os.getenv("MAKER_MOMENTUM_WINDOW_TICKS", "20"))
        self.bi_side_enabled = os.getenv("BI_SIDE_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
        self.bi_side_decision_mode = os.getenv("BI_SIDE_DECISION_MODE", "boundary_only").strip().lower()
        self.bi_side_default_mode = str(os.getenv("BI_SIDE_DEFAULT_MODE", "NONE") or "NONE").strip().upper()
        if self.bi_side_default_mode not in {ActiveSide.UP.value, ActiveSide.DOWN.value, ActiveSide.NONE.value}:
            self.bi_side_default_mode = ActiveSide.NONE.value
        self.bi_side_decision_grace_sec = max(0, int(os.getenv("BI_SIDE_DECISION_GRACE_SEC", "30")))
        self.bi_side_lock_until_reduce_only = os.getenv("BI_SIDE_LOCK_UNTIL_REDUCE_ONLY", "1").strip().lower() not in ("0", "false", "no")
        self.bi_side_allow_intramarket_flip = os.getenv("BI_SIDE_ALLOW_INTRAMARKET_FLIP", "0").strip().lower() in ("1", "true", "yes", "on")
        self.bi_side_min_score_up = Decimal(str(os.getenv("BI_SIDE_MIN_SCORE_UP", "1")))
        self.bi_side_max_score_down = Decimal(str(os.getenv("BI_SIDE_MAX_SCORE_DOWN", "-1")))
        self.bi_side_mixed_low = Decimal(str(os.getenv("BI_SIDE_MIXED_LOW", "-1")))
        self.bi_side_mixed_high = Decimal(str(os.getenv("BI_SIDE_MIXED_HIGH", "1")))
        self.bi_side_strike_gap_pct = Decimal(str(os.getenv("BI_SIDE_STRIKE_GAP_PCT", "0.0015")))
        self.bi_side_mom_window_ticks = max(2, int(os.getenv("BI_SIDE_MOM_WINDOW_TICKS", str(self.maker_momentum_window_ticks))))
        self.bi_side_mom_pct = Decimal(str(os.getenv("BI_SIDE_MOM_PCT", "0.0025")))
        self.bi_side_open_drift_pct = Decimal(str(os.getenv("BI_SIDE_OPEN_DRIFT_PCT", "0.0020")))
        self.bi_side_require_confirming_signal = os.getenv(
            "BI_SIDE_REQUIRE_CONFIRMING_SIGNAL", "1"
        ).strip().lower() not in ("0", "false", "no")
        self.bi_side_regime_n_markets = max(2, int(os.getenv("BI_SIDE_REGIME_N_MARKETS", "4")))
        self.bi_side_regime_sum_pnl_usdc = Decimal(str(os.getenv("BI_SIDE_REGIME_SUM_PNL_USDC", "-2.0")))
        self.bi_side_regime_min_neg = max(1, int(os.getenv("BI_SIDE_REGIME_MIN_NEG", "3")))
        self.bi_side_mixed_policy = str(os.getenv("BI_SIDE_MIXED_POLICY", "none") or "none").strip().lower()
        self.bi_side_mixed_small_size_mult = Decimal(str(os.getenv("BI_SIDE_MIXED_SMALL_SIZE_MULT", "0.0")))
        self.bi_side_down_size_mult = Decimal(str(os.getenv("BI_SIDE_DOWN_SIZE_MULT", "1.0")))
        self.bi_side_min_time_left_sec = max(0, int(os.getenv("BI_SIDE_MIN_TIME_LEFT_SEC", "180")))
        self.bi_side_reeval_interval_sec = max(0.2, float(os.getenv("BI_SIDE_REEVAL_INTERVAL_SEC", "1.0")))
        self.bi_side_decision_log_interval_sec = max(1.0, float(os.getenv("BI_SIDE_LOG_INTERVAL_SEC", "15.0")))
        self.bi_side_flip_confirmations = max(1, int(os.getenv("BI_SIDE_FLIP_CONFIRMATIONS", "2")))
        self.bi_side_flip_max_per_market = max(0, int(os.getenv("BI_SIDE_FLIP_MAX_PER_MARKET", "1")))
        self.bi_side_flip_min_score_up = Decimal(str(os.getenv("BI_SIDE_FLIP_MIN_SCORE_UP", "2")))
        self.bi_side_flip_max_score_down = Decimal(str(os.getenv("BI_SIDE_FLIP_MAX_SCORE_DOWN", "-2")))
        self.bi_side_flip_min_fair = Decimal(str(os.getenv("BI_SIDE_FLIP_MIN_FAIR", "0.60")))

        # --- SignalEngine toggle (new vs legacy side decision) ---
        # Set SIDE_DECISION_ENGINE_NEW=0 to revert to legacy integer-voting system
        self.side_decision_engine_new = os.getenv(
            "SIDE_DECISION_ENGINE_NEW", "1"
        ).strip().lower() in ("1", "true", "yes", "on")
        self.side_signal_min_confidence = float(os.getenv("SIDE_SIGNAL_MIN_CONFIDENCE", "0.15"))
        self.side_signal_threshold_up = float(os.getenv("SIDE_SIGNAL_THRESHOLD_UP", "0.05"))
        self.side_signal_threshold_down = float(os.getenv("SIDE_SIGNAL_THRESHOLD_DOWN", "0.05"))
        # SignalEngine EMA windows
        self.side_signal_btc_ema_fast_sec = float(os.getenv("SIDE_SIGNAL_BTC_EMA_FAST_SEC", "3.0"))
        self.side_signal_btc_ema_slow_sec = float(os.getenv("SIDE_SIGNAL_BTC_EMA_SLOW_SEC", "10.0"))
        self.side_signal_mid_ema_fast_sec = float(os.getenv("SIDE_SIGNAL_MID_EMA_FAST_SEC", "5.0"))
        self.side_signal_mid_ema_slow_sec = float(os.getenv("SIDE_SIGNAL_MID_EMA_SLOW_SEC", "20.0"))
        # BTC trend normalisation factor
        self.side_signal_btc_trend_norm_pct = float(os.getenv("SIDE_SIGNAL_BTC_TREND_NORM_PCT", "0.0005"))
        # Mid-price velocity reversal threshold
        self.side_signal_mid_velocity_reversal = float(os.getenv("SIDE_SIGNAL_MID_VELOCITY_REVERSAL", "0.010"))
        self.maker_fair_pricer_mode = os.getenv("MAKER_FAIR_PRICER_MODE", "drift").strip().lower()
        if self.maker_fair_pricer_mode not in {"drift", "digital"}:
            self.maker_fair_pricer_mode = "drift"
        self.maker_digital_vol_window = max(10, int(os.getenv("MAKER_DIGITAL_VOL_WINDOW", "120")))
        self.maker_digital_vol_min_points = max(5, int(os.getenv("MAKER_DIGITAL_VOL_MIN_POINTS", "20")))
        self.maker_digital_sigma_default = Decimal(os.getenv("MAKER_DIGITAL_SIGMA_DEFAULT", "0.60"))
        self.maker_digital_sigma_floor = Decimal(os.getenv("MAKER_DIGITAL_SIGMA_FLOOR", "0.20"))
        self.maker_digital_sigma_ceiling = Decimal(os.getenv("MAKER_DIGITAL_SIGMA_CEILING", "2.00"))
        self.maker_digital_vol_scale = Decimal(os.getenv("MAKER_DIGITAL_VOL_SCALE", "1.00"))
        # Dynamic sigma: time decay (sigma decreases as expiry approaches)
        self.maker_digital_sigma_time_decay_enabled = os.getenv("MAKER_DIGITAL_SIGMA_TIME_DECAY", "1") == "1"
        self.maker_digital_sigma_time_decay_ref_sec = float(os.getenv("MAKER_DIGITAL_SIGMA_TIME_DECAY_REF_SEC", "600"))
        self.maker_digital_sigma_time_decay_min = float(os.getenv("MAKER_DIGITAL_SIGMA_TIME_DECAY_MIN", "0.30"))
        self.taker_exit_enabled = os.getenv("TAKER_EXIT_ENABLED", "1").strip().lower() not in ("0", "false", "no")
        self.taker_exit_min_net_usdc = Decimal(os.getenv("TAKER_EXIT_MIN_NET_USDC", "0.02"))
        self.taker_exit_stop_loss_usdc = Decimal(os.getenv("TAKER_EXIT_STOP_LOSS_USDC", "0.15"))
        self.taker_exit_max_hold_sec = int(os.getenv("TAKER_EXIT_MAX_HOLD_SEC", "120"))
        self.taker_exit_min_hold_sec = int(os.getenv("TAKER_EXIT_MIN_HOLD_SEC", "20"))
        self.taker_exit_cooldown_sec = int(os.getenv("TAKER_EXIT_COOLDOWN_SEC", "8"))
        self.taker_exit_eval_interval_sec = max(
            0.0,
            float(os.getenv("TAKER_EXIT_EVAL_INTERVAL_SEC", "1.0")),
        )
        self.taker_exit_slippage_buffer_pct = Decimal(os.getenv("TAKER_EXIT_SLIPPAGE_BUFFER_PCT", "0.002"))
        self.taker_exit_only_on_profit = os.getenv("TAKER_EXIT_ONLY_ON_PROFIT", "0").strip().lower() in ("1", "true", "yes", "on")
        self.taker_exit_max_spread_pct = Decimal(os.getenv("TAKER_EXIT_MAX_SPREAD_PCT", "0.02"))
        self.taker_exit_stop_loss_max_spread_pct = Decimal(os.getenv("TAKER_EXIT_STOP_LOSS_MAX_SPREAD_PCT", "0.03"))
        self.taker_exit_wait_for_sell_quote_sec = max(
            0,
            int(os.getenv("TAKER_EXIT_WAIT_FOR_SELL_QUOTE_SEC", "20")),
        )
        self.market_stop_loss_max_per_market = max(
            0,
            int(os.getenv("MARKET_STOP_LOSS_MAX_PER_MARKET", "2")),
        )
        self.market_max_buy_events_per_market = max(
            0,
            int(os.getenv("MARKET_MAX_BUY_EVENTS_PER_MARKET", "2")),
        )
        self.taker_exit_max_hold_near_close_sec = max(
            0,
            int(os.getenv("TAKER_EXIT_MAX_HOLD_NEAR_CLOSE_SEC", "90")),
        )
        self.taker_exit_reject_cooldown_sec = max(
            0,
            int(os.getenv("TAKER_EXIT_REJECT_COOLDOWN_SEC", "20")),
        )
        self.taker_exit_skip_log_interval_sec = max(
            1,
            int(os.getenv("TAKER_EXIT_SKIP_LOG_INTERVAL_SEC", "20")),
        )
        self.taker_exit_disable_stop_loss_last_sec = max(
            0,
            int(os.getenv("TAKER_EXIT_DISABLE_STOP_LOSS_LAST_SEC", "45")),
        )
        self.taker_exit_stop_loss_confirmations = max(
            1,
            int(os.getenv("TAKER_EXIT_STOP_LOSS_CONFIRMATIONS", "2")),
        )
        self.stop_loss_reentry_cooldown_sec = max(
            0,
            int(os.getenv("STOP_LOSS_REENTRY_COOLDOWN_SEC", "180")),
        )
        self.exit_conviction_band_min_price = Decimal(
            os.getenv("EXIT_CONVICTION_BAND_MIN_PRICE", "0.60")
        )
        self.exit_hold_band_min_price = Decimal(
            os.getenv("EXIT_HOLD_BAND_MIN_PRICE", "0.68")
        )
        self.exit_conviction_band_min_score_abs = Decimal(
            os.getenv("EXIT_CONVICTION_BAND_MIN_SCORE_ABS", "1")
        )
        self.exit_hold_band_min_score_abs = Decimal(
            os.getenv("EXIT_HOLD_BAND_MIN_SCORE_ABS", "1")
        )
        self.exit_conviction_stop_loss_multiplier = Decimal(
            os.getenv("EXIT_CONVICTION_STOP_LOSS_MULTIPLIER", "1.75")
        )
        self.exit_conviction_extra_confirmations = max(
            0,
            int(os.getenv("EXIT_CONVICTION_EXTRA_CONFIRMATIONS", "1")),
        )
        self.exit_stop_loss_requires_thesis_weakening = os.getenv(
            "EXIT_STOP_LOSS_REQUIRES_THESIS_WEAKENING", "1"
        ).strip().lower() not in ("0", "false", "no")
        self.exit_stop_loss_thesis_min_score_abs = Decimal(
            os.getenv("EXIT_STOP_LOSS_THESIS_MIN_SCORE_ABS", "1")
        )
        self.exit_hold_band_requires_locked = os.getenv(
            "EXIT_HOLD_BAND_REQUIRES_LOCKED", "1"
        ).strip().lower() not in ("0", "false", "no")
        self.maker_profit_run_enabled = os.getenv(
            "MAKER_PROFIT_RUN_ENABLED", "1"
        ).strip().lower() not in ("0", "false", "no")
        self.maker_profit_run_min_hold_sec = max(
            0,
            int(os.getenv("MAKER_PROFIT_RUN_MIN_HOLD_SEC", "20")),
        )
        self.maker_profit_run_min_score_abs = Decimal(
            os.getenv("MAKER_PROFIT_RUN_MIN_SCORE_ABS", "1")
        )
        self.maker_profit_run_min_profit_ps = Decimal(
            os.getenv("MAKER_PROFIT_RUN_MIN_PROFIT_PS", "0.04")
        )
        self.maker_profit_run_trailing_drawdown_ps = Decimal(
            os.getenv("MAKER_PROFIT_RUN_TRAILING_DRAWDOWN_PS", "0.05")
        )
        self.maker_profit_run_unlock_profit_ps = Decimal(
            os.getenv("MAKER_PROFIT_RUN_UNLOCK_PROFIT_PS", "0.18")
        )
        self.maker_profit_run_unlock_trailing_drawdown_ps = Decimal(
            os.getenv("MAKER_PROFIT_RUN_UNLOCK_TRAILING_DRAWDOWN_PS", "0.02")
        )
        
        # Maker-style urgent exit: place maker SELL at best_bid when thesis weakens
        self.maker_urgent_exit_enabled = os.getenv(
            "MAKER_URGENT_EXIT_ENABLED", "1"
        ).strip().lower() in ("1", "true", "yes", "on")
        self.maker_urgent_exit_min_loss_usdc = Decimal(
            os.getenv("MAKER_URGENT_EXIT_MIN_LOSS_USDC", "0.10")
        )
        self.maker_urgent_exit_ttl_sec = max(
            5, int(os.getenv("MAKER_URGENT_EXIT_TTL_SEC", "15"))
        )
        self.maker_urgent_exit_cooldown_sec = max(
            1, int(os.getenv("MAKER_URGENT_EXIT_COOLDOWN_SEC", "5"))
        )
        self.maker_urgent_exit_min_confirmations = max(
            1, int(os.getenv("MAKER_URGENT_EXIT_MIN_CONFIRMATIONS", "3"))
        )
        # Implied sigma: derive σ from market mid to improve fair price
        self.maker_implied_sigma_enabled = os.getenv(
            "MAKER_DIGITAL_IMPLIED_SIGMA_ENABLED", "1"
        ).strip().lower() in ("1", "true", "yes", "on")
        self.maker_implied_sigma_weight = Decimal(
            os.getenv("MAKER_DIGITAL_IMPLIED_SIGMA_WEIGHT", "0.50")
        )
        self.exit_policy = ExitPolicy(
            ExitPolicyConfig(
                aggressive_stage_sec=max(30, int(os.getenv("EXIT_POLICY_AGGRESSIVE_STAGE_SEC", "180"))),
                taker_stage_sec=max(15, int(os.getenv("EXIT_POLICY_TAKER_STAGE_SEC", "75"))),
            )
        )
        self.exit_policy_engine = ExitPolicyEngine(
            ExitEngineConfig(
                min_hold_sec=self.taker_exit_min_hold_sec,
                stop_loss_usdc=self.taker_exit_stop_loss_usdc,
                stop_loss_confirmations=self.taker_exit_stop_loss_confirmations,
                stop_loss_requires_thesis_weakening=self.exit_stop_loss_requires_thesis_weakening,
                stop_loss_thesis_min_score_abs=self.exit_stop_loss_thesis_min_score_abs,
                conviction_band_min_price=self.exit_conviction_band_min_price,
                hold_band_min_price=self.exit_hold_band_min_price,
                conviction_band_min_score_abs=self.exit_conviction_band_min_score_abs,
                hold_band_min_score_abs=self.exit_hold_band_min_score_abs,
                conviction_stop_loss_multiplier=self.exit_conviction_stop_loss_multiplier,
                conviction_extra_confirmations=self.exit_conviction_extra_confirmations,
                hold_band_requires_locked=self.exit_hold_band_requires_locked,
            ),
        )
        self.runtime_git_revision = detect_runtime_git_revision(project_root)

        self.regime_guard_enabled = os.getenv("REGIME_GUARD_ENABLED", "1").strip().lower() not in ("0", "false", "no")
        self.regime_guard_n_markets = max(2, int(os.getenv("REGIME_GUARD_N_MARKETS", "4")))
        self.regime_guard_trigger_sum_pnl_usdc = Decimal(os.getenv("REGIME_GUARD_TRIGGER_SUM_PNL_USDC", "-3.5"))
        self.regime_guard_cooldown_sec = max(60, int(os.getenv("REGIME_GUARD_COOLDOWN_SEC", "3600")))
        self.regime_guard_min_negative_markets = max(
            1,
            min(
                self.regime_guard_n_markets,
                int(
                    os.getenv(
                        "REGIME_GUARD_MIN_NEGATIVE_MARKETS",
                        str(max(1, self.regime_guard_n_markets - 1)),
                    )
                ),
            ),
        )
        self.regime_guard_bootstrap_lookback_markets = max(
            self.regime_guard_n_markets,
            int(
                os.getenv(
                    "REGIME_GUARD_BOOTSTRAP_LOOKBACK_MARKETS",
                    str(self.regime_guard_n_markets * 3),
                )
            ),
        )
        self.maker_sell_cost_protect_enabled = os.getenv(
            "MAKER_SELL_COST_PROTECT_ENABLED", "1"
        ).strip().lower() not in ("0", "false", "no")
        self.maker_sell_cost_protect_fee_buffer_ps = Decimal(
            os.getenv("MAKER_SELL_COST_PROTECT_FEE_BUFFER_PS", "0.005")
        )
        self.maker_sell_min_profit_floor_ps = Decimal(
            os.getenv("MAKER_SELL_MIN_PROFIT_FLOOR_PS", "0")
        )
        self.maker_sell_cost_protect_emergency_last_sec = max(
            0, int(os.getenv("MAKER_SELL_COST_PROTECT_EMERGENCY_LAST_SEC", "60"))
        )
        self.maker_high_cost_exit_cooldown_enabled = os.getenv(
            "MAKER_HIGH_COST_EXIT_COOLDOWN_ENABLED", "1"
        ).strip().lower() not in ("0", "false", "no")
        self.maker_high_cost_fill_threshold = Decimal(
            os.getenv("MAKER_HIGH_COST_FILL_THRESHOLD", "0.75")
        )
        self.maker_high_cost_exit_cooldown_sec = max(
            0, int(os.getenv("MAKER_HIGH_COST_EXIT_COOLDOWN_SEC", "180"))
        )
        self.regime_guard_policy = RegimeGuardPolicy(
            RegimeGuardConfig(
                n_markets=self.regime_guard_n_markets,
                trigger_sum_pnl_usdc=self.regime_guard_trigger_sum_pnl_usdc,
                min_negative_markets=self.regime_guard_min_negative_markets,
            )
        )
        
        # Performance / Execution
        self.maker_cancel_max_retries = int(os.getenv("MAKER_CANCEL_MAX_RETRIES", "3"))
        self.maker_cancel_cooldown_sec = int(os.getenv("MAKER_CANCEL_COOLDOWN_SEC", "2"))
        self.maker_cancel_ack_timeout_sec = int(os.getenv("MAKER_CANCEL_ACK_TIMEOUT_SEC", "8"))
        self.maker_requote_min_age_sec = max(
            0.0,
            float(os.getenv("MAKER_REQUOTE_MIN_AGE_SEC", "6")),
        )
        self.maker_requote_min_age_sec_sell = max(
            0.0,
            float(os.getenv("MAKER_REQUOTE_MIN_AGE_SEC_SELL", "0")),
        )
        self.maker_early_sell_only_sec = max(
            0,
            int(os.getenv("MAKER_EARLY_SELL_ONLY_SEC", "120")),
        )
        self.quote_healthcheck_interval_sec = int(os.getenv("QUOTE_HEALTHCHECK_INTERVAL_SEC", "10"))
        self.strategy_status_interval_sec = max(15, int(os.getenv("STRATEGY_STATUS_INTERVAL_SEC", "60")))
        self.quote_stale_sec = int(os.getenv("QUOTE_STALE_SEC", "30"))
        self.quote_invalid_tick_reload_threshold = int(os.getenv("QUOTE_INVALID_TICK_RELOAD_THRESHOLD", "80"))
        self.quote_reload_cooldown_sec = int(os.getenv("QUOTE_RELOAD_COOLDOWN_SEC", "60"))
        self.last_quote_update_ts = 0.0
        self.quote_pause_until_ts = 0.0
        # --- Adverse Selection Protection ---
        self.post_fill_buy_cooldown_sec: float = float(os.getenv("MAKER_POST_FILL_BUY_COOLDOWN_SEC", "15"))
        self.buy_cooldown_until_ts: float = 0.0
        self.max_consecutive_losses: int = int(os.getenv("MAKER_MAX_CONSECUTIVE_LOSSES", "3"))
        self.loss_pause_sec: float = float(os.getenv("MAKER_LOSS_PAUSE_SEC", "60"))
        self.recent_fill_pnl_results: list = []  # list of realized_net_usdc from recent fills
        self.fill_cooldown_policy = FillCooldownPolicy(
            FillCooldownConfig(
                post_fill_buy_cooldown_sec=self.post_fill_buy_cooldown_sec,
                max_consecutive_losses=self.max_consecutive_losses,
                loss_pause_sec=self.loss_pause_sec,
            )
        )
        self.last_valid_quote_ts = 0.0
        self.consecutive_invalid_quote_ticks = 0
        self.last_quote_watchdog_check_ts = 0.0
        self.last_quote_watchdog_reload_ts = 0.0

        # --- New Engines Init ---
        maker_config = MakerEngineConfig(
            maker_half_spread=self.maker_half_spread,
            maker_quote_size_usdc=self.maker_quote_size_usdc,
            maker_min_shares=self.maker_min_shares,
            maker_fixed_shares=self.maker_fixed_shares,
            maker_max_order_usdc=self.maker_max_order_usdc,
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
            maker_buy_taker_leakage_prob=self.maker_buy_taker_leakage_prob,
        )
        self.maker_engine = MakerEngine(maker_config)

        self.last_status_log_ts = 0.0
        self.orderbook_unavailable_until_ts = 0.0
        self.orderbook_unavailable_token: Optional[str] = None
        self.last_external_spot: Optional[Decimal] = None
        self.latest_external_spot: Optional[Decimal] = None
        self.external_spot_consecutive_failures: int = 0  # BUG-5 FIX
        self.external_spot_max_failures: int = int(os.getenv("EXTERNAL_SPOT_MAX_FAILURES", "10"))
        self.external_spot_history: List[Tuple[float, Decimal]] = []
        self.external_spot_history_max = max(60, int(os.getenv("EXTERNAL_SPOT_HISTORY_MAX", "1200")))
        self.market_strike_cache_by_slug: Dict[str, Decimal] = {}
        self.market_strike_source_by_slug: Dict[str, str] = {}
        self.market_start_ts_by_slug: Dict[str, int] = {}
        self.market_strike_anchor_max_lag_sec = max(10, int(os.getenv("MARKET_STRIKE_ANCHOR_MAX_LAG_SEC", "180")))
        self.market_strike_anchor_near_sec = max(5, int(os.getenv("MARKET_STRIKE_ANCHOR_NEAR_SEC", "30")))
        self.market_strike_rest_retry_sec = max(10, int(os.getenv("MARKET_STRIKE_REST_RETRY_SEC", "60")))
        self.market_strike_rest_last_try_ts_by_slug: Dict[str, float] = {}
        self.market_strike_gamma_validate_interval_sec = max(
            30, int(os.getenv("MARKET_STRIKE_GAMMA_VALIDATE_INTERVAL_SEC", "180"))
        )
        self.market_strike_gamma_warn_abs_usd = max(
            Decimal("1"), Decimal(str(os.getenv("MARKET_STRIKE_GAMMA_WARN_ABS_USD", "5")))
        )
        self.market_strike_gamma_mismatch_warn_interval_sec = max(
            30, int(os.getenv("MARKET_STRIKE_GAMMA_WARN_INTERVAL_SEC", "120"))
        )
        self.market_strike_last_gamma_validate_ts_by_slug: Dict[str, float] = {}
        self.market_strike_last_gamma_warn_ts_by_slug: Dict[str, float] = {}
        self._last_strike_slug_log_ts = 0.0
        self.no_quote_diag_interval_sec = max(15, int(os.getenv("NO_QUOTE_DIAG_INTERVAL_SEC", "60")))
        self._last_no_quote_diag_ts_by_inst: Dict[str, float] = {}
        self._last_sellable_skip_log_ts_by_inst: Dict[str, float] = {}
        self.maker_profit_run_peak_bid_by_inst: Dict[str, Decimal] = {}
        self.maker_profit_run_peak_fair_by_inst: Dict[str, Decimal] = {}
        self.recent_buy_fill_ts_by_inst: Dict[str, float] = {}
        self.sellable_fallback_after_buy_sec = max(
            0,
            int(os.getenv("SELLABLE_FALLBACK_AFTER_BUY_SEC", "600")),
        )
        self.maker_gate_block_grace_sec = max(0, int(os.getenv("MAKER_GATE_BLOCK_GRACE_SEC", "4")))
        self._gate_block_since_by_order_key: Dict[str, float] = {}
        self._gate_block_reason_by_order_key: Dict[str, str] = {}
        self._gate_last_cancel_ts_by_order_key: Dict[str, float] = {}
        self._cancel_ack_dedupe_window_sec = max(
            1, int(os.getenv("MAKER_CANCEL_ACK_DEDUPE_WINDOW_SEC", "3"))
        )
        self._last_cancel_ack_ts_by_client_order_id: Dict[str, float] = {}
        # Requote state machine: upgrade target "version" only when desired price
        # moves outside hysteresis band. Active orders requote only when lagging version.
        self._target_anchor_price_by_order_key: Dict[str, Decimal] = {}
        self._target_version_by_order_key: Dict[str, int] = {}
        self.strike_fallback_log_interval_sec = max(10, int(os.getenv("STRIKE_FALLBACK_LOG_INTERVAL_SEC", "60")))
        self._last_strike_fallback_log_ts = 0.0
        self._last_digital_pricer_log_ts = 0.0
        self.live_inventory_cost: Dict[str, Dict[str, Any]] = {}
        self.last_taker_exit_ts_by_inst: Dict[str, float] = {}
        self.pending_taker_exit_by_inst: Dict[str, str] = {}
        self.taker_exit_reason_by_client_order_id: Dict[str, str] = {}
        self.taker_exit_tail_attempted_by_inst: Dict[str, float] = {}
        self.taker_exit_last_eval_ts_by_inst: Dict[str, float] = {}
        self.taker_exit_reject_cooldown_until_by_inst: Dict[str, float] = {}
        self.taker_exit_stop_loss_hits_by_inst: Dict[str, int] = {}
        self.stop_loss_reentry_pause_until_by_inst: Dict[str, float] = {}
        self.side_stop_loss_penalty_until_by_market_side: Dict[str, float] = {}
        self.market_stop_loss_count_by_slug: Dict[str, int] = {}
        self.market_buy_count_by_slug: Dict[str, int] = {}
        self.market_buy_counted_order_ids_by_slug: Dict[str, Set[str]] = {}
        self._taker_exit_skip_log_ts_by_key: Dict[str, float] = {}
        self.high_cost_exit_cooldown_until_by_inst: Dict[str, float] = {}
        self.high_cost_last_fill_price_by_inst: Dict[str, float] = {}
        self.market_cycle_realized_net_usdc = Decimal("0")
        self.recent_market_combined_pnls: deque[float] = deque(maxlen=self.regime_guard_n_markets)
        self.regime_guard_conservative_until_ts: float = 0.0
        self.fee_log_interval_sec = max(5, int(os.getenv("FEE_LOG_INTERVAL_SEC", "60")))
        self._last_fee_log_state_by_token: Dict[str, Dict[str, Any]] = {}
        self.fee_rate_fetch_interval_sec = max(
            5,
            int(os.getenv("FEE_RATE_FETCH_INTERVAL_SEC", os.getenv("FEE_RATE_CACHE_TTL_SEC", "300"))),
        )
        self._fee_rate_local_cache_by_token: Dict[str, Dict[str, Any]] = {}
        self.latest_market_bid: Optional[Decimal] = None
        self.latest_market_ask: Optional[Decimal] = None
        self.latest_market_bid_ts: float = 0.0  # BUG-2 FIX: timestamp for staleness check
        self.latest_market_ask_ts: float = 0.0
        self.stale_quote_synth_max_age_sec: float = float(os.getenv("STALE_QUOTE_SYNTH_MAX_AGE_SEC", "10"))
        self.latest_quote_depth_by_inst: Dict[str, Tuple[Optional[Decimal], Optional[Decimal]]] = {}
        self.orderbook_levels_cache_by_token: Dict[str, Dict[str, Any]] = {}
        self._inventory_delta_shares = Decimal("0")
        self._startup_rehydrated_inventory_force_sell_only = False
        self.inventory_last_update_ts = 0.0
        self.consecutive_denied_orders = 0
        self.maker_kill_switch = False
        self.active_maker_orders: Dict[str, Any] = {}
        self.current_token_id: Optional[str] = None
        self.current_market_instruments: List[InstrumentId] = []
        self.current_up_instrument_id: Optional[InstrumentId] = None
        self.current_down_instrument_id: Optional[InstrumentId] = None
        self.current_market_open_spot: Optional[Decimal] = None
        self.active_side: ActiveSide = ActiveSide.UP if not self.bi_side_enabled else ActiveSide.NONE
        self.active_side_locked: bool = False
        self.side_decision_ts: float = 0.0
        self.side_decision_score: Decimal = Decimal("0")
        self.side_decision_reason: str = "startup"
        self.side_decision_due_ts: float = 0.0
        self.side_decision_done_for_market: bool = False
        self.side_decision_inputs: Dict[str, Any] = {}
        self._force_quote_refresh_once: bool = False
        self._force_quote_refresh_reason: str = ""
        self.side_flip_count: int = 0
        self.side_pending_flip_side: ActiveSide = ActiveSide.NONE
        self.side_pending_flip_count: int = 0
        self._last_side_observation_signature: Optional[Tuple[str, str, str]] = None
        self._side_decision_skip_log_ts_by_reason: Dict[str, float] = {}
        self.side_decision_skip_log_interval_sec = max(
            2.0, float(os.getenv("BI_SIDE_SKIP_LOG_INTERVAL_SEC", "10"))
        )
        self._last_side_decision_log_ts: float = 0.0
        self._last_side_decision_log_signature: Optional[Tuple[str, str, str, str]] = None
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
        self.auto_redeem_min_gap_sec = max(0, int(os.getenv("AUTO_REDEEM_MIN_GAP_SEC", "300")))
        self.auto_redeem_slug_filter = os.getenv("AUTO_REDEEM_SLUG_FILTER", "btc-updown-15m").strip()
        self._redeem_stop_event = threading.Event()
        self._redeem_thread: Optional[threading.Thread] = None
        self._redeem_job_lock = threading.Lock()
        self._last_redeem_run_ts = 0.0
        self._balance_stop_event = threading.Event()
        self._balance_thread: Optional[threading.Thread] = None
        self._balance_refresh_lock = threading.Lock()
        self._balance_refresh_inflight = False
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
        self._terminal_dashboard_stop_event = threading.Event()
        self._terminal_dashboard_thread: Optional[threading.Thread] = None
        # --- Balance Pre-check ---
        self._cached_usdc_balance: Optional[Decimal] = None
        self._balance_last_check_ts: float = 0.0
        self._last_balance_log_ts: float = 0.0
        self._last_logged_balance_value: Optional[Decimal] = None
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
        self._sell_recovery_required_by_inst: Dict[str, float] = {}
        self._sell_recovery_reason_by_inst: Dict[str, str] = {}
        self._sell_recovery_venue_cap_by_inst: Dict[str, Decimal] = {}
        self.sell_delay_after_buy_sec = max(
            0.0, float(os.getenv("SELL_DELAY_AFTER_BUY_SEC", "3"))
        )
        self.sell_balance_retry_pause_sec = max(
            1.0, float(os.getenv("SELL_BALANCE_RETRY_PAUSE_SEC", "3"))
        )

        # --- Binance WebSocket for real-time BTC price ---
        self._binance_ws_price: Optional[Decimal] = None
        self._binance_ws_price_ts: float = 0.0
        self._binance_ws_stop_event = threading.Event()
        self._binance_ws_thread: Optional[threading.Thread] = None

        # --- SignalEngine (continuous probabilistic side decision) ---
        from bot.signal_engine import SignalEngine, SignalEngineConfig
        self._signal_engine = SignalEngine(SignalEngineConfig(
            btc_ema_fast_sec=self.side_signal_btc_ema_fast_sec,
            btc_ema_slow_sec=self.side_signal_btc_ema_slow_sec,
            mid_ema_fast_sec=self.side_signal_mid_ema_fast_sec,
            mid_ema_slow_sec=self.side_signal_mid_ema_slow_sec,
            min_confidence=self.side_signal_min_confidence,
            btc_trend_norm_pct=self.side_signal_btc_trend_norm_pct,
            mid_velocity_reversal_threshold=self.side_signal_mid_velocity_reversal,
        ))
        self._maker_worker_lock = threading.Lock()
        self._maker_worker_running = False
        self.run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        self.trade_db_enabled = os.getenv("TRADE_DB_ENABLED", "1").strip().lower() not in ("0", "false", "no")
        self.trade_db = TradeJournalDB(
            db_path=os.getenv("TRADE_DB_PATH", "./logs/trade_journal.db"),
        ) if self.trade_db_enabled else None
        self._cycle_total_trades = 0
        self._cycle_total_wins = 0

        if test_mode:
            logger.info("⚠️  TEST MODE ACTIVE - Trading every minute!")
        logger.info("Integrated BTC strategy initialized.")

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
        logger.info(
            "Config summary: "
            f"mode={'maker' if self.maker_mode else 'signal'} "
            f"quote_sides={self.maker_quote_sides} "
            f"pricer={self.maker_fair_pricer_mode} "
            f"bi_side={'on' if self.bi_side_enabled else 'off'} "
            f"post_only={'on' if self.maker_use_post_only else 'off'} "
            f"auto_tune={'on' if self.auto_tune_enabled else 'off'} "
            f"auto_redeem={'on' if self.auto_redeem_enabled else 'off'} "
            f"trade_db={'on' if self.trade_db_enabled else 'off'}"
        )
        logger.info(
            "Risk/ops: "
            f"max_order_usdc={float(self.maker_max_order_usdc):.2f} "
            f"reduce_only_cutoff_min={self.maker_min_minutes_to_close:.1f} "
            f"watchdog_stale={self.quote_stale_sec}s "
            f"requote_min_age={self.maker_requote_min_age_sec:.1f}s"
        )
        if self.startup_verbose:
            logger.info(
                "Verbose config: "
                f"fee_interval={self.fee_rate_fetch_interval_sec}s "
                f"balance_guard={self.conditional_balance_check_interval_sec}s/"
                f"{float(self.conditional_balance_safety_buffer_pct)*100:.2f}% "
                f"taker_exit={'on' if self.taker_exit_enabled else 'off'} "
                f"taker_max_spread={float(self.taker_exit_max_spread_pct):.3f} "
                f"early_sell_only={self.maker_early_sell_only_sec}s "
                f"dir_edge_gate={'on' if self.maker_directional_edge_gate_enabled else 'off'} "
                f"dir_edge_min={float(self.maker_min_directional_edge_ps):.4f} "
                f"regime_guard={'on' if self.regime_guard_enabled else 'off'}"
            )

    def _max_inventory_avg_entry(self) -> Decimal:
        return InventoryLedger.max_avg_entry(self.live_inventory_cost)

    def _clear_profit_run_state(self, instrument_id: Any) -> None:
        inst_key = self._instrument_key(instrument_id)
        if not inst_key:
            return
        self.maker_profit_run_peak_bid_by_inst.pop(inst_key, None)
        self.maker_profit_run_peak_fair_by_inst.pop(inst_key, None)

    def _update_profit_run_peaks(
        self,
        instrument_id: Any,
        *,
        best_bid: Optional[Decimal],
        fair: Optional[Decimal],
    ) -> None:
        inst_key = self._instrument_key(instrument_id)
        if not inst_key:
            return
        state = self.live_inventory_cost.get(inst_key)
        if not state:
            self._clear_profit_run_state(instrument_id)
            return
        try:
            qty = Decimal(str(state.get("qty", "0")))
        except Exception:
            qty = Decimal("0")
        if qty <= 0:
            self._clear_profit_run_state(instrument_id)
            return
        if best_bid is not None and best_bid > 0:
            prev_peak_bid = self.maker_profit_run_peak_bid_by_inst.get(inst_key)
            if prev_peak_bid is None or best_bid > prev_peak_bid:
                self.maker_profit_run_peak_bid_by_inst[inst_key] = best_bid
        if fair is not None and fair > 0:
            prev_peak_fair = self.maker_profit_run_peak_fair_by_inst.get(inst_key)
            if prev_peak_fair is None or fair > prev_peak_fair:
                self.maker_profit_run_peak_fair_by_inst[inst_key] = fair

    def _should_hold_profitable_position(
        self,
        *,
        instrument_id: Any,
        best_bid: Decimal,
        fair: Optional[Decimal],
        avg_entry: Decimal,
        time_left_sec: Optional[float],
        thesis_weakened: bool,
        offside_confirmed: bool,
    ) -> tuple[bool, str]:
        if not self.maker_profit_run_enabled:
            return False, ""
        inst_key = self._instrument_key(instrument_id)
        if not inst_key or avg_entry <= 0 or best_bid <= 0:
            return False, ""
        state = self.live_inventory_cost.get(inst_key)
        if not state:
            return False, ""
        try:
            qty = Decimal(str(state.get("qty", "0")))
        except Exception:
            qty = Decimal("0")
        if qty <= 0:
            return False, ""
        if offside_confirmed or thesis_weakened:
            return False, ""
        if not self.active_side_locked or self.active_side == ActiveSide.NONE:
            return False, ""
        if self._instrument_for_side(self.active_side) != instrument_id:
            return False, ""
        if abs(self.side_decision_score) < self.maker_profit_run_min_score_abs:
            return False, ""
        if self.exit_policy.stage(time_left_sec).value != "PASSIVE":
            return False, ""
        peak_bid = self.maker_profit_run_peak_bid_by_inst.get(inst_key, best_bid)
        peak_fair = self.maker_profit_run_peak_fair_by_inst.get(inst_key, fair or best_bid)
        peak_profit_ps = max(peak_bid - avg_entry, peak_fair - avg_entry)
        if peak_profit_ps < self.maker_profit_run_min_profit_ps:
            return False, ""
        unlock_active = (
            self.maker_profit_run_unlock_profit_ps > 0
            and peak_profit_ps >= self.maker_profit_run_unlock_profit_ps
        )
        trailing_drawdown_ps = self.maker_profit_run_trailing_drawdown_ps
        if (
            unlock_active
            and self.maker_profit_run_unlock_trailing_drawdown_ps > 0
        ):
            trailing_drawdown_ps = min(
                trailing_drawdown_ps,
                self.maker_profit_run_unlock_trailing_drawdown_ps,
            )
        hold_sec = 0.0
        try:
            opened_ts = float(state.get("opened_ts", 0.0))
            if opened_ts > 0:
                hold_sec = max(0.0, time.time() - opened_ts)
        except Exception:
            hold_sec = 0.0
        drawdown_bid = max(Decimal("0"), peak_bid - best_bid)
        fair_now = fair if fair is not None else peak_fair
        drawdown_fair = max(Decimal("0"), peak_fair - fair_now)
        if hold_sec < float(self.maker_profit_run_min_hold_sec) and not unlock_active:
            return True, (
                f"profit_run_hold hold={hold_sec:.1f}s<{self.maker_profit_run_min_hold_sec}s "
                f"peak_profit={float(peak_profit_ps):.4f}"
            )
        if (
            drawdown_bid < trailing_drawdown_ps
            and drawdown_fair < trailing_drawdown_ps
        ):
            reason_prefix = "profit_run_hold_unlocked" if unlock_active else "profit_run_hold"
            return True, (
                f"{reason_prefix} drawdown_bid={float(drawdown_bid):.4f} "
                f"drawdown_fair={float(drawdown_fair):.4f} "
                f"< trail={float(trailing_drawdown_ps):.4f} "
                f"peak_profit={float(peak_profit_ps):.4f}"
            )
        return False, ""



    def _is_emergency_exit_window(self, time_left_sec: Optional[float]) -> bool:
        if time_left_sec is None:
            return False
        if self.maker_sell_cost_protect_emergency_last_sec <= 0:
            return False
        return time_left_sec <= float(self.maker_sell_cost_protect_emergency_last_sec)

    def _normalize_active_side(self, side: Any) -> ActiveSide:
        txt = str(side or "").strip().upper()
        if txt == ActiveSide.UP.value:
            return ActiveSide.UP
        if txt == ActiveSide.DOWN.value:
            return ActiveSide.DOWN
        return ActiveSide.NONE

    def _primary_instrument_for_market(self) -> Optional[InstrumentId]:
        return self.current_up_instrument_id or self.current_down_instrument_id or self.instrument_id

    def _instrument_for_side(self, side: ActiveSide) -> Optional[InstrumentId]:
        if side == ActiveSide.UP:
            return self.current_up_instrument_id or self._primary_instrument_for_market()
        if side == ActiveSide.DOWN:
            return self.current_down_instrument_id
        return None

    def _side_for_instrument_id(self, instrument_id: Optional[Any]) -> ActiveSide:
        inst = self._normalize_instrument_id(instrument_id)
        if inst is None:
            return ActiveSide.NONE
        if self.current_up_instrument_id is not None and inst == self.current_up_instrument_id:
            return ActiveSide.UP
        if self.current_down_instrument_id is not None and inst == self.current_down_instrument_id:
            return ActiveSide.DOWN
        return ActiveSide.NONE

    def _sync_active_instrument(self) -> None:
        target = self._instrument_for_side(self.active_side)
        if target is None:
            target = self._primary_instrument_for_market()
        self.instrument_id = target
        self.current_token_id = self._extract_token_id_from_instrument(str(target)) if target is not None else None

    def _capture_market_open_spot(self) -> Optional[Decimal]:
        # Side-decision needs the freshest possible BTC spot. If we prefer the
        # cached external spot first, it can freeze at the market-open value when
        # fair-pricer updates stall, which in turn keeps strike/open-drift signals
        # pinned at zero.
        if self._binance_ws_price is not None and self._binance_ws_price > 0:
            ws_age = time.time() - float(self._binance_ws_price_ts or 0.0)
            if ws_age < 10.0:
                return self._binance_ws_price
        spot = self.latest_external_spot or self.last_external_spot
        if spot is not None and spot > 0:
            return spot
        if self._binance_ws_price is not None and self._binance_ws_price > 0:
            return self._binance_ws_price
        if self.external_spot_history:
            _, hist_px = self.external_spot_history[-1]
            if hist_px > 0:
                return hist_px
        return None

    # Side decision methods extracted to bot/side_decision.py (SideDecisionMixin)

    def _db_strategy_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        if not self.trade_db:
            return
        payload_out: Dict[str, Any] = dict(payload or {})
        if self.current_market_slug and "slug" not in payload_out:
            payload_out["slug"] = self.current_market_slug
        if self.current_market_slug and "market_slug" not in payload_out:
            payload_out["market_slug"] = self.current_market_slug
        if self.instrument_id and "instrument_id" not in payload_out:
            payload_out["instrument_id"] = str(self.instrument_id)
        self.trade_db.log_strategy_event(
            run_id=self.run_id,
            event_type=event_type,
            payload=payload_out,
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
        payload_out: Dict[str, Any] = dict(payload or {})
        if self.current_market_slug and "slug" not in payload_out:
            payload_out["slug"] = self.current_market_slug
        if self.current_market_slug and "market_slug" not in payload_out:
            payload_out["market_slug"] = self.current_market_slug
        if self.instrument_id and "instrument_id" not in payload_out:
            payload_out["instrument_id"] = str(self.instrument_id)

        side_out = side
        if side_out:
            side_norm = self._normalize_side_text(side_out)
            if side_norm:
                side_out = side_norm.upper()
        self.trade_db.log_order_event(
            run_id=self.run_id,
            event_type=event_type,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            side=side_out,
            price=price,
            qty=qty,
            status=status,
            reason=reason,
            instrument_id=str(self.instrument_id) if self.instrument_id else None,
            token_id=self.current_token_id,
            fee_rate_bps=self.last_observed_fee_rate_bps,
            expected_net_usdc=expected_net_usdc,
            commission_usdc=commission_usdc,
            payload=payload_out,
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
            if self.terminal_dashboard:
                self.terminal_dashboard.record_position_closed(
                    realized_pnl=realized_pnl,
                    total_trades=self._live_total_trades,
                    win_rate=win_rate,
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

    def _update_terminal_dashboard_snapshot(self) -> None:
        if not self.terminal_dashboard:
            return
        try:
            self.terminal_dashboard.update(
                phase=self.market_phase.value,
                slug=self.current_market_slug or self.selected_slug or "-",
                active_side=self.active_side.value,
                inventory_shares=float(self.inventory_delta_shares),
                wallet_balance_usdc=(
                    float(self._cached_usdc_balance) if self._cached_usdc_balance is not None else None
                ),
                active_orders=len(self.active_maker_orders),
            )
        except Exception as e:
            logger.debug(f"Failed to update terminal dashboard snapshot: {e}")

    def _start_terminal_dashboard_sync(self) -> None:
        if not self.terminal_dashboard:
            return
        try:
            self.terminal_dashboard.start()
            while not self._terminal_dashboard_stop_event.wait(self.terminal_dashboard_refresh_sec):
                self._refresh_balance_cache()
                self._update_terminal_dashboard_snapshot()
        except Exception as e:
            logger.error(f"Failed to start terminal dashboard: {e}")
    
    def _is_dry_run_mode(self) -> bool:
        """
        Test mode remains a safety rail, but simulation execution paths are removed.
        """
        return bool(self.test_mode)

    # ------------------------------------------------------------------
    # Binance WebSocket for real-time BTC price
    # ------------------------------------------------------------------

    # Spot pricer methods extracted to bot/spot_pricer.py (SpotPricerMixin)

    @staticmethod
    def _extract_market_start_ts_from_slug(slug: str) -> Optional[int]:
        return extract_market_start_ts_from_slug(slug)

    @staticmethod
    def _extract_price_to_beat_from_market_payload(market: Dict[str, Any]) -> Optional[Decimal]:
        return extract_price_to_beat_from_market_payload(market)

    @staticmethod
    def _resolve_btc_15m_market_slugs() -> List[str]:
        return resolve_btc_15m_market_slugs()
            
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

    @staticmethod
    def _reason_family(reason: str) -> str:
        """
        Collapse verbose diagnostic strings into stable reason families.
        This avoids state-machine jitter when numeric diagnostics change every tick.
        """
        r = str(reason or "")
        if r.startswith("econ_gate"):
            return "econ_gate"
        if r.startswith("reduce_only_tail_guard"):
            return "reduce_only_tail_guard"
        if r.startswith("reduce_only"):
            return "reduce_only"
        if r.startswith("side_disabled:momentum_buy_block") or r.startswith("side_disabled:momentum_sell_block"):
            return "trend_protection"
        if r.startswith("balance_forced_sell_only"):
            return "balance_forced_sell_only"
        if r.startswith("regime_guard_sell_only"):
            return "balance_forced_sell_only"
        if r.startswith("sell_pause"):
            return "sell_pause"
        if r.startswith("sellable_below_min"):
            return "sellable_below_min"
        if r.startswith("side_disabled"):
            return "side_disabled"
        if r.startswith("risk:no_desired_quote"):
            return "no_desired_quote"
        return "risk"

    @staticmethod
    def _is_maker_fill_liquidity(liquidity_side: Any) -> bool:
        txt = str(liquidity_side or "").strip().upper()
        return txt in {"MAKER", "1"}

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
    def _extract_venue_balance_shares_from_reject(reason: str) -> Optional[Decimal]:
        txt = str(reason or "")
        m = re.search(r"balance:\s*([0-9]+)", txt)
        if not m:
            return None
        try:
            return Decimal(m.group(1)) / Decimal("1000000")
        except Exception:
            return None

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
    def _normalize_instrument_id(instrument_id: Any) -> Optional[InstrumentId]:
        if instrument_id is None:
            return None
        if isinstance(instrument_id, InstrumentId):
            return instrument_id
        try:
            return InstrumentId.from_str(str(instrument_id))
        except Exception:
            return None

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
        # Feed UP token mid into SignalEngine for market consensus signal
        up_inst = getattr(self, 'current_up_instrument_id', None)
        if (
            up_inst is not None
            and self._normalize_instrument_id(instrument_id) == up_inst
            and hasattr(self, '_signal_engine')
        ):
            import time as _time
            self._signal_engine.update_market_mid(mid_price, _time.time())

    def _activate_maker_kill_switch(self, reason: str) -> None:
        self.maker_kill_switch = True
        self._cancel_active_maker_orders()
        logger.error(f"MAKER KILL SWITCH ACTIVATED: {reason}")

    def _reset_maker_state_for_new_market(
        self,
        prev_instrument_id: Optional[str],
        new_instrument_id: Optional[str],
        *,
        previous_slug: str = "",
        current_slug: str = "",
    ) -> None:
        """
        Per-market maker state reset.
        Inventory and kill-switch are strategy-local controls and should not carry across 15m markets.
        When the slug is unchanged (same-market rollover / side flip), preserve inventory
        tracking so SELL quotes are not incorrectly blocked.
        """
        if prev_instrument_id == new_instrument_id:
            return
        same_slug = bool(previous_slug and current_slug and previous_slug == current_slug)
        self._cancel_active_maker_orders()
        if same_slug:
            logger.info(
                f"Same-slug rollover: preserving inventory_delta_shares="
                f"{float(self.inventory_delta_shares):.6f} and live_inventory_cost "
                f"(slug={current_slug}, inst {prev_instrument_id} -> {new_instrument_id})"
            )
        else:
            self.inventory_delta_shares = Decimal("0")
            self.live_inventory_cost.clear()
            self._startup_rehydrated_inventory_force_sell_only = False
        self.market_cycle_realized_net_usdc = Decimal("0")
        self.pending_taker_exit_by_inst.clear()
        self.taker_exit_tail_attempted_by_inst.clear()
        self.taker_exit_last_eval_ts_by_inst.clear()
        self.taker_exit_reject_cooldown_until_by_inst.clear()
        self.taker_exit_stop_loss_hits_by_inst.clear()
        self.stop_loss_reentry_pause_until_by_inst.clear()
        self.side_stop_loss_penalty_until_by_market_side.clear()
        self.market_stop_loss_count_by_slug.clear()
        self.market_buy_count_by_slug.clear()
        self.market_buy_counted_order_ids_by_slug.clear()
        self.taker_exit_reason_by_client_order_id.clear()
        self._taker_exit_skip_log_ts_by_key.clear()
        self.high_cost_exit_cooldown_until_by_inst.clear()
        self.high_cost_last_fill_price_by_inst.clear()
        self._sell_reject_pause_until_by_inst.clear()
        self._conditional_balance_cache_by_token.clear()
        self.latest_quote_depth_by_inst.clear()
        self.maker_profit_run_peak_bid_by_inst.clear()
        self.maker_profit_run_peak_fair_by_inst.clear()
        self.recent_buy_fill_ts_by_inst.clear()
        self.orderbook_levels_cache_by_token.clear()
        if self.maker_kill_switch and self.maker_kill_switch_reset_on_rollover:
            self.maker_kill_switch = False
            logger.warning("Maker kill switch auto-reset on market rollover.")
        self.last_quote_update_ts = 0.0
        logger.info(f"Reset maker per-market state: {prev_instrument_id} -> {new_instrument_id} (same_slug={same_slug})")

    def _project_inventory_after_fill(self, side: str, qty: Decimal, instrument_id: Optional[Any] = None) -> Decimal:
        inst_id = instrument_id if instrument_id is not None else self.instrument_id
        projected = self.inventory_delta_shares
        if side.lower() == "sell":
            projected = self._get_confirmed_inventory_qty_for_instrument(inst_id)
        
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
        instruments: List[InstrumentId] = []
        seen: Set[str] = set()

        def _append(inst: Optional[InstrumentId]) -> None:
            if inst is None:
                return
            key = self._instrument_key(inst)
            if not key or key in seen:
                return
            instruments.append(inst)
            seen.add(key)

        if self.bi_side_enabled:
            active_inst = self._instrument_for_side(self.active_side)
            if self.active_side != ActiveSide.NONE and active_inst is not None:
                _append(active_inst)
        elif self.instrument_id is not None:
            _append(self.instrument_id)

        # Always include legs with confirmed inventory so they remain in the
        # normal maker requote loop even after active-side flips.
        for inst_key, state in list(self.live_inventory_cost.items()):
            try:
                qty = Decimal(str(state.get("qty", "0")))
            except Exception:
                qty = Decimal("0")
            if qty <= 0:
                continue
            inst = self._normalize_instrument_id(inst_key)
            if inst is not None:
                _append(inst)

        # Preserve instruments that recently failed SELL due to venue balance lag.
        for inst_key in list(self._sell_recovery_required_by_inst.keys()):
            inst = self._normalize_instrument_id(inst_key)
            if inst is not None:
                _append(inst)

        return instruments

    async def _quote_maker_orders(self, bid_price: Decimal, ask_price: Decimal) -> None:
        """
        Place symmetric maker quotes if expected net economics is positive.
        """
        if self.maker_kill_switch:
            return

        if time.time() < self.quote_pause_until_ts:
            return

        # --- Market Lifecycle Gate ---
        phase = self._update_market_phase()
        if phase in (MarketPhase.WAITING, MarketPhase.SETTLING):
            self._cancel_active_maker_orders()
            return
        await self._maybe_finalize_side_decision(time.time(), phase)
        if self.bi_side_enabled and self.active_side == ActiveSide.NONE:
            if self.inventory_delta_shares <= 0:
                self._cancel_active_maker_orders()
                return

        # --- Balance Pre-check (live only) ---
        _balance_forced_sell_only = False
        _regime_guard_active = False
        if not self._is_dry_run_mode():
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
        if self.regime_guard_enabled:
            now_guard_ts = time.time()
            if self.regime_guard_conservative_until_ts > 0 and now_guard_ts >= self.regime_guard_conservative_until_ts:
                self.regime_guard_conservative_until_ts = 0.0
                self._db_strategy_event("REGIME_GUARD_RECOVERED", {"ts": now_guard_ts})
            elif now_guard_ts < self.regime_guard_conservative_until_ts:
                _regime_guard_active = True
        if self.inventory_delta_shares <= 0 and self._startup_rehydrated_inventory_force_sell_only:
            self._startup_rehydrated_inventory_force_sell_only = False
        _forced_sell_only = _balance_forced_sell_only or self._startup_rehydrated_inventory_force_sell_only

        # Check whether current inventory should be force-closed via taker orders.
        await self._maybe_taker_exit_positions(time.time(), is_simulation=self._is_dry_run_mode())
        # Maker-style urgent exit: thesis weakened → place SELL at best_bid (zero taker fee)
        await self._maybe_maker_urgent_exit(time.time())

        now_ts = time.time()
        force_quote_refresh_once = bool(getattr(self, "_force_quote_refresh_once", False))
        if not force_quote_refresh_once and now_ts - self.last_quote_update_ts < self.quote_refresh_sec:
            return
        if force_quote_refresh_once:
            logger.info(
                "Fast requote triggered after locked side change: "
                f"reason={getattr(self, '_force_quote_refresh_reason', 'locked_side_change')}"
            )
            self._force_quote_refresh_once = False
            self._force_quote_refresh_reason = ""
        self.last_quote_update_ts = now_ts
        self._maybe_auto_tune(now_ts)
        self._cleanup_stale_pending_cancels(now_ts)

        # Cancel stale quotes by TTL before computing new target quotes.
        for order_key, state in list(self.active_maker_orders.items()):
            created_ts = float(state.get("created_ts", 0.0))
            # Urgent exit orders use their own (shorter) TTL
            if state.get("is_urgent_exit"):
                ttl = float(state.get("urgent_exit_ttl", self.maker_order_ttl_sec))
            else:
                ttl = self.maker_order_ttl_sec
            if created_ts <= 0 or (now_ts - created_ts) >= ttl:
                side = str(state.get("side", "") or "")
                is_urgent = " (urgent_exit)" if state.get("is_urgent_exit") else ""
                logger.info(f"Maker order [{side}]{is_urgent} exceeded TTL={ttl}s, cancel and requote.")
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
            if self.bi_side_enabled:
                self._cancel_active_maker_orders()
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
        diag_context_by_inst: Dict[str, Dict[str, Any]] = {}
        submitted_attempts = 0
        end_ts = getattr(self, "current_market_end_timestamp", None)
        time_left_sec_global = (end_ts - now_ts) if end_ts is not None else None

        for inst_id in target_instruments:
            quote_ctx = await build_quote_instrument_context(
                inst_id=inst_id,
                normalize_instrument_id_fn=self._normalize_instrument_id,
                instrument_key_fn=self._instrument_key,
                get_quote_for_instrument_fn=self._get_quote_for_instrument,
                compute_fair_probability_fn=self._compute_fair_probability,
                cache_instrument_fn=self.cache.instrument,
                extract_token_id_fn=self._extract_token_id_from_instrument,
                get_dynamic_fee_rate_fn=self._get_dynamic_fee_rate,
                get_orderbook_levels_fn=self._get_orderbook_levels_for_token,
                latest_quote_depth_by_inst=self.latest_quote_depth_by_inst,
                maker_econ_fee_rate_decimal=self.maker_econ_fee_rate_decimal,
            )
            inst_key = quote_ctx.inst_key
            diag_context_by_inst[inst_key] = quote_ctx.diag_context
            if quote_ctx.quote is None or quote_ctx.fair is None:
                continue
            inst_bid, inst_ask = quote_ctx.quote
            fair = quote_ctx.fair
            self.rebate_reporter.record_api_health(self.fee_rate_client.get_health_snapshot())
            if quote_ctx.dynamic_fee_rate is not None:
                pass

            side_plan = self.maker_engine.generate_quote_plan(
                inst_bid=inst_bid,
                inst_ask=inst_ask,
                fair_price=fair,
                fee_rate=quote_ctx.fee_rate_val,
                inventory_delta_shares=self.inventory_delta_shares,
                inventory_last_update_ts=self.inventory_last_update_ts,
                current_time_ts=now_ts,
                tick_size=quote_ctx.tick,
                recent_vol=recent_vol,
                balance_forced_sell_only=_forced_sell_only,
                bid_depth=quote_ctx.bid_depth,
                ask_depth=quote_ctx.ask_depth,
                bid_levels=quote_ctx.bid_levels,
                ask_levels=quote_ctx.ask_levels,
            )
            if not side_plan:
                diag_context_by_inst[inst_key]["reason"] = "invalid_quote_plan"
                continue

            momentum_history = self._momentum_history_for_instrument(inst_id)
            guard_outcome = apply_quote_plan_guards(
                side_plan=side_plan,
                quote_mode=self.maker_quote_sides,
                phase_value=phase.value,
                inventory_delta_shares=self.inventory_delta_shares,
                early_sell_only_sec=float(self.maker_early_sell_only_sec),
                time_left_sec_global=time_left_sec_global,
                directional_edge_gate_enabled=self.maker_directional_edge_gate_enabled,
                regime_guard_active=_regime_guard_active,
                min_directional_edge_ps=self.maker_min_directional_edge_ps,
                min_directional_edge_ps_conservative=self.maker_min_directional_edge_ps_conservative,
                now_ts=now_ts,
                buy_cooldown_until_ts=float(self.buy_cooldown_until_ts),
                momentum_filter_pct=self.maker_momentum_filter_pct,
                momentum_window_ticks=self.maker_momentum_window_ticks,
                momentum_history=momentum_history,
                fair=fair,
                min_fair_price=self.maker_min_fair_price,
                max_fair_price=self.maker_max_fair_price,
                end_ts=end_ts,
                min_minutes_to_close=self.maker_min_minutes_to_close,
                reduce_only_no_new_sell_last_sec=self.maker_reduce_only_no_new_sell_last_sec,
                forced_sell_only=_forced_sell_only,
                active_side=self.active_side.value,
                min_directional_edge_ps_down=self.maker_min_directional_edge_ps_down,
            )
            side_disable_reason_by_side = guard_outcome.side_disable_reason_by_side
            reduce_only_reason = guard_outcome.reduce_only.reason
            reduce_only_tail_sell_block = guard_outcome.reduce_only.tail_sell_block
            reduce_only_tail_sec_left = guard_outcome.reduce_only.tail_sec_left

            if guard_outcome.buy_cooldown_remaining is not None:
                remaining = guard_outcome.buy_cooldown_remaining
                if not getattr(self, "_logged_buy_cd", False) or time.time() - getattr(self, "_last_buy_cd_log_ts", 0) > 30:
                    logger.info(f"Post-fill buy cooldown active: {remaining:.1f}s remaining")
                    self._logged_buy_cd = True
                    self._last_buy_cd_log_ts = time.time()
            elif getattr(self, "_logged_buy_cd", False):
                self._logged_buy_cd = False
                logger.info("Post-fill buy cooldown cleared.")



            if guard_outcome.momentum_buy_blocked and guard_outcome.momentum_trend_pct is not None:
                if not getattr(self, "_logged_mom_buy", False) or time.time() - getattr(self, "_last_mom_ts", 0) > 30:
                    logger.warning(
                        f"Trend Protection: momentum filter (dropped {float(guard_outcome.momentum_trend_pct * 100):.1f}%). Blocking BUY orders."
                    )
                    self._logged_mom_buy = True
                    self._last_mom_ts = time.time()
            elif "buy" in side_plan and getattr(self, "_logged_mom_buy", False):
                self._logged_mom_buy = False

            if guard_outcome.momentum_sell_blocked and guard_outcome.momentum_trend_pct is not None:
                if not getattr(self, "_logged_mom_sell", False) or time.time() - getattr(self, "_last_mom_ts_s", 0) > 30:
                    logger.warning(
                        f"Trend Protection: momentum filter (pumped {float(guard_outcome.momentum_trend_pct * 100):.1f}%). Blocking SELL orders."
                    )
                    self._logged_mom_sell = True
                    self._last_mom_ts_s = time.time()
            elif "sell" in side_plan and getattr(self, "_logged_mom_sell", False):
                self._logged_mom_sell = False

            if reduce_only_reason:
                if "buy" in side_plan:
                    if not getattr(self, "_logged_reduce_only", False) or time.time() - getattr(self, "_last_ro_log_ts", 0) > 60:
                        logger.warning(f"Maker Reduce-Only active ({reduce_only_reason}). Blocking BUY orders.")
                        self._logged_reduce_only = True
                        self._last_ro_log_ts = time.time()
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
            elif "buy" in side_plan and getattr(self, "_logged_reduce_only", False):
                self._logged_reduce_only = False
                self._logged_extreme_sell_block = False
                self._logged_reduce_only_tail_sell_block = False

            for side, quote_data in side_plan.items():
                order_key = self._order_key_for(side, inst_id)
                inst_key = self._instrument_key(inst_id)
                current_slug = str(self.current_market_slug or "")
                market_stop_loss_count = int(self.market_stop_loss_count_by_slug.get(current_slug, 0))
                market_buy_count = int(self.market_buy_count_by_slug.get(current_slug, 0))
                if (
                    side == "buy"
                    and current_slug
                    and self.market_stop_loss_max_per_market > 0
                    and market_stop_loss_count >= self.market_stop_loss_max_per_market
                ):
                    self._db_order_event(
                        event_type="ORDER_SKIP_MARKET_STOP_LOSS_LIMIT",
                        side=side.upper(),
                        status="SKIPPED",
                        reason="market_stop_loss_limit",
                        payload={
                            "slug": current_slug,
                            "instrument_id": str(inst_id),
                            "market_stop_loss_count": market_stop_loss_count,
                            "market_stop_loss_max_per_market": self.market_stop_loss_max_per_market,
                        },
                    )
                    continue
                if (
                    side == "buy"
                    and current_slug
                    and self.market_max_buy_events_per_market > 0
                    and market_buy_count >= self.market_max_buy_events_per_market
                ):
                    self._db_order_event(
                        event_type="ORDER_SKIP_MARKET_BUY_LIMIT",
                        side=side.upper(),
                        status="SKIPPED",
                        reason="market_buy_limit",
                        payload={
                            "slug": current_slug,
                            "instrument_id": str(inst_id),
                            "market_buy_count": market_buy_count,
                            "market_max_buy_events_per_market": self.market_max_buy_events_per_market,
                        },
                    )
                    continue
                if inst_key in self.pending_taker_exit_by_inst:
                    self._db_order_event(
                        event_type="ORDER_SKIP_PENDING_TAKER_EXIT",
                        side=side.upper(),
                        status="SKIPPED",
                        reason="pending_taker_exit",
                        payload={
                            "instrument_id": str(inst_id),
                            "pending_taker_exit_client_order_id": self.pending_taker_exit_by_inst.get(inst_key),
                        },
                    )
                    continue
                sell_pause_until = float(self._sell_reject_pause_until_by_inst.get(inst_key, 0.0))
                sellable_qty = None
                confirmed_inventory_qty = Decimal("0")
                other_held_inventory_qty = Decimal("0")
                if side == "sell":
                    confirmed_inventory_qty = self._get_confirmed_inventory_qty_for_instrument(inst_id)
                    for held_key, held_state in list(self.live_inventory_cost.items()):
                        if held_key == inst_key:
                            continue
                        try:
                            held_qty = Decimal(str(held_state.get("qty", "0")))
                        except Exception:
                            held_qty = Decimal("0")
                        if held_qty > 0:
                            other_held_inventory_qty += held_qty
                    recent_buy_ts = float(self.recent_buy_fill_ts_by_inst.get(inst_key, 0.0))
                    if recent_buy_ts > 0 and self.sell_delay_after_buy_sec > 0:
                        sell_pause_until = max(
                            sell_pause_until,
                            recent_buy_ts + float(self.sell_delay_after_buy_sec),
                        )
                if side == "sell" and not self._is_dry_run_mode():
                    sellable_qty = self._get_effective_sellable_qty(instrument_id=inst_id)
                inv_state = self.live_inventory_cost.get(inst_key) if inst_key else None
                avg_entry = (
                    Decimal(str(inv_state.get("avg_entry_price", "0")))
                    if inv_state is not None
                    else Decimal("0")
                )
                current_inst_inventory_qty = (
                    Decimal(str(inv_state.get("qty", "0")))
                    if inv_state is not None
                    else Decimal("0")
                )
                min_expected_net_usdc = self.maker_min_expected_net_usdc
                if (
                    side == "buy"
                    and abs(self.side_decision_score) < self.directional_entry_min_score_abs
                ):
                    self._db_order_event(
                        event_type="ORDER_SKIP_DIRECTIONAL_ENTRY_GATE",
                        side=side.upper(),
                        status="SKIPPED",
                        reason="directional_entry_gate",
                        payload={
                            "slug": current_slug,
                            "instrument_id": str(inst_id),
                            "side_score": float(self.side_decision_score),
                            "required_score_abs": float(self.directional_entry_min_score_abs),
                        },
                    )
                    continue
                if (
                    side == "buy"
                    and self.maker_reload_min_expected_net_multiplier > Decimal("1")
                    and current_inst_inventory_qty + Decimal("0.000001")
                    >= self.maker_reload_inventory_threshold_shares
                ):
                    min_expected_net_usdc = (
                        self.maker_min_expected_net_usdc
                        * self.maker_reload_min_expected_net_multiplier
                    )
                # Determine if the directional thesis has weakened against our position.
                # Loss-selling should be allowed more aggressively when we are confirmed
                # offside against a locked side decision, even if cost-protect would
                # normally block the new SELL price.
                _thesis_weakened = False
                _offside_confirmed = False
                if (
                    side == "sell"
                    and self.inventory_delta_shares > 0
                    and self.bi_side_enabled
                ):
                    target_active_inst = self._instrument_for_side(self.active_side)
                    if (
                        self.active_side_locked
                        and self.active_side != ActiveSide.NONE
                        and target_active_inst is not None
                        and target_active_inst != inst_id
                    ):
                        _offside_confirmed = True
                    score = float(self.side_decision_score)
                    if self.active_side == ActiveSide.UP and score < 0:
                        _thesis_weakened = True
                    elif self.active_side == ActiveSide.DOWN and score > 0:
                        _thesis_weakened = True
                    elif self.active_side != ActiveSide.NONE and abs(score) < 0.5:
                        _thesis_weakened = True
                    if quote_ctx.quote is not None:
                        self._update_profit_run_peaks(
                            inst_id,
                            best_bid=quote_ctx.quote[0],
                            fair=quote_ctx.fair,
                        )

                desired_entry = build_desired_quote_entry(
                    order_key=order_key,
                    side=side,
                    inst_id=inst_id,
                    quote_data=quote_data,
                    side_disable_reason_by_side=side_disable_reason_by_side,
                    reduce_only_reason=reduce_only_reason,
                    reduce_only_tail_sell_block=reduce_only_tail_sell_block,
                    reduce_only_no_new_sell_last_sec=self.maker_reduce_only_no_new_sell_last_sec,
                    forced_sell_only=_forced_sell_only,
                    min_expected_net_usdc=min_expected_net_usdc,
                    now_ts=now_ts,
                    sell_pause_until=sell_pause_until,
                    is_dry_run_mode=self._is_dry_run_mode(),
                    sellable_qty=sellable_qty,
                    maker_exchange_min_shares=self.maker_exchange_min_shares,
                    avg_entry=avg_entry,
                    emergency_window=self._is_emergency_exit_window(time_left_sec_global),
                    high_cost_exit_cooldown_enabled=self.maker_high_cost_exit_cooldown_enabled,
                    high_cost_exit_cooldown_sec=float(self.maker_high_cost_exit_cooldown_sec),
                    high_cost_exit_cooldown_until=float(self.high_cost_exit_cooldown_until_by_inst.get(inst_key, 0.0)),
                    maker_sell_cost_protect_enabled=self.maker_sell_cost_protect_enabled,
                    maker_sell_cost_protect_fee_buffer_ps=self.maker_sell_cost_protect_fee_buffer_ps,
                    maker_sell_min_profit_floor_ps=self.maker_sell_min_profit_floor_ps,
                    thesis_weakened=_thesis_weakened,
                    offside_confirmed=_offside_confirmed,
                    time_left_sec=time_left_sec_global,
                )
                desired_entry["dynamic_fee_rate"] = quote_ctx.dynamic_fee_rate
                desired_entry["min_expected_net_usdc"] = min_expected_net_usdc
                if side == "sell" and confirmed_inventory_qty <= 0:
                    desired_entry["should_quote"] = False
                    if other_held_inventory_qty > 0:
                        desired_entry["diag_reason"] = (
                            f"confirmed_inventory_zero_current_leg other_held={float(other_held_inventory_qty):.6f}"
                        )
                    else:
                        desired_entry["diag_reason"] = "confirmed_inventory_zero"

                # FIX: Protect profitable existing sell orders from being canceled.
                # When sell_cost_protect or high_cost_exit_cooldown blocks the NEW
                # calculated price, check whether the EXISTING order on the book is
                # already profitable. If so, keep the existing order alive instead of
                # canceling it and losing a good fill opportunity.
                if (
                    side == "sell"
                    and not desired_entry.get("should_quote", False)
                ):
                    diag = str(desired_entry.get("diag_reason", ""))
                    if "sell_cost_protect" in diag or "high_cost_exit_cooldown" in diag or "min_profit_floor" in diag:
                        existing_state = self.active_maker_orders.get(order_key)
                        if existing_state is not None:
                            existing_price = Decimal(str(existing_state.get("price", 0)))
                            cost_floor = avg_entry + self.maker_sell_cost_protect_fee_buffer_ps + self.maker_sell_min_profit_floor_ps
                            if existing_price >= cost_floor:
                                # Existing order is profitable — keep it alive.
                                desired_entry["should_quote"] = True
                                desired_entry["price"] = existing_price
                                desired_entry["diag_reason"] = (
                                    f"sell_preserved existing={float(existing_price):.4f} "
                                    f">= floor={float(cost_floor):.4f} "
                                    f"(new_blocked: {diag})"
                                )
                if (
                    side == "sell"
                    and desired_entry.get("should_quote", False)
                    and quote_ctx.quote is not None
                ):
                    hold_profit_run, hold_reason = self._should_hold_profitable_position(
                        instrument_id=inst_id,
                        best_bid=quote_ctx.quote[0],
                        fair=quote_ctx.fair,
                        avg_entry=avg_entry,
                        time_left_sec=time_left_sec_global,
                        thesis_weakened=_thesis_weakened,
                        offside_confirmed=_offside_confirmed,
                    )
                    if hold_profit_run:
                        desired_entry["should_quote"] = False
                        desired_entry["diag_reason"] = hold_reason

                if (
                    side == "buy"
                    and current_inst_inventory_qty + Decimal("0.000001")
                    >= self.maker_reload_inventory_threshold_shares
                ):
                    directional_edge_ps = desired_entry.get("directional_edge_ps")
                    required_reload_edge = self.maker_reload_min_directional_edge_ps
                    desired_entry["reload_min_directional_edge_ps"] = required_reload_edge
                    if (
                        isinstance(directional_edge_ps, Decimal)
                        and directional_edge_ps < required_reload_edge
                    ):
                        desired_entry["should_quote"] = False
                        desired_entry["diag_reason"] = (
                            f"reload_edge_gate directional_edge_ps={float(directional_edge_ps):.6f} "
                            f"< min={float(required_reload_edge):.6f}"
                        )
                desired_quotes[order_key] = desired_entry

        reconcile_unwanted_quotes(
            active_maker_orders=self.active_maker_orders,
            desired_quotes=desired_quotes,
            target_inst_set=target_inst_set,
            now_ts=now_ts,
            cancel_cooldown_sec=float(self.maker_cancel_cooldown_sec),
            gate_block_grace_sec=float(self.maker_gate_block_grace_sec),
            reason_family_fn=self._reason_family,
            cancel_order_fn=self._cancel_maker_order_side,
            gate_block_since_by_order_key=self._gate_block_since_by_order_key,
            gate_block_reason_by_order_key=self._gate_block_reason_by_order_key,
            gate_last_cancel_ts_by_order_key=self._gate_last_cancel_ts_by_order_key,
        )

        # Quote desired sides with selective requote.
        for order_key, desired in desired_quotes.items():
            if not bool(desired.get("should_quote", False)):
                continue
            submitted_attempts += 1
            side = str(desired["side"])
            inst_id = desired["instrument_id"]
            limit_price = Decimal(str(desired["price"]))
            econ = desired["econ"]
            dynamic_fee_rate = desired.get("dynamic_fee_rate")
            directional_snapshot = build_directional_snapshot(desired)

            # Use per-instrument tick size to build stable target versions.
            inst_for_tick = self._normalize_instrument_id(inst_id)
            inst_obj = self.cache.instrument(inst_for_tick) if inst_for_tick else None
            tick = extract_instrument_tick(inst_obj, default_tick="0.01")

            target_version = compute_requote_target_version(
                order_key=order_key,
                limit_price=limit_price,
                tick=tick,
                maker_requote_hysteresis_ticks=self.maker_requote_hysteresis_ticks,
                target_anchor_price_by_order_key=self._target_anchor_price_by_order_key,
                target_version_by_order_key=self._target_version_by_order_key,
            )

            current = self.active_maker_orders.get(order_key)
            if should_requote_existing_order(
                current=current,
                target_version=target_version,
                now_ts=now_ts,
                maker_requote_min_age_sec=float(self.maker_requote_min_age_sec),
                side=side,
                maker_requote_min_age_sec_sell=float(self.maker_requote_min_age_sec_sell),
            ):
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
            if current:
                continue

            await self._submit_maker_quote(
                inst_id,
                side,
                limit_price,
                econ,
                dynamic_fee_rate,
                directional_snapshot=directional_snapshot,
                target_version=target_version,
                loss_sell_reason=desired.get("loss_sell_reason", ""),
            )

        log_no_quote_diagnostics(
            submitted_attempts=submitted_attempts,
            target_instruments=target_instruments,
            desired_quotes=desired_quotes,
            diag_context_by_inst=diag_context_by_inst,
            now_ts=now_ts,
            no_quote_diag_interval_sec=float(self.no_quote_diag_interval_sec),
            phase_value=phase.value,
            instrument_key_fn=self._instrument_key,
            active_order_keys_fn=self._active_order_keys,
            last_no_quote_diag_ts_by_inst=self._last_no_quote_diag_ts_by_inst,
            logger_info_fn=logger.info,
            reason_family_fn=self._reason_family,
            strategy_event_fn=self._db_strategy_event,
        )

    async def _submit_maker_quote(
        self,
        instrument_id: Any,
        side: str,
        limit_price: Decimal,
        econ,
        dynamic_fee_rate: Optional[Decimal] = None,
        directional_snapshot: Optional[Dict[str, Any]] = None,
        target_version: Optional[int] = None,
        loss_sell_reason: str = "",
    ) -> None:
        instrument_id = self._normalize_instrument_id(instrument_id)
        instrument = self.cache.instrument(instrument_id) if instrument_id else None
        limit_price = self._align_price_to_tick(limit_price, side, instrument)
        expected_net_usdc = Decimal(str(getattr(econ, "expected_net_usdc", "0")))
        if expected_net_usdc <= Decimal("0"):
            self._db_order_event(
                event_type="ORDER_SKIP_EXPECTED_NET",
                side=side.upper(),
                price=float(limit_price),
                status="SKIPPED",
                reason="expected_net_non_positive",
                expected_net_usdc=float(expected_net_usdc),
            )
            return

        # Pre-submit clamp: avoid posting BUY quotes that cross best ask.
        # This removes structural crossing when spread is tight (e.g. 1 tick).
        quote_now = self._get_quote_for_instrument(instrument_id)
        if quote_now is not None and side == "buy":
            limit_price = retreat_crossing_buy_quote(
                limit_price=limit_price,
                instrument=instrument,
                quote_now=quote_now,
                align_price_fn=self._align_price_to_tick,
                logger_warning_fn=logger.warning,
                logger_info_fn=logger.info,
            )
            if limit_price is None:
                return

        precision = int(getattr(instrument, "size_precision", 6)) if instrument is not None else 6
        qty_dec = self._compute_maker_order_qty(limit_price, precision)
        if side == "buy":
            inst_key = self._instrument_key(instrument_id)
            reentry_pause_until = float(self.stop_loss_reentry_pause_until_by_inst.get(inst_key, 0.0))
            if time.time() < reentry_pause_until:
                cooldown_left = reentry_pause_until - time.time()
                logger.info(
                    "Skip maker BUY quote: stop-loss re-entry cooldown active "
                    f"(inst={inst_key}, cooldown_left={cooldown_left:.1f}s)"
                )
                self._db_order_event(
                    event_type="ORDER_SKIP_REENTRY_COOLDOWN",
                    side=side.upper(),
                    price=float(limit_price),
                    qty=float(qty_dec),
                    status="SKIPPED",
                    reason="stop_loss_reentry_cooldown",
                    payload={
                        "instrument_id": str(instrument_id),
                        "cooldown_left_sec": cooldown_left,
                    },
                )
                return

        # Live-only guard: prevent SELL submissions when we don't actually hold enough tokens.
        # If sellable is less than requested, REDUCE the qty to sellable amount before projected checks.
        if side == "sell" and not self._is_dry_run_mode():
            sellable_qty = self._get_effective_sellable_qty(instrument_id=instrument_id)
            confirmed_qty = self._get_confirmed_inventory_qty_for_instrument(instrument_id=instrument_id)
            inst_key = self._instrument_key(instrument_id)
            venue_cap = self._sell_recovery_venue_cap_by_inst.get(inst_key, None) if inst_key else None
            if venue_cap is not None and venue_cap > 0:
                sellable_qty = min(sellable_qty, venue_cap)
            adjusted_qty, sellable_guard_reason = apply_sellable_inventory_guard(
                qty_dec=qty_dec,
                precision=precision,
                sellable_qty=sellable_qty,
                maker_exchange_min_shares=self.maker_exchange_min_shares,
            )
            if adjusted_qty is None:
                inst_key = self._instrument_key(instrument_id)
                now_ts = time.time()
                last_skip = float(self._last_sellable_skip_log_ts_by_inst.get(inst_key, 0.0))
                if now_ts - last_skip >= float(self.no_quote_diag_interval_sec):
                    self._last_sellable_skip_log_ts_by_inst[inst_key] = now_ts
                    if sellable_guard_reason == "no_sellable_inventory":
                        logger.info(
                            "NO_QUOTE diagnostic: "
                            f"inst={inst_key} side=sell reason=no_sellable_inventory "
                            f"sellable={float(sellable_qty):.6f} confirmed={float(confirmed_qty):.6f} "
                            f"inventory={float(self.inventory_delta_shares):.6f}"
                        )
                    else:
                        logger.info(
                            "NO_QUOTE diagnostic: "
                            f"inst={inst_key} side=sell reason=sellable_below_min_after_reduce "
                            f"qty={float(qty_dec):.6f} min={float(self.maker_exchange_min_shares):.6f}"
                        )
                return
            if adjusted_qty + Decimal("0.000001") < qty_dec:
                old_qty = qty_dec
                qty_dec = adjusted_qty
                logger.info(
                    f"Maker SELL qty reduced to sellable amount: "
                    f"{float(old_qty):.6f} -> {float(qty_dec):.6f} "
                    f"(on-chain tokens after fees)"
                )
            else:
                qty_dec = adjusted_qty

        projected_inventory = self._project_inventory_after_fill(side, qty_dec, instrument_id=instrument_id)
        if side == "buy" and projected_inventory > self.maker_max_inventory_shares:
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
        if side == "sell" and projected_inventory < Decimal("0"):
            confirmed_qty = self._get_confirmed_inventory_qty_for_instrument(instrument_id=instrument_id)
            logger.info(
                "Skip maker quote: projected inventory would go negative "
                f"(side={side}, qty={float(qty_dec):.6f}, projected={float(projected_inventory):.6f}, "
                f"confirmed={float(confirmed_qty):.6f}, global={float(self.inventory_delta_shares):.6f})"
            )
            self._db_order_event(
                event_type="ORDER_SKIP_SELLABLE_PROJECTED",
                side=side.upper(),
                price=float(limit_price),
                qty=float(qty_dec),
                reason="projected_inventory_below_zero",
                payload={
                    "current_inventory": float(confirmed_qty),
                    "global_inventory": float(self.inventory_delta_shares),
                    "projected_inventory": float(projected_inventory),
                },
            )
            return

        if self._is_dry_run_mode():
            self._db_order_event(
                event_type="ORDER_DRY_RUN_SKIP",
                side=side.upper(),
                price=float(limit_price),
                qty=float(qty_dec),
                status="SKIPPED",
                reason="test_mode_dry_run",
                expected_net_usdc=float(econ.expected_net_usdc),
                payload={"instrument_id": str(instrument_id)},
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
        order, self.maker_use_post_only = build_limit_order(
            order_factory=self.order_factory,
            order_kwargs=order_kwargs,
            maker_use_post_only=self.maker_use_post_only,
            maker_post_only_strict=self.maker_post_only_strict,
            logger_error_fn=logger.error,
            logger_warning_fn=logger.warning,
        )
        if order is None:
            return

        # Final guard: maker quotes must remain non-crossing on both sides.
        # Emergency/tail taker exits use a separate market-order path.
        quote = self._get_quote_for_instrument(instrument_id)
        if violates_final_crossing_guard(
            side=side,
            limit_price=limit_price,
            quote=quote,
            maker_use_post_only=self.maker_use_post_only,
            maker_post_only_strict=getattr(self, "maker_post_only_strict", False),
            logger_warning_fn=logger.warning,
        ):
            return

        self.submit_order(order)
        self.consecutive_denied_orders = 0
        order_key = self._order_key_for(side, instrument_id)
        self.active_maker_orders[order_key] = build_active_maker_order_state(
            order=order,
            econ=econ,
            directional_snapshot=directional_snapshot,
            limit_price=limit_price,
            side=side,
            instrument_id=instrument_id,
            token_id=self._extract_token_id_from_instrument(str(instrument_id)),
            token_qty=token_qty,
            created_ts=time.time(),
            target_version=int(target_version or 0),
        )
        if side == "sell":
            inst_key = self._instrument_key(instrument_id)
            if inst_key:
                self._sell_recovery_required_by_inst.pop(inst_key, None)
                self._sell_recovery_reason_by_inst.pop(inst_key, None)
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
                "directional_edge_ps": (
                    float(directional_snapshot.get("directional_edge_ps"))
                    if directional_snapshot and directional_snapshot.get("directional_edge_ps") is not None
                    else None
                ),
                "directional_edge_usdc": (
                    float(directional_snapshot.get("directional_edge_usdc"))
                    if directional_snapshot and directional_snapshot.get("directional_edge_usdc") is not None
                    else None
                ),
                "p_fair": (
                    float(directional_snapshot.get("p_fair"))
                    if directional_snapshot and directional_snapshot.get("p_fair") is not None
                    else None
                ),
                "fee_ps": (
                    float(directional_snapshot.get("fee_ps"))
                    if directional_snapshot and directional_snapshot.get("fee_ps") is not None
                    else None
                ),
                "other_cost_ps": (
                    float(directional_snapshot.get("other_cost_ps"))
                    if directional_snapshot and directional_snapshot.get("other_cost_ps") is not None
                    else None
                ),
                "exec_penalty_usdc": (
                    float(directional_snapshot.get("exec_penalty_usdc"))
                    if directional_snapshot and directional_snapshot.get("exec_penalty_usdc") is not None
                    else None
                ),
                "robust_net_usdc": (
                    float(directional_snapshot.get("robust_net_usdc"))
                    if directional_snapshot and directional_snapshot.get("robust_net_usdc") is not None
                    else None
                ),
                "sell_recovery_required": (
                    bool(self._sell_recovery_required_by_inst.get(self._instrument_key(instrument_id), 0.0))
                    if side == "sell"
                    else False
                ),
                "loss_sell_reason": loss_sell_reason if side == "sell" and loss_sell_reason else None,
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
        logger.info("Strategy start sequence initiated.")
        self._log_strategy_config_summary()
        self.last_valid_quote_ts = time.time()
        self.consecutive_invalid_quote_ticks = 0
        
        # Find BTC instrument FIRST and wait for it
        if not self._wait_for_btc_instrument(timeout_sec=60, poll_interval_sec=2):
            raise RuntimeError("Startup check failed: no BTC 15-min instrument loaded")

        # Recover local inventory tracking if the strategy restarted mid-market.
        self._rehydrate_inventory_state_on_startup()

        log_strategy_run_start(
            trade_db=self.trade_db,
            run_id=self.run_id,
            is_dry_run_mode=self._is_dry_run_mode(),
            test_mode=self.test_mode,
            maker_mode=self.maker_mode,
            instrument_id=self.instrument_id,
            selected_slug=self.selected_slug,
            maker_quote_sides=self.maker_quote_sides,
            maker_quote_size_usdc=self.maker_quote_size_usdc,
        )
        self._db_strategy_event(
            "STRATEGY_START",
            {
                "instrument_id": str(self.instrument_id) if self.instrument_id else None,
                "selected_slug": self.selected_slug,
                "test_mode": self.test_mode,
                "bi_side_enabled": self.bi_side_enabled,
                "active_side": self.active_side.value,
                "git_revision": self.runtime_git_revision,
                "bi_side_require_confirming_signal": self.bi_side_require_confirming_signal,
                "maker_fixed_shares": float(self.maker_fixed_shares),
                "maker_max_order_usdc": float(self.maker_max_order_usdc),
                "directional_entry_min_score_abs": float(self.directional_entry_min_score_abs),
                "maker_urgent_exit_enabled": self.maker_urgent_exit_enabled,
                "maker_min_directional_edge_ps_down": float(self.maker_min_directional_edge_ps_down),
                "maker_reload_inventory_threshold_shares": float(self.maker_reload_inventory_threshold_shares),
                "maker_reload_min_expected_net_multiplier": float(self.maker_reload_min_expected_net_multiplier),
                "maker_reload_min_directional_edge_ps": float(self.maker_reload_min_directional_edge_ps),
                "taker_exit_eval_interval_sec": float(self.taker_exit_eval_interval_sec),
                "taker_exit_stop_loss_confirmations": int(self.taker_exit_stop_loss_confirmations),
                "taker_exit_stop_loss_usdc": float(self.taker_exit_stop_loss_usdc),
                "taker_exit_wait_for_sell_quote_sec": float(self.taker_exit_wait_for_sell_quote_sec),
                "market_stop_loss_max_per_market": int(self.market_stop_loss_max_per_market),
                "market_max_buy_events_per_market": int(self.market_max_buy_events_per_market),
                "stop_loss_reentry_cooldown_sec": int(self.stop_loss_reentry_cooldown_sec),
                "exit_stop_loss_requires_thesis_weakening": self.exit_stop_loss_requires_thesis_weakening,
                "exit_stop_loss_thesis_min_score_abs": float(self.exit_stop_loss_thesis_min_score_abs),
                "exit_conviction_band_min_price": float(self.exit_conviction_band_min_price),
                "exit_hold_band_min_price": float(self.exit_hold_band_min_price),
                "exit_conviction_band_min_score_abs": float(self.exit_conviction_band_min_score_abs),
                "exit_hold_band_min_score_abs": float(self.exit_hold_band_min_score_abs),
                "exit_conviction_stop_loss_multiplier": float(self.exit_conviction_stop_loss_multiplier),
                "exit_conviction_extra_confirmations": int(self.exit_conviction_extra_confirmations),
                "exit_hold_band_requires_locked": self.exit_hold_band_requires_locked,
            },
        )
        self._bootstrap_regime_guard_window_from_db()

        # Log which side-decision engine is active
        if self.bi_side_enabled:
            engine_label = "SignalEngine (probabilistic)" if self.side_decision_engine_new else "Legacy (integer voting)"
            logger.info(
                f"Side decision engine: {engine_label} | "
                f"min_confidence={self.side_signal_min_confidence} "
                f"threshold_up={self.side_signal_threshold_up} "
                f"threshold_down={self.side_signal_threshold_down} | "
                f"BTC EMA {self.side_signal_btc_ema_fast_sec}s/{self.side_signal_btc_ema_slow_sec}s | "
                f"Mid EMA {self.side_signal_mid_ema_fast_sec}s/{self.side_signal_mid_ema_slow_sec}s"
            )
        
        # Ensure we have sufficient history.
        if len(self.price_history) < 20:
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
            except Exception as e:
                logger.debug(f"Could not get real price: {e}")
                logger.debug("Using synthetic prices until real quotes arrive")
        
        # Start market lifecycle timer (replaces fixed 12-min reload)
        self._lifecycle_stop_event.clear()
        self._lifecycle_thread = start_background_thread(self._start_market_lifecycle_timer, "market-lifecycle")
        # Initialize live Prometheus trading metrics
        self._init_live_prom_metrics()
        # Start Binance WebSocket for real-time BTC price
        self._start_binance_ws()
        # Also start the legacy reload timer as a fallback
        self._reload_stop_event.clear()
        self._reload_thread = start_background_thread(self._start_reload_timer, "reload-timer")
        self._quote_watchdog_stop_event.clear()
        self._quote_watchdog_thread = start_background_thread(self._start_quote_watchdog_timer, "quote-watchdog")
        # Initialize phase based on current market
        self._update_market_phase()
        if self.auto_redeem_enabled:
            self._redeem_stop_event.clear()
            self._redeem_thread = start_background_thread(self._start_auto_redeem_timer, "auto-redeem")
            self._schedule_auto_redeem(reason="startup")
        self._balance_stop_event.clear()
        self._balance_thread = start_background_thread(self._start_balance_refresh_timer, "balance-refresh")
        try:
            self._refresh_balance_cache_sync()
        except Exception as e:
            logger.debug(f"Initial balance refresh failed: {e}")
        
        # Start Grafana if enabled
        if self.grafana_exporter:
            start_background_thread(self._start_grafana_sync, "grafana-sync")
        if self.terminal_dashboard:
            self._terminal_dashboard_stop_event.clear()
            self._terminal_dashboard_thread = start_background_thread(
                self._start_terminal_dashboard_sync,
                "terminal-dashboard",
            )
            self._update_terminal_dashboard_snapshot()

        logger.info(f"Strategy active. price_history_points={len(self.price_history)}")
        if len(self.price_history) >= 20:
            logger.info("Ready to trade at next interval.")
        else:
            logger.warning(f"Need more history ({len(self.price_history)}/20)")
                
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
        logger.info("Preloading price history...")
        
        # Get current instrument
        if not self.instrument_id:
            logger.warning("No instrument ID, skipping preload")
            return
        
        # Try to get current price from cache first
        quote = self.cache.quote_tick(self.instrument_id)
        if quote:
            current_price = (quote.bid_price + quote.ask_price) / 2
            self.price_history.append(current_price)
        
        # Try to get historical quotes from cache
        # Note: This depends on your data provider storing history
        quotes = self.cache.quote_tick(self.instrument_id)
        if quotes and len(quotes) > 0:
            for quote in quotes[-20:]:  # Take last 20 quotes
                mid_price = (quote.bid_price + quote.ask_price) / 2
                self.price_history.append(mid_price)
        
        self.price_history = dedupe_price_history(self.price_history)
        
        # If still not enough, generate synthetic data
        if len(self.price_history) < 20:
            logger.warning(f"Only {len(self.price_history)} historical quotes found, generating synthetic data to fill")
            self._generate_synthetic_history(existing_count=len(self.price_history))
        
        logger.info(f"Price history preload complete: points={len(self.price_history)}")
        if len(self.price_history) >= 20:
            logger.info("Price history is sufficient.")
        else:
            logger.warning("Still need more history - will collect from live data")
    
    def _generate_synthetic_history(self, target_count: int = 20, existing_count: int = 0):
        """Generate synthetic price history for testing/initialization."""
        needed = extend_synthetic_history(
            price_history=self.price_history,
            target_count=target_count,
            existing_count=existing_count,
        )
        if needed > 0:
            logger.info(f"Synthetic history added: +{needed} (total={len(self.price_history)})")
    
    def _start_reload_timer(self):
        """Start timer to reload instruments every 12 minutes."""
        while not self._reload_stop_event.wait(720):  # 12 minutes
            logger.info("Reloading instruments (timer)...")
            
            try:
                # Request instrument reload from data client
                instruments = self.cache.instruments()
                
                # Re-find BTC instrument (this will select the active one)
                previous_slug = self.current_market_slug
                if not self._find_btc_instrument():
                    logger.warning("Reload completed but no BTC 15-min instrument found")
                elif self.auto_redeem_enabled and self.auto_redeem_on_rollover and previous_slug and self.current_market_slug and previous_slug != self.current_market_slug:
                    self._schedule_auto_redeem(reason=f"market_rollover:{previous_slug}->{self.current_market_slug}")
                
                logger.info(f"Instruments reload complete. cached={len(instruments)}")
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
                skip_run, elapsed_since_last = should_skip_auto_redeem_run(
                    now_ts=now_ts,
                    auto_redeem_min_gap_sec=float(self.auto_redeem_min_gap_sec),
                    last_redeem_run_ts=float(self._last_redeem_run_ts),
                )
                if skip_run:
                    logger.info(
                        "Auto redeem skipped by min gap: "
                        f"reason={reason} elapsed={elapsed_since_last:.1f}s "
                        f"required={self.auto_redeem_min_gap_sec}s"
                    )
                    return
                self._last_redeem_run_ts = now_ts
                run_auto_redeem_script(
                    repo_root=Path(__file__).parent,
                    reason=reason,
                    auto_redeem_slug_filter=self.auto_redeem_slug_filter,
                    auto_redeem_apply=self.auto_redeem_apply,
                    auto_redeem_timeout_sec=int(self.auto_redeem_timeout_sec),
                    logger_info_fn=logger.info,
                    logger_warning_fn=logger.warning,
                    db_strategy_event_fn=self._db_strategy_event,
                )
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
                    old_delta, self.inventory_delta_shares = adjust_inventory_after_merge(
                        tokens=tokens,
                        deduct_qty=deduct_qty,
                        live_inventory_cost=self.live_inventory_cost,
                        inventory_delta_shares=self.inventory_delta_shares,
                        instrument_key_fn=self._instrument_key,
                    )
                    logger.info(
                        f"Deducted {float(deduct_qty):.6f} from live_inventory_cost "
                        f"and inventory_delta after merge. "
                        f"delta {float(old_delta):.6f} -> {float(self.inventory_delta_shares):.6f}"
                    )

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
        selected_ok, reload_ts, _stale_for, prev_instrument = handle_quote_watchdog_recovery(
            trigger=trigger,
            now_ts=now_ts,
            last_quote_watchdog_reload_ts=float(self.last_quote_watchdog_reload_ts),
            quote_reload_cooldown_sec=float(self.quote_reload_cooldown_sec),
            instrument_id=self.instrument_id,
            last_valid_quote_ts=float(self.last_valid_quote_ts),
            consecutive_invalid_quote_ticks=int(self.consecutive_invalid_quote_ticks),
            db_strategy_event_fn=self._db_strategy_event,
            cancel_active_maker_orders_fn=self._cancel_active_maker_orders,
            find_btc_instrument_fn=self._find_btc_instrument,
            logger_warning_fn=logger.warning,
            logger_error_fn=logger.error,
        )
        if reload_ts == self.last_quote_watchdog_reload_ts:
            return
        self.last_quote_watchdog_reload_ts = reload_ts
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
        should_run, stale_for = should_run_quote_watchdog(
            now_ts=now_ts,
            last_quote_watchdog_check_ts=float(self.last_quote_watchdog_check_ts),
            quote_healthcheck_interval_sec=float(self.quote_healthcheck_interval_sec),
            last_valid_quote_ts=float(self.last_valid_quote_ts),
            quote_stale_sec=float(self.quote_stale_sec),
            consecutive_invalid_quote_ticks=int(self.consecutive_invalid_quote_ticks),
            quote_invalid_tick_reload_threshold=int(self.quote_invalid_tick_reload_threshold),
        )
        if not should_run:
            return
        self.last_quote_watchdog_check_ts = now_ts

        reason = trigger
        stale_hit = self.last_valid_quote_ts > 0 and stale_for >= self.quote_stale_sec
        invalid_hit = self.consecutive_invalid_quote_ticks >= self.quote_invalid_tick_reload_threshold
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
        if self.market_phase == MarketPhase.WAITING:
            reasons.append("phase_waiting")
        elif self.market_phase == MarketPhase.SETTLING:
            reasons.append("phase_settling")
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
        if now_ts < float(self.regime_guard_conservative_until_ts):
            reasons.append(f"regime_guard_{int(self.regime_guard_conservative_until_ts - now_ts)}s")
        current_slug = str(self.current_market_slug or "")
        market_stop_loss_count = int(self.market_stop_loss_count_by_slug.get(current_slug, 0))
        if (
            current_slug
            and self.market_stop_loss_max_per_market > 0
            and market_stop_loss_count >= self.market_stop_loss_max_per_market
        ):
            reasons.append(
                f"market_stop_loss_limit_{market_stop_loss_count}/{self.market_stop_loss_max_per_market}"
            )
        if self.bi_side_enabled:
            if self.active_side == ActiveSide.NONE:
                reasons.append("active_side_none")
            elif self.active_side == ActiveSide.DOWN and self.current_down_instrument_id is None:
                reasons.append("down_instrument_missing")

        bid_txt = f"{float(self.latest_market_bid):.4f}" if self.latest_market_bid is not None else "None"
        ask_txt = f"{float(self.latest_market_ask):.4f}" if self.latest_market_ask is not None else "None"
        stale_for = (now_ts - self.last_valid_quote_ts) if self.last_valid_quote_ts > 0 else -1.0
        active_orders = list(self.active_maker_orders.keys())
        tradable = "YES" if len(reasons) == 0 else "NO"
        reason_txt = "ok" if len(reasons) == 0 else ",".join(reasons)
        side_score_txt = f"{float(self.side_decision_score):+.2f}" if self.bi_side_enabled else "n/a"
        side_reason_txt = self.side_decision_reason if self.bi_side_enabled else "disabled"
        side_locked_txt = "1" if self.active_side_locked else "0"
        side_due_in = max(0.0, self.side_decision_due_ts - now_ts) if self.bi_side_enabled and self.side_decision_due_ts > 0 else 0.0

        logger.info(
            "STATUS "
            f"tradable={tradable} reason={reason_txt} "
            f"phase={self.market_phase.value} "
            f"slug={self.current_market_slug or '-'} "
            f"instrument={self.instrument_id or '-'} "
            f"active_side={self.active_side.value} "
            f"side_score={side_score_txt} "
            f"side_locked={side_locked_txt} "
            f"side_reason={side_reason_txt} "
            f"side_due_in={side_due_in:.1f}s "
            f"bid={bid_txt} ask={ask_txt} "
            f"stale_for={stale_for:.1f}s invalid_ticks={self.consecutive_invalid_quote_ticks} "
            f"inventory={float(self.inventory_delta_shares):.4f}/{float(self.maker_max_inventory_shares):.4f} "
            f"active_orders={active_orders}"
            f"{self._format_time_left()}"
        )
        if "active_side_none" in reasons and self.bi_side_enabled:
            throttle_key = f"{self.current_market_slug or '-'}:active_side_none"
            last_ts = float(getattr(self, "_last_no_trade_reason_event_ts_by_key", {}).get(throttle_key, 0.0))
            if now_ts - last_ts >= max(30.0, float(self.strategy_status_interval_sec)):
                if not hasattr(self, "_last_no_trade_reason_event_ts_by_key"):
                    self._last_no_trade_reason_event_ts_by_key = {}
                self._last_no_trade_reason_event_ts_by_key[throttle_key] = now_ts
                self._db_strategy_event(
                    "NO_TRADE_ACTIVE_SIDE_NONE",
                    {
                        "phase": self.market_phase.value,
                        "side_score": float(self.side_decision_score),
                        "side_reason": self.side_decision_reason,
                        "side_locked": bool(self.active_side_locked),
                        "time_left_sec": (
                            float(self.current_market_end_timestamp - now_ts)
                            if self.current_market_end_timestamp is not None
                            else None
                        ),
                    },
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
        end_ts = getattr(self, "current_market_end_timestamp", None)
        decision = evaluate_market_phase(
            current_phase_value=self.market_phase.value,
            end_ts=end_ts,
            now_ts=now_ts,
            min_minutes_to_close=self.maker_min_minutes_to_close,
            settling_since_ts=self._market_settling_since_ts,
            settling_grace_sec=self.market_settling_grace_sec,
        )
        if decision is not None:
            if decision.set_settling_since:
                self._market_settling_since_ts = now_ts
            self._transition_market_phase(MarketPhase(decision.next_phase_value), now_ts)

        return self.market_phase

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
        if self.startup_verbose:
            logger.info(f"Checking {len(instruments)} loaded instruments...")
        
        if not instruments:
            logger.error("NO INSTRUMENTS LOADED!")
            return False
        
        btc_instruments, current_timestamp = collect_btc_market_candidates(
            instruments=instruments,
            startup_verbose=self.startup_verbose,
        )
        
        if not btc_instruments:
            logger.error("NO BTC 15-MIN INSTRUMENTS FOUND!")
            return False

        selection, selection_kind, current_count, future_count = resolve_bi_side_market_selection(
            btc_instruments=btc_instruments,
            current_timestamp=current_timestamp,
            extract_outcome=self._extract_outcome_from_instrument,
        )
        if self.startup_verbose:
            logger.info(
                f"Market candidates: total={len(btc_instruments)} "
                f"current={current_count} future={future_count}"
            )

        if selection_kind is None and selection is not None:
            pass
        elif selection_kind == "future" and selection is not None:
            logger.warning(
                f"No current market, selecting next: {selection.selected_market['slug']} "
                f"(starts in {selection.selected_market['time_diff_minutes']:.1f} min)"
            )
        else:
            logger.warning("No active or future BTC 15-min instruments found in cache. All are PAST.")
            return False
        
        previous_instrument = str(self.instrument_id) if self.instrument_id else None
        previous_slug = str(self.current_market_slug or "")
        previous_active_side = self.active_side
        previous_side_locked = self.active_side_locked
        previous_side_reason = self.side_decision_reason
        previous_side_score = self.side_decision_score
        previous_side_ts = self.side_decision_ts
        previous_side_inputs = dict(self.side_decision_inputs)
        previous_side_flip_count = self.side_flip_count
        previous_pending_flip_side = self.side_pending_flip_side
        previous_pending_flip_count = self.side_pending_flip_count
        self.current_market_slug = selection.current_market_slug
        start_ts = selection.selected_market.get("market_timestamp")
        if self.current_market_slug and start_ts:
            self.market_start_ts_by_slug[self.current_market_slug] = int(start_ts)
        self.current_market_end_timestamp = selection.current_market_end_timestamp
        self.current_up_instrument_id = self._normalize_instrument_id(
            selection.up_instrument_id if selection.matched_up else selection.instrument_id
        )
        self.current_down_instrument_id = self._normalize_instrument_id(
            selection.down_instrument_id if selection.matched_down else None
        )
        if not selection.matched_up:
            logger.warning(
                f"UP outcome instrument not found explicitly for slug={self.current_market_slug}; "
                "falling back to selected primary instrument."
            )
        if self.bi_side_enabled and not selection.matched_down:
            logger.warning(
                f"DOWN outcome instrument not found explicitly for slug={self.current_market_slug}; "
                "falling back to selected primary instrument."
            )

        seen_market_insts: List[InstrumentId] = []
        for inst in (self.current_up_instrument_id, self.current_down_instrument_id):
            if inst is not None and inst not in seen_market_insts:
                seen_market_insts.append(inst)
        self.current_market_instruments = seen_market_insts or [self._normalize_instrument_id(selection.instrument_id)]
        self.instrument_id = self._normalize_instrument_id(selection.instrument_id)
        preserve_side_state = bool(
            self.bi_side_enabled
            and previous_slug
            and self.current_market_slug == previous_slug
        )
        if preserve_side_state:
            self.active_side = previous_active_side
            self.active_side_locked = previous_side_locked
            self.side_decision_reason = previous_side_reason
            self.side_decision_score = previous_side_score
            self.side_decision_ts = previous_side_ts
            self.side_decision_inputs = previous_side_inputs
            self.side_flip_count = previous_side_flip_count
            self.side_pending_flip_side = previous_pending_flip_side
            self.side_pending_flip_count = previous_pending_flip_count
            self.side_decision_done_for_market = previous_active_side != ActiveSide.NONE or previous_side_ts > 0
            self._sync_active_instrument()
            logger.info(
                "Preserving side decision across same-market reload: "
                f"slug={self.current_market_slug} active_side={self.active_side.value} "
                f"locked={'yes' if self.active_side_locked else 'no'} reason={self.side_decision_reason}"
            )
        else:
            self._reset_side_decision_state()
            if self.bi_side_enabled and start_ts:
                self.side_decision_due_ts = max(time.time(), float(start_ts) + float(self.bi_side_decision_grace_sec))
        logger.info(
            f"Selected market: slug={self.current_market_slug} "
            f"instruments={len(self.current_market_instruments)} "
            f"primary={self.instrument_id} "
            f"up={self.current_up_instrument_id} down={self.current_down_instrument_id} "
            f"active_side={self.active_side.value}"
        )
        if self.current_market_slug != previous_slug:
            self._log_strike_status(self.current_market_slug)
        self._reset_maker_state_for_new_market(
            previous_instrument, str(self.instrument_id),
            previous_slug=previous_slug,
            current_slug=str(self.current_market_slug or ""),
        )
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
            # BUG-2 FIX: add staleness TTL to prevent using very old cached values.
            _synth_now = time.time()
            _stale_max = self.stale_quote_synth_max_age_sec
            if bid_decimal is None and ask_decimal is not None:
                bid_age = _synth_now - self.latest_market_bid_ts if self.latest_market_bid_ts > 0 else float("inf")
                if self.latest_market_bid is not None and bid_age < _stale_max:
                    bid_decimal = self.latest_market_bid
                else:
                    bid_decimal = max(Decimal("0.01"), ask_decimal - Decimal("0.01"))
            if ask_decimal is None and bid_decimal is not None:
                ask_age = _synth_now - self.latest_market_ask_ts if self.latest_market_ask_ts > 0 else float("inf")
                if self.latest_market_ask is not None and ask_age < _stale_max:
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

            self.latest_quote_depth_by_inst[str(tick.instrument_id)] = (bid_size_decimal, ask_size_decimal)
            mid_price = (bid_decimal + ask_decimal) / 2
            self._append_real_mid_price(tick.instrument_id, mid_price)
            preferred_inst = self._instrument_for_side(self.active_side) or self._primary_instrument_for_market()
            if preferred_inst is None or tick.instrument_id == preferred_inst:
                self.last_valid_quote_ts = time.time()
                self.consecutive_invalid_quote_ticks = 0
                self.latest_market_bid = bid_decimal
                self.latest_market_ask = ask_decimal
                self.latest_market_bid_ts = time.time()  # BUG-2 FIX: track freshness
                self.latest_market_ask_ts = self.latest_market_bid_ts
                self.price_history.append(mid_price)
                if len(self.price_history) > self.max_history:
                    self.price_history.pop(0)
            if self.maker_mode:
                self._start_maker_worker(bid_decimal, ask_decimal)
                return
            logger.warning("Non-maker mode is no longer supported in the slimmed bot path.")
        
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
        filled_directional_snapshot: Dict[str, Any] = {}
        filled_inst: Any = None
        maker_matched = False
        pending_fill_qty_dec = Decimal(str(float(getattr(event, "last_qty", 0.0) or 0.0)))
        for order_key, state in list(self.active_maker_orders.items()):
            side = str(state.get("side", "") or "")
            order = state.get("order")
            if order and str(order.client_order_id) == filled_id:
                maker_matched = True
                filled_side = side
                filled_econ = state.get("econ")
                snap = state.get("directional_snapshot")
                if isinstance(snap, dict):
                    filled_directional_snapshot = snap
                filled_inst = state.get("instrument_id")
                fill_qty = pending_fill_qty_dec
                if fill_qty <= 0:
                    fill_qty = Decimal(str(state.get("quantity", "0")))
                total_qty = Decimal(str(state.get("quantity", "0")))
                accumulated = Decimal(str(state.get("filled_qty", "0"))) + fill_qty
                if accumulated > total_qty and total_qty > 0:
                    fill_qty = max(Decimal("0"), total_qty - Decimal(str(state.get("filled_qty", "0"))))
                    accumulated = total_qty
                state["filled_qty"] = accumulated
                if total_qty <= 0 or accumulated >= total_qty:
                    self.active_maker_orders.pop(order_key, None)
                break
        if filled_inst is None:
            filled_inst = getattr(event, "instrument_id", None) or self.instrument_id

        fill_price_dec = Decimal(str(float(getattr(event, "last_px", 0.0) or 0.0)))
        fill_qty_dec = pending_fill_qty_dec
        raw_commission_dec = Decimal(str(float(getattr(event, "commission", 0.0) or 0.0)))
        taker_exit_reason = self.taker_exit_reason_by_client_order_id.get(filled_id)
        liquidity_side_raw = getattr(event, "liquidity_side", "")
        is_maker_fill = self._is_maker_fill_liquidity(liquidity_side_raw)
        side_for_ledger = filled_side or self._normalize_side_text(getattr(event, "order_side", ""))
        fill_side_norm = side_for_ledger or self._normalize_side_text(getattr(event, "order_side", ""))
        effective_fee_usdc_dec = Decimal("0")
        effective_fee_shares_dec = Decimal("0")
        if not is_maker_fill and side_for_ledger:
            # Official Polymarket taker fee model:
            # - fee is calculated in USDC
            # - BUY collects fee in shares
            # - SELL collects fee in USDC
            effective_fee_usdc_calc = estimate_taker_fee_usdc(
                shares=fill_qty_dec,
                probability=fill_price_dec,
            )
            if side_for_ledger == "buy":
                effective_fee_shares_dec = estimate_taker_buy_fee_shares(
                    shares=fill_qty_dec,
                    probability=fill_price_dec,
                )
            else:
                effective_fee_usdc_dec = effective_fee_usdc_calc
        inventory_fill_delta_dec = fill_qty_dec
        if side_for_ledger == "buy":
            inventory_fill_delta_dec = max(Decimal("0"), fill_qty_dec - effective_fee_shares_dec)
        if maker_matched and fill_qty_dec > 0:
            if side_for_ledger == "buy":
                self.inventory_delta_shares += inventory_fill_delta_dec
            elif side_for_ledger == "sell":
                self.inventory_delta_shares -= fill_qty_dec
        # Non-maker fills (e.g. taker-exit IOC market sells) are not in active_maker_orders.
        # Keep inventory_delta_shares in sync for those fills as well.
        if not maker_matched and fill_qty_dec > 0:
            side_norm = self._normalize_side_text(getattr(event, "order_side", ""))
            if side_norm == "buy":
                self.inventory_delta_shares += inventory_fill_delta_dec
            elif side_norm == "sell":
                self.inventory_delta_shares -= fill_qty_dec
        realized_net_usdc = None
        if side_for_ledger:
            realized_net_usdc = self._update_live_inventory_cost_from_fill(
                instrument_id=filled_inst,
                side=side_for_ledger,
                fill_price=fill_price_dec,
                fill_qty=fill_qty_dec,
                fee_usdc=effective_fee_usdc_dec,
                fee_shares=effective_fee_shares_dec,
            )
        filled_inst_key = self._instrument_key(filled_inst)
        if side_for_ledger == "sell" and filled_inst_key:
            self._sell_recovery_required_by_inst.pop(filled_inst_key, None)
            self._sell_recovery_reason_by_inst.pop(filled_inst_key, None)
            self._sell_recovery_venue_cap_by_inst.pop(filled_inst_key, None)
        if (
            side_for_ledger == "buy"
            and self.maker_high_cost_exit_cooldown_enabled
            and self.maker_high_cost_exit_cooldown_sec > 0
            and fill_price_dec >= self.maker_high_cost_fill_threshold
        ):
            inst_key = self._instrument_key(filled_inst)
            if inst_key:
                cooldown_until = time.time() + float(self.maker_high_cost_exit_cooldown_sec)
                self.high_cost_exit_cooldown_until_by_inst[inst_key] = max(
                    float(self.high_cost_exit_cooldown_until_by_inst.get(inst_key, 0.0)),
                    cooldown_until,
                )
                self.high_cost_last_fill_price_by_inst[inst_key] = float(fill_price_dec)
                logger.warning(
                    "High-cost BUY fill cooldown armed: "
                    f"inst={inst_key} fill={float(fill_price_dec):.4f} "
                    f"threshold={float(self.maker_high_cost_fill_threshold):.4f} "
                    f"cooldown={self.maker_high_cost_exit_cooldown_sec}s"
                )
        self._clear_pending_taker_exit_for_order(filled_id)
        if taker_exit_reason == "stop_loss" and self.stop_loss_reentry_cooldown_sec > 0:
            inst_key = self._instrument_key(filled_inst)
            if inst_key:
                pause_until = time.time() + float(self.stop_loss_reentry_cooldown_sec)
                self.stop_loss_reentry_pause_until_by_inst[inst_key] = max(
                    float(self.stop_loss_reentry_pause_until_by_inst.get(inst_key, 0.0)),
                    pause_until,
                )
                logger.warning(
                    "Stop-loss re-entry cooldown armed: "
                    f"inst={inst_key} cooldown={self.stop_loss_reentry_cooldown_sec}s"
                )
            current_slug = str(self.current_market_slug or "")
            if current_slug:
                new_count = int(self.market_stop_loss_count_by_slug.get(current_slug, 0)) + 1
                self.market_stop_loss_count_by_slug[current_slug] = new_count
                self._db_strategy_event(
                    "MARKET_STOP_LOSS_COUNT_UPDATED",
                    {
                        "slug": current_slug,
                        "count": new_count,
                        "max_per_market": int(self.market_stop_loss_max_per_market),
                        "instrument_id": str(filled_inst) if filled_inst else None,
                        "client_order_id": filled_id,
                    },
                )
                if (
                    self.market_stop_loss_max_per_market > 0
                    and new_count >= self.market_stop_loss_max_per_market
                ):
                    self._db_strategy_event(
                        "MARKET_STOP_LOSS_LIMIT_REACHED",
                        {
                            "slug": current_slug,
                            "count": new_count,
                            "max_per_market": int(self.market_stop_loss_max_per_market),
                            "instrument_id": str(filled_inst) if filled_inst else None,
                            "client_order_id": filled_id,
                        },
                    )
                    logger.warning(
                        "Market stop-loss limit reached: "
                        f"slug={current_slug} count={new_count}/{self.market_stop_loss_max_per_market}"
                    )
            penalty_side = self._side_for_instrument_id(filled_inst)
            if self.current_market_slug and penalty_side != ActiveSide.NONE:
                penalty_until = time.time() + float(self.stop_loss_reentry_cooldown_sec)
                penalty_key = f"{self.current_market_slug}:{penalty_side.value}"
                self.side_stop_loss_penalty_until_by_market_side[penalty_key] = max(
                    float(self.side_stop_loss_penalty_until_by_market_side.get(penalty_key, 0.0)),
                    penalty_until,
                )
                payload = {
                    "slug": self.current_market_slug,
                    "penalized_side": penalty_side.value,
                    "penalty_until_ts": penalty_until,
                    "penalty_remaining_sec": float(self.stop_loss_reentry_cooldown_sec),
                    "instrument_id": str(filled_inst) if filled_inst else None,
                    "client_order_id": filled_id,
                }
                self._db_strategy_event("SIDE_STOP_LOSS_PENALIZED", payload)
                if penalty_side == self.active_side:
                    self.active_side = ActiveSide.NONE
                    self.active_side_locked = False
                    self.side_pending_flip_side = ActiveSide.NONE
                    self.side_pending_flip_count = 0
                    self.side_decision_due_ts = time.time()
                    self.side_decision_reason = f"stop_loss_penalty:{penalty_side.value.lower()}"
                    self._sync_active_instrument()
                    self._cancel_maker_order_side(side="buy", instrument_id=filled_inst, reason="stop_loss_penalty")
                    logger.warning(
                        "Side penalized after stop-loss: "
                        f"slug={self.current_market_slug} side={penalty_side.value} "
                        f"cooldown={self.stop_loss_reentry_cooldown_sec}s"
                    )
        current_slug = str(self.current_market_slug or "")
        self._record_market_buy_count_if_needed(
            side_for_ledger=str(side_for_ledger or ""),
            current_slug=current_slug,
            filled_id=filled_id,
            filled_inst=filled_inst,
            liquidity_side_raw=liquidity_side_raw,
        )

        self.consecutive_denied_orders = 0
        self.last_quote_update_ts = 0.0

        self._record_observed_fee_rate_from_fill(
            side_for_ledger=str(side_for_ledger or ""),
            fill_qty_dec=fill_qty_dec,
            fill_price_dec=fill_price_dec,
            effective_fee_usdc_dec=effective_fee_usdc_dec,
            effective_fee_shares_dec=effective_fee_shares_dec,
        )

        self.rebate_reporter.record_fill(
            econ=filled_econ,
            fill_qty=float(event.last_qty),
            fill_price=float(event.last_px),
        )
        self._db_order_event(
            event_type="ORDER_FILLED",
            client_order_id=str(getattr(event, "client_order_id", "")),
            venue_order_id=str(getattr(event, "venue_order_id", "")) if getattr(event, "venue_order_id", None) else None,
            side=(fill_side_norm.upper() if fill_side_norm else None),
            price=float(getattr(event, "last_px", 0.0)),
            qty=float(getattr(event, "last_qty", 0.0)),
            status="FILLED",
            expected_net_usdc=(
                float(getattr(filled_econ, "expected_net_usdc", 0.0))
                if filled_econ is not None
                else None
            ),
            commission_usdc=float(effective_fee_usdc_dec),
            payload=build_fill_order_event_payload(
                liquidity_side_raw=liquidity_side_raw,
                inventory_delta_shares=self.inventory_delta_shares,
                raw_commission_dec=raw_commission_dec,
                effective_fee_usdc_dec=effective_fee_usdc_dec,
                effective_fee_shares_dec=effective_fee_shares_dec,
                filled_econ=filled_econ,
                filled_directional_snapshot=filled_directional_snapshot,
                realized_net_usdc=realized_net_usdc,
            ),
        )
        self.rebate_reporter.flush_daily_report()
        if self.terminal_dashboard:
            side_norm = side_for_ledger or self._normalize_side_text(getattr(event, "order_side", ""))
            self.terminal_dashboard.increment_fill(
                is_maker_fill=is_maker_fill,
                side=side_norm,
                qty=float(getattr(event, "last_qty", 0.0) or 0.0),
                price=float(getattr(event, "last_px", 0.0) or 0.0),
                commission_usdc=float(effective_fee_usdc_dec),
                client_order_id=filled_id,
                is_taker_exit=filled_id.startswith("BTC-15M-TAKER-EXIT-"),
            )
        self._update_terminal_dashboard_snapshot()

        fill_side_norm = filled_side or self._normalize_side_text(getattr(event, "order_side", ""))
        self._apply_post_fill_followup(
            fill_side_norm=fill_side_norm,
            realized_net_usdc=realized_net_usdc,
        )

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
        cancel_result = reconcile_cancel_ack(
            canceled_id=canceled_id,
            event=event,
            active_maker_orders=self.active_maker_orders,
            last_cancel_ack_ts_by_client_order_id=self._last_cancel_ack_ts_by_client_order_id,
            cancel_ack_dedupe_window_sec=float(self._cancel_ack_dedupe_window_sec),
        )
        if cancel_result.should_skip:
            logger.debug(f"Skip duplicate cancel ack log for {canceled_id}")
            return
        self._update_terminal_dashboard_snapshot()
        self._db_order_event(
            event_type="ORDER_CANCELED",
            client_order_id=canceled_id,
            venue_order_id=str(getattr(event, "venue_order_id", "")) if getattr(event, "venue_order_id", None) else None,
            side=str(getattr(event, "order_side", "")),
            status="CANCELED",
            reason=cancel_result.cancel_reason,
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
            if reconcile_benign_cancel_reject(
                rejected_id=rejected_id,
                active_maker_orders=self.active_maker_orders,
            ):
                logger.info(f"Clearing {rejected_id} from active_maker_orders due to benign CancelReject.")
            
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
        reject_result = reconcile_rejected_order(
            denied_id=denied_id,
            event=event,
            active_maker_orders=self.active_maker_orders,
            normalize_side_text_fn=self._normalize_side_text,
            instrument_key_fn=self._instrument_key,
        )
        if reject_result.is_taker_exit_reject and reject_result.rejected_inst_key and self.taker_exit_reject_cooldown_sec > 0:
            cooldown_until = time.time() + float(self.taker_exit_reject_cooldown_sec)
            prev_until = float(self.taker_exit_reject_cooldown_until_by_inst.get(reject_result.rejected_inst_key, 0.0))
            self.taker_exit_reject_cooldown_until_by_inst[reject_result.rejected_inst_key] = max(prev_until, cooldown_until)
            self._log_taker_exit_skip_throttled(
                inst_key=reject_result.rejected_inst_key,
                reason_tag="reject_cooldown",
                message=(
                    "Taker exit rejection cooldown activated: "
                    f"inst={reject_result.rejected_inst_key} cooldown={self.taker_exit_reject_cooldown_sec}s"
                ),
                now_ts=time.time(),
            )

        self.consecutive_denied_orders += 1
        reason = reject_result.reason
        venue_balance_shares = self._extract_venue_balance_shares_from_reject(reason)
        self._db_order_event(
            event_type="ORDER_REJECTED" if "REJECTED" in title else "ORDER_DENIED",
            client_order_id=str(getattr(event, "client_order_id", "")),
            venue_order_id=str(getattr(event, "venue_order_id", "")) if getattr(event, "venue_order_id", None) else None,
            side=str(getattr(event, "order_side", "")),
            status="REJECTED",
            reason=reason,
            payload={
                "title": title,
                "consecutive_denied": self.consecutive_denied_orders,
                "instrument_id": str(getattr(event, "instrument_id", "") or ""),
                "venue_balance_shares": float(venue_balance_shares) if venue_balance_shares is not None else None,
                "sell_recovery_candidate": bool(reject_result.rejected_side == "sell"),
            },
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
            pause_sec = max(1.0, float(self.sell_balance_retry_pause_sec))
            now_ts = time.time()
            if reject_result.rejected_side == "sell":
                inst_key = self._instrument_key(reject_result.rejected_inst)
                if inst_key:
                    self._sell_reject_pause_until_by_inst[inst_key] = max(
                        float(self._sell_reject_pause_until_by_inst.get(inst_key, 0.0)),
                        now_ts + pause_sec,
                    )
                    self._sell_recovery_required_by_inst[inst_key] = now_ts
                    self._sell_recovery_reason_by_inst[inst_key] = reason
                    if venue_balance_shares is not None and venue_balance_shares > 0:
                        self._sell_recovery_venue_cap_by_inst[inst_key] = venue_balance_shares
                # Keep BUY quotes alive; block SELL quotes only.
                self._cancel_maker_order_side("sell", reason="sell_balance_reject", instrument_id=reject_result.rejected_inst)
                # Refresh conditional balance cache immediately to reduce repeated rejects.
                token_id = (
                    self._extract_token_id_from_instrument(str(reject_result.rejected_inst))
                    if reject_result.rejected_inst is not None
                    else None
                )
                self._get_conditional_balance_for_token(token_id=token_id, force_refresh=True)
                self._force_quote_refresh_once = True
                self._force_quote_refresh_reason = "sell_recovery_balance_reject"
                venue_balance_txt = (
                    f"{float(venue_balance_shares):.6f}"
                    if venue_balance_shares is not None
                    else "unknown"
                )
                logger.warning(
                    "SELL balance/allowance rejection detected; "
                    f"treat as venue balance lag and retry after {pause_sec:.1f}s "
                    f"(instrument={inst_key or '-'}, venue_balance={venue_balance_txt}). "
                    "BUY side remains active."
                )
                # Venue balance lag is a synchronization issue, not a strategy failure.
                self.consecutive_denied_orders = max(0, self.consecutive_denied_orders - 1)
                self.rebate_reporter.record_denied()
                self._increment_order_metric("rejected")
                return
            else:
                pause_sec = max(1, self.maker_error_pause_sec)
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
        stop_event_threads(
            stop_events=[
                self._lifecycle_stop_event,
                self._reload_stop_event,
                self._quote_watchdog_stop_event,
                self._redeem_stop_event,
                self._balance_stop_event,
                self._binance_ws_stop_event,
                self._terminal_dashboard_stop_event,
            ],
            threads=[
                self._lifecycle_thread,
                self._reload_thread,
                self._quote_watchdog_thread,
                self._redeem_thread,
                self._balance_thread,
                self._binance_ws_thread,
                self._terminal_dashboard_thread,
            ],
            join_timeout_sec=2.0,
        )
        logger.info("Integrated BTC strategy stopped")
        self._cancel_active_maker_orders()
        self.rebate_reporter.flush_daily_report()
        self._db_strategy_event(
            "STRATEGY_STOP",
            {
                "mode": "TEST_DRY_RUN" if self._is_dry_run_mode() else "LIVE",
                "inventory_delta_shares": float(self.inventory_delta_shares),
                "active_side": self.active_side.value,
            },
        )
        log_strategy_run_stop(
            trade_db=self.trade_db,
            run_id=self.run_id,
            is_dry_run_mode=self._is_dry_run_mode(),
            test_mode=self.test_mode,
            maker_mode=self.maker_mode,
            instrument_id=self.instrument_id,
            selected_slug=self.selected_slug,
            final_inventory_shares=self.inventory_delta_shares,
            market_cycle_realized_net_usdc=self.market_cycle_realized_net_usdc,
        )
        
        if self.grafana_exporter:
            try:
                self.grafana_exporter.stop()
            except:
                pass
        if self.terminal_dashboard:
            try:
                self.terminal_dashboard.stop()
            except Exception:
                pass


def run_integrated_bot(
    simulation: bool = True,
    enable_grafana: bool = True,
    test_mode: bool = False,
    enable_terminal_dashboard: bool = False,
):
    """Run the integrated BTC 15-min trading bot."""
    startup_verbose = os.getenv("STARTUP_VERBOSE", "0").strip().lower() in ("1", "true", "yes", "on")
    logger.info("Starting integrated Polymarket BTC 15-min trading bot.")
    
    # Initialize Redis
    redis_client = init_redis()
    
    # Set initial simulation mode in Redis
    if redis_client:
        try:
            redis_client.set('btc_trading:simulation_mode', '1' if simulation else '0')
            logger.info(f"Initial mode set in Redis: {'SIMULATION' if simulation else 'LIVE'}")
        except Exception as e:
            logger.warning(f"Could not set Redis simulation mode: {e}")
    
    auto_rollover_enabled = os.getenv("AUTO_NODE_ROLLOVER_ENABLED", "1").strip().lower() not in ("0", "false", "no")
    auto_rollover_sec = max(300, int(os.getenv("AUTO_NODE_ROLLOVER_SEC", "1800")))
    auto_rollover_cooldown_sec = max(1, int(os.getenv("AUTO_NODE_ROLLOVER_COOLDOWN_SEC", "3")))
    auto_rollover_max_failures = max(1, int(os.getenv("AUTO_NODE_ROLLOVER_MAX_FAILURES", "5")))
    auto_restart_on_unexpected_exit = os.getenv("AUTO_NODE_RESTART_ON_UNEXPECTED_EXIT", "0").strip().lower() in ("1", "true", "yes", "on")
    logger.info(
        "Startup config: "
        f"mode={'SIMULATION' if simulation else 'LIVE'} "
        f"redis={'on' if redis_client else 'off'} "
        f"grafana={'on' if enable_grafana else 'off'} "
        f"terminal_dashboard={'on' if enable_terminal_dashboard else 'off'} "
        f"auto_rollover={'on' if auto_rollover_enabled else 'off'}({auto_rollover_sec}s)"
    )
    if startup_verbose:
        logger.info(
            f"Startup detail: restart_on_unexpected_exit={'on' if auto_restart_on_unexpected_exit else 'off'} "
            f"rollover_cooldown={auto_rollover_cooldown_sec}s max_failures={auto_rollover_max_failures}"
        )

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

        logger.info(
            f"Market discovery cycle={cycle_index}: primary={primary_slug} "
            f"load_slugs={len(slugs_to_load)} instrument_ids={len(instrument_ids)} "
            f"window_back={window_back_minutes}m window_forward={window_forward_minutes}m"
        )
        if os.getenv("STARTUP_VERBOSE", "0").strip().lower() in ("1", "true", "yes", "on"):
            logger.info(f"Market discovery details: slugs={slugs_to_load} ids={[inst.value for inst in instrument_ids]}")

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

        poly_http_max_retries = max(1, int(os.getenv("POLY_HTTP_MAX_RETRIES", "4")))
        poly_http_retry_initial_ms = max(50, int(os.getenv("POLY_HTTP_RETRY_INITIAL_MS", "250")))
        poly_http_retry_max_ms = max(poly_http_retry_initial_ms, int(os.getenv("POLY_HTTP_RETRY_MAX_MS", "2000")))

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
            max_retries=poly_http_max_retries,
            retry_delay_initial_ms=poly_http_retry_initial_ms,
            retry_delay_max_ms=poly_http_retry_max_ms,
        )

        config = TradingNodeConfig(
            environment="live",
            trader_id="BTC-15MIN-INTEGRATED-001",
            logging=LoggingConfig(
                log_level=os.getenv("NAUTILUS_LOG_LEVEL", "ERROR"),
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
            enable_terminal_dashboard=enable_terminal_dashboard,
        )

        logger.info("Building Nautilus node...")
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

            logger.info(f"Bot cycle {cycle_idx} starting...")
            node.run()
            consecutive_failures = 0
        except KeyboardInterrupt:
            user_stopped = True
            logger.info("Shutdown requested by user.")
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
        "--terminal-dashboard",
        action="store_true",
        help="Show simplified Rich terminal dashboard"
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
    enable_terminal_dashboard = args.terminal_dashboard

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
        test_mode=test_mode,
        enable_terminal_dashboard=enable_terminal_dashboard,
    )

if __name__ == "__main__":
    main()
