"""Pure, fail-closed state machine for Outcome BTC-mark lead/lag research."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from bot.outcome_lead_lag_types import LeadLagDecision, ReferenceTick


@dataclass(frozen=True)
class OutcomeLeadLagStateConfig:
    feature_version: str = "outcome_lead_lag_v1"
    max_source_age_ms: int = 1_000
    shock_cents: int = 500
    residual_cents: int = 300
    debounce_ticks: int = 2
    windows_ms: tuple[int, ...] = (250, 1_000, 5_000, 10_000)


class OutcomeLeadLagState:
    """Consumes ticks only; it has no strategy, I/O, wall-clock, or order access."""

    def __init__(self, config: OutcomeLeadLagStateConfig | None = None) -> None:
        self.config = config or OutcomeLeadLagStateConfig()
        self._latest: dict[str, ReferenceTick] = {}
        self._history: dict[str, deque[ReferenceTick]] = defaultdict(lambda: deque(maxlen=512))
        self._persistence = 0
        self._last_direction = 0

    def _return_for_window(self, source: str, tick: ReferenceTick, window_ms: int) -> int | None:
        history = self._history[source]
        target_ns = tick.received_monotonic_ns - window_ms * 1_000_000
        prior = next((item for item in reversed(history) if item.received_monotonic_ns <= target_ns), None)
        return None if prior is None else tick.price_cents - prior.price_cents

    def _unavailable(self, tick: ReferenceTick, reason: str) -> LeadLagDecision:
        self._persistence = 0
        self._last_direction = 0
        return LeadLagDecision("unavailable", 0, 0, 0, 0, self.config.feature_version, tick.received_monotonic_ns, reason)

    def apply(self, tick: ReferenceTick) -> LeadLagDecision:
        previous = self._latest.get(tick.source)
        if previous is not None and tick.received_monotonic_ns <= previous.received_monotonic_ns:
            return self._unavailable(tick, "out_of_order")
        self._latest[tick.source] = tick
        self._history[tick.source].append(tick)
        outcome, twap = self._latest.get("outcome_btc_mark"), self._latest.get("polymarket_twap")
        if outcome is None or twap is None:
            return self._unavailable(tick, "missing_reference")
        if abs(outcome.received_monotonic_ns - twap.received_monotonic_ns) > self.config.max_source_age_ms * 1_000_000:
            return self._unavailable(tick, "stale_reference")
        if outcome.connection_epoch != tick.connection_epoch and tick.source == "outcome_btc_mark":
            return self._unavailable(tick, "cross_epoch")
        outcome_return = self._return_for_window("outcome_btc_mark", outcome, 1_000)
        window_returns = tuple((window, self._return_for_window("outcome_btc_mark", outcome, window)) for window in self.config.windows_ms)
        if outcome_return is None:
            return LeadLagDecision("observe", 0, 0, outcome.price_cents - twap.price_cents, 0, self.config.feature_version, tick.received_monotonic_ns, "insufficient_history", window_returns)
        residual = outcome.price_cents - twap.price_cents
        direction = 1 if outcome_return > 0 else -1 if outcome_return < 0 else 0
        qualifying = direction and abs(outcome_return) >= self.config.shock_cents and abs(residual) >= self.config.residual_cents and (residual > 0) == (direction > 0)
        if not qualifying:
            self._persistence, self._last_direction = 0, 0
            return LeadLagDecision("observe", direction, outcome_return, residual, 0, self.config.feature_version, tick.received_monotonic_ns, "no_untranslated_shock", window_returns)
        self._persistence = self._persistence + 1 if direction == self._last_direction else 1
        self._last_direction = direction
        state = "adverse_confirmed" if self._persistence >= self.config.debounce_ticks else "adverse_candidate"
        return LeadLagDecision(state, direction, outcome_return, residual, self._persistence, self.config.feature_version, tick.received_monotonic_ns, "outcome_shock_untranslated", window_returns)
