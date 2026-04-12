from __future__ import annotations

from dataclasses import dataclass
import time
from decimal import Decimal
from typing import Any, Optional, Protocol

from loguru import logger

from bot.inventory import InventoryLedger
from bot.post_trade import apply_fill_followup
from execution.rebate_model import estimate_taker_buy_fee_shares, estimate_taker_fee_usdc


@dataclass
class FillLiquidityInterpretation:
    liquidity_class: str
    is_maker_fill: bool
    effective_fee_usdc_dec: Decimal
    effective_fee_shares_dec: Decimal
    warning_message: str = ""


class FillLedgerHost(Protocol):
    live_inventory_cost: dict[str, Any]
    maker_profit_run_peak_bid_by_inst: dict[str, Decimal]
    maker_profit_run_peak_fair_by_inst: dict[str, Decimal]
    recent_buy_fill_ts_by_inst: dict[str, float]
    recent_sell_fill_ts_by_inst: dict[str, float]
    post_fill_buy_cooldown_sec: float
    buy_cooldown_until_ts: float
    fill_cooldown_policy: Any
    market_cycle_realized_net_usdc: Decimal
    recent_fill_pnl_results: list[Any]
    quote_pause_until_ts: float
    market_buy_count_by_slug: dict[str, int]
    market_buy_counted_order_ids_by_slug: dict[str, set[str]]
    market_max_buy_events_per_market: int

    def _instrument_key(self, instrument_id: Any) -> str: ...
    def _normalize_side_text(self, side: str) -> str: ...
    def _clear_profit_run_state(self, instrument_id: Any) -> None: ...
    def _market_buy_budget_key(self, slug: str) -> str: ...
    def _current_thesis_epoch(self, slug: str) -> int: ...
    def _db_strategy_event(self, event_type: str, payload: dict[str, Any]) -> None: ...


def classify_fill_liquidity(
    liquidity_side: Any,
    raw_commission_dec: Decimal,
    maker_matched: bool,
) -> str:
    txt = str(liquidity_side or "").strip().upper()
    if txt in {"MAKER", "1", "ADD", "ADD_LIQUIDITY", "PROVIDE", "PROVIDER"}:
        return "maker"
    if txt in {"TAKER", "2", "REMOVE", "REMOVE_LIQUIDITY", "TAKE", "TAKER_A", "TAKER_B"}:
        return "taker"
    if raw_commission_dec > 0:
        return "taker"
    if maker_matched:
        return "maker"
    return "unknown"


def interpret_fill_liquidity(
    *,
    liquidity_side: Any,
    raw_commission_dec: Decimal,
    maker_matched: bool,
    side_for_ledger: str,
    fill_price_dec: Decimal,
    fill_qty_dec: Decimal,
    filled_limit_price: Decimal,
    filled_id: str,
) -> FillLiquidityInterpretation:
    liquidity_class = classify_fill_liquidity(
        liquidity_side=liquidity_side,
        raw_commission_dec=raw_commission_dec,
        maker_matched=maker_matched,
    )
    is_maker_fill = liquidity_class == "maker"
    effective_fee_usdc_dec = Decimal("0")
    effective_fee_shares_dec = Decimal("0")
    if not is_maker_fill and side_for_ledger:
        effective_fee_usdc_calc = estimate_taker_fee_usdc(
            shares=fill_qty_dec,
            probability=fill_price_dec,
        )
        if side_for_ledger == "buy":
            effective_fee_shares_dec = estimate_taker_buy_fee_shares(
                shares=fill_qty_dec,
                probability=fill_price_dec,
            )
        else:
            effective_fee_usdc_dec = effective_fee_usdc_calc
    warning_message = ""
    if maker_matched and liquidity_class == "taker" and filled_limit_price > 0 and side_for_ledger:
        if (
            (side_for_ledger == "buy" and fill_price_dec <= filled_limit_price)
            or (side_for_ledger == "sell" and fill_price_dec >= filled_limit_price)
        ):
            warning_message = (
                "Maker order fill was labeled taker despite passive price improvement "
                f"(order={filled_id} side={side_for_ledger} limit={float(filled_limit_price):.4f} "
                f"fill={float(fill_price_dec):.4f} liquidity_side={liquidity_side!r} "
                f"commission={float(raw_commission_dec):.6f})"
            )
    return FillLiquidityInterpretation(
        liquidity_class=liquidity_class,
        is_maker_fill=is_maker_fill,
        effective_fee_usdc_dec=effective_fee_usdc_dec,
        effective_fee_shares_dec=effective_fee_shares_dec,
        warning_message=warning_message,
    )


class FillLedgerMixin:
    """
    Helpers for post-fill inventory and bookkeeping.

    These methods are part of the live maker trading path, but they are not
    quote generation or lifecycle orchestration. Keeping them separate makes
    `on_order_filled()` smaller without changing its runtime behavior.
    """

    def _update_live_inventory_cost_from_fill(
        self: FillLedgerHost,
        instrument_id: Any,
        side: str,
        fill_price: Decimal,
        fill_qty: Decimal,
        fee_usdc: Decimal,
        fee_shares: Decimal,
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
            fee_usdc=fee_usdc,
            fee_shares=fee_shares,
            now_ts=time.time(),
        )
        if realized_net is None or side_norm != "sell":
            if side_norm == "buy" and inst_key:
                # A new winning run starts from the fresh fill price.
                self.maker_profit_run_peak_bid_by_inst[inst_key] = fill_price
                self.maker_profit_run_peak_fair_by_inst[inst_key] = fill_price
                self.recent_buy_fill_ts_by_inst[inst_key] = time.time()
            return realized_net
        state = self.live_inventory_cost.get(inst_key, {})
        if inst_key:
            self.recent_sell_fill_ts_by_inst[inst_key] = time.time()
        sell_qty = min(fill_qty, pre_qty)
        opened_ts = float(pre_state.get("opened_ts", 0.0) or 0.0)
        hold_sec = max(0.0, time.time() - opened_ts) if opened_ts > 0 else 0.0
        logger.info(
            f"Inventory realized[{inst_key[:18]}..]: sold={float(sell_qty):.6f} "
            f"entry={float(pre_avg_entry):.4f} exit={float(fill_price):.4f} "
            f"net_pnl={float(realized_net):+.4f} remaining={float(state['qty']):.6f}"
        )
        try:
            metrics = getattr(self, "_baseline_metrics", None)
            if isinstance(metrics, dict):
                metrics["realized_exit_count"] = int(metrics.get("realized_exit_count", 0)) + 1
                metrics["realized_exit_net_sum"] = float(metrics.get("realized_exit_net_sum", 0.0)) + float(realized_net)
                metrics["realized_exit_hold_sec_sum"] = float(metrics.get("realized_exit_hold_sec_sum", 0.0)) + float(hold_sec)
                if realized_net > 0:
                    metrics["realized_win_count"] = int(metrics.get("realized_win_count", 0)) + 1
                elif realized_net < 0:
                    metrics["realized_loss_count"] = int(metrics.get("realized_loss_count", 0)) + 1
            self._db_strategy_event(
                "EXIT_BASELINE_METRIC",
                {
                    "instrument_id": inst_key,
                    "sell_qty": float(sell_qty),
                    "entry_price": float(pre_avg_entry),
                    "exit_price": float(fill_price),
                    "net_pnl": float(realized_net),
                    "hold_sec": float(hold_sec),
                    "remaining_qty": float(state["qty"]),
                },
            )
        except Exception:
            pass
        try:
            remaining_qty = Decimal(str(state.get("qty", "0")))
        except Exception:
            remaining_qty = Decimal("0")
        if remaining_qty <= 0:
            self._clear_profit_run_state(instrument_id)
            if inst_key:
                self.recent_buy_fill_ts_by_inst.pop(inst_key, None)
                self.recent_sell_fill_ts_by_inst.pop(inst_key, None)
        return realized_net

    def _record_market_buy_count_if_needed(
        self: FillLedgerHost,
        *,
        side_for_ledger: str,
        current_slug: str,
        filled_id: str,
        filled_inst: Any,
        liquidity_side_raw: Any,
    ) -> None:
        if side_for_ledger != "buy" or not current_slug:
            return
        budget_key = self._market_buy_budget_key(current_slug)
        thesis_epoch = self._current_thesis_epoch(current_slug)
        counted_ids = self.market_buy_counted_order_ids_by_slug.setdefault(budget_key, set())
        if not filled_id or filled_id in counted_ids:
            return
        counted_ids.add(filled_id)
        new_buy_count = int(self.market_buy_count_by_slug.get(budget_key, 0)) + 1
        self.market_buy_count_by_slug[budget_key] = new_buy_count
        self._db_strategy_event(
            "MARKET_BUY_COUNT_UPDATED",
            {
                "slug": current_slug,
                "thesis_epoch": thesis_epoch,
                "budget_key": budget_key,
                "count": new_buy_count,
                "max_per_market": int(self.market_max_buy_events_per_market),
                "instrument_id": str(filled_inst) if filled_inst else None,
                "client_order_id": filled_id,
                "liquidity_side": str(liquidity_side_raw or ""),
            },
        )

    def _record_observed_fee_rate_from_fill(
        self: FillLedgerHost,
        *,
        side_for_ledger: str,
        fill_qty_dec: Decimal,
        fill_price_dec: Decimal,
        effective_fee_usdc_dec: Decimal,
        effective_fee_shares_dec: Decimal,
    ) -> None:
        try:
            notional = fill_qty_dec * fill_price_dec
            commission_usdc = effective_fee_usdc_dec
            if notional > 0 and commission_usdc > 0:
                observed_bps = int(round(float((commission_usdc / notional) * Decimal("10000"))))
                if observed_bps > 0:
                    self.last_observed_fee_rate_bps = observed_bps
                    logger.info(
                        f"Observed effective fee rate from fill: {observed_bps} bps "
                        f"(fee_usdc={float(commission_usdc):.6f}, notional={float(notional):.6f})"
                    )
            elif side_for_ledger == "buy" and effective_fee_shares_dec > 0:
                logger.info(
                    "Observed taker BUY fee in shares: "
                    f"fee_shares={float(effective_fee_shares_dec):.6f} "
                    f"gross_qty={float(fill_qty_dec):.6f} "
                    f"price={float(fill_price_dec):.4f}"
                )
        except Exception as e:
            logger.debug(f"Could not derive observed fee bps from fill: {e}")

    def _apply_post_fill_followup(
        self: FillLedgerHost,
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
