from __future__ import annotations

import asyncio
import time
from concurrent.futures import Future
from types import SimpleNamespace

from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import BookAction, BookType, OrderSide, RecordFlag
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.objects import Price, Quantity

from bot.adapter_overrides import (
    build_bounded_order_book_snapshot,
    enqueue_bounded_market_data,
    flush_coalesced_quote_ticks,
    cleanup_data_engine_runtime_state,
    should_publish_l2_snapshot,
    l2_publish_interval_sec,
    install_runtime_compatibility_overrides,
    record_quote_provenance,
    record_quote_data_engine_latency,
)
import bot.adapter_overrides as adapter_overrides


def _instrument() -> InstrumentId:
    return InstrumentId(Symbol("BTCUSDT"), Venue("BINANCE"))


def _apply_level(book, instrument_id, side, price: str, size: str, *, action=BookAction.UPDATE):
    order = BookOrder(side, Price.from_str(price), Quantity.from_str(size), 0)
    delta = OrderBookDelta(
        instrument_id, action, order, RecordFlag.F_LAST, 0, 1, 1,
    )
    book.apply_deltas(OrderBookDeltas(instrument_id, [delta]))


def test_bounded_snapshot_is_a_replacing_snapshot_with_exact_top_n_levels():
    inst = _instrument()
    local = OrderBook(inst, BookType.L2_MBP)
    for price in ("100", "99", "98", "97"):
        _apply_level(local, inst, OrderSide.BUY, price, "3")
    for price in ("101", "102", "103", "104"):
        _apply_level(local, inst, OrderSide.SELL, price, "4")

    snapshot = build_bounded_order_book_snapshot(
        instrument_id=inst, book=local, depth=2, ts_event=10, ts_init=11,
    )
    assert snapshot.is_snapshot
    assert len(snapshot.deltas) <= 1 + 2 * 2

    cached = OrderBook(inst, BookType.L2_MBP)
    cached.apply_deltas(snapshot)
    assert [str(level.price) for level in cached.bids()] == ["100", "99"]
    assert [str(level.price) for level in cached.asks()] == ["101", "102"]

    # Deleting a top level must not allow previously published deep levels to
    # linger in the cache. A CLEAR + rebuilt top-N snapshot handles re-entry.
    _apply_level(local, inst, OrderSide.BUY, "100", "0", action=BookAction.DELETE)
    snapshot = build_bounded_order_book_snapshot(
        instrument_id=inst, book=local, depth=2, ts_event=12, ts_init=13,
    )
    cached.apply_deltas(snapshot)
    assert [str(level.price) for level in cached.bids()] == ["99", "98"]


def test_full_and_reconnect_snapshots_refresh_local_book_but_share_l2_cadence():
    install_runtime_compatibility_overrides()
    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient

    inst = _instrument()

    def make_snapshot(bid):
        book = OrderBook(inst, BookType.L2_MBP)
        _apply_level(book, inst, OrderSide.BUY, bid, "3")
        _apply_level(book, inst, OrderSide.SELL, "101", "4")
        return build_bounded_order_book_snapshot(
            instrument_id=inst, book=book, depth=10, ts_event=10, ts_init=11,
        )

    class Message:
        timestamp = 1000.0
        def __init__(self, snapshot):
            self.snapshot = snapshot
        def parse_to_snapshot(self, *, instrument, ts_init):
            return self.snapshot

    published = []
    fake = SimpleNamespace(
        _clock=SimpleNamespace(timestamp_ns=lambda: 11),
        _local_books={},
        _btc15m_l2_publish_state={},
        _btc15m_l2_snapshot_publish_ts={},
        _btc15m_l2_depth=10,
        _btc15m_disconnecting=False,
        _log=SimpleNamespace(warning=lambda *_args: None),
        subscribed_order_book_deltas=lambda: {inst},
        subscribed_quote_ticks=lambda: set(),
        _publish_quote=lambda *_args, **_kwargs: None,
        _handle_data=lambda event: published.append(event),
    )
    fake._publish_l2_snapshot_if_due = lambda instrument, message: (
        PolymarketDataClient._publish_l2_snapshot_if_due(fake, instrument, message)
    )

    PolymarketDataClient._handle_book_snapshot(fake, SimpleNamespace(id=inst), Message(make_snapshot("100")))
    PolymarketDataClient._handle_book_snapshot(fake, SimpleNamespace(id=inst), Message(make_snapshot("99")))

    assert len(published) == 1
    assert str(fake._local_books[inst].best_bid_price()) == "99"
    assert published[0].is_snapshot


def test_l2_publish_failure_uses_backoff_instead_of_per_update_retry(monkeypatch):
    install_runtime_compatibility_overrides()
    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient

    inst = _instrument()
    book = OrderBook(inst, BookType.L2_MBP)
    _apply_level(book, inst, OrderSide.BUY, "100", "3")
    _apply_level(book, inst, OrderSide.SELL, "101", "4")
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(adapter_overrides, "time", SimpleNamespace(monotonic=lambda: clock.now))

    class Message:
        timestamp = 1000.0

    attempts = []
    warnings = []
    def publish(_snapshot):
        attempts.append(clock.now)
        if len(attempts) == 1:
            raise RuntimeError("temporary DataEngine failure")

    fake = SimpleNamespace(
        _local_books={inst: book}, _btc15m_l2_publish_state={},
        _btc15m_l2_snapshot_publish_ts={}, _btc15m_l2_depth=2,
        _btc15m_disconnecting=False, _clock=SimpleNamespace(timestamp_ns=lambda: 11),
        _log=SimpleNamespace(warning=lambda message: warnings.append(message)),
        subscribed_order_book_deltas=lambda: {inst}, _handle_data=publish,
    )
    snapshot_instrument = SimpleNamespace(id=inst)

    assert not PolymarketDataClient._publish_l2_snapshot_if_due(fake, snapshot_instrument, Message())
    clock.now = 10.1
    assert not PolymarketDataClient._publish_l2_snapshot_if_due(fake, snapshot_instrument, Message())
    assert len(attempts) == 1
    clock.now = 10.6
    assert PolymarketDataClient._publish_l2_snapshot_if_due(fake, snapshot_instrument, Message())
    assert len(attempts) == 2
    assert len(warnings) == 1


def test_saturated_data_queue_coalesces_quotes_and_suppresses_l2_without_queuefull():
    class QuoteTick:
        def __init__(self, instrument_id, sequence):
            self.instrument_id = instrument_id
            self.sequence = sequence

    class OrderBookDeltas:
        def __init__(self, instrument_id, sequence):
            self.instrument_id = instrument_id
            self.sequence = sequence

    class Engine:
        def __init__(self):
            self._data_queue = asyncio.Queue(maxsize=32)
            self._config = SimpleNamespace(qsize=32)

    engine = Engine()
    # Exercise the same bounded delivery policy with a slower consumer than
    # the raw update producer. The latest quote per token must survive bursts.
    for sequence in range(128):
        enqueue_bounded_market_data(engine, OrderBookDeltas("up", sequence))
        enqueue_bounded_market_data(engine, OrderBookDeltas("down", sequence))
        enqueue_bounded_market_data(engine, QuoteTick("up", sequence))
        enqueue_bounded_market_data(engine, QuoteTick("down", sequence))
        if sequence % 8 == 0:
            for _ in range(2):
                if engine._data_queue.empty():
                    break
                engine._data_queue.get_nowait()
                flush_coalesced_quote_ticks(engine, consumer_drained=True)

    flush_coalesced_quote_ticks(engine)
    assert engine._data_queue.qsize() <= 32
    assert engine._data_queue.qsize() > 0
    latest_by_instrument = {}
    while not engine._data_queue.empty():
        item = engine._data_queue.get_nowait()
        if type(item).__name__ == "QuoteTick":
            latest_by_instrument[item.instrument_id] = item.sequence
    assert latest_by_instrument == {"up": 127, "down": 127}
    assert engine._btc15m_backpressure["l2_suppressed_total"] > 0
    assert engine._btc15m_backpressure["quote_coalesced_total"] > 0


def test_l2_rate_limit_coalesces_reconnect_snapshot_bursts():
    last = 0.0
    published = 0
    for now in (10.0, 10.01, 10.02, 10.03, 10.25, 10.26, 10.50):
        if should_publish_l2_snapshot(
            has_delta_subscription=True,
            now_monotonic=now,
            last_publish_monotonic=last,
            interval_sec=0.25,
        ):
            published += 1
            last = now
    assert published == 3


def test_l2_interval_is_configurable_and_clamped_to_sane_minimum(monkeypatch):
    monkeypatch.setenv("POLYMARKET_L2_PUBLISH_INTERVAL_SEC", "0.10")
    assert l2_publish_interval_sec() == 0.1
    monkeypatch.setenv("POLYMARKET_L2_PUBLISH_INTERVAL_SEC", "0.001")
    assert l2_publish_interval_sec() == 0.05
    monkeypatch.setenv("POLYMARKET_L2_PUBLISH_INTERVAL_SEC", "invalid")
    assert l2_publish_interval_sec() == 0.25


def test_sustained_two_instrument_load_keeps_queue_bounded_and_quotes_fresh():
    class QuoteTick:
        def __init__(self, instrument_id, sequence):
            self.instrument_id, self.sequence = instrument_id, sequence

    class OrderBookDeltas:
        def __init__(self, instrument_id, sequence):
            self.instrument_id, self.sequence = instrument_id, sequence

    class Engine:
        def __init__(self):
            self._data_queue = asyncio.Queue(maxsize=64)
            self._config = SimpleNamespace(qsize=64)

    engine = Engine()
    peak = 0
    accepted_l2 = 0
    latest_delivered = {}
    # 360 L2 updates/sec * 30 simulated seconds, split across two instruments.
    for sequence in range(5400):
        for instrument in ("up", "down"):
            accepted_l2 += enqueue_bounded_market_data(
                engine, OrderBookDeltas(instrument, sequence),
            )
            enqueue_bounded_market_data(engine, QuoteTick(instrument, sequence))
        peak = max(peak, engine._data_queue.qsize())
        # A deterministic slow consumer: drains only every 120 raw frames.
        if sequence % 120 == 0:
            for _ in range(8):
                if engine._data_queue.empty():
                    break
                item = engine._data_queue.get_nowait()
                if type(item).__name__ == "QuoteTick":
                    latest_delivered[item.instrument_id] = item.sequence
            flush_coalesced_quote_ticks(engine, consumer_drained=True)

    # Drain and flush until no latest-only quote is pending.
    state = engine._btc15m_backpressure
    for _ in range(4):
        while not engine._data_queue.empty():
            item = engine._data_queue.get_nowait()
            if type(item).__name__ == "QuoteTick":
                latest_delivered[item.instrument_id] = item.sequence
        flush_coalesced_quote_ticks(engine, consumer_drained=True)
        if not state["quotes"]:
            while not engine._data_queue.empty():
                item = engine._data_queue.get_nowait()
                if type(item).__name__ == "QuoteTick":
                    latest_delivered[item.instrument_id] = item.sequence
            break

    assert peak <= 32  # queue guard keeps pressure at or below 50% capacity
    assert engine._data_queue.qsize() <= 64
    assert accepted_l2 < 500  # optional L2 publication collapses under sustained pressure
    assert latest_delivered == {"up": 5399, "down": 5399}
    assert state["l2_suppressed_total"] > 10_000
    assert state["quote_coalesced_total"] > 0


def test_quote_buffer_replaces_intermediates_and_flushes_latest_after_capacity_returns():
    class QuoteTick:
        def __init__(self, instrument_id, sequence):
            self.instrument_id, self.sequence = instrument_id, sequence

    class Engine:
        def __init__(self):
            self._data_queue = asyncio.Queue(maxsize=4)
            self._config = SimpleNamespace(qsize=4)

    engine = Engine()
    for sequence in range(4):
        engine._data_queue.put_nowait(QuoteTick("other", sequence))
    for sequence in (1, 2, 3):
        assert not enqueue_bounded_market_data(engine, QuoteTick("up", sequence))
    assert engine._btc15m_backpressure["quotes"]["up"].sequence == 3

    for _ in range(4):
        engine._data_queue.get_nowait()
    assert flush_coalesced_quote_ticks(engine) == 1
    assert engine._data_queue.get_nowait().sequence == 3
    assert engine._btc15m_backpressure["quotes"] == {}


def test_pressure_latch_recovers_only_after_consumer_drains_below_release_mark():
    class QuoteTick:
        def __init__(self, instrument_id):
            self.instrument_id = instrument_id

    class OrderBookDeltas:
        pass

    class Engine:
        def __init__(self):
            self._data_queue = asyncio.Queue(maxsize=8)
            self._config = SimpleNamespace(qsize=8)

    engine = Engine()
    for index in range(2):
        engine._data_queue.put_nowait(index)
    enqueue_bounded_market_data(engine, QuoteTick("up"))
    assert engine._btc15m_backpressure["l2_suppression_active"] is True
    assert not enqueue_bounded_market_data(engine, OrderBookDeltas())
    while not engine._data_queue.empty():
        engine._data_queue.get_nowait()
    flush_coalesced_quote_ticks(engine, consumer_drained=True)
    assert engine._btc15m_backpressure["l2_suppression_active"] is False
    assert enqueue_bounded_market_data(engine, OrderBookDeltas())


def test_dispose_cleanup_drops_pending_quote_and_telemetry_state():
    engine = SimpleNamespace(
        _btc15m_backpressure={"quotes": {"up": object()}},
        _btc15m_queue_telemetry_state={"counts": {"QuoteTick": 1}},
        _btc15m_queue_telemetry_pending_report={"queue_depth": 1},
        _btc15m_sentinel_enqueue_task=None,
    )
    cleanup_data_engine_runtime_state(engine)
    assert engine._btc15m_backpressure is None
    assert engine._btc15m_queue_telemetry_state is None
    assert engine._btc15m_queue_telemetry_pending_report is None
    assert engine._btc15m_sentinel_enqueue_task is None


def test_dispose_cleanup_cancels_async_task_and_concurrent_future():
    async def scenario():
        task = asyncio.create_task(asyncio.Event().wait())
        future = Future()
        engine = SimpleNamespace(_btc15m_sentinel_enqueue_task=task)
        cleanup_data_engine_runtime_state(engine)
        await asyncio.sleep(0)
        assert task.cancelled()
        assert engine._btc15m_sentinel_enqueue_task is None

        engine._btc15m_sentinel_enqueue_task = future
        cleanup_data_engine_runtime_state(engine)
        assert future.cancelled()

    asyncio.run(scenario())


def test_quote_publish_timestamp_is_set_only_when_queue_admits_buffered_quote():
    class QuoteTick:
        instrument_id = "up"
        ts_event = 1
        ts_init = 2

    class Engine:
        def __init__(self):
            self._data_queue = asyncio.Queue(maxsize=4)
            self._config = SimpleNamespace(qsize=4)

    quote = QuoteTick()
    record_quote_provenance(quote, source="ws_price_change", raw_ws_received_ts=time.time())
    engine = Engine()
    for value in range(4):
        engine._data_queue.put_nowait(value)
    assert not enqueue_bounded_market_data(engine, quote)
    assert record_quote_data_engine_latency(quote, time.time()) is None

    while not engine._data_queue.empty():
        engine._data_queue.get_nowait()
    assert flush_coalesced_quote_ticks(engine) == 1
    latency = record_quote_data_engine_latency(quote, time.time())
    assert latency is not None and latency >= 0
