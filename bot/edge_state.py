from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class EdgeState:
    """Probability-vs-price diagnostics shared by entry and exit paths.

    This object is intentionally observational for the first rollout. It
    records both midpoint and executable-price edges without changing order
    behavior by itself.
    """

    model_probability_up: Decimal | None
    market_probability_up: Decimal | None
    up_bid: Decimal | None
    up_ask: Decimal | None
    down_bid: Decimal | None
    down_ask: Decimal | None
    total_cost_buffer: Decimal
    up_edge_vs_mid: Decimal | None
    up_edge_vs_ask: Decimal | None
    down_edge_vs_ask: Decimal | None
    up_net_edge_vs_ask: Decimal | None
    down_net_edge_vs_ask: Decimal | None

    @property
    def edge_available(self) -> bool:
        return self.model_probability_up is not None and self.market_probability_up is not None

    def to_dict(self) -> dict[str, Any]:
        def as_float(value: Decimal | None) -> float | None:
            return float(value) if value is not None else None

        return {
            "model_probability_up": as_float(self.model_probability_up),
            "market_probability_up": as_float(self.market_probability_up),
            "up_bid": as_float(self.up_bid),
            "up_ask": as_float(self.up_ask),
            "down_bid": as_float(self.down_bid),
            "down_ask": as_float(self.down_ask),
            "total_cost_buffer": as_float(self.total_cost_buffer),
            "up_edge_vs_mid": as_float(self.up_edge_vs_mid),
            "up_edge_vs_ask": as_float(self.up_edge_vs_ask),
            "down_edge_vs_ask": as_float(self.down_edge_vs_ask),
            "up_net_edge_vs_ask": as_float(self.up_net_edge_vs_ask),
            "down_net_edge_vs_ask": as_float(self.down_net_edge_vs_ask),
            "edge_available": self.edge_available,
        }


def _positive(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    value = Decimal(str(value))
    return value if value > 0 else None


def _subtract(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None or right is None:
        return None
    return left - right


def build_edge_state(
    *,
    model_probability_up: Decimal | None,
    market_mid: Decimal | None,
    up_bid: Decimal | None,
    up_ask: Decimal | None,
    down_bid: Decimal | None,
    down_ask: Decimal | None,
    total_cost_buffer: Decimal,
) -> EdgeState:
    model = _positive(model_probability_up)
    market = _positive(market_mid)
    up_bid = _positive(up_bid)
    up_ask = _positive(up_ask)
    down_bid = _positive(down_bid)
    down_ask = _positive(down_ask)
    cost = max(Decimal("0"), Decimal(str(total_cost_buffer)))

    # p(UP) - p(market) is a diagnostic market-implied edge. Executable
    # edges use asks and therefore include the actual price paid by a taker.
    up_edge_vs_mid = _subtract(model, market)
    down_edge_vs_ask = _subtract(
        (Decimal("1") - model) if model is not None else None,
        down_ask,
    )
    up_edge_vs_ask = _subtract(model, up_ask)
    up_net_edge_vs_ask = (
        up_edge_vs_ask - cost if up_edge_vs_ask is not None else None
    )
    down_net_edge_vs_ask = (
        down_edge_vs_ask - cost if down_edge_vs_ask is not None else None
    )

    return EdgeState(
        model_probability_up=model,
        market_probability_up=market,
        up_bid=up_bid,
        up_ask=up_ask,
        down_bid=down_bid,
        down_ask=down_ask,
        total_cost_buffer=cost,
        up_edge_vs_mid=up_edge_vs_mid,
        up_edge_vs_ask=up_edge_vs_ask,
        down_edge_vs_ask=down_edge_vs_ask,
        up_net_edge_vs_ask=up_net_edge_vs_ask,
        down_net_edge_vs_ask=down_net_edge_vs_ask,
    )
