"""Runtime verification of the installed Nautilus Polymarket exit semantics."""
from __future__ import annotations

import inspect
from dataclasses import asdict, dataclass
from typing import Any

from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.adapters.polymarket.http.conversion import (
    convert_tif_to_polymarket_order_type,
)
from nautilus_trader.model.enums import TimeInForce


@dataclass(frozen=True)
class ExitOrderCapability:
    requested_tif: str
    limit_ioc_order_type: str
    market_order_type: str
    market_orders_allow_partial_fill: bool
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_exit_order_capability() -> ExitOrderCapability:
    """Inspect the installed adapter rather than assuming its TIF behavior.

    The strategy currently creates a Nautilus *market* order with IOC.  The
    installed adapter separately constructs market orders, so its actual venue
    type must be verified independently from the generic IOC-to-FAK converter.
    """
    limit_ioc_type = str(convert_tif_to_polymarket_order_type(TimeInForce.IOC))
    market_source = inspect.getsource(PolymarketExecutionClient._submit_market_order)
    forces_fok = (
        "order_type=PolyOrderType.FOK" in market_source
        and "order_type_override=PolyOrderType.FOK" in market_source
    )
    return ExitOrderCapability(
        requested_tif="IOC",
        limit_ioc_order_type=limit_ioc_type,
        market_order_type="FOK" if forces_fok else "unknown",
        market_orders_allow_partial_fill=not forces_fok,
        evidence=(
            "PolymarketExecutionClient._submit_market_order forces PolyOrderType.FOK"
            if forces_fok
            else "Installed adapter market-order implementation does not match the expected FOK signature"
        ),
    )
