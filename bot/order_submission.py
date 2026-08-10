from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, Optional

from loguru import logger
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.objects import Price, Quantity

from bot.quote_service import (
    apply_sellable_inventory_guard,
    build_active_maker_order_state,
    build_limit_order,
    retreat_crossing_buy_quote,
    violates_final_crossing_guard,
)


def submit_maker_quote(
    strategy: Any,
    *,
    instrument_id: Any,
    side: str,
    limit_price: Decimal,
    econ: Any,
    dynamic_fee_rate: Optional[Decimal] = None,
    directional_snapshot: Optional[Dict[str, Any]] = None,
    target_version: Optional[int] = None,
    loss_sell_reason: str = "",
    target_qty_override: Optional[Decimal] = None,
    fair_edge_bucket_shadow: Optional[str] = None,
) -> None:
    instrument_id = strategy._normalize_instrument_id(instrument_id)
    instrument = strategy.cache.instrument(instrument_id) if instrument_id else None
    limit_price = strategy._align_price_to_tick(limit_price, side, instrument)
    expected_net_usdc = Decimal(str(getattr(econ, "expected_net_usdc", "0")))
    tail_protect_tp = bool((directional_snapshot or {}).get("tail_protect_tp", False))
    if expected_net_usdc <= Decimal("0") and not tail_protect_tp:
        strategy._db_order_event(
            event_type="ORDER_SKIP_EXPECTED_NET",
            side=side.upper(),
            price=float(limit_price),
            status="SKIPPED",
            reason="expected_net_non_positive",
            expected_net_usdc=float(expected_net_usdc),
        )
        return

    quote_now = strategy._get_quote_for_instrument(instrument_id)
    if quote_now is not None and side == "buy":
        if strategy._should_skip_buy_submit_for_quote_drift(
            instrument_id=instrument_id,
            quote_now=quote_now,
            directional_snapshot=directional_snapshot,
            instrument=instrument,
        ):
            return
        limit_price = retreat_crossing_buy_quote(
            limit_price=limit_price,
            instrument=instrument,
            quote_now=quote_now,
            align_price_fn=strategy._align_price_to_tick,
            logger_warning_fn=logger.warning,
            logger_info_fn=logger.info,
        )
        if limit_price is None:
            return

    precision = int(getattr(instrument, "size_precision", 6)) if instrument is not None else 6
    if target_qty_override is not None:
        qty_dec = Decimal(str(target_qty_override))
    else:
        qty_dec = strategy._compute_maker_order_qty(limit_price, precision)
    if side == "buy":
        entry_mode = str((directional_snapshot or {}).get("entry_mode", "value") or "value").lower()
        size_multiplier = Decimal(
            str(
                (directional_snapshot or {}).get(
                    "size_multiplier",
                    (
                        strategy.continuation_entry_size_multiplier
                        if entry_mode == "continuation"
                        else (
                            getattr(strategy, "trend_buy_size_multiplier", Decimal("1"))
                            if entry_mode == "trend"
                            else Decimal("1")
                        )
                    ),
                )
            )
        )
        adjusted_qty = qty_dec * size_multiplier
        exchange_min_qty = max(
            strategy.maker_exchange_min_shares,
            Decimal(str(10 ** (-precision))),
        )
        # A risk size-down that falls below the venue minimum must be skipped,
        # not rounded back up. Rounding turned a configured 50% high-price
        # reduction into nearly full exposure.
        if size_multiplier < Decimal("1") and adjusted_qty + Decimal("0.000001") < exchange_min_qty:
            strategy._db_order_event(
                event_type="ORDER_SKIP_SIZE_BELOW_EXCHANGE_MIN",
                side="BUY",
                price=float(limit_price),
                qty=float(adjusted_qty),
                status="SKIPPED",
                reason="risk_adjusted_size_below_exchange_min",
                payload={
                    "unadjusted_qty": float(qty_dec),
                    "adjusted_qty": float(adjusted_qty),
                    "size_multiplier": float(size_multiplier),
                    "exchange_min_qty": float(exchange_min_qty),
                },
            )
            logger.info(
                "Skip maker BUY quote: risk-adjusted quantity below exchange minimum "
                f"({float(adjusted_qty):.4f} < {float(exchange_min_qty):.4f})"
            )
            return
        if target_qty_override is not None and adjusted_qty + Decimal("0.000001") < exchange_min_qty:
            strategy._db_order_event(
                event_type="ORDER_SKIP_SIZE_BELOW_EXCHANGE_MIN",
                side="BUY",
                price=float(limit_price),
                qty=float(adjusted_qty),
                status="SKIPPED",
                reason="target_quantity_below_exchange_min",
                payload={"exchange_min_qty": float(exchange_min_qty)},
            )
            return
        min_buy_qty = exchange_min_qty if size_multiplier < Decimal("1") else max(strategy.maker_min_shares, exchange_min_qty)
        qty_dec = max(adjusted_qty, min_buy_qty)
    if qty_dec <= 0:
        return
    inst_key = strategy._instrument_key(instrument_id)
    if side == "buy":
        reentry_pause_until = float(strategy.stop_loss_reentry_pause_until_by_inst.get(inst_key, 0.0))
        if time.time() < reentry_pause_until:
            cooldown_left = reentry_pause_until - time.time()
            logger.info(
                "Skip maker BUY quote: stop-loss re-entry cooldown active "
                f"(inst={inst_key}, cooldown_left={cooldown_left:.1f}s)"
            )
            strategy._db_order_event(
                event_type="ORDER_SKIP_REENTRY_COOLDOWN",
                side=side.upper(),
                price=float(limit_price),
                qty=float(qty_dec),
                status="SKIPPED",
                reason="stop_loss_reentry_cooldown",
                payload={
                    "instrument_id": str(instrument_id),
                    "cooldown_left_sec": cooldown_left,
                },
            )
            return

    if side == "sell" and not strategy._is_dry_run_mode():
        recent_buy_ts = float(getattr(strategy, "recent_buy_fill_ts_by_inst", {}).get(inst_key, 0.0))
        sell_delay_sec = float(getattr(strategy, "sell_delay_after_buy_sec", 0.0) or 0.0)
        if recent_buy_ts > 0 and sell_delay_sec > 0:
            now_ts = time.time()
            pause_left = (recent_buy_ts + sell_delay_sec) - now_ts
            if pause_left > 0:
                logger.info(
                    "Skip maker SELL quote: waiting for post-BUY venue balance sync "
                    f"(inst={inst_key}, pause_left={pause_left:.1f}s)"
                )
                strategy._db_order_event(
                    event_type="ORDER_SKIP_SELL_DELAY_AFTER_BUY",
                    side=side.upper(),
                    price=float(limit_price),
                    qty=float(qty_dec),
                    status="SKIPPED",
                    reason="sell_delay_after_buy",
                    payload={
                        "instrument_id": str(instrument_id),
                        "recent_buy_ts": recent_buy_ts,
                        "sell_delay_after_buy_sec": sell_delay_sec,
                        "pause_left_sec": pause_left,
                    },
                )
                return
        sellable_qty = strategy._get_effective_sellable_qty(instrument_id=instrument_id)
        confirmed_qty = strategy._get_confirmed_inventory_qty_for_instrument(instrument_id=instrument_id)
        venue_cap = strategy._sell_recovery_venue_cap_by_inst.get(inst_key, None) if inst_key else None
        if venue_cap is not None and venue_cap > 0:
            sellable_qty = min(sellable_qty, venue_cap)
        adjusted_qty, sellable_guard_reason = apply_sellable_inventory_guard(
            qty_dec=qty_dec,
            precision=precision,
            sellable_qty=sellable_qty,
            maker_exchange_min_shares=strategy.maker_exchange_min_shares,
        )
        if adjusted_qty is None:
            now_ts = time.time()
            last_skip = float(strategy._last_sellable_skip_log_ts_by_inst.get(inst_key, 0.0))
            if now_ts - last_skip >= float(strategy.no_quote_diag_interval_sec):
                strategy._last_sellable_skip_log_ts_by_inst[inst_key] = now_ts
                if sellable_guard_reason == "no_sellable_inventory":
                    logger.info(
                        "NO_QUOTE diagnostic: "
                        f"inst={inst_key} side=sell reason=no_sellable_inventory "
                        f"sellable={float(sellable_qty):.6f} confirmed={float(confirmed_qty):.6f} "
                        f"inventory={float(strategy.inventory_delta_shares):.6f}"
                    )
                else:
                    logger.info(
                        "NO_QUOTE diagnostic: "
                        f"inst={inst_key} side=sell reason=sellable_below_min_after_reduce "
                        f"qty={float(qty_dec):.6f} min={float(strategy.maker_exchange_min_shares):.6f}"
                    )
            return
        if adjusted_qty + Decimal("0.000001") < qty_dec:
            old_qty = qty_dec
            qty_dec = adjusted_qty
            logger.info(
                "Maker SELL qty reduced to sellable amount: "
                f"{float(old_qty):.6f} -> {float(qty_dec):.6f} "
                "(on-chain tokens after fees)"
            )
        else:
            qty_dec = adjusted_qty

    projected_inventory = strategy._project_inventory_after_fill(side, qty_dec, instrument_id=instrument_id)
    if side == "buy" and projected_inventory > strategy.maker_max_inventory_shares:
        logger.warning(
            "Skip maker quote: projected inventory would exceed max "
            f"(side={side}, qty={float(qty_dec):.6f}, projected={float(projected_inventory):.6f}, "
            f"max={float(strategy.maker_max_inventory_shares):.6f})"
        )
        strategy._db_order_event(
            event_type="ORDER_SKIP_INVENTORY_CAP",
            side=side.upper(),
            price=float(limit_price),
            qty=float(qty_dec),
            reason="projected_inventory_exceeds_max",
            payload={
                "current_inventory": float(strategy.inventory_delta_shares),
                "projected_inventory": float(projected_inventory),
                "max_inventory": float(strategy.maker_max_inventory_shares),
            },
        )
        return
    if side == "sell" and projected_inventory < Decimal("0"):
        confirmed_qty = strategy._get_confirmed_inventory_qty_for_instrument(instrument_id=instrument_id)
        logger.info(
            "Skip maker quote: projected inventory would go negative "
            f"(side={side}, qty={float(qty_dec):.6f}, projected={float(projected_inventory):.6f}, "
            f"confirmed={float(confirmed_qty):.6f}, global={float(strategy.inventory_delta_shares):.6f})"
        )
        strategy._db_order_event(
            event_type="ORDER_SKIP_SELLABLE_PROJECTED",
            side=side.upper(),
            price=float(limit_price),
            qty=float(qty_dec),
            reason="projected_inventory_below_zero",
            payload={
                "current_inventory": float(confirmed_qty),
                "global_inventory": float(strategy.inventory_delta_shares),
                "projected_inventory": float(projected_inventory),
            },
        )
        return

    if fair_edge_bucket_shadow:
        if side == "buy" and hasattr(strategy, "_record_fair_edge_bucket_shadow_entry"):
            strategy._record_fair_edge_bucket_shadow_entry(
                instrument_id=instrument_id,
                limit_price=limit_price,
                qty=qty_dec,
                econ=econ,
                directional_snapshot=directional_snapshot,
                bucket=str(fair_edge_bucket_shadow),
            )
        return

    quote = strategy._get_quote_for_instrument(instrument_id)
    if violates_final_crossing_guard(
        side=side,
        limit_price=limit_price,
        quote=quote,
        maker_use_post_only=strategy.maker_use_post_only,
        maker_post_only_strict=getattr(strategy, "maker_post_only_strict", False),
        logger_warning_fn=logger.warning,
    ):
        return

    token_qty = float(qty_dec)
    order_key = strategy._order_key_for(side, instrument_id)
    created_ts = time.time()
    if strategy._is_dry_run_mode():
        if side != "buy" or not hasattr(strategy, "_record_shadow_simulated_entry"):
            strategy._db_order_event(
                event_type="ORDER_DRY_RUN_SKIP",
                side=side.upper(),
                price=float(limit_price),
                qty=token_qty,
                status="SKIPPED",
                reason="dry_run_sell_execution_not_modeled",
                expected_net_usdc=float(econ.expected_net_usdc),
                payload={"instrument_id": str(instrument_id)},
            )
            return
        accepted = strategy._record_shadow_simulated_entry(
            instrument_id=instrument_id,
            limit_price=limit_price,
            qty=qty_dec,
            econ=econ,
            directional_snapshot=directional_snapshot,
            target_version=int(target_version or 0),
            order_key=order_key,
        )
        if not accepted:
            return
        simulation = strategy._load_shadow_simulation_for_slug(
            str(getattr(strategy, "current_market_slug", "") or "")
        )
        simulation_id = str((simulation or {}).get("simulation_id") or "")
        state = build_active_maker_order_state(
            order=None,
            econ=econ,
            directional_snapshot=directional_snapshot,
            limit_price=limit_price,
            side=side,
            instrument_id=instrument_id,
            token_id=strategy._extract_token_id_from_instrument(str(instrument_id)),
            token_qty=token_qty,
            created_ts=created_ts,
            target_version=int(target_version or 0),
            loss_sell_reason=loss_sell_reason,
        )
        state.update(
            {
                "dry_run_simulated": True,
                "dry_run_simulation_id": simulation_id,
                "dry_run_client_order_id": f"DRY-MAKER-{side.upper()}-{int(created_ts * 1000)}",
            }
        )
        strategy.active_maker_orders[order_key] = state
        strategy._db_order_event(
            event_type="ORDER_DRY_RUN_SUBMITTED",
            client_order_id=state["dry_run_client_order_id"],
            side=side.upper(),
            price=float(limit_price),
            qty=token_qty,
            status="OPEN",
            reason="dry_run_live_lifecycle_submit",
            expected_net_usdc=float(econ.expected_net_usdc),
            payload={
                "instrument_id": str(instrument_id),
                "order_key": order_key,
                "target_version": int(target_version or 0),
                "simulation_id": simulation_id,
            },
        )
        return
    if not instrument_id or not instrument:
        return

    qty = Quantity(token_qty, precision=precision)
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    price_precision = int(getattr(instrument, "price_precision", 3))
    price = Price.from_str(f"{float(limit_price):.{price_precision}f}")
    order_id = ClientOrderId(f"BTC-15M-MAKER-{side.upper()}-{int(time.time() * 1000)}")

    order_kwargs = dict(
        instrument_id=instrument_id,
        order_side=order_side,
        quantity=qty,
        price=price,
        client_order_id=order_id,
        quote_quantity=False,
        time_in_force=TimeInForce.GTC,
    )
    order, strategy.maker_use_post_only = build_limit_order(
        order_factory=strategy.order_factory,
        order_kwargs=order_kwargs,
        maker_use_post_only=strategy.maker_use_post_only,
        maker_post_only_strict=strategy.maker_post_only_strict,
        logger_error_fn=logger.error,
        logger_warning_fn=logger.warning,
    )
    if order is None:
        return

    strategy.submit_order(order)
    strategy.consecutive_denied_orders = 0
    strategy.active_maker_orders[order_key] = build_active_maker_order_state(
        order=order,
        econ=econ,
        directional_snapshot=directional_snapshot,
        limit_price=limit_price,
        side=side,
        instrument_id=instrument_id,
        token_id=strategy._extract_token_id_from_instrument(str(instrument_id)),
        token_qty=token_qty,
        created_ts=created_ts,
        target_version=int(target_version or 0),
        loss_sell_reason=loss_sell_reason,
    )
    if side == "buy" and inst_key:
        recent_buy_submits = getattr(strategy, "recent_buy_submit_by_inst", None)
        if not isinstance(recent_buy_submits, dict):
            recent_buy_submits = {}
            setattr(strategy, "recent_buy_submit_by_inst", recent_buy_submits)
        recent_buy_submits[inst_key] = {
            "price": Decimal(str(limit_price)),
            "quantity": Decimal(str(token_qty)),
            "created_ts": time.time(),
            "client_order_id": str(order.client_order_id),
        }
    if getattr(strategy, "terminal_dashboard", None):
        token_side = getattr(strategy._side_for_instrument_id(instrument_id), "value", "NONE")
        strategy.terminal_dashboard.record_order_submitted(
            side=side,
            token_side=token_side,
            qty=float(token_qty),
            price=float(limit_price),
            client_order_id=str(order.client_order_id),
            is_taker=False,
        )
    if side == "sell":
        inst_key = strategy._instrument_key(instrument_id)
        if inst_key:
            strategy._sell_recovery_required_by_inst.pop(inst_key, None)
            strategy._sell_recovery_reason_by_inst.pop(inst_key, None)
    strategy._db_order_event(
        event_type="ORDER_SUBMIT",
        client_order_id=str(order.client_order_id),
        side=side.upper(),
        price=float(limit_price),
        qty=float(token_qty),
        status="SUBMITTED",
        expected_net_usdc=float(econ.expected_net_usdc),
        payload={
            "maker": True,
            "submitted_instrument_id": str(instrument_id),
            "rebate_estimate_usdc": float(econ.expected_rebate_usdc),
            "spread_capture_estimate_usdc": float(econ.expected_spread_capture_usdc),
            "directional_edge_ps": (
                float(directional_snapshot.get("directional_edge_ps"))
                if directional_snapshot and directional_snapshot.get("directional_edge_ps") is not None
                else None
            ),
            "directional_edge_usdc": (
                float(directional_snapshot.get("directional_edge_usdc"))
                if directional_snapshot and directional_snapshot.get("directional_edge_usdc") is not None
                else None
            ),
            "p_fair": (
                float(directional_snapshot.get("p_fair"))
                if directional_snapshot and directional_snapshot.get("p_fair") is not None
                else None
            ),
            "fee_ps": (
                float(directional_snapshot.get("fee_ps"))
                if directional_snapshot and directional_snapshot.get("fee_ps") is not None
                else None
            ),
            "other_cost_ps": (
                float(directional_snapshot.get("other_cost_ps"))
                if directional_snapshot and directional_snapshot.get("other_cost_ps") is not None
                else None
            ),
            "exec_penalty_usdc": (
                float(directional_snapshot.get("exec_penalty_usdc"))
                if directional_snapshot and directional_snapshot.get("exec_penalty_usdc") is not None
                else None
            ),
            "robust_net_usdc": (
                float(directional_snapshot.get("robust_net_usdc"))
                if directional_snapshot and directional_snapshot.get("robust_net_usdc") is not None
                else None
            ),
            "tail_protect_tp": (
                bool(directional_snapshot.get("tail_protect_tp", False))
                if directional_snapshot
                else False
            ),
            "tail_protect_tp_price": (
                float(directional_snapshot.get("tail_protect_tp_price"))
                if directional_snapshot and directional_snapshot.get("tail_protect_tp_price") is not None
                else None
            ),
            "target_qty_override": (
                float(directional_snapshot.get("target_qty_override"))
                if directional_snapshot and directional_snapshot.get("target_qty_override") is not None
                else None
            ),
            "entry_mode": (
                str(directional_snapshot.get("entry_mode", "value"))
                if directional_snapshot
                else "value"
            ),
            "size_multiplier": (
                float(directional_snapshot.get("size_multiplier"))
                if directional_snapshot and directional_snapshot.get("size_multiplier") is not None
                else 1.0
            ),
            "entry_quality": (
                directional_snapshot.get("entry_quality")
                if directional_snapshot
                else None
            ),
            "external_entry_confirmation": (
                directional_snapshot.get("external_entry_confirmation")
                if directional_snapshot
                else None
            ),
            "external_entry_confirmation_size_adjustment": (
                directional_snapshot.get("external_entry_confirmation_size_adjustment")
                if directional_snapshot
                else None
            ),
            "entry_quality_quote_price_cap": (
                float(directional_snapshot.get("entry_quality_quote_price_cap"))
                if directional_snapshot and directional_snapshot.get("entry_quality_quote_price_cap") is not None
                else None
            ),
            "sell_recovery_required": (
                bool(strategy._sell_recovery_required_by_inst.get(strategy._instrument_key(instrument_id), 0.0))
                if side == "sell"
                else False
            ),
            "loss_sell_reason": loss_sell_reason if side == "sell" and loss_sell_reason else None,
        },
    )
    strategy.rebate_reporter.record_quote(
        fee_equivalent=float(econ.fee_equivalent_usdc),
        rebate=float(econ.expected_rebate_usdc),
        spread_capture=float(econ.expected_spread_capture_usdc),
        expected_net=float(econ.expected_net_usdc),
    )
    logger.info(
        f"MAKER QUOTE {side.upper()} qty={token_qty:.6f} px={float(limit_price):.4f} "
        f"net={float(econ.expected_net_usdc):.6f} rebate={float(econ.expected_rebate_usdc):.6f} "
        f"inventory={float(strategy.inventory_delta_shares):.4f}"
    )
