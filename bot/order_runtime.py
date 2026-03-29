from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, List, Optional, Tuple

from loguru import logger
from nautilus_trader.model.identifiers import InstrumentId


class OrderRuntimeMixin:
    """
    Active maker order coordination and cancel/reconcile helpers.

    This is live-path runtime behavior, but it is infrastructure around quoting,
    not quote signal generation itself.
    """

    @staticmethod
    def _order_key_for(side: str, instrument_id: Any) -> str:
        return f"{side}:{instrument_id}"

    def _active_order_keys(self, side: Optional[str] = None, instrument_id: Optional[Any] = None) -> List[str]:
        keys: List[str] = []
        target_inst = str(instrument_id) if instrument_id is not None else None
        for key, state in self.active_maker_orders.items():
            state_side = str(state.get("side", "") or "")
            state_inst = str(state.get("instrument_id", "") or "")
            if side is not None and state_side != side:
                continue
            if target_inst is not None and state_inst != target_inst:
                continue
            keys.append(key)
        return keys

    def _get_quote_for_instrument(self, instrument_id: Any) -> Optional[Tuple[Decimal, Decimal]]:
        inst = self._normalize_instrument_id(instrument_id)
        if inst is None:
            return None
        quote = self.cache.quote_tick(inst)
        if quote is None:
            return None
        bid_decimal = quote.bid_price.as_decimal() if quote.bid_price is not None else None
        ask_decimal = quote.ask_price.as_decimal() if quote.ask_price is not None else None
        if bid_decimal is None and ask_decimal is not None:
            bid_decimal = max(Decimal("0.01"), ask_decimal - Decimal("0.01"))
        if ask_decimal is None and bid_decimal is not None:
            ask_decimal = min(Decimal("0.99"), bid_decimal + Decimal("0.01"))
        if bid_decimal is None or ask_decimal is None:
            return None
        if bid_decimal > ask_decimal:
            mid_tmp = (bid_decimal + ask_decimal) / 2
            bid_decimal = max(Decimal("0.01"), mid_tmp - Decimal("0.005"))
            ask_decimal = min(Decimal("0.99"), mid_tmp + Decimal("0.005"))
        return bid_decimal, ask_decimal

    def _activate_maker_kill_switch(self, reason: str) -> None:
        self.maker_kill_switch = True
        self._cancel_active_maker_orders()
        logger.error(f"MAKER KILL SWITCH ACTIVATED: {reason}")

    def _cancel_active_maker_orders(self) -> None:
        for order_key in list(self.active_maker_orders.keys()):
            self._cancel_maker_order_side(order_key, reason="risk")

    def _cancel_maker_order_side(self, side: str, reason: str = "risk", instrument_id: Optional[Any] = None) -> None:
        target_keys: List[str] = []
        if side in self.active_maker_orders and instrument_id is None:
            target_keys = [side]
        else:
            target_keys = self._active_order_keys(side=side, instrument_id=instrument_id)
        for order_key in target_keys:
            self._cancel_maker_order_key(order_key, reason=reason)

    def _cancel_maker_order_key(self, order_key: str, reason: str = "risk") -> None:
        state = self.active_maker_orders.get(order_key)
        if not state:
            return
        side = str(state.get("side", "") or "")
        order = state.get("order")
        now_ts = time.time()
        if state.get("pending_cancel"):
            last_cancel_ts = float(state.get("last_cancel_ts", 0.0))
            if last_cancel_ts > 0 and (now_ts - last_cancel_ts) < self.maker_cancel_cooldown_sec:
                logger.debug(f"Skip duplicate cancel [{side}] within cooldown")
                return
        if order is None:
            state["pending_cancel"] = True
            state["last_cancel_ts"] = now_ts
            state["cancel_reason"] = reason
            return
        try:
            status_text = str(getattr(order, "status", "")).upper()
            if any(flag in status_text for flag in ("REJECTED", "FILLED", "CANCELED", "CANCELLED")):
                logger.debug(f"Skip cancel [{side}] because order state is terminal: {status_text}")
                state["pending_cancel"] = True
                state["last_cancel_ts"] = now_ts
                state["cancel_reason"] = reason
            else:
                self.cancel_order(order)
                state["pending_cancel"] = True
                state["last_cancel_ts"] = now_ts
                state["cancel_retries"] = int(state.get("cancel_retries", 0))
                state["cancel_reason"] = reason
                logger.info(f"Cancelled maker order [{side}] {order.client_order_id}")
        except Exception as e:
            logger.debug(f"Failed to cancel maker order [{side}]: {e}")
        self.rebate_reporter.record_cancel(reason)

    def _is_order_ttl_expired(self, order_key: str, now_ts: float) -> bool:
        state = self.active_maker_orders.get(order_key)
        if not state:
            return False
        created_ts = float(state.get("created_ts", 0.0))
        if created_ts <= 0:
            return True
        return (now_ts - created_ts) >= self.maker_order_ttl_sec

    def _cleanup_stale_pending_cancels(self, now_ts: float) -> None:
        for order_key, state in list(self.active_maker_orders.items()):
            side = str(state.get("side", "") or "")
            if not state.get("pending_cancel"):
                continue
            last_cancel_ts = float(state.get("last_cancel_ts", 0.0))
            if last_cancel_ts <= 0:
                continue
            if (now_ts - last_cancel_ts) >= self.maker_cancel_ack_timeout_sec:
                order = state.get("order")
                coid = str(order.client_order_id) if order else "unknown"
                is_open = self._is_order_still_open_in_cache(coid)
                retries = int(state.get("cancel_retries", 0))
                if is_open is False:
                    logger.info(f"Cancel reconciled for [{side}] {coid}; removing local pending-cancel state.")
                    self._db_order_event(
                        event_type="ORDER_CANCEL_RECONCILED",
                        client_order_id=coid,
                        side=side.upper(),
                        status="CANCELED_RECONCILED",
                    )
                    self.active_maker_orders.pop(order_key, None)
                    continue

                if is_open is None:
                    unknown_retries = int(state.get("reconcile_unknown_retries", 0)) + 1
                    state["reconcile_unknown_retries"] = unknown_retries
                    if unknown_retries > self.maker_cancel_max_retries * 2:
                        logger.error(f"Cancel reconcile unknown for [{side}] {coid} exceeded max retries. Triggering Maker Kill Switch.")
                        self._db_order_event(
                            event_type="ORDER_CANCEL_RECONCILE_UNKNOWN_KILL",
                            client_order_id=coid,
                            side=side.upper(),
                            status="KILL_SWITCH_UNKNOWN",
                            reason="max_unknown_retries",
                        )
                        self._activate_maker_kill_switch(f"Order {coid} state unknown after {unknown_retries} retries")
                        continue

                    state["last_cancel_ts"] = now_ts
                    pause_sec = max(1, min(self.maker_error_pause_sec, self.maker_cancel_ack_timeout_sec))
                    self.quote_pause_until_ts = max(self.quote_pause_until_ts, now_ts + pause_sec)
                    logger.warning(
                        f"Cancel reconcile unknown for [{side}] {coid}; "
                        f"keeping pending-cancel state and retrying later "
                        f"(unknown_count={unknown_retries}, pause={pause_sec}s)."
                    )
                    self._db_order_event(
                        event_type="ORDER_CANCEL_RECONCILE_UNKNOWN",
                        client_order_id=coid,
                        side=side.upper(),
                        status="PENDING_CANCEL_UNKNOWN",
                        reason="cache_unknown",
                        payload={"unknown_count": unknown_retries},
                    )
                    continue

                if retries < self.maker_cancel_max_retries and order is not None:
                    try:
                        self.cancel_order(order)
                        state["last_cancel_ts"] = now_ts
                        state["cancel_retries"] = retries + 1
                        logger.warning(
                            f"Pending-cancel timeout for [{side}] {coid}; "
                            f"reconcile suggests still open (or unknown), retry cancel "
                            f"{state['cancel_retries']}/{self.maker_cancel_max_retries}."
                        )
                        self._db_order_event(
                            event_type="ORDER_CANCEL_RETRY",
                            client_order_id=coid,
                            side=side.upper(),
                            status="PENDING_CANCEL_RETRY",
                            reason=f"timeout_reconcile_open={is_open}",
                            payload={"retry": state["cancel_retries"]},
                        )
                    except Exception as e:
                        logger.warning(f"Cancel retry failed for [{side}] {coid}: {e}")
                    continue

                logger.error(
                    f"Cancel reconciliation failed for [{side}] {coid} after "
                    f"{retries} retries; activating maker kill switch."
                )
                self._db_order_event(
                    event_type="ORDER_CANCEL_RECONCILE_FAILED",
                    client_order_id=coid,
                    side=side.upper(),
                    status="PENDING_CANCEL_GIVE_UP",
                    reason=f"open_after_retries={retries}",
                )
                self._activate_maker_kill_switch(
                    f"Cancel reconcile failed for {coid} after {retries} retries"
                )

    def _is_order_still_open_in_cache(self, client_order_id: str) -> Optional[bool]:
        try:
            open_orders = []
            if hasattr(self.cache, "orders_open"):
                oo = self.cache.orders_open()
                if oo:
                    open_orders.extend(list(oo))
            elif hasattr(self.cache, "orders"):
                oo = self.cache.orders()
                if oo:
                    open_orders.extend(list(oo))

            if len(open_orders) == 0:
                return None

            target = str(client_order_id)
            for o in open_orders:
                coid = str(getattr(o, "client_order_id", "") or "")
                if coid != target:
                    continue
                status_text = str(getattr(o, "status", "")).upper()
                if any(flag in status_text for flag in ("FILLED", "REJECTED", "CANCELED", "CANCELLED", "EXPIRED")):
                    return False
                return True
            return False
        except Exception as e:
            logger.debug(f"Open-order cache reconciliation failed for {client_order_id}: {e}")
            return None
