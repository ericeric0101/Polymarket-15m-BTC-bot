"""Conservative probability calibration for short-dated binary entries.

The digital pricer is useful as a directional model, but raw probabilities are
not calibrated execution probabilities.  This module intentionally shrinks the
model toward the tradable market mid before an entry gate consumes it.
"""
from __future__ import annotations

from decimal import Decimal


def clamp_probability(value: Decimal) -> Decimal:
    return max(Decimal("0.001"), min(Decimal("0.999"), Decimal(str(value))))


def calibrate_probability(
    *,
    raw_probability: Decimal,
    market_mid: Decimal,
    side: str,
    enabled: bool,
    up_model_weight: Decimal,
    down_model_weight: Decimal,
) -> Decimal:
    """Shrink a raw model probability toward executable market consensus.

    A weight of 1 preserves the model; 0 uses the market mid.  The separate
    DOWN weight reflects the observed asymmetric overconfidence and is fully
    configurable rather than hard-coded into the pricer.
    """
    raw = clamp_probability(raw_probability)
    mid = clamp_probability(market_mid)
    if not enabled:
        return raw
    weight = up_model_weight if str(side).upper() == "UP" else down_model_weight
    weight = max(Decimal("0"), min(Decimal("1"), Decimal(str(weight))))
    return clamp_probability(mid + weight * (raw - mid))


def settlement_ev_per_share(
    *,
    calibrated_probability: Decimal,
    entry_price: Decimal,
    fee_per_share: Decimal = Decimal("0"),
) -> Decimal:
    """Estimate binary settlement EV for one purchased share.

    This is deliberately a *settlement* metric, rather than the live
    executable economics gate.  It assumes a filled BUY paying one USDC on a
    correct settlement and zero otherwise, then subtracts the supplied quote
    fee assumption.  Execution risk, fill probability, markout, exits, and
    rebates are intentionally excluded so research can compare this value to
    the separately recorded ``robust_net_usdc``.
    """
    probability = clamp_probability(calibrated_probability)
    price = Decimal(str(entry_price))
    fee = max(Decimal("0"), Decimal(str(fee_per_share)))
    return probability - price - fee


def fractional_kelly_stake_fraction(
    *,
    probability: Decimal,
    entry_price: Decimal,
    fraction: Decimal,
) -> Decimal:
    """Return a capped fractional-Kelly fraction of collateral to spend.

    For a binary share costing ``c`` and paying one on success, full Kelly is
    ``(p - c) / (1 - c)``.  Non-positive edge deliberately produces zero.
    """
    p = clamp_probability(probability)
    c = Decimal(str(entry_price))
    if c <= 0 or c >= 1 or p <= c or fraction <= 0:
        return Decimal("0")
    full = (p - c) / (Decimal("1") - c)
    return max(Decimal("0"), full * Decimal(str(fraction)))
