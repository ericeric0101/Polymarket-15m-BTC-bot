from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional


class InventoryLedger:
    @staticmethod
    def max_avg_entry(live_inventory_cost: dict[str, dict[str, Any]]) -> Decimal:
        max_avg_entry = Decimal("0")
        for state in live_inventory_cost.values():
            qty = Decimal(str(state.get("qty", "0")))
            avg = Decimal(str(state.get("avg_entry_price", "0")))
            if qty > 0 and avg > max_avg_entry:
                max_avg_entry = avg
        return max_avg_entry

    @staticmethod
    def update_from_fill(
        live_inventory_cost: dict[str, dict[str, Any]],
        instrument_key: str,
        side: str,
        fill_price: Decimal,
        fill_qty: Decimal,
        fee_usdc: Decimal,
        fee_shares: Decimal,
        now_ts: float,
    ) -> Optional[Decimal]:
        if not instrument_key or fill_qty <= 0 or fill_price <= 0:
            return None
        state = live_inventory_cost.setdefault(
            instrument_key,
            {
                "qty": Decimal("0"),
                "avg_entry_price": Decimal("0"),
                "entry_fee_remaining": Decimal("0"),
                "opened_ts": 0.0,
            },
        )
        if side == "buy":
            old_qty = Decimal(str(state.get("qty", "0")))
            old_avg = Decimal(str(state.get("avg_entry_price", "0")))
            net_fill_qty = max(Decimal("0"), fill_qty - max(Decimal("0"), fee_shares))
            new_qty = old_qty + net_fill_qty
            if new_qty <= 0:
                return None
            weighted_notional = (old_qty * old_avg) + (net_fill_qty * fill_price) if old_qty > 0 and old_avg > 0 else (net_fill_qty * fill_price)
            state["qty"] = new_qty
            state["avg_entry_price"] = weighted_notional / new_qty
            state["entry_fee_remaining"] = Decimal(str(state.get("entry_fee_remaining", "0"))) + max(Decimal("0"), fee_usdc)
            if float(state.get("opened_ts", 0.0)) <= 0:
                state["opened_ts"] = now_ts
            return None

        if side != "sell":
            return None
        old_qty = Decimal(str(state.get("qty", "0")))
        if old_qty <= 0:
            return None
        sell_qty = min(fill_qty, old_qty)
        if sell_qty <= 0:
            return None
        avg_entry = Decimal(str(state.get("avg_entry_price", "0")))
        fee_remaining = Decimal(str(state.get("entry_fee_remaining", "0")))
        alloc_ratio = sell_qty / old_qty if old_qty > 0 else Decimal("0")
        entry_fee_alloc = fee_remaining * alloc_ratio
        realized_net = (sell_qty * (fill_price - avg_entry)) - entry_fee_alloc - max(Decimal("0"), fee_usdc)

        remaining_qty = old_qty - sell_qty
        remaining_fee = max(Decimal("0"), fee_remaining - entry_fee_alloc)
        if remaining_qty <= 0:
            state["qty"] = Decimal("0")
            state["avg_entry_price"] = Decimal("0")
            state["entry_fee_remaining"] = Decimal("0")
            state["opened_ts"] = 0.0
        else:
            state["qty"] = remaining_qty
            state["entry_fee_remaining"] = remaining_fee
        return realized_net
