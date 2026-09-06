"""Pure L2-based entry caps and counterfactual large-order simulations.

The live BUY path uses :func:`cap_buy_quantity`; it never changes a SELL.
The shadow helpers intentionally model immediate taker execution, which is a
conservative way to measure visible depth and a future emergency exit.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence


Level = tuple[Decimal, Decimal]


@dataclass(frozen=True)
class DepthRiskDecision:
    quantity: Decimal
    risk_notional_quantity: Decimal
    risk_loss_quantity: Decimal
    depth_quantity: Decimal
    inventory_quantity: Decimal
    visible_depth: Decimal
    price_boundary: Decimal
    limiting_factor: str
    valid_l2: bool

    def as_payload(self) -> dict[str, float | str | bool]:
        return {
            "quantity": float(self.quantity),
            "risk_notional_quantity": float(self.risk_notional_quantity),
            "risk_loss_quantity": float(self.risk_loss_quantity),
            "depth_quantity": float(self.depth_quantity),
            "inventory_quantity": float(self.inventory_quantity),
            "visible_depth": float(self.visible_depth),
            "price_boundary": float(self.price_boundary),
            "limiting_factor": self.limiting_factor,
            "valid_l2": self.valid_l2,
        }


@dataclass(frozen=True)
class ExecutionEstimate:
    requested_quantity: Decimal
    filled_quantity: Decimal
    fill_rate: Decimal
    vwap: Decimal | None
    best_price: Decimal | None
    slippage_per_share: Decimal | None
    visible_depth: Decimal
    price_boundary: Decimal

    def as_payload(self) -> dict[str, float | None]:
        return {
            "requested_quantity": float(self.requested_quantity),
            "filled_quantity": float(self.filled_quantity),
            "fill_rate": float(self.fill_rate),
            "vwap": float(self.vwap) if self.vwap is not None else None,
            "best_price": float(self.best_price) if self.best_price is not None else None,
            "slippage_per_share": (
                float(self.slippage_per_share) if self.slippage_per_share is not None else None
            ),
            "visible_depth": float(self.visible_depth),
            "price_boundary": float(self.price_boundary),
        }


def _clean_levels(levels: Iterable[tuple[Decimal, Decimal]] | None, *, reverse: bool) -> list[Level]:
    cleaned: list[Level] = []
    for raw_price, raw_quantity in levels or ():
        try:
            price, quantity = Decimal(str(raw_price)), Decimal(str(raw_quantity))
        except Exception:
            continue
        if price > 0 and quantity > 0:
            cleaned.append((price, quantity))
    return sorted(cleaned, key=lambda item: item[0], reverse=reverse)


def estimate_taker_execution(
    *,
    side: str,
    requested_quantity: Decimal,
    levels: Iterable[tuple[Decimal, Decimal]] | None,
    price_boundary: Decimal,
) -> ExecutionEstimate:
    """Estimate an immediate FOK-like fill from visible L2 inside a boundary."""
    quantity = max(Decimal("0"), Decimal(str(requested_quantity)))
    is_buy = str(side).lower() == "buy"
    ordered = _clean_levels(levels, reverse=not is_buy)
    eligible = [
        (price, size)
        for price, size in ordered
        if (price <= price_boundary if is_buy else price >= price_boundary)
    ]
    visible_depth = sum((size for _, size in eligible), Decimal("0"))
    best_price = eligible[0][0] if eligible else None
    remaining, notional = quantity, Decimal("0")
    for price, size in eligible:
        take = min(remaining, size)
        notional += take * price
        remaining -= take
        if remaining <= 0:
            break
    filled = quantity - remaining
    vwap = notional / filled if filled > 0 else None
    slippage = None
    if vwap is not None and best_price is not None:
        slippage = vwap - best_price if is_buy else best_price - vwap
    return ExecutionEstimate(
        requested_quantity=quantity,
        filled_quantity=filled,
        fill_rate=(filled / quantity if quantity > 0 else Decimal("0")),
        vwap=vwap,
        best_price=best_price,
        slippage_per_share=slippage,
        visible_depth=visible_depth,
        price_boundary=price_boundary,
    )


def cap_buy_quantity(
    *,
    entry_price: Decimal,
    tick_size: Decimal,
    asks: Iterable[tuple[Decimal, Decimal]] | None,
    max_entry_notional_usdc: Decimal,
    max_loss_usdc: Decimal,
    depth_fraction: Decimal,
    boundary_ticks: int,
    inventory_headroom: Decimal,
    size_multiplier: Decimal = Decimal("1"),
) -> DepthRiskDecision:
    """Return ``min(risk budget, depth fraction, inventory headroom)`` for BUY.

    A binary share can lose its full entry price, so the loss cap is intentionally
    converted using the entry price.  Existing signal-quality multipliers reduce
    the risk budget before the final minimum; they can never increase it.
    """
    price = Decimal(str(entry_price))
    tick = Decimal(str(tick_size))
    multiplier = min(Decimal("1"), max(Decimal("0"), Decimal(str(size_multiplier))))
    if price <= 0 or tick <= 0:
        return DepthRiskDecision(*(Decimal("0") for _ in range(7)), "invalid_price", False)
    boundary = price + (tick * max(0, int(boundary_ticks)))
    estimate = estimate_taker_execution(
        side="buy", requested_quantity=Decimal("999999999"), levels=asks, price_boundary=boundary,
    )
    valid_l2 = estimate.best_price is not None and estimate.visible_depth > 0
    if not valid_l2:
        return DepthRiskDecision(
            Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"),
            max(Decimal("0"), Decimal(str(inventory_headroom))), Decimal("0"), boundary,
            "missing_l2", False,
        )
    notional_qty = max(Decimal("0"), Decimal(str(max_entry_notional_usdc))) / price * multiplier
    loss_qty = max(Decimal("0"), Decimal(str(max_loss_usdc))) / price * multiplier
    depth_qty = estimate.visible_depth * min(Decimal("1"), max(Decimal("0"), Decimal(str(depth_fraction))))
    inventory_qty = max(Decimal("0"), Decimal(str(inventory_headroom)))
    values = {
        "risk_notional": notional_qty,
        "risk_loss": loss_qty,
        "l2_depth": depth_qty,
        "inventory": inventory_qty,
    }
    limiting_factor = min(values, key=values.get)
    return DepthRiskDecision(
        quantity=values[limiting_factor], risk_notional_quantity=notional_qty,
        risk_loss_quantity=loss_qty, depth_quantity=depth_qty,
        inventory_quantity=inventory_qty, visible_depth=estimate.visible_depth,
        price_boundary=boundary, limiting_factor=limiting_factor, valid_l2=True,
    )


def simulate_large_order_ladder(
    *,
    asks: Sequence[Level] | None,
    bids: Sequence[Level] | None,
    tick_size: Decimal,
    boundary_ticks: int,
    quantities: Sequence[Decimal] = (
        Decimal("10"), Decimal("25"), Decimal("50"), Decimal("100"), Decimal("200"),
    ),
) -> list[dict[str, object]]:
    """Simulate entry fill, immediate executable exit depth, and round-trip markout."""
    clean_asks = _clean_levels(asks, reverse=False)
    clean_bids = _clean_levels(bids, reverse=True)
    if not clean_asks or not clean_bids:
        return []
    tick = max(Decimal("0"), Decimal(str(tick_size)))
    entry_boundary = clean_asks[0][0] + tick * max(0, int(boundary_ticks))
    rows: list[dict[str, object]] = []
    for requested in quantities:
        entry = estimate_taker_execution(
            side="buy", requested_quantity=Decimal(str(requested)), levels=clean_asks,
            price_boundary=entry_boundary,
        )
        exit_estimate = None
        if entry.vwap is not None and entry.filled_quantity > 0:
            exit_estimate = estimate_taker_execution(
                side="sell", requested_quantity=entry.filled_quantity, levels=clean_bids,
                price_boundary=entry.vwap - tick * max(0, int(boundary_ticks)),
            )
        immediate_markout = None
        if exit_estimate and entry.vwap is not None and exit_estimate.vwap is not None:
            immediate_markout = (exit_estimate.vwap - entry.vwap) * exit_estimate.filled_quantity
        rows.append({
            "requested_quantity": Decimal(str(requested)),
            "entry": entry,
            "exit": exit_estimate,
            "immediate_round_trip_markout_usdc": immediate_markout,
        })
    return rows
