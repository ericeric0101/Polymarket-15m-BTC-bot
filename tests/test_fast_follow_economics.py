from decimal import Decimal

from bot.fast_follow_economics import evaluate_fast_follow_economics


def test_fast_follow_economics_requires_positive_resolution_ev_after_taker_fee_and_penalty():
    allowed = evaluate_fast_follow_economics(
        fair_price=Decimal("0.72"), limit_price=Decimal("0.61"), quantity=Decimal("10"),
        adverse_markout_per_share=Decimal("0.02"), min_expected_net_usdc=Decimal("0.0001"),
    )
    blocked = evaluate_fast_follow_economics(
        fair_price=Decimal("0.62"), limit_price=Decimal("0.61"), quantity=Decimal("10"),
        adverse_markout_per_share=Decimal("0.02"), min_expected_net_usdc=Decimal("0.0001"),
    )

    assert allowed.allowed is True
    assert allowed.expected_net_usdc > 0
    assert blocked.allowed is False
    assert blocked.expected_net_usdc <= 0


def test_fast_follow_economics_fails_closed_without_empirical_penalty():
    result = evaluate_fast_follow_economics(
        fair_price=Decimal("0.90"), limit_price=Decimal("0.61"), quantity=Decimal("10"),
        adverse_markout_per_share=None, min_expected_net_usdc=Decimal("0.0001"),
    )

    assert result.allowed is False
    assert result.reason == "execution_penalty_unavailable"
