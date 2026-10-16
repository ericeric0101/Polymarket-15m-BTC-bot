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
    insufficient_depth_can_reject_entire_order: bool
    limit_ioc_uses_adapter_tif_converter: bool
    price_bounded_limit_ioc_avoids_fok_requirement: bool
    aggressive_partial_exit_proven: bool
    verdict: str
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
    limit_source = inspect.getsource(PolymarketExecutionClient._submit_limit_order)
    post_source = inspect.getsource(PolymarketExecutionClient._post_signed_order)
    forces_fok = (
        "order_type=PolyOrderType.FOK" in market_source
        and "order_type_override=PolyOrderType.FOK" in market_source
    )
    limit_uses_converter = (
        "self._post_signed_order(order, signed_order, post_only=order.is_post_only)" in limit_source
        and "convert_tif_to_polymarket_order_type" in post_source
        and "order_type_override or" in post_source
    )
    return ExitOrderCapability(
        requested_tif="IOC",
        limit_ioc_order_type=limit_ioc_type,
        market_order_type="FOK" if forces_fok else "unknown",
        market_orders_allow_partial_fill=not forces_fok,
        insufficient_depth_can_reject_entire_order=forces_fok,
        limit_ioc_uses_adapter_tif_converter=limit_uses_converter,
        price_bounded_limit_ioc_avoids_fok_requirement=(limit_uses_converter and limit_ioc_type == "FAK"),
        aggressive_partial_exit_proven=False,
        verdict=("NOT_PROVEN_MARKET_PATH_IS_FOK" if forces_fok
                 else "NOT_PROVEN_ADAPTER_SIGNATURE_UNKNOWN"),
        evidence=(
            "market path forces PolyOrderType.FOK; limit path delegates IOC through the FAK converter"
            if forces_fok
            else "Installed adapter market-order implementation does not match the expected FOK signature"
        ),
    )
