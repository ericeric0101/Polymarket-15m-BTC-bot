"""Immutable data exchanged by the event-driven Outcome lead/lag subsystem."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ReferenceSource = Literal["outcome_btc_mark", "polymarket_twap", "polymarket_spot", "binance", "polymarket_bbo"]
LeadLagStateName = Literal[
    "unavailable", "observe", "supports_position", "adverse_candidate",
    "adverse_confirmed", "follower_wait", "follower_confirmed",
]


@dataclass(frozen=True)
class ReferenceTick:
    source: ReferenceSource
    price_cents: int
    received_epoch_ns: int
    received_monotonic_ns: int
    source_event_ts_ms: int | None = None
    run_id: str = ""
    slug: str = ""
    market_id: int | None = None
    connection_epoch: int = 0
    bid_cents: int | None = None
    ask_cents: int | None = None
    bid_size_e6: int | None = None
    ask_size_e6: int | None = None


@dataclass(frozen=True)
class LeadLagDecision:
    state: LeadLagStateName
    direction: int
    outcome_return_cents: int
    residual_cents: int
    persistence: int
    feature_version: str
    decision_monotonic_ns: int
    reason: str
    window_returns_cents: tuple[tuple[int, int | None], ...] = ()
    # ``residual_cents`` is relative to the rolling Outcome--TWAP basis, not
    # the raw cross-venue level difference.  Retain both values so research
    # can audit the calibration instead of silently treating venue basis as
    # a lead/lag signal.
    raw_residual_cents: int = 0
    baseline_cents: int | None = None
    follower_price_cents: int | None = None
    # Actual elapsed time of the Outcome return used for the shock score.
    # The value is persisted with the decision so live cadence can be audited.
    outcome_interval_ms: int | None = None
    low_confidence: bool = False


@dataclass(frozen=True)
class LeadLagCandidate:
    decision: LeadLagDecision
    run_id: str
    slug: str
    market_id: int | None
    created_epoch_ns: int


@dataclass(frozen=True)
class LatencySpan:
    name: str
    started_monotonic_ns: int
    ended_monotonic_ns: int
    run_id: str = ""
    client_order_id: str = ""

    @property
    def elapsed_ns(self) -> int:
        return max(0, self.ended_monotonic_ns - self.started_monotonic_ns)
