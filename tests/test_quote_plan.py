from decimal import Decimal

from bot.quote_service import parse_quote_plan


def test_parse_quote_plan_names_tuple_fields_and_units():
    plan = parse_quote_plan(("0.42", "5", True, "0.10", "0.01", "0.02", "0.10", "0.44", "0.001", "0.002"))

    assert plan.price == Decimal("0.42")
    assert plan.quantity == Decimal("5")
    assert plan.fee_per_share == Decimal("0.001")
    assert plan.other_cost_per_share == Decimal("0.002")


def test_parse_quote_plan_rejects_short_tuple():
    assert parse_quote_plan(("0.42",)) is None
