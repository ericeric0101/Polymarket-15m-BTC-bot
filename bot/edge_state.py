from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class EdgeState:
    """Probability-vs-price diagnostics for shadow and future entry gates."""

    model_probability_up: Decimal | None
    market_probability_up: Decimal | None
    up_bid: Decimal | None
    up_ask: Decimal | None
    down_bid: Decimal | None
    down_ask: Decimal | None
    fee_buffer: Decimal
    slippage_buffer: Decimal
    adverse_selection_buffer: Decimal
    model_error_buffer: Decimal
    total_cost_buffer: Decimal
    quote_age_sec: Decimal | None
    up_quote_age_sec: Decimal | None
    down_quote_age_sec: Decimal | None
    max_quote_age_sec: Decimal
    up_edge_vs_mid: Decimal | None
    up_edge_vs_ask: Decimal | None
    down_edge_vs_ask: Decimal | None
    up_net_edge_vs_ask: Decimal | None
    down_net_edge_vs_ask: Decimal | None

    @property
    def diagnostic_edge_available(self) -> bool:
        return (
            self.model_probability_up is not None
            and self.market_probability_up is not None
        )

    @property
    def executable_edge_available(self) -> bool:
        return (
            self.model_probability_up is not None
            and (self.up_ask is not None or self.down_ask is not None)
        )

    @property
    def up_executable_edge_available(self) -> bool:
        return self.model_probability_up is not None and self.up_ask is not None

    @property
    def down_executable_edge_available(self) -> bool:
        return self.model_probability_up is not None and self.down_ask is not None

    @property
    def up_fresh_executable_edge_available(self) -> bool:
        return (
            self.up_executable_edge_available
            and self.up_quote_age_sec is not None
            and self.up_quote_age_sec <= self.max_quote_age_sec
        )

    @property
    def down_fresh_executable_edge_available(self) -> bool:
        return (
            self.down_executable_edge_available
            and self.down_quote_age_sec is not None
            and self.down_quote_age_sec <= self.max_quote_age_sec
        )

    @property
    def fresh_executable_edge_available(self) -> bool:
        return (
            self.executable_edge_available
            and self.quote_age_sec is not None
            and self.quote_age_sec <= self.max_quote_age_sec
        )

    @property
    def edge_available(self) -> bool:
        """Backward-compatible alias for diagnostic availability."""
        return self.diagnostic_edge_available

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
            "fee_buffer": as_float(self.fee_buffer),
            "slippage_buffer": as_float(self.slippage_buffer),
            "adverse_selection_buffer": as_float(self.adverse_selection_buffer),
            "model_error_buffer": as_float(self.model_error_buffer),
            "total_cost_buffer": as_float(self.total_cost_buffer),
            "quote_age_sec": as_float(self.quote_age_sec),
            "up_quote_age_sec": as_float(self.up_quote_age_sec),
            "down_quote_age_sec": as_float(self.down_quote_age_sec),
            "observed_quote_age_sec": as_float(self.quote_age_sec),
            "max_quote_age_sec": as_float(self.max_quote_age_sec),
            "up_edge_vs_mid": as_float(self.up_edge_vs_mid),
            "up_edge_vs_ask": as_float(self.up_edge_vs_ask),
            "down_edge_vs_ask": as_float(self.down_edge_vs_ask),
            "up_net_edge_vs_ask": as_float(self.up_net_edge_vs_ask),
            "down_net_edge_vs_ask": as_float(self.down_net_edge_vs_ask),
            "edge_available": self.edge_available,
            "diagnostic_edge_available": self.diagnostic_edge_available,
            "executable_edge_available": self.executable_edge_available,
            "fresh_executable_edge_available": self.fresh_executable_edge_available,
            "up_executable_edge_available": self.up_executable_edge_available,
            "down_executable_edge_available": self.down_executable_edge_available,
            "up_fresh_executable_edge_available": self.up_fresh_executable_edge_available,
            "down_fresh_executable_edge_available": self.down_fresh_executable_edge_available,
        }


def _probability(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    value = Decimal(str(value))
    return value if Decimal("0") <= value <= Decimal("1") else None


def _price(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    value = Decimal(str(value))
    return value if Decimal("0") < value <= Decimal("1") else None


def _subtract(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None or right is None:
        return None
    return left - right


def _buffer(value: Decimal | None) -> Decimal:
    return max(Decimal("0"), Decimal(str(value or "0")))


def build_edge_state(
    *,
    model_probability_up: Decimal | None,
    market_mid: Decimal | None,
    up_bid: Decimal | None,
    up_ask: Decimal | None,
    down_bid: Decimal | None,
    down_ask: Decimal | None,
    total_cost_buffer: Decimal | None = None,
    fee_buffer: Decimal | None = None,
    slippage_buffer: Decimal | None = None,
    adverse_selection_buffer: Decimal | None = None,
    model_error_buffer: Decimal | None = None,
    quote_age_sec: Decimal | None = None,
    up_quote_age_sec: Decimal | None = None,
    down_quote_age_sec: Decimal | None = None,
    max_quote_age_sec: Decimal = Decimal("2"),
) -> EdgeState:
    model = _probability(model_probability_up)
    market = _probability(market_mid)
    up_bid = _price(up_bid)
    up_ask = _price(up_ask)
    down_bid = _price(down_bid)
    down_ask = _price(down_ask)

    fee = _buffer(fee_buffer)
    slippage = _buffer(slippage_buffer)
    adverse = _buffer(adverse_selection_buffer)
    model_error = _buffer(model_error_buffer)
    component_total = fee + slippage + adverse + model_error
    legacy_total = _buffer(total_cost_buffer)
    cost = max(component_total, legacy_total)
    def _age(value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        parsed = Decimal(str(value))
        return parsed if parsed >= 0 else None

    age = _age(quote_age_sec)
    up_age = _age(up_quote_age_sec if up_quote_age_sec is not None else quote_age_sec)
    down_age = _age(down_quote_age_sec if down_quote_age_sec is not None else quote_age_sec)
    max_age = max(Decimal("0"), Decimal(str(max_quote_age_sec)))

    up_edge_vs_mid = _subtract(model, market)
    down_edge_vs_ask = _subtract(
        (Decimal("1") - model) if model is not None else None,
        down_ask,
    )
    up_edge_vs_ask = _subtract(model, up_ask)
    up_net_edge_vs_ask = up_edge_vs_ask - cost if up_edge_vs_ask is not None else None
    down_net_edge_vs_ask = down_edge_vs_ask - cost if down_edge_vs_ask is not None else None

    return EdgeState(
        model_probability_up=model,
        market_probability_up=market,
        up_bid=up_bid,
        up_ask=up_ask,
        down_bid=down_bid,
        down_ask=down_ask,
        fee_buffer=fee,
        slippage_buffer=slippage,
        adverse_selection_buffer=adverse,
        model_error_buffer=model_error,
        total_cost_buffer=cost,
        quote_age_sec=age,
        up_quote_age_sec=up_age,
        down_quote_age_sec=down_age,
        max_quote_age_sec=max_age,
        up_edge_vs_mid=up_edge_vs_mid,
        up_edge_vs_ask=up_edge_vs_ask,
        down_edge_vs_ask=down_edge_vs_ask,
        up_net_edge_vs_ask=up_net_edge_vs_ask,
        down_net_edge_vs_ask=down_net_edge_vs_ask,
    )
