"""Pure, fail-closed state machine for Outcome BTC-mark lead/lag research."""
from __future__ import annotations

from collections import defaultdict, deque
from statistics import median
from dataclasses import dataclass

from bot.outcome_lead_lag_types import LeadLagDecision, ReferenceTick


@dataclass(frozen=True)
class OutcomeLeadLagStateConfig:
    feature_version: str = "outcome_lead_lag_v2"
    max_source_age_ms: int = 1_000
    shock_cents: int = 500
    residual_cents: int = 300
    debounce_ticks: int = 2
    windows_ms: tuple[int, ...] = (250, 1_000, 5_000, 10_000)
    baseline_window_samples: int = 120
    baseline_warmup_samples: int = 30
    follower_confirm_window_ms: int = 5_000
    follower_confirm_cents: int = 100


class OutcomeLeadLagState:
    """Consumes ticks only; it has no strategy, I/O, wall-clock, or order access."""

    def __init__(self, config: OutcomeLeadLagStateConfig | None = None) -> None:
        self.config = config or OutcomeLeadLagStateConfig()
        self._latest: dict[str, ReferenceTick] = {}
        self._history: dict[str, deque[ReferenceTick]] = defaultdict(lambda: deque(maxlen=512))
        self._persistence = 0
        self._last_direction = 0
        self._basis_samples: deque[int] = deque(maxlen=max(1, self.config.baseline_window_samples))
        self._last_basis_outcome_ns: int | None = None
        self._armed_direction = 0
        self._armed_monotonic_ns: int | None = None
        self._armed_follower_price_cents: int | None = None

    def _return_for_window(self, source: str, tick: ReferenceTick, window_ms: int) -> int | None:
        history = self._history[source]
        target_ns = tick.received_monotonic_ns - window_ms * 1_000_000
        prior = next((item for item in reversed(history) if item.received_monotonic_ns <= target_ns), None)
        return None if prior is None else tick.price_cents - prior.price_cents

    def _unavailable(self, tick: ReferenceTick, reason: str) -> LeadLagDecision:
        self._persistence = 0
        self._last_direction = 0
        self._armed_direction = 0
        self._armed_monotonic_ns = None
        self._armed_follower_price_cents = None
        return LeadLagDecision("unavailable", 0, 0, 0, 0, self.config.feature_version, tick.received_monotonic_ns, reason)

    def _update_basis(self, outcome: ReferenceTick, twap: ReferenceTick) -> int | None:
        """Return the robust raw Outcome--TWAP basis after one new Outcome mark.

        The two references are different venue feeds and can legitimately have
        a sizeable static level difference.  Only the deviation from their
        recent basis is a possible untranslated move.
        """
        # Score against the basis known *before* the current Outcome update.
        # Otherwise the event being tested would immediately calibrate itself
        # away, especially with a short warm-up in deterministic tests.
        baseline = (
            int(median(self._basis_samples))
            if len(self._basis_samples) >= self.config.baseline_warmup_samples
            else None
        )
        if self._last_basis_outcome_ns != outcome.received_monotonic_ns:
            self._basis_samples.append(outcome.price_cents - twap.price_cents)
            self._last_basis_outcome_ns = outcome.received_monotonic_ns
        return baseline

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

        # A live fast-follow signal is complete only after the authoritative
        # Chainlink TWAP moves in the same direction after the Outcome shock.
        # Freeze the follower level when the shock is armed so an older TWAP
        # move cannot be mistaken for confirmation.
        if self._armed_direction and self._armed_monotonic_ns is not None:
            elapsed_ms = (tick.received_monotonic_ns - self._armed_monotonic_ns) // 1_000_000
            if elapsed_ms > self.config.follower_confirm_window_ms:
                self._armed_direction = 0
                self._armed_monotonic_ns = None
                self._armed_follower_price_cents = None
            elif tick.source == "polymarket_twap" and self._armed_follower_price_cents is not None:
                follower_move = tick.price_cents - self._armed_follower_price_cents
                confirms = (
                    abs(follower_move) >= self.config.follower_confirm_cents
                    and (follower_move > 0) == (self._armed_direction > 0)
                )
                if confirms:
                    direction = self._armed_direction
                    self._armed_direction = 0
                    self._armed_monotonic_ns = None
                    self._armed_follower_price_cents = None
                    return LeadLagDecision(
                        "follower_confirmed", direction, 0, 0,
                        self._persistence, self.config.feature_version,
                        tick.received_monotonic_ns, "twap_followed_outcome",
                        follower_price_cents=tick.price_cents,
                    )
                return LeadLagDecision(
                    "follower_wait", self._armed_direction, 0, 0,
                    self._persistence, self.config.feature_version,
                    tick.received_monotonic_ns, "awaiting_twap_follow_through",
                    follower_price_cents=tick.price_cents,
                )
            else:
                return LeadLagDecision(
                    "follower_wait", self._armed_direction, 0, 0,
                    self._persistence, self.config.feature_version,
                    tick.received_monotonic_ns, "awaiting_twap_follow_through",
                    follower_price_cents=twap.price_cents,
                )
        raw_residual = outcome.price_cents - twap.price_cents
        baseline = self._update_basis(outcome, twap)
        if baseline is None:
            return LeadLagDecision("observe", 0, 0, 0, 0, self.config.feature_version,
                                   tick.received_monotonic_ns, "basis_warmup",
                                   raw_residual_cents=raw_residual,
                                   follower_price_cents=twap.price_cents)
        outcome_return = self._return_for_window("outcome_btc_mark", outcome, 1_000)
        window_returns = tuple((window, self._return_for_window("outcome_btc_mark", outcome, window)) for window in self.config.windows_ms)
        residual = raw_residual - baseline
        if outcome_return is None:
            return LeadLagDecision("observe", 0, 0, residual, 0, self.config.feature_version,
                                   tick.received_monotonic_ns, "insufficient_history", window_returns,
                                   raw_residual, baseline, twap.price_cents)
        direction = 1 if outcome_return > 0 else -1 if outcome_return < 0 else 0
        if tick.source != "outcome_btc_mark":
            return LeadLagDecision(
                "observe", direction, outcome_return, residual, self._persistence,
                self.config.feature_version, tick.received_monotonic_ns,
                "waiting_for_outcome_shock", window_returns, raw_residual,
                baseline, twap.price_cents,
            )
        qualifying = direction and abs(outcome_return) >= self.config.shock_cents and abs(residual) >= self.config.residual_cents and (residual > 0) == (direction > 0)
        if not qualifying:
            self._persistence, self._last_direction = 0, 0
            return LeadLagDecision("observe", direction, outcome_return, residual, 0,
                                   self.config.feature_version, tick.received_monotonic_ns,
                                   "no_untranslated_shock", window_returns, raw_residual,
                                   baseline, twap.price_cents)
        self._persistence = self._persistence + 1 if direction == self._last_direction else 1
        self._last_direction = direction
        state = "adverse_confirmed" if self._persistence >= self.config.debounce_ticks else "adverse_candidate"
        if state == "adverse_confirmed":
            self._armed_direction = direction
            self._armed_monotonic_ns = tick.received_monotonic_ns
            self._armed_follower_price_cents = twap.price_cents
        return LeadLagDecision(state, direction, outcome_return, residual, self._persistence,
                               self.config.feature_version, tick.received_monotonic_ns,
                               "outcome_shock_untranslated", window_returns, raw_residual,
                               baseline, twap.price_cents)
