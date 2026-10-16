"""Pure eligibility rule for the Chainlink-TWAP endgame protective exit."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class EndgameTwapExitDecision:
    eligible: bool
    reason: str
    twap_minus_strike: Decimal | None = None


def evaluate_endgame_twap_exit(
    *,
    enabled: bool,
    time_left_sec: float | None,
    max_time_left_sec: float,
    twap_price: Decimal | None,
    twap_source: str,
    twap_age_sec: float | None,
    max_twap_age_sec: float,
    strike: Decimal | None,
    strike_verified: bool,
    held_side: str,
    min_distance_usd: Decimal,
) -> EndgameTwapExitDecision:
    """Return whether a proven endgame TWAP disagreement must flatten a side.

    The settlement convention is UP when TWAP >= strike.  This is deliberately
    an exit-only rule: it has no authority to open or reverse a position.
    """
    if not enabled:
        return EndgameTwapExitDecision(False, "disabled")
    if time_left_sec is None or time_left_sec < 0 or time_left_sec > max_time_left_sec:
        return EndgameTwapExitDecision(False, "outside_time_window")
    if twap_price is None or not str(twap_source).startswith("polymarket_chainlink_twap_"):
        return EndgameTwapExitDecision(False, "twap_unavailable")
    if twap_age_sec is None or twap_age_sec < 0 or twap_age_sec > max_twap_age_sec:
        return EndgameTwapExitDecision(False, "twap_stale")
    if strike is None or strike <= 0 or not strike_verified:
        return EndgameTwapExitDecision(False, "strike_unverified")
    side = str(held_side or "").upper()
    if side not in {"UP", "DOWN"}:
        return EndgameTwapExitDecision(False, "held_side_unknown")
    delta = twap_price - strike
    if abs(delta) < min_distance_usd:
        return EndgameTwapExitDecision(False, "distance_below_minimum", delta)
    settlement_side = "UP" if delta >= 0 else "DOWN"
    if side == settlement_side:
        return EndgameTwapExitDecision(False, "twap_confirms_position", delta)
    return EndgameTwapExitDecision(True, "adverse_twap_endgame", delta)
