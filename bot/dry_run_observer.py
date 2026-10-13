"""DRY-RUN -> E5B observer bridge (research telemetry only; no order authority).

In DRY-RUN the maker BUY never reaches ``hook_entry_shadow_buy_submit`` (the paper-order branch
of ``submit_maker_quote`` returns first), so the frozen C/B/D tracker had no attempts.  This
module turns the DRY-RUN journal events that pass the ``_db_order_event`` choke point into the
same normalized submit / order-state events LIVE produces, and feeds them to the **same frozen
tracker methods** (``EntryShadowTracker.on_buy_submit / on_cancel_ack / on_fill``).  No C/B/D
logic is copied; ``bot.entry_shadow`` and ``bot.entry_shadow_runtime`` (frozen E5B v1 files) are
not modified.

It reads its arguments and cached quotes only.  It never submits, cancels or replaces an order,
never touches the paper simulation, positions or the account, and ignores every LIVE event.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import asdict, dataclass, fields
from typing import Any, Optional

from bot.dry_run_plumbing import EXECUTION_MODE_DRY_RUN, evidence_tier, execution_mode, execution_tags
from bot.entry_shadow import EdgeCalibration, EntryShadowTracker
from bot.entry_shadow_runtime import _read_sample, _tracker

DRY_RUN_OBSERVED_EVENTS = frozenset({"ORDER_DRY_RUN_SUBMITTED", "ORDER_DRY_RUN_CANCELLED", "SHADOW_SIM_ENTRY_FILLED"})


def build_entry_shadow(strategy: Any, *, d: bool, c: bool, b: bool) -> Optional[EntryShadowTracker]:
    """``entry_shadow_runtime.build_entry_shadow`` plus population tags and per-observer counters."""
    if not (d or c or b):
        return None
    db = getattr(strategy, "trade_db", None)
    run_id = str(getattr(strategy, "run_id", "") or "")
    counts: Counter = Counter()
    emit = None
    if db is not None and hasattr(db, "enqueue_strategy_event"):
        def emit(event_type: str, payload: dict) -> Any:
            # Every observer record names its population (DRY_RUN/SIMULATED_ENV vs LIVE/LIVE_VENUE).
            counts[event_type] += 1
            return db.enqueue_strategy_event(run_id, event_type, {**payload, **execution_tags(strategy)})
    tracker = EntryShadowTracker(emit=emit, calibration=EdgeCalibration.load(), d_enabled=d, c_enabled=c,
                                 b_enabled=b)
    # Evaluation records written per observer (every evaluation, not only triggers).
    tracker.observer_counts = counts
    return tracker


# ---------------------------------------------------------------- normalized order events
@dataclass(frozen=True)
class NormalizedOrderEvent:
    """One submit / order-state event with the same structure for LIVE and DRY-RUN.

    A field a mode cannot provide is None with a reason in ``missing_reasons`` (never a fake value).
    """

    event_kind: str                 # SUBMIT | CANCEL | FILL
    client_order_id: str
    market: str
    instrument: str
    side: str
    maker_price: Any
    qty: Any
    event_ts: float
    execution_mode: str
    evidence_tier: Optional[str]
    venue_order_id: Optional[str]
    request_to_ack_sec: Optional[float]
    missing_reasons: tuple = ()

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["missing_reasons"] = dict(self.missing_reasons)
        return out


NORMALIZED_EVENT_FIELDS = tuple(f.name for f in fields(NormalizedOrderEvent))


def _side_text(strategy: Any, instrument_id: Any) -> str:
    side_fn = getattr(strategy, "_side_for_instrument_id", None)
    return str(getattr(side_fn(instrument_id), "value", "NONE")) if callable(side_fn) else "NONE"


def _submit_event(strategy: Any, *, client_order_id: str, instrument_id: Any, limit_price: Any, qty: Any, now: float,
                  venue_order_id: Optional[str], missing: dict[str, str]) -> NormalizedOrderEvent:
    mode = execution_mode(strategy)
    return NormalizedOrderEvent(
        event_kind="SUBMIT", client_order_id=str(client_order_id or ""),
        market=str(getattr(strategy, "current_market_slug", "") or ""),
        instrument=str(strategy._instrument_key(instrument_id)), side=_side_text(strategy, instrument_id),
        maker_price=limit_price, qty=qty, event_ts=float(now), execution_mode=mode, evidence_tier=evidence_tier(mode),
        venue_order_id=venue_order_id, request_to_ack_sec=None, missing_reasons=tuple(sorted(missing.items())))


def normalize_live_submit(strategy: Any, *, order: Any, instrument_id: Any, limit_price: Any, qty: Any,
                          now: float) -> NormalizedOrderEvent:
    """What ``entry_shadow_runtime.hook_buy_submit`` passes to the tracker, as a normalized event."""
    venue = getattr(order, "venue_order_id", None)
    missing = {"request_to_ack_sec": "NOT_AN_ACK_EVENT"}
    if venue is None:
        missing["venue_order_id"] = "VENUE_ID_NOT_YET_ASSIGNED"
    return _submit_event(strategy, client_order_id=str(getattr(order, "client_order_id", "") or ""),
                         instrument_id=instrument_id, limit_price=limit_price, qty=qty, now=now,
                         venue_order_id=str(venue) if venue is not None else None, missing=missing)


def normalize_dry_run_submit(strategy: Any, *, client_order_id: str, instrument_id: Any, limit_price: Any, qty: Any,
                             now: float) -> NormalizedOrderEvent:
    missing = {"venue_order_id": "DRY_RUN_NO_VENUE_ORDER", "request_to_ack_sec": "DRY_RUN_NO_VENUE_ACK"}
    return _submit_event(strategy, client_order_id=client_order_id, instrument_id=instrument_id,
                         limit_price=limit_price, qty=qty, now=now, venue_order_id=None, missing=missing)


def dispatch_submit(strategy: Any, tracker: EntryShadowTracker, ev: NormalizedOrderEvent) -> None:
    """A normalized submit into the frozen tracker, exactly as the LIVE hook calls it."""
    tracker.on_buy_submit(coid=ev.client_order_id, market=ev.market, instrument=ev.instrument, side=ev.side,
                          maker_price=ev.maker_price, qty=ev.qty, place_ts=ev.event_ts,
                          sample=_read_sample(strategy, ev.instrument, ev.event_ts))


def observe_dry_run_order_event(strategy: Any, *, event_type: str, client_order_id: Optional[str], side: Any,
                                price: Any, qty: Any, reason: Any, payload: Optional[dict]) -> None:
    """DRY-RUN only: the paper maker lifecycle reaches the frozen tracker (submit / cancel / fill)."""
    if event_type not in DRY_RUN_OBSERVED_EVENTS:
        return
    tracker = _tracker(strategy)
    if tracker is None or execution_mode(strategy) != EXECUTION_MODE_DRY_RUN:
        return
    try:
        payload = payload or {}
        now = time.time()
        if event_type == "ORDER_DRY_RUN_SUBMITTED":
            if str(side or "").upper() != "BUY" or not client_order_id:
                return
            inst = payload.get("instrument_id") or getattr(strategy, "instrument_id", None)
            dispatch_submit(strategy, tracker, normalize_dry_run_submit(
                strategy, client_order_id=client_order_id, instrument_id=inst, limit_price=price, qty=qty, now=now))
        elif event_type == "ORDER_DRY_RUN_CANCELLED":
            if client_order_id:
                # Local paper cancel: there is no venue ACK, so no request->ack latency.
                tracker.on_cancel_ack(coid=str(client_order_id), ack_ts=now, request_to_ack_sec=None,
                                      reason=str(reason) if reason is not None else None)
        else:  # SHADOW_SIM_ENTRY_FILLED: the paper order names its DRY-RUN order (never matched by time)
            coid = payload.get("dry_run_client_order_id")
            if not coid:
                tracker.counters["dry_run_fill_unmapped"] = tracker.counters.get("dry_run_fill_unmapped", 0) + 1
                return
            fill_ts = payload.get("filled_ts")
            tracker.on_fill(coid=str(coid), fill_ts=float(fill_ts) if fill_ts is not None else now, qty=qty)
    except Exception:
        tracker.counters["exceptions"] += 1
