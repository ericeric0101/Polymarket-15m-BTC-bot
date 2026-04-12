"""
bot/spot_pricer.py – SpotPricerMixin

Extracted from run_bot.py (L1575-L1942).
Contains BTC spot price fetching, Binance WebSocket management,
strike resolution, and fair probability calculation.

IntegratedBTCStrategy inherits this mixin so all self.* references remain valid.
"""
from __future__ import annotations

import asyncio
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
    extract_price_to_beat_from_market_payload,
)
from bot.price_streams import (
    BINANCE_AGGTRADE_WS_URL,
    POLYMARKET_CHAINLINK_SUBSCRIBE_PAYLOAD,
    POLYMARKET_LIVE_WS_URL,
    extract_binance_aggtrade_tick,
    extract_polymarket_chainlink_tick,
)
from execution.maker_engine import MakerEngine


class SpotPricerMixin:
    """Mixin providing BTC spot price, Binance WS, and fair probability logic."""

    _AUTHORITATIVE_STRIKE_SOURCES = {
        "polymarket_chainlink_open",
        "polymarket_chainlink_live_latch",
    }

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
                    logger.info("✓ Polymarket Chainlink WS connected")
                    ws.send(_json.dumps(POLYMARKET_CHAINLINK_SUBSCRIBE_PAYLOAD))
                    while not self._polymarket_chainlink_ws_stop_event.is_set():
                        try:
                            raw = ws.recv(timeout=5)
                        except TimeoutError:
                            continue
                        tick = extract_polymarket_chainlink_tick(raw)
                        if tick is None:
                            continue
                        self._polymarket_chainlink_price = tick.price
                        self._polymarket_chainlink_price_ts = tick.received_at_ts
                        self._polymarket_chainlink_event_ts_ms = tick.updated_at_ms
                        self._record_polymarket_chainlink_observation(
                            tick.price,
                            tick.received_at_ts,
                        )
            except Exception as exc:
                logger.debug(
                    f"Polymarket Chainlink WS error: {exc}; reconnect in {reconnect_delay:.0f}s"
                )
                self._polymarket_chainlink_ws_stop_event.wait(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

    # ------------------------------------------------------------------
    # Binance WebSocket
    # ------------------------------------------------------------------

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
                            tick = extract_binance_aggtrade_tick(raw)
                            if tick is not None:
                                self._binance_ws_price = tick.price
                                self._binance_ws_price_ts = tick.received_at_ts
                                # Feed into SignalEngine's BTC EMA tracker
                                _sig_eng = getattr(self, "_signal_engine", None)
                                if _sig_eng is not None:
                                    _sig_eng.update_btc_price(
                                        self._binance_ws_price,
                                        self._binance_ws_price_ts,
                                    )
                        except Exception:
                            pass
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
        Primary: Polymarket Chainlink WS.
        Fallback: Binance WS.
        Last resort: Coinbase HTTP.
        """
        # Primary: Polymarket Chainlink WS if fresh.
        if self._polymarket_chainlink_price is not None:
            age = time.time() - self._polymarket_chainlink_price_ts
            if age < 10.0:
                price = self._polymarket_chainlink_price
                self.latest_external_spot_source = "polymarket_chainlink_ws"
                self.latest_external_spot_source_ts = self._polymarket_chainlink_price_ts
                if not getattr(self, "_logged_first_spot", False):
                    logger.info(f"✓ First BTC reference spot via Polymarket Chainlink WS: ${price:,.2f}")
                    self._logged_first_spot = True
                return price
            logger.debug(f"Polymarket Chainlink WS price stale ({age:.1f}s), falling back")

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

    def _record_polymarket_chainlink_observation(self, price: Decimal, ts: float) -> None:
        history = getattr(self, "polymarket_chainlink_history", None)
        if history is None:
            return
        history.append((ts, price))
        max_len = int(getattr(self, "polymarket_chainlink_history_max", 1200) or 1200)
        if len(history) > max_len:
            history.pop(0)

    def _resolve_opening_strike_from_history(self, start_ts: int) -> Optional[tuple]:
        return resolve_opening_strike_from_history(
            external_spot_history=self.external_spot_history,
            start_ts=start_ts,
            max_lag_sec=float(self.market_strike_anchor_max_lag_sec),
            near_window_sec=float(self.market_strike_anchor_near_sec),
        )

    def _resolve_opening_strike_from_polymarket_history(self, start_ts: int) -> Optional[tuple]:
        return resolve_opening_strike_from_history(
            external_spot_history=getattr(self, "polymarket_chainlink_history", []),
            start_ts=start_ts,
            max_lag_sec=float(self.market_strike_anchor_max_lag_sec),
            near_window_sec=float(self.market_strike_anchor_near_sec),
        )

    def _is_authoritative_strike_source(self, source: str) -> bool:
        return str(source or "") in self._AUTHORITATIVE_STRIKE_SOURCES

    def _set_provisional_strike(self, *, slug: str, strike: Decimal, source: str) -> None:
        if not slug or strike is None or strike <= 0:
            return
        self.market_strike_provisional_by_slug[slug] = strike
        self.market_strike_provisional_source_by_slug[slug] = str(source or "provisional")

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
        if str(getattr(self, "latest_external_spot_source", "") or "") != "polymarket_chainlink_ws":
            return None
        price = getattr(self, "latest_external_spot", None)
        if price is None or price <= 0:
            return None
        src_ts = float(getattr(self, "latest_external_spot_source_ts", 0.0) or 0.0)
        if src_ts <= 0 or abs(src_ts - float(start_ts)) > float(self.market_strike_anchor_max_lag_sec):
            return None
        self.market_strike_cache_by_slug[slug] = price
        self.market_strike_source_by_slug[slug] = "polymarket_chainlink_live_latch"
        self.market_strike_provisional_by_slug.pop(slug, None)
        self.market_strike_provisional_source_by_slug.pop(slug, None)
        logger.info(
            f"[STRIKE] Locked opening strike from Polymarket Chainlink live latch: "
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

    async def _maybe_validate_strike_with_gamma(self, slug: str, local_strike: Decimal) -> None:
        now_ts = time.time()
        last_validate = float(self.market_strike_last_gamma_validate_ts_by_slug.get(slug, 0.0))
        if now_ts - last_validate < float(self.market_strike_gamma_validate_interval_sec):
            return
        self.market_strike_last_gamma_validate_ts_by_slug[slug] = now_ts
        try:
            market = await fetch_gamma_market_by_slug(slug)
        except Exception:
            return
        if not isinstance(market, dict):
            return
        gamma_ptb = extract_price_to_beat_from_market_payload(market)
        if gamma_ptb is None:
            return
        diff_abs = abs(gamma_ptb - local_strike)
        if diff_abs <= self.market_strike_gamma_warn_abs_usd:
            return
        last_warn = float(self.market_strike_last_gamma_warn_ts_by_slug.get(slug, 0.0))
        if now_ts - last_warn < float(self.market_strike_gamma_mismatch_warn_interval_sec):
            return
        self.market_strike_last_gamma_warn_ts_by_slug[slug] = now_ts
        logger.warning(
            f"Strike validation mismatch for {slug}: "
            f"local={float(local_strike):.2f} gamma_priceToBeat={float(gamma_ptb):.2f} "
            f"diff=${float(diff_abs):.2f}. Keeping local opening strike."
        )

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
            if self._is_authoritative_strike_source(source):
                await self._maybe_validate_strike_with_gamma(slug, cached)
                return cached
            logger.warning(
                f"[STRIKE] Ignoring non-authoritative cached strike for {slug}: "
                f"source={source} value=${float(cached):.2f}. Waiting for Polymarket Chainlink open lock."
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

        # 3) Primary: Polymarket Chainlink history around market open
        anchor = self._resolve_opening_strike_from_polymarket_history(start_ts)
        if anchor is not None:
            anchor_ts, anchor_px = anchor
            self.market_strike_cache_by_slug[slug] = anchor_px
            self.market_strike_source_by_slug[slug] = "polymarket_chainlink_open"
            self.market_strike_provisional_by_slug.pop(slug, None)
            self.market_strike_provisional_source_by_slug.pop(slug, None)
            logger.info(
                f"[STRIKE] Locked opening strike from Polymarket Chainlink history: "
                f"${float(anchor_px):.2f} for slug={slug} "
                f"(sample_dt={anchor_ts - float(start_ts):+.2f}s)"
            )
            await self._maybe_validate_strike_with_gamma(slug, anchor_px)
            return anchor_px

        # 4) If market just started and we already have a fresh Polymarket tick, latch it.
        live_latched = self._maybe_latch_opening_strike_from_live_reference(
            slug=slug,
            start_ts=int(start_ts),
        )
        if live_latched is not None:
            await self._maybe_validate_strike_with_gamma(slug, live_latched)
            return live_latched

        # 5) Fallback: parse from question text
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

        # 6) Generic external spot history fallback
        anchor = self._resolve_opening_strike_from_history(start_ts)
        if anchor is not None:
            anchor_ts, anchor_px = anchor
            self._set_provisional_strike(slug=slug, strike=anchor_px, source="spot_history_open")
            logger.info(
                f"[STRIKE] Provisional strike from spot history fallback: "
                f"${float(anchor_px):.2f} for slug={slug} "
                f"(sample_dt={anchor_ts - float(start_ts):+.2f}s; not authoritative)"
            )

        # 7) Binance REST backfill as last resort
        import asyncio as _asyncio
        now_ts = time.time()
        last_try = float(self.market_strike_rest_last_try_ts_by_slug.get(slug, 0.0))
        if now_ts >= float(start_ts) and (now_ts - last_try) >= float(self.market_strike_rest_retry_sec):
            self.market_strike_rest_last_try_ts_by_slug[slug] = now_ts
            backfilled = await _asyncio.to_thread(self._fetch_binance_open_price_sync, start_ts)
            if backfilled is not None:
                self._set_provisional_strike(slug=slug, strike=backfilled, source="binance_rest_open")
                logger.info(
                    f"[STRIKE] Provisional strike from Binance REST backfill: "
                    f"${float(backfilled):.2f} for slug={slug} "
                    "(not authoritative; trading should wait for Polymarket Chainlink open lock)"
                )

        logger.debug(f"[STRIKE] Opening strike pending for slug={slug}; provisional={strike}")
        return None

    # ------------------------------------------------------------------
    # Fair probability
    # ------------------------------------------------------------------

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
                est_sigma = self._estimate_external_spot_sigma_annualized()
                if est_sigma and est_sigma > 0:
                    sigma = est_sigma
                sigma = sigma * self.maker_digital_vol_scale
                sigma = max(self.maker_digital_sigma_floor, min(self.maker_digital_sigma_ceiling, sigma))

                # Dynamic sigma: time decay — reduce sigma as expiry approaches
                if self.maker_digital_sigma_time_decay_enabled and time_left_sec > 0:
                    ref = self.maker_digital_sigma_time_decay_ref_sec
                    decay = max(self.maker_digital_sigma_time_decay_min, min(1.0, time_left_sec / ref))
                    sigma = sigma * Decimal(str(round(decay, 4)))
                    sigma = max(self.maker_digital_sigma_floor, sigma)

                instrument = self.cache.instrument(self._normalize_instrument_id(instrument_id)) if instrument_id is not None else None
                outcome = self._extract_outcome_from_instrument(instrument) if instrument is not None else ""

                # Implied sigma: derive σ from market mid price (DIAGNOSTIC ONLY)
                # Computed and logged to compare with realized sigma, but NOT
                # blended into fair price — that would create circular calibration
                # (market price → implied σ → fair ≈ market price → no edge).
                implied_sigma_used = None
                sigma_before_implied_floor = sigma
                implied_sigma_floor = None
                implied_sigma_floor_applied = False
                if (
                    getattr(self, "maker_implied_sigma_enabled", False)
                    and strike is not None
                    and time_left_sec > 30
                    and float(market_mid) > 0.05
                    and float(market_mid) < 0.95
                ):
                    imp_sigma = MakerEngine.implied_sigma_from_market_mid(
                        market_mid=float(market_mid),
                        spot=float(external),
                        strike=float(strike),
                        time_left_sec=time_left_sec,
                        outcome=outcome or "up",
                    )
                    if imp_sigma is not None and imp_sigma > 0:
                        implied_sigma_used = imp_sigma
                        # Guardrail only: prevent realized sigma from being
                        # absurdly lower than what the market implies (floor at 60% of implied)
                        sigma_floor_from_implied = imp_sigma * Decimal("0.6")
                        implied_sigma_floor = sigma_floor_from_implied
                        if sigma < sigma_floor_from_implied:
                            sigma = max(sigma, sigma_floor_from_implied)
                            sigma = max(self.maker_digital_sigma_floor, min(self.maker_digital_sigma_ceiling, sigma))
                            implied_sigma_floor_applied = sigma > sigma_before_implied_floor

                self.last_digital_pricer_diagnostics = {
                    "sigma": sigma,
                    "implied_sigma": implied_sigma_used,
                    "sigma_before_implied_floor": sigma_before_implied_floor,
                    "implied_sigma_floor": implied_sigma_floor,
                    "implied_sigma_floor_applied": implied_sigma_floor_applied,
                    "strike": strike,
                    "time_left_sec": time_left_sec,
                    "outcome": outcome,
                    "market_mid": market_mid,
                    "spot": external,
                }

                if strike is None:
                    if time.time() - self._last_strike_fallback_log_ts >= self.strike_fallback_log_interval_sec:
                        logger.debug("Digital pricer fallback: strike unavailable, using drift mode.")
                        self._last_strike_fallback_log_ts = time.time()
                else:
                    now_ts = time.time()
                    if now_ts - self._last_digital_pricer_log_ts >= 60:
                        up_prob = MakerEngine.digital_up_probability(
                            spot=float(external),
                            strike=float(strike),
                            sigma_annual=float(sigma),
                            time_left_sec=time_left_sec,
                        )
                        fair_for_token = up_prob
                        if outcome == "down":
                            fair_for_token = Decimal("1.0") - up_prob
                        imp_str = f" implied_σ={float(implied_sigma_used):.4f}" if implied_sigma_used else ""
                        fair_color = "green" if fair_for_token >= Decimal("0.60") else "yellow" if fair_for_token >= Decimal("0.40") else "red"
                        side_color = "green" if self.active_side.value == "UP" else "red" if self.active_side.value == "DOWN" else "yellow"
                        source_color = "cyan" if (self.latest_external_spot_source or "") == "polymarket_chainlink_ws" else "yellow"
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
                pricer_mode=self.maker_fair_pricer_mode
            )

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
