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
from bot.db_runtime import take_sync_journal_write_report
from bot.edge_observation import build_quote_age_telemetry
from bot.lifecycle import collect_btc_market_candidates, resolve_bi_side_market_selection
from bot.ops import log_strategy_run_stop, stop_event_threads
from bot.outcome_lead_lag_ingress import publish_strategy_tick
from bot.trend_entry_shadow import record_strategy_quote
from bot.forward_shadow import record_strategy_quote as record_forward_shadow_quote


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


def quote_delivery_is_fresh(
    *,
    received_ts: float,
    adapter_emitted_ts: float,
    max_delivery_delay_sec: float,
    clock_skew_tolerance_sec: Decimal | float,
) -> bool:
    """Reject native books delayed in the local adapter/DataEngine pipeline."""
    return quote_event_is_fresh(
        received_ts=received_ts,
        event_ts=adapter_emitted_ts,
        max_age_sec=max_delivery_delay_sec,
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
    provenance = quote_provenance_for_tick(tick)
    data_engine_published_ts = provenance.get("data_engine_published_ts")
    adapter_coalesce_delay_sec = (
        float(data_engine_published_ts) - float(adapter_emitted_ts)
        if data_engine_published_ts is not None
        else None
    )
    data_engine_delivery_delay_sec = (
        float(received_ts) - float(data_engine_published_ts)
        if data_engine_published_ts is not None
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
    callback_report = getattr(strategy, "_quote_callback_last_report", None)
    journal_write_report = take_sync_journal_write_report(strategy)
    strategy._last_market_data_health = {
        "quote_fresh": bool(quote_is_fresh), "queue_depth": data_engine_queue_depth,
        "observed_ts": received_ts, "instrument_id": instrument_key,
        "queue_window": provenance.get("data_engine_queue_window"),
    }
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
            "adapter_coalesce_delay_sec": adapter_coalesce_delay_sec,
            "data_engine_published_ts": data_engine_published_ts,
            "data_engine_delivery_delay_sec": data_engine_delivery_delay_sec,
            "data_engine_queue_depth": data_engine_queue_depth,
            "data_engine_queue_window": provenance.get("data_engine_queue_window"),
            "adapter_to_strategy_delay_sec": float(adapter_delay_sec),
            "quote_callback_window": callback_report,
            "sync_journal_write_window": journal_write_report,
            "quote_is_fresh": bool(quote_is_fresh),
            "raw_bid_present": bool(raw_bid_present),
            "raw_ask_present": bool(raw_ask_present),
            "bid_size": float(bid_size) if bid_size is not None else None,
            "ask_size": float(ask_size) if ask_size is not None else None,
        },
    )
    if callback_report is not None:
        strategy._quote_callback_last_report = None


def _record_quote_callback_duration(
    strategy: Any,
    duration_sec: float,
    *,
    now_ts: Optional[float] = None,
) -> Optional[dict[str, float | int]]:
    """Aggregate strategy callback cost over 10s without per-tick DB writes."""
    now = time.monotonic() if now_ts is None else float(now_ts)
    state = getattr(strategy, "_quote_callback_timing_state", None)
    if state is None:
        state = {
            "started": now,
            "count": 0,
            "total_ms": 0.0,
            "max_ms": 0.0,
            "over_50ms": 0,
            "over_250ms": 0,
        }
        strategy._quote_callback_timing_state = state
    duration_ms = max(0.0, float(duration_sec) * 1000.0)
    state["count"] += 1
    state["total_ms"] += duration_ms
    state["max_ms"] = max(float(state["max_ms"]), duration_ms)
    state["over_50ms"] += int(duration_ms > 50.0)
    state["over_250ms"] += int(duration_ms > 250.0)
    if now - float(state["started"]) < 10.0:
        return None
    report = {
        "window_sec": max(0.0, now - float(state["started"])),
        "count": int(state["count"]),
        "total_ms": round(float(state["total_ms"]), 3),
        "avg_ms": round(float(state["total_ms"]) / max(1, int(state["count"])), 3),
        "max_ms": round(float(state["max_ms"]), 3),
        "over_50ms": int(state["over_50ms"]),
        "over_250ms": int(state["over_250ms"]),
    }
    strategy._quote_callback_last_report = report
    strategy._quote_callback_timing_state = {
        "started": now,
        "count": 0,
        "total_ms": 0.0,
        "max_ms": 0.0,
        "over_50ms": 0,
        "over_250ms": 0,
    }
    return report


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
        getattr(strategy, "last_quote_source_ts_by_inst", {}).pop(inst_key, None)
        getattr(strategy, "last_quote_received_ts_by_inst", {}).pop(inst_key, None)


def refresh_quote_tick_subscriptions(strategy: Any) -> List[InstrumentId]:
    """Refresh only stale current-market legs, retaining healthy sibling books."""
    now_ts = time.time()
    stale_after = max(0.0, float(getattr(strategy, "quote_stale_sec", 0.0) or 0.0))
    last_updates = getattr(strategy, "last_quote_update_ts_by_inst", {})
    quotes = getattr(strategy, "latest_quote_by_inst", {})
    pending = getattr(strategy, "quote_recovery_pending_instruments", set()) or set()
    instrument_ids = []
    for inst_id in list(getattr(strategy, "current_market_instruments", []) or []):
        key = str(inst_id)
        try:
            age = now_ts - float(last_updates.get(key, 0.0) or 0.0)
        except (AttributeError, TypeError, ValueError):
            age = float("inf")
        if key in pending or key not in quotes or not (0.0 <= age < stale_after):
            instrument_ids.append(inst_id)
    if not instrument_ids:
        return []
    mark_quote_subscription_pending(strategy, instrument_ids, clear_cached_quotes=True)
    # Polymarket's WebSocket client reference-counts assets shared by quote and
    # L2 subscriptions. Remove both references before adding either one back;
    # interleaving unsubscribe/subscribe leaves the count above zero and sends
    # no actual WebSocket refresh request.
    for inst_id in instrument_ids:
        try:
            strategy.unsubscribe_quote_ticks(inst_id)
        except Exception as exc:
            logger.debug(f"Quote unsubscribe skipped for {inst_id}: {exc}")
        try:
            strategy.unsubscribe_order_book_deltas(inst_id)
        except Exception as exc:
            logger.debug(f"L2 unsubscribe skipped for {inst_id}: {exc}")
    for inst_id in instrument_ids:
        try:
            strategy.subscribe_quote_ticks(inst_id)
        except Exception as exc:
            logger.warning(f"Quote resubscribe failed for {inst_id}: {exc}")
        try:
            strategy.subscribe_order_book_deltas(inst_id)
        except Exception as exc:
            logger.warning(f"L2 resubscribe failed for {inst_id}: {exc}")
    return instrument_ids


def next_market_pair_instruments(
    btc_instruments: list[dict[str, Any]],
    *, current_slug: str, current_start_ts: int | None,
    extract_outcome: Any,
) -> list[InstrumentId]:
    """Return the next distinct BTC-15m UP/DOWN pair already in cache.

    This is an operational prewarm, not a trading-market selection.  Keeping
    one future pair subscribed means a rollover does not depend on a dynamic
    websocket subscribe command arriving at the exact market boundary.
    """
    current_start = int(current_start_ts or 0)
    candidates = [
        item for item in btc_instruments
        if str(item.get("slug") or "") != str(current_slug)
        and int(item.get("market_timestamp") or 0) > current_start
    ]
    if not candidates:
        return []
    next_start = min(int(item["market_timestamp"]) for item in candidates)
    next_slug_items = [item for item in candidates if int(item.get("market_timestamp") or 0) == next_start]
    by_outcome: dict[str, InstrumentId] = {}
    for item in next_slug_items:
        instrument = item.get("instrument")
        instrument_id = getattr(instrument, "id", None)
        outcome = str(extract_outcome(instrument) or "").lower() if instrument is not None else ""
        if instrument_id is not None and outcome in {"up", "down"}:
            by_outcome.setdefault(outcome, instrument_id)
    return [by_outcome[side] for side in ("up", "down") if side in by_outcome]


def market_pair_instruments_by_slug(
    btc_instruments: list[dict[str, Any]], *, extract_outcome: Any,
) -> dict[str, dict[str, str]]:
    """Index cached BTC market outcome instruments for research-time joins.

    This is metadata only: quote freshness is still checked at the point of
    use, so an unsubscribed future market cannot inherit an old BBO.
    """
    result: dict[str, dict[str, str]] = {}
    for item in btc_instruments:
        slug = str(item.get("slug") or "")
        instrument = item.get("instrument")
        instrument_id = getattr(instrument, "id", None) if instrument is not None else None
        if not slug or instrument_id is None:
            continue
        try:
            side = str(extract_outcome(instrument) or "").strip().upper()
        except Exception:
            continue
        if side not in {"UP", "DOWN"}:
            continue
        result.setdefault(slug, {}).setdefault(side, str(instrument_id))
    return result


def replace_market_subscriptions(
    strategy: Any,
    previous_instrument_ids: List[Any],
    current_instrument_ids: List[Any],
    *,
    prewarm_instrument_ids: List[Any] | None = None,
) -> bool:
    """Keep current quote/L2 plus one future pair's quote-only prewarm."""
    previous = {str(inst): inst for inst in previous_instrument_ids if inst is not None}
    tracked_quote = getattr(strategy, "_managed_market_quote_subscription_ids", None)
    tracked_l2 = getattr(strategy, "_managed_market_l2_subscription_ids", None)
    if not isinstance(tracked_quote, set):
        tracked_quote = set(previous)
    else:
        tracked_quote = set(tracked_quote)
    if not isinstance(tracked_l2, set):
        tracked_l2 = set(previous)
    else:
        tracked_l2 = set(tracked_l2)
    tracked_instruments = getattr(strategy, "_managed_market_subscription_instruments", None)
    tracked_instruments = dict(tracked_instruments) if isinstance(tracked_instruments, dict) else dict(previous)
    current = {str(inst): inst for inst in current_instrument_ids if inst is not None}
    desired_quotes = dict(current)
    desired_quotes.update({str(inst): inst for inst in (prewarm_instrument_ids or []) if inst is not None})
    # The next market needs a warm BBO, not a second pair of 4 Hz full-book
    # snapshots competing with the current market in the DataEngine queue.
    desired_l2 = current
    healthy = True

    for inst_key in sorted((tracked_quote - set(desired_quotes)) | (tracked_l2 - set(desired_l2))):
        inst = tracked_instruments.get(inst_key, previous.get(inst_key, inst_key))
        for tracked, desired, unsubscribe in (
            (tracked_quote, desired_quotes, strategy.unsubscribe_quote_ticks),
            (tracked_l2, desired_l2, strategy.unsubscribe_order_book_deltas),
        ):
            if inst_key not in tracked or inst_key in desired:
                continue
            try:
                unsubscribe(inst)
                tracked.discard(inst_key)
            except Exception as exc:
                healthy = False
                logger.warning(f"Old market subscription cleanup failed for {inst}: {exc}")
        if inst_key not in tracked_quote and inst_key not in tracked_l2:
            tracked_instruments.pop(inst_key, None)

    for inst_key, inst in desired_quotes.items():
        for tracked, desired, subscribe in (
            (tracked_quote, desired_quotes, strategy.subscribe_quote_ticks),
            (tracked_l2, desired_l2, strategy.subscribe_order_book_deltas),
        ):
            if inst_key in tracked or inst_key not in desired:
                continue
            try:
                subscribe(inst)
                tracked.add(inst_key)
                tracked_instruments[inst_key] = inst
            except Exception as exc:
                healthy = False
                logger.warning(f"Market subscription failed for {inst}: {exc}")

    strategy._managed_market_quote_subscription_ids = tracked_quote
    strategy._managed_market_l2_subscription_ids = tracked_l2
    strategy._managed_market_subscription_instruments = tracked_instruments
    return healthy


def promote_prewarm_quotes_to_current_market(strategy: Any, *, now_ts: float | None = None) -> set[str]:
    """Promote only still-fresh BBOs captured while the next pair was prewarmed.

    Promotion updates quote state only; it does not replay a tick through
    signal, shadow, fast-follow, or order-submission callbacks. Missing/stale
    legs remain pending and continue to fail their normal quote/depth gates.
    """
    now = time.time() if now_ts is None else float(now_ts)
    stale_after = max(0.0, float(getattr(strategy, "quote_stale_sec", 0.0) or 0.0))
    prewarmed = getattr(strategy, "quote_prewarm_latest_by_inst", {})
    if not isinstance(prewarmed, dict):
        prewarmed = {}
    pending = getattr(strategy, "quote_recovery_pending_instruments", set())
    if not isinstance(pending, set):
        pending = set(pending or ())
        strategy.quote_recovery_pending_instruments = pending
    promoted: set[str] = set()
    preferred = None
    try:
        preferred = strategy._instrument_for_side(strategy.active_side)
    except Exception:
        preferred = None
    if preferred is None:
        preferred = getattr(strategy, "instrument_id", None)
    preferred_key = str(preferred) if preferred is not None else None

    for instrument in list(getattr(strategy, "current_market_instruments", []) or []):
        key = str(instrument)
        quote = prewarmed.pop(key, None)
        if not isinstance(quote, dict):
            continue
        try:
            received_ts = float(quote.get("received_ts", 0.0) or 0.0)
            bid = Decimal(str(quote["bid"]))
            ask = Decimal(str(quote["ask"]))
        except (KeyError, ArithmeticError, TypeError, ValueError):
            continue
        age = now - received_ts
        if received_ts <= 0 or age < 0 or age >= stale_after or bid <= 0 or ask <= 0 or bid > ask:
            continue

        strategy.latest_quote_by_inst[key] = (bid, ask)
        strategy.latest_quote_depth_by_inst[key] = (
            quote.get("bid_size"), quote.get("ask_size"),
        )
        strategy.last_quote_received_ts_by_inst[key] = received_ts
        strategy.last_quote_update_ts_by_inst[key] = float(
            quote.get("adapter_emitted_ts", received_ts) or received_ts
        )
        source_ts = float(quote.get("source_ts", 0.0) or 0.0)
        if source_ts > 0:
            strategy.last_quote_source_ts_by_inst[key] = source_ts
        pending.discard(key)
        promoted.add(key)
        if key == preferred_key:
            strategy.latest_market_bid = bid
            strategy.latest_market_ask = ask
            strategy.latest_market_bid_ts = received_ts
            strategy.latest_market_ask_ts = received_ts
            strategy.last_valid_quote_ts = received_ts
            strategy.last_quote_update_ts = strategy.last_quote_update_ts_by_inst[key]

    strategy.quote_prewarm_latest_by_inst = prewarmed
    if not pending:
        strategy.quote_recovery_started_ts = 0.0
        strategy.quote_recovery_attempts = 0
        if promoted:
            record_handoff = getattr(strategy, "_db_strategy_event", None)
            if callable(record_handoff):
                record_handoff(
                    "QUOTE_MARKET_HANDOFF_PREWARM_READY",
                    {
                        "slug": str(getattr(strategy, "current_market_slug", "") or ""),
                        "instruments": sorted(promoted),
                        "received_ts": now,
                    },
                )
    return promoted


def handle_order_book_deltas(strategy: Any, deltas: Any) -> None:
    """Stamp fresh native L2 delivery for fast-follow's FOK precheck.

    The DataEngine/cache owns book reconstruction. This callback intentionally
    performs no pricing, database I/O, or order action.
    """
    instrument_id = getattr(deltas, "instrument_id", None)
    if instrument_id is None:
        return
    updates = getattr(strategy, "fast_follow_l2_update_ts_by_inst", None)
    if not isinstance(updates, dict):
        updates = {}
        strategy.fast_follow_l2_update_ts_by_inst = updates
    updates[str(instrument_id)] = time.time()


def find_btc_instrument(strategy: Any) -> bool:
    """Serialize market selection because lifecycle, reload, and watchdog share it."""
    lock = getattr(strategy, "_market_selection_lock", None)
    if lock is None:
        lock = threading.RLock()
        strategy._market_selection_lock = lock
    with lock:
        return _find_btc_instrument_unlocked(strategy)


def _find_btc_instrument_unlocked(strategy: Any) -> bool:
    """Find the current active BTC 15-min instrument."""
    instruments = strategy.cache.instruments()
    if strategy.startup_verbose:
        logger.info(f"Checking {len(instruments)} loaded instruments...")

    if not instruments:
        # During node startup the data client can become ready before its
        # instrument provider has populated the cache. Callers already retry
        # and emit a terminal startup/reload warning if the wait expires.
        logger.debug("Instrument cache is not populated yet; waiting for market data.")
        return False

    btc_instruments, current_timestamp = collect_btc_market_candidates(
        instruments=instruments,
        startup_verbose=strategy.startup_verbose,
    )

    if not btc_instruments:
        logger.error("NO BTC 15-MIN INSTRUMENTS FOUND!")
        return False

    # Index current and cached future markets for research joins. Freshness is
    # validated when a quote is consumed; this never authorizes trading.
    strategy.research_market_instruments_by_slug = market_pair_instruments_by_slug(
        btc_instruments,
        extract_outcome=strategy._extract_outcome_from_instrument,
    )

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
    previous_market_instruments = list(getattr(strategy, "current_market_instruments", []) or [])
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
    strategy.current_up_instrument_matched = bool(selection.matched_up)
    strategy.current_down_instrument_matched = bool(selection.matched_down)
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
    prewarm_instruments = next_market_pair_instruments(
        btc_instruments,
        current_slug=strategy.current_market_slug,
        current_start_ts=start_ts,
        extract_outcome=strategy._extract_outcome_from_instrument,
    )
    strategy.quote_prewarm_instruments = {str(inst) for inst in prewarm_instruments}
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
        promoted = promote_prewarm_quotes_to_current_market(strategy)
        if promoted:
            logger.info(
                "Promoted fresh prewarm quotes at market handoff: "
                f"slug={strategy.current_market_slug} instruments={sorted(promoted)} "
                f"pending={sorted(strategy.quote_recovery_pending_instruments)}"
            )
    subscriptions_healthy = replace_market_subscriptions(
        strategy,
        previous_market_instruments,
        strategy.current_market_instruments,
        prewarm_instrument_ids=prewarm_instruments,
    )
    if not subscriptions_healthy:
        logger.error(
            "Market selection is incomplete because one or more quote/L2 subscriptions failed: "
            f"slug={strategy.current_market_slug} instruments="
            f"{[str(item) for item in strategy.current_market_instruments]}"
        )
        return False
    if prewarm_instruments:
        logger.info(
            "Quote prewarm subscribed for next BTC 15m pair: "
            f"current={strategy.current_market_slug} instruments={','.join(map(str, prewarm_instruments))}"
        )
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


def _capture_fresh_prewarm_quote(strategy: Any, tick: QuoteTick) -> None:
    """Keep a bounded latest BBO for the one prewarmed market pair only."""
    instrument_key = str(getattr(tick, "instrument_id", "") or "")
    allowed = {str(item) for item in (getattr(strategy, "quote_prewarm_instruments", set()) or set())}
    if not instrument_key or instrument_key not in allowed:
        return
    try:
        bid_raw = getattr(tick, "bid_price", None)
        ask_raw = getattr(tick, "ask_price", None)
        if bid_raw is None or ask_raw is None:
            return
        bid = bid_raw.as_decimal() if hasattr(bid_raw, "as_decimal") else Decimal(str(bid_raw))
        ask = ask_raw.as_decimal() if hasattr(ask_raw, "as_decimal") else Decimal(str(ask_raw))
        if bid <= 0 or ask <= 0 or bid > ask:
            return
    except (ArithmeticError, TypeError, ValueError):
        return

    received_ts = time.time()
    event_ts = quote_tick_event_timestamp(tick, received_ts)
    adapter_ts = quote_tick_adapter_timestamp(tick, received_ts)
    provenance = quote_provenance_for_tick(tick)
    source = str(provenance.get("source") or "unknown")
    tolerance = getattr(strategy, "quote_event_clock_skew_tolerance_sec", Decimal("0.25"))
    if source in {"ws_price_change", "ws_snapshot"}:
        fresh = quote_delivery_is_fresh(
            received_ts=received_ts,
            adapter_emitted_ts=adapter_ts,
            max_delivery_delay_sec=float(
                getattr(strategy, "quote_max_delivery_delay_sec", strategy.quote_stale_sec)
            ),
            clock_skew_tolerance_sec=tolerance,
        )
    else:
        fresh = source != "transport_heartbeat" and quote_event_is_fresh(
            received_ts=received_ts,
            event_ts=event_ts,
            max_age_sec=float(strategy.quote_stale_sec),
            clock_skew_tolerance_sec=tolerance,
        )
    if not fresh:
        return

    def size_decimal(name: str) -> Decimal | None:
        raw = getattr(tick, name, None)
        if raw is None:
            return None
        try:
            return raw.as_decimal() if hasattr(raw, "as_decimal") else Decimal(str(raw))
        except (ArithmeticError, TypeError, ValueError):
            return None

    quotes = getattr(strategy, "quote_prewarm_latest_by_inst", None)
    if not isinstance(quotes, dict):
        quotes = {}
        strategy.quote_prewarm_latest_by_inst = quotes
    quotes[instrument_key] = {
        "bid": bid,
        "ask": ask,
        "bid_size": size_decimal("bid_size"),
        "ask_size": size_decimal("ask_size"),
        "received_ts": received_ts,
        "source_ts": quote_tick_event_timestamp(tick, 0.0),
        "adapter_emitted_ts": adapter_ts,
        "source": source,
    }


def handle_quote_tick(strategy: Any, tick: QuoteTick) -> None:
    """Handle quote tick updates."""
    if strategy._stopping:
        return
    callback_started = time.monotonic()
    callback_stage_ms: dict[str, float] = {}
    try:
        if strategy.instrument_id is not None and tick.instrument_id != strategy.instrument_id:
            allowed = {str(i) for i in (strategy.current_market_instruments or [])}
            if str(tick.instrument_id) not in allowed:
                # A prewarmed next-market quote is intentionally not allowed
                # to influence current-market pricing.  It is only an
                # upstream subscription acknowledgement for rollover health.
                if str(tick.instrument_id) in set(getattr(strategy, "quote_prewarm_instruments", set()) or set()):
                    seen = getattr(strategy, "quote_prewarm_first_quote_ts_by_inst", None)
                    if not isinstance(seen, dict):
                        seen = {}
                        strategy.quote_prewarm_first_quote_ts_by_inst = seen
                    seen.setdefault(str(tick.instrument_id), time.time())
                    _capture_fresh_prewarm_quote(strategy, tick)
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
        quote_source_ts = quote_tick_event_timestamp(tick, 0.0)
        provenance = quote_provenance_for_tick(tick)
        quote_source = str(provenance.get("source") or "unknown")
        adapter_emitted_ts = quote_tick_adapter_timestamp(tick, quote_received_ts)
        preferred_inst = strategy._instrument_for_side(strategy.active_side) or strategy._primary_instrument_for_market()
        is_preferred_quote = preferred_inst is None or tick.instrument_id == preferred_inst
        # Receipt time proves the subscribed transport remains alive. It is
        # deliberately independent from the exchange event timestamp below.
        getattr(strategy, "last_quote_received_ts_by_inst", {})[str(tick.instrument_id)] = quote_received_ts
        clock_skew_tolerance_sec = getattr(
            strategy,
            "quote_event_clock_skew_tolerance_sec",
            Decimal("0.25"),
        )
        is_native_book_update = quote_source in {"ws_price_change", "ws_snapshot"}
        quote_is_fresh = (
            quote_delivery_is_fresh(
                received_ts=quote_received_ts,
                adapter_emitted_ts=adapter_emitted_ts,
                max_delivery_delay_sec=float(
                    getattr(strategy, "quote_max_delivery_delay_sec", strategy.quote_stale_sec)
                ),
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
        if not quote_is_fresh:
            _record_quote_transport_telemetry(
                strategy, tick=tick, received_ts=quote_received_ts,
                event_ts=quote_event_ts, adapter_emitted_ts=adapter_emitted_ts,
                source=quote_source, quote_is_fresh=False,
                raw_ws_received_ts=provenance.get("raw_ws_received_ts"),
                data_engine_queue_depth=provenance.get("data_engine_queue_depth"),
                raw_bid_present=raw_bid_present, raw_ask_present=raw_ask_present,
                bid_size=bid_size_decimal, ask_size=ask_size_decimal,
            )
            # A cached transport heartbeat or old exchange event is not valid
            # pricing. Do not update the executable quote state, but do retain
            # the receipt timestamp above so the watchdog can distinguish an
            # idle connected socket from a dead transport.
            return

        strategy.latest_quote_depth_by_inst[str(tick.instrument_id)] = (bid_size_decimal, ask_size_decimal)
        getattr(strategy, "latest_quote_by_inst", {})[str(tick.instrument_id)] = (bid_decimal, ask_decimal)
        if quote_source_ts > 0:
            getattr(strategy, "last_quote_source_ts_by_inst", {})[str(tick.instrument_id)] = quote_source_ts
        else:
            getattr(strategy, "last_quote_source_ts_by_inst", {}).pop(str(tick.instrument_id), None)
        # Quote plans need the local time at which a current CLOB book was
        # emitted, not the book's last internal market-update timestamp.
        getattr(strategy, "last_quote_update_ts_by_inst", {})[str(tick.instrument_id)] = adapter_emitted_ts
        publish_strategy_tick(strategy, source="polymarket_bbo", price=(bid_decimal + ask_decimal) / 2, bid=bid_decimal, ask=ask_decimal, bid_size=bid_size_decimal, ask_size=ask_size_decimal)
        # A confirmed FOK candidate has a short TTL. Run its handoff before
        # optional shadow/research work and before the per-tick telemetry DB
        # write, while the fresh L2 state is already available.
        fast_follow = getattr(strategy, "outcome_fast_follow_live", None)
        if fast_follow is not None:
            stage_started = time.monotonic()
            try:
                fast_follow.on_quote(
                    instrument_id=tick.instrument_id,
                    best_bid=bid_decimal,
                    best_ask=ask_decimal,
                    bid_size=bid_size_decimal,
                    ask_size=ask_size_decimal,
                    now_ts=quote_received_ts,
                )
            except Exception as fast_follow_error:
                strategy._db_strategy_event("FAST_FOLLOW_ERROR", {
                    "slug": str(getattr(strategy, "current_market_slug", "") or ""),
                    "instrument_id": str(tick.instrument_id),
                    "error": f"{type(fast_follow_error).__name__}: {fast_follow_error}",
                })
                logger.error(f"Fast-follow handoff failed: {fast_follow_error}")
            finally:
                callback_stage_ms["fast_follow"] = (time.monotonic() - stage_started) * 1000.0
        # Research-only early-entry comparison. It is fed only a fresh quote,
        # writes to the asynchronous research DB, and has no venue authority.
        stage_started = time.monotonic()
        record_strategy_quote(
            strategy,
            instrument_id=tick.instrument_id,
            now_ts=quote_received_ts,
            bid=bid_decimal,
            ask=ask_decimal,
            bid_size=bid_size_decimal,
            ask_size=ask_size_decimal,
            quote_source_ts=adapter_emitted_ts,
        )
        callback_stage_ms["trend_shadow"] = (time.monotonic() - stage_started) * 1000.0
        # Separate prospective early-entry experiment; observation-only and
        # isolated from all order, cancel, ownership, and risk decisions.
        stage_started = time.monotonic()
        record_forward_shadow_quote(
            strategy,
            instrument_id=tick.instrument_id,
            now_ts=quote_received_ts,
            bid=bid_decimal,
            ask=ask_decimal,
            bid_size=bid_size_decimal,
            ask_size=ask_size_decimal,
            quote_source_ts=adapter_emitted_ts,
        )
        callback_stage_ms["forward_shadow"] = (time.monotonic() - stage_started) * 1000.0
        stage_started = time.monotonic()
        _record_quote_transport_telemetry(
            strategy,
            tick=tick,
            received_ts=quote_received_ts,
            event_ts=quote_event_ts,
            adapter_emitted_ts=adapter_emitted_ts,
            source=quote_source,
            quote_is_fresh=True,
            raw_ws_received_ts=provenance.get("raw_ws_received_ts"),
            data_engine_queue_depth=provenance.get("data_engine_queue_depth"),
            raw_bid_present=raw_bid_present,
            raw_ask_present=raw_ask_present,
            bid_size=bid_size_decimal,
            ask_size=ask_size_decimal,
        )
        callback_stage_ms["transport_telemetry"] = (time.monotonic() - stage_started) * 1000.0
        pending_instruments = getattr(strategy, "quote_recovery_pending_instruments", set())
        if str(tick.instrument_id) in pending_instruments:
            # A binary market needs a fresh book for every subscribed outcome.
            # Clearing the whole set after the first token hid a missing UP/DOWN
            # subscription behind transport heartbeats and prevented recovery.
            pending_instruments.discard(str(tick.instrument_id))
            if not pending_instruments:
                strategy.quote_recovery_started_ts = 0.0
                strategy.quote_recovery_attempts = 0
                record_handoff = getattr(strategy, "_db_strategy_event", None)
                if callable(record_handoff):
                    record_handoff(
                        "QUOTE_MARKET_HANDOFF_READY",
                        {
                            "slug": str(getattr(strategy, "current_market_slug", "") or ""),
                            "instruments": [str(item) for item in (strategy.current_market_instruments or [])],
                            "received_ts": quote_received_ts,
                        },
                    )
        mid_price = (bid_decimal + ask_decimal) / 2
        strategy._append_real_mid_price(tick.instrument_id, mid_price)
        stage_started = time.monotonic()
        if hasattr(strategy, "_lead_lag_observation_on_quote"):
            strategy._lead_lag_observation_on_quote(quote_received_ts)
        callback_stage_ms["lead_lag"] = (time.monotonic() - stage_started) * 1000.0
        stage_started = time.monotonic()
        if hasattr(strategy, "_shadow_simulation_on_quote"):
            strategy._shadow_simulation_on_quote(
                tick.instrument_id,
                bid_decimal,
                ask_decimal,
                quote_received_ts,
            )
        callback_stage_ms["shadow_simulation"] = (time.monotonic() - stage_started) * 1000.0
        stage_started = time.monotonic()
        if hasattr(strategy, "_depth_risk_shadow_on_quote"):
            strategy._depth_risk_shadow_on_quote(
                tick.instrument_id,
                bid_decimal,
                ask_decimal,
                quote_received_ts,
            )
        callback_stage_ms["depth_shadow"] = (time.monotonic() - stage_started) * 1000.0
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
    finally:
        callback_duration = time.monotonic() - callback_started
        if callback_duration >= 1.0 and time.monotonic() - float(getattr(strategy, "_quote_slow_stage_log_ts", 0.0)) >= 10.0:
            strategy._quote_slow_stage_log_ts = time.monotonic()
            logger.warning(
                f"Slow quote callback: elapsed_ms={callback_duration * 1000.0:.1f} "
                f"stages_ms={{{', '.join(f'{name}: {value:.1f}' for name, value in callback_stage_ms.items())}}} "
                f"instrument={getattr(tick, 'instrument_id', None)}"
            )
        try:
            _record_quote_callback_duration(
                strategy,
                callback_duration,
            )
        except Exception:
            # Diagnostics must not interfere with market-data handling.
            pass


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
    shutdown_started = time.monotonic()

    def log_shutdown_stage(stage: str, stage_started: float) -> None:
        logger.info(
            f"Strategy shutdown stage completed: stage={stage} "
            f"elapsed_sec={time.monotonic() - stage_started:.3f} "
            f"total_sec={time.monotonic() - shutdown_started:.3f}"
        )

    strategy._stopping = True
    stage_started = time.monotonic()
    logger.info("Strategy shutdown stage started: stage=outcome_observers")
    outcome_observer = getattr(strategy, "hyperliquid_outcome_observer", None)
    if outcome_observer is not None:
        try:
            outcome_observer.stop()
        except Exception:
            logger.debug("Failed to stop Hyperliquid Outcome observer", exc_info=True)
    lead_lag_runtime = getattr(strategy, "outcome_lead_lag_runtime", None)
    if lead_lag_runtime is not None:
        try:
            lead_lag_runtime.stop()
        except Exception:
            logger.debug("Failed to stop Outcome lead/lag runtime", exc_info=True)
    log_shutdown_stage("outcome_observers", stage_started)
    stage_started = time.monotonic()
    logger.info("Strategy shutdown stage started: stage=background_threads")
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
    history_collector = getattr(strategy, "btc_1s_history_collector", None)
    if history_collector is not None:
        try:
            if not history_collector.stop(timeout_sec=5.0):
                logger.warning("BTC 1s history writer shutdown incomplete or data lost")
        except Exception:
            logger.debug("Failed to stop BTC 1s history collector; live shutdown continues", exc_info=True)
    log_shutdown_stage("background_threads", stage_started)
    stage_started = time.monotonic()
    logger.info("Strategy shutdown stage started: stage=orders_and_final_journal_events")
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
    log_shutdown_stage("orders_and_final_journal_events", stage_started)
    stage_started = time.monotonic()
    seen_writers = set()
    for writer_name in ("lead_lag_db", "twap_research_db"):
        writer = getattr(strategy, writer_name, None)
        if writer is None or id(writer) in seen_writers:
            continue
        seen_writers.add(id(writer))
        try:
            if not writer.stop():
                logger.warning(f"Research writer shutdown incomplete or data lost: writer={writer_name}")
        except Exception:
            logger.warning(f"Research writer shutdown failed: writer={writer_name}", exc_info=True)
    log_shutdown_stage("research_writers", stage_started)
    stage_started = time.monotonic()
    logger.info("Strategy shutdown stage started: stage=trade_journal_final_backup")
    trade_db = getattr(strategy, "trade_db", None)
    if trade_db is not None:
        try:
            trade_db.stop()
        except Exception:
            logger.debug("Failed to flush trade journal backup during shutdown", exc_info=True)
    log_shutdown_stage("trade_journal_final_backup", stage_started)

    if strategy.terminal_dashboard:
        try:
            strategy.terminal_dashboard.stop()
        except Exception:
            pass
