from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ExitStage(Enum):
    PASSIVE = "PASSIVE"
    AGGRESSIVE = "AGGRESSIVE"
    TAKER = "TAKER"


@dataclass
class ExitPolicyConfig:
    aggressive_stage_sec: int
    taker_stage_sec: int


class ExitPolicy:
    """
    Time-to-close driven exit policy.
    - PASSIVE: let maker SELL quotes work.
    - AGGRESSIVE: tighten sell behavior but still avoid taker unless needed.
    - TAKER: allow taker fail-safe for guaranteed flattening.
    """

    def __init__(self, config: ExitPolicyConfig) -> None:
        self.config = config

    def stage(self, time_left_sec: Optional[float]) -> ExitStage:
        if time_left_sec is None:
            return ExitStage.PASSIVE
        if time_left_sec <= float(self.config.taker_stage_sec):
            return ExitStage.TAKER
        if time_left_sec <= float(self.config.aggressive_stage_sec):
            return ExitStage.AGGRESSIVE
        return ExitStage.PASSIVE
