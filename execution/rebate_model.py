"""
Maker rebate and taker fee economics for fee-enabled Polymarket markets.

Polymarket Fee Model (as of 2026 Q1)
--------------------------------------
- **Maker fills**: fee rate is effectively 0 USDC. The bot pays no fee when its
  limit orders are filled as the passive side.
- **Taker fills (IOC/Market)**: non-zero taker fee applies.  Do NOT use this
  model for taker-exit cost estimation — use the actual taker fee rate instead.
- **GM Liquidity Rewards**: makers receive pro-rata rewards from the Polymarket
  liquidity pool.  These are NOT captured in this module; they are accrued
  off-chain and settled separately.

The `fee_rate` parameter in `FeeCurveConfig` is the *shape parameter* of the
Polymarket S-curve used to compute the **rebate weight** — it is NOT a fee
being charged.  When `fee_rate_override=0` is passed to `estimate_quote_economics`
(via MAKER_ECON_FEE_RATE_DECIMAL=0), the `expected_rebate_usdc` will be 0,
which is correct for Maker orders if you are ignoring the GM reward channel.
This makes `expected_net_usdc` purely spread-capture-based — a conservative
but safe assumption.
"""
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class FeeCurveConfig:
    """
    Fee-curve parameters for a market type.
    """
    fee_rate: Decimal
    exponent: Decimal
    maker_rebate_share: Decimal


@dataclass
class QuoteEconomics:
    """
    Per-quote economics snapshot.
    """
    shares: Decimal
    probability: Decimal
    fee_equivalent_usdc: Decimal
    expected_rebate_usdc: Decimal
    expected_spread_capture_usdc: Decimal
    expected_net_usdc: Decimal


# 5-min and 15-min crypto markets.
CRYPTO_FEE_CURVE = FeeCurveConfig(
    fee_rate=Decimal(os.getenv("REBATE_FEE_RATE", "0.25")),
    exponent=Decimal(os.getenv("REBATE_EXPONENT", "2")),
    maker_rebate_share=Decimal(os.getenv("MAKER_REBATE_SHARE", "0.20")),
)

# Official Polymarket taker fee schedule for crypto markets.
OFFICIAL_CRYPTO_TAKER_FEE_CURVE = FeeCurveConfig(
    fee_rate=Decimal("0.072"),
    exponent=Decimal("1"),
    maker_rebate_share=Decimal("0"),
)


def bps_to_fee_rate(bps: int) -> Decimal:
    """
    Convert basis points to decimal fee rate.
    """
    return Decimal(str(bps)) / Decimal("10000")


def _clamp_probability(p: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), p))


def estimate_fee_equivalent_usdc(
    shares: Decimal,
    probability: Decimal,
    config: FeeCurveConfig = CRYPTO_FEE_CURVE,
    fee_rate_override: Optional[Decimal] = None,
) -> Decimal:
    """
    fee_equivalent = C × p × feeRate × (p × (1 - p))^exponent
    """
    p = _clamp_probability(probability)
    shape = (p * (Decimal("1") - p)) ** config.exponent
    fee_rate = fee_rate_override if fee_rate_override is not None else config.fee_rate
    return shares * p * fee_rate * shape


def estimate_quote_economics(
    quote_size_usdc: Decimal,
    probability: Decimal,
    half_spread: Decimal,
    adverse_selection_buffer: Decimal = Decimal("0"),
    config: FeeCurveConfig = CRYPTO_FEE_CURVE,
    fee_rate_override: Optional[Decimal] = None,
) -> QuoteEconomics:
    """
    Estimate maker quote economics in USDC terms.

    When fee_rate_override=0 (MAKER_ECON_FEE_RATE_DECIMAL=0):
    - fee_equivalent_usdc = 0  (correct: Maker pays no fee on Polymarket)
    - expected_rebate_usdc = 0  (conservative: ignores off-chain GM rewards)
    - expected_net_usdc = expected_spread_capture - adverse_selection_buffer
    This is the intended conservative baseline for economic gating.
    """
    p = _clamp_probability(probability)
    shares = quote_size_usdc / p if p > 0 else Decimal("0")
    fee_equivalent = estimate_fee_equivalent_usdc(
        shares=shares,
        probability=p,
        config=config,
        fee_rate_override=fee_rate_override,
    )
    expected_rebate = fee_equivalent * config.maker_rebate_share
    expected_spread_capture = shares * half_spread
    expected_net = expected_spread_capture + expected_rebate - adverse_selection_buffer
    return QuoteEconomics(
        shares=shares,
        probability=p,
        fee_equivalent_usdc=fee_equivalent,
        expected_rebate_usdc=expected_rebate,
        expected_spread_capture_usdc=expected_spread_capture,
        expected_net_usdc=expected_net,
    )


def estimate_taker_fee_usdc(
    shares: Decimal,
    probability: Decimal,
    config: FeeCurveConfig = OFFICIAL_CRYPTO_TAKER_FEE_CURVE,
) -> Decimal:
    """
    Official Polymarket taker fee in USDC.
    """
    return estimate_fee_equivalent_usdc(
        shares=shares,
        probability=probability,
        config=config,
    )


def estimate_taker_buy_fee_shares(
    shares: Decimal,
    probability: Decimal,
    config: FeeCurveConfig = OFFICIAL_CRYPTO_TAKER_FEE_CURVE,
) -> Decimal:
    """
    For BUY taker fills, Polymarket collects the fee in shares.
    """
    p = _clamp_probability(probability)
    fee_usdc = estimate_taker_fee_usdc(
        shares=shares,
        probability=p,
        config=config,
    )
    if p <= 0:
        return Decimal("0")
    return fee_usdc / p
