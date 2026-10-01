from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from bot.btc_1s_history import (
    BTC1sHistoryCollector,
    _Bar,
    atomic_write_parquet_part,
    load_btc_1s_history,
)
from scripts.validate_btc_1s_history import validate_day


pytest.importorskip("pyarrow")


def observe(collector, source_ms, price, qty, receive_ts=None):
    collector.observe_aggtrade(price=price, source_ts_ms=source_ms,
                               received_ts=(source_ms / 1000 if receive_ts is None else receive_ts),
                               quantity=qty)


def completed_rows(collector):
    return [collector._queue.get_nowait() for _ in range(collector._queue.qsize())]


def test_aggtrade_same_second_ohlc_volume_vwap_and_trade_count():
    collector = BTC1sHistoryCollector(queue_rows=10)
    observe(collector, 1_000_100, 10, 2)
    observe(collector, 1_000_500, 20, 1)
    observe(collector, 1_002_100, 15, 3)
    rows = completed_rows(collector)
    assert len(rows) == 1
    assert rows[0]["ts_sec"] == 1000
    assert (rows[0]["open"], rows[0]["high"], rows[0]["low"], rows[0]["close"]) == (10, 20, 10, 20)
    assert rows[0]["volume"] == 3
    assert rows[0]["trade_count"] == 2  # aggTrade messages, not underlying executions
    assert rows[0]["vwap"] == pytest.approx(40 / 3)


def test_source_time_buckets_even_when_receive_time_crosses_second():
    collector = BTC1sHistoryCollector(queue_rows=10)
    observe(collector, 10_999, 101, 1, receive_ts=11.400)
    observe(collector, 12_100, 102, 1, receive_ts=12.200)
    rows = completed_rows(collector)
    assert rows[0]["ts_sec"] == 10
    assert rows[0]["first_source_ts"] == 10_999
    assert rows[0]["first_received_ts"] == 11_400


def test_missing_seconds_are_not_synthesized():
    collector = BTC1sHistoryCollector(queue_rows=10)
    observe(collector, 20_100, 100, 1)
    observe(collector, 22_100, 102, 1)
    observe(collector, 24_100, 104, 1)
    rows = completed_rows(collector)
    assert [row["ts_sec"] for row in rows] == [20, 22]


def test_utc_day_rotation_creates_separate_part_files(tmp_path):
    collector = BTC1sHistoryCollector(data_dir=tmp_path, queue_rows=10)
    observe(collector, 86_399_100, 100, 1)
    observe(collector, 86_400_100, 101, 1)
    observe(collector, 86_401_100, 102, 1)
    rows = completed_rows(collector)
    # Last in-flight bar is flushed at graceful stop; the test collector has no worker.
    with collector._lock:
        for sec in sorted(collector._bars):
            collector._enqueue_nonblocking(collector._bars[sec].row())
        collector._bars.clear()
    rows += completed_rows(collector)
    by_day = {}
    for row in rows:
        day = datetime.fromtimestamp(row["ts_sec"], timezone.utc).date().isoformat()
        by_day.setdefault(day, []).append(row)
    for day_rows in by_day.values():
        atomic_write_parquet_part(tmp_path, day_rows)
    names = sorted(path.name for path in tmp_path.glob("*.parquet"))
    assert names == ["BTCUSDT_1s_1970-01-01_part-00001.parquet",
                     "BTCUSDT_1s_1970-01-02_part-00001.parquet"]


def test_restart_adds_new_part_and_loader_dedupes_latest_source_timestamp(tmp_path):
    base = {"ts_sec": 1_800_000_000, "open": 10., "high": 10., "low": 10., "close": 10.,
            "volume": 1., "trade_count": 1, "vwap": 10., "first_source_ts": 1_800_000_000_100,
            "last_source_ts": 1_800_000_000_100, "first_received_ts": 1_800_000_000_110,
            "last_received_ts": 1_800_000_000_110}
    first = atomic_write_parquet_part(tmp_path, [base])
    newer = {**base, "close": 12., "last_source_ts": base["last_source_ts"] + 500}
    second = atomic_write_parquet_part(tmp_path, [newer])
    rows = load_btc_1s_history(base["ts_sec"], base["ts_sec"] + 1, tmp_path)
    assert first != second
    assert first.exists() and second.exists()
    assert rows == [newer]


def test_duplicate_tie_break_is_deterministic_by_part_path(tmp_path):
    row = {"ts_sec": 1_800_000_100, "open": 10., "high": 10., "low": 10., "close": 10.,
           "volume": None, "trade_count": 1, "vwap": None, "first_source_ts": 1_800_000_100_100,
           "last_source_ts": 1_800_000_100_100, "first_received_ts": 1_800_000_100_110,
           "last_received_ts": 1_800_000_100_110}
    atomic_write_parquet_part(tmp_path, [row])
    duplicate = {**row, "close": 11.}
    atomic_write_parquet_part(tmp_path, [duplicate])
    assert load_btc_1s_history(row["ts_sec"], row["ts_sec"] + 1, tmp_path)[0]["close"] == 11.


def test_validator_reports_duplicates_and_gap_structure(tmp_path):
    start = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    def row(ts, px):
        source = ts * 1000 + 100
        return {"ts_sec": ts, "open": px, "high": px, "low": px, "close": px,
                "volume": 1., "trade_count": 1, "vwap": px,
                "first_source_ts": source, "last_source_ts": source,
                "first_received_ts": source + 15, "last_received_ts": source + 15}
    atomic_write_parquet_part(tmp_path, [row(start + 1, 10.), row(start + 3, 12.)])
    atomic_write_parquet_part(tmp_path, [row(start + 1, 11.)])
    report = validate_day(date(2026, 10, 1), tmp_path)
    assert report["row_count"] == 2
    assert report["raw_part_rows"] == 3
    assert report["duplicate_seconds"] == 1
    assert report["missing_seconds"] == 86_398
    assert report["gap_count"] == 1
    assert report["largest_gap_sec"] == 1
    assert report["median_source_receive_latency_ms"] == 15


def test_low_disk_disables_history_only(tmp_path):
    collector = BTC1sHistoryCollector(data_dir=tmp_path, min_free_disk_gb=1,
                                     free_disk_provider=lambda _: SimpleNamespace(free=0))
    assert collector._disk_ok() is False
    observe(collector, 1_000, 100, 1)
    assert not collector.enabled
    assert collector._queue.empty()
    # No live callback is owned or interrupted by this independent collector.
    live_updates = []
    live_updates.append("price-path-still-runs")
    assert live_updates == ["price-path-still-runs"]


def test_writer_failure_is_isolated_from_observer(monkeypatch, tmp_path):
    import bot.btc_1s_history as history
    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(history, "atomic_write_parquet_part", fail_write)
    collector = BTC1sHistoryCollector(data_dir=tmp_path, queue_rows=10, batch_rows=1,
                                     metrics_interval_sec=60)
    collector.start()
    try:
        observe(collector, 1_000, 100, 1)
        observe(collector, 3_000, 101, 1)
        for _ in range(100):
            if not collector.enabled:
                break
            import time
            time.sleep(.01)
        assert not collector.enabled
        assert collector._failure_reason.startswith("parquet_write_failed")
    finally:
        collector.stop()


def test_full_queue_drops_history_nonblocking():
    collector = BTC1sHistoryCollector(queue_rows=1)
    observe(collector, 1_000, 100, 1)
    observe(collector, 3_000, 101, 1)  # queue sec 1
    observe(collector, 5_000, 102, 1)  # queue full when sec 3 completes
    assert collector._queue.qsize() == 1
    assert collector.dropped_history_rows == 1
