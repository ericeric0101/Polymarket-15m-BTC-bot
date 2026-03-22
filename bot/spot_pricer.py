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
from execution.maker_engine import MakerEngine


class SpotPricerMixin:
    """Mixin providing BTC spot price, Binance WS, and fair probability logic."""

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

    # ------------------------------------------------------------------
    # Spot price fetch
    # ------------------------------------------------------------------

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

    def _resolve_opening_strike_from_history(self, start_ts: int) -> Optional[tuple]:
        return resolve_opening_strike_from_history(
            external_spot_history=self.external_spot_history,
            start_ts=start_ts,
            max_lag_sec=float(self.market_strike_anchor_max_lag_sec),
            near_window_sec=float(self.market_strike_anchor_near_sec),
        )

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

        if slug and slug in self.market_strike_cache_by_slug:
            cached = self.market_strike_cache_by_slug[slug]
            await self._maybe_validate_strike_with_gamma(slug, cached)
            return cached

        info = getattr(instrument, "info", None) or {}
        if not isinstance(info, dict):
            info = {}
        question = str(info.get("question", "") or "")
        strike = self._extract_strike_from_question(question)
        if strike is not None and slug:
            self.market_strike_cache_by_slug[slug] = strike
            self.market_strike_source_by_slug[slug] = "parsed"
            logger.info(f"✓ Locked opening strike for {slug} via question parsing: ${strike:,.2f}")
        return strike

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
            if self.current_market_open_spot is None and self.current_market_slug:
                self.current_market_open_spot = external
                logger.info(f"✓ Locked current_market_open_spot for {self.current_market_slug}: ${external:,.2f}")

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

                if strike is None:
                    if time.time() - self._last_strike_fallback_log_ts >= self.strike_fallback_log_interval_sec:
                        logger.debug("Digital pricer fallback: strike unavailable, using drift mode.")
                        self._last_strike_fallback_log_ts = time.time()
                else:
                    now_ts = time.time()
                    if now_ts - self._last_digital_pricer_log_ts >= 30:
                        up_prob = MakerEngine.digital_up_probability(
                            spot=float(external),
                            strike=float(strike),
                            sigma_annual=float(sigma),
                            time_left_sec=time_left_sec,
                        )
                        fair_for_token = up_prob
                        if outcome == "down":
                            fair_for_token = Decimal("1.0") - up_prob
                        logger.info(
                            "Digital pricer inputs: "
                            f"spot={float(external):.2f} strike={float(strike):.2f} "
                            f"sigma={float(sigma):.4f} t_left={time_left_sec:.1f}s "
                            f"token_outcome={outcome or 'unknown'} "
                            f"up_prob={float(up_prob):.4f} "
                            f"fair_down={float(Decimal('1.0') - up_prob):.4f} "
                            f"fair_for_token={float(fair_for_token):.4f} "
                            f"active_side={self.active_side.value}"
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
