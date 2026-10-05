from __future__ import annotations

import time
import math
from decimal import Decimal
from typing import Any, Dict, Protocol

from loguru import logger

from bot.enums import ActiveSide, MarketPhase
from bot.lifecycle import determine_lifecycle_timer_action, select_next_market_window
from bot.ops import handle_waiting_phase_search
from bot.post_trade import compute_settlement_summary


def _canonical_twap_shadow_label(
    *, twap_price: Any, source_ts: Any, window_sec: Any, strike: Any,
    settlement_ts: float, freshness_sec: float = 10.0,
) -> dict[str, Any]:
    """Describe whether the latest direct official-TWAP tick is a valid label.

    This is research provenance only; it does not determine live settlement PnL.
    """
    try:
        price = float(twap_price)
        observed = float(source_ts)
        window = int(window_sec)
        strike_value = float(strike)
        settled = float(settlement_ts)
        age = settled - observed
        values_valid = all(map(math.isfinite, (price, observed, strike_value, settled, age)))
    except (TypeError, ValueError, OverflowError):
        price = observed = strike_value = 0.0
        window, age, values_valid = 0, None, False
    source = f"polymarket_chainlink_twap_{window}s_ws" if values_valid and price > 0 and window > 0 else "unavailable"
    canonical = bool(
        values_valid and price > 0 and strike_value > 0 and observed > 0
        and window == 60 and age is not None and 0.0 <= age <= float(freshness_sec)
    )
    return {
        "source": source,
        "age_sec": age,
        "canonical": canonical,
        "side": ("UP" if price >= strike_value else "DOWN") if canonical else None,
    }


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

            # Shadow labels must be based on the direct Chainlink-TWAP tick
            # clock/value, not the general-purpose spot cache (which may stop
            # refreshing when no pricing cycle runs near market close).
            shadow_settlement_ts = time.time()
            shadow_label = _canonical_twap_shadow_label(
                twap_price=getattr(self, "_polymarket_chainlink_twap_price", None),
                source_ts=getattr(self, "_polymarket_chainlink_twap_observation_ts", None),
                window_sec=getattr(self, "_polymarket_chainlink_twap_window_sec", None),
                strike=strike,
                settlement_ts=shadow_settlement_ts,
                freshness_sec=float(getattr(self, "_RAW_SPOT_FRESHNESS_SEC", 10.0)),
            )
            shadow_settlement_side = shadow_label["side"] or ("UP" if spot >= strike else "DOWN")

            stop_shadow = getattr(self, "stop_forensics_shadow", None)
            if stop_shadow is not None:
                try:
                    stop_shadow.record_settlement(
                        slug=slug,
                        settlement_side=shadow_settlement_side,
                        settlement_ts=shadow_settlement_ts,
                        settlement_reference_source=shadow_label["source"],
                        settlement_reference_is_canonical=shadow_label["canonical"],
                        settlement_reference_age_sec=shadow_label["age_sec"],
                    )
                except Exception as shadow_error:
                    logger.warning(
                        "Stop continuation shadow settlement capture failed: "
                        f"{type(shadow_error).__name__}: {shadow_error}"
                    )

            trend_shadow = getattr(self, "trend_entry_shadow", None)
            if trend_shadow is not None:
                try:
                    trend_shadow.on_settlement(
                        slug=slug,
                        outcome="UP" if spot >= strike else "DOWN",
                        settlement_ts=time.time(),
                    )
                except Exception as shadow_error:
                    logger.warning(
                        "Trend-entry shadow settlement capture failed: "
                        f"{type(shadow_error).__name__}: {shadow_error}"
                    )
            forward_shadow = getattr(self, "forward_shadow_experiment", None)
            if forward_shadow is not None:
                try:
                    forward_shadow.on_settlement(
                        slug=slug,
                        outcome=shadow_label["side"] or "UNKNOWN",
                        settlement_ts=shadow_settlement_ts,
                        settlement_source=shadow_label["source"],
                    )
                except Exception as shadow_error:
                    logger.warning(
                        "Forward-shadow settlement capture failed: "
                        f"{type(shadow_error).__name__}: {shadow_error}"
                    )
            twap_shadow = getattr(self, "twap_forward_shadow", None)
            if twap_shadow is not None:
                try:
                    twap_shadow.finalize_market(
                        slug,
                        settlement_side=shadow_settlement_side,
                        settlement_ts=shadow_settlement_ts,
                        settlement_reference_source=shadow_label["source"],
                        settlement_reference_is_canonical=shadow_label["canonical"],
                        settlement_reference_age_sec=shadow_label["age_sec"],
                    )
                except Exception as shadow_error:
                    logger.warning(
                        "TWAP forward-shadow settlement capture failed: "
                        f"{type(shadow_error).__name__}: {shadow_error}"
                    )

            if inv < 0.001:
                logger.info("Settlement: no inventory to settle.")
                # Persist the market label even with no trade. This is required
                # for deterministic replay against every observed market.
                self._db_strategy_event(
                    "MARKET_SETTLEMENT",
                    {
                        "slug": slug,
                        "spot": spot,
                        "strike": strike,
                        "outcome": "UP" if spot >= strike else "DOWN",
                        "outcome_only": True,
                        "reference_source": str(getattr(self, "latest_external_spot_source", "") or ""),
                        "active_side": self.active_side.value,
                        "inventory_side": None,
                        "inventory_shares": 0.0,
                        "redeem_per_share": 0.0,
                        "redeem_value_usdc": 0.0,
                        "inventory_cost_usdc": 0.0,
                        "settlement_pnl_usdc": 0.0,
                    },
                )
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
                # `cycle_fill_realized` has already been applied on each SELL
                # fill.  The no-inventory branch therefore has no additional
                # finalized settlement PnL to add to the session guard.
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
            record_session_pnl = getattr(self, "_record_session_realized_pnl", None)
            if callable(record_session_pnl) and Decimal(str(settlement.settlement_pnl)) != 0:
                # Only the residual position's final payout belongs here;
                # prior SELL fills were accounted for at fill time.
                record_session_pnl(Decimal(str(settlement.settlement_pnl)), source="settlement")
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
                max_waiting_misses = 3

                def _request_rollover() -> None:
                    self._collection_stop_context = {
                        "stop_request_source": "strategy_requested",
                        "lifecycle_reason": "stale_instrument_lifecycle",
                        "waiting_miss_count": self._waiting_miss_count,
                        "next_market_slug": self.next_market_slug,
                    }
                    self._waiting_miss_count = 0
                    self._stopping = True
                    self._rollover_requested_flag = True
                    try:
                        stop_node = getattr(self, "_request_node_stop_callback", None)
                        if not callable(stop_node):
                            raise RuntimeError("launcher node-stop callback is not configured")
                        stop_node()
                    except Exception as exc:
                        self._stopping = False
                        self._rollover_requested_flag = False
                        logger.error(f"Lifecycle node rollover stop failed: {exc}")

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
