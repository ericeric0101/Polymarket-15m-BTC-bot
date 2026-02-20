"""
Simple adaptive parameter tuner for maker strategy controls.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Any


@dataclass
class TunerBounds:
    min_half_spread: Decimal = Decimal("0.002")
    max_half_spread: Decimal = Decimal("0.05")
    min_expected_net: Decimal = Decimal("0.00005")
    max_expected_net: Decimal = Decimal("0.01")


class ParameterTuner:
    def __init__(self, bounds: TunerBounds | None = None) -> None:
        self.bounds = bounds or TunerBounds()

    def suggest(self, current_half_spread: Decimal, current_min_expected_net: Decimal, metrics: Dict[str, Any]) -> Dict[str, Decimal]:
        quotes = float(metrics.get("quotes_submitted", 0))
        fills = float(metrics.get("fills", 0))
        denied = float(metrics.get("denied", 0))
        fill_rate = (fills / quotes) if quotes > 0 else 0.0
        deny_rate = (denied / quotes) if quotes > 0 else 0.0

        new_half = current_half_spread
        new_min_net = current_min_expected_net

        # Too many denies -> widen and demand higher expected net.
        if deny_rate > 0.20:
            new_half *= Decimal("1.10")
            new_min_net *= Decimal("1.15")
        # Too few fills with high quoting activity -> tighten slightly.
        elif quotes > 100 and fill_rate < 0.01:
            new_half *= Decimal("0.95")
        # Healthy fill rate and net positive -> can keep or slightly raise threshold.
        elif fill_rate > 0.08:
            new_min_net *= Decimal("1.02")

        new_half = max(self.bounds.min_half_spread, min(self.bounds.max_half_spread, new_half))
        new_min_net = max(self.bounds.min_expected_net, min(self.bounds.max_expected_net, new_min_net))
        return {
            "maker_half_spread": new_half,
            "maker_min_expected_net_usdc": new_min_net,
        }
