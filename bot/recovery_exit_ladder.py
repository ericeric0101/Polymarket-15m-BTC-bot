"""Pure state selection for the confirmed-invalidation exit ladder."""
from __future__ import annotations

from typing import Literal


RecoveryExitAction = Literal[
    "legacy_market",
    "passive_limit",
    "wait_passive",
    "cancel_passive",
    "limit_fak",
]


def recovery_exit_owns_sell_reservation(stage: str | None) -> bool:
    """Return whether the recovery ladder exclusively owns a SELL lifecycle.

    While a confirmed-invalidation exit is replacing a normal take-profit
    order, the regular quote loop must not recreate or requote that TP order.
    Either action reserves the same tokens and prevents the recovery ladder
    from submitting its replacement.
    """
    return stage in {
        "awaiting_existing_sell_cancel",
        "passive",
        "awaiting_passive_cancel",
        "aggressive",
    }


def select_recovery_exit_action(
    *,
    enabled: bool,
    stage: str | None,
    time_left_sec: float | None,
    passive_min_time_left_sec: float,
    passive_order_active: bool,
    passive_age_sec: float,
    passive_ttl_sec: float,
) -> RecoveryExitAction:
    """Choose execution mechanics without changing the recovery eligibility gate."""
    if not enabled:
        return "legacy_market"
    if stage == "passive":
        if passive_order_active and passive_age_sec < passive_ttl_sec:
            return "wait_passive"
        if passive_order_active:
            return "cancel_passive"
        return "limit_fak"
    if stage == "awaiting_passive_cancel":
        return "wait_passive" if passive_order_active else "limit_fak"
    if stage == "aggressive":
        return "limit_fak"
    if time_left_sec is not None and time_left_sec >= passive_min_time_left_sec:
        return "passive_limit"
    return "limit_fak"
