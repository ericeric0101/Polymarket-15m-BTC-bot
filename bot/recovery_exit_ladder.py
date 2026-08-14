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
