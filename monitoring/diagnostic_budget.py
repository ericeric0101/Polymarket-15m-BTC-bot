"""Byte budget for high-volume diagnostic journal events.

The trade journal is the exposure/risk authority and is copied as a full image
on every backup, yet most of its daily growth is diagnostics. Each named
diagnostic type gets a token bucket that refills evenly across the day, so the
retained rows remain a time-uniform sample instead of the first hours of a day.
Exposure, fill, risk and settlement events are never in these sets.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, Optional

# Types never read back by runtime code (verified by grep of journal loaders).
DIAGNOSTIC_STRATEGY_EVENTS = frozenset({
    "ENTRY_DECISION_TRACE", "QUOTE_TRANSPORT_TELEMETRY", "EVENT_LOOP_CONSUMER_TIMING",
    "SIDE_DECISION_OBSERVATION", "BUY_PATH_DIAGNOSTIC", "ENTRY_CONFIRMATION_OBSERVATION",
    "SMART_MONEY_OBSERVATION", "SHADOW_SIGNAL_CANDIDATE_LIVE", "MAIN_SIGNAL_CANDIDATE_LIVE",
    "ENTRY_REGIME_OBSERVATION",
})
DIAGNOSTIC_ORDER_EVENTS = frozenset({
    "ENTRY_EDGE_OBSERVATION", "DEPTH_RISK_SHADOW_MARKOUT", "DEPTH_RISK_SHADOW_CANDIDATE",
    "ORDER_OBSERVE_BUY_BLOCKED",
})
# Per-quote-loop skip reasons share one bucket.
DIAGNOSTIC_ORDER_PREFIX = "ORDER_SKIP_"
SUMMARY_EVENT = "DIAGNOSTIC_SAMPLING_SUMMARY"
DEFAULT_MB_PER_TYPE_DAY = 4.0


def diagnostic_bucket(table: str, event_type: str) -> Optional[str]:
    """Return the budget bucket for an event, or None when it must always persist."""
    if table == "strategy" and event_type in DIAGNOSTIC_STRATEGY_EVENTS:
        return event_type
    if table == "order":
        if event_type in DIAGNOSTIC_ORDER_EVENTS:
            return event_type
        if event_type.startswith(DIAGNOSTIC_ORDER_PREFIX):
            return DIAGNOSTIC_ORDER_PREFIX + "*"
    return None


class DiagnosticBudget:
    def __init__(self, *, bytes_per_day: float, clock: Callable[[], float] = time.monotonic,
                 summary_interval_sec: float = 3600.0) -> None:
        self.bytes_per_day = max(0.0, float(bytes_per_day))
        self.rate = self.bytes_per_day / 86400.0
        # One hour of burst keeps short bursts intact while bounding the day.
        self.capacity = self.bytes_per_day / 24.0
        self._clock = clock
        self._summary_interval_sec = float(summary_interval_sec)
        self._lock = threading.Lock()
        self._tokens: Dict[str, float] = {}
        self._updated: Dict[str, float] = {}
        self._dropped: Dict[str, list[int]] = {}
        self._last_summary = clock()

    @classmethod
    def from_env(cls) -> "DiagnosticBudget":
        try:
            mb = float(os.getenv("TRADE_JOURNAL_DIAGNOSTIC_MB_PER_TYPE_DAY", DEFAULT_MB_PER_TYPE_DAY))
        except ValueError:
            mb = DEFAULT_MB_PER_TYPE_DAY
        return cls(bytes_per_day=mb * 1024 * 1024)

    def allow(self, bucket: str, size: int) -> bool:
        if self.bytes_per_day <= 0:
            return True  # explicit opt-out: 0 disables sampling
        now = self._clock()
        with self._lock:
            tokens = self._tokens.get(bucket, self.capacity)
            tokens = min(self.capacity, tokens + (now - self._updated.get(bucket, now)) * self.rate)
            self._updated[bucket] = now
            if size <= tokens:
                self._tokens[bucket] = tokens - size
                return True
            self._tokens[bucket] = tokens
            dropped = self._dropped.setdefault(bucket, [0, 0])
            dropped[0] += 1
            dropped[1] += int(size)
            return False

    def take_summary(self) -> Optional[Dict[str, Any]]:
        """Return and reset drop counters once per summary interval."""
        now = self._clock()
        with self._lock:
            if now - self._last_summary < self._summary_interval_sec or not self._dropped:
                return None
            self._last_summary = now
            dropped, self._dropped = self._dropped, {}
        return {
            "bytes_per_type_day": self.bytes_per_day,
            "dropped": {bucket: {"rows": rows, "bytes": size} for bucket, (rows, size) in sorted(dropped.items())},
        }
