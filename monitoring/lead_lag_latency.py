"""Non-blocking latency-span recorder for Outcome lead/lag research."""
from __future__ import annotations

import time


def monotonic_ns() -> int:
    return time.perf_counter_ns()


def record_span(db, *, run_id: str, name: str, started_monotonic_ns: int, client_order_id: str = "") -> None:
    db.enqueue_latency(run_id=run_id, client_order_id=client_order_id, name=name, started_monotonic_ns=started_monotonic_ns, ended_monotonic_ns=monotonic_ns(), created_epoch_ns=time.time_ns())
