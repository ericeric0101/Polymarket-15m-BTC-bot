from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from bot.research.price_recorder import (
    HourlyParquetStore,
    SecondBucketer,
    parse_coinbase_ticker,
)

GIB = 1024 ** 3
T0 = datetime(2026, 10, 15, 3, 0, 0, tzinfo=timezone.utc).timestamp()


def _disk(free_gb):
    return lambda _path: SimpleNamespace(free=int(free_gb * GIB))


# --- bucketer -------------------------------------------------------------------------------

def test_bucketer_emits_one_row_per_source_second_with_last_price():
    rows = []
    b = SecondBucketer(rows.append)
    b.observe("binance_btcusdt", 100.0, T0 + 0.1, 1)
    b.observe("binance_btcusdt", 102.0, T0 + 0.5, 2)
    b.observe("binance_btcusdt", 101.0, T0 + 0.9, 3)
    b.observe("coinbase_btcusd", 99.0, T0 + 0.2, None)
    assert rows == []
    b.observe("binance_btcusdt", 105.0, T0 + 1.1, 4)
    assert len(rows) == 1
    r = rows[0]
    assert (r["source"], r["ts_sec"], r["close"], r["high"], r["low"], r["n_updates"]) == (
        "binance_btcusdt", int(T0), 101.0, 102.0, 100.0, 3)
    assert r["last_source_ms"] == 3 and r["last_received_ms"] == int((T0 + 0.9) * 1000)


def test_bucketer_flushes_quiet_feeds_and_drops_bad_or_backward_ticks():
    rows = []
    b = SecondBucketer(rows.append)
    b.observe("chainlink_spot", 100.0, T0 + 5.2, 10)
    b.observe("chainlink_spot", 50.0, T0 + 4.9, 9)       # clock stepped back: dropped
    b.observe("chainlink_spot", float("nan"), T0 + 5.3, 11)
    b.observe("chainlink_spot", -1.0, T0 + 5.4, 12)
    b.flush_older_than(T0 + 5.9)                          # same second: still open
    assert rows == []
    b.flush_older_than(T0 + 6.0)
    assert [(r["close"], r["n_updates"]) for r in rows] == [(100.0, 1)]


# --- store ----------------------------------------------------------------------------------

def _row(source, ts, price=100.0):
    return {"source": source, "ts_sec": int(ts), "close": price, "high": price, "low": price,
            "n_updates": 1, "last_source_ms": None, "last_received_ms": int(ts * 1000)}


def test_store_writes_one_file_per_completed_utc_hour(tmp_path):
    now = {"t": T0 + 1800}
    store = HourlyParquetStore(tmp_path, free_disk_provider=_disk(50), clock=lambda: now["t"])
    for s in range(0, 3600, 600):
        store.add(_row("binance_btcusdt", T0 - 3600 + s))   # hour 02 (complete)
    store.add(_row("coinbase_btcusd", T0 + 10))              # hour 03 (current)
    written = store.flush()
    assert [p.relative_to(tmp_path).as_posix() for p in written] == ["2026-10-15/02.parquet"]
    assert len(pd.read_parquet(written[0])) == 6
    assert store.stats.rows_buffered == 1
    # Shutdown writes the current hour too; a second write in the same hour never overwrites.
    store.add(_row("coinbase_btcusd", T0 + 20))
    first = store.flush(include_current_hour=True)
    store.add(_row("coinbase_btcusd", T0 + 30))
    second = store.flush(include_current_hour=True)
    assert [p.name for p in first + second] == ["03.parquet", "03_2.parquet"]
    assert store.stats.rows_written == 9 and store.stats.files_written == 3
    assert not list(tmp_path.rglob("*.tmp"))


def test_store_bounds_buffer_and_pauses_on_low_disk(tmp_path):
    store = HourlyParquetStore(tmp_path, max_buffer_rows=3, free_disk_provider=_disk(5),
                               min_free_disk_gb=10, clock=lambda: T0 + 7200)
    for i in range(5):
        store.add(_row("binance_btcusdt", T0 + i))
    assert store.stats.rows_dropped_buffer_full == 2
    assert store.flush() == []
    assert store.stats.rows_dropped_disk_low == 3 and store.stats.rows_buffered == 0
    assert not any(tmp_path.rglob("*.parquet"))


def test_retention_removes_only_old_day_directories(tmp_path):
    for name in ("2026-09-14", "2026-09-15", "2026-10-15", "notes"):
        (tmp_path / name).mkdir()
    (tmp_path / "_status.json").write_text("{}")
    store = HourlyParquetStore(tmp_path, retention_days=30, clock=lambda: T0)
    removed = store.enforce_retention()
    assert [p.name for p in removed] == ["2026-09-14"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-09-15", "2026-10-15", "_status.json", "notes"]


# --- coinbase parser -------------------------------------------------------------------------

def test_parse_coinbase_ticker():
    msg = {"type": "ticker", "product_id": "BTC-USD", "price": "81555.83",
           "time": "2026-10-14T23:58:24.123456Z"}
    price, ms = parse_coinbase_ticker(msg)
    assert price == pytest.approx(81555.83)
    assert ms == int(datetime(2026, 10, 14, 23, 58, 24, 123456, tzinfo=timezone.utc).timestamp() * 1000)
    assert parse_coinbase_ticker({**msg, "type": "subscriptions"}) is None
    assert parse_coinbase_ticker({**msg, "product_id": "ETH-USD"}) is None
    assert parse_coinbase_ticker({**msg, "price": "x"}) is None
    assert parse_coinbase_ticker({**msg, "time": "bad"}) == (pytest.approx(81555.83), None)
    assert parse_coinbase_ticker(None) is None
