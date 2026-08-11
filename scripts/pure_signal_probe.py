#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
import uuid
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.market_data import (  # noqa: E402
    estimate_external_spot_sigma_annualized,
    extract_market_start_ts_from_slug,
    extract_price_to_beat_from_market_payload,
    extract_strike_from_question,
    fetch_binance_open_price_sync,
    fetch_coinbase_spot_sync,
    fetch_gamma_market_by_slug,
    record_external_spot_observation,
    resolve_opening_strike_from_history,
)
from bot.runtime_env import load_runtime_env  # noqa: E402
from bot.lifecycle import collect_btc_market_candidates, select_market_outcome_instruments  # noqa: E402
from execution.maker_engine import MakerEngine  # noqa: E402
from monitoring.trade_journal_db import TradeJournalDB  # noqa: E402

from nautilus_trader.adapters.polymarket import POLYMARKET  # noqa: E402
from nautilus_trader.adapters.polymarket import PolymarketDataClientConfig  # noqa: E402
from nautilus_trader.adapters.polymarket.factories import PolymarketLiveDataClientFactory  # noqa: E402
from nautilus_trader.config import (  # noqa: E402
    InstrumentProviderConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode  # noqa: E402
from nautilus_trader.model.data import QuoteTick  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402
from nautilus_trader.trading.strategy import Strategy  # noqa: E402

load_runtime_env(repo_root=PROJECT_ROOT)

try:
    from py_clob_client_v2.client import ClobClient  # type: ignore
except Exception:
    ClobClient = None


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
    txt = str(value or "").strip()
    return txt.isdigit() and len(txt) >= 20


async def _hydrate_gamma_market_details(market: Dict[str, Any]) -> Dict[str, Any]:
    import httpx

    api_base = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")
    timeout = float(os.getenv("GAMMA_DISCOVERY_TIMEOUT_SEC", "8"))
    market_id = market.get("id") or market.get("marketId") or market.get("conditionId") or market.get("condition_id")
    if not market_id:
        return market
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(f"{api_base}/markets/{market_id}")
            if resp.status_code != 200:
                return market
            payload = resp.json()
            if isinstance(payload, dict):
                merged = dict(market)
                merged.update(payload)
                return merged
        except Exception:
            return market
    return market


def _extract_instrument_ids_from_gamma_market(market: Dict[str, Any]) -> List[InstrumentId]:
    condition_id = str(market.get("conditionId") or market.get("condition_id") or "").strip()
    if not condition_id:
        return []

    token_ids = _parse_json_list(market.get("clobTokenIds") or market.get("clob_token_ids") or market.get("clobTokenIDs"))
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
    except Exception:
        return []
    if not market:
        return []
    instrument_ids = _extract_instrument_ids_from_gamma_market(market)
    if instrument_ids:
        return instrument_ids
    try:
        hydrated = asyncio.run(_hydrate_gamma_market_details(market))
    except Exception:
        hydrated = market
    return _extract_instrument_ids_from_gamma_market(hydrated)


def _build_btc_15m_slug_candidates(lookback: int = 1, lookahead: int = 4) -> List[str]:
    now = datetime.now(timezone.utc)
    interval_start = int(now.timestamp() // 900) * 900
    slugs: List[str] = []
    for offset in range(-lookback, lookahead + 1):
        ts = interval_start + (offset * 900)
        if ts > 0:
            slugs.append(f"btc-updown-15m-{ts}")
    return slugs


async def _discover_existing_btc_15m_slugs(candidates: List[str]) -> List[str]:
    import httpx

    api_base = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")
    timeout = float(os.getenv("GAMMA_DISCOVERY_TIMEOUT_SEC", "8"))
    existing: List[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for slug in candidates:
            try:
                resp = await client.get(
                    f"{api_base}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "archived": "false",
                        "slug": slug,
                        "limit": 1,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list) and data:
                    existing.append(slug)
            except Exception:
                continue
    return existing


def resolve_btc_15m_market_slugs() -> List[str]:
    lookback = int(os.getenv("BTC_MARKET_LOOKBACK_INTERVALS", "1"))
    lookahead = int(os.getenv("BTC_MARKET_LOOKAHEAD_INTERVALS", "4"))
    candidates = _build_btc_15m_slug_candidates(lookback=lookback, lookahead=lookahead)
    if not candidates:
        return []
    try:
        existing = asyncio.run(_discover_existing_btc_15m_slugs(candidates))
    except Exception:
        existing = []
    if existing:
        return existing
    now_ts = int(datetime.now(timezone.utc).timestamp())
    return [s for s in candidates if int(s.rsplit("-", 1)[-1]) >= now_ts - 900]


def select_primary_btc_15m_slug(slugs: List[str]) -> Optional[str]:
    if not slugs:
        return None
    now_ts = int(datetime.now(timezone.utc).timestamp())
    parsed: List[Tuple[int, str]] = []
    for slug in slugs:
        try:
            parsed.append((int(slug.rsplit("-", 1)[-1]), slug))
        except Exception:
            continue
    if not parsed:
        return slugs[0]
    future = [(ts, s) for ts, s in parsed if ts >= now_ts - 900]
    if future:
        future.sort(key=lambda item: item[0])
        return future[0][1]
    parsed.sort(key=lambda item: item[0], reverse=True)
    return parsed[0][1]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _run_id(prefix: str = "pure_probe") -> str:
    return f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def _safe_float(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _extract_tokens(market: Dict[str, Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    tokens = market.get("tokens")
    if not isinstance(tokens, list):
        tokens = []
    for token in tokens:
        if not isinstance(token, dict):
            continue
        token_id = str(token.get("token_id") or token.get("tokenId") or "").strip()
        outcome_raw = str(token.get("outcome") or "").strip().lower()
        if not _valid_token_id(token_id):
            continue
        if outcome_raw in ("up", "yes"):
            result["up"] = token_id
        elif outcome_raw in ("down", "no"):
            result["down"] = token_id

    if "up" in result and "down" in result:
        return result

    token_ids = _parse_json_list(market.get("clobTokenIds") or market.get("clob_token_ids") or market.get("clobTokenIDs"))
    token_ids = [str(token_id).strip() for token_id in token_ids if _valid_token_id(token_id)]
    outcomes = [str(item).strip().lower() for item in _parse_json_list(market.get("outcomes"))]
    if len(token_ids) == len(outcomes):
        for token_id, outcome_raw in zip(token_ids, outcomes):
            if outcome_raw in ("up", "yes"):
                result.setdefault("up", token_id)
            elif outcome_raw in ("down", "no"):
                result.setdefault("down", token_id)
    return result


def _extract_question(market: Dict[str, Any]) -> str:
    for key in ("question", "title"):
        txt = str(market.get(key) or "").strip()
        if txt:
            return txt
    return ""


def resolve_polymarket_auth() -> Optional[Dict[str, str]]:
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

    if private_key and api_key and api_secret and passphrase:
        return {
            "private_key": private_key,
            "api_key": api_key,
            "api_secret": api_secret,
            "passphrase": passphrase,
            "funder": funder or "",
            "signature_type": str(signature_type),
        }

    if not private_key or ClobClient is None:
        return None

    try:
        kwargs: Dict[str, Any] = {
            "key": private_key,
            "signature_type": signature_type,
        }
        if funder:
            kwargs["funder"] = funder
        client = ClobClient(clob_host, chain_id, **kwargs)
        try:
            derived = client.create_api_key()
        except Exception:
            derived = client.derive_api_key()
        d_key = getattr(derived, "api_key", None) or (derived.get("api_key") if isinstance(derived, dict) else None)
        d_secret = getattr(derived, "api_secret", None) or (derived.get("api_secret") if isinstance(derived, dict) else None)
        d_pass = getattr(derived, "api_passphrase", None) or (derived.get("api_passphrase") if isinstance(derived, dict) else None)
        d_key = d_key or (derived.get("key") if isinstance(derived, dict) else None)
        d_secret = d_secret or (derived.get("secret") if isinstance(derived, dict) else None)
        d_pass = d_pass or (derived.get("passphrase") if isinstance(derived, dict) else None)
        logger.info("Pure probe: derived API creds from private key for Nautilus data client")
        return {
            "private_key": private_key,
            "api_key": d_key,
            "api_secret": d_secret,
            "passphrase": d_pass,
            "funder": funder or (client.get_address() or ""),
            "signature_type": str(signature_type),
        }
    except Exception as exc:
        logger.warning(f"Pure probe: failed to resolve Polymarket auth: {exc}")
        return None


class ProbeQuoteStrategy(Strategy):
    def __init__(self) -> None:
        super().__init__()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._subscribed_inst_ids: set[str] = set()
        self._latest_quotes_by_inst: Dict[str, Dict[str, Any]] = {}
        self._market_map_by_slug: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def on_start(self) -> None:
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._subscription_loop, name="probe-quote-subscriptions", daemon=True)
        self._worker.start()

    def on_stop(self) -> None:
        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2)

    @staticmethod
    def _extract_outcome_from_instrument(instrument: Any) -> str:
        try:
            info = getattr(instrument, "info", None) or {}
            if not isinstance(info, dict):
                return ""
            inst_id = str(getattr(instrument, "id", "") or "")
            token_id = inst_id.rsplit("-", 1)[-1].replace(".POLYMARKET", "") if ".POLYMARKET" in inst_id else ""
            tokens = info.get("tokens")
            if isinstance(tokens, list):
                for token in tokens:
                    if not isinstance(token, dict):
                        continue
                    t_id = str(token.get("token_id") or token.get("tokenId") or "")
                    if token_id and t_id == token_id:
                        return str(token.get("outcome") or "").strip().lower()
        except Exception:
            return ""
        return ""

    def _subscription_loop(self) -> None:
        while not self._stop_event.wait(2.0):
            try:
                instruments = list(self.cache.instruments())
            except Exception:
                continue
            btc_instruments, current_timestamp = collect_btc_market_candidates(instruments)
            for item in btc_instruments:
                instrument = item.get("instrument")
                instrument_id = getattr(instrument, "id", None)
                if instrument_id is None:
                    continue
                inst_key = str(instrument_id)
                if inst_key not in self._subscribed_inst_ids:
                    try:
                        self.subscribe_quote_ticks(instrument_id)
                        self._subscribed_inst_ids.add(inst_key)
                    except Exception:
                        continue
            seen_slugs = {str(item.get("slug") or "") for item in btc_instruments if item.get("slug")}
            for slug in seen_slugs:
                up_inst, down_inst, matched_up, matched_down = select_market_outcome_instruments(
                    btc_instruments=btc_instruments,
                    current_market_slug=slug,
                    extract_outcome=self._extract_outcome_from_instrument,
                    fallback_instrument_id=btc_instruments[0]["instrument"].id if btc_instruments else None,
                )
                with self._lock:
                    self._market_map_by_slug[slug] = {
                        "up": up_inst if up_inst is not None and matched_up else up_inst,
                        "down": down_inst if down_inst is not None and matched_down else down_inst,
                    }

    def on_quote_tick(self, tick: QuoteTick) -> None:
        try:
            bid = tick.bid_price.as_decimal() if tick.bid_price is not None else None
            ask = tick.ask_price.as_decimal() if tick.ask_price is not None else None
            with self._lock:
                self._latest_quotes_by_inst[str(tick.instrument_id)] = {
                    "bid": bid,
                    "ask": ask,
                    "ts": time.time(),
                }
        except Exception:
            return

    def quote_snapshot_for_slug(self, slug: str) -> Dict[str, Any]:
        with self._lock:
            mapping = dict(self._market_map_by_slug.get(slug, {}))
            up_inst = mapping.get("up")
            down_inst = mapping.get("down")
            up_quote = dict(self._latest_quotes_by_inst.get(str(up_inst), {})) if up_inst else {}
            down_quote = dict(self._latest_quotes_by_inst.get(str(down_inst), {})) if down_inst else {}
        if up_inst is not None:
            try:
                cached = self.cache.quote_tick(up_inst)
                if cached is not None:
                    up_quote["bid"] = cached.bid_price.as_decimal() if cached.bid_price is not None else up_quote.get("bid")
                    up_quote["ask"] = cached.ask_price.as_decimal() if cached.ask_price is not None else up_quote.get("ask")
            except Exception:
                pass
        if down_inst is not None:
            try:
                cached = self.cache.quote_tick(down_inst)
                if cached is not None:
                    down_quote["bid"] = cached.bid_price.as_decimal() if cached.bid_price is not None else down_quote.get("bid")
                    down_quote["ask"] = cached.ask_price.as_decimal() if cached.ask_price is not None else down_quote.get("ask")
            except Exception:
                pass
        return {
            "up_instrument_id": str(up_inst) if up_inst is not None else None,
            "down_instrument_id": str(down_inst) if down_inst is not None else None,
            "bid_up": up_quote.get("bid"),
            "ask_up": up_quote.get("ask"),
            "bid_down": down_quote.get("bid"),
            "ask_down": down_quote.get("ask"),
        }

    def resolve_instruments_by_tokens(
        self,
        slug: str,
        up_token_id: Optional[str],
        down_token_id: Optional[str],
    ) -> Dict[str, Any]:
        up_inst = None
        down_inst = None
        try:
            instruments = list(self.cache.instruments())
        except Exception:
            instruments = []
        slug_norm = str(slug or "").strip().lower()
        for instrument in instruments:
            info = getattr(instrument, "info", None) or {}
            if not isinstance(info, dict):
                continue
            inst_slug = str(info.get("market_slug") or info.get("slug") or "").strip().lower()
            token_id = str(info.get("token_id") or "").strip()
            if slug_norm and inst_slug != slug_norm:
                continue
            inst_id = getattr(instrument, "id", None)
            if inst_id is None:
                continue
            if up_token_id and token_id == up_token_id and up_inst is None:
                up_inst = inst_id
            if down_token_id and token_id == down_token_id and down_inst is None:
                down_inst = inst_id
        return {"up": up_inst, "down": down_inst}

@dataclass
class ProbeConfig:
    db_path: str
    interval_sec: float
    duration_sec: float
    verbose: bool
    verbose_every_sec: float
    min_edge: Decimal
    min_prob_band: Decimal
    max_prob_band: Decimal
    min_entry_sec: float
    reduce_only_sec: float
    force_flat_sec: float
    sigma_default: Decimal
    sigma_floor: Decimal
    sigma_ceiling: Decimal
    sigma_min_points: int
    sigma_window_points: int
    paper_trade: bool
    paper_persistence_sec: float
    paper_settle_grace_sec: float
    paper_entry_qty: float


class PureSignalProbe:
    def __init__(self, cfg: ProbeConfig) -> None:
        self.cfg = cfg
        self.db = TradeJournalDB(cfg.db_path)
        self.run_id = _run_id()
        self.spot_history: List[Tuple[float, Decimal]] = []
        self.already_logged_first_spot = False
        self.node: Optional[TradingNode] = None
        self.node_thread: Optional[threading.Thread] = None
        self.quote_strategy: Optional[ProbeQuoteStrategy] = None
        self.current_slug: Optional[str] = None
        self.last_candidate_signature: Optional[str] = None
        self.last_verbose_ts: float = 0.0
        self.strike_cache_by_slug: Dict[str, Decimal] = {}
        self.strike_source_by_slug: Dict[str, str] = {}
        self.stop_requested = False
        self._shutdown_started = False
        self.latest_snapshot_by_slug: Dict[str, Dict[str, Any]] = {}
        self.candidate_streaks: Dict[str, Dict[str, Any]] = {}
        self.paper_positions_by_slug: Dict[str, Dict[str, Any]] = {}
        self.paper_settled_slugs: set[str] = set()

    def _build_data_node(self) -> None:
        auth = resolve_polymarket_auth()
        if not auth:
            raise RuntimeError("Cannot resolve Polymarket auth for pure probe data client.")

        slugs = resolve_btc_15m_market_slugs()
        if not slugs:
            raise RuntimeError("No BTC 15m slugs resolved for pure probe.")
        instrument_ids: List[InstrumentId] = []
        seen_ids: set[str] = set()
        for slug in slugs:
            for inst_id in resolve_primary_btc_15m_instrument_ids(slug):
                if inst_id.value in seen_ids:
                    continue
                seen_ids.add(inst_id.value)
                instrument_ids.append(inst_id)
        if not instrument_ids:
            raise RuntimeError("No instrument IDs resolved for pure probe.")

        instrument_cfg = InstrumentProviderConfig(
            load_all=False,
            load_ids=frozenset(instrument_ids),
            use_gamma_markets=True,
            filters={
                "active": True,
                "closed": False,
                "archived": False,
                "limit": 25,
            },
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
        config = TradingNodeConfig(
            environment="live",
            trader_id="BTC-15MIN-PURE-PROBE-001",
            logging=LoggingConfig(
                log_level=os.getenv("NAUTILUS_LOG_LEVEL", "ERROR"),
                log_directory="./logs/nautilus",
            ),
            data_engine=LiveDataEngineConfig(qsize=6000),
            exec_engine=LiveExecEngineConfig(qsize=1000),
            risk_engine=LiveRiskEngineConfig(bypass=True),
            data_clients={POLYMARKET: poly_data_cfg},
        )
        node = TradingNode(config=config)
        node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
        strategy = ProbeQuoteStrategy()
        node.trader.add_strategy(strategy)
        node.build()
        node_thread = threading.Thread(target=node.run, name="pure-probe-node", daemon=False)
        node_thread.start()
        self.node = node
        self.node_thread = node_thread
        self.quote_strategy = strategy
        logger.info("Pure probe: Nautilus data node started for quote ticks")

    def _shutdown_node(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True

        if self.node is None:
            return

        try:
            self.node.stop()
        except Exception as exc:
            logger.warning(f"Pure probe: node.stop() failed during shutdown: {exc}")

        if self.node_thread is not None and self.node_thread.is_alive():
            self.node_thread.join(timeout=15)

        if self.node_thread is not None and self.node_thread.is_alive():
            logger.warning("Pure probe: node thread still alive after graceful stop; disposing node.")

        try:
            self.node.dispose()
        except Exception as exc:
            logger.warning(f"Pure probe: node.dispose() failed during shutdown: {exc}")

        if self.node_thread is not None and self.node_thread.is_alive():
            self.node_thread.join(timeout=5)

        if self.node_thread is not None and self.node_thread.is_alive():
            logger.warning("Pure probe: node thread still alive after dispose.")

    def _fetch_spot(self) -> Optional[Decimal]:
        spot, self.already_logged_first_spot = fetch_coinbase_spot_sync(
            timeout_sec=2.5,
            already_logged_first_spot=self.already_logged_first_spot,
            logger_info_fn=logger.info,
            logger_debug_fn=logger.debug,
        )
        if spot is not None:
            record_external_spot_observation(
                external_spot_history=self.spot_history,
                external_spot_history_max=max(300, self.cfg.sigma_window_points + 20),
                now_ts=time.time(),
                price=spot,
            )
        return spot

    def _estimate_sigma(self) -> Decimal:
        est = estimate_external_spot_sigma_annualized(
            external_spot_history=self.spot_history,
            min_points=self.cfg.sigma_min_points,
            digital_vol_window=self.cfg.sigma_window_points,
        )
        sigma = est if est is not None and est > 0 else self.cfg.sigma_default
        sigma = max(self.cfg.sigma_floor, min(self.cfg.sigma_ceiling, sigma))
        return sigma

    def _resolve_strike(self, market: Dict[str, Any], slug: str, latest_spot: Optional[Decimal]) -> Tuple[Optional[Decimal], str]:
        cached = self.strike_cache_by_slug.get(slug)
        if cached is not None and cached > 0:
            return cached, self.strike_source_by_slug.get(slug, "cache")

        ptb = extract_price_to_beat_from_market_payload(market)
        if ptb is not None and ptb > 0:
            self.strike_cache_by_slug[slug] = ptb
            self.strike_source_by_slug[slug] = "gamma_price_to_beat"
            return ptb, "gamma_price_to_beat"

        question = _extract_question(market)
        parsed = extract_strike_from_question(question, latest_spot)
        if parsed is not None and parsed > 0:
            self.strike_cache_by_slug[slug] = parsed
            self.strike_source_by_slug[slug] = "question_parsed"
            return parsed, "question_parsed"

        start_ts = extract_market_start_ts_from_slug(slug)
        if start_ts is not None:
            anchor = resolve_opening_strike_from_history(
                external_spot_history=self.spot_history,
                start_ts=start_ts,
                max_lag_sec=float(os.getenv("MARKET_STRIKE_ANCHOR_MAX_LAG_SEC", "180")),
                near_window_sec=float(os.getenv("MARKET_STRIKE_ANCHOR_NEAR_SEC", "30")),
            )
            if anchor is not None:
                _, anchor_px = anchor
                if anchor_px > 0:
                    self.strike_cache_by_slug[slug] = anchor_px
                    self.strike_source_by_slug[slug] = "spot_history_open"
                    return anchor_px, "spot_history_open"

            backfilled = fetch_binance_open_price_sync(
                start_ts=start_ts,
                timeout_sec=2.5,
                logger_debug_fn=logger.debug,
            )
            if backfilled is not None and backfilled > 0:
                self.strike_cache_by_slug[slug] = backfilled
                self.strike_source_by_slug[slug] = "binance_rest_open"
                return backfilled, "binance_rest_open"

        return None, "unresolved"

    def _resolve_market(self) -> Optional[str]:
        slugs = resolve_btc_15m_market_slugs()
        if not slugs:
            return None
        return select_primary_btc_15m_slug(slugs)

    def _fetch_market(self, slug: str) -> Optional[Dict[str, Any]]:
        try:
            market = asyncio.run(fetch_gamma_market_by_slug(slug))
            if not market:
                return None
            tokens = _extract_tokens(market)
            if "up" in tokens and "down" in tokens:
                return market
            hydrated = asyncio.run(_hydrate_gamma_market_details(market))
            return hydrated
        except Exception as exc:
            logger.warning(f"Failed to fetch market payload for slug={slug}: {exc}")
            return None

    def _candidate_side(
        self,
        fair_up: Decimal,
        fair_down: Decimal,
        ask_up: Optional[Decimal],
        ask_down: Optional[Decimal],
        time_left_sec: float,
    ) -> Tuple[Optional[str], Optional[Decimal]]:
        if time_left_sec < self.cfg.min_entry_sec:
            return None, None
        if not (self.cfg.min_prob_band <= fair_up <= self.cfg.max_prob_band):
            return None, None

        best_side: Optional[str] = None
        best_edge: Optional[Decimal] = None

        if ask_up is not None and ask_up > 0:
            edge_up = fair_up - ask_up
            if edge_up >= self.cfg.min_edge:
                best_side = "BUY_UP"
                best_edge = edge_up

        if ask_down is not None and ask_down > 0:
            edge_down = fair_down - ask_down
            if edge_down >= self.cfg.min_edge and (best_edge is None or edge_down > best_edge):
                best_side = "BUY_DOWN"
                best_edge = edge_down

        return best_side, best_edge

    def _snapshot_payload(self, market: Dict[str, Any], slug: str, spot: Decimal, sigma: Decimal) -> Dict[str, Any]:
        strike, strike_source = self._resolve_strike(market=market, slug=slug, latest_spot=spot)
        market_start_ts = extract_market_start_ts_from_slug(slug)
        market_end_ts = market_start_ts + 900 if market_start_ts is not None else None
        now_ts = time.time()
        time_left_sec = max(0.0, float(market_end_ts - now_ts)) if market_end_ts is not None else 0.0
        tokens = _extract_tokens(market)
        up_token_id = tokens.get("up")
        down_token_id = tokens.get("down")
        quote_snapshot: Dict[str, Any] = {}
        if self.quote_strategy is not None:
            resolved = self.quote_strategy.resolve_instruments_by_tokens(
                slug=slug,
                up_token_id=up_token_id,
                down_token_id=down_token_id,
            )
            if resolved.get("up") is not None or resolved.get("down") is not None:
                up_inst = resolved.get("up")
                down_inst = resolved.get("down")
                up_quote = self.quote_strategy.cache.quote_tick(up_inst) if up_inst is not None else None
                down_quote = self.quote_strategy.cache.quote_tick(down_inst) if down_inst is not None else None
                quote_snapshot = {
                    "up_instrument_id": str(up_inst) if up_inst is not None else None,
                    "down_instrument_id": str(down_inst) if down_inst is not None else None,
                    "bid_up": up_quote.bid_price.as_decimal() if up_quote is not None and up_quote.bid_price is not None else None,
                    "ask_up": up_quote.ask_price.as_decimal() if up_quote is not None and up_quote.ask_price is not None else None,
                    "bid_down": down_quote.bid_price.as_decimal() if down_quote is not None and down_quote.bid_price is not None else None,
                    "ask_down": down_quote.ask_price.as_decimal() if down_quote is not None and down_quote.ask_price is not None else None,
                }
            else:
                quote_snapshot = self.quote_strategy.quote_snapshot_for_slug(slug)
        bid_up = quote_snapshot.get("bid_up")
        ask_up = quote_snapshot.get("ask_up")
        bid_down = quote_snapshot.get("bid_down")
        ask_down = quote_snapshot.get("ask_down")
        up_inst_id = quote_snapshot.get("up_instrument_id")
        down_inst_id = quote_snapshot.get("down_instrument_id")

        fair_up = Decimal("0.5")
        if strike is not None and strike > 0:
            fair_up = MakerEngine.digital_up_probability(
                spot=float(spot),
                strike=float(strike),
                sigma_annual=float(sigma),
                time_left_sec=time_left_sec,
            )
        fair_up = max(Decimal("0.01"), min(Decimal("0.99"), fair_up))
        fair_down = Decimal("1") - fair_up

        edge_buy_up = (fair_up - ask_up) if ask_up is not None else None
        edge_buy_down = (fair_down - ask_down) if ask_down is not None else None
        candidate_side, candidate_edge = self._candidate_side(
            fair_up=fair_up,
            fair_down=fair_down,
            ask_up=ask_up,
            ask_down=ask_down,
            time_left_sec=time_left_sec,
        )

        return {
            "slug": slug,
            "question": _extract_question(market),
            "market_start_ts": market_start_ts,
            "market_end_ts": market_end_ts,
            "time_left_sec": round(time_left_sec, 3),
            "spot": float(spot),
            "strike": _safe_float(strike),
            "strike_source": strike_source,
            "sigma_annual": float(sigma),
            "up_token_id": up_token_id,
            "down_token_id": down_token_id,
            "up_instrument_id": up_inst_id,
            "down_instrument_id": down_inst_id,
            "bid_up": _safe_float(bid_up),
            "ask_up": _safe_float(ask_up),
            "bid_down": _safe_float(bid_down),
            "ask_down": _safe_float(ask_down),
            "fair_up": float(fair_up),
            "fair_down": float(fair_down),
            "edge_buy_up": _safe_float(edge_buy_up),
            "edge_buy_down": _safe_float(edge_buy_down),
            "candidate_side": candidate_side,
            "candidate_edge": _safe_float(candidate_edge),
            "min_edge": float(self.cfg.min_edge),
            "min_entry_sec": float(self.cfg.min_entry_sec),
            "reduce_only_sec": float(self.cfg.reduce_only_sec),
            "force_flat_sec": float(self.cfg.force_flat_sec),
        }

    def _log_candidate(self, payload: Dict[str, Any]) -> None:
        candidate_side = payload.get("candidate_side")
        candidate_edge = payload.get("candidate_edge")
        if not candidate_side or candidate_edge is None:
            self.last_candidate_signature = None
            return
        signature = json.dumps(
            {
                "slug": payload.get("slug"),
                "candidate_side": candidate_side,
                "candidate_edge": round(float(candidate_edge), 4),
            },
            sort_keys=True,
        )
        if signature == self.last_candidate_signature:
            return
        self.last_candidate_signature = signature
        self.db.log_strategy_event(self.run_id, "PURE_SIGNAL_CANDIDATE", payload)
        logger.info(
            f"PURE candidate slug={payload.get('slug')} side={candidate_side} "
            f"edge={float(candidate_edge):.4f} fair_up={float(payload.get('fair_up') or 0.0):.4f} "
            f"ask_up={payload.get('ask_up')} ask_down={payload.get('ask_down')}"
        )

    def _log_verbose_snapshot(self, payload: Dict[str, Any]) -> None:
        if not self.cfg.verbose:
            return
        now_ts = time.time()
        if self.last_verbose_ts > 0 and (now_ts - self.last_verbose_ts) < self.cfg.verbose_every_sec:
            return
        self.last_verbose_ts = now_ts
        logger.info(
            "PURE snapshot "
            f"slug={payload.get('slug')} "
            f"t_left={float(payload.get('time_left_sec') or 0.0):.1f}s "
            f"spot={float(payload.get('spot') or 0.0):.2f} "
            f"strike={payload.get('strike')} "
            f"up_token={'yes' if payload.get('up_token_id') else 'no'} "
            f"down_token={'yes' if payload.get('down_token_id') else 'no'} "
            f"up_inst={'yes' if payload.get('up_instrument_id') else 'no'} "
            f"down_inst={'yes' if payload.get('down_instrument_id') else 'no'} "
            f"fair_up={float(payload.get('fair_up') or 0.0):.4f} "
            f"ask_up={payload.get('ask_up')} "
            f"ask_down={payload.get('ask_down')} "
            f"candidate={payload.get('candidate_side') or 'NONE'} "
            f"edge={payload.get('candidate_edge')}"
        )

    def _update_candidate_streak(self, payload: Dict[str, Any]) -> None:
        slug = str(payload.get("slug") or "")
        if not slug:
            return
        side = payload.get("candidate_side")
        now_ts = time.time()
        if not side:
            self.candidate_streaks.pop(slug, None)
            return

        streak = self.candidate_streaks.get(slug)
        if streak and streak.get("side") == side:
            streak["last_seen_ts"] = now_ts
            streak["last_payload"] = dict(payload)
            return

        self.candidate_streaks[slug] = {
            "side": side,
            "start_ts": now_ts,
            "last_seen_ts": now_ts,
            "last_payload": dict(payload),
        }

    def _candidate_matured_payload(self, slug: str) -> Optional[Dict[str, Any]]:
        streak = self.candidate_streaks.get(slug)
        if not streak:
            return None
        if (float(streak["last_seen_ts"]) - float(streak["start_ts"])) < self.cfg.paper_persistence_sec:
            return None
        payload = dict(streak.get("last_payload") or {})
        if not payload:
            return None
        return payload

    def _maybe_open_paper_trade(self, payload: Dict[str, Any]) -> None:
        if not self.cfg.paper_trade:
            return
        slug = str(payload.get("slug") or "")
        if not slug or slug in self.paper_positions_by_slug or slug in self.paper_settled_slugs:
            return

        matured = self._candidate_matured_payload(slug)
        if not matured:
            return

        side = str(matured.get("candidate_side") or "")
        if side not in {"BUY_UP", "BUY_DOWN"}:
            return
        entry_price = matured.get("ask_up") if side == "BUY_UP" else matured.get("ask_down")
        if entry_price is None:
            return
        entry_price = float(entry_price)
        if entry_price <= 0:
            return

        paper_id = f"paper_{slug}"
        paper_payload = {
            "paper_id": paper_id,
            "slug": slug,
            "side": side,
            "entry_price": entry_price,
            "entry_qty": float(self.cfg.paper_entry_qty),
            "entry_ts": _utc_now().isoformat(),
            "candidate_edge": matured.get("candidate_edge"),
            "fair_up": matured.get("fair_up"),
            "fair_down": matured.get("fair_down"),
            "spot": matured.get("spot"),
            "strike": matured.get("strike"),
            "market_end_ts": matured.get("market_end_ts"),
            "time_left_sec": matured.get("time_left_sec"),
            "paper_persistence_sec": self.cfg.paper_persistence_sec,
            "mode": "paper_trade",
        }
        self.paper_positions_by_slug[slug] = paper_payload
        self.db.log_strategy_event(self.run_id, "PAPER_TRADE_ENTRY", paper_payload)
        self.db.log_order_event(
            run_id=self.run_id,
            event_type="PAPER_ENTRY",
            client_order_id=paper_id,
            side=side,
            price=entry_price,
            qty=float(self.cfg.paper_entry_qty),
            status="filled",
            reason="paper_trade",
            instrument_id=matured.get("up_instrument_id") if side == "BUY_UP" else matured.get("down_instrument_id"),
            token_id=matured.get("up_token_id") if side == "BUY_UP" else matured.get("down_token_id"),
            expected_net_usdc=_safe_float(Decimal(str(matured.get("candidate_edge")))) if matured.get("candidate_edge") is not None else None,
            payload=paper_payload,
        )
        logger.info(
            f"PAPER entry slug={slug} side={side} px={entry_price:.4f} "
            f"edge={float(matured.get('candidate_edge') or 0.0):.4f} "
            f"t_left={float(matured.get('time_left_sec') or 0.0):.1f}s"
        )

    def _maybe_settle_paper_trades(self, force: bool = False) -> None:
        if not self.cfg.paper_trade:
            return
        now_ts = time.time()
        to_settle: List[str] = []
        for slug, position in self.paper_positions_by_slug.items():
            snapshot = self.latest_snapshot_by_slug.get(slug)
            if not snapshot:
                continue
            market_end_ts = snapshot.get("market_end_ts") or position.get("market_end_ts")
            if market_end_ts is None:
                continue
            try:
                market_end_ts = float(market_end_ts)
            except Exception:
                continue
            if force or now_ts >= (market_end_ts + self.cfg.paper_settle_grace_sec):
                to_settle.append(slug)

        for slug in to_settle:
            self._settle_paper_trade(slug)

    def _settle_paper_trade(self, slug: str) -> None:
        position = self.paper_positions_by_slug.get(slug)
        snapshot = self.latest_snapshot_by_slug.get(slug)
        if not position or not snapshot:
            return

        spot = snapshot.get("spot")
        strike = snapshot.get("strike")
        if spot is None or strike is None:
            return
        spot_f = float(spot)
        strike_f = float(strike)
        outcome = "UP" if spot_f > strike_f else "DOWN"
        side = str(position.get("side") or "")
        entry_price = float(position.get("entry_price") or 0.0)
        won = (side == "BUY_UP" and outcome == "UP") or (side == "BUY_DOWN" and outcome == "DOWN")
        exit_price = 1.0 if won else 0.0
        pnl = exit_price - entry_price
        settle_payload = {
            **position,
            "exit_ts": _utc_now().isoformat(),
            "settlement_spot": spot_f,
            "settlement_strike": strike_f,
            "settlement_outcome": outcome,
            "exit_price": exit_price,
            "realized_pnl": pnl,
            "won": won,
        }
        self.db.log_strategy_event(self.run_id, "PAPER_TRADE_SETTLEMENT", settle_payload)
        self.db.log_order_event(
            run_id=self.run_id,
            event_type="PAPER_SETTLEMENT",
            client_order_id=str(position.get("paper_id") or f"paper_{slug}"),
            side=side,
            price=exit_price,
            qty=float(position.get("entry_qty") or self.cfg.paper_entry_qty),
            status="filled",
            reason=outcome,
            instrument_id=None,
            token_id=None,
            expected_net_usdc=pnl,
            commission_usdc=0.0,
            payload=settle_payload,
        )
        logger.info(
            f"PAPER settlement slug={slug} side={side} outcome={outcome} "
            f"entry={entry_price:.4f} exit={exit_price:.4f} pnl={pnl:+.4f}"
        )
        self.paper_settled_slugs.add(slug)
        self.paper_positions_by_slug.pop(slug, None)
        self.candidate_streaks.pop(slug, None)

    def run(self) -> int:
        started_at = time.time()
        self._build_data_node()
        self.db.log_run_start(
            run_id=self.run_id,
            mode="probe",
            test_mode=True,
            maker_mode=False,
            notes={
                "script": "scripts/pure_signal_probe.py",
                "version": 1,
                "min_edge": float(self.cfg.min_edge),
                "min_entry_sec": self.cfg.min_entry_sec,
                "reduce_only_sec": self.cfg.reduce_only_sec,
                "force_flat_sec": self.cfg.force_flat_sec,
                "paper_trade": self.cfg.paper_trade,
                "paper_persistence_sec": self.cfg.paper_persistence_sec,
                "paper_settle_grace_sec": self.cfg.paper_settle_grace_sec,
            },
        )
        logger.info(f"Pure signal probe started run_id={self.run_id}")

        while not self.stop_requested:
            if self.cfg.duration_sec > 0:
                elapsed_sec = time.time() - started_at
                if elapsed_sec >= self.cfg.duration_sec:
                    break

            slug = self._resolve_market()
            if not slug:
                logger.warning("No BTC 15m slug resolved; retrying.")
                time.sleep(self.cfg.interval_sec)
                continue

            market = self._fetch_market(slug)
            if not market:
                logger.warning(f"No market payload for slug={slug}; retrying.")
                time.sleep(self.cfg.interval_sec)
                continue

            if slug != self.current_slug:
                self.current_slug = slug
                strike, strike_source = self._resolve_strike(market=market, slug=slug, latest_spot=None)
                self.db.log_strategy_event(
                    self.run_id,
                    "PURE_PROBE_MARKET",
                    {
                        "slug": slug,
                        "question": _extract_question(market),
                        "strike": _safe_float(strike),
                        "strike_source": strike_source,
                        "market_start_ts": extract_market_start_ts_from_slug(slug),
                    },
                )
                logger.info(f"PURE market slug={slug} strike={_safe_float(strike)} source={strike_source}")

            spot = self._fetch_spot()
            if spot is None or spot <= 0:
                logger.warning("Spot fetch unavailable; retrying.")
                time.sleep(self.cfg.interval_sec)
                continue

            sigma = self._estimate_sigma()
            payload = self._snapshot_payload(market=market, slug=slug, spot=spot, sigma=sigma)
            self.latest_snapshot_by_slug[slug] = dict(payload)
            self.db.log_strategy_event(self.run_id, "PURE_SIGNAL_SNAPSHOT", payload)
            self._log_verbose_snapshot(payload)
            self._update_candidate_streak(payload)
            self._log_candidate(payload)
            self._maybe_open_paper_trade(payload)
            self._maybe_settle_paper_trades()
            time.sleep(self.cfg.interval_sec)

        self._maybe_settle_paper_trades(force=True)
        self._shutdown_node()
        self.db.log_run_stop(self.run_id, notes={"stopped_at": _utc_now().isoformat()})
        logger.info(f"Pure signal probe stopped run_id={self.run_id}")
        return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Non-trading pure signal probe for BTC 15m Polymarket markets")
    ap.add_argument("--db", default="./logs/trade_journal.db", help="SQLite DB path")
    ap.add_argument("--interval-sec", type=float, default=2.0, help="Polling interval in seconds")
    ap.add_argument("--duration-sec", type=float, default=0.0, help="Run duration in seconds; 0 means until interrupted")
    ap.add_argument("--verbose", action="store_true", help="Print lightweight periodic snapshot summaries")
    ap.add_argument("--verbose-every-sec", type=float, default=30.0, help="Minimum seconds between verbose snapshot lines")
    ap.add_argument("--min-edge", type=float, default=0.04, help="Minimum edge to flag a candidate")
    ap.add_argument("--min-prob-band", type=float, default=0.08, help="Lower fair probability band for entries")
    ap.add_argument("--max-prob-band", type=float, default=0.92, help="Upper fair probability band for entries")
    ap.add_argument("--min-entry-sec", type=float, default=90.0, help="No new entries below this time remaining")
    ap.add_argument("--reduce-only-sec", type=float, default=30.0, help="Reference threshold for reduction-only window")
    ap.add_argument("--force-flat-sec", type=float, default=15.0, help="Reference threshold for forced flat window")
    ap.add_argument("--sigma-default", type=float, default=0.60, help="Fallback annualized sigma")
    ap.add_argument("--sigma-floor", type=float, default=0.20, help="Minimum annualized sigma")
    ap.add_argument("--sigma-ceiling", type=float, default=1.20, help="Maximum annualized sigma")
    ap.add_argument("--sigma-min-points", type=int, default=20, help="Minimum spot points before realized sigma is trusted")
    ap.add_argument("--sigma-window-points", type=int, default=120, help="Spot history window for realized sigma")
    ap.add_argument("--paper-trade", action="store_true", help="Enable one-paper-trade-per-market simulated entries")
    ap.add_argument("--paper-persistence-sec", type=float, default=10.0, help="Candidate must persist this many seconds before paper entry")
    ap.add_argument("--paper-settle-grace-sec", type=float, default=5.0, help="Wait this many seconds after market end before paper settlement")
    ap.add_argument("--paper-entry-qty", type=float, default=1.0, help="Simulated entry quantity for paper mode")
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    cfg = ProbeConfig(
        db_path=args.db,
        interval_sec=max(0.5, float(args.interval_sec)),
        duration_sec=max(0.0, float(args.duration_sec)),
        verbose=bool(args.verbose),
        verbose_every_sec=max(1.0, float(args.verbose_every_sec)),
        min_edge=Decimal(str(args.min_edge)),
        min_prob_band=Decimal(str(args.min_prob_band)),
        max_prob_band=Decimal(str(args.max_prob_band)),
        min_entry_sec=max(0.0, float(args.min_entry_sec)),
        reduce_only_sec=max(0.0, float(args.reduce_only_sec)),
        force_flat_sec=max(0.0, float(args.force_flat_sec)),
        sigma_default=Decimal(str(args.sigma_default)),
        sigma_floor=Decimal(str(args.sigma_floor)),
        sigma_ceiling=Decimal(str(args.sigma_ceiling)),
        sigma_min_points=max(2, int(args.sigma_min_points)),
        sigma_window_points=max(5, int(args.sigma_window_points)),
        paper_trade=bool(args.paper_trade),
        paper_persistence_sec=max(0.0, float(args.paper_persistence_sec)),
        paper_settle_grace_sec=max(0.0, float(args.paper_settle_grace_sec)),
        paper_entry_qty=max(0.0, float(args.paper_entry_qty)),
    )
    probe = PureSignalProbe(cfg)
    signal_hits = {"count": 0}

    def _handle_signal(signum: int, _frame: Any) -> None:
        signal_hits["count"] += 1
        logger.info(f"Signal received: {signum}; stopping probe.")
        probe.stop_requested = True
        probe._shutdown_node()
        if signal_hits["count"] >= 2:
            logger.warning("Second interrupt received; forcing exit.")
            os._exit(130)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    return probe.run()


if __name__ == "__main__":
    raise SystemExit(main())
