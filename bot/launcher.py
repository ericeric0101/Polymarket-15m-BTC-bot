import argparse
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx
from loguru import logger
import redis

from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket import (
    PolymarketDataClientConfig,
    PolymarketExecClientConfig,
)
from nautilus_trader.adapters.polymarket.factories import (
    PolymarketLiveDataClientFactory,
    PolymarketLiveExecClientFactory,
)
from nautilus_trader.config import (
    InstrumentProviderConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId

from bot.app_config import AppConfig
from bot.compat_patches import apply_compatibility_patches
from bot.market_discovery import (
    resolve_best_btc_15m_market,
    resolve_btc_15m_market_slugs,
    resolve_primary_btc_15m_instrument_ids,
)
from run_bot import (
    IntegratedBTCStrategy,
)


def _request_clob_l2_api_creds_direct(*, client, clob_host: str) -> Optional[Dict[str, str]]:
    """
    Direct HTTP fallback for CLOB API-key create/derive using py-clob's signer headers.
    Avoids depending on py_clob_client_v2.http_helpers transport behavior.
    """
    from py_clob_client_v2.client import CREATE_API_KEY, DERIVE_API_KEY
    from py_clob_client_v2.headers.headers import create_level_1_headers

    headers = dict(create_level_1_headers(client.signer))
    headers["Connection"] = "close"
    timeout = httpx.Timeout(10.0, connect=10.0)
    with httpx.Client(http2=False, timeout=timeout) as http:
        attempts = [
            ("create", "POST", f"{clob_host}{CREATE_API_KEY}"),
            ("derive", "GET", f"{clob_host}{DERIVE_API_KEY}"),
        ]
        last_error: Optional[str] = None
        for label, method, url in attempts:
            try:
                response = http.request(method, url, headers=headers)
                response.raise_for_status()
                payload = response.json()
            except Exception as e:
                last_error = f"{label}:{type(e).__name__}:{e}"
                continue

            api_key = payload.get("apiKey") or payload.get("key")
            api_secret = payload.get("secret")
            api_passphrase = payload.get("passphrase")
            if api_key and api_secret and api_passphrase:
                logger.info(f"Polymarket API credentials {label}d via direct HTTP fallback.")
                return {
                    "api_key": api_key,
                    "api_secret": api_secret,
                    "api_passphrase": api_passphrase,
                }
            last_error = f"{label}:incomplete_payload"

        if last_error:
            logger.warning(f"Direct CLOB auth fallback failed: {last_error}")
    return None


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
        resolved_funder = funder or ""
        if not resolved_funder:
            try:
                from py_clob_client_v2.client import ClobClient

                tmp_client = ClobClient(
                    clob_host,
                    chain_id,
                    key=private_key,
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
        from py_clob_client_v2.client import ClobClient
    except Exception as e:
        logger.error(f"py-clob-client-v2 not available for API credential derivation: {e}")
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
            try:
                derived = client.create_api_key()
            except Exception:
                derived = client.derive_api_key()
        except Exception as primary_error:
            logger.warning(f"py-clob credential derivation failed, trying direct HTTP fallback: {primary_error}")
            direct = _request_clob_l2_api_creds_direct(client=client, clob_host=clob_host.rstrip("/"))
            if direct is None:
                raise
            derived = direct
        d_key = derived.api_key if hasattr(derived, "api_key") else derived.get("api_key")
        d_secret = derived.api_secret if hasattr(derived, "api_secret") else derived.get("api_secret")
        d_pass = derived.api_passphrase if hasattr(derived, "api_passphrase") else derived.get("api_passphrase")
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
    try:
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_password = os.getenv("REDIS_PASSWORD")
        redis_username = os.getenv("REDIS_USERNAME")
        redis_client = redis.Redis(
            host=redis_host,
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=int(os.getenv("REDIS_DB", 2)),
            username=redis_username if redis_username else None,
            password=redis_password if redis_password else None,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_keepalive=True,
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


def run_integrated_bot(
    simulation: bool = True,
    enable_grafana: bool = True,
    test_mode: bool = False,
    enable_terminal_dashboard: bool = False,
):
    startup_verbose = os.getenv("STARTUP_VERBOSE", "0").strip().lower() in ("1", "true", "yes", "on")
    logger.info("Starting integrated Polymarket BTC 15-min trading bot.")

    redis_client = init_redis()
    if redis_client:
        try:
            redis_client.set("btc_trading:simulation_mode", "1" if simulation else "0")
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

        try:
            strategies = node.trader.strategies() if node else []
            for strat in strategies:
                if getattr(strat, "_rollover_requested_flag", False):
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
    app_config = AppConfig.from_env(enable_terminal_dashboard=enable_terminal_dashboard)
    if not enable_terminal_dashboard and app_config.observability.terminal_dashboard_enabled:
        enable_terminal_dashboard = True

    if enable_terminal_dashboard:
        logger.remove()
        log_dir = Path("logs/bot")
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(str(log_dir / "terminal_bot.log"), rotation="20 MB", retention="5 days", level="DEBUG")
        print(f"\n[INFO] Terminal dashboard enabled.")
        print(f"[INFO] Background logs are re-routed to: {log_dir}/terminal_bot.log")
        print(f"[INFO] Tip: Run 'tail -f {log_dir}/terminal_bot.log' in another terminal to view live logs.\n")

    compatibility = app_config.compatibility
    apply_compatibility_patches(
        project_root=Path(__file__).resolve().parent.parent,
        enabled=compatibility.auto_apply_patches,
        mode=compatibility.patch_mode,
    )

    if not run_preflight_checks(simulation=simulation):
        print("Preflight check failed. Startup aborted.")
        return

    if args.preflight_only:
        print("Preflight check passed. Exiting without starting bot.")
        return

    if not simulation:
        print("WARNING: LIVE TRADING MODE - REAL MONEY AT RISK!")
        confirm = input("Type 'yes' to continue: ")
        if confirm.lower() != "yes":
            print("Cancelled.")
            return

    run_integrated_bot(
        simulation=simulation,
        enable_grafana=enable_grafana,
        test_mode=test_mode,
        enable_terminal_dashboard=enable_terminal_dashboard,
    )
