"""Read-only adapter between the strategy and the E5B shadow tracker.

Every hook is a guarded no-op unless ``strategy.entry_shadow`` exists (it is only
constructed when a shadow flag is on).  The adapter reads cached market state and
forwards it, together with a non-blocking telemetry sink, to
``bot.entry_shadow.EntryShadowTracker``.  It never passes the strategy, an order,
or any submit/cancel callable to the tracker, and it never calls one itself.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Optional

from bot.entry_shadow import EdgeCalibration, EntryShadowTracker, QuoteSample
from bot.settlement_side import canonical_twap_side_state

L2_LEVELS = 10
L2_FRESH_SEC = 2.0


def entry_shadow_summary(d: bool, c: bool, b: bool) -> str:
    return ("Entry shadow (E5B, telemetry only): "
            f"PRICE_ADJUSTED_EDGE_SHADOW_ENABLED={int(bool(d))} EARLY_CANCEL_SHADOW_ENABLED={int(bool(c))} "
            f"MAKER_CHASE_SHADOW_ENABLED={int(bool(b))} (no order authority)")


def build_entry_shadow(strategy: Any, *, d: bool, c: bool, b: bool) -> Optional[EntryShadowTracker]:
    if not (d or c or b):
        return None
    db = getattr(strategy, "trade_db", None)
    run_id = str(getattr(strategy, "run_id", "") or "")
    emit = None
    if db is not None and hasattr(db, "enqueue_strategy_event"):
        emit = lambda event_type, payload: db.enqueue_strategy_event(run_id, event_type, payload)  # noqa: E731
    return EntryShadowTracker(emit=emit, calibration=EdgeCalibration.load(), d_enabled=d, c_enabled=c, b_enabled=b)


def _read_sample(strategy: Any, inst_key: str, now: float) -> Optional[QuoteSample]:
    """Read-only snapshot of cached state for one instrument. Never raises."""
    try:
        quote = (getattr(strategy, "latest_quote_by_inst", None) or {}).get(inst_key)
        rts = (getattr(strategy, "last_quote_update_ts_by_inst", None) or {}).get(inst_key)
        depth = (getattr(strategy, "latest_quote_depth_by_inst", None) or {}).get(inst_key)
        bid, ask = (quote if quote else (None, None))
        slug = str(getattr(strategy, "current_market_slug", "") or "")
        end_ts = getattr(strategy, "current_market_end_timestamp", None)
        strike = (getattr(strategy, "market_strike_cache_by_slug", None) or {}).get(slug)
        verified = None
        verifier = getattr(strategy, "_market_strike_is_entry_eligible", None)
        if callable(verifier):
            try:
                verified = bool(verifier(slug))
            except Exception:
                verified = None
        side = None
        side_fn = getattr(strategy, "_side_for_instrument_id", None)
        norm = getattr(strategy, "_normalize_instrument_id", None)
        if callable(side_fn):
            try:
                inst = norm(inst_key) if callable(norm) else inst_key
                side = str(getattr(side_fn(inst), "value", side_fn(inst))).upper()
            except Exception:
                side = None
        twap_state = twap_value = distance = None
        shadow = getattr(strategy, "twap_forward_shadow", None)
        if shadow is not None and side in ("UP", "DOWN"):
            state = canonical_twap_side_state(
                held_side=side, twap_features=shadow.latest(slug), strike=strike,
                strike_verified=bool(verified), expected_slug=slug, now_ts=now)
            twap_state, twap_value, distance = state["state"], state["twap"], state["signed_distance_usd"]
        cl_ts = getattr(strategy, "_polymarket_chainlink_price_ts", None)
        l2_ts = (getattr(strategy, "l2_update_ts_by_inst", None) or {}).get(inst_key)
        l2_age = (now - float(l2_ts)) if l2_ts else None
        levels = None
        if l2_age is not None and -1.0 <= l2_age <= L2_FRESH_SEC:
            cache = getattr(strategy, "cache", None)
            book_fn = getattr(cache, "order_book", None)
            levels_fn = getattr(strategy, "_levels_from_native_order_book", None)
            if callable(book_fn) and callable(levels_fn):
                try:
                    book = book_fn(norm(inst_key) if callable(norm) else inst_key)
                    if book is not None:
                        levels = tuple(levels_fn(book, "asks", L2_LEVELS))
                except Exception:
                    levels = None
        score = getattr(strategy, "side_decision_score", None)
        return QuoteSample(
            sample_ts=now, quote_receipt_ts=float(rts) if rts else None,
            best_bid=Decimal(str(bid)) if bid is not None else None,
            best_ask=Decimal(str(ask)) if ask is not None else None,
            top_bid_size=Decimal(str(depth[0])) if depth else None,
            top_ask_size=Decimal(str(depth[1])) if depth else None,
            side_score=float(score) if score is not None else None,
            tte_sec=(float(end_ts) - now) if end_ts else None,
            strike=float(strike) if strike is not None else None, strike_verified=verified,
            binance_spot=float(getattr(strategy, "_binance_ws_price")) if getattr(strategy, "_binance_ws_price", None) is not None else None,
            chainlink_spot=float(getattr(strategy, "_polymarket_chainlink_price")) if getattr(strategy, "_polymarket_chainlink_price", None) is not None else None,
            chainlink_age_sec=(now - float(cl_ts)) if cl_ts else None,
            twap_value=twap_value, twap_state=twap_state, signed_distance_usd=distance,
            l2_age_sec=l2_age, ask_levels=levels,
        )
    except Exception:
        return None


# ---------------------------------------------------------------- guarded hooks
def _tracker(strategy: Any) -> Optional[EntryShadowTracker]:
    tracker = getattr(strategy, "entry_shadow", None)
    return tracker if isinstance(tracker, EntryShadowTracker) and tracker.enabled else None


def hook_buy_submit(strategy: Any, *, order: Any, instrument_id: Any, limit_price: Any, qty: Any) -> None:
    tracker = _tracker(strategy)
    if tracker is None:
        return
    try:
        now = time.time()
        inst_key = str(strategy._instrument_key(instrument_id))
        side_fn = getattr(strategy, "_side_for_instrument_id", None)
        side = str(getattr(side_fn(instrument_id), "value", "NONE")) if callable(side_fn) else "NONE"
        tracker.on_buy_submit(
            coid=str(getattr(order, "client_order_id", "") or ""),
            market=str(getattr(strategy, "current_market_slug", "") or ""), instrument=inst_key, side=side,
            maker_price=limit_price, qty=qty, place_ts=now, sample=_read_sample(strategy, inst_key, now))
    except Exception:
        tracker.counters["exceptions"] += 1


def hook_reject(strategy: Any, coid: str) -> None:
    tracker = _tracker(strategy)
    if tracker is None:
        return
    try:
        tracker.on_reject(coid=str(coid))
    except Exception:
        tracker.counters["exceptions"] += 1


def hook_fill(strategy: Any, coid: str, qty: Any) -> None:
    tracker = _tracker(strategy)
    if tracker is None:
        return
    try:
        started = (getattr(strategy, "_cancel_started_ns_by_order_id", None) or {}).get(str(coid))
        tracker.on_fill(coid=str(coid), fill_ts=time.time(), qty=qty, cancel_pending=started is not None,
                        cancel_request_age_sec=((time.perf_counter_ns() - int(started)) / 1e9) if started else None)
    except Exception:
        tracker.counters["exceptions"] += 1


def hook_cancel_ack(strategy: Any, coid: str, cancel_started_ns: Any, reason: Any) -> None:
    tracker = _tracker(strategy)
    if tracker is None:
        return
    try:
        tracker.on_cancel_ack(
            coid=str(coid), ack_ts=time.time(),
            request_to_ack_sec=((time.perf_counter_ns() - int(cancel_started_ns)) / 1e9) if cancel_started_ns else None,
            reason=str(reason) if reason is not None else None)
    except Exception:
        tracker.counters["exceptions"] += 1


def hook_tick(strategy: Any) -> None:
    tracker = _tracker(strategy)
    if tracker is None or not tracker.attempts:
        return
    try:
        now = time.time()
        tracker.on_tick(now, lambda inst_key: _read_sample(strategy, inst_key, now))
    except Exception:
        tracker.counters["exceptions"] += 1
