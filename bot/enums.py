"""
bot/enums.py – Shared enumerations extracted from run_bot.py.

Kept here so that both run_bot.py and bot/side_decision.py
(and future mixins) can safely import them without circular dependencies.
"""
from __future__ import annotations

from enum import Enum


class MarketPhase(Enum):
    """Market lifecycle phases for BTC 15-min markets."""
    WAITING = "WAITING"           # No active market; searching for next one
    ACTIVE = "ACTIVE"             # Market is live, quoting is allowed
    REDUCE_ONLY = "REDUCE_ONLY"   # Close to market end, BUY blocked
    SETTLING = "SETTLING"         # Market has ended, all orders cancelled


class ActiveSide(Enum):
    UP = "UP"
    DOWN = "DOWN"
    NONE = "NONE"
