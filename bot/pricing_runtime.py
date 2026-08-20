from __future__ import annotations

import asyncio
import math
import time
from decimal import Decimal
from typing import Any, List, Optional, Protocol, Tuple

from loguru import logger

from bot.wallet_ops import ensure_balance_clob_client
from execution.rebate_model import bps_to_fee_rate


class PricingRuntimeHost(Protocol):
    real_price_history_by_inst: dict[str, list[Decimal]]
    real_price_history: list[Decimal]
    instrument_id: Any
    cache: Any
    maker_fee_rate_default_decimal: Decimal
    maker_fee_rate_legacy_bps_default: int
    _fee_rate_local_cache_by_token: dict[str, Any]
    fee_rate_fetch_interval_sec: int
    fee_rate_client: Any
    _last_fee_log_state_by_token: dict[str, Any]
    fee_log_interval_sec: int
    orderbook_levels_cache_by_token: dict[str, Any]
    orderbook_fetch_interval_sec: int
    orderbook_levels_limit: int
    live_inventory_cost: dict[str, Any]
    current_token_id: Optional[str]
    _balance_clob_client: Any

    def _normalize_instrument_id(self, instrument_id: Any) -> Any: ...
    def _instrument_key(self, instrument_id: Any) -> str: ...


class PricingRuntimeMixin:
    """
    Pricing, fee-rate, orderbook, and sizing helpers for the live quote path.

    These methods support quoting and inventory sizing, but they are not the
    strategy decision policy itself.
    """

    def _momentum_history_for_instrument(self: PricingRuntimeHost, instrument_id: Any) -> List[Decimal]:
        inst_key = str(instrument_id) if instrument_id is not None else ""
        if inst_key:
            per_inst = self.real_price_history_by_inst.get(inst_key)
            if per_inst:
                return per_inst
        return self.real_price_history

    def _get_total_sellable_qty(self: PricingRuntimeHost, instrument_ids: Optional[List[Any]] = None) -> Decimal:
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

    def _infer_market_fee_rate_default(self: PricingRuntimeHost) -> Decimal:
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
                return Decimal("0.001")
            if "ncaab" in text or "serie a" in text or "serie_a" in text:
                return Decimal("0.0175")
        except Exception:
            pass
        return default_rate

    async def _get_dynamic_fee_rate(self: PricingRuntimeHost, token_id: Optional[str] = None) -> Optional[Decimal]:
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
                and cached_rate >= 0
                and cached_ts > 0
                and (now_ts - cached_ts) < self.fee_rate_fetch_interval_sec
            ):
                return cached_rate

        fee_rate = await self.fee_rate_client.get_fee_rate_decimal(token)
        source = "clob_fee_rate"
        if fee_rate is None or fee_rate < 0:
            fee_rate = self._infer_market_fee_rate_default()
            source = "market_type_default"
            if (fee_rate is None or fee_rate < 0) and self.maker_fee_rate_legacy_bps_default > 0:
                fee_rate = bps_to_fee_rate(self.maker_fee_rate_legacy_bps_default)
                source = "legacy_bps_default"
        if fee_rate is None or fee_rate < 0:
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
            client = ensure_balance_clob_client(
                current_client=None,
                logger_info_fn=logger.info,
                logger_warning_fn=logger.warning,
            )
            if client is not None:
                self._balance_clob_client = client
        if client is None:
            return None, None

        try:
            raw = await asyncio.to_thread(client.get_order_book, token)

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

    def _get_confirmed_inventory_qty_for_instrument(self, instrument_id: Optional[Any] = None) -> Decimal:
        """
        Return the strategy's confirmed inventory for a specific instrument.

        This is derived from the bot's internal fill ledger (`live_inventory_cost`),
        not from external position/balance caches, so SELL quoting cannot race ahead
        of a locally confirmed BUY fill.
        """
        inst = instrument_id if instrument_id is not None else self.instrument_id
        inst_key = self._instrument_key(inst)
        if not inst_key:
            return Decimal("0")
        state = self.live_inventory_cost.get(inst_key)
        if not state:
            return Decimal("0")
        qty = Decimal(str(state.get("qty", "0")))
        return max(Decimal("0"), qty)

    def _reconcile_ghost_inventory(
        self: PricingRuntimeHost,
        *,
        instrument_id: Optional[Any],
        confirmed_qty: Decimal,
        onchain_qty: Decimal,
    ) -> Decimal:
        inst = instrument_id if instrument_id is not None else self.instrument_id
        inst_key = self._instrument_key(inst)
        if not inst_key:
            return max(Decimal("0"), onchain_qty)

        now_ts = time.time()
        cooldown_by_inst = getattr(self, "_ghost_inventory_reconcile_ts_by_inst", None)
        if not isinstance(cooldown_by_inst, dict):
            cooldown_by_inst = {}
            setattr(self, "_ghost_inventory_reconcile_ts_by_inst", cooldown_by_inst)
        last_ts = float(cooldown_by_inst.get(inst_key, 0.0))
        if last_ts > 0 and (now_ts - last_ts) < 5.0:
            return max(Decimal("0"), onchain_qty)
        cooldown_by_inst[inst_key] = now_ts

        state = self.live_inventory_cost.setdefault(
            inst_key,
            {
                "qty": Decimal("0"),
                "avg_entry_price": Decimal("0"),
                "entry_fee_remaining": Decimal("0"),
                "opened_ts": 0.0,
            },
        )
        old_qty = max(Decimal("0"), Decimal(str(state.get("qty", "0"))))
        avg_entry = max(Decimal("0"), Decimal(str(state.get("avg_entry_price", "0"))))
        old_fee_remaining = max(Decimal("0"), Decimal(str(state.get("entry_fee_remaining", "0"))))
        opened_ts = float(state.get("opened_ts", 0.0) or 0.0)

        restored_qty = max(Decimal("0"), onchain_qty)
        if restored_qty <= old_qty:
            return restored_qty

        inferred_entry = self._infer_ghost_inventory_entry_price(
            instrument_id=inst,
            instrument_key=inst_key,
            restored_qty=restored_qty,
            now_ts=now_ts,
        )
        if avg_entry <= 0 and inferred_entry is not None and inferred_entry > 0:
            avg_entry = inferred_entry

        restored_fee_remaining = old_fee_remaining
        if old_qty > 0 and old_fee_remaining > 0:
            restored_fee_remaining = old_fee_remaining * (restored_qty / old_qty)

        state["qty"] = restored_qty
        state["avg_entry_price"] = avg_entry
        state["entry_fee_remaining"] = max(Decimal("0"), restored_fee_remaining)
        if opened_ts <= 0:
            state["opened_ts"] = now_ts

        recovered_delta = restored_qty - old_qty
        try:
            self.inventory_delta_shares = max(
                Decimal("0"),
                Decimal(str(getattr(self, "inventory_delta_shares", Decimal("0")))) + recovered_delta,
            )
        except Exception:
            pass

        cleared_order_ids: list[str] = []
        active_orders = getattr(self, "active_maker_orders", None)
        if isinstance(active_orders, dict):
            for order_key, order_state in list(active_orders.items()):
                if str(order_state.get("side", "") or "") != "sell":
                    continue
                if str(order_state.get("instrument_id", "") or "") != str(inst):
                    continue
                order = order_state.get("order")
                if order is not None:
                    client_order_id = str(getattr(order, "client_order_id", "") or "")
                    if client_order_id:
                        cleared_order_ids.append(client_order_id)
                active_orders.pop(order_key, None)

        for attr_name in (
            "_sell_recovery_required_by_inst",
            "_sell_recovery_reason_by_inst",
            "_sell_recovery_venue_cap_by_inst",
        ):
            mapping = getattr(self, attr_name, None)
            if isinstance(mapping, dict):
                mapping.pop(inst_key, None)

        if hasattr(self, "_clear_profit_run_state"):
            try:
                self._clear_profit_run_state(inst)
            except Exception:
                pass

        position_manager = getattr(self, "position_manager", None)
        if position_manager is not None and hasattr(position_manager, "sync_position"):
            try:
                thesis_side = "NONE"
                if hasattr(self, "_side_for_instrument_id"):
                    detected = self._side_for_instrument_id(inst)
                    thesis_side = str(getattr(detected, "value", detected) or "NONE")
                position_manager.sync_position(
                    inst_key=inst_key,
                    qty=restored_qty,
                    opened_ts=float(state.get("opened_ts", now_ts) or now_ts),
                    thesis_side=thesis_side,
                    now_ts=now_ts,
                )
            except Exception:
                pass

        setattr(self, "_force_quote_refresh_once", True)
        setattr(self, "_force_quote_refresh_reason", "ghost_inventory_reconciled")

        if hasattr(self, "_db_strategy_event"):
            try:
                self._db_strategy_event(
                    "GHOST_INVENTORY_RECONCILED",
                    {
                        "instrument_id": str(inst),
                        "confirmed_qty_before": float(confirmed_qty),
                        "onchain_qty": float(onchain_qty),
                        "restored_qty": float(restored_qty),
                        "avg_entry_price": float(avg_entry),
                        "avg_entry_recovered": bool(inferred_entry is not None and inferred_entry > 0),
                        "cleared_sell_orders": cleared_order_ids,
                    },
                )
            except Exception:
                pass

        logger.warning(
            "Ghost inventory reconciled: "
            f"inst={inst_key} confirmed_before={float(old_qty):.4f} "
            f"onchain={float(onchain_qty):.4f} restored={float(restored_qty):.4f} "
            f"avg_entry={float(avg_entry):.4f} "
            f"cleared_sell_orders={len(cleared_order_ids)}"
        )
        return restored_qty

    def _infer_ghost_inventory_entry_price(
        self: PricingRuntimeHost,
        *,
        instrument_id: Any,
        instrument_key: str,
        restored_qty: Decimal,
        now_ts: float,
    ) -> Optional[Decimal]:
        """
        Recover cost basis when a Polymarket maker fill matched on-chain but
        the venue fill ack failed before Nautilus emitted ORDER_FILLED.
        """
        candidates: list[tuple[float, Decimal, Decimal, str]] = []
        active_orders = getattr(self, "active_maker_orders", None)
        if isinstance(active_orders, dict):
            for state in active_orders.values():
                if str(state.get("side", "") or "").lower() != "buy":
                    continue
                if str(state.get("instrument_id", "") or "") != str(instrument_id):
                    continue
                try:
                    price = Decimal(str(state.get("price", "0")))
                    qty = Decimal(str(state.get("quantity", "0")))
                    created_ts = float(state.get("created_ts", 0.0) or 0.0)
                except Exception:
                    continue
                candidates.append((created_ts, price, qty, "active_order"))

        recent_submits = getattr(self, "recent_buy_submit_by_inst", None)
        if isinstance(recent_submits, dict):
            state = recent_submits.get(instrument_key)
            if isinstance(state, dict):
                try:
                    price = Decimal(str(state.get("price", "0")))
                    qty = Decimal(str(state.get("quantity", "0")))
                    created_ts = float(state.get("created_ts", 0.0) or 0.0)
                except Exception:
                    price = Decimal("0")
                    qty = Decimal("0")
                    created_ts = 0.0
                candidates.append((created_ts, price, qty, "recent_submit"))

        trade_db = getattr(self, "trade_db", None)
        if trade_db is not None and hasattr(trade_db, "load_recent_buy_submits"):
            try:
                rows = trade_db.load_recent_buy_submits(str(instrument_id), limit=20)
            except Exception:
                rows = []
            for row in rows or []:
                try:
                    price = Decimal(str(row.get("price", "0")))
                    qty = Decimal(str(row.get("qty", "0")))
                    created_ts = float(row.get("epoch_ts", 0.0) or 0.0)
                except Exception:
                    continue
                candidates.append((created_ts, price, qty, "trade_db"))

        fresh_candidates = [
            (created_ts, price, qty, source)
            for created_ts, price, qty, source in candidates
            if price > 0
            and qty > 0
            and (created_ts <= 0 or now_ts - created_ts <= 180.0)
            and qty >= min(restored_qty, Decimal("1"))
        ]
        if not fresh_candidates:
            return None
        fresh_candidates.sort(key=lambda item: item[0], reverse=True)
        created_ts, price, qty, source = fresh_candidates[0]
        logger.warning(
            "Recovered ghost inventory cost basis from recent BUY submit: "
            f"inst={instrument_key} source={source} price={float(price):.4f} "
            f"submit_qty={float(qty):.6f} restored_qty={float(restored_qty):.6f}"
        )
        return price

    def _get_effective_sellable_qty(self, instrument_id: Optional[Any]) -> Decimal:
        """
        Conservative sellable qty:
        min(cache open positions, on-chain conditional balance with safety buffer).
        """
        sell_fill_balance_sync_grace_sec = 8.0
        confirmed_qty = self._get_confirmed_inventory_qty_for_instrument(instrument_id=instrument_id)
        local_qty = self._get_sellable_qty_for_current_instrument(instrument_id=instrument_id)
        inst_txt = str(instrument_id or "")
        inst_key = self._instrument_key(instrument_id)
        token_id = self._extract_token_id_from_instrument(inst_txt)
        onchain_qty = self._get_conditional_balance_for_token(token_id=token_id, force_refresh=False)
        recent_sell_ts = float(getattr(self, "recent_sell_fill_ts_by_inst", {}).get(inst_key, 0.0)) if inst_key else 0.0
        recent_sell_elapsed = (time.time() - recent_sell_ts) if recent_sell_ts > 0 else None

        if (
            onchain_qty is not None
            and recent_sell_elapsed is not None
            and recent_sell_elapsed < sell_fill_balance_sync_grace_sec
            and (onchain_qty - confirmed_qty) >= Decimal("1.0")
        ):
            logger.info(
                "Balance sync grace after SELL fill: "
                f"inst={inst_key} internal={float(confirmed_qty):.4f} "
                f"onchain={float(onchain_qty):.4f} elapsed={recent_sell_elapsed:.2f}s"
            )
            fallback_qty = min(confirmed_qty, local_qty) if local_qty > 0 else confirmed_qty
            return max(Decimal("0"), fallback_qty)

        if onchain_qty is not None and (onchain_qty - confirmed_qty) >= Decimal("1.0"):
            if recent_sell_elapsed is not None:
                refreshed_onchain = self._get_conditional_balance_for_token(token_id=token_id, force_refresh=True)
                if refreshed_onchain is not None:
                    onchain_qty = refreshed_onchain
                if onchain_qty is None or (onchain_qty - confirmed_qty) < Decimal("1.0"):
                    fallback_qty = min(confirmed_qty, local_qty) if local_qty > 0 else confirmed_qty
                    return max(Decimal("0"), fallback_qty)
                if recent_sell_elapsed < sell_fill_balance_sync_grace_sec:
                    fallback_qty = min(confirmed_qty, local_qty) if local_qty > 0 else confirmed_qty
                    return max(Decimal("0"), fallback_qty)
            recent_buy_ts = float(getattr(self, "recent_buy_fill_ts_by_inst", {}).get(inst_key, 0.0))
            if time.time() - recent_buy_ts > 15.0:
                logger.warning(
                    f"GHOST INVENTORY RECOVERED: internal={float(confirmed_qty):.4f}, onchain={float(onchain_qty):.4f}. "
                    "A previous sell matched locally but reverted on-chain. Restoring sellable qty."
                )
                reconciled_qty = self._reconcile_ghost_inventory(
                    instrument_id=instrument_id,
                    confirmed_qty=confirmed_qty,
                    onchain_qty=onchain_qty,
                )
                safe_qty = reconciled_qty * (Decimal("1") - getattr(self, "conditional_balance_safety_buffer_pct", Decimal("0.02")))
                return max(Decimal("0"), safe_qty)
        
        if confirmed_qty <= 0:
            return Decimal("0")
        venue_cap = self._sell_recovery_venue_cap_by_inst.get(inst_key, None) if inst_key else None
        recent_buy_ts = float(self.recent_buy_fill_ts_by_inst.get(inst_key, 0.0)) if inst_key else 0.0
        after_buy_window_active = (
            recent_buy_ts > 0
            and self.sellable_fallback_after_buy_sec > 0
            and (time.time() - recent_buy_ts) <= float(self.sellable_fallback_after_buy_sec)
        )
        after_buy_buffer = getattr(self, "sellable_after_buy_buffer_shares", Decimal("0"))

        def _quantize_safe(qty: Decimal) -> Decimal:
            return max(Decimal("0"), qty)

        if after_buy_window_active:
            base_qty = min(confirmed_qty, local_qty) if local_qty > 0 else confirmed_qty
            applied_venue_cap = False
            if venue_cap is not None and venue_cap > 0:
                base_qty = min(base_qty, venue_cap)
                applied_venue_cap = True
            if not applied_venue_cap:
                base_qty = _quantize_safe(base_qty - after_buy_buffer)
            if base_qty > 0:
                return base_qty

        if (
            onchain_qty is not None
            and onchain_qty <= 0
            and inst_key
            and self.sellable_fallback_after_buy_sec > 0
        ):
            if after_buy_window_active:
                if local_qty > 0:
                    return _quantize_safe(min(confirmed_qty, local_qty) - after_buy_buffer)
                return confirmed_qty
        if onchain_qty is None:
            fallback_qty = min(confirmed_qty, local_qty) if local_qty > 0 else confirmed_qty
            if venue_cap is not None and venue_cap > 0:
                fallback_qty = min(fallback_qty, venue_cap)
            return _quantize_safe(fallback_qty)
        safe_onchain = onchain_qty * (Decimal("1") - self.conditional_balance_safety_buffer_pct)
        safe_onchain = _quantize_safe(safe_onchain)
        candidates = [confirmed_qty]
        if local_qty > 0:
            candidates.append(local_qty)
        candidates.append(safe_onchain)
        if venue_cap is not None and venue_cap > 0:
            candidates.append(venue_cap)
        return min(candidates)

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
            qty = max(self.maker_fixed_shares, min_qty)
            if limit_price > 0 and self.maker_max_order_usdc > 0:
                max_shares_by_notional = self.maker_max_order_usdc / limit_price
                if qty > max_shares_by_notional:
                    qty = max(max_shares_by_notional, min_qty)
            return qty

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

    def _compute_recent_volatility(self, instrument_id: Any = None) -> Optional[Decimal]:
        """
        Compute volatility from one outcome token's REAL quote history only.

        UP and DOWN tokens are complementary contracts.  Combining their
        alternating mids makes normal updates look like large returns and
        creates a false ``EXTREME`` volatility regime.  The global history is
        retained only as a compatibility fallback when no instrument is known.
        Uses clipped returns + max(rolling_std, ewma_std).
        """
        min_quotes = max(2, self.maker_vol_warmup_quotes)
        history = self._momentum_history_for_instrument(instrument_id)
        if len(history) < min_quotes:
            return None

        window = max(5, self.maker_vol_rolling_window)
        recent = history[-(window + 1):]
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

        roll = returns[-window:]
        mean_r = sum(roll) / len(roll)
        var = sum((r - mean_r) ** 2 for r in roll) / len(roll)
        rolling_std = math.sqrt(max(0.0, var))

        alpha = max(0.01, min(0.99, float(self.maker_vol_ewma_alpha)))
        ewma_var = roll[0] ** 2
        for r in roll[1:]:
            ewma_var = alpha * (r ** 2) + (1.0 - alpha) * ewma_var
        ewma_std = math.sqrt(max(0.0, ewma_var))

        return Decimal(str(max(rolling_std, ewma_std)))
