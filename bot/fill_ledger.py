from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Optional

from loguru import logger

from bot.inventory import InventoryLedger
from bot.post_trade import apply_fill_followup


class FillLedgerMixin:
    """
    Helpers for post-fill inventory and bookkeeping.

    These methods are part of the live maker trading path, but they are not
    quote generation or lifecycle orchestration. Keeping them separate makes
    `on_order_filled()` smaller without changing its runtime behavior.
    """

    def _update_live_inventory_cost_from_fill(
        self,
        instrument_id: Any,
        side: str,
        fill_price: Decimal,
        fill_qty: Decimal,
        commission: Decimal,
    ) -> Optional[Decimal]:
        inst_key = self._instrument_key(instrument_id)
        side_norm = self._normalize_side_text(side)
        pre_state = self.live_inventory_cost.get(inst_key, {}) if inst_key else {}
        pre_qty = Decimal(str(pre_state.get("qty", "0")))
        pre_avg_entry = Decimal(str(pre_state.get("avg_entry_price", "0")))
        realized_net = InventoryLedger.update_from_fill(
            live_inventory_cost=self.live_inventory_cost,
            instrument_key=inst_key,
            side=side_norm,
            fill_price=fill_price,
            fill_qty=fill_qty,
            commission=commission,
            now_ts=time.time(),
        )
        if realized_net is None or side_norm != "sell":
            return realized_net
        state = self.live_inventory_cost.get(inst_key, {})
        sell_qty = min(fill_qty, pre_qty)
        logger.info(
            f"Inventory realized[{inst_key[:18]}..]: sold={float(sell_qty):.6f} "
            f"entry={float(pre_avg_entry):.4f} exit={float(fill_price):.4f} "
            f"net_pnl={float(realized_net):+.4f} remaining={float(state['qty']):.6f}"
        )
        return realized_net

    def _record_market_buy_count_if_needed(
        self,
        *,
        side_for_ledger: str,
        current_slug: str,
        filled_id: str,
        filled_inst: Any,
        liquidity_side_raw: Any,
    ) -> None:
        if side_for_ledger != "buy" or not current_slug:
            return
        counted_ids = self.market_buy_counted_order_ids_by_slug.setdefault(current_slug, set())
        if not filled_id or filled_id in counted_ids:
            return
        counted_ids.add(filled_id)
        new_buy_count = int(self.market_buy_count_by_slug.get(current_slug, 0)) + 1
        self.market_buy_count_by_slug[current_slug] = new_buy_count
        self._db_strategy_event(
            "MARKET_BUY_COUNT_UPDATED",
            {
                "slug": current_slug,
                "count": new_buy_count,
                "max_per_market": int(self.market_max_buy_events_per_market),
                "instrument_id": str(filled_inst) if filled_inst else None,
                "client_order_id": filled_id,
                "liquidity_side": str(liquidity_side_raw or ""),
            },
        )

    def _record_observed_fee_rate_from_fill(
        self,
        *,
        fill_qty_dec: Decimal,
        fill_price_dec: Decimal,
        fill_commission_dec: Decimal,
    ) -> None:
        try:
            notional = fill_qty_dec * fill_price_dec
            commission = fill_commission_dec
            if notional > 0 and commission >= 0:
                observed_bps = int(round(float((commission / notional) * Decimal("10000"))))
                if observed_bps > 0:
                    self.last_observed_fee_rate_bps = observed_bps
                    logger.info(
                        f"Observed effective fee rate from fill: {observed_bps} bps "
                        f"(commission={float(commission):.6f}, notional={float(notional):.6f})"
                    )
        except Exception as e:
            logger.debug(f"Could not derive observed fee bps from fill: {e}")

    def _apply_post_fill_followup(
        self,
        *,
        fill_side_norm: str | None,
        realized_net_usdc: Decimal | None,
    ) -> None:
        followup = apply_fill_followup(
            fill_side_norm=fill_side_norm,
            post_fill_buy_cooldown_sec=float(self.post_fill_buy_cooldown_sec),
            buy_cooldown_until_ts=float(self.buy_cooldown_until_ts),
            fill_cooldown_policy=self.fill_cooldown_policy,
            realized_net_usdc=realized_net_usdc,
            market_cycle_realized_net_usdc=self.market_cycle_realized_net_usdc,
            recent_fill_pnl_results=self.recent_fill_pnl_results,
            quote_pause_until_ts=float(self.quote_pause_until_ts),
            now_ts=time.time(),
        )
        self.buy_cooldown_until_ts = followup.buy_cooldown_until_ts
        self.market_cycle_realized_net_usdc = followup.market_cycle_realized_net_usdc
        self.recent_fill_pnl_results = followup.recent_fill_pnl_results
        self.quote_pause_until_ts = followup.quote_pause_until_ts
        if fill_side_norm == "buy" and self.post_fill_buy_cooldown_sec > 0:
            logger.info(
                f"Post-fill buy cooldown activated: {self.post_fill_buy_cooldown_sec}s "
                f"(no new BUY orders until cooldown expires)"
            )

        if followup.triggered_loss_pause:
            logger.warning(
                f"Consecutive loss pause activated: {self.max_consecutive_losses} consecutive losses "
                f"(total={followup.total_loss:.4f} USDC). Pausing all quoting for {self.loss_pause_sec}s."
            )
