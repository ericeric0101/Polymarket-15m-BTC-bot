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
from bot.adapter_overrides import quote_provenance_for_tick
from bot.edge_observation import build_quote_age_telemetry
from bot.lifecycle import collect_btc_market_candidates, resolve_bi_side_market_selection
from bot.ops import log_strategy_run_stop, stop_event_threads


def quote_tick_event_timestamp(tick: Any, fallback_now: float) -> float:
    """Return QuoteTick event time in epoch seconds, with safe fallback."""
    try:
        raw = float(getattr(tick, "ts_event", 0) or 0)
        if raw > 1e17:
            raw /= 1e9
        elif raw > 1e11:
            raw /= 1e3
        if raw > 0:
            return raw
    except Exception:
        pass
    return float(fallback_now)


def quote_tick_adapter_timestamp(tick: Any, fallback_now: float) -> float:
    """Return the local adapter creation time carried by a Nautilus QuoteTick."""
    try:
        raw = float(getattr(tick, "ts_init", 0) or 0)
        if raw > 1e17:
            raw /= 1e9
        elif raw > 1e11:
            raw /= 1e3
        if raw > 0:
            return raw
    except Exception:
        pass
    return float(fallback_now)


def quote_event_is_fresh(
    *,
    received_ts: float,
    event_ts: float,
    max_age_sec: float,
    clock_skew_tolerance_sec: Decimal | float,
) -> bool:
    """Return whether an exchange quote is current enough to drive execution."""
    age = build_quote_age_telemetry(
        observation_ts=received_ts,
        quote_ts=event_ts,
        clock_skew_tolerance_sec=clock_skew_tolerance_sec,
    )
    return age.effective_age_sec is not None and age.effective_age_sec <= Decimal(str(max_age_sec))


def quote_transport_is_fresh(
    *,
    received_ts: float,
    adapter_emitted_ts: float,
    max_age_sec: float,
    clock_skew_tolerance_sec: Decimal | float,
) -> bool:
    """Return whether a native CLOB book was emitted recently enough to execute on.

    Polymarket's message timestamp identifies the book's most recent market
    update. It can legitimately predate a newly received current snapshot, so
    it is telemetry rather than the execution-freshness clock.
    """
    return quote_event_is_fresh(
        received_ts=received_ts,
        event_ts=adapter_emitted_ts,
        max_age_sec=max_age_sec,
        clock_skew_tolerance_sec=clock_skew_tolerance_sec,
    )


def _record_quote_transport_telemetry(
    strategy: Any,
    *,
    tick: Any,
    received_ts: float,
    event_ts: float,
    adapter_emitted_ts: float,
    source: str,
    quote_is_fresh: bool,
    raw_ws_received_ts: Optional[float],
    data_engine_queue_depth: Optional[int],
    raw_bid_present: bool,
    raw_ask_present: bool,
    bid_size: Optional[Decimal],
    ask_size: Optional[Decimal],
) -> None:
    """Persist a throttled source/timestamp trace without affecting quote handling."""
    if not hasattr(strategy, "_db_strategy_event"):
        return
    instrument_key = str(tick.instrument_id)
    raw_age_sec = received_ts - event_ts
    adapter_delay_sec = received_ts - adapter_emitted_ts
    ws_to_adapter_delay_sec = (
        adapter_emitted_ts - raw_ws_received_ts
        if raw_ws_received_ts is not None
        else None
    )
    state = getattr(strategy, "_quote_transport_telemetry_state", None)
    if state is None:
        state = {}
        strategy._quote_transport_telemetry_state = state
    last_ts = float(state.get(instrument_key, 0.0))
    if received_ts - last_ts < 5.0:
        return
    state[instrument_key] = received_ts
    strategy._db_strategy_event(
        "QUOTE_TRANSPORT_TELEMETRY",
        {
            "quote_source": source,
            "quote_event_ts": float(event_ts),
            "raw_ws_received_ts": raw_ws_received_ts,
            "adapter_emitted_ts": float(adapter_emitted_ts),
            "quote_received_ts": float(received_ts),
            "quote_age_raw_sec": float(raw_age_sec),
            "ws_to_adapter_delay_sec": ws_to_adapter_delay_sec,
            "data_engine_queue_depth": data_engine_queue_depth,
            "adapter_to_strategy_delay_sec": float(adapter_delay_sec),
            "quote_is_fresh": bool(quote_is_fresh),
            "raw_bid_present": bool(raw_bid_present),
            "raw_ask_present": bool(raw_ask_present),
            "bid_size": float(bid_size) if bid_size is not None else None,
            "ask_size": float(ask_size) if ask_size is not None else None,
        },
    )


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


def mark_quote_subscription_pending(
    strategy: Any,
    instrument_ids: List[InstrumentId],
    *,
    clear_cached_quotes: bool,
) -> None:
    """Require a new quote for the current market before declaring its feed healthy."""
    now_ts = time.time()
    instrument_keys = {str(inst_id) for inst_id in instrument_ids}
    strategy.quote_recovery_started_ts = now_ts
    strategy.quote_recovery_pending_instruments = instrument_keys
    strategy.quote_recovery_attempts = 0
    if not clear_cached_quotes:
        return
    for inst_key in instrument_keys:
        getattr(strategy, "latest_quote_by_inst", {}).pop(inst_key, None)
        getattr(strategy, "latest_quote_depth_by_inst", {}).pop(inst_key, None)
        getattr(strategy, "last_quote_update_ts_by_inst", {}).pop(inst_key, None)
        getattr(strategy, "last_quote_received_ts_by_inst", {}).pop(inst_key, None)


def refresh_quote_tick_subscriptions(strategy: Any) -> None:
    """Replace subscriptions instead of relying on an idempotent subscribe after a feed stall."""
    instrument_ids = list(getattr(strategy, "current_market_instruments", []) or [])
    mark_quote_subscription_pending(strategy, instrument_ids, clear_cached_quotes=True)
    for inst_id in instrument_ids:
        try:
            strategy.unsubscribe_quote_ticks(inst_id)
        except Exception as exc:
            logger.debug(f"Quote unsubscribe skipped for {inst_id}: {exc}")
        try:
            strategy.subscribe_quote_ticks(inst_id)
        except Exception as exc:
            logger.warning(f"Quote resubscribe failed for {inst_id}: {exc}")


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

    preferred_slug = None
    phase_value = str(getattr(getattr(strategy, "current_phase", None), "value", "") or "")
    next_market_slug = str(getattr(strategy, "next_market_slug", "") or "").strip()
    if next_market_slug and phase_value in {"WAITING", "SETTLING"}:
        preferred_slug = next_market_slug
    elif not str(strategy.current_market_slug or ""):
        preferred_slug = str(strategy.selected_slug or "") or None

    selection, selection_kind, current_count, future_count = resolve_bi_side_market_selection(
        btc_instruments=btc_instruments,
        current_timestamp=current_timestamp,
        extract_outcome=strategy._extract_outcome_from_instrument,
        preferred_slug=preferred_slug,
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
    if hasattr(strategy, "_restore_shadow_simulation_for_slug"):
        strategy._restore_shadow_simulation_for_slug(strategy.current_market_slug or "")
    if strategy.current_market_slug != previous_slug:
        mark_quote_subscription_pending(
            strategy,
            strategy.current_market_instruments,
            clear_cached_quotes=True,
        )
    for inst_id in strategy.current_market_instruments:
        strategy.subscribe_quote_ticks(inst_id)
    return True


def wait_for_btc_instrument(strategy: Any, timeout_sec: int = 60, poll_interval_sec: int = 2) -> bool:
    """Wait for instruments to arrive in cache during startup."""
    deadline = time.time() + timeout_sec
    bootstrap_attempted = False
    while time.time() < deadline:
        if find_btc_instrument(strategy):
            return True
        if not bootstrap_attempted and hasattr(strategy, "_bootstrap_btc_instruments_into_cache"):
            bootstrap_attempted = True
            try:
                loaded = int(strategy._bootstrap_btc_instruments_into_cache())
            except Exception:
                loaded = 0
            if loaded > 0 and find_btc_instrument(strategy):
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

        raw_bid_present = tick.bid_price is not None
        raw_ask_present = tick.ask_price is not None
        bid_decimal = tick.bid_price.as_decimal() if raw_bid_present else None
        ask_decimal = tick.ask_price.as_decimal() if raw_ask_present else None
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

        quote_received_ts = time.time()
        quote_event_ts = quote_tick_event_timestamp(tick, quote_received_ts)
        provenance = quote_provenance_for_tick(tick)
        quote_source = str(provenance.get("source") or "unknown")
        adapter_emitted_ts = quote_tick_adapter_timestamp(tick, quote_received_ts)
        preferred_inst = strategy._instrument_for_side(strategy.active_side) or strategy._primary_instrument_for_market()
        is_preferred_quote = preferred_inst is None or tick.instrument_id == preferred_inst
        # Receipt time proves the subscribed transport remains alive. It is
        # deliberately independent from the exchange event timestamp below.
        getattr(strategy, "last_quote_received_ts_by_inst", {})[str(tick.instrument_id)] = quote_received_ts
        if is_preferred_quote:
            strategy.last_valid_quote_ts = quote_received_ts
        clock_skew_tolerance_sec = getattr(
            strategy,
            "quote_event_clock_skew_tolerance_sec",
            Decimal("0.25"),
        )
        is_native_book_update = quote_source in {"ws_price_change", "ws_snapshot"}
        quote_is_fresh = (
            quote_transport_is_fresh(
                received_ts=quote_received_ts,
                adapter_emitted_ts=adapter_emitted_ts,
                max_age_sec=float(strategy.quote_stale_sec),
                clock_skew_tolerance_sec=clock_skew_tolerance_sec,
            )
            if is_native_book_update
            else quote_source != "transport_heartbeat"
            and quote_event_is_fresh(
                received_ts=quote_received_ts,
                event_ts=quote_event_ts,
                max_age_sec=float(strategy.quote_stale_sec),
                clock_skew_tolerance_sec=clock_skew_tolerance_sec,
            )
        )
        _record_quote_transport_telemetry(
            strategy,
            tick=tick,
            received_ts=quote_received_ts,
            event_ts=quote_event_ts,
            adapter_emitted_ts=adapter_emitted_ts,
            source=quote_source,
            quote_is_fresh=quote_is_fresh,
            raw_ws_received_ts=(
                float(provenance["raw_ws_received_ts"])
                if provenance.get("raw_ws_received_ts") is not None
                else None
            ),
            data_engine_queue_depth=(
                int(provenance["data_engine_queue_depth"])
                if provenance.get("data_engine_queue_depth") is not None
                else None
            ),
            raw_bid_present=raw_bid_present,
            raw_ask_present=raw_ask_present,
            bid_size=bid_size_decimal,
            ask_size=ask_size_decimal,
        )
        if not quote_is_fresh:
            # A cached transport heartbeat or old exchange event is not valid
            # pricing. Do not update the executable quote state, but do retain
            # the receipt timestamp above so the watchdog can distinguish an
            # idle connected socket from a dead transport.
            return

        strategy.latest_quote_depth_by_inst[str(tick.instrument_id)] = (bid_size_decimal, ask_size_decimal)
        getattr(strategy, "latest_quote_by_inst", {})[str(tick.instrument_id)] = (bid_decimal, ask_decimal)
        # Quote plans need the local time at which a current CLOB book was
        # emitted, not the book's last internal market-update timestamp.
        getattr(strategy, "last_quote_update_ts_by_inst", {})[str(tick.instrument_id)] = adapter_emitted_ts
        pending_instruments = getattr(strategy, "quote_recovery_pending_instruments", set())
        if str(tick.instrument_id) in pending_instruments:
            # A binary market needs a fresh book for every subscribed outcome.
            # Clearing the whole set after the first token hid a missing UP/DOWN
            # subscription behind transport heartbeats and prevented recovery.
            pending_instruments.discard(str(tick.instrument_id))
            if not pending_instruments:
                strategy.quote_recovery_started_ts = 0.0
                strategy.quote_recovery_attempts = 0
        mid_price = (bid_decimal + ask_decimal) / 2
        strategy._append_real_mid_price(tick.instrument_id, mid_price)
        if hasattr(strategy, "_lead_lag_observation_on_quote"):
            strategy._lead_lag_observation_on_quote(quote_received_ts)
        if hasattr(strategy, "_shadow_simulation_on_quote"):
            strategy._shadow_simulation_on_quote(
                tick.instrument_id,
                bid_decimal,
                ask_decimal,
                quote_received_ts,
            )
        if is_preferred_quote:
            strategy.last_valid_quote_ts = quote_received_ts
            strategy.consecutive_invalid_quote_ticks = 0
            strategy.latest_market_bid = bid_decimal
            strategy.latest_market_ask = ask_decimal
            strategy.latest_market_bid_ts = time.time()
            strategy.latest_market_ask_ts = strategy.latest_market_bid_ts
            strategy.price_history.append(mid_price)
            telemetry = getattr(strategy, "trade_telemetry", None)
            if telemetry is not None:
                try:
                    markouts = telemetry.observe(
                        strategy._instrument_key(tick.instrument_id),
                        mid_price,
                        time.time(),
                    )
                    for markout in markouts:
                        strategy._db_order_event(
                            event_type="FILL_MARKOUT",
                            client_order_id=markout["fill_id"],
                            side=str(markout["side"]).upper(),
                            price=markout["markout_mid"],
                            qty=0.0,
                            status="OBSERVED",
                            payload=markout,
                        )
                except Exception as telemetry_error:
                    logger.debug(f"Trade telemetry markout update skipped: {telemetry_error}")
            if len(strategy.price_history) > strategy.max_history:
                strategy.price_history.pop(0)
        if strategy.maker_mode:
            start_maker_worker(strategy, bid_decimal, ask_decimal)
            return
        logger.warning("Non-maker mode is no longer supported in the slimmed bot path.")
    except Exception as e:
        if hasattr(strategy, "_record_dashboard_error"):
            strategy._record_dashboard_error(f"Quote tick error: {e}")
        logger.error(f"Error processing quote tick: {e}")
        traceback.print_exc()


def maker_quote_sync(strategy: Any, bid_price: float, ask_price: float) -> None:
    loop = asyncio.new_event_loop()
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
    smart_money_tracker = getattr(strategy, "smart_money_tracker", None)
    if smart_money_tracker is not None:
        try:
            smart_money_tracker.stop()
        except Exception:
            pass
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
