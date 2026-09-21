"""Tiny non-blocking ingress helpers used by market-data threads."""
from __future__ import annotations

import time
from decimal import Decimal

from bot.outcome_lead_lag_types import ReferenceTick


def publish_strategy_tick(strategy, *, source: str, price, source_event_ts_ms: int | None = None, bid=None, ask=None, bid_size=None, ask_size=None, connection_epoch: int = 0) -> None:
    runtime = getattr(strategy, "outcome_lead_lag_runtime", None)
    if runtime is None:
        return
    try:
        price_cents = int((Decimal(str(price)) * 100).to_integral_value())
        if price_cents <= 0:
            return
        cents = lambda value: int((Decimal(str(value)) * 100).to_integral_value()) if value is not None else None
        scale = lambda value: int((Decimal(str(value)) * 1_000_000).to_integral_value()) if value is not None else None
        observer = getattr(strategy, "hyperliquid_outcome_observer", None)
        runtime.publish(ReferenceTick(
            source=source, price_cents=price_cents, received_epoch_ns=time.time_ns(), received_monotonic_ns=time.perf_counter_ns(),
            source_event_ts_ms=source_event_ts_ms, run_id=str(getattr(strategy, "run_id", "")),
            slug=str(getattr(strategy, "current_market_slug", "") or ""),
            market_id=(getattr(observer, "market_id", None) if source == "outcome_btc_mark" else None),
            connection_epoch=int(connection_epoch),
            bid_cents=cents(bid), ask_cents=cents(ask), bid_size_e6=scale(bid_size), ask_size_e6=scale(ask_size),
        ))
    except (ArithmeticError, TypeError, ValueError):
        return


def record_hyperliquid_btc_probe(
    strategy, *, source: str, price, source_event_ts_ms: int | None = None,
    bid=None, ask=None, connection_epoch: int = 0,
) -> None:
    """Persist high-frequency Hyperliquid BTC observations for research only."""
    db = getattr(strategy, "lead_lag_db", None)
    if db is None:
        return
    try:
        price_cents = int((Decimal(str(price)) * 100).to_integral_value())
        if price_cents <= 0:
            return
        received_epoch_ns = time.time_ns()
        db.enqueue_reference_1s(
            run_id=str(getattr(strategy, "run_id", "")),
            slug=str(getattr(strategy, "current_market_slug", "") or ""),
            market_id=None,
            bucket_epoch_ms=(received_epoch_ns // 1_000_000 // 1_000) * 1_000,
            source=str(source), price_cents=price_cents, received_epoch_ns=received_epoch_ns,
        )
    except (ArithmeticError, TypeError, ValueError):
        return
