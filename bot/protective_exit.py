"""Protective-exit availability for held inventory, independent of entry pauses.

``_run_protective_exit_cycle`` is the single canonical entry point to the
existing exit engine (``TakerExitMixin._maybe_taker_exit_positions`` and the
urgent-exit path).  It runs whether or not NEW BUY authority is paused, so a
Telegram pause or an error pause can no longer suppress the hard breakers.

It never invents data:
* an instrument whose order book has no fresh quote within ``QUOTE_STALE_SEC``
  is not evaluated (no stop from a stale price) and is reported as
  ``PROTECTIVE_EXIT_DEGRADED``;
* an instrument whose existing SELL order is in an unknown venue state is not
  sold again (inventory reservation unreliable, error-pause class ii);
* the maker kill switch keeps its existing "no order activity" semantics; held
  inventory is reported as degraded and the exit policy under kill switch is
  an open decision (UNRESOLVED);
* a position below the venue SELL minimum is reported once as
  ``PROTECTIVE_EXIT_UNSELLABLE`` and never retried.

Every real protective SELL still goes through ``ExecutionSafetyMixin``.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict

from loguru import logger

DEGRADED_EVENT = "PROTECTIVE_EXIT_DEGRADED"
UNSELLABLE_EVENT = "PROTECTIVE_EXIT_UNSELLABLE"

REASON_KILL_SWITCH = "kill_switch_active_exit_policy_unresolved"
REASON_ORDERBOOK_STALE = "orderbook_stale"
REASON_INVENTORY_UNRELIABLE = "inventory_unreliable_sell_order_state_unknown"
REASON_REFERENCE_STALE = "reference_feed_stale_partial_trend_inputs"

# Same freshness bound the existing TWAP stop vote and endgame exit use.
REFERENCE_FRESH_SEC = 5.0


class ProtectiveExitMixin:
    def _held_protective_positions(self) -> Dict[str, Dict[str, Any]]:
        held: Dict[str, Dict[str, Any]] = {}
        for inst_key, state in list((getattr(self, "live_inventory_cost", None) or {}).items()):
            try:
                qty = Decimal(str((state or {}).get("qty", "0")))
            except Exception:
                continue
            if qty > 0:
                held[str(inst_key)] = state
        return held

    def _record_protective_exit_degraded(self, inst_key: str, reason: str, now_ts: float, **extra: Any) -> bool:
        """One log line + journal event per (instrument, reason) per interval."""
        last_by_key = getattr(self, "_protective_exit_degraded_ts_by_key", None)
        if not isinstance(last_by_key, dict):
            last_by_key = {}
            self._protective_exit_degraded_ts_by_key = last_by_key
        key = f"{inst_key}:{reason}"
        interval = float(getattr(self, "taker_exit_skip_log_interval_sec", 20) or 20)
        if key in last_by_key and now_ts - float(last_by_key[key]) < interval:
            return False
        last_by_key[key] = now_ts
        payload = {
            "reason": reason,
            "instrument_id": inst_key,
            "slug": str(getattr(self, "current_market_slug", "") or ""),
            **extra,
        }
        logger.warning(f"{DEGRADED_EVENT}: inst={inst_key} reason={reason} details={extra}")
        record = getattr(self, "_db_strategy_event", None)
        if callable(record):
            record(DEGRADED_EVENT, payload)
        return True

    def _protective_inventory_unreliable(self, inst_key: str) -> bool:
        """Error-pause class ii: an existing SELL on this instrument has unknown venue state."""
        for state in list((getattr(self, "active_maker_orders", None) or {}).values()):
            if str(state.get("side", "") or "").lower() != "sell":
                continue
            if str(state.get("instrument_id", "") or "") != inst_key:
                continue
            if int(state.get("reconcile_unknown_retries", 0) or 0) > 0:
                return True
        return False

    def _protective_orderbook_is_fresh(self, inst_key: str, now_ts: float) -> bool:
        fresh_ts = (getattr(self, "last_quote_update_ts_by_inst", None) or {}).get(inst_key)
        if fresh_ts is None:
            return False
        try:
            age = now_ts - float(fresh_ts)
        except (TypeError, ValueError):
            return False
        return age <= float(getattr(self, "quote_stale_sec", 30))

    def _protective_exit_blocked_instruments(self, now_ts: float) -> Dict[str, str]:
        """Instruments that must not be evaluated/sold now, with a degraded reason."""
        blocked: Dict[str, str] = {}
        held = self._held_protective_positions()
        if not held:
            return blocked
        exchange_min = Decimal(str(getattr(self, "maker_exchange_min_shares", "5")))
        unsellable = getattr(self, "_protective_exit_unsellable_positions", None)
        if not isinstance(unsellable, set):
            unsellable = set()
            self._protective_exit_unsellable_positions = unsellable
        for inst_key, state in held.items():
            qty = Decimal(str(state.get("qty", "0")))
            if qty + Decimal("0.000001") < exchange_min:
                position_key = (inst_key, float(state.get("opened_ts", 0.0) or 0.0))
                if position_key not in unsellable:
                    unsellable.add(position_key)
                    logger.warning(
                        f"{UNSELLABLE_EVENT}: inst={inst_key} qty={float(qty):.6f} < venue min "
                        f"{float(exchange_min):.2f}; held to settlement, no SELL retries"
                    )
                    record = getattr(self, "_db_strategy_event", None)
                    if callable(record):
                        record(UNSELLABLE_EVENT, {
                            "instrument_id": inst_key, "qty": float(qty),
                            "venue_min_sell_shares": float(exchange_min),
                            "opened_ts": position_key[1],
                            "slug": str(getattr(self, "current_market_slug", "") or ""),
                        })
                blocked[inst_key] = "unsellable_below_venue_minimum"
                continue
            if self._protective_inventory_unreliable(inst_key):
                blocked[inst_key] = REASON_INVENTORY_UNRELIABLE
                self._record_protective_exit_degraded(inst_key, REASON_INVENTORY_UNRELIABLE, now_ts,
                                                      held_qty=float(qty))
                continue
            if not self._protective_orderbook_is_fresh(inst_key, now_ts):
                blocked[inst_key] = REASON_ORDERBOOK_STALE
                self._record_protective_exit_degraded(
                    inst_key, REASON_ORDERBOOK_STALE, now_ts, held_qty=float(qty),
                    quote_stale_sec=float(getattr(self, "quote_stale_sec", 30)),
                )
                continue
            reference_ts = float(getattr(self, "latest_external_spot_source_ts", 0.0) or 0.0)
            if reference_ts <= 0 or now_ts - reference_ts > REFERENCE_FRESH_SEC:
                # Not blocking: rules still evaluate with the inputs that are
                # fresh; stale reference votes are already treated as unavailable.
                self._record_protective_exit_degraded(
                    inst_key, REASON_REFERENCE_STALE, now_ts, held_qty=float(qty),
                    reference_age_sec=(now_ts - reference_ts) if reference_ts > 0 else None,
                )
        return blocked

    def _report_protective_exit_availability(self, now_ts: float) -> None:
        """Watchdog-driven observability; never evaluates exits or submits orders."""
        held = self._held_protective_positions()
        if not held:
            return
        if bool(getattr(self, "maker_kill_switch", False)):
            for inst_key in held:
                self._record_protective_exit_degraded(inst_key, REASON_KILL_SWITCH, now_ts)
            return
        self._protective_exit_blocked_instruments(now_ts)

    async def _run_protective_exit_cycle(self, now_ts: float | None = None, *, entry_block_reason: str = "") -> None:
        """The single protective-exit evaluation path for held inventory."""
        now_ts = time.time() if now_ts is None else float(now_ts)
        held = self._held_protective_positions()
        if not held:
            return
        if bool(getattr(self, "maker_kill_switch", False)):
            # Existing kill-switch semantics (cancel everything, no new orders)
            # are unchanged; report the unprotected inventory explicitly.
            for inst_key in held:
                self._record_protective_exit_degraded(inst_key, REASON_KILL_SWITCH, now_ts,
                                                      entry_block_reason=entry_block_reason or "kill_switch")
            return
        blocked = self._protective_exit_blocked_instruments(now_ts)
        await self._maybe_taker_exit_positions(
            now_ts, is_simulation=self._is_dry_run_mode(), blocked_instruments=set(blocked),
        )
        if not blocked:
            await self._maybe_maker_urgent_exit(now_ts)
