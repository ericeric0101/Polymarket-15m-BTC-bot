from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Any, Dict, Protocol

from loguru import logger

from bot.enums import ActiveSide, MarketPhase
from bot.lifecycle import determine_lifecycle_timer_action, select_next_market_window
from bot.ops import handle_waiting_phase_search
from bot.post_trade import compute_settlement_summary


class StrategyLifecycleHost(Protocol):
    """
    Minimum runtime contract for StrategyLifecycleMixin.

    The mixin still relies on many strategy-level methods; this protocol lists
    the critical ones so lifecycle refactors do not break silently.
    """

    market_phase: MarketPhase
    current_market_slug: str
    current_market_end_timestamp: Any
    inventory_delta_shares: Decimal
    market_cycle_realized_net_usdc: Decimal
    active_side: ActiveSide
    active_maker_orders: Dict[str, Dict[str, Any]]

    def _db_strategy_event(self, event_type: str, payload: Dict[str, Any]) -> None: ...
    def _cancel_active_maker_orders(self) -> None: ...
    def _cancel_maker_order_side(self, order_key: str, reason: str = "") -> None: ...
    def _record_market_settlement(self) -> None: ...
    def _update_terminal_dashboard_snapshot(self) -> None: ...
    def _append_cycle_and_maybe_trigger_regime_guard(
        self,
        *,
        cycle_combined_pnl: float,
        slug: str,
        source: str,
    ) -> None: ...


class StrategyLifecycleMixin:
    """
    Runtime lifecycle orchestration for the live BTC 15-minute bot.

    This keeps market phase transition, settlement bookkeeping, and proactive
    next-market search out of `run_bot.py` while preserving existing behavior.
    """

    def _transition_market_phase(self, new_phase: MarketPhase, now_ts: float) -> None:
        old_phase = self.market_phase
        self.market_phase = new_phase

        end_ts = getattr(self, "current_market_end_timestamp", None)
        time_left = (end_ts - now_ts) if end_ts else None

        logger.warning(
            f"MARKET PHASE: {old_phase.value} → {new_phase.value} "
            f"slug={self.current_market_slug or '-'} "
            f"time_left={time_left / 60:.1f}m" if time_left is not None else
            f"MARKET PHASE: {old_phase.value} → {new_phase.value} "
            f"slug={self.current_market_slug or '-'} "
            f"time_left=N/A"
        )
        self._db_strategy_event(
            "MARKET_PHASE_CHANGE",
            {
                "from": old_phase.value,
                "to": new_phase.value,
                "slug": self.current_market_slug,
                "time_left_sec": time_left,
            },
        )

        if new_phase == MarketPhase.SETTLING:
            self._cancel_active_maker_orders()
            self._record_market_settlement()
            logger.info("Settling: all maker orders cancelled. Waiting for grace period.")
        elif new_phase == MarketPhase.WAITING:
            self._cancel_active_maker_orders()
            self.current_market_end_timestamp = None
            logger.info("Waiting: proactively searching for next market.")
        elif new_phase == MarketPhase.REDUCE_ONLY:
            for order_key, state in list(self.active_maker_orders.items()):
                if str(state.get("side", "")) == "buy":
                    self._cancel_maker_order_side(order_key, reason="reduce_only")
        self._update_terminal_dashboard_snapshot()

    def _record_market_settlement(self) -> None:
        try:
            raw_inv = float(self.inventory_delta_shares)
            ledger_inv = 0.0
            inventory_side = None
            has_inventory_ledger = bool(self.live_inventory_cost)
            for inv_key, inv_state in self.live_inventory_cost.items():
                inv_qty = float(inv_state.get("qty", 0))
                if inv_qty > 0.001:
                    detected = self._side_for_instrument_id(inv_key)
                    if detected != ActiveSide.NONE:
                        ledger_inv += inv_qty
                        if inventory_side is None:
                            inventory_side = detected.value

            inv = ledger_inv if has_inventory_ledger else raw_inv
            if has_inventory_ledger and abs(raw_inv - ledger_inv) > 0.001:
                logger.warning(
                    f"Settlement inventory reconciled from ledger: "
                    f"raw_inv={raw_inv:.6f} ledger_inv={ledger_inv:.6f} "
                    f"slug={self.current_market_slug or ''}"
                )
                self._db_strategy_event(
                    "MARKET_SETTLEMENT_INVENTORY_RECONCILED",
                    {
                        "slug": self.current_market_slug or "",
                        "raw_inventory_delta_shares": raw_inv,
                        "ledger_inventory_shares": ledger_inv,
                        "reason": "settlement_uses_live_inventory_cost_ledger",
                    },
                )
                self.inventory_delta_shares = Decimal(str(ledger_inv))

            spot = 0.0
            if self.latest_external_spot is not None and self.latest_external_spot > 0:
                spot = float(self.latest_external_spot)
            elif self.last_external_spot is not None and self.last_external_spot > 0:
                spot = float(self.last_external_spot)
            elif self._binance_ws_price is not None and self._binance_ws_price > 0:
                ws_age = time.time() - float(self._binance_ws_price_ts or 0.0)
                if ws_age <= 60.0:
                    spot = float(self._binance_ws_price)
            if spot <= 0 and self.external_spot_history:
                _, hist_px = self.external_spot_history[-1]
                if hist_px > 0:
                    spot = float(hist_px)

            slug = self.current_market_slug or ""
            strike = 0.0
            if slug and slug in self.market_strike_cache_by_slug:
                strike = float(self.market_strike_cache_by_slug[slug])
            if hasattr(self, "_settle_shadow_simulation"):
                self._settle_shadow_simulation(slug=slug, spot=spot, strike=strike)

            if inv < 0.001:
                logger.info("Settlement: no inventory to settle.")
                cycle_fill_realized = float(self.market_cycle_realized_net_usdc)
                self._append_cycle_and_maybe_trigger_regime_guard(
                    cycle_combined_pnl=cycle_fill_realized,
                    slug=self.current_market_slug or "",
                    source="settlement_no_inventory",
                )
                self._db_strategy_event(
                    "MARKET_CYCLE_PNL",
                    {
                        "slug": self.current_market_slug or "",
                        "active_side": self.active_side.value,
                        "cycle_fill_realized_usdc": cycle_fill_realized,
                        "cycle_settlement_pnl_usdc": 0.0,
                        "cycle_combined_pnl_usdc": cycle_fill_realized,
                        "recent_window_size": len(self.recent_market_combined_pnls),
                    },
                )
                self._cycle_total_trades += 1
                if cycle_fill_realized > 0:
                    self._cycle_total_wins += 1
                if self.terminal_dashboard:
                    self.terminal_dashboard.record_cycle(
                        slug=self.current_market_slug or "",
                        pnl_usdc=cycle_fill_realized,
                    )
                self.market_cycle_realized_net_usdc = Decimal("0")
                return

            if spot <= 0 or strike <= 0:
                logger.warning(
                    f"Settlement: cannot determine outcome. spot={spot} strike={strike} "
                    f"inv={inv} slug={slug}"
                )
                return

            if spot < 1000 and strike > 1000:
                logger.warning(
                    f"Settlement: invalid spot/strike scale mismatch. "
                    f"spot={spot:.6f} strike={strike:.2f} inv={inv} slug={slug}. "
                    "Skipping settlement PnL to avoid false outcome."
                )
                self._db_strategy_event("MARKET_SETTLEMENT_INVALID_DATA", {
                    "slug": slug,
                    "spot": spot,
                    "strike": strike,
                    "inventory_shares": inv,
                    "reason": "spot_strike_scale_mismatch",
                })
                return

            settlement = compute_settlement_summary(
                spot=spot,
                strike=strike,
                inventory_shares=inv,
                live_inventory_cost=self.live_inventory_cost,
                market_cycle_realized_net_usdc=self.market_cycle_realized_net_usdc,
                active_side=self.active_side.value,
                inventory_side=inventory_side,
            )

            logger.info(
                f"SETTLEMENT: slug={slug} spot={spot:.2f} strike={strike:.2f} "
                f"outcome={settlement.outcome} active_side={settlement.active_side} "
                f"inventory_side={inventory_side or settlement.active_side} "
                f"inv={inv:.4f} redeem=${settlement.redeem_value:.4f} "
                f"cost=${settlement.inventory_cost:.4f} pnl={settlement.settlement_pnl:+.4f}"
            )

            self._db_strategy_event("MARKET_SETTLEMENT", {
                "slug": slug,
                "spot": spot,
                "strike": strike,
                "outcome": settlement.outcome,
                "active_side": settlement.active_side,
                "inventory_side": inventory_side or settlement.active_side,
                "inventory_shares": inv,
                "redeem_per_share": settlement.redeem_per_share,
                "redeem_value_usdc": settlement.redeem_value,
                "inventory_cost_usdc": settlement.inventory_cost,
                "settlement_pnl_usdc": settlement.settlement_pnl,
            })
            self._append_cycle_and_maybe_trigger_regime_guard(
                cycle_combined_pnl=settlement.cycle_combined_pnl,
                slug=slug,
                source="settlement",
            )
            self._db_strategy_event(
                "MARKET_CYCLE_PNL",
                {
                    "slug": slug,
                    "active_side": settlement.active_side,
                    "cycle_fill_realized_usdc": settlement.cycle_fill_realized,
                    "cycle_settlement_pnl_usdc": settlement.settlement_pnl,
                    "cycle_combined_pnl_usdc": settlement.cycle_combined_pnl,
                    "recent_window_size": len(self.recent_market_combined_pnls),
                },
            )
            self._cycle_total_trades += 1
            if settlement.cycle_combined_pnl > 0:
                self._cycle_total_wins += 1
            if self.terminal_dashboard:
                self.terminal_dashboard.record_cycle(
                    slug=slug,
                    pnl_usdc=settlement.cycle_combined_pnl,
                )
            self.market_cycle_realized_net_usdc = Decimal("0")
            self._update_terminal_dashboard_snapshot()
        except Exception as e:
            logger.warning(f"Settlement recording failed: {e}")

    def _search_next_market(self) -> bool:
        try:
            btc_slugs = self._resolve_btc_15m_market_slugs()
            if not btc_slugs:
                logger.debug("Next market search: no slugs found")
                self.next_market_slug = None
                self.next_market_start_ts = None
                return False

            now_ts = time.time()
            best_slug, best_start_ts = select_next_market_window(
                btc_slugs=btc_slugs,
                now_ts=now_ts,
            )

            if best_slug:
                self.next_market_slug = best_slug
                self.next_market_start_ts = best_start_ts
                time_until = best_start_ts - now_ts if best_start_ts else 0
                logger.info(
                    f"Next market found: {best_slug} "
                    f"(starts in {time_until / 60:.1f}m)"
                )

                previous_slug = self.current_market_slug
                if self._find_btc_instrument():
                    if self.current_market_slug != previous_slug:
                        logger.info(
                            f"Switched to new market: {previous_slug} → {self.current_market_slug}"
                        )
                        if self.auto_redeem_enabled and self.auto_redeem_on_rollover and previous_slug:
                            self._schedule_auto_redeem(
                                reason=f"lifecycle_rollover:{previous_slug}->{self.current_market_slug}"
                            )
                    return True
            else:
                self.next_market_slug = None
                self.next_market_start_ts = None
                logger.debug("Next market search: no future markets found")
        except Exception as e:
            logger.warning(f"Next market search failed: {e}")
        return False

    def _start_market_lifecycle_timer(self) -> None:
        while not self._lifecycle_stop_event.is_set():
            if self._stopping:
                return

            now_ts = time.time()
            phase = self._update_market_phase()
            end_ts = getattr(self, "current_market_end_timestamp", None)
            action = determine_lifecycle_timer_action(
                phase_value=phase.value,
                now_ts=now_ts,
                end_ts=end_ts,
                min_minutes_to_close=float(self.maker_min_minutes_to_close),
                settling_grace_sec=float(self.market_settling_grace_sec),
                market_settling_since_ts=float(self._market_settling_since_ts),
            )

            if action.should_reload_instrument:
                self._lifecycle_stop_event.wait(action.wait_sec or 0.0)
                try:
                    if not self._find_btc_instrument():
                        logger.warning("Lifecycle timer: no BTC instrument found")
                except Exception as e:
                    logger.error(f"Lifecycle timer reload failed: {e}")
                continue

            if action.wait_sec is not None and not action.should_search_next:
                self._lifecycle_stop_event.wait(action.wait_sec)
                continue

            if action.should_search_next:
                max_waiting_misses = int(os.getenv("MARKET_WAITING_MAX_MISSES", "3"))

                def _request_rollover() -> None:
                    self._waiting_miss_count = 0
                    self._stopping = True
                    self._rollover_requested_flag = True
                    try:
                        import nautilus_trader  # noqa: F811
                        if hasattr(self, "_trader") and hasattr(self._trader, "node"):
                            self._trader.node.stop()
                        else:
                            raise SystemExit("rollover_needed")
                    except SystemExit:
                        raise
                    except Exception:
                        pass

                self._waiting_miss_count = handle_waiting_phase_search(
                    search_next_market_fn=self._search_next_market,
                    update_market_phase_fn=self._update_market_phase,
                    schedule_auto_redeem_fn=self._schedule_auto_redeem if self.auto_redeem_enabled else None,
                    next_market_slug=self.next_market_slug,
                    market_next_poll_sec=float(self.market_next_poll_sec),
                    waiting_miss_count=getattr(self, "_waiting_miss_count", 0),
                    max_waiting_misses=max_waiting_misses,
                    lifecycle_wait_fn=self._lifecycle_stop_event.wait,
                    logger_info_fn=logger.info,
                    logger_warning_fn=logger.warning,
                    request_rollover_fn=_request_rollover,
                )
                if self._stopping and self._rollover_requested_flag:
                    return
