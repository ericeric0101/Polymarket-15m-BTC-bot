from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any


@dataclass
class CancelAckResult:
    should_skip: bool
    cancel_reason: str


@dataclass
class RejectResult:
    rejected_side: str
    rejected_inst: Any
    reason: str
    is_taker_exit_reject: bool
    rejected_inst_key: str


def is_benign_cancel_reject_reason(reason: str) -> bool:
    """Whether an exchange cancel rejection confirms the order is terminal."""
    normalized = str(reason or "").lower()
    return any(
        marker in normalized
        for marker in (
            "already canceled",
            "already cancelled",
            "order can't be found",
            "order cannot be found",
            "matched orders can't be canceled",
        )
    )


def reconcile_cancel_ack(
    canceled_id: str,
    event: Any,
    active_maker_orders: dict[str, dict[str, Any]],
    last_cancel_ack_ts_by_client_order_id: dict[str, float],
    cancel_ack_dedupe_window_sec: float,
) -> CancelAckResult:
    now_ts = time.time()
    if canceled_id:
        last_ack_ts = float(last_cancel_ack_ts_by_client_order_id.get(canceled_id, 0.0))
        if (now_ts - last_ack_ts) < cancel_ack_dedupe_window_sec:
            return CancelAckResult(should_skip=True, cancel_reason="")
        last_cancel_ack_ts_by_client_order_id[canceled_id] = now_ts

    cancel_reason = ""
    for order_key, state in list(active_maker_orders.items()):
        order = state.get("order")
        state_coid = str(state.get("client_order_id", "") or "")
        if (order and str(order.client_order_id) == canceled_id) or (state_coid and state_coid == canceled_id):
            cancel_reason = str(state.get("cancel_reason", "") or "")
            active_maker_orders.pop(order_key, None)
            break
    return CancelAckResult(should_skip=False, cancel_reason=cancel_reason)


def reconcile_rejected_order(
    denied_id: str,
    event: Any,
    active_maker_orders: dict[str, dict[str, Any]],
    normalize_side_text_fn,
    instrument_key_fn,
) -> RejectResult:
    rejected_side = ""
    rejected_inst: Any = None
    for order_key, state in list(active_maker_orders.items()):
        order = state.get("order")
        if order and str(order.client_order_id) == denied_id:
            rejected_side = str(state.get("side", "") or "")
            rejected_inst = state.get("instrument_id")
            active_maker_orders.pop(order_key, None)
            break
    if not rejected_side:
        rejected_side = normalize_side_text_fn(getattr(event, "order_side", ""))
    if rejected_inst is None:
        rejected_inst = getattr(event, "instrument_id", None)
    if not rejected_side and denied_id.startswith("BTC-15M-TAKER-EXIT-"):
        rejected_side = "sell"
    is_taker_exit_reject = denied_id.startswith("BTC-15M-TAKER-EXIT-")
    rejected_inst_key = instrument_key_fn(rejected_inst) if rejected_inst is not None else ""
    return RejectResult(
        rejected_side=rejected_side,
        rejected_inst=rejected_inst,
        reason=str(getattr(event, "reason", "") or ""),
        is_taker_exit_reject=is_taker_exit_reject,
        rejected_inst_key=rejected_inst_key,
    )


def reconcile_benign_cancel_reject(
    rejected_id: str,
    active_maker_orders: dict[str, dict[str, Any]],
) -> bool:
    for order_key, state in list(active_maker_orders.items()):
        order = state.get("order")
        state_coid = str(state.get("client_order_id", "") or "")
        if (order and str(order.client_order_id) == rejected_id) or (state_coid and state_coid == rejected_id):
            active_maker_orders.pop(order_key, None)
            return True
    return False
