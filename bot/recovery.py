from __future__ import annotations

import sqlite3
import threading
import time
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from loguru import logger
from nautilus_trader.model.identifiers import InstrumentId

from bot.inventory import InventoryLedger
from bot.wallet_ops import (
    ensure_balance_clob_client,
    fetch_conditional_balance,
    refresh_collateral_balance,
)


class StrategyRecoveryMixin:
    """
    Startup recovery and balance/position cache helpers.

    These methods are part of the live trading path, but they are not strategy
    decision logic. Keeping them out of `run_bot.py` reduces blast radius for
    future changes and makes restart / recovery behavior easier to reason about.
    """

    def _rebuild_inventory_state_from_db(
        self,
        instrument_id: Any,
        target_qty: Decimal,
        lookback_hours: int = 72,
    ) -> Optional[Dict[str, Any]]:
        inst = self._normalize_instrument_id(instrument_id)
        inst_key = self._instrument_key(inst)
        if inst is None or not inst_key or target_qty <= 0:
            return None
        if not self.trade_db:
            return None
        db_path = getattr(self.trade_db, "db_path", "")
        if not db_path:
            return None

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, lookback_hours))).isoformat()
        ledger: Dict[str, Dict[str, Any]] = {}
        first_fill_ts: float = 0.0
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT ts, side, price, qty, commission_usdc, payload_json
                FROM order_events
                WHERE event_type='ORDER_FILLED'
                  AND instrument_id=?
                  AND ts >= ?
                ORDER BY id
                """,
                (inst_key, cutoff),
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.warning(f"Inventory rehydrate DB replay failed for {inst_key}: {e}")
            return None

        for row in rows:
            side_norm = self._normalize_side_text(row["side"])
            fill_price = Decimal(str(row["price"] or 0))
            fill_qty = Decimal(str(row["qty"] or 0))
            fee_usdc = Decimal(str(row["commission_usdc"] or 0))
            fee_shares = Decimal("0")
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
                if isinstance(payload, dict):
                    fee_shares = Decimal(str(payload.get("effective_fee_shares", 0) or 0))
            except Exception:
                fee_shares = Decimal("0")
            if not side_norm or fill_price <= 0 or fill_qty <= 0:
                continue
            if first_fill_ts <= 0:
                try:
                    first_fill_ts = datetime.fromisoformat(str(row["ts"])).timestamp()
                except Exception:
                    first_fill_ts = time.time()
            InventoryLedger.update_from_fill(
                live_inventory_cost=ledger,
                instrument_key=inst_key,
                side=side_norm,
                fill_price=fill_price,
                fill_qty=fill_qty,
                fee_usdc=fee_usdc,
                fee_shares=fee_shares,
                now_ts=first_fill_ts or time.time(),
            )

        state = ledger.get(inst_key)
        if not state:
            return None

        replay_qty = Decimal(str(state.get("qty", "0")))
        if replay_qty <= 0:
            return None

        if replay_qty != target_qty:
            fee_remaining = Decimal(str(state.get("entry_fee_remaining", "0")))
            scaled_fee = fee_remaining
            if replay_qty > 0 and fee_remaining > 0:
                scaled_fee = fee_remaining * (target_qty / replay_qty)
            state["qty"] = target_qty
            state["entry_fee_remaining"] = max(Decimal("0"), scaled_fee)
        if float(state.get("opened_ts", 0.0)) <= 0:
            state["opened_ts"] = first_fill_ts or time.time()
        return state

    def _rehydrate_inventory_state_on_startup(self) -> None:
        if self.live_inventory_cost or self.inventory_delta_shares > 0:
            return

        restore_targets: List[InstrumentId] = []
        for inst in self.current_market_instruments or []:
            norm = self._normalize_instrument_id(inst)
            if norm is not None and norm not in restore_targets:
                restore_targets.append(norm)
        if self.instrument_id is not None:
            norm = self._normalize_instrument_id(self.instrument_id)
            if norm is not None and norm not in restore_targets:
                restore_targets.append(norm)
        if not restore_targets:
            return

        restored_total = Decimal("0")
        restored_items: List[Dict[str, Any]] = []
        for inst in restore_targets:
            open_qty = self._get_sellable_qty_for_current_instrument(instrument_id=inst)
            if open_qty <= 0:
                continue
            state = self._rebuild_inventory_state_from_db(inst, target_qty=open_qty)
            if state is None:
                state = {
                    "qty": open_qty,
                    "avg_entry_price": Decimal("0"),
                    "entry_fee_remaining": Decimal("0"),
                    "opened_ts": time.time(),
                }
                logger.warning(
                    f"Inventory rehydrate fallback: restored qty without cost basis "
                    f"inst={self._instrument_key(inst)} qty={float(open_qty):.6f}"
                )
            self.live_inventory_cost[self._instrument_key(inst)] = state
            restored_total += Decimal(str(state.get("qty", "0")))
            restored_items.append(
                {
                    "instrument_id": self._instrument_key(inst),
                    "qty": float(Decimal(str(state.get("qty", "0")))),
                    "avg_entry_price": float(Decimal(str(state.get("avg_entry_price", "0")))),
                }
            )

        if restored_total > 0:
            self.inventory_delta_shares = restored_total
            self._startup_rehydrated_inventory_force_sell_only = True
            logger.warning(
                "Startup inventory rehydrated: "
                f"slug={self.current_market_slug or ''} restored_qty={float(restored_total):.6f} "
                f"legs={len(restored_items)}"
            )
            self._db_strategy_event(
                "STARTUP_INVENTORY_REHYDRATED",
                {
                    "slug": self.current_market_slug or "",
                    "restored_total_qty": float(restored_total),
                    "legs": restored_items,
                },
            )

    def _get_sellable_qty_for_current_instrument(self, instrument_id: Optional[Any] = None) -> Decimal:
        inst = instrument_id if instrument_id is not None else self.instrument_id
        inst = self._normalize_instrument_id(inst)
        if inst is None:
            return Decimal("0")
        total = Decimal("0")
        try:
            positions = self.cache.positions_open(instrument_id=inst)
            for pos in positions or []:
                signed = Decimal(str(getattr(pos, "signed_qty", 0.0) or 0.0))
                if signed > 0:
                    total += signed
        except Exception as e:
            logger.debug(f"Could not read sellable qty from cache positions: {e}")
        return total

    def _refresh_balance_cache_sync(self) -> Optional[Decimal]:
        self._balance_clob_client = ensure_balance_clob_client(
            current_client=getattr(self, "_balance_clob_client", None),
            logger_info_fn=logger.info,
            logger_warning_fn=logger.warning,
        )
        self._balance_clob_client, refreshed_balance = refresh_collateral_balance(
            current_client=self._balance_clob_client,
            cached_balance=self._cached_usdc_balance,
            logger_info_fn=logger.info,
            logger_warning_fn=logger.warning,
            logger_debug_fn=logger.debug,
        )
        if refreshed_balance is not None:
            self._cached_usdc_balance = refreshed_balance
            self._balance_last_check_ts = time.time()
            try:
                if not hasattr(self, "_prom_wallet_balance"):
                    from prometheus_client import Gauge

                    self._prom_wallet_balance = Gauge(
                        "trading_wallet_balance_usdc",
                        "Real on-chain wallet USDC balance",
                    )
                self._prom_wallet_balance.set(float(self._cached_usdc_balance))
            except Exception:
                pass
            try:
                self._db_strategy_event(
                    "ACCOUNT_SUMMARY",
                    {
                        "usdc_balance": float(self._cached_usdc_balance),
                        "pol_balance": float(getattr(self, "_cached_pol_balance", 0.0) or 0.0),
                    },
                )
            except Exception:
                pass
            self._update_terminal_dashboard_snapshot()
        return self._cached_usdc_balance

    def _start_balance_refresh_timer(self) -> None:
        interval = max(5.0, float(self.balance_check_interval_sec))
        while not self._balance_stop_event.wait(interval):
            if self._stopping:
                return
            try:
                self._refresh_balance_cache_sync()
            except Exception as exc:
                logger.debug(f"Background balance refresh failed: {exc}")

    def _refresh_balance_cache(self) -> Optional[Decimal]:
        now_ts = time.time()
        if now_ts - self._balance_last_check_ts < self.balance_check_interval_sec:
            return self._cached_usdc_balance
        if not self._balance_refresh_inflight and self._balance_refresh_lock.acquire(blocking=False):
            self._balance_refresh_inflight = True
            try:
                def _runner() -> None:
                    try:
                        self._refresh_balance_cache_sync()
                    except Exception as exc:
                        logger.debug(f"Deferred balance refresh failed: {exc}")
                    finally:
                        self._balance_refresh_inflight = False
                        self._balance_refresh_lock.release()

                threading.Thread(target=_runner, daemon=True).start()
            except Exception:
                self._balance_refresh_inflight = False
                self._balance_refresh_lock.release()
        return self._cached_usdc_balance

    def _get_conditional_balance_for_token(
        self,
        token_id: Optional[str],
        force_refresh: bool = False,
    ) -> Optional[Decimal]:
        token = str(token_id or "").strip()
        if not token:
            return None

        self._balance_clob_client = ensure_balance_clob_client(
            current_client=getattr(self, "_balance_clob_client", None),
            logger_info_fn=logger.info,
            logger_warning_fn=logger.warning,
        )
        cache_entry = self._conditional_balance_cache_by_token.get(token)
        self._balance_clob_client, balance, updated_entry = fetch_conditional_balance(
            token=token,
            current_client=self._balance_clob_client,
            cached_entry=cache_entry,
            conditional_balance_check_interval_sec=float(self.conditional_balance_check_interval_sec),
            force_refresh=force_refresh,
            logger_debug_fn=logger.debug,
        )
        if updated_entry is not None:
            self._conditional_balance_cache_by_token[token] = updated_entry
        return balance
