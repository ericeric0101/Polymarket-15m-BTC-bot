"""Conservative, synchronous economics gate for Outcome FOK entries."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from execution.rebate_model import estimate_taker_fee_usdc


@dataclass(frozen=True)
class FastFollowEconomics:
    allowed: bool
    reason: str
    resolution_ev_usdc: Decimal
    taker_fee_usdc: Decimal
    execution_penalty_usdc: Decimal
    expected_net_usdc: Decimal


def evaluate_fast_follow_economics(*, fair_price: Decimal, limit_price: Decimal,
                                  quantity: Decimal, adverse_markout_per_share: Decimal | None,
                                  min_expected_net_usdc: Decimal) -> FastFollowEconomics:
    if adverse_markout_per_share is None or adverse_markout_per_share < 0:
        return FastFollowEconomics(False, "execution_penalty_unavailable", Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
    if not (Decimal("0") < fair_price <= Decimal("1")) or limit_price <= 0 or quantity <= 0:
        return FastFollowEconomics(False, "invalid_economics_inputs", Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
    resolution_ev = quantity * (fair_price - limit_price)
    taker_fee = estimate_taker_fee_usdc(shares=quantity, probability=limit_price)
    penalty = quantity * adverse_markout_per_share
    expected_net = resolution_ev - taker_fee - penalty
    allowed = expected_net >= min_expected_net_usdc
    return FastFollowEconomics(
        allowed, "allowed" if allowed else "expected_net_below_minimum",
        resolution_ev, taker_fee, penalty, expected_net,
    )
