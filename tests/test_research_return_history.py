"""The research BTC return buffer must cover 60s regardless of trade rate."""
import threading

from bot.candidate_telemetry import _returns_from_history
from bot.research_return_history import TimeWindowedHistory


def _fill(hist, start, seconds, per_sec, price0=100_000.0):
    for i in range(int(seconds * per_sec)):
        t = start + i / per_sec
        hist.append((t - 0.05, price0 + i * 0.01, t))


def test_busy_feed_still_yields_60s_return():
    hist = TimeWindowedHistory(window_sec=90.0)
    _fill(hist, 1000.0, 70, 100)  # 7,000 trades: old deque(maxlen=2400) held only 24s
    missing = {}
    out = _returns_from_history(hist, cutoff=1070.0, missing=missing)
    assert out["btc_ret_60s"] is not None
    assert "btc_returns" not in missing


def test_points_older_than_window_are_trimmed():
    hist = TimeWindowedHistory(window_sec=90.0)
    _fill(hist, 0.0, 200, 2)
    pts = list(hist)
    assert pts[-1][2] - pts[0][2] <= 90.0
    assert hist.stats()["span_sec"] <= 90.0


def test_hard_cap_counts_drops():
    hist = TimeWindowedHistory(window_sec=1e9, max_points=10)
    _fill(hist, 0.0, 15, 1)
    assert len(hist) == 10 and hist.dropped_by_cap == 5


def test_snapshot_is_safe_while_feed_thread_appends():
    hist = TimeWindowedHistory(window_sec=5.0)
    stop = threading.Event()

    def writer():
        t = 0.0
        while not stop.is_set():
            t += 0.001
            hist.append((t, 1.0, t))

    th = threading.Thread(target=writer)
    th.start()
    try:
        for _ in range(2000):
            list(hist)
    finally:
        stop.set()
        th.join()
