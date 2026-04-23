from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, Optional

from loguru import logger

from bot.execution_events import (
    reconcile_benign_cancel_reject,
    reconcile_cancel_ack,
    reconcile_rejected_order,
)
from bot.fill_ledger import interpret_fill_liquidity
from bot.enums import ActiveSide
from bot.post_trade import build_fill_order_event_payload


def handle_order_filled(strategy: Any, event: Any) -> None:
    """Handle when a live order is filled."""
    logger.info("=" * 80)
    logger.info("ORDER FILLED!")
    logger.info(f"  Order: {event.client_order_id}")
    logger.info(f"  Fill Price: ${float(event.last_px):.4f}")
    logger.info(f"  Quantity: {float(event.last_qty):.6f}")
    logger.info("=" * 80)

    filled_id = str(event.client_order_id)
    filled_side: Optional[str] = None
    filled_econ = None
    filled_directional_snapshot: Dict[str, Any] = {}
    filled_inst: Any = None
    filled_entry_mode = "value"
    filled_limit_price = Decimal("0")
    maker_matched = False
    pending_fill_qty_dec = Decimal(str(float(getattr(event, "last_qty", 0.0) or 0.0)))
    for order_key, state in list(strategy.active_maker_orders.items()):
        side = str(state.get("side", "") or "")
        order = state.get("order")
        if order and str(order.client_order_id) == filled_id:
            maker_matched = True
            filled_side = side
            filled_econ = state.get("econ")
            snap = state.get("directional_snapshot")
            if isinstance(snap, dict):
                filled_directional_snapshot = snap
                filled_entry_mode = str(snap.get("entry_mode", "value") or "value").lower()
            filled_inst = state.get("instrument_id")
            filled_limit_price = Decimal(str(state.get("price", "0")))
            fill_qty = pending_fill_qty_dec
            if fill_qty <= 0:
                fill_qty = Decimal(str(state.get("quantity", "0")))
            total_qty = Decimal(str(state.get("quantity", "0")))
            accumulated = Decimal(str(state.get("filled_qty", "0"))) + fill_qty
            if accumulated > total_qty and total_qty > 0:
                fill_qty = max(Decimal("0"), total_qty - Decimal(str(state.get("filled_qty", "0"))))
                accumulated = total_qty
            state["filled_qty"] = accumulated
            if total_qty <= 0 or accumulated >= total_qty:
                strategy.active_maker_orders.pop(order_key, None)
            break
    if filled_inst is None:
        filled_inst = getattr(event, "instrument_id", None) or strategy.instrument_id

    fill_price_dec = Decimal(str(float(getattr(event, "last_px", 0.0) or 0.0)))
    fill_qty_dec = pending_fill_qty_dec
    raw_commission_dec = Decimal(str(float(getattr(event, "commission", 0.0) or 0.0)))
    taker_exit_reason = strategy.taker_exit_reason_by_client_order_id.get(filled_id)
    liquidity_side_raw = getattr(event, "liquidity_side", "")
    side_for_ledger = filled_side or strategy._normalize_side_text(getattr(event, "order_side", ""))
    fill_interpretation = interpret_fill_liquidity(
        liquidity_side=liquidity_side_raw,
        raw_commission_dec=raw_commission_dec,
        maker_matched=maker_matched,
        side_for_ledger=side_for_ledger,
        fill_price_dec=fill_price_dec,
        fill_qty_dec=fill_qty_dec,
        filled_limit_price=filled_limit_price,
        filled_id=filled_id,
    )
    liquidity_class = fill_interpretation.liquidity_class
    is_maker_fill = fill_interpretation.is_maker_fill
    fill_side_norm = side_for_ledger or strategy._normalize_side_text(getattr(event, "order_side", ""))
    effective_fee_usdc_dec = fill_interpretation.effective_fee_usdc_dec
    effective_fee_shares_dec = fill_interpretation.effective_fee_shares_dec
    if fill_interpretation.warning_message:
        logger.warning(fill_interpretation.warning_message)
    inventory_fill_delta_dec = fill_qty_dec
    if side_for_ledger == "buy":
        inventory_fill_delta_dec = max(Decimal("0"), fill_qty_dec - effective_fee_shares_dec)
    if maker_matched and fill_qty_dec > 0:
        if side_for_ledger == "buy":
            strategy.inventory_delta_shares += inventory_fill_delta_dec
        elif side_for_ledger == "sell":
            strategy.inventory_delta_shares -= fill_qty_dec
    if not maker_matched and fill_qty_dec > 0:
        side_norm = strategy._normalize_side_text(getattr(event, "order_side", ""))
        if side_norm == "buy":
            strategy.inventory_delta_shares += inventory_fill_delta_dec
        elif side_norm == "sell":
            strategy.inventory_delta_shares -= fill_qty_dec
    realized_net_usdc = None
    if side_for_ledger:
        realized_net_usdc = strategy._update_live_inventory_cost_from_fill(
            instrument_id=filled_inst,
            side=side_for_ledger,
            fill_price=fill_price_dec,
            fill_qty=fill_qty_dec,
            fee_usdc=effective_fee_usdc_dec,
            fee_shares=effective_fee_shares_dec,
        )
        inst_key = strategy._instrument_key(filled_inst)
        if inst_key and hasattr(strategy, "position_manager") and hasattr(strategy, "live_inventory_cost"):
            post_state = strategy.live_inventory_cost.get(inst_key, {})
            try:
                remaining_qty = Decimal(str(post_state.get("qty", "0")))
            except Exception:
                remaining_qty = Decimal("0")
            strategy.position_manager.on_fill(
                inst_key=inst_key,
                side=side_for_ledger,
                remaining_qty=remaining_qty,
                thesis_side=strategy._side_for_instrument_id(filled_inst).value,
                entry_mode=filled_entry_mode,
                now_ts=time.time(),
            )
    if (
        side_for_ledger == "buy"
        and bool(getattr(strategy, "bi_side_enabled", False))
        and bool(getattr(strategy, "active_side_locked", False))
    ):
        target_inst = strategy._normalize_instrument_id(strategy._instrument_for_side(strategy.active_side))
        actual_inst = strategy._normalize_instrument_id(filled_inst)
        if target_inst is not None and actual_inst is not None and actual_inst != target_inst:
            logger.warning(
                "Offside BUY fill detected after side change: "
                f"filled_inst={actual_inst} active_side={strategy.active_side.value} "
                f"target_inst={target_inst} order={filled_id}"
            )
            strategy._db_strategy_event(
                "OFFSIDE_BUY_FILL_DETECTED",
                {
                    "slug": str(strategy.current_market_slug or ""),
                    "active_side": strategy.active_side.value,
                    "target_instrument_id": str(target_inst),
                    "filled_instrument_id": str(actual_inst),
                    "client_order_id": filled_id,
                    "fill_price": float(fill_price_dec),
                    "fill_qty": float(fill_qty_dec),
                },
            )
            strategy._cancel_maker_order_side(
                side="buy",
                instrument_id=filled_inst,
                reason="offside_buy_fill",
            )
            strategy._force_quote_refresh_once = True
            strategy._force_quote_refresh_reason = "offside_buy_fill"
            strategy.side_decision_due_ts = time.time()
    filled_inst_key = strategy._instrument_key(filled_inst)
    if side_for_ledger == "sell" and filled_inst_key:
        strategy._sell_recovery_required_by_inst.pop(filled_inst_key, None)
        strategy._sell_recovery_reason_by_inst.pop(filled_inst_key, None)
        strategy._sell_recovery_venue_cap_by_inst.pop(filled_inst_key, None)
    if (
        side_for_ledger == "buy"
        and strategy.maker_high_cost_exit_cooldown_enabled
        and strategy.maker_high_cost_exit_cooldown_sec > 0
        and fill_price_dec >= strategy.maker_high_cost_fill_threshold
    ):
        inst_key = strategy._instrument_key(filled_inst)
        if inst_key:
            cooldown_until = time.time() + float(strategy.maker_high_cost_exit_cooldown_sec)
            strategy.high_cost_exit_cooldown_until_by_inst[inst_key] = max(
                float(strategy.high_cost_exit_cooldown_until_by_inst.get(inst_key, 0.0)),
                cooldown_until,
            )
            strategy.high_cost_last_fill_price_by_inst[inst_key] = float(fill_price_dec)
            logger.warning(
                "High-cost BUY fill cooldown armed: "
                f"inst={inst_key} fill={float(fill_price_dec):.4f} "
                f"threshold={float(strategy.maker_high_cost_fill_threshold):.4f} "
                f"cooldown={strategy.maker_high_cost_exit_cooldown_sec}s"
            )
    strategy._clear_pending_taker_exit_for_order(filled_id)
    if taker_exit_reason == "stop_loss" and strategy.stop_loss_reentry_cooldown_sec > 0:
        inst_key = strategy._instrument_key(filled_inst)
        if inst_key:
            pause_until = time.time() + float(strategy.stop_loss_reentry_cooldown_sec)
            strategy.stop_loss_reentry_pause_until_by_inst[inst_key] = max(
                float(strategy.stop_loss_reentry_pause_until_by_inst.get(inst_key, 0.0)),
                pause_until,
            )
            logger.warning(
                "Stop-loss re-entry cooldown armed: "
                f"inst={inst_key} cooldown={strategy.stop_loss_reentry_cooldown_sec}s"
            )
        current_slug = str(strategy.current_market_slug or "")
        if current_slug:
            new_count = int(strategy.market_stop_loss_count_by_slug.get(current_slug, 0)) + 1
            strategy.market_stop_loss_count_by_slug[current_slug] = new_count
            strategy._db_strategy_event(
                "MARKET_STOP_LOSS_COUNT_UPDATED",
                {
                    "slug": current_slug,
                    "count": new_count,
                    "max_per_market": int(strategy.market_stop_loss_max_per_market),
                    "instrument_id": str(filled_inst) if filled_inst else None,
                    "client_order_id": filled_id,
                },
            )
            if (
                strategy.market_stop_loss_max_per_market > 0
                and new_count >= strategy.market_stop_loss_max_per_market
            ):
                strategy._db_strategy_event(
                    "MARKET_STOP_LOSS_LIMIT_REACHED",
                    {
                        "slug": current_slug,
                        "count": new_count,
                        "max_per_market": int(strategy.market_stop_loss_max_per_market),
                        "instrument_id": str(filled_inst) if filled_inst else None,
                        "client_order_id": filled_id,
                    },
                )
                logger.warning(
                    "Market stop-loss limit reached: "
                    f"slug={current_slug} count={new_count}/{strategy.market_stop_loss_max_per_market}"
                )
        penalty_side = strategy._side_for_instrument_id(filled_inst)
        if strategy.current_market_slug and penalty_side != ActiveSide.NONE:
            penalty_until = time.time() + float(strategy.stop_loss_reentry_cooldown_sec)
            penalty_key = f"{strategy.current_market_slug}:{penalty_side.value}"
            strategy.side_stop_loss_penalty_until_by_market_side[penalty_key] = max(
                float(strategy.side_stop_loss_penalty_until_by_market_side.get(penalty_key, 0.0)),
                penalty_until,
            )
            payload = {
                "slug": strategy.current_market_slug,
                "penalized_side": penalty_side.value,
                "penalty_until_ts": penalty_until,
                "penalty_remaining_sec": float(strategy.stop_loss_reentry_cooldown_sec),
                "instrument_id": str(filled_inst) if filled_inst else None,
                "client_order_id": filled_id,
            }
            strategy._db_strategy_event("SIDE_STOP_LOSS_PENALIZED", payload)
            if penalty_side == strategy.active_side:
                strategy.active_side = ActiveSide.NONE
                strategy.active_side_locked = False
                strategy.active_side_locked_since_ts = 0.0
                strategy.side_pending_flip_side = ActiveSide.NONE
                strategy.side_pending_flip_count = 0
                strategy.side_decision_due_ts = time.time()
                strategy.side_decision_reason = f"stop_loss_penalty:{penalty_side.value.lower()}"
                strategy._sync_active_instrument()
                strategy._cancel_maker_order_side(side="buy", instrument_id=filled_inst, reason="stop_loss_penalty")
                logger.warning(
                    "Side penalized after stop-loss: "
                    f"slug={strategy.current_market_slug} side={penalty_side.value} "
                    f"cooldown={strategy.stop_loss_reentry_cooldown_sec}s"
                )
    current_slug = str(strategy.current_market_slug or "")
    strategy._record_market_buy_count_if_needed(
        side_for_ledger=str(side_for_ledger or ""),
        current_slug=current_slug,
        filled_id=filled_id,
        filled_inst=filled_inst,
        liquidity_side_raw=liquidity_side_raw,
    )

    strategy.consecutive_denied_orders = 0
    strategy.last_quote_update_ts = 0.0

    strategy._record_observed_fee_rate_from_fill(
        side_for_ledger=str(side_for_ledger or ""),
        fill_qty_dec=fill_qty_dec,
        fill_price_dec=fill_price_dec,
        effective_fee_usdc_dec=effective_fee_usdc_dec,
        effective_fee_shares_dec=effective_fee_shares_dec,
    )

    strategy.rebate_reporter.record_fill(
        econ=filled_econ,
        fill_qty=float(event.last_qty),
        fill_price=float(event.last_px),
    )
    strategy._db_order_event(
        event_type="ORDER_FILLED",
        client_order_id=str(getattr(event, "client_order_id", "")),
        venue_order_id=str(getattr(event, "venue_order_id", "")) if getattr(event, "venue_order_id", None) else None,
        side=(fill_side_norm.upper() if fill_side_norm else None),
        price=float(getattr(event, "last_px", 0.0)),
        qty=float(getattr(event, "last_qty", 0.0)),
        status="FILLED",
        expected_net_usdc=(
            float(getattr(filled_econ, "expected_net_usdc", 0.0))
            if filled_econ is not None
            else None
        ),
        commission_usdc=float(effective_fee_usdc_dec),
        payload=build_fill_order_event_payload(
            liquidity_side_raw=liquidity_side_raw,
            inventory_delta_shares=strategy.inventory_delta_shares,
            raw_commission_dec=raw_commission_dec,
            effective_fee_usdc_dec=effective_fee_usdc_dec,
            effective_fee_shares_dec=effective_fee_shares_dec,
            filled_econ=filled_econ,
            filled_directional_snapshot=filled_directional_snapshot,
            realized_net_usdc=realized_net_usdc,
        ),
    )
    strategy.rebate_reporter.flush_daily_report()
    if strategy.terminal_dashboard:
        side_norm = side_for_ledger or strategy._normalize_side_text(getattr(event, "order_side", ""))
        strategy.terminal_dashboard.increment_fill(
            is_maker_fill=is_maker_fill,
            side=side_norm,
            qty=float(getattr(event, "last_qty", 0.0) or 0.0),
            price=float(getattr(event, "last_px", 0.0) or 0.0),
            commission_usdc=float(effective_fee_usdc_dec),
            client_order_id=filled_id,
            is_taker_exit=filled_id.startswith("BTC-15M-TAKER-EXIT-"),
        )
    strategy._update_terminal_dashboard_snapshot()

    fill_side_norm = filled_side or strategy._normalize_side_text(getattr(event, "order_side", ""))
    strategy._apply_post_fill_followup(
        fill_side_norm=fill_side_norm,
        realized_net_usdc=realized_net_usdc,
    )

    if (
        not strategy._stopping
        and strategy.maker_mode
        and not strategy.maker_kill_switch
        and strategy.latest_market_bid
        and strategy.latest_market_ask
    ):
        logger.info(f"Replenishing maker quote after fill on side={filled_side or 'unknown'}")
        strategy._start_maker_worker(strategy.latest_market_bid, strategy.latest_market_ask)

    strategy._increment_order_metric("filled")
    strategy._update_inventory_metric()


def handle_order_canceled(strategy: Any, event: Any) -> None:
    """Handle cancel acknowledgements to clear pending-cancel state."""
    canceled_id = str(getattr(event, "client_order_id", "") or "")
    strategy._clear_pending_taker_exit_for_order(canceled_id)
    cancel_result = reconcile_cancel_ack(
        canceled_id=canceled_id,
        event=event,
        active_maker_orders=strategy.active_maker_orders,
        last_cancel_ack_ts_by_client_order_id=strategy._last_cancel_ack_ts_by_client_order_id,
        cancel_ack_dedupe_window_sec=float(strategy._cancel_ack_dedupe_window_sec),
    )
    if cancel_result.should_skip:
        logger.debug(f"Skip duplicate cancel ack log for {canceled_id}")
        return
    strategy._update_terminal_dashboard_snapshot()
    strategy._db_order_event(
        event_type="ORDER_CANCELED",
        client_order_id=canceled_id,
        venue_order_id=str(getattr(event, "venue_order_id", "")) if getattr(event, "venue_order_id", None) else None,
        side=str(getattr(event, "order_side", "")),
        status="CANCELED",
        reason=cancel_result.cancel_reason,
    )


def handle_order_cancel_rejected(strategy: Any, event: Any) -> None:
    """Handle when an order cancellation is rejected by exchange."""
    rejected_id = str(getattr(event, "client_order_id", "") or "")
    strategy._clear_pending_taker_exit_for_order(rejected_id)
    reason = str(getattr(event, "reason", "") or "").lower()

    logger.warning(f"OrderCancelRejected for {rejected_id}: {reason}")

    if "already canceled or matched" in reason or "order can't be found" in reason:
        if reconcile_benign_cancel_reject(
            rejected_id=rejected_id,
            active_maker_orders=strategy.active_maker_orders,
        ):
            logger.info(f"Clearing {rejected_id} from active_maker_orders due to benign CancelReject.")

        strategy._db_order_event(
            event_type="ORDER_CANCEL_REJECTED",
            client_order_id=rejected_id,
            venue_order_id=str(getattr(event, "venue_order_id", "")) if getattr(event, "venue_order_id", None) else None,
            side=str(getattr(event, "order_side", "")),
            status="CANCEL_REJECTED_RECONCILED",
            reason=reason,
        )


def handle_order_rejection_like_event(strategy: Any, event: Any, title: str = "ORDER REJECTED") -> None:
    """Shared handler for denied/rejected order events."""
    logger.error("=" * 80)
    logger.error(f"{title}!")
    logger.error(f"  Order: {event.client_order_id}")
    logger.error(f"  Reason: {event.reason}")
    logger.error("=" * 80)

    denied_id = str(event.client_order_id)
    strategy._clear_pending_taker_exit_for_order(denied_id)
    reject_result = reconcile_rejected_order(
        denied_id=denied_id,
        event=event,
        active_maker_orders=strategy.active_maker_orders,
        normalize_side_text_fn=strategy._normalize_side_text,
        instrument_key_fn=strategy._instrument_key,
    )
    if (
        reject_result.is_taker_exit_reject
        and reject_result.rejected_inst_key
        and strategy.taker_exit_reject_cooldown_sec > 0
    ):
        cooldown_until = time.time() + float(strategy.taker_exit_reject_cooldown_sec)
        prev_until = float(strategy.taker_exit_reject_cooldown_until_by_inst.get(reject_result.rejected_inst_key, 0.0))
        strategy.taker_exit_reject_cooldown_until_by_inst[reject_result.rejected_inst_key] = max(
            prev_until,
            cooldown_until,
        )
        strategy._log_taker_exit_skip_throttled(
            inst_key=reject_result.rejected_inst_key,
            reason_tag="reject_cooldown",
            message=(
                "Taker exit rejection cooldown activated: "
                f"inst={reject_result.rejected_inst_key} cooldown={strategy.taker_exit_reject_cooldown_sec}s"
            ),
            now_ts=time.time(),
        )

    strategy.consecutive_denied_orders += 1
    reason = reject_result.reason
    venue_balance_shares = strategy._extract_venue_balance_shares_from_reject(reason)
    strategy._db_order_event(
        event_type="ORDER_REJECTED" if "REJECTED" in title else "ORDER_DENIED",
        client_order_id=str(getattr(event, "client_order_id", "")),
        venue_order_id=str(getattr(event, "venue_order_id", "")) if getattr(event, "venue_order_id", None) else None,
        side=str(getattr(event, "order_side", "")),
        status="REJECTED",
        reason=reason,
        payload={
            "title": title,
            "consecutive_denied": strategy.consecutive_denied_orders,
            "instrument_id": str(getattr(event, "instrument_id", "") or ""),
            "venue_balance_shares": float(venue_balance_shares) if venue_balance_shares is not None else None,
            "sell_recovery_candidate": bool(reject_result.rejected_side == "sell"),
        },
    )
    if "POST_ONLY_NOT_SUPPORTED" in reason:
        if strategy.maker_use_post_only:
            logger.warning("Exchange rejected post-only orders; disabling post-only and continuing maker mode.")
        strategy.maker_use_post_only = False
    lowered = reason.lower()
    if ("orderbook" in lowered) and ("does not exist" in lowered):
        pause_sec = max(1, strategy.maker_error_pause_sec)
        now_ts = time.time()
        strategy.quote_pause_until_ts = max(strategy.quote_pause_until_ts, now_ts + pause_sec)
        strategy.orderbook_unavailable_until_ts = max(strategy.orderbook_unavailable_until_ts, now_ts + pause_sec)
        inst_id_txt = str(getattr(event, "instrument_id", "") or "")
        strategy.orderbook_unavailable_token = strategy._extract_token_id_from_instrument(inst_id_txt)
        strategy._cancel_active_maker_orders()
        logger.warning(
            f"Orderbook missing rejection detected; pause quoting for {pause_sec}s and reload instrument "
            f"(instrument={inst_id_txt}, token={strategy.orderbook_unavailable_token})."
        )
        strategy.consecutive_denied_orders = max(0, strategy.consecutive_denied_orders - 1)
        strategy._trigger_quote_watchdog_reload("orderbook_not_exist", now_ts)
        strategy.rebate_reporter.record_denied()
        strategy._increment_order_metric("rejected")
        return

    if ("not enough balance" in lowered) or ("allowance" in lowered):
        pause_sec = max(1.0, float(strategy.sell_balance_retry_pause_sec))
        now_ts = time.time()
        if reject_result.rejected_side == "sell":
            inst_key = strategy._instrument_key(reject_result.rejected_inst)
            if inst_key:
                recent_buy_ts = float(getattr(strategy, "recent_buy_fill_ts_by_inst", {}).get(inst_key, 0.0))
                sell_delay_sec = float(getattr(strategy, "sell_delay_after_buy_sec", 0.0) or 0.0)
                if recent_buy_ts > 0 and sell_delay_sec > 0:
                    pause_sec = max(pause_sec, max(0.0, (recent_buy_ts + sell_delay_sec) - now_ts))
                strategy._sell_reject_pause_until_by_inst[inst_key] = max(
                    float(strategy._sell_reject_pause_until_by_inst.get(inst_key, 0.0)),
                    now_ts + pause_sec,
                )
                strategy._sell_recovery_required_by_inst[inst_key] = now_ts
                strategy._sell_recovery_reason_by_inst[inst_key] = reason
                if venue_balance_shares is not None and venue_balance_shares > 0:
                    venue_cap = max(
                        Decimal("0"),
                        venue_balance_shares - strategy.sell_recovery_qty_buffer_shares,
                    )
                    strategy._sell_recovery_venue_cap_by_inst[inst_key] = venue_cap
            strategy._cancel_maker_order_side("sell", reason="sell_balance_reject", instrument_id=reject_result.rejected_inst)
            token_id = (
                strategy._extract_token_id_from_instrument(str(reject_result.rejected_inst))
                if reject_result.rejected_inst is not None
                else None
            )
            strategy._get_conditional_balance_for_token(token_id=token_id, force_refresh=True)
            strategy._force_quote_refresh_once = True
            strategy._force_quote_refresh_reason = "sell_recovery_balance_reject"
            venue_balance_txt = (
                f"{float(venue_balance_shares):.6f}"
                if venue_balance_shares is not None
                else "unknown"
            )
            logger.warning(
                "SELL balance/allowance rejection detected; "
                f"treat as venue balance lag and retry after {pause_sec:.1f}s "
                f"(instrument={inst_key or '-'}, venue_balance={venue_balance_txt}). "
                "BUY side remains active."
            )
            strategy.consecutive_denied_orders = max(0, strategy.consecutive_denied_orders - 1)
            strategy.rebate_reporter.record_denied()
            strategy._increment_order_metric("rejected")
            return
        pause_sec = max(1, strategy.maker_error_pause_sec)
        strategy.quote_pause_until_ts = max(strategy.quote_pause_until_ts, now_ts + pause_sec)
        strategy._cancel_active_maker_orders()
        logger.warning(
            f"Balance/allowance rejection detected; pause quoting for {pause_sec}s. "
            "Check wallet balance and token allowance."
        )
    strategy.rebate_reporter.record_denied()
    if strategy.consecutive_denied_orders >= strategy.maker_max_consecutive_denied:
        strategy._activate_maker_kill_switch(
            f"Consecutive denied orders reached {strategy.consecutive_denied_orders}"
        )

    strategy._increment_order_metric("rejected")
