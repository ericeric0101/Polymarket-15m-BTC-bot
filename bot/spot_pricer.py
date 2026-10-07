"""
bot/spot_pricer.py – SpotPricerMixin

Extracted from run_bot.py (L1575-L1942).
Contains BTC spot price fetching, Binance WebSocket management,
strike resolution, and fair probability calculation.

IntegratedBTCStrategy inherits this mixin so all self.* references remain valid.
"""
from __future__ import annotations

import asyncio
import math
import time
from decimal import Decimal
from typing import Any, Optional

from loguru import logger

from bot.market_data import (
    estimate_external_spot_sigma_annualized,
    extract_market_start_ts_from_slug,
    extract_strike_from_question,
    fetch_coinbase_spot_sync,
    record_external_spot_observation,
    resolve_opening_strike_from_history,
    fetch_binance_open_price_sync,
    fetch_gamma_market_by_slug,
    fetch_crypto_price_to_beat,
)
from bot.price_streams import (
    BINANCE_AGGTRADE_WS_URL,
    POLYMARKET_LIVE_WS_URL,
    RTDS_APPLICATION_HEARTBEAT_TEXT,
    build_polymarket_chainlink_subscribe_payload,
    chainlink_observation_ts,
    extract_binance_aggtrade_tick,
    extract_polymarket_chainlink_tick,
    rtds_application_heartbeat_due,
    rtds_silent_stall_due,
)
from bot.forecast_state import ForecastState, build_forecast_state
from execution.maker_engine import MakerEngine


class SpotPricerMixin:
    """Mixin providing BTC spot price, Binance WS, and fair probability logic."""

    _AUTHORITATIVE_STRIKE_SOURCES = {
        "polymarket_crypto_price_twap_open",
    }
    _MARKET_STRIKE_INITIAL_RETRY_INTERVAL_SEC = 3.0
    _MARKET_STRIKE_INITIAL_RESOLUTION_WINDOW_SEC = 30.0
    # Existing freshness window used by the external/raw spot path diagnostics.
    _RAW_SPOT_FRESHNESS_SEC = 10.0

    @staticmethod
    def _twap_research_market_context(
        now_ts: float, *, current_slug: str | None, current_end: float | None,
    ) -> tuple[str, float]:
        """Attribute research ticks to the wall-clock 15-minute market.

        Market discovery/subscription can lag a boundary. Research sampling is
        independent of that lifecycle, so a stale runtime slug must not move
        opening ticks into the prior market. This context is shadow-only.
        """
        market_start = int(float(now_ts) // 900) * 900
        wall_slug = f"btc-updown-15m-{market_start}"
        runtime_slug = str(current_slug or "")
        try:
            runtime_start = int(runtime_slug.rsplit("-", 1)[-1])
        except (TypeError, ValueError):
            runtime_start = None
        if runtime_slug == wall_slug and runtime_start == market_start:
            try:
                end = float(current_end)
            except (TypeError, ValueError):
                end = float(market_start + 900)
            if market_start < end <= market_start + 900:
                return runtime_slug, end
        return wall_slug, float(market_start + 900)

    def _is_twap_spot_source(self, source: str) -> bool:
        return str(source or "").startswith("polymarket_chainlink_twap_")

    def _maybe_log_strike_pending_state(
        self,
        *,
        slug: str,
        provisional_strike: Optional[Decimal],
        provisional_source: str,
    ) -> None:
        slug_key = str(slug or "")
        if not slug_key:
            return
        state_key = (
            f"pending:{provisional_source or 'none'}:"
            f"{float(provisional_strike):.2f}"
            if provisional_strike is not None
            else "pending:none"
        )
        last_state = str(getattr(self, "_strike_pending_log_state_by_slug", {}).get(slug_key, ""))
        if last_state == state_key:
            return
        self._strike_pending_log_state_by_slug[slug_key] = state_key
        logger.info(
            f"[STRIKE] Opening strike pending for slug={slug_key}; "
            f"provisional_source={provisional_source or 'none'} "
            f"provisional={'None' if provisional_strike is None else f'${float(provisional_strike):.2f}'}"
        )

    # ------------------------------------------------------------------
    # Polymarket Chainlink WebSocket
    # ------------------------------------------------------------------

    def _start_polymarket_chainlink_ws(self) -> None:
        import threading
        if (
            self._polymarket_chainlink_ws_thread is not None
            and self._polymarket_chainlink_ws_thread.is_alive()
        ):
            return
        self._polymarket_chainlink_ws_stop_event.clear()
        self._polymarket_chainlink_ws_thread = threading.Thread(
            target=self._polymarket_chainlink_ws_loop,
            name="polymarket-chainlink-ws",
            daemon=True,
        )
        self._polymarket_chainlink_ws_thread.start()
        logger.info("Polymarket Chainlink WebSocket thread started")

    def _polymarket_chainlink_ws_loop(self) -> None:
        import json as _json
        import websockets.sync.client as ws_sync  # type: ignore

        reconnect_delay = 1.0
        max_reconnect_delay = 30.0
        while not self._polymarket_chainlink_ws_stop_event.is_set():
            try:
                with ws_sync.connect(
                    POLYMARKET_LIVE_WS_URL,
                    close_timeout=5,
                    ping_interval=None,
                    ping_timeout=None,
                ) as ws:
                    reconnect_delay = 1.0
                    self._polymarket_chainlink_twap_reconnect_count = (
                        int(getattr(self, "_polymarket_chainlink_twap_reconnect_count", 0)) + 1
                    )
                    use_twap = bool(getattr(self, "polymarket_chainlink_twap_enabled", True))
                    twap_window = int(getattr(self, "polymarket_chainlink_twap_window_sec", 60) or 60)
                    twap_symbol = str(getattr(self, "polymarket_chainlink_twap_symbol", "btc/usd") or "btc/usd")
                    subscribe_payload = build_polymarket_chainlink_subscribe_payload(
                        use_twap=use_twap,
                        window_seconds=twap_window,
                        symbol=twap_symbol,
                        include_spot=use_twap,
                    )
                    mode = f"TWAP {twap_window}s" if use_twap else "spot"
                    logger.info(f"✓ Polymarket Chainlink WS connected ({mode})")
                    ws.send(_json.dumps(subscribe_payload))
                    connected_monotonic = time.monotonic()
                    connection_epoch = int(
                        getattr(self, "_polymarket_chainlink_twap_connection_epoch", 0)
                    ) + 1
                    self._polymarket_chainlink_twap_connection_epoch = connection_epoch
                    self._polymarket_chainlink_twap_connection_monotonic = connected_monotonic
                    self._polymarket_chainlink_twap_pending_recovery = True
                    last_application_ping_monotonic = 0.0
                    last_valid_twap_monotonic = connected_monotonic
                    self._db_strategy_event(
                        "POLYMARKET_TWAP_WS_CONNECTED",
                        {
                            "mode": mode,
                            "connection_epoch": connection_epoch,
                            "reconnect_count": self._polymarket_chainlink_twap_reconnect_count,
                            "previous_disconnect_age_sec": (
                                max(0.0, connected_monotonic - float(
                                    getattr(self, "_polymarket_chainlink_twap_last_disconnect_monotonic", 0.0) or 0.0
                                ))
                                if float(getattr(self, "_polymarket_chainlink_twap_last_disconnect_monotonic", 0.0) or 0.0) > 0
                                else None
                            ),
                            "silence_reconnect_sec": float(
                                getattr(self, "polymarket_chainlink_twap_silence_reconnect_sec", 15.0)
                            ),
                        },
                    )
                    while not self._polymarket_chainlink_ws_stop_event.is_set():
                        now_monotonic = time.monotonic()
                        if rtds_application_heartbeat_due(
                            now_monotonic=now_monotonic,
                            last_sent_monotonic=last_application_ping_monotonic,
                        ):
                            # RTDS requires this application frame; websocket
                            # protocol ping is not a substitute.
                            ws.send(RTDS_APPLICATION_HEARTBEAT_TEXT)
                            last_application_ping_monotonic = now_monotonic
                        max_silence_sec = float(
                            getattr(self, "polymarket_chainlink_twap_silence_reconnect_sec", 15.0)
                        )
                        if use_twap and rtds_silent_stall_due(
                            now_monotonic=now_monotonic,
                            last_valid_twap_monotonic=last_valid_twap_monotonic,
                            max_silence_sec=max_silence_sec,
                        ):
                            self._polymarket_chainlink_twap_silent_stall_count = (
                                int(getattr(self, "_polymarket_chainlink_twap_silent_stall_count", 0)) + 1
                            )
                            silence_sec = now_monotonic - last_valid_twap_monotonic
                            self._db_strategy_event(
                                "POLYMARKET_TWAP_SILENT_STALL",
                                {
                                    "silence_sec": silence_sec,
                                    "max_silence_sec": max_silence_sec,
                                    "silent_stall_count": self._polymarket_chainlink_twap_silent_stall_count,
                                    "reconnect_count": self._polymarket_chainlink_twap_reconnect_count,
                                    "last_twap_received_age_sec": max(
                                        0.0,
                                        time.time()
                                        - float(getattr(self, "_polymarket_chainlink_twap_price_ts", 0.0) or 0.0),
                                    ),
                                },
                            )
                            raise ConnectionError(
                                f"RTDS silent TWAP stall for {silence_sec:.1f}s; reconnecting"
                            )
                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        tick = extract_polymarket_chainlink_tick(raw)
                        if tick is None:
                            continue
                        # Preserve the raw Chainlink stream separately.  The
                        # TWAP stream remains the trading reference.
                        if not self._is_twap_spot_source(tick.source):
                            raw_source_ts = chainlink_observation_ts(tick)
                            self._polymarket_chainlink_price = tick.price
                            self._polymarket_chainlink_price_ts = tick.received_at_ts
                            self._polymarket_chainlink_price_observation_ts = raw_source_ts
                            self._record_polymarket_chainlink_receipt_observation(
                                tick.price, tick.received_at_ts,
                            )
                            self._polymarket_chainlink_event_ts_ms = tick.updated_at_ms
                            self._update_btc_trend_price(
                                tick.price,
                                tick.received_at_ts,
                                source="polymarket_chainlink_ws",
                            )
                        if self._is_twap_spot_source(tick.source):
                            tick_monotonic = time.monotonic()
                            last_valid_twap_monotonic = tick_monotonic
                            self._polymarket_chainlink_twap_price = tick.price
                            # Preserve receipt and source clocks separately.
                            # RTDS documents payload.timestamp as Chainlink's
                            # observation time and mandates it for freshness.
                            self._polymarket_chainlink_twap_price_ts = tick.received_at_ts
                            self._polymarket_chainlink_twap_event_ts_ms = tick.updated_at_ms
                            # Bind the source time once to this exact tick.
                            # A prior version assigned this after the shadow
                            # observer, so tick N could be paired with tick
                            # N-1's timestamp (and tick 1 raised locally).
                            observation_ts = chainlink_observation_ts(tick)
                            self._polymarket_chainlink_twap_observation_ts = observation_ts
                            self._polymarket_chainlink_twap_window_sec = tick.window_seconds
                            twap_shadow = getattr(self, "twap_forward_shadow", None)
                            if twap_shadow is not None:
                                try:
                                    now_wall = time.time()
                                    runtime_slug = str(getattr(self, "current_market_slug", "") or "")
                                    slug, research_market_end = self._twap_research_market_context(
                                        float(observation_ts or now_wall),
                                        current_slug=runtime_slug,
                                        current_end=getattr(self, "current_market_end_timestamp", None),
                                    )
                                    strike = getattr(self, "market_strike_cache_by_slug", {}).get(slug)
                                    time_left = max(0.0, research_market_end - now_wall)
                                    research_pair = self._research_market_quote_instruments(
                                        slug=slug, runtime_slug=runtime_slug,
                                    )
                                    if research_pair is not None:
                                        shadow_inputs = self._settlement_probability_shadow_inputs(
                                            slug=slug, official_twap=tick.price, strike=strike,
                                            time_left_sec=time_left,
                                            now_ts=now_wall, source_observed_ts=observation_ts,
                                        )
                                    else:
                                        # Until the CLOB market pair rolls over,
                                        # the current quote cache belongs to the
                                        # old market. Never attach it to this
                                        # market's opening research observations.
                                        shadow_inputs = {
                                            "probability_model_version": "existing_twap_average_approx_v1",
                                            "probability_model_mode": "UNAVAILABLE",
                                            "data_quality": "market_runtime_rollover_pending",
                                            "official_current_twap": float(tick.price),
                                            "path_spot": None, "path_spot_source": "unavailable",
                                            "path_spot_age_sec": None,
                                            "settlement_state_side": "UNKNOWN",
                                            "path_spot_side": "UNKNOWN",
                                            "settlement_path_side_divergence": None,
                                            "currently_dominant_side": "UNKNOWN",
                                            "required_move_mode": "UNAVAILABLE",
                                            "sigma_ex_market_age_sec": None,
                                            "sigma_ex_market_fresh": False,
                                            "market_bbo_up_age_sec": None,
                                            "market_bbo_down_age_sec": None,
                                            "market_bbo_up_unavailable_reason": "market_runtime_rollover_pending",
                                            "market_bbo_down_unavailable_reason": "market_runtime_rollover_pending",
                                            "p_up_ex_market": None, "p_down_ex_market": None,
                                            "required_move_usd": None, "required_move_bps": None,
                                            "required_move_sigma": None,
                                        }
                                    twap_shadow.observe(
                                        slug=slug, now_ts=now_wall,
                                        source_ts=float(observation_ts or now_wall),
                                        fast_spot=getattr(self, "_binance_ws_price", None), official_twap=tick.price,
                                        strike=strike,
                                        time_left_sec=time_left,
                                        settlement_diagnostics=shadow_inputs,
                                    )
                                except Exception as exc:
                                    logger.debug(f"TWAP forward shadow observation failed: {exc}")
                            research_observer = getattr(self, "_observe_live_strike_reference", None)
                            if callable(research_observer) and observation_ts is not None:
                                try:
                                    research_observer(
                                        spot=tick.price,
                                        source=f"polymarket_chainlink_twap_{tick.window_seconds}s_ws",
                                        observed_ts=float(observation_ts),
                                    )
                                except Exception:
                                    # Research telemetry cannot interfere with feed ingestion.
                                    pass
                            if bool(getattr(self, "_polymarket_chainlink_twap_pending_recovery", False)):
                                disconnected_at = float(
                                    getattr(self, "_polymarket_chainlink_twap_last_disconnect_monotonic", 0.0) or 0.0
                                )
                                self._polymarket_chainlink_twap_pending_recovery = False
                                self._db_strategy_event(
                                    "POLYMARKET_TWAP_WS_RECOVERED",
                                    {
                                        "connection_epoch": connection_epoch,
                                        "reconnect_count": self._polymarket_chainlink_twap_reconnect_count,
                                        "first_valid_twap_after_connect_sec": max(0.0, tick_monotonic - connected_monotonic),
                                        "feed_unavailable_sec": (
                                            max(0.0, tick_monotonic - disconnected_at)
                                            if disconnected_at > 0 else None
                                        ),
                                        "twap_source": tick.source,
                                        "source_event_ts_ms": tick.updated_at_ms,
                                    },
                                )
                        if not self._is_twap_spot_source(tick.source):
                            self._record_polymarket_chainlink_observation(
                                tick.price,
                                chainlink_observation_ts(tick),
                                received_ts=tick.received_at_ts,
                            )
            except Exception as exc:
                disconnected_monotonic = time.monotonic()
                connected_monotonic = float(
                    getattr(self, "_polymarket_chainlink_twap_connection_monotonic", 0.0) or 0.0
                )
                last_valid_twap_monotonic = float(locals().get("last_valid_twap_monotonic", 0.0) or 0.0)
                self._polymarket_chainlink_twap_last_disconnect_monotonic = disconnected_monotonic
                self._db_strategy_event(
                    "POLYMARKET_TWAP_WS_DISCONNECTED",
                    {
                        "connection_epoch": int(getattr(self, "_polymarket_chainlink_twap_connection_epoch", 0)),
                        "error_type": type(exc).__name__,
                        "error_detail": str(exc)[:300],
                        "connected_duration_sec": (
                            max(0.0, disconnected_monotonic - connected_monotonic)
                            if connected_monotonic > 0 else None
                        ),
                        "last_valid_twap_age_sec": (
                            max(0.0, disconnected_monotonic - last_valid_twap_monotonic)
                            if last_valid_twap_monotonic > 0 else None
                        ),
                        "will_retry_after_sec": reconnect_delay,
                    },
                )
                logger.debug(
                    f"Polymarket Chainlink WS error: {exc}; reconnect in {reconnect_delay:.0f}s"
                )
                self._polymarket_chainlink_ws_stop_event.wait(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

    # ------------------------------------------------------------------
    # Binance WebSocket
    # ------------------------------------------------------------------

    def _update_btc_trend_price(
        self,
        price: Decimal,
        ts: float,
        *,
        source: str,
    ) -> None:
        """Feed the BTC trend EMA from the preferred fresh raw-spot source.

        Binance aggTrade remains the primary microstructure feed.  Its Futures
        stream can connect yet remain silent in some network regions, so a
        fresh raw Chainlink spot tick is used only while Binance is unavailable.
        The 60-second TWAP is deliberately excluded: it is a settlement
        reference, not a momentum input.
        """
        if price <= 0 or ts <= 0:
            return
        now_ts = time.time()
        primary_stale_sec = float(
            getattr(self, "side_signal_btc_trend_primary_stale_sec", 10.0) or 10.0
        )
        binance_ts = float(getattr(self, "_binance_ws_price_ts", 0.0) or 0.0)
        binance_fresh = binance_ts > 0 and (now_ts - binance_ts) <= primary_stale_sec
        if source != "binance_ws" and binance_fresh:
            return

        signal_engine = getattr(self, "_signal_engine", None)
        if signal_engine is None:
            return
        try:
            signal_engine.update_btc_price(price, ts)
        except (ArithmeticError, TypeError, ValueError) as exc:
            logger.warning(f"BTC trend tick rejected source={source}: {exc}")
            return

        previous_source = str(getattr(self, "_btc_trend_source", "unavailable") or "unavailable")
        self._btc_trend_source = source
        self._btc_trend_source_ts = ts
        self._btc_trend_source_price = price
        if previous_source != source:
            logger.info(f"BTC trend source switched: {previous_source} -> {source}")

    def _start_binance_ws(self) -> None:
        """Start a background thread that streams BTC price from Binance WebSocket."""
        import threading
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

    def _observe_btc_1s_history(self, tick: Any) -> None:
        """Passively forward an aggTrade to history capture; never raise to live feed."""
        collector = getattr(self, "btc_1s_history_collector", None)
        if collector is None:
            return
        try:
            collector.observe_aggtrade(
                price=tick.price,
                source_ts_ms=getattr(tick, "exchange_trade_ts_ms", None) or tick.updated_at_ms,
                received_ts=tick.received_at_ts,
                quantity=getattr(tick, "quantity", None),
            )
        except Exception as exc:
            logger.debug(f"BTC 1s history observer skipped: {exc}")

    def _binance_ws_loop(self) -> None:
        """
        Persistent WebSocket connection to Binance Spot for BTC/USDT aggTrade.
        Per Binance docs:
        - Base URL: wss://stream.binance.com:9443
        - Stream: /ws/btcusdt@aggTrade
        - Connection valid for max 24 hours → reconnect at 23h
        - Server pings every 3 min; must pong within 10 min
        - Max 10 incoming messages/sec
        """
        import websockets.sync.client as ws_sync  # type: ignore

        url = BINANCE_AGGTRADE_WS_URL
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
                    logger.info("✓ Binance Spot WS connected (btcusdt@aggTrade)")
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
                            tick = extract_binance_aggtrade_tick(raw)
                            if tick is not None:
                                self._binance_ws_price = tick.price
                                self._binance_ws_price_ts = tick.received_at_ts
                                source_ms = getattr(tick, "exchange_trade_ts_ms", None) or tick.updated_at_ms
                                self._binance_ws_price_source_ts = float(source_ms) / 1000.0
                                # Small bounded in-memory research buffer only;
                                # no disk access occurs on the feed callback.
                                history = getattr(self, "_prediction_btc_research_history", None)
                                if history is not None:
                                    history.append((self._binance_ws_price_source_ts,
                                                    float(tick.price), float(tick.received_at_ts)))
                                self._update_btc_trend_price(
                                    tick.price,
                                    tick.received_at_ts,
                                    source="binance_ws",
                                )
                                self._observe_btc_1s_history(tick)
                        except Exception as exc:
                            logger.warning(f"Binance WS tick processing failed: {exc}")
            except Exception as e:
                logger.debug(f"Binance WS error: {e}; reconnect in {reconnect_delay:.0f}s")
                self._binance_ws_stop_event.wait(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

    # ------------------------------------------------------------------
    # Spot price fetch
    # ------------------------------------------------------------------

    async def _fetch_external_spot_price(self) -> Optional[Decimal]:
        """
        Get BTC reference spot price.
        Primary: Polymarket Chainlink TWAP WS.
        Fallback: Binance WS.
        Last resort: Coinbase HTTP.
        """
        max_delta_abs = Decimal(
            str(getattr(self, "external_spot_source_delta_abs_max_usd", Decimal("0")) or "0")
        )
        binance_fresh = False
        binance_price = None
        if self._binance_ws_price is not None:
            binance_age = time.time() - self._binance_ws_price_ts
            if binance_age < 10.0:
                binance_fresh = True
                binance_price = self._binance_ws_price
        require_twap = bool(getattr(self, "require_twap_reference_spot", True))
        twap_price = getattr(self, "_polymarket_chainlink_twap_price", None)
        twap_received_ts = float(getattr(self, "_polymarket_chainlink_twap_price_ts", 0.0) or 0.0)
        twap_ts = float(getattr(self, "_polymarket_chainlink_twap_observation_ts", 0.0) or 0.0)
        twap_window = int(getattr(self, "_polymarket_chainlink_twap_window_sec", 0) or getattr(self, "polymarket_chainlink_twap_window_sec", 60) or 60)

        # Primary: Polymarket Chainlink TWAP WS if fresh.
        if twap_price is not None:
            age = time.time() - twap_ts if twap_ts > 0 else float("inf")
            if 0.0 <= age < 10.0:
                price = twap_price
                if bool(getattr(self, "_twap_reference_degraded", False)):
                    logger.info("Polymarket Chainlink TWAP recovered; new entries may resume")
                self._twap_reference_degraded = False
                if (
                    binance_fresh
                    and binance_price is not None
                    and max_delta_abs > 0
                    and abs(price - binance_price) > max_delta_abs
                ):
                    last_warn_ts = float(getattr(self, "_last_spot_source_delta_warn_ts", 0.0) or 0.0)
                    now_ts = time.time()
                    if now_ts - last_warn_ts >= 30.0:
                        logger.info(
                            "TWAP/spot divergence observed; retaining settlement-aligned TWAP: "
                            f"twap={float(price):.2f} binance={float(binance_price):.2f} "
                            f"delta={float(price - binance_price):+.2f} "
                            f"guard={float(max_delta_abs):.2f}"
                        )
                        self._last_spot_source_delta_warn_ts = now_ts
                self.latest_external_spot_source = f"polymarket_chainlink_twap_{twap_window}s_ws"
                self.latest_external_spot_source_ts = twap_ts
                if not getattr(self, "_logged_first_spot", False):
                    logger.info(f"✓ First BTC reference spot via Polymarket Chainlink {twap_window}s TWAP WS: ${price:,.2f}")
                    self._logged_first_spot = True
                return price
            receipt_age = time.time() - twap_received_ts if twap_received_ts > 0 else float("inf")
            logger.debug(
                "Polymarket Chainlink TWAP observation stale or missing "
                f"(observation_age={age:.1f}s, receipt_age="
                f"{receipt_age:.1f}s)"
            )

        if require_twap:
            # The final outcome is TWAP-based, but pausing the whole signal
            # pipeline during a short public-feed hiccup is worse than clearly
            # marking a degraded reference source. Snapshot fallback remains
            # disabled in this strict mode; Binance is the next source below.
            now_ts = time.time()
            self._twap_reference_degraded = True
            last_warn_ts = float(getattr(self, "_last_twap_stale_fallback_warn_ts", 0.0) or 0.0)
            if now_ts - last_warn_ts >= 30.0:
                logger.warning(
                    "Polymarket Chainlink TWAP stale; using degraded external fallback until TWAP recovers"
                )
                self._last_twap_stale_fallback_warn_ts = now_ts
                if hasattr(self, "_db_strategy_event"):
                    try:
                        self._db_strategy_event(
                            "TWAP_REFERENCE_DEGRADED",
                            {
                                "twap_age_sec": (time.time() - twap_ts) if twap_ts > 0 else None,
                                "twap_received_age_sec": (
                                    time.time() - twap_received_ts if twap_received_ts > 0 else None
                                ),
                                "fallback_preference": "binance_ws_then_coinbase_http",
                            },
                        )
                    except Exception:
                        pass

        # Legacy snapshot is allowed only when strict TWAP alignment is disabled.
        if not require_twap and self._polymarket_chainlink_price is not None:
            age = time.time() - self._polymarket_chainlink_price_ts
            if age < 10.0:
                price = self._polymarket_chainlink_price
                self.latest_external_spot_source = "polymarket_chainlink_ws"
                self.latest_external_spot_source_ts = self._polymarket_chainlink_price_ts
                if not getattr(self, "_logged_first_spot", False):
                    logger.info(f"✓ First BTC reference spot via Polymarket Chainlink snapshot WS fallback: ${price:,.2f}")
                    self._logged_first_spot = True
                return price
            logger.debug(f"Polymarket Chainlink snapshot WS price stale ({age:.1f}s), falling back")

        # Fallback: Binance WS price if fresh.
        if self._binance_ws_price is not None:
            age = time.time() - self._binance_ws_price_ts
            if age < 10.0:
                price = self._binance_ws_price
                self.latest_external_spot_source = "binance_ws"
                self.latest_external_spot_source_ts = self._binance_ws_price_ts
                if not getattr(self, "_logged_first_spot", False):
                    logger.info(f"✓ First BTC reference spot via Binance WS fallback: ${price:,.2f}")
                    self._logged_first_spot = True
                return price
            logger.debug(f"Binance WS price stale ({age:.1f}s), falling back to HTTP")

        # Fallback: Coinbase HTTP
        price = await asyncio.to_thread(self._fetch_coinbase_spot_sync)
        if price is not None and price > 0:
            self.latest_external_spot_source = "coinbase_http"
            self.latest_external_spot_source_ts = time.time()
        return price

    def _fetch_coinbase_spot_sync(self) -> Optional[Decimal]:
        """Coinbase HTTP fallback for BTC spot price."""
        import os
        price, self._logged_first_spot = fetch_coinbase_spot_sync(
            timeout_sec=float(os.getenv("EXTERNAL_SPOT_TIMEOUT_SEC", "2.5")),
            already_logged_first_spot=bool(getattr(self, "_logged_first_spot", False)),
            logger_info_fn=logger.info,
            logger_debug_fn=logger.debug,
        )
        return price

    def _record_external_spot_observation(self, price: Decimal) -> None:
        record_external_spot_observation(
            external_spot_history=self.external_spot_history,
            external_spot_history_max=self.external_spot_history_max,
            now_ts=time.time(),
            price=price,
        )

    def _record_polymarket_chainlink_observation(
        self, price: Decimal, ts: float | None, *, received_ts: float | None = None,
    ) -> bool:
        """Store raw Chainlink spot against its source clock, never receipt time."""
        history = getattr(self, "polymarket_chainlink_history", None)
        try:
            source_ts = float(ts) if ts is not None else 0.0
            received = float(received_ts) if received_ts is not None else 0.0
            if history is None or price is None or Decimal(str(price)) <= 0:
                return False
            if not source_ts > 0 or not source_ts < float("inf") or not received > 0 or source_ts > received:
                return False
        except (TypeError, ValueError, ArithmeticError):
            return False
        history.append((source_ts, Decimal(str(price))))
        max_len = int(getattr(self, "polymarket_chainlink_history_max", 1200) or 1200)
        if len(history) > max_len:
            history.pop(0)
        return True

    def _record_polymarket_chainlink_receipt_observation(self, price: Decimal, received_ts: float) -> None:
        """Keep bounded receipt-clock samples solely for unchanged live pricing."""
        history = getattr(self, "polymarket_chainlink_receipt_history", None)
        if history is None or price is None or Decimal(str(price)) <= 0 or received_ts <= 0:
            return
        history.append((float(received_ts), Decimal(str(price))))
        max_len = int(getattr(self, "polymarket_chainlink_history_max", 1200) or 1200)
        if len(history) > max_len:
            history.pop(0)

    def _final_twap_observation(
        self, *, now_ts: float, end_ts: float, window_sec: int,
        history: Optional[list[tuple[float, Decimal]]] = None,
    ) -> tuple[Optional[Decimal], float]:
        """Return the observed partial final-window average from raw ticks.

        The result is intentionally unavailable until the final resolution
        window and until a raw tick covers part of that interval.  That avoids
        inventing a path from the rolling TWAP value itself.
        """
        window = max(1.0, float(window_sec))
        start_ts = float(end_ts) - window
        if now_ts <= start_ts:
            return None, 0.0
        selected_history = history if history is not None else getattr(self, "polymarket_chainlink_history", [])
        records = sorted(
            (
                (float(ts), Decimal(str(price)))
                for ts, price in selected_history
                if float(ts) <= now_ts and Decimal(str(price)) > 0
            ),
            key=lambda item: item[0],
        )
        if not records:
            return None, 0.0
        previous = None
        for record in records:
            if record[0] <= start_ts:
                previous = record
            else:
                break
        if previous is None:
            return None, 0.0
        cursor = start_ts
        last_price = previous[1]
        weighted_sum = Decimal("0")
        observed_sec = 0.0
        for ts, price in records:
            if ts <= start_ts:
                continue
            segment_end = min(ts, now_ts)
            if segment_end > cursor:
                duration = segment_end - cursor
                weighted_sum += last_price * Decimal(str(duration))
                observed_sec += duration
                cursor = segment_end
            last_price = price
            if cursor >= now_ts:
                break
        if cursor < now_ts:
            duration = now_ts - cursor
            weighted_sum += last_price * Decimal(str(duration))
            observed_sec += duration
        if observed_sec <= 0:
            return None, 0.0
        return weighted_sum / Decimal(str(observed_sec)), observed_sec

    def _resolve_opening_strike_from_history(self, start_ts: int) -> Optional[tuple]:
        return resolve_opening_strike_from_history(
            external_spot_history=self.external_spot_history,
            start_ts=start_ts,
            max_lag_sec=float(self.market_strike_anchor_max_lag_sec),
            near_window_sec=float(self.market_strike_anchor_near_sec),
        )

    def _resolve_opening_strike_from_polymarket_history(self, start_ts: int) -> Optional[tuple]:
        return resolve_opening_strike_from_history(
            # Keep the existing live strike-anchor timestamp behavior; the
            # source-clock series is reserved for shadow research integrals.
            external_spot_history=getattr(
                self, "polymarket_chainlink_receipt_history",
                getattr(self, "polymarket_chainlink_history", []),
            ),
            start_ts=start_ts,
            max_lag_sec=float(self.market_strike_anchor_max_lag_sec),
            near_window_sec=float(self.market_strike_anchor_near_sec),
        )

    def _is_authoritative_strike_source(self, source: str) -> bool:
        return str(source or "") in self._AUTHORITATIVE_STRIKE_SOURCES

    def _market_strike_is_entry_eligible(self, slug: str) -> bool:
        """Return whether ``slug`` has a proven market-scoped settlement strike."""
        slug_txt = str(slug or "")
        strike = self.market_strike_cache_by_slug.get(slug_txt)
        source = self.market_strike_source_by_slug.get(slug_txt, "")
        status = getattr(self, "market_strike_status_by_slug", {}).get(slug_txt, "pending")
        return bool(
            isinstance(strike, Decimal)
            and strike > 0
            and self._is_authoritative_strike_source(source)
            and status == "verified"
        )

    def _record_strike_provenance(
        self,
        *,
        slug: str,
        start_ts: int,
        end_ts: int,
        gamma_event_slug: str,
        crypto_open_price: Optional[Decimal],
        twap_enabled: bool,
        twap_lookback_seconds: Optional[int],
        attempt_age_sec: float,
        status: str,
    ) -> None:
        """Journal the market configuration and strike retrieval result."""
        crypto_value = float(crypto_open_price) if crypto_open_price is not None else None
        payload = {
            "slug": slug,
            "status": status,
            "gamma_event_slug": gamma_event_slug,
            "crypto_open_price": crypto_value,
            "market_identity_source": "gamma_market",
            "crypto_source": "polymarket_crypto_price.openPrice",
            "crypto_request": {
                "symbol": "BTC",
                "event_start_ts": int(start_ts),
                "event_end_ts": int(end_ts),
                "variant": "fifteen",
                "twapEnabled": bool(twap_enabled),
                "twapLookbackSeconds": twap_lookback_seconds,
            },
            "attempt_age_sec": round(max(0.0, attempt_age_sec), 3),
        }
        signature = repr(payload)
        signatures = getattr(self, "market_strike_last_provenance_signature_by_slug", None)
        if signatures is None:
            signatures = {}
            self.market_strike_last_provenance_signature_by_slug = signatures
        if signatures.get(slug) == signature:
            return
        signatures[slug] = signature
        self._record_strike_event(
            event_type="MARKET_STRIKE_PROVENANCE",
            slug=slug,
            strike=crypto_open_price,
            source="polymarket_crypto_price_twap_open" if crypto_open_price is not None else "pending",
            extra=payload,
        )

    def _record_strike_event(
        self,
        *,
        event_type: str,
        slug: str,
        strike: Optional[Decimal],
        source: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        payload: dict[str, Any] = {
            "slug": str(slug or ""),
            "strike_source": str(source or ""),
            "authoritative": bool(self._is_authoritative_strike_source(source)),
        }
        if strike is not None:
            payload["strike"] = float(strike)
        if extra:
            payload.update(extra)
        try:
            self._db_strategy_event(event_type, payload)
        except Exception:
            pass

    def _set_provisional_strike(self, *, slug: str, strike: Decimal, source: str) -> None:
        if not slug or strike is None or strike <= 0:
            return
        self.market_strike_provisional_by_slug[slug] = strike
        self.market_strike_provisional_source_by_slug[slug] = str(source or "provisional")
        self._record_strike_event(
            event_type="MARKET_STRIKE_PROVISIONAL",
            slug=slug,
            strike=strike,
            source=source,
        )

    def _maybe_latch_opening_strike_from_live_reference(
        self,
        *,
        slug: str,
        start_ts: int,
    ) -> Optional[Decimal]:
        now_ts = time.time()
        if now_ts < float(start_ts):
            return None
        if now_ts > float(start_ts) + float(self.market_strike_anchor_max_lag_sec):
            return None
        source = str(getattr(self, "latest_external_spot_source", "") or "")
        if source != "polymarket_chainlink_ws":
            return None
        price = getattr(self, "latest_external_spot", None)
        if price is None or price <= 0:
            return None
        src_ts = float(getattr(self, "latest_external_spot_source_ts", 0.0) or 0.0)
        if src_ts <= 0 or abs(src_ts - float(start_ts)) > float(self.market_strike_anchor_max_lag_sec):
            return None
        self.market_strike_cache_by_slug[slug] = price
        strike_source = "polymarket_chainlink_raw_open"
        self.market_strike_source_by_slug[slug] = strike_source
        self.market_strike_provisional_by_slug.pop(slug, None)
        self.market_strike_provisional_source_by_slug.pop(slug, None)
        self._record_strike_event(
            event_type="MARKET_STRIKE_LOCKED",
            slug=slug,
            strike=price,
            source=strike_source,
            extra={
                "sample_dt_sec": float(src_ts - float(start_ts)),
            },
        )
        logger.info(
            f"[STRIKE] Locked opening strike from Polymarket Chainlink raw live latch: "
            f"${float(price):.2f} for slug={slug} "
            f"(sample_dt={src_ts - float(start_ts):+.2f}s)"
        )
        return price

    def _fetch_binance_open_price_sync(self, start_ts: int) -> Optional[Decimal]:
        import os
        return fetch_binance_open_price_sync(
            start_ts=start_ts,
            timeout_sec=float(os.getenv("EXTERNAL_SPOT_TIMEOUT_SEC", "2.5")),
            logger_debug_fn=logger.debug,
        )

    def _estimate_external_spot_sigma_annualized(self) -> Optional[Decimal]:
        return estimate_external_spot_sigma_annualized(
            external_spot_history=self.external_spot_history,
            min_points=self.maker_digital_vol_min_points,
            digital_vol_window=self.maker_digital_vol_window,
        )

    def _estimate_polymarket_raw_spot_sigma_annualized(self) -> Optional[Decimal]:
        """Shadow volatility from raw Chainlink spot history only."""
        return estimate_external_spot_sigma_annualized(
            external_spot_history=list(getattr(self, "polymarket_chainlink_history", [])),
            min_points=self.maker_digital_vol_min_points,
            digital_vol_window=self.maker_digital_vol_window,
        )

    def _settlement_probability_shadow_inputs(
        self, *, slug: str, official_twap: Decimal, strike: Decimal | None,
        time_left_sec: float | None, now_ts: float, source_observed_ts: float | None = None,
    ) -> dict[str, Any]:
        """Build research-only settlement/path diagnostics from existing state.

        This intentionally does not assign ``last_forecast_state`` and never
        enters the quote/order decision path.
        """
        from bot.live_entry_research import build_safety_sigma

        # Match the existing approved 10-second external-spot freshness window.
        # Official TWAP is settlement state and is never a future-path fallback.
        path_spot: Decimal | None = None
        path_spot_source, path_spot_age = "unavailable", None
        raw_price = getattr(self, "_polymarket_chainlink_price", None)
        raw_ts = float(getattr(self, "_polymarket_chainlink_price_observation_ts", 0.0) or 0.0)
        raw_received_ts = float(getattr(self, "_polymarket_chainlink_price_ts", 0.0) or 0.0)
        if raw_price is not None and raw_ts > 0 and raw_received_ts > 0:
            # Research freshness uses the locally received tick, not a remote
            # Chainlink timestamp against the local wall clock.
            age = float(now_ts) - raw_received_ts
            if 0.0 <= age < self._RAW_SPOT_FRESHNESS_SEC and Decimal(str(raw_price)) > 0:
                path_spot, path_spot_source, path_spot_age = Decimal(str(raw_price)), "polymarket_chainlink_spot", age
        if path_spot is None:
            binance_price = getattr(self, "_binance_ws_price", None)
            binance_ts = float(getattr(self, "_binance_ws_price_ts", 0.0) or 0.0)
            if binance_price is not None and binance_ts > 0:
                age = float(now_ts) - binance_ts
                if 0.0 <= age < self._RAW_SPOT_FRESHNESS_SEC and Decimal(str(binance_price)) > 0:
                    path_spot, path_spot_source, path_spot_age = Decimal(str(binance_price)), "binance_ws", age
        quote_instruments = self._research_market_quote_instruments(
            slug=slug,
            runtime_slug=str(getattr(self, "current_market_slug", "") or ""),
        )
        up_inst, down_inst = quote_instruments or ("", "")
        quote_map = getattr(self, "latest_quote_by_inst", {})
        quote_ts = getattr(self, "last_quote_source_ts_by_inst", {})
        quote_received_ts = getattr(self, "last_quote_received_ts_by_inst", {})
        quote_update_ts = getattr(self, "last_quote_update_ts_by_inst", {})
        max_age = max(0.1, float(getattr(self, "quote_max_delivery_delay_sec", 2.0)))

        from bot.research.clocks import observed_source_reference
        source_reference_ts = observed_source_reference(quote_ts, quote_received_ts, (up_inst, down_inst), float(now_ts))

        def fresh_book(inst: str) -> tuple[Decimal | None, Decimal | None, float | None, float | None, str | None]:
            if not inst:
                return None, None, None, None, "instrument_unavailable"
            book = quote_map.get(inst)
            if not book or book[0] is None or book[1] is None:
                return None, None, None, None, "quote_missing"
            source_ts = float(quote_ts.get(inst, 0.0) or 0.0)
            received_ts = float(quote_received_ts.get(inst, 0.0) or 0.0)
            if source_ts <= 0 or received_ts <= 0:
                return None, None, None, None, "quote_timestamp_missing"
            source_age = source_reference_ts - source_ts if source_reference_ts is not None else None
            received_age = float(now_ts) - received_ts
            if source_age is None or source_age < 0 or received_age < 0:
                return None, None, source_age, received_age, "quote_timestamp_future"
            if source_age > max_age or received_age > max_age:
                return None, None, source_age, received_age, "quote_stale"
            bid, ask = Decimal(str(book[0])), Decimal(str(book[1]))
            if bid < 0 or ask <= 0 or bid > ask:
                return None, None, source_age, received_age, "quote_invalid"
            return bid, ask, source_age, received_age, None

        bid_up, ask_up, bbo_up_source_age, bbo_up_received_age, bbo_up_reason = fresh_book(up_inst)
        bid_down, ask_down, bbo_down_source_age, bbo_down_received_age, bbo_down_reason = fresh_book(down_inst)
        bbo_up_age = (float(now_ts) - float(quote_update_ts[up_inst])
                      if up_inst in quote_update_ts else None)
        bbo_down_age = (float(now_ts) - float(quote_update_ts[down_inst])
                        if down_inst in quote_update_ts else None)
        mid_up = (bid_up + ask_up) / 2 if bid_up is not None and ask_up is not None else None
        mid_down = (bid_down + ask_down) / 2 if bid_down is not None and ask_down is not None else None
        if strike is None or strike <= 0 or time_left_sec is None:
            return {
                "probability_model_version": "existing_twap_average_approx_v1",
                "probability_model_mode": "UNAVAILABLE",
                "data_quality": "spot_strike_or_horizon_unavailable",
                "official_current_twap": float(official_twap),
                "path_spot": None, "path_spot_source": "unavailable", "path_spot_age_sec": None,
                "settlement_state_side": "UNKNOWN", "path_spot_side": "UNKNOWN",
                "settlement_path_side_divergence": None, "currently_dominant_side": "UNKNOWN",
                "required_move_mode": "UNAVAILABLE",
                "sigma_ex_market_age_sec": None, "sigma_ex_market_fresh": False,
                "market_bbo_up_age_sec": bbo_up_age,
                "market_bbo_down_age_sec": bbo_down_age,
                "market_bbo_up_source_age_sec": bbo_up_source_age,
                "market_bbo_down_source_age_sec": bbo_down_source_age,
                "market_bbo_up_received_age_sec": bbo_up_received_age,
                "market_bbo_down_received_age_sec": bbo_down_received_age,
                "market_bbo_max_age_sec": max_age,
                "market_bbo_up_unavailable_reason": bbo_up_reason,
                "market_bbo_down_unavailable_reason": bbo_down_reason,
                "market_mid_probability_up": float(mid_up) if mid_up is not None else None,
                "market_mid_probability_down": float(mid_down) if mid_down is not None else None,
                "best_bid_up": float(bid_up) if bid_up is not None else None,
                "best_ask_up": float(ask_up) if ask_up is not None else None,
                "best_bid_down": float(bid_down) if bid_down is not None else None,
                "best_ask_down": float(ask_down) if ask_down is not None else None,
                "market_bbo_age_sec": max(
                    [float(now_ts) - float(quote_ts[k]) for k in (up_inst, down_inst)
                     if k and quote_ts.get(k)]
                ) if any(k and quote_ts.get(k) for k in (up_inst, down_inst)) else None,
                "p_up_ex_market": None, "p_down_ex_market": None,
                "required_move_usd": None, "required_move_bps": None,
                "required_move_sigma": None,
            }
        twap_window = int(getattr(self, "_polymarket_chainlink_twap_window_sec", 60) or 60)
        observed_avg, observed_sec = (None, 0.0)
        raw_history = list(getattr(self, "polymarket_chainlink_history", []))
        try:
            candidate_integral_ts = float(source_observed_ts or 0.0)
        except (TypeError, ValueError):
            candidate_integral_ts = 0.0
        integral_ts = (candidate_integral_ts if 0 < candidate_integral_ts <= float(now_ts)
                       else float(raw_history[-1][0]) if raw_history else 0.0)
        if float(time_left_sec) <= twap_window:
            end_ts = getattr(self, "current_market_end_timestamp", None)
            if integral_ts > 0:
                observed_avg, observed_sec = self._final_twap_observation(
                    now_ts=integral_ts, end_ts=float(end_ts) if end_ts is not None else integral_ts + float(time_left_sec),
                    window_sec=twap_window,
                )
        raw_sigma = self._estimate_polymarket_raw_spot_sigma_annualized()
        sigma_history = raw_history[-int(getattr(self, "maker_digital_vol_window", 30) or 30):]
        sigma_window_sec = float(sigma_history[-1][0]) - float(sigma_history[0][0]) if len(sigma_history) > 1 else None
        # The sigma estimator/math is unchanged. Its input value age belongs
        # to the raw Chainlink clock; receipt age belongs to the local clock.
        sigma_reference_ts = raw_ts  # exact raw tick, with its local receipt above
        sigma_value_age = (sigma_reference_ts - float(sigma_history[-1][0])
                           if raw_sigma is not None and sigma_history and raw_ts > 0 else None)
        sigma_transport_age = float(now_ts) - raw_received_ts if raw_received_ts > 0 else None
        sigma_ages_valid = (sigma_value_age is not None and sigma_transport_age is not None
                            and sigma_value_age >= 0 and sigma_transport_age >= 0)
        sigma_ex_market_age = max(sigma_value_age, sigma_transport_age) if sigma_ages_valid else None
        sigma_ex_market_fresh = bool(
            raw_sigma is not None and sigma_ex_market_age is not None
            and sigma_ex_market_age < self._RAW_SPOT_FRESHNESS_SEC
        )
        try:
            settlement_twap_value = float(official_twap)
            settlement_twap_available = settlement_twap_value > 0 and math.isfinite(settlement_twap_value)
        except (TypeError, ValueError):
            settlement_twap_value, settlement_twap_available = 0.0, False
        settlement_state_side = (
            "UP" if settlement_twap_available and settlement_twap_value >= float(strike)
            else "DOWN" if settlement_twap_available else "UNKNOWN"
        )
        if path_spot is None:
            return {
                "probability_model_version": "existing_twap_average_approx_v1",
                "probability_model_mode": "UNAVAILABLE", "data_quality": "raw_path_spot_unavailable",
                "official_current_twap": float(official_twap), "path_spot": None,
                "fast_spot": None, "path_spot_source": "unavailable", "path_spot_age_sec": None,
                "sigma_ex_market": float(raw_sigma) if raw_sigma is not None else None,
                "sigma_ex_market_source": "polymarket_chainlink_spot_history" if raw_sigma is not None else "unavailable",
                "sigma_ex_market_available": raw_sigma is not None,
                "sigma_ex_market_age_sec": sigma_ex_market_age,
                "sigma_ex_market_transport_age_sec": sigma_transport_age,
                "sigma_ex_market_value_age_sec": sigma_value_age,
                "sigma_ex_market_fresh": sigma_ex_market_fresh,
                "sigma_ex_market_sample_count": len(sigma_history),
                "sigma_ex_market_window_sec": sigma_window_sec,
                "p_up_ex_market": None, "p_down_ex_market": None,
                "p_up_market_conditioned": None, "p_down_market_conditioned": None,
                "required_move_usd": None, "required_move_bps": None, "required_move_sigma": None,
                "required_move_mode": "UNAVAILABLE",
                "required_future_avg_basis": "unavailable",
                "observed_final_window_avg": float(observed_avg) if observed_avg is not None else None,
                "observed_final_window_sec": observed_sec,
                "final_window_integral_source": "polymarket_chainlink_raw_spot_history" if observed_avg is not None else "unavailable",
                "final_window_integral_clock": "chainlink_source_observation_ts",
                "remaining_avg_decision_boundary": None, "required_future_avg_to_flip": None,
                "path_boundary_proxy": float(strike) if float(time_left_sec) > twap_window else None,
                "path_boundary_proxy_source": "strike" if float(time_left_sec) > twap_window else None,
                "settlement_state_side": settlement_state_side,
                "path_spot_side": "UNKNOWN",
                "settlement_path_side_divergence": None,
                "currently_dominant_side": settlement_state_side,
                "required_future_avg_is_exact_partial_integral": False,
                "market_bbo_up_age_sec": bbo_up_age,
                "market_bbo_down_age_sec": bbo_down_age,
                "market_bbo_up_source_age_sec": bbo_up_source_age,
                "market_bbo_down_source_age_sec": bbo_down_source_age,
                "market_bbo_up_received_age_sec": bbo_up_received_age,
                "market_bbo_down_received_age_sec": bbo_down_received_age,
                "market_bbo_max_age_sec": max_age,
                "market_bbo_up_unavailable_reason": bbo_up_reason,
                "market_bbo_down_unavailable_reason": bbo_down_reason,
                "market_mid_probability_up": float(mid_up) if mid_up is not None else None,
                "market_mid_probability_down": float(mid_down) if mid_down is not None else None,
                "best_bid_up": float(bid_up) if bid_up is not None else None,
                "best_ask_up": float(ask_up) if ask_up is not None else None,
                "best_bid_down": float(bid_down) if bid_down is not None else None,
                "best_ask_down": float(ask_down) if ask_down is not None else None,
                "required_path_mode": "UNAVAILABLE",
            }
        common = dict(
            spot=path_spot, strike=strike, time_left_sec=float(time_left_sec),
            reference_source=f"polymarket_chainlink_twap_{twap_window}s_ws",
            outcome="up", sigma_default=self.maker_digital_sigma_default,
            sigma_raw_realized=raw_sigma if sigma_ex_market_fresh else None,
            sigma_scale=self.maker_digital_vol_scale,
            sigma_floor=self.maker_digital_sigma_floor, sigma_ceiling=self.maker_digital_sigma_ceiling,
            time_decay_enabled=bool(self.maker_digital_sigma_time_decay_enabled),
            time_decay_ref_sec=float(self.maker_digital_sigma_time_decay_ref_sec),
            time_decay_min=float(self.maker_digital_sigma_time_decay_min),
            twap_window_sec=twap_window, observed_twap_average=observed_avg,
            observed_twap_seconds=observed_sec,
            source_observed_ts=float(source_observed_ts or now_ts),
            source_age_sec=max(0.0, now_ts - float(source_observed_ts or now_ts)),
        )
        from bot.forecast_state import build_forecast_state
        conditioned = build_forecast_state(
            **common, market_mid=mid_up if mid_up is not None else Decimal("0.5"),
            implied_sigma_enabled=bool(getattr(self, "maker_implied_sigma_enabled", False) and mid_up is not None),
        )
        p_cond = (conditioned.twap_average_up_probability
                  if conditioned.twap_average_up_probability is not None else conditioned.standard_up_probability)
        # sigma_after_time_decay is upstream of the implied-market floor, so it
        # is the already-computed market-independent sigma for this same state.
        math_diag = MakerEngine.twap_settlement_diagnostics(
            spot=float(path_spot),
            strike=float(strike), sigma_annual=float(conditioned.sigma_after_time_decay),
            time_left_sec=float(time_left_sec), twap_window_sec=twap_window,
            observed_window_avg=float(observed_avg) if observed_avg is not None else None,
            observed_window_sec=observed_sec,
        )
        p_ex = math_diag["p_up"] if sigma_ex_market_fresh else None
        final_window = float(time_left_sec) <= twap_window
        path_spot_side = "UP" if float(path_spot) >= float(strike) else "DOWN"
        settlement_path_side_divergence = (
            settlement_state_side != path_spot_side
            if settlement_state_side in {"UP", "DOWN"} else None
        )
        integral_available = observed_avg is not None and observed_sec > 0
        exact_boundary = math_diag.get("remaining_avg_decision_boundary") if final_window and integral_available else None
        if final_window and exact_boundary is not None:
            required_move_mode = "EXACT_FINAL_WINDOW_BOUNDARY"
            required_avg = exact_boundary
            target = required_avg
        elif not final_window:
            required_move_mode = "PRE_FINAL_STRIKE_PROXY"
            required_avg = None
            target = float(strike)
        else:
            required_move_mode = "UNAVAILABLE"
            required_avg = None
            target = None
        if path_spot is None:
            data_quality = "raw_path_spot_unavailable"
        elif raw_sigma is not None and not sigma_ex_market_fresh:
            data_quality = "stale_raw_spot_sigma"
        elif raw_sigma is None:
            data_quality = "raw_spot_sigma_unavailable"
        elif final_window and not integral_available:
            data_quality = "insufficient_raw_final_window_history"
        elif not final_window:
            data_quality = "pre_final_window_approximation"
        else:
            data_quality = "ok"
        path_spot_float = float(path_spot)
        move_sigma = (build_safety_sigma(
            spot=path_spot_float, strike=float(target), sigma_annual=float(conditioned.sigma_after_time_decay),
            time_left_sec=(max(1.0, twap_window - observed_sec) if final_window and integral_available else float(time_left_sec)),
            sigma_source="forecast_sigma_after_time_decay_ex_market",
        ) if sigma_ex_market_fresh and target is not None else {"safety_sigma": None})
        move_usd = float(target) - path_spot_float if target is not None else None
        move_bps = move_usd / path_spot_float * 10000 if move_usd is not None and path_spot_float > 0 else None
        p_market = float(mid_up) if mid_up is not None else None
        result = {
            "probability_model_version": "existing_twap_average_approx_v1",
            "probability_model_mode": ("FINAL_WINDOW_PARTIAL_INTEGRAL" if integral_available and final_window else
                                       "FINAL_WINDOW_RAW_UNAVAILABLE" if final_window else "PRE_FINAL_WINDOW_APPROX"),
            "data_quality": data_quality,
            "official_current_twap": float(official_twap),
            "path_spot": path_spot_float, "path_spot_source": path_spot_source,
            "path_spot_age_sec": path_spot_age,
            "sigma_ex_market": float(raw_sigma) if raw_sigma is not None else None,
            "sigma_ex_market_source": "polymarket_chainlink_spot_history" if raw_sigma is not None else "unavailable",
            "sigma_ex_market_available": raw_sigma is not None,
            "sigma_ex_market_age_sec": sigma_ex_market_age,
            "sigma_ex_market_transport_age_sec": sigma_transport_age,
            "sigma_ex_market_value_age_sec": sigma_value_age,
            "sigma_ex_market_fresh": sigma_ex_market_fresh,
            "sigma_ex_market_sample_count": len(sigma_history),
            "sigma_ex_market_window_sec": sigma_window_sec,
            "final_window_integral_source": "polymarket_chainlink_raw_spot_history" if observed_avg is not None else "unavailable",
            "final_window_integral_clock": "chainlink_source_observation_ts",
            "required_path_mode": ("FINAL_WINDOW_PARTIAL_INTEGRAL" if final_window and observed_avg is not None
                                    else "PRE_FINAL_WINDOW_APPROX" if not final_window else "UNAVAILABLE"),
            "required_move_mode": required_move_mode,
            "observed_final_window_avg": float(observed_avg) if observed_avg is not None else None,
            "observed_final_window_sec": observed_sec,
            "remaining_final_window_sec": max(0.0, twap_window - observed_sec) if final_window else None,
            "required_future_avg_to_flip": required_avg,
            "required_future_avg_is_exact_partial_integral": required_move_mode == "EXACT_FINAL_WINDOW_BOUNDARY",
            "required_future_avg_basis": ("raw_chainlink_partial_integral" if required_move_mode == "EXACT_FINAL_WINDOW_BOUNDARY"
                                           else "strike_proxy" if required_move_mode == "PRE_FINAL_STRIKE_PROXY" else "unavailable"),
            "remaining_avg_decision_boundary": required_avg,
            "path_boundary_proxy": float(strike) if required_move_mode == "PRE_FINAL_STRIKE_PROXY" else None,
            "path_boundary_proxy_source": "strike" if not final_window else None,
            "fast_spot": path_spot_float,
            "required_avg_for_up": required_avg,
            "required_avg_for_down": required_avg,
            "settlement_state_side": settlement_state_side,
            "path_spot_side": path_spot_side,
            "settlement_path_side_divergence": settlement_path_side_divergence,
            "currently_dominant_side": settlement_state_side,
            "required_move_usd": move_usd, "required_move_bps": move_bps,
            "required_move_sigma": move_sigma["safety_sigma"],
            "sigma_final": float(conditioned.sigma_final),
            "sigma_after_time_decay_ex_market": float(conditioned.sigma_after_time_decay) if sigma_ex_market_fresh else None,
            "sigma_implied_floor_applied": bool(conditioned.implied_sigma_floor_applied),
            "p_up_market_conditioned": float(p_cond),
            "p_down_market_conditioned": 1.0 - float(p_cond),
            "p_up_ex_market": float(p_ex) if p_ex is not None else None,
            "p_down_ex_market": 1.0 - float(p_ex) if p_ex is not None else None,
            "market_mid_probability_up": p_market,
            "market_mid_probability_down": float(mid_down) if mid_down is not None else None,
            "market_bbo_up_age_sec": bbo_up_age,
            "market_bbo_down_age_sec": bbo_down_age,
            "market_bbo_up_source_age_sec": bbo_up_source_age,
            "market_bbo_down_source_age_sec": bbo_down_source_age,
            "market_bbo_up_received_age_sec": bbo_up_received_age,
            "market_bbo_down_received_age_sec": bbo_down_received_age,
            "market_bbo_max_age_sec": max_age,
            "market_bbo_up_unavailable_reason": bbo_up_reason,
            "market_bbo_down_unavailable_reason": bbo_down_reason,
            "best_bid_up": float(bid_up) if bid_up is not None else None,
            "best_ask_up": float(ask_up) if ask_up is not None else None,
            "executable_buy_probability_up": float(ask_up) if ask_up is not None else None,
            "executable_sell_probability_up": float(bid_up) if bid_up is not None else None,
            "best_bid_down": float(bid_down) if bid_down is not None else None,
            "best_ask_down": float(ask_down) if ask_down is not None else None,
            "executable_buy_probability_down": float(ask_down) if ask_down is not None else None,
            "executable_sell_probability_down": float(bid_down) if bid_down is not None else None,
            "model_minus_market_mid_up": float(p_ex) - p_market if p_market is not None and p_ex is not None else None,
            "model_minus_best_ask_up": float(p_ex) - float(ask_up) if ask_up is not None and p_ex is not None else None,
            "model_minus_best_bid_up": float(p_ex) - float(bid_up) if bid_up is not None and p_ex is not None else None,
            "model_minus_market_mid_down": (1.0 - float(p_ex)) - float(mid_down) if mid_down is not None and p_ex is not None else None,
            "model_minus_best_ask_down": (1.0 - float(p_ex)) - float(ask_down) if ask_down is not None and p_ex is not None else None,
            "model_minus_best_bid_down": (1.0 - float(p_ex)) - float(bid_down) if bid_down is not None and p_ex is not None else None,
            "market_bbo_age_sec": max(
                [now_ts - float(quote_ts[k]) for k in (up_inst, down_inst) if k and quote_ts.get(k)]
            ) if any(k and quote_ts.get(k) for k in (up_inst, down_inst)) else None,
            "twap_required_path_math_mode": (math_diag["mode"] if integral_available or not final_window
                                             else "FINAL_WINDOW_RAW_UNAVAILABLE"),
        }
        return result

    def _research_market_quote_instruments(
        self, *, slug: str, runtime_slug: str,
    ) -> tuple[str, str] | None:
        """Resolve target-market quote IDs without borrowing a stale pair."""
        by_slug = getattr(self, "research_market_instruments_by_slug", {})
        pair = by_slug.get(str(slug)) if isinstance(by_slug, dict) else None
        if isinstance(pair, dict) and (pair.get("UP") or pair.get("DOWN")):
            return str(pair.get("UP") or ""), str(pair.get("DOWN") or "")
        if str(slug) == str(runtime_slug):
            return (
                str(getattr(self, "current_up_instrument_id", "") or ""),
                str(getattr(self, "current_down_instrument_id", "") or ""),
            )
        return None

    # ------------------------------------------------------------------
    # Strike status logging
    # ------------------------------------------------------------------

    def _log_strike_status(self, slug: Optional[str]) -> None:
        slug_txt = str(slug or "").strip()
        if not slug_txt:
            logger.info(
                "Strike status: slug unavailable; digital pricer will temporarily fallback until market slug resolves."
            )
            return
        strike = self.market_strike_cache_by_slug.get(slug_txt)
        source = self.market_strike_source_by_slug.get(slug_txt, "pending")
        if strike is None:
            provisional = self.market_strike_provisional_by_slug.get(slug_txt)
            provisional_source = self.market_strike_provisional_source_by_slug.get(slug_txt, "pending")
            if provisional is not None:
                logger.info(
                    f"Strike status: slug={slug_txt} source={source} value=pending "
                    f"(provisional={provisional_source}:${float(provisional):.2f}; trading should wait for authoritative lock)."
                )
                return
            logger.info(
                f"Strike status: slug={slug_txt} source={source} value=pending "
                "(digital pricer may fallback to drift until opening anchor is locked)."
            )
            return
        logger.info(
            f"Strike status: slug={slug_txt} source={source} value=${float(strike):.2f} (locked); spot remains realtime."
        )

    # ------------------------------------------------------------------
    # Strike extraction helpers
    # ------------------------------------------------------------------

    def _extract_strike_from_question(self, question_text: str) -> Optional[Decimal]:
        return extract_strike_from_question(question_text, self.latest_external_spot)

    async def _get_market_strike_for_instrument(self, instrument_id: Any) -> Optional[Decimal]:
        inst = self._normalize_instrument_id(instrument_id)
        if inst is None:
            return None
        instrument = self.cache.instrument(inst)
        if instrument is None:
            return None
        slug = self._extract_market_slug_from_instrument(instrument)
        if not slug:
            slug = str(self.current_market_slug or "")

        # 1) Cache hit — fastest path
        if slug and slug in self.market_strike_cache_by_slug:
            cached = self.market_strike_cache_by_slug[slug]
            source = self.market_strike_source_by_slug.get(slug, "pending")
            if self._market_strike_is_entry_eligible(slug):
                return cached
            if self._is_authoritative_strike_source(source):
                # A market-scoped strike may be useful for diagnostics/exit
                # context, but cannot be used for a new entry until the
                # competing source provenance is verified again.
                logger.warning(
                    f"[STRIKE] Market-scoped strike is not entry-eligible for {slug}: "
                    f"status={getattr(self, 'market_strike_status_by_slug', {}).get(slug, 'pending')} "
                    f"value=${float(cached):.2f}."
                )
            if not self._is_authoritative_strike_source(source):
                logger.warning(
                    f"[STRIKE] Ignoring non-authoritative cached strike for {slug}: "
                    f"source={source} value=${float(cached):.2f}. Waiting for market-scoped Price To Beat."
                )
                self._record_strike_event(
                    event_type="MARKET_STRIKE_CACHE_REJECTED",
                    slug=slug,
                    strike=cached,
                    source=source,
                )
                self._set_provisional_strike(slug=slug, strike=cached, source=source)
                self.market_strike_cache_by_slug.pop(slug, None)
                self.market_strike_source_by_slug.pop(slug, None)

        strike = None

        # If no slug, can't do history/REST lookups
        if not slug:
            info = getattr(instrument, "info", None) or {}
            if not isinstance(info, dict):
                info = {}
            question = str(info.get("question", "") or "")
            return self._extract_strike_from_question(question)

        # 2) Resolve market start time
        start_ts = self.market_start_ts_by_slug.get(slug)
        if start_ts is None:
            parsed_start = extract_market_start_ts_from_slug(slug)
            if parsed_start is not None:
                start_ts = parsed_start
                self.market_start_ts_by_slug[slug] = parsed_start
        if start_ts is None:
            info = getattr(instrument, "info", None) or {}
            if not isinstance(info, dict):
                info = {}
            question = str(info.get("question", "") or "")
            strike = self._extract_strike_from_question(question)
            return strike

        # 3) Gamma verifies the requested market identity and provides the
        # market's TWAP contract. The frontend's matching crypto-price request
        # is the authoritative opening Price To Beat; Gamma does not expose a
        # live eventMetadata.priceToBeat field for these markets.
        end_ts = int(start_ts) + 900
        now_ts = time.time()
        if now_ts >= float(start_ts):
            attempt_state = getattr(self, "market_strike_crypto_price_last_try_ts_by_slug", None)
            if attempt_state is None:
                attempt_state = {}
                self.market_strike_crypto_price_last_try_ts_by_slug = attempt_state
            first_attempt_state = getattr(self, "market_strike_crypto_price_first_try_ts_by_slug", None)
            if first_attempt_state is None:
                first_attempt_state = {}
                self.market_strike_crypto_price_first_try_ts_by_slug = first_attempt_state
            last_try = float(attempt_state.get(slug, 0.0))
            first_try = float(first_attempt_state.setdefault(slug, now_ts))
            attempt_age_sec = now_ts - first_try
            retry_due = now_ts - last_try >= self._MARKET_STRIKE_INITIAL_RETRY_INTERVAL_SEC
            if retry_due and attempt_age_sec <= self._MARKET_STRIKE_INITIAL_RESOLUTION_WINDOW_SEC:
                attempt_state[slug] = now_ts
                gamma_market = await fetch_gamma_market_by_slug(slug)
                gamma_event_slug = str(gamma_market.get("_gamma_event_slug") or "") if isinstance(gamma_market, dict) else ""
                crypto_config = gamma_market.get("cryptoMarketConfig") if isinstance(gamma_market, dict) else None
                if not isinstance(crypto_config, dict) and isinstance(gamma_market, dict):
                    crypto_config = gamma_market.get("crypto_market_config")
                twap_enabled = bool(crypto_config.get("twapEnabled") is True) if isinstance(crypto_config, dict) else False
                try:
                    twap_lookback_seconds = int(crypto_config.get("twapLookbackSeconds")) if twap_enabled else None
                except (TypeError, ValueError):
                    twap_lookback_seconds = None
                market_identity_verified = gamma_event_slug == slug
                crypto_open_price = (
                    await fetch_crypto_price_to_beat(
                        start_ts=int(start_ts),
                        end_ts=end_ts,
                        twap_enabled=twap_enabled,
                        twap_lookback_seconds=twap_lookback_seconds,
                    )
                    if market_identity_verified and twap_enabled and twap_lookback_seconds == 60
                    else None
                )
                status_by_slug = getattr(self, "market_strike_status_by_slug", None)
                if status_by_slug is None:
                    status_by_slug = {}
                    self.market_strike_status_by_slug = status_by_slug
                provenance_by_slug = getattr(self, "market_strike_provenance_by_slug", None)
                if provenance_by_slug is None:
                    provenance_by_slug = {}
                    self.market_strike_provenance_by_slug = provenance_by_slug
                status = "verified" if crypto_open_price is not None else "pending_frontend_twap_open"
                provenance_by_slug[slug] = {
                    "status": status,
                    "gamma_event_slug": gamma_event_slug,
                    "crypto_open_price": crypto_open_price,
                    "twap_enabled": twap_enabled,
                    "twap_lookback_seconds": twap_lookback_seconds,
                    "attempt_age_sec": attempt_age_sec,
                }
                status_by_slug[slug] = status
                self._record_strike_provenance(
                    slug=slug,
                    start_ts=int(start_ts),
                    end_ts=end_ts,
                    gamma_event_slug=gamma_event_slug,
                    crypto_open_price=crypto_open_price,
                    twap_enabled=twap_enabled,
                    twap_lookback_seconds=twap_lookback_seconds,
                    attempt_age_sec=attempt_age_sec,
                    status=status,
                )
                if crypto_open_price is not None:
                    self.market_strike_cache_by_slug[slug] = crypto_open_price
                    self.market_strike_source_by_slug[slug] = "polymarket_crypto_price_twap_open"
                    self.market_strike_provisional_by_slug.pop(slug, None)
                    self.market_strike_provisional_source_by_slug.pop(slug, None)
                    self._strike_pending_log_state_by_slug.pop(slug, None)
                    self._record_strike_event(
                        event_type="MARKET_STRIKE_LOCKED",
                        slug=slug,
                        strike=crypto_open_price,
                        source="polymarket_crypto_price_twap_open",
                        extra={
                            "strike_status": status,
                            "twap_lookback_seconds": twap_lookback_seconds,
                            "attempt_age_sec": attempt_age_sec,
                        },
                    )
                    logger.info(
                        f"[STRIKE] Locked verified frontend TWAP Price To Beat: "
                        f"${float(crypto_open_price):.2f} for slug={slug}; "
                        f"twap_window={twap_lookback_seconds}s attempt_age={attempt_age_sec:.1f}s"
                    )
                    return crypto_open_price

        # 4) Local opening references remain provisional until the published
        # Price To Beat is available. They are recorded for diagnostics only.
        anchor = self._resolve_opening_strike_from_polymarket_history(start_ts)
        if anchor is not None:
            anchor_ts, anchor_px = anchor
            self._set_provisional_strike(
                slug=slug,
                strike=anchor_px,
                source="polymarket_chainlink_raw_open",
            )
            logger.info(
                f"[STRIKE] Provisional Chainlink opening reference: "
                f"${float(anchor_px):.2f} for slug={slug} "
                f"(sample_dt={anchor_ts - float(start_ts):+.2f}s; awaiting published Price To Beat)"
            )

        # 5) Keep a live local latch provisional as well.
        live_latched = self._maybe_latch_opening_strike_from_live_reference(
            slug=slug,
            start_ts=int(start_ts),
        )
        if live_latched is not None:
            self.market_strike_cache_by_slug.pop(slug, None)
            self.market_strike_source_by_slug.pop(slug, None)
            self._set_provisional_strike(
                slug=slug,
                strike=live_latched,
                source="polymarket_chainlink_live_latch",
            )

        # 6) Fallback: parse from question text
        info = getattr(instrument, "info", None) or {}
        if not isinstance(info, dict):
            info = {}
        question = str(info.get("question", "") or "")
        strike = self._extract_strike_from_question(question)
        if strike is not None and slug:
            self._set_provisional_strike(slug=slug, strike=strike, source="parsed")
            logger.info(
                f"[STRIKE] Provisional strike via question parsing for {slug}: ${float(strike):,.2f} "
                "(not authoritative; waiting for Polymarket Chainlink open lock)"
            )

        # 7) Generic external spot history fallback, only when no stronger
        # local Chainlink candidate has already been recorded.
        if slug not in self.market_strike_provisional_by_slug:
            anchor = self._resolve_opening_strike_from_history(start_ts)
            if anchor is not None:
                anchor_ts, anchor_px = anchor
                self._set_provisional_strike(slug=slug, strike=anchor_px, source="spot_history_open")
                logger.info(
                    f"[STRIKE] Provisional strike from spot history fallback: "
                    f"${float(anchor_px):.2f} for slug={slug} "
                    f"(sample_dt={anchor_ts - float(start_ts):+.2f}s; not authoritative)"
                )

        # 8) Binance REST backfill as last resort.
        import asyncio as _asyncio
        now_ts = time.time()
        last_try = float(self.market_strike_rest_last_try_ts_by_slug.get(slug, 0.0))
        if (
            slug not in self.market_strike_provisional_by_slug
            and now_ts >= float(start_ts)
            and (now_ts - last_try) >= float(self.market_strike_rest_retry_sec)
        ):
            self.market_strike_rest_last_try_ts_by_slug[slug] = now_ts
            backfilled = await _asyncio.to_thread(self._fetch_binance_open_price_sync, start_ts)
            if backfilled is not None:
                self._set_provisional_strike(slug=slug, strike=backfilled, source="binance_rest_open")
                logger.info(
                    f"[STRIKE] Provisional strike from Binance REST backfill: "
                    f"${float(backfilled):.2f} for slug={slug} "
                    "(not authoritative; trading should wait for Polymarket Chainlink open lock)"
                )

        self._maybe_log_strike_pending_state(
            slug=slug,
            provisional_strike=self.market_strike_provisional_by_slug.get(slug),
            provisional_source=str(self.market_strike_provisional_source_by_slug.get(slug, "") or ""),
        )
        return None

    # ------------------------------------------------------------------
    # Fair probability
    # ------------------------------------------------------------------

    def _build_forecast_state(
        self,
        *,
        spot: Decimal,
        strike: Decimal,
        time_left_sec: float,
        market_mid: Decimal,
        outcome: str,
        reference_source: str,
        now_ts: Optional[float] = None,
        source_observed_ts: Optional[float] = None,
        source_age_sec: Optional[float] = None,
    ) -> ForecastState:
        """Build the one forecast representation used by price and side logic."""
        now = float(now_ts if now_ts is not None else time.time())
        end_ts = getattr(self, "current_market_end_timestamp", None)
        twap_window_sec = int(getattr(self, "polymarket_chainlink_twap_window_sec", 60) or 60)
        observed_twap_average: Optional[Decimal] = None
        observed_twap_seconds = 0.0
        if self._is_twap_spot_source(reference_source):
            observed_twap_average, observed_twap_seconds = self._final_twap_observation(
                now_ts=now,
                end_ts=float(end_ts) if end_ts is not None else now + time_left_sec,
                window_sec=twap_window_sec,
                history=getattr(self, "polymarket_chainlink_receipt_history", []),
            )
        state = build_forecast_state(
            spot=spot,
            strike=strike,
            time_left_sec=time_left_sec,
            reference_source=reference_source,
            market_mid=market_mid,
            outcome=outcome,
            sigma_default=self.maker_digital_sigma_default,
            sigma_raw_realized=self._estimate_external_spot_sigma_annualized(),
            sigma_scale=self.maker_digital_vol_scale,
            sigma_floor=self.maker_digital_sigma_floor,
            sigma_ceiling=self.maker_digital_sigma_ceiling,
            time_decay_enabled=bool(self.maker_digital_sigma_time_decay_enabled),
            time_decay_ref_sec=float(self.maker_digital_sigma_time_decay_ref_sec),
            time_decay_min=float(self.maker_digital_sigma_time_decay_min),
            implied_sigma_enabled=bool(getattr(self, "maker_implied_sigma_enabled", False)),
            twap_window_sec=twap_window_sec,
            observed_twap_average=observed_twap_average,
            observed_twap_seconds=observed_twap_seconds,
            source_observed_ts=source_observed_ts,
            source_age_sec=source_age_sec,
        )
        self.last_forecast_state = state
        return state


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
            self.external_spot_consecutive_failures = 0  # BUG-5 FIX: reset on success
            self._record_external_spot_observation(external)

            strike = None
            sigma = self.maker_digital_sigma_default
            time_left_sec = 0.0
            outcome = ""

            if self.maker_fair_pricer_mode == "digital":
                strike = await self._get_market_strike_for_instrument(instrument_id)
                end_ts = getattr(self, "current_market_end_timestamp", None)
                time_left_sec = float(end_ts - time.time()) if end_ts is not None else 0.0
                instrument = self.cache.instrument(self._normalize_instrument_id(instrument_id)) if instrument_id is not None else None
                outcome = self._extract_outcome_from_instrument(instrument) if instrument is not None else ""

                slug = str(self.current_market_slug or "")
                if strike is None or not self._market_strike_is_entry_eligible(slug):
                    strike = None
                    if time.time() - self._last_strike_fallback_log_ts >= self.strike_fallback_log_interval_sec:
                        logger.warning(
                            "Digital pricer entry-safe fallback: market strike is unavailable or unverified; "
                            "digital probability will not be used for a new BUY."
                        )
                        self._last_strike_fallback_log_ts = time.time()
                else:
                    now_ts = time.time()
                    forecast = self._build_forecast_state(
                        spot=external,
                        strike=strike,
                        time_left_sec=time_left_sec,
                        market_mid=market_mid,
                        outcome=outcome,
                        reference_source=str(self.latest_external_spot_source or ""),
                        now_ts=now_ts,
                    )
                    sigma = forecast.sigma_final
                    self.last_digital_pricer_diagnostics = forecast.diagnostics(
                        market_mid=market_mid,
                        outcome=outcome,
                    )
                    if now_ts - self._last_digital_pricer_log_ts >= 60:
                        up_prob = forecast.selected_up_probability
                        fair_for_token = up_prob
                        if outcome == "down":
                            fair_for_token = Decimal("1.0") - up_prob
                        imp_str = f" implied_σ={float(forecast.implied_sigma):.4f}" if forecast.implied_sigma else ""
                        fair_color = "green" if fair_for_token >= Decimal("0.60") else "yellow" if fair_for_token >= Decimal("0.40") else "red"
                        side_color = "green" if self.active_side.value == "UP" else "red" if self.active_side.value == "DOWN" else "yellow"
                        source_color = "cyan" if self._is_twap_spot_source(self.latest_external_spot_source or "") else "yellow"
                        msg = (
                            "<white>Digital pricer inputs:</white> "
                            f"spot=<cyan>{float(external):.2f}</cyan> "
                            f"spot_source=<{source_color}>{self.latest_external_spot_source or '-'}</{source_color}> "
                            f"strike=<magenta>{float(strike):.2f}</magenta> "
                            f"sigma=<white>{float(sigma):.4f}</white>{imp_str} "
                            f"t_left=<white>{time_left_sec:.1f}s</white> "
                            f"token_outcome=<blue>{outcome or 'unknown'}</blue> "
                            f"up_prob=<yellow>{float(up_prob):.4f}</yellow> "
                            f"fair_down=<yellow>{float(Decimal('1.0') - up_prob):.4f}</yellow> "
                            f"fair_for_token=<{fair_color}>{float(fair_for_token):.4f}</{fair_color}> "
                            f"active_side=<{side_color}>{self.active_side.value}</{side_color}>"
                        )
                        if self._binance_ws_price is not None:
                            msg += f" binance_spot=<white>{float(self._binance_ws_price):.2f}</white>"
                        logger.opt(colors=True).info(msg)
                        if self._binance_ws_price is not None:
                            delta = external - self._binance_ws_price
                            delta_color = "green" if delta > 0 else "red" if delta < 0 else "yellow"
                            logger.opt(colors=True).info(
                                "<white>Digital pricer reference check:</white> "
                                f"reference_spot=<cyan>{float(external):.2f}</cyan> "
                                f"binance_spot=<white>{float(self._binance_ws_price):.2f}</white> "
                                f"delta=<{delta_color}>{float(delta):+.2f}</{delta_color}>"
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
                pricer_mode=self.maker_fair_pricer_mode,
            )
            if self.maker_fair_pricer_mode == "digital" and strike is not None:
                fair = self.last_forecast_state.probability_for_outcome(outcome)

            self.last_external_spot = external
        else:
            # BUG-5 FIX: track consecutive external spot failures and pause quoting
            self.external_spot_consecutive_failures += 1
            if self.external_spot_consecutive_failures >= self.external_spot_max_failures:
                pause_sec = min(30.0, self.external_spot_consecutive_failures * 2.0)
                self.quote_pause_until_ts = max(
                    self.quote_pause_until_ts, time.time() + pause_sec
                )
                if self.external_spot_consecutive_failures % 10 == 0:
                    logger.warning(
                        f"External spot unavailable for {self.external_spot_consecutive_failures} "
                        f"consecutive attempts; pausing quotes for {pause_sec:.0f}s"
                    )

        return fair
