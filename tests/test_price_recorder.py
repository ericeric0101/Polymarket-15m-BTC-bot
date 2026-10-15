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


# --- source-clock bucketing (Chainlink) ------------------------------------------------------

def test_source_clock_bucketer_keeps_every_source_second_and_waits_for_grace():
    rows = []
    b = SecondBucketer(rows.append, by_source_clock=True, grace_sec=5)
    # Two source seconds received within the same local second must both survive.
    b.observe("chainlink_spot", 100.0, T0 + 10.2, int((T0 + 8.9) * 1000))
    b.observe("chainlink_spot", 101.0, T0 + 10.4, int((T0 + 9.9) * 1000))
    b.observe("chainlink_spot", 102.0, T0 + 10.5, None)   # no source clock: dropped
    assert [(r["ts_sec"], r["close"]) for r in rows] == [(int(T0 + 8), 100.0)]
    b.flush_older_than(T0 + 13.0)                         # 9 >= 13 - 5: still within grace
    assert len(rows) == 1
    b.flush_older_than(T0 + 15.5)
    assert [(r["ts_sec"], r["close"]) for r in rows][-1] == (int(T0 + 9), 101.0)


# --- order books ------------------------------------------------------------------------------

from bot.research.price_recorder import BookSampler, market_tokens_for  # noqa: E402

META = {"tu": {"slug": "btc-updown-15m-1", "outcome": "Up", "end_ts": T0 + 900},
        "td": {"slug": "btc-updown-15m-1", "outcome": "Down", "end_ts": T0 + 900}}


def test_book_sampler_snapshot_deltas_carry_forward_and_reset():
    s = BookSampler()
    assert s.set_tokens(META) == ["tu", "td"]
    s.apply({"event_type": "price_change", "price_changes": [
        {"asset_id": "tu", "price": "0.4", "size": "5", "side": "BUY"}]}, T0)
    assert s.sample(T0) == []                       # no snapshot yet: nothing sampled
    s.apply([{"event_type": "book", "asset_id": "tu", "timestamp": str(int(T0 * 1000)),
              "bids": [{"price": "0.40", "size": "10"}, {"price": "0.38", "size": "3"}],
              "asks": [{"price": "0.42", "size": "7"}]},
             {"event_type": "book", "asset_id": "zz", "bids": [], "asks": []}], T0 + 0.1)
    s.apply({"event_type": "price_change", "timestamp": str(int(T0 * 1000) + 500), "price_changes": [
        {"asset_id": "tu", "price": "0.40", "size": "0", "side": "BUY"},
        {"asset_id": "tu", "price": "0.41", "size": "2", "side": "SELL"}]}, T0 + 0.5)
    (row,) = s.sample(T0 + 0.9)
    assert (row["outcome"], row["best_bid"], row["best_ask"], row["bid_size"], row["ask_size"]) == (
        "Up", 0.38, 0.41, 3.0, 2.0)
    assert row["n_updates"] == 3 and row["last_event_ms"] == int(T0 * 1000) + 500
    (row2,) = s.sample(T0 + 1.9)                    # quiet second: state carried forward
    assert row2["best_ask"] == 0.41 and row2["n_updates"] == 0
    assert s.sample(T0 + 906) == []                 # market ended (> end + 5 s)
    s.reset()
    assert s.sample(T0 + 2) == []


def test_set_tokens_drops_untracked_books_and_reports_new():
    s = BookSampler()
    s.set_tokens(META)
    s.apply({"event_type": "book", "asset_id": "td", "bids": [{"price": "0.6", "size": "1"}], "asks": []}, T0)
    nxt = {"tn": {"slug": "btc-updown-15m-2", "outcome": "Up", "end_ts": T0 + 1800}}
    assert s.set_tokens(nxt) == ["tn"]
    assert s.sample(T0 + 1) == [] and s.tokens() == ["tn"]


def test_market_tokens_for_maps_outcomes_and_skips_unsupported():
    v1 = {"version": "v1", "conditionId": "0xabc", "outcomes": '["Up", "Down"]', "clobTokenIds": '["11", "22"]'}
    bad = {"version": "v9", "conditionId": "0xabc", "outcomes": '["Up", "Down"]', "clobTokenIds": '["1", "2"]'}
    markets = {"btc-updown-15m-900": v1, "btc-updown-15m-1800": bad}
    meta = market_tokens_for(list(markets) + ["btc-updown-15m-2700"], markets.get)
    assert meta == {"11": {"slug": "btc-updown-15m-900", "outcome": "Up", "end_ts": 1800.0},
                    "22": {"slug": "btc-updown-15m-900", "outcome": "Down", "end_ts": 1800.0}}
