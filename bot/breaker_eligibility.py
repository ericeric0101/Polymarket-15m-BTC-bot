"""Conditional absolute-loss breaker eligibility: the single shared predicate.

``ExitPolicyEngine.evaluate`` (production) and the STOP_TIMING legacy shadow
comparator (``bot.stop_timing_telemetry.absolute_breaker_components``) both call
these functions, so production and telemetry can never drift apart.  Pure.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

ABSOLUTE_FAST_PATH_TTE_SEC = 120.0
ABSOLUTE_PERSISTENCE_SEC = 15.0
ABSOLUTE_MIN_VOTES = 2


def strong_opposite_signal(*, signal_matches_position: bool, signal_side: str, signal_locked: bool,
                           signal_score: Decimal, thesis_min_score_abs: Decimal) -> bool:
    """A locked, explicit opposite-side signal with |score| >= the thesis bound."""
    signal_is_none = str(signal_side).upper() == "NONE"
    explicit_offside = (not signal_matches_position) and not signal_is_none
    return bool(explicit_offside and signal_locked and abs(signal_score) >= thesis_min_score_abs)


def adverse_trend_confirmed(*, locked_side_invalidated: bool, confirmed_adverse_exit_active: bool = False,
                            external_thesis_weakened: Optional[bool] = None,
                            external_offside_confirmed: Optional[bool] = None,
                            strong_opposite: bool = False) -> bool:
    return bool(
        locked_side_invalidated
        or confirmed_adverse_exit_active
        or external_thesis_weakened is True
        or external_offside_confirmed is True
        or strong_opposite
    )


def conditional_absolute_components(
    *, enabled: bool, min_hold_sec: float, loss_usdc: Decimal, hold_sec: float, avg_entry: Decimal,
    best_bid: Decimal, gross_if_exit: Decimal, net_if_exit: Decimal, time_left_sec: Optional[float],
    adverse_trend_confirmed: bool, adverse_persistence_sec: float, weakening_count: int,
    available_count: int,
) -> dict[str, bool]:
    """Every component of the conditional absolute-loss breaker plus ``eligible``."""
    comps = {
        "abs_min_hold": float(hold_sec) >= max(0.0, float(min_hold_sec)),
        "abs_price_adverse": bool(avg_entry > 0 and best_bid < avg_entry and gross_if_exit < 0),
        "abs_adverse_trend_confirmed": bool(adverse_trend_confirmed),
        "abs_loss_threshold": net_if_exit <= -abs(loss_usdc),
        "abs_tte_le_120": time_left_sec is not None and float(time_left_sec) <= ABSOLUTE_FAST_PATH_TTE_SEC,
        "abs_persistence_ge_15s": float(adverse_persistence_sec or 0.0) >= ABSOLUTE_PERSISTENCE_SEC,
        "abs_votes_ge_2_of_2": (int(weakening_count) >= ABSOLUTE_MIN_VOTES
                                and int(available_count) >= ABSOLUTE_MIN_VOTES),
    }
    comps["eligible"] = bool(
        enabled and comps["abs_min_hold"] and comps["abs_price_adverse"]
        and comps["abs_adverse_trend_confirmed"] and comps["abs_loss_threshold"]
        and (comps["abs_tte_le_120"] or (comps["abs_persistence_ge_15s"] and comps["abs_votes_ge_2_of_2"]))
    )
    return comps
