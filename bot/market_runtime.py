from __future__ import annotations

import asyncio
import threading
import time
import traceback
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, List, Optional

from loguru import logger
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId

from bot.enums import ActiveSide
from bot.lifecycle import collect_btc_market_candidates, resolve_bi_side_market_selection
from bot.ops import log_strategy_run_stop, stop_event_threads


def align_price_to_tick(strategy: Any, price: Decimal, side: str, instrument: Optional[Any]) -> Decimal:
    """Align quote price to current instrument tick size and precision."""
    aligned = price
    tick = Decimal("0.001")
    try:
        if instrument is not None:
            raw_tick = getattr(instrument, "price_increment", None)
            if raw_tick is not None:
                if hasattr(raw_tick, "as_decimal"):
                    tick = Decimal(str(raw_tick.as_decimal()))
                else:
                    tick = Decimal(str(raw_tick))
            elif hasattr(instrument, "info") and instrument.info:
                min_tick = instrument.info.get("minimum_tick_size")
                if min_tick:
                    tick = Decimal(str(min_tick))
    except Exception:
        tick = Decimal("0.001")

    if tick <= 0:
        tick = Decimal("0.001")
    units = aligned / tick
    if side == "buy":
        aligned = units.to_integral_value(rounding=ROUND_FLOOR) * tick
    else:
        aligned = units.to_integral_value(rounding=ROUND_CEILING) * tick

    aligned = max(Decimal("0.01"), min(Decimal("0.99"), aligned))
    try:
        if instrument is not None:
            precision = int(getattr(instrument, "price_precision", 3))
            precision = max(0, precision)
            quantum = Decimal("1").scaleb(-precision)
            aligned = aligned.quantize(quantum)
    except Exception:
        pass
    return aligned


def start_maker_worker(strategy: Any, bid_decimal: Decimal, ask_decimal: Decimal) -> None:
    with strategy._maker_worker_lock:
        if strategy._maker_worker_running or strategy._stopping:
            return
        strategy._maker_worker_running = True

    def _worker() -> None:
        try:
            maker_quote_sync(strategy, float(bid_decimal), float(ask_decimal))
        finally:
            with strategy._maker_worker_lock:
                strategy._maker_worker_running = False

    threading.Thread(target=_worker, daemon=True).start()


def find_btc_instrument(strategy: Any) -> bool:
    """Find the current active BTC 15-min instrument."""
    instruments = strategy.cache.instruments()
    if strategy.startup_verbose:
        logger.info(f"Checking {len(instruments)} loaded instruments...")

    if not instruments:
        logger.error("NO INSTRUMENTS LOADED!")
        return False

    btc_instruments, current_timestamp = collect_btc_market_candidates(
        instruments=instruments,
        startup_verbose=strategy.startup_verbose,
    )

    if not btc_instruments:
        logger.error("NO BTC 15-MIN INSTRUMENTS FOUND!")
        return False

    selection, selection_kind, current_count, future_count = resolve_bi_side_market_selection(
        btc_instruments=btc_instruments,
        current_timestamp=current_timestamp,
        extract_outcome=strategy._extract_outcome_from_instrument,
        preferred_slug=(
            str(strategy.selected_slug or "")
            if not str(strategy.current_market_slug or "")
            else None
        ),
    )
    if strategy.startup_verbose:
        logger.info(
            f"Market candidates: total={len(btc_instruments)} "
            f"current={current_count} future={future_count}"
        )

    if selection_kind is None and selection is not None:
        pass
    elif selection_kind == "future" and selection is not None:
        logger.warning(
            f"No current market, selecting next: {selection.selected_market['slug']} "
            f"(starts in {selection.selected_market['time_diff_minutes']:.1f} min)"
        )
    else:
        logger.warning("No active or future BTC 15-min instruments found in cache. All are PAST.")
        return False

    previous_instrument = str(strategy.instrument_id) if strategy.instrument_id else None
    previous_slug = str(strategy.current_market_slug or "")
    previous_active_side = strategy.active_side
    previous_side_locked = strategy.active_side_locked
    previous_side_reason = strategy.side_decision_reason
    previous_side_score = strategy.side_decision_score
    previous_side_ts = strategy.side_decision_ts
    previous_side_inputs = dict(strategy.side_decision_inputs)
    previous_side_flip_count = strategy.side_flip_count
    previous_pending_flip_side = strategy.side_pending_flip_side
    previous_pending_flip_count = strategy.side_pending_flip_count
    previous_pending_flip_since_ts = float(getattr(strategy, "side_pending_flip_since_ts", 0.0))
    strategy.current_market_slug = selection.current_market_slug
    start_ts = selection.selected_market.get("market_timestamp")
    if strategy.current_market_slug and start_ts:
        strategy.market_start_ts_by_slug[strategy.current_market_slug] = int(start_ts)
    strategy.current_market_end_timestamp = selection.current_market_end_timestamp
    strategy.current_up_instrument_id = strategy._normalize_instrument_id(
        selection.up_instrument_id if selection.matched_up else selection.instrument_id
    )
    strategy.current_down_instrument_id = strategy._normalize_instrument_id(
        selection.down_instrument_id if selection.matched_down else None
    )
    if not selection.matched_up:
        logger.warning(
            f"UP outcome instrument not found explicitly for slug={strategy.current_market_slug}; "
            "falling back to selected primary instrument."
        )
    if strategy.bi_side_enabled and not selection.matched_down:
        logger.warning(
            f"DOWN outcome instrument not found explicitly for slug={strategy.current_market_slug}; "
            "falling back to selected primary instrument."
        )

    seen_market_insts: List[InstrumentId] = []
    for inst in (strategy.current_up_instrument_id, strategy.current_down_instrument_id):
        if inst is not None and inst not in seen_market_insts:
            seen_market_insts.append(inst)
    strategy.current_market_instruments = seen_market_insts or [strategy._normalize_instrument_id(selection.instrument_id)]
    strategy.instrument_id = strategy._normalize_instrument_id(selection.instrument_id)
    preserve_side_state = bool(
        strategy.bi_side_enabled
        and previous_slug
        and strategy.current_market_slug == previous_slug
    )
    if preserve_side_state:
        strategy.active_side = previous_active_side
        strategy.active_side_locked = previous_side_locked
        strategy.side_decision_reason = previous_side_reason
        strategy.side_decision_score = previous_side_score
        strategy.side_decision_ts = previous_side_ts
        strategy.side_decision_inputs = previous_side_inputs
        strategy.side_flip_count = previous_side_flip_count
        strategy.side_pending_flip_side = previous_pending_flip_side
        strategy.side_pending_flip_count = previous_pending_flip_count
        strategy.side_pending_flip_since_ts = previous_pending_flip_since_ts
        strategy.side_decision_done_for_market = previous_active_side != ActiveSide.NONE or previous_side_ts > 0
        strategy._sync_active_instrument()
        logger.info(
            "Preserving side decision across same-market reload: "
            f"slug={strategy.current_market_slug} active_side={strategy.active_side.value} "
            f"locked={'yes' if strategy.active_side_locked else 'no'} reason={strategy.side_decision_reason}"
        )
    else:
        strategy._reset_side_decision_state()
        if strategy.bi_side_enabled and start_ts:
            strategy.side_decision_due_ts = max(time.time(), float(start_ts) + float(strategy.bi_side_decision_grace_sec))
    logger.info(
        f"Selected market: slug={strategy.current_market_slug} "
        f"instruments={len(strategy.current_market_instruments)} "
        f"primary={strategy.instrument_id} "
        f"up={strategy.current_up_instrument_id} down={strategy.current_down_instrument_id} "
        f"active_side={strategy.active_side.value}"
    )
    if strategy.current_market_slug != previous_slug:
        strategy._log_strike_status(strategy.current_market_slug)
    strategy._reset_maker_state_for_new_market(
        previous_instrument,
        str(strategy.instrument_id),
        previous_slug=previous_slug,
        current_slug=str(strategy.current_market_slug or ""),
    )
    for inst_id in strategy.current_market_instruments:
        strategy.subscribe_quote_ticks(inst_id)
    return True


def wait_for_btc_instrument(strategy: Any, timeout_sec: int = 60, poll_interval_sec: int = 2) -> bool:
    """Wait for instruments to arrive in cache during startup."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if find_btc_instrument(strategy):
            return True
        time.sleep(poll_interval_sec)
    return False


def handle_quote_tick(strategy: Any, tick: QuoteTick) -> None:
    """Handle quote tick updates."""
    if strategy._stopping:
        return
    try:
        if strategy.instrument_id is not None and tick.instrument_id != strategy.instrument_id:
            allowed = {str(i) for i in (strategy.current_market_instruments or [])}
            if str(tick.instrument_id) not in allowed:
                return

        if tick.bid_price is None and tick.ask_price is None:
            strategy.consecutive_invalid_quote_ticks += 1
            logger.debug(f"Skipping empty quote: bid={tick.bid_price}, ask={tick.ask_price}")
            strategy._maybe_run_quote_watchdog(trigger="empty_quote")
            return

        bid_decimal = tick.bid_price.as_decimal() if tick.bid_price is not None else None
        ask_decimal = tick.ask_price.as_decimal() if tick.ask_price is not None else None
        bid_size_decimal: Optional[Decimal] = None
        ask_size_decimal: Optional[Decimal] = None
        try:
            if getattr(tick, "bid_size", None) is not None:
                bs = tick.bid_size
                bid_size_decimal = bs.as_decimal() if hasattr(bs, "as_decimal") else Decimal(str(bs))
        except Exception:
            bid_size_decimal = None
        try:
            if getattr(tick, "ask_size", None) is not None:
                a_s = tick.ask_size
                ask_size_decimal = a_s.as_decimal() if hasattr(a_s, "as_decimal") else Decimal(str(a_s))
        except Exception:
            ask_size_decimal = None

        synth_now = time.time()
        stale_max = strategy.stale_quote_synth_max_age_sec
        if bid_decimal is None and ask_decimal is not None:
            bid_age = synth_now - strategy.latest_market_bid_ts if strategy.latest_market_bid_ts > 0 else float("inf")
            if strategy.latest_market_bid is not None and bid_age < stale_max:
                bid_decimal = strategy.latest_market_bid
            else:
                bid_decimal = max(Decimal("0.01"), ask_decimal - Decimal("0.01"))
        if ask_decimal is None and bid_decimal is not None:
            ask_age = synth_now - strategy.latest_market_ask_ts if strategy.latest_market_ask_ts > 0 else float("inf")
            if strategy.latest_market_ask is not None and ask_age < stale_max:
                ask_decimal = strategy.latest_market_ask
            else:
                ask_decimal = min(Decimal("0.99"), bid_decimal + Decimal("0.01"))

        if bid_decimal is None or ask_decimal is None:
            strategy.consecutive_invalid_quote_ticks += 1
            strategy._maybe_run_quote_watchdog(trigger="incomplete_quote")
            return

        if bid_decimal > ask_decimal:
            mid_tmp = (bid_decimal + ask_decimal) / 2
            bid_decimal = max(Decimal("0.01"), mid_tmp - Decimal("0.005"))
            ask_decimal = min(Decimal("0.99"), mid_tmp + Decimal("0.005"))

        strategy.latest_quote_depth_by_inst[str(tick.instrument_id)] = (bid_size_decimal, ask_size_decimal)
        mid_price = (bid_decimal + ask_decimal) / 2
        strategy._append_real_mid_price(tick.instrument_id, mid_price)
        preferred_inst = strategy._instrument_for_side(strategy.active_side) or strategy._primary_instrument_for_market()
        if preferred_inst is None or tick.instrument_id == preferred_inst:
            strategy.last_valid_quote_ts = time.time()
            strategy.consecutive_invalid_quote_ticks = 0
            strategy.latest_market_bid = bid_decimal
            strategy.latest_market_ask = ask_decimal
            strategy.latest_market_bid_ts = time.time()
            strategy.latest_market_ask_ts = strategy.latest_market_bid_ts
            strategy.price_history.append(mid_price)
            if len(strategy.price_history) > strategy.max_history:
                strategy.price_history.pop(0)
        if strategy.maker_mode:
            start_maker_worker(strategy, bid_decimal, ask_decimal)
            return
        logger.warning("Non-maker mode is no longer supported in the slimmed bot path.")
    except Exception as e:
        logger.error(f"Error processing quote tick: {e}")
        traceback.print_exc()


def maker_quote_sync(strategy: Any, bid_price: float, ask_price: float) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            strategy._quote_maker_orders(
                Decimal(str(bid_price)),
                Decimal(str(ask_price)),
            )
        )
    finally:
        loop.close()


def handle_generic_event(strategy: Any, event: Any) -> None:
    """Handle Nautilus events used for metrics updates."""
    event_type = type(event).__name__
    if event_type == "PositionClosed":
        try:
            realized_pnl = float(getattr(event, "realized_pnl", 0.0))
            duration_ns = int(getattr(event, "duration_ns", 0))
            strategy._push_position_closed_to_prometheus(realized_pnl, duration_ns)
        except Exception as e:
            logger.debug(f"Failed to handle PositionClosed event for metrics: {e}")
    elif event_type == "PositionOpened":
        if getattr(strategy, "_prom_live_metrics_ok", False):
            try:
                strategy._prom_live_open_pos.set(1)
            except Exception:
                pass


def handle_stop(strategy: Any) -> None:
    """Called when strategy stops."""
    strategy._stopping = True
    stop_event_threads(
        stop_events=[
            strategy._lifecycle_stop_event,
            strategy._reload_stop_event,
            strategy._quote_watchdog_stop_event,
            strategy._redeem_stop_event,
            strategy._balance_stop_event,
            strategy._binance_ws_stop_event,
            strategy._polymarket_chainlink_ws_stop_event,
            strategy._terminal_dashboard_stop_event,
        ],
        threads=[
            strategy._lifecycle_thread,
            strategy._reload_thread,
            strategy._quote_watchdog_thread,
            strategy._redeem_thread,
            strategy._balance_thread,
            strategy._binance_ws_thread,
            strategy._polymarket_chainlink_ws_thread,
            strategy._terminal_dashboard_thread,
        ],
        join_timeout_sec=2.0,
    )
    logger.info("Integrated BTC strategy stopped")
    strategy._cancel_active_maker_orders()
    strategy.rebate_reporter.flush_daily_report()
    strategy._db_strategy_event(
        "STRATEGY_STOP",
        {
            "mode": "TEST_DRY_RUN" if strategy._is_dry_run_mode() else "LIVE",
            "inventory_delta_shares": float(strategy.inventory_delta_shares),
            "active_side": strategy.active_side.value,
        },
    )
    log_strategy_run_stop(
        trade_db=strategy.trade_db,
        run_id=strategy.run_id,
        is_dry_run_mode=strategy._is_dry_run_mode(),
        test_mode=strategy.test_mode,
        maker_mode=strategy.maker_mode,
        instrument_id=strategy.instrument_id,
        selected_slug=strategy.selected_slug,
        final_inventory_shares=strategy.inventory_delta_shares,
        market_cycle_realized_net_usdc=strategy.market_cycle_realized_net_usdc,
    )

    if strategy.grafana_exporter:
        try:
            strategy.grafana_exporter.stop()
        except Exception:
            pass
    if strategy.terminal_dashboard:
        try:
            strategy.terminal_dashboard.stop()
        except Exception:
            pass
