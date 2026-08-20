from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from bot.enums import ActiveSide, MarketPhase
from bot.quote_service import (
    build_directional_snapshot,
    compute_requote_target_version,
    extract_instrument_tick,
    lifecycle_ttl_for_order,
    log_no_quote_diagnostics,
    reconcile_unwanted_quotes,
    should_requote_existing_order,
)
from bot.recovery_exit_ladder import recovery_exit_owns_sell_reservation


class QuoteRuntimeMixin:
    @staticmethod
    def _reason_family(reason: str) -> str:
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
    def _latest_observation_supports_locked_side(active_side: Any, side_score: Decimal) -> bool:
        if active_side == ActiveSide.UP:
            return side_score > 0
        if active_side == ActiveSide.DOWN:
            return side_score < 0
        return False

    def _should_skip_buy_submit_for_quote_drift(
        self,
        *,
        instrument_id: Any,
        quote_now: tuple[Decimal, Decimal] | None,
        directional_snapshot: Optional[Dict[str, Any]],
        instrument: Any,
    ) -> bool:
        if quote_now is None or not directional_snapshot:
            return False
        try:
            planned_bid = Decimal(str(directional_snapshot.get("planned_best_bid")))
            planned_ask = Decimal(str(directional_snapshot.get("planned_best_ask")))
            planned_quote_ts = float(directional_snapshot.get("planned_quote_ts") or 0.0)
        except Exception:
            return False
        if planned_bid <= 0 or planned_ask <= 0 or planned_quote_ts <= 0:
            return False
        quote_age_sec = max(0.0, time.time() - planned_quote_ts)
        max_quote_age_sec = float(
            getattr(self, "maker_buy_planned_quote_max_age_sec", 10.0)
        )
        received_quote_ts = getattr(self, "last_quote_received_ts_by_inst", {}).get(
            str(instrument_id)
        )
        if received_quote_ts is not None:
            try:
                received_quote_age_sec = max(0.0, time.time() - float(received_quote_ts))
            except (TypeError, ValueError):
                received_quote_age_sec = None
            if (
                received_quote_age_sec is not None
                and received_quote_age_sec > max_quote_age_sec
            ):
                logger.warning(
                    "Skip BUY quote: cached top-of-book has not been received recently "
                    f"(inst={self._instrument_key(instrument_id)}, "
                    f"age={received_quote_age_sec:.2f}s, max_age={max_quote_age_sec:.2f}s)"
                )
                return True
        if quote_age_sec > max_quote_age_sec:
            logger.warning(
                "Skip BUY quote: planned quote snapshot is stale "
                f"(inst={self._instrument_key(instrument_id)}, age={quote_age_sec:.2f}s, "
                f"max_age={max_quote_age_sec:.2f}s)"
            )
            return True
        current_bid, current_ask = quote_now
        tick = extract_instrument_tick(instrument, default_tick="0.01")
        max_drift = tick * 2
        if (
            abs(current_bid - planned_bid) > max_drift
            or abs(current_ask - planned_ask) > max_drift
        ):
            logger.warning(
                "Skip BUY quote: top-of-book drifted before submit "
                f"(inst={self._instrument_key(instrument_id)} "
                f"planned={float(planned_bid):.4f}/{float(planned_ask):.4f} "
                f"current={float(current_bid):.4f}/{float(current_ask):.4f} "
                f"max_drift={float(max_drift):.4f})"
            )
            return True
        return False

    async def _prepare_quote_cycle(self) -> Optional[Dict[str, Any]]:
        if self.maker_kill_switch:
            return None
        if time.time() < self.quote_pause_until_ts:
            return None

        phase = self._update_market_phase()
        if phase in (MarketPhase.WAITING, MarketPhase.SETTLING):
            self._cancel_active_maker_orders()
            return None
        await self._maybe_finalize_side_decision(time.time(), phase)
        if self.bi_side_enabled and self.active_side == ActiveSide.NONE:
            if self.inventory_delta_shares <= 0:
                self._cancel_active_maker_orders()
                return None

        balance_forced_sell_only = False
        regime_guard_active = False
        if not self._is_dry_run_mode():
            balance = self._refresh_balance_cache()
            if balance is not None:
                required = self.maker_quote_size_usdc * Decimal("1.1")
                if balance < required:
                    gross_sellable = self._get_total_sellable_qty(self._maker_quote_instruments())
                    if gross_sellable > 0:
                        balance_forced_sell_only = True
                        if time.time() - getattr(self, "_last_balance_warn_ts", 0) >= 60:
                            logger.warning(
                                f"Balance pre-check: low USDC "
                                f"(available={float(balance):.2f}, needed≈{float(required):.2f}). "
                                f"Switching to SELL-only until balance recovers. "
                                f"net_inventory={float(self.inventory_delta_shares):.4f} "
                                f"gross_sellable={float(gross_sellable):.4f}"
                            )
                            self._last_balance_warn_ts = time.time()
                    else:
                        if time.time() - getattr(self, "_last_balance_warn_ts", 0) >= 60:
                            logger.warning(
                                f"Balance pre-check: insufficient USDC and no inventory "
                                f"(available={float(balance):.2f}, needed≈{float(required):.2f}). "
                                f"Skipping maker quotes."
                            )
                            self._last_balance_warn_ts = time.time()
                        return None
        if self.regime_guard_enabled:
            now_guard_ts = time.time()
            if self.regime_guard_conservative_until_ts > 0 and now_guard_ts >= self.regime_guard_conservative_until_ts:
                self.regime_guard_conservative_until_ts = 0.0
                self._db_strategy_event("REGIME_GUARD_RECOVERED", {"ts": now_guard_ts})
            elif now_guard_ts < self.regime_guard_conservative_until_ts:
                regime_guard_active = True
        if self.inventory_delta_shares <= 0 and self._startup_rehydrated_inventory_force_sell_only:
            self._startup_rehydrated_inventory_force_sell_only = False
        forced_sell_only = balance_forced_sell_only or self._startup_rehydrated_inventory_force_sell_only

        await self._maybe_taker_exit_positions(time.time(), is_simulation=self._is_dry_run_mode())
        await self._maybe_maker_urgent_exit(time.time())

        now_ts = time.time()
        force_quote_refresh_once = bool(getattr(self, "_force_quote_refresh_once", False))
        if not force_quote_refresh_once and now_ts - self.last_quote_update_ts < self.quote_refresh_sec:
            return None
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

        for order_key, state in list(self.active_maker_orders.items()):
            created_ts = float(state.get("created_ts", 0.0))
            ttl = lifecycle_ttl_for_order(
                state,
                normal_ttl_sec=float(self.maker_order_ttl_sec),
                loss_sell_reprice_min_interval_sec=float(
                    getattr(self, "maker_loss_sell_reprice_min_interval_sec", self.maker_order_ttl_sec)
                ),
            )
            if ttl is None:
                continue
            if created_ts <= 0 or (now_ts - created_ts) >= ttl:
                side = str(state.get("side", "") or "")
                is_urgent = " (urgent_exit)" if state.get("is_urgent_exit") else ""
                logger.info(f"Maker order [{side}]{is_urgent} exceeded TTL={ttl}s, cancel and requote.")
                self._cancel_maker_order_side(order_key, reason="ttl")

        if abs(self.inventory_delta_shares) > self.maker_max_inventory_shares:
            self._activate_maker_kill_switch(
                f"Inventory {self.inventory_delta_shares} exceeds max {self.maker_max_inventory_shares}"
            )
            return None

        # This is diagnostic-only cycle state.  Quote economics receive the
        # correct per-instrument value in ``_evaluate_quote_targets``.
        recent_vol = self._compute_recent_volatility(self.instrument_id)
        if recent_vol is None:
            logger.debug(
                f"Volatility gate warmup: real_quotes={len(self._momentum_history_for_instrument(self.instrument_id))}/{self.maker_vol_warmup_quotes}"
            )

        target_instruments = self._maker_quote_instruments()
        if not target_instruments:
            if self.bi_side_enabled:
                self._cancel_active_maker_orders()
            return None

        time_passed = max(0.0, now_ts - self.requote_bucket_last_refill)
        self.requote_bucket_tokens = min(
            self.maker_requote_max_per_sec,
            self.requote_bucket_tokens + (time_passed * self.maker_requote_max_per_sec),
        )
        self.requote_bucket_last_refill = now_ts

        target_inst_set = {str(inst) for inst in target_instruments}
        end_ts = getattr(self, "current_market_end_timestamp", None)
        time_left_sec_global = (end_ts - now_ts) if end_ts is not None else None

        return {
            "phase": phase,
            "forced_sell_only": forced_sell_only,
            "regime_guard_active": regime_guard_active,
            "now_ts": now_ts,
            "recent_vol": recent_vol,
            "target_instruments": target_instruments,
            "target_inst_set": target_inst_set,
            "end_ts": end_ts,
            "time_left_sec_global": time_left_sec_global,
        }

    async def _submit_quote_cycle(
        self,
        *,
        phase: MarketPhase,
        now_ts: float,
        target_instruments: List[Any],
        target_inst_set: Set[str],
        desired_quotes: Dict[str, Dict[str, Any]],
        diag_context_by_inst: Dict[str, Dict[str, Any]],
    ) -> None:
        submitted_attempts = 0

        # Fair-edge research candidates must never influence the live order
        # book. In particular, they cannot cause an existing live quote to be
        # cancelled merely because their counterfactual target differs.
        live_desired_quotes = {
            key: desired
            for key, desired in desired_quotes.items()
            if not desired.get("fair_edge_bucket_shadow")
        }
        for key, desired in desired_quotes.items():
            if desired.get("fair_edge_bucket_shadow") and key in self.active_maker_orders:
                # Keep the real quote untouched; its normal lifecycle will
                # resume once the fair-edge gate allows a live replacement.
                live_desired_quotes[key] = {"should_quote": True}
        reconcile_unwanted_quotes(
            active_maker_orders=self.active_maker_orders,
            desired_quotes=live_desired_quotes,
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

        for order_key, desired in desired_quotes.items():
            if not bool(desired.get("should_quote", False)):
                continue
            submitted_attempts += 1
            side = str(desired["side"])
            inst_id = desired["instrument_id"]
            inst_key = self._instrument_key(inst_id)
            recovery_stage = getattr(self, "recovery_exit_stage_by_inst", {}).get(inst_key)
            if side == "sell" and recovery_exit_owns_sell_reservation(recovery_stage):
                # A confirmed-invalidation recovery owns this instrument's
                # SELL reservation. Do not let the ordinary TP lifecycle
                # cancel, requote, or recreate a 0.97 maker order here.
                continue
            limit_price = Decimal(str(desired["price"]))
            econ = desired["econ"]
            dynamic_fee_rate = desired.get("dynamic_fee_rate")
            directional_snapshot = build_directional_snapshot(desired)

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
            if desired.get("fair_edge_bucket_shadow"):
                # A real open order retains ownership of the order key. The
                # counterfactual is only submit-time observable when it would
                # have been a new order, and it must not requote/cancel live.
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
                    target_qty_override=desired.get("target_qty_override"),
                    fair_edge_bucket_shadow=desired.get("fair_edge_bucket_shadow"),
                )
                continue
            if should_requote_existing_order(
                current=current,
                target_version=target_version,
                now_ts=now_ts,
                maker_requote_min_age_sec=float(self.maker_requote_min_age_sec),
                side=side,
                maker_requote_min_age_sec_sell=float(self.maker_requote_min_age_sec_sell),
                desired_loss_sell_reason=str(desired.get("loss_sell_reason", "") or ""),
            ):
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
                target_qty_override=desired.get("target_qty_override"),
                fair_edge_bucket_shadow=desired.get("fair_edge_bucket_shadow"),
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
