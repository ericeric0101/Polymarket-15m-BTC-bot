"""Pure offline capital and orderbook-capacity calculations."""
from __future__ import annotations

from typing import Iterable


def capital_efficiency(*, capital_committed_usdc: float, entry_ts: float, exit_ts: float,
                       gross_pnl: float, net_pnl: float | None = None) -> dict[str, float | None]:
    holding_sec = max(0.0, float(exit_ts) - float(entry_ts))
    capital_minutes = float(capital_committed_usdc) * holding_sec / 60.0
    pnl = net_pnl if net_pnl is not None else gross_pnl
    return {"holding_sec": holding_sec, "capital_seconds": capital_minutes * 60.0,
            "capital_minutes": capital_minutes,
            "profit_per_dollar_minute": float(pnl) / capital_minutes if capital_minutes else None}


def bounded_capacity(levels: Iterable[tuple[float, float]], *, side: str, reference_price: float,
                     max_slippage: float) -> dict[str, float]:
    """Capacity using already-captured book levels; never assumes a partial fill is full."""
    eligible = []
    for price, quantity in levels:
        if (side.upper() == "BUY" and price <= reference_price + max_slippage) or (
            side.upper() == "SELL" and price >= reference_price - max_slippage):
            eligible.append((float(price), float(quantity)))
    qty = sum(qty for _, qty in eligible)
    notional = sum(price * qty for price, qty in eligible)
    return {"depth_available": qty, "capital_usdc": notional,
            "avg_fill_price": notional / qty if qty else 0.0,
            "worst_fill_price": eligible[-1][0] if eligible else 0.0}


def entry_timing_bin(time_left_sec: float | None) -> str:
    if time_left_sec is None:
        return "UNKNOWN"
    value = float(time_left_sec)
    if value > 600: return ">600s"
    if value >= 480: return "480–600s"
    if value >= 360: return "360–480s"
    if value >= 240: return "240–360s"
    if value >= 120: return "120–240s"
    return "<120s"
