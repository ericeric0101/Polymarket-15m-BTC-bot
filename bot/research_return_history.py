"""Time-bounded, thread-safe buffer for the research BTC return features.

The Binance aggTrade feed thread appends one point per trade while decision
code snapshots the buffer. A fixed ``deque(maxlen=2400)`` covered less than 60
seconds in busy periods (btc_ret_60s went missing exactly when it matters), and
iterating a deque while another thread appends can raise. This buffer keeps
every point received within ``window_sec`` of the newest one, behind a lock,
with a hard point cap as a memory backstop. Research telemetry only.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any, Iterator


class TimeWindowedHistory:
    """Points are ``(source_ts, price, received_ts)``, appended in receipt order."""

    def __init__(self, window_sec: float = 90.0, max_points: int = 50_000) -> None:
        self.window_sec = float(window_sec)
        self.max_points = int(max_points)
        self._points: deque = deque(maxlen=self.max_points)
        self._lock = threading.Lock()
        self.dropped_by_cap = 0

    def append(self, point: Any) -> None:
        received = float(point[2])
        with self._lock:
            if len(self._points) == self.max_points:
                self.dropped_by_cap += 1
            self._points.append(point)
            horizon = received - self.window_sec
            while self._points and float(self._points[0][2]) < horizon:
                self._points.popleft()

    def __iter__(self) -> Iterator[Any]:
        with self._lock:
            snapshot = list(self._points)
        return iter(snapshot)

    def __len__(self) -> int:
        with self._lock:
            return len(self._points)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            n = len(self._points)
            span = (float(self._points[-1][2]) - float(self._points[0][2])) if n >= 2 else 0.0
        return {"points": n, "span_sec": span, "window_sec": self.window_sec,
                "max_points": self.max_points, "dropped_by_cap": self.dropped_by_cap}
