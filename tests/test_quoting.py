from decimal import Decimal

from bot.quoting import set_side_should_quote


def test_set_side_should_quote_preserves_execution_penalty_components():
    components = {"vwap_usdc": Decimal("0.24"), "non_atomic_usdc": Decimal("0.03")}
    side_plan = {
        "buy": (
            Decimal("0.50"),
            object(),
            True,
            Decimal("0.10"),
            Decimal("0.30"),
            Decimal("0.02"),
            Decimal("0.12"),
            Decimal("0.52"),
            Decimal("0.01"),
            Decimal("0.02"),
            components,
        )
    }
    reasons: dict[str, str] = {}

    set_side_should_quote(side_plan, reasons, "buy", False, "edge_gate_buy")

    assert len(side_plan["buy"]) == 11
    assert side_plan["buy"][2] is False
    assert side_plan["buy"][10] == components
    assert reasons == {"buy": "edge_gate_buy"}
