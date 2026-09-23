import argparse
import asyncio
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx
from loguru import logger

from alert_watcher import AlertWatcher
from dashboard_state import DashboardState
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
from bot.process_lock import ProcessLock
from run_bot import (
    IntegratedBTCStrategy,
)
from telegram_bot import start_telegram_bot_thread
from telegram_notifier import TelegramNotifier


# The execution layer has a hard lower bound for nonzero balance checks, but a
# venue SELL must also meet the strategy's configured exchange minimum (5 by
# default).  Rollover protection uses the latter when available so dust that
# cannot form an order does not indefinitely prevent a stale-instrument refresh.
_MIN_ROLLOVER_PROTECTED_INVENTORY_SHARES = 0.01
_MARKET_DISCOVERY_RETRY_SEC = 15.0


class MarketDiscoveryUnavailable(RuntimeError):
    """Gamma has not published a usable current/future BTC market yet.

    This is an availability condition, not a strategy or credential failure.
    A node must never be built without instrument IDs, but the launcher should
    wait and retry rather than treat a transient Gamma publication gap as an
    unexpected node crash.
    """


def _install_fresh_main_thread_event_loop() -> None:
    try:
        current_loop = asyncio.get_event_loop_policy().get_event_loop()
    except Exception:
        current_loop = None

    if current_loop is not None and not current_loop.is_closed():
        return

    asyncio.set_event_loop(asyncio.new_event_loop())


def _live_exec_engine_config() -> LiveExecEngineConfig:
    # Polymarket can report a completed conditional-token fill with a quantity
    # slightly above the requested token amount. Rejecting that venue-confirmed
    # event leaves the local ledger at zero while the wallet already holds the
    # tokens, which can strand a TP.
    return LiveExecEngineConfig(qsize=6000, allow_overfills=True)


def _strategy_requested_rollover(node: Optional[TradingNode]) -> bool:
    """Read strategy rollover state before disposing the trading node."""
    if node is None:
        return False
    try:
        return any(
            getattr(strategy, "_rollover_requested_flag", False)
            for strategy in node.trader.strategies()
        )
    except Exception:
        return False


def _strategy_rollover_exposure_reasons(node: Optional[TradingNode]) -> list[str]:
    """Return exposure that makes an automatic node stop unsafe.

    Stopping a Nautilus node invokes the strategy shutdown hook, which cancels
    every tracked maker order. A scheduled operational refresh must therefore
    not stop a strategy while it owns conditional-token inventory or a live
    SELL that protects inventory whose local ledger may still be catching up.
    This is deliberately a fixed safety invariant, not an operator-tuned
    timeout or profile setting.
    """
    if node is None:
        return []
    try:
        strategies = list(node.trader.strategies())
    except Exception:
        return []

    reasons: list[str] = []
    terminal_states = ("REJECTED", "FILLED", "CANCELED", "CANCELLED")
    for index, strategy in enumerate(strategies):
        try:
            inventory = float(getattr(strategy, "inventory_delta_shares", 0) or 0)
        except (TypeError, ValueError):
            inventory = 0.0
        try:
            exchange_min = float(
                getattr(strategy, "maker_exchange_min_shares", _MIN_ROLLOVER_PROTECTED_INVENTORY_SHARES)
                or _MIN_ROLLOVER_PROTECTED_INVENTORY_SHARES
            )
        except (TypeError, ValueError):
            exchange_min = _MIN_ROLLOVER_PROTECTED_INVENTORY_SHARES
        protected_min = max(_MIN_ROLLOVER_PROTECTED_INVENTORY_SHARES, exchange_min)
        if inventory + 0.000001 >= protected_min:
            reasons.append(f"strategy[{index}]:inventory={inventory:.6f}")

        active_orders = getattr(strategy, "active_maker_orders", {})
        if not isinstance(active_orders, dict):
            continue
        for order_key, state in active_orders.items():
            if not isinstance(state, dict):
                continue
            side = str(state.get("side", "") or "").lower()
            if side != "sell" and not str(order_key).lower().startswith("sell:"):
                continue
            order = state.get("order")
            status = str(getattr(order, "status", "") or "").upper()
            if any(terminal in status for terminal in terminal_states):
                continue
            current_instruments = getattr(strategy, "current_market_instruments", None)
            order_instrument = str(state.get("instrument_id", "") or "")
            current_instrument_ids = {
                str(instrument) for instrument in (current_instruments or ()) if instrument is not None
            }
            is_prior_market_order = bool(
                current_instrument_ids
                and order_instrument
                and order_instrument not in current_instrument_ids
            )
            if side == "sell" and is_prior_market_order:
                # Once the selected market pair has moved on, an old-token
                # SELL cannot protect inventory in the current market. Keep
                # the normal market-transition cancel/reconcile path, but do
                # not let an orphaned old-market tracker wedge scheduled node
                # refresh indefinitely.
                continue
            reasons.append(f"strategy[{index}]:active_sell={order_key}")
    return reasons


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


def run_preflight_checks(simulation: bool) -> Optional[Dict[str, str]]:
    logger.info("Preflight check started.")

    auth = resolve_polymarket_auth()
    if not auth:
        logger.error("Polymarket auth resolution failed.")
        return None

    slugs = resolve_btc_15m_market_slugs()
    if not slugs:
        logger.error("Preflight failed: no BTC 15-min market slugs resolved")
        return None
    startup_verbose = os.getenv("STARTUP_VERBOSE", "0").strip().lower() in ("1", "true", "yes", "on")
    if startup_verbose:
        logger.info(f"Preflight market slugs: {slugs}")

    primary_slug, instrument_ids = resolve_best_btc_15m_market(slugs)
    if not primary_slug:
        logger.error("Preflight failed: no primary BTC 15-min slug selected")
        return None
    if not instrument_ids:
        logger.error(f"Preflight failed: no instrument IDs resolved for slug {primary_slug}")
        return None
    logger.info(f"Preflight market: primary_slug={primary_slug} instruments={len(instrument_ids)}")
    if startup_verbose:
        logger.info(f"Preflight instrument_ids: {[inst.value for inst in instrument_ids]}")

    mode_text = "SIMULATION" if simulation else "LIVE TRADING"
    logger.info(f"Preflight mode target: {mode_text}")
    logger.info("Polymarket auth check: OK")
    logger.info("PREFLIGHT CHECK PASSED")
    return auth


def run_integrated_bot(
    simulation: bool = True,
    test_mode: bool = True,
    enable_terminal_dashboard: bool = False,
    auth: Optional[Dict[str, str]] = None,
):
    startup_verbose = os.getenv("STARTUP_VERBOSE", "0").strip().lower() in ("1", "true", "yes", "on")
    logger.info("Starting integrated Polymarket BTC 15-min trading bot.")

    auto_rollover_enabled = os.getenv("AUTO_NODE_ROLLOVER_ENABLED", "1").strip().lower() not in ("0", "false", "no")
    # Node rollover is operational recovery, not strategy tuning. Preserve the
    # established hourly policy without carrying three profile readers.
    auto_rollover_sec = 3600
    auto_rollover_cooldown_sec = 3
    auto_rollover_max_failures = 5
    logger.info(
        "Startup config: "
        f"mode={'SIMULATION' if simulation else 'LIVE'} "
        f"terminal_dashboard={'on' if enable_terminal_dashboard else 'off'} "
        f"auto_rollover={'on' if auto_rollover_enabled else 'off'}({auto_rollover_sec}s)"
    )
    if startup_verbose:
        logger.info(
            f"Startup detail: unexpected_exit_restart=on "
            f"rollover_cooldown={auto_rollover_cooldown_sec}s max_failures={auto_rollover_max_failures}"
        )

    auth = auth or resolve_polymarket_auth()
    if not auth:
        raise RuntimeError("Cannot resolve Polymarket auth (provide PK or full API credentials).")

    dashboard_state = DashboardState(
        strike_price=0.0,
        spot_price=0.0,
        position_side=None,
        position_entry=None,
        position_qty=None,
        position_ask=None,
        current_market_price=0.0,
        trades=[],
        cumulative_pnl=0.0,
        usdc_balance=0.0,
        pol_balance=0.0,
        account_last_updated=datetime.now(timezone.utc),
    )
    telegram_notifier = TelegramNotifier()
    alert_watcher = AlertWatcher()
    telegram_thread = start_telegram_bot_thread(dashboard_state)
    if telegram_thread is not None:
        logger.info("Telegram bot controller started in background thread.")

    def _build_node_for_cycle(cycle_index: int) -> tuple[TradingNode, str]:
        _install_fresh_main_thread_event_loop()
        btc_slugs = resolve_btc_15m_market_slugs()
        if not btc_slugs:
            raise MarketDiscoveryUnavailable(
                "No BTC 15-min market slugs resolved; waiting for Gamma publication."
            )

        primary_slug, primary_instrument_ids = resolve_best_btc_15m_market(btc_slugs)
        if not primary_slug:
            raise MarketDiscoveryUnavailable(
                "No primary BTC 15-min slug selected; waiting for Gamma publication."
            )
        if not primary_instrument_ids:
            raise MarketDiscoveryUnavailable(
                f"No instrument IDs resolved for slug {primary_slug}; waiting for Gamma publication."
            )

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
            exec_engine=_live_exec_engine_config(),
            risk_engine=LiveRiskEngineConfig(
                bypass=simulation,
            ),
            data_clients={POLYMARKET: poly_data_cfg},
            exec_clients={POLYMARKET: poly_exec_cfg},
        )

        strategy = IntegratedBTCStrategy(
            test_mode=test_mode,
            selected_slug=primary_slug,
            enable_terminal_dashboard=enable_terminal_dashboard,
            dashboard_state=dashboard_state,
            telegram_notifier=telegram_notifier,
            alert_watcher=alert_watcher,
        )

        logger.info("Building Nautilus node...")
        node = TradingNode(config=config)
        node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
        node.add_exec_client_factory(POLYMARKET, PolymarketLiveExecClientFactory)
        node.trader.add_strategy(strategy)
        node.build()
        # Strategies are Actors and do not have a public back-reference to the
        # TradingNode. Give lifecycle/watchdog recovery an explicit stop hook so
        # a requested rollover actually returns node.run() to this launcher.
        strategy._request_node_stop_callback = node.stop
        logger.info("Nautilus node built successfully")
        return node, primary_slug

    cycle_idx = 0
    consecutive_failures = 0
    user_stopped = False
    retry_delay_sec = auto_rollover_cooldown_sec

    while True:
        cycle_idx += 1
        cycle_started_at = time.time()
        retry_delay_sec = auto_rollover_cooldown_sec
        node: Optional[TradingNode] = None
        rollover_requested = threading.Event()
        strategy_requested_rollover = False
        node_run_returned = False
        rollover_stop = threading.Event()
        rollover_thread: Optional[threading.Thread] = None

        try:
            node, cycle_slug = _build_node_for_cycle(cycle_idx)
            logger.info(f"Cycle {cycle_idx} ready (slug={cycle_slug})")

            if auto_rollover_enabled:
                def _rollover_worker() -> None:
                    wait_sec = auto_rollover_sec
                    last_deferred_log_ts = 0.0
                    while not rollover_stop.wait(wait_sec):
                        exposure_reasons = _strategy_rollover_exposure_reasons(node)
                        if exposure_reasons:
                            now_ts = time.time()
                            if now_ts - last_deferred_log_ts >= 60.0:
                                logger.warning(
                                    "Auto node rollover deferred: preserving live exit protection "
                                    f"({'; '.join(exposure_reasons)})."
                                )
                                last_deferred_log_ts = now_ts
                            # Recheck frequently after the original timer expires. The
                            # normal exit lifecycle, not this operational timer,
                            # decides when the protected position is gone.
                            wait_sec = 5.0
                            continue

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
                        return

                rollover_thread = threading.Thread(target=_rollover_worker, daemon=True)
                rollover_thread.start()

            logger.info(f"Bot cycle {cycle_idx} starting...")
            node.run()
            node_run_returned = True
        except KeyboardInterrupt:
            user_stopped = True
            logger.info("Shutdown requested by user.")
        except MarketDiscoveryUnavailable as e:
            # Gamma can have a short gap between publishing the deterministic
            # 15-minute slug and serving its token IDs.  No node was built, so
            # no order or exit is affected; retry discovery on a bounded,
            # intentional cadence without consuming the crash-failure budget.
            rollover_requested.set()
            retry_delay_sec = _MARKET_DISCOVERY_RETRY_SEC
            logger.warning(
                f"Node cycle {cycle_idx} deferred: {e} "
                f"Retrying market discovery in {retry_delay_sec:.0f}s."
            )
        except Exception as e:
            consecutive_failures += 1
            recent_errors = list(dashboard_state.recent_errors)[-19:]
            recent_errors.append((datetime.now(timezone.utc), f"Node cycle {cycle_idx} failed: {e}"))
            dashboard_state.update(recent_errors=recent_errors)
            logger.exception(f"Node cycle {cycle_idx} failed: {e}")
        finally:
            rollover_stop.set()
            if rollover_thread and rollover_thread.is_alive():
                rollover_thread.join(timeout=1)
            strategy_requested_rollover = _strategy_requested_rollover(node)
            if strategy_requested_rollover:
                rollover_requested.set()
                logger.info("Strategy requested rollover (stale instruments)")
            if node_run_returned:
                if rollover_requested.is_set():
                    consecutive_failures = 0
                else:
                    # A node that returns without an intended rollover is
                    # retryable, but must consume the same bounded failure
                    # budget as an exception so a clean-return loop cannot
                    # restart forever.
                    consecutive_failures += 1
            if node is not None:
                try:
                    node.dispose()
                except Exception as e:
                    logger.warning(f"Node dispose raised: {e}")
            _install_fresh_main_thread_event_loop()
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
            logger.warning(
                f"Node cycle exited without rollover request (runtime={run_sec}s). "
                "Attempting automatic rebuild."
            )
        time.sleep(retry_delay_sec)


def acquire_live_process_lock() -> ProcessLock | None:
    """Prevent two live launchers on the same host from sharing one wallet."""
    lock_path = os.getenv("LIVE_PROCESS_LOCK_PATH", "/tmp/polymarket-btc-15m-live.lock")
    lock = ProcessLock(lock_path)
    if lock.acquire():
        return lock
    logger.error(
        f"Another local live bot process already holds {lock_path}. "
        "Refusing to start a second wallet writer."
    )
    return None


def main():
    parser = argparse.ArgumentParser(description="Integrated BTC 15-Min Trading Bot")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run in LIVE mode (real money at risk!). Default is simulation."
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
    # The CLOB execution client is always live-capable. Dry-run must therefore
    # be enabled explicitly for every invocation that did not opt into --live.
    test_mode = bool(args.test_mode or not args.live)
    enable_terminal_dashboard = args.terminal_dashboard
    app_config = AppConfig.from_env(enable_terminal_dashboard=enable_terminal_dashboard)

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

    auth = run_preflight_checks(simulation=simulation)
    if auth is None:
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

    live_lock = acquire_live_process_lock() if not simulation else None
    if not simulation and live_lock is None:
        return
    try:
        run_integrated_bot(
            simulation=simulation,
            test_mode=test_mode,
            enable_terminal_dashboard=enable_terminal_dashboard,
            auth=auth,
        )
    finally:
        if live_lock is not None:
            live_lock.release()
