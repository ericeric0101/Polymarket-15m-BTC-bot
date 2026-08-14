"""Structured, non-invasive audit payloads for protective exits."""
from __future__ import annotations

from decimal import Decimal
from typing import Any


def build_invalidation_exit_audit(
    *,
    slug: str,
    instrument_id: str,
    time_left_sec: float | None,
    best_bid: Decimal,
    best_ask: Decimal,
    qty: Decimal,
    sellable_qty: Decimal,
    avg_entry: Decimal,
    hold_sec: float,
    locked_side_invalidated: bool,
    twap_confirms_adverse: bool,
    twap_fresh: bool,
    recovery_candidate: bool,
    recovery_ratio: Decimal | None,
    min_recovery_ratio: Decimal,
    min_hold_sec: float,
    max_time_left_sec: float,
    min_bid: Decimal,
    disable_if_bid_below: Decimal,
    pending_exit: bool,
    requested_tif: str | None = None,
    outcome: str | None = None,
    block_reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return JSON-safe evidence for one invalidation-exit decision.

    This deliberately records the existing policy rather than deciding it.  It
    is used to distinguish a deliberate hold from a venue/execution failure.
    """
    gross_recovery = best_bid * sellable_qty
    cost_basis = avg_entry * sellable_qty
    payload: dict[str, Any] = {
        "slug": slug,
        "instrument_id": instrument_id,
        "time_left_sec": time_left_sec,
        "best_bid": float(best_bid),
        "best_ask": float(best_ask),
        "qty": float(qty),
        "sellable_qty": float(sellable_qty),
        "avg_entry": float(avg_entry),
        "hold_sec": hold_sec,
        "locked_side_invalidated": locked_side_invalidated,
        "twap_confirms_adverse": twap_confirms_adverse,
        "twap_fresh": twap_fresh,
        "recovery_candidate": recovery_candidate,
        "recovery_ratio": float(recovery_ratio) if recovery_ratio is not None else None,
        "min_recovery_ratio": float(min_recovery_ratio),
        "gross_recovery": float(gross_recovery),
        "cost_basis": float(cost_basis),
        "min_hold_sec": min_hold_sec,
        "max_time_left_sec": max_time_left_sec,
        "min_bid": float(min_bid),
        "disable_if_bid_below": float(disable_if_bid_below),
        "pending_exit": pending_exit,
        "requested_tif": requested_tif,
        "outcome": outcome,
        "block_reason": block_reason,
    }
    if extra:
        payload.update(extra)
    return payload
