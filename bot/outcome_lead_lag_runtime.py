"""Bounded serial shadow runtime; never owns execution."""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import asdict
from typing import Callable

from bot.outcome_lead_lag_state import OutcomeLeadLagState, OutcomeLeadLagStateConfig
from bot.outcome_lead_lag_types import LeadLagCandidate, ReferenceTick


class OutcomeLeadLagRuntime:
    def __init__(self, *, config: OutcomeLeadLagStateConfig, db, candidate_handler: Callable[[LeadLagCandidate], None] | None = None, tick_handler: Callable[[ReferenceTick, object], None] | None = None) -> None:
        self._state, self._db, self._candidate_handler, self._tick_handler = OutcomeLeadLagState(config), db, candidate_handler, tick_handler
        self._queue: queue.Queue[ReferenceTick] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped_ticks = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True, name="outcome-lead-lag-runtime")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def publish(self, tick: ReferenceTick) -> None:
        try:
            self._queue.put_nowait(tick)
        except queue.Full:
            self.dropped_ticks += 1

    def _serve(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                tick = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            decision = self._state.apply(tick)
            decision_monotonic_ns = time.perf_counter_ns()
            decision_epoch_ns = time.time_ns()
            if hasattr(self._db, "enqueue_latency"):
                self._db.enqueue_latency(
                    run_id=tick.run_id, client_order_id="", name=f"{tick.source}_to_decision",
                    started_monotonic_ns=tick.received_monotonic_ns,
                    ended_monotonic_ns=decision_monotonic_ns, created_epoch_ns=decision_epoch_ns,
                )
            self._db.enqueue_reference_1s(
                run_id=tick.run_id, slug=tick.slug, market_id=tick.market_id,
                bucket_epoch_ms=(tick.received_epoch_ns // 1_000_000 // 1_000) * 1_000,
                source=tick.source, price_cents=tick.price_cents, received_epoch_ns=tick.received_epoch_ns,
            )
            self._db.enqueue_decision(
                run_id=tick.run_id, slug=tick.slug, market_id=tick.market_id,
                decision_epoch_ns=decision_epoch_ns, payload={**asdict(decision), "source": tick.source},
            )
            if self._tick_handler is not None:
                self._tick_handler(tick, decision)
            if decision.state in {"adverse_candidate", "adverse_confirmed"} and self._candidate_handler is not None:
                self._candidate_handler(LeadLagCandidate(decision, tick.run_id, tick.slug, tick.market_id, decision_epoch_ns))
