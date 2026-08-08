from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class QuoteAgeTelemetry:
    """Quote-age values for shadow observations only."""

    raw_age_sec: Decimal | None
    effective_age_sec: Decimal | None
    clock_skew_sec: Decimal | None
    tolerance_applied: bool


def build_quote_age_telemetry(
    *,
    observation_ts: float,
    quote_ts: float | None,
    clock_skew_tolerance_sec: Decimal | float = Decimal("0.25"),
) -> QuoteAgeTelemetry:
    """Normalize a quote event timestamp without hiding material clock errors.

    A small negative age can occur when an exchange timestamp is marginally ahead
    of the local receive clock. Larger negative values remain invalid. Callers
    should take ``observation_ts`` at the point the observation is emitted, not
    at the beginning of an asynchronous quote cycle.
    """
    if quote_ts is None:
        return QuoteAgeTelemetry(None, None, None, False)

    raw_age = Decimal(str(observation_ts)) - Decimal(str(quote_ts))
    tolerance = max(Decimal("0"), Decimal(str(clock_skew_tolerance_sec)))
    if raw_age >= 0:
        return QuoteAgeTelemetry(raw_age, raw_age, None, False)
    if raw_age >= -tolerance:
        return QuoteAgeTelemetry(raw_age, Decimal("0"), -raw_age, True)
    return QuoteAgeTelemetry(raw_age, None, -raw_age, False)
