"""Diagnostics preserve consumer results and remain bounded without live resources."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from bot import consumer_timing as timing
from monitoring.trade_journal_db import TradeJournalDB


class Clock:
    value = 0.0
    def __call__(self):
        return self.value


class Loop:
    def __init__(self):
        self.value = 0.0
        self.calls = []
    def time(self):
        return self.value
    def call_later(self, delay, callback, expected):
        handle = SimpleNamespace(cancelled=False)
        handle.cancel = lambda: setattr(handle, 'cancelled', True)
        self.calls.append((delay, callback, expected, handle))
        return handle


def monitor(monkeypatch):
    events = []
    monkeypatch.setattr(timing, 'diagnostic', lambda host, kind, **fields: events.append((kind, fields)))
    clock = Clock()
    return timing.ConsumerTiming(SimpleNamespace(), Loop(), monotonic=clock), clock, events


def test_aggregate_quantiles_counts_and_no_event_writes(monkeypatch):
    m, clock, events = monitor(monkeypatch)
    for value in range(100):
        m.observe('quote', 'QuoteTick', value)
    assert events == []
    clock.value = 60
    m.observe('quote', 'QuoteTick', 100)
    assert len(events) == 1
    kind, report = events[0]
    assert kind == 'EVENT_LOOP_CONSUMER_TIMING'
    row = report['handlers'][0]
    assert (row['count'], row['median'], row['P95'], row['P99'], row['max']) == (101, 50, 95, 99, 100)
    assert m.states == {}


def test_samples_keys_and_queue_metadata_are_bounded(monkeypatch):
    m, clock, events = monitor(monkeypatch)
    for _ in range(2000):
        m.observe('quote', 'QuoteTick', 1)
    assert next(iter(m.states.values()))['count'] == 2000
    assert len(next(iter(m.states.values()))['samples']) == 512
    for n in range(100):
        m.observe(str(n), 'Other', 1)
    assert len(m.states) == m.HANDLER_LIMIT
    objects = [object() for _ in range(100)]
    for event in objects:
        m.mark_enqueued(event)
    assert len(m.enqueued) == 32
    assert events == []


def test_contention_drops_instead_of_waiting(monkeypatch):
    m, clock, events = monitor(monkeypatch)
    m.lock.acquire()
    try:
        m.observe('quote', 'QuoteTick', 1)
        m.mark_enqueued(object())
        assert m.snapshot() == {'unavailable': 'collector_busy'}
    finally:
        m.lock.release()
    assert m.dropped == 1
    assert m.states == m.enqueued == {}


def test_slow_traces_sparse_and_bucketed(monkeypatch):
    m, clock, events = monitor(monkeypatch)
    for n in range(30):
        clock.value = n
        m.observe('quote', 'QuoteTick', 1500)
    slow = [row for kind, row in events if kind == 'SLOW_CONSUMER_CALLBACK']
    assert len(slow) == 3
    assert all(row['threshold_ms'] == 1000 for row in slow)
    assert all(row['callback_exec_ms'] == 1500 for row in slow)


def test_wrappers_preserve_return_exception_and_ignore_journal_failure(monkeypatch):
    m, clock, events = monitor(monkeypatch)
    def work(value):
        clock.value += 2
        if value == 'error':
            raise ValueError('original')
        return value
    owner = SimpleNamespace(work=work)
    assert m.wrap(owner, 'work', 'work', 'Test')
    monkeypatch.setattr(timing, 'diagnostic', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('journal')))
    assert owner.work(42) == 42
    with pytest.raises(ValueError, match='original'):
        owner.work('error')
    assert not m.emitting


def test_queue_wait_separate_from_execution(monkeypatch):
    m, clock, events = monitor(monkeypatch)
    event = object()
    m.mark_enqueued(event)
    m.dequeued[id(event)] = .4
    clock.value = .5
    def work(event):
        clock.value = .52
    owner = SimpleNamespace(work=work)
    m.wrap(owner, 'work', 'data_engine_handler', 'dynamic')
    owner.work(event)
    rows = {row['handler']: row for row in m.snapshot()['handlers']}
    assert rows['data_engine_queue_wait']['max'] == pytest.approx(400)
    assert rows['dequeue_to_handler']['max'] == pytest.approx(100)
    assert rows['data_engine_handler']['max'] == pytest.approx(20)
    assert m.enqueued == m.dequeued == {}


def test_loop_lag_accurate_no_catchup_and_cancelled(monkeypatch):
    m, clock, events = monitor(monkeypatch)
    before = threading.active_count()
    m.start()
    m.start()
    assert len(m.loop.calls) == 1
    m.loop.value = 3.5
    m.loop.calls[0][1](m.loop.calls[0][2])
    assert m.last_lag == {'scheduled_at':1.0, 'executed_at':3.5, 'lag_ms':2500.0, 'clock':'event_loop_monotonic'}
    assert m.loop.calls[-1][2] == 4.5
    heartbeat = next(row for row in m.snapshot()['handlers'] if row['handler'] == 'loop_heartbeat_callback')
    assert heartbeat['count'] == 1
    assert heartbeat['total_exec_ms'] == 0
    assert heartbeat['timing_scope'] == 'synchronous_inclusive'
    handle = m.loop.calls[-1][3]
    m.close()
    assert handle.cancelled
    assert threading.active_count() == before


def test_owner_loop_distinguished_from_background(monkeypatch):
    m, clock, events = monitor(monkeypatch)
    async def run():
        m.loop = asyncio.get_running_loop()
        m.observe('quote', 'QuoteTick', 1)
    asyncio.run(run())
    m.observe('quote', 'QuoteTick', 2)
    assert {row['owner_loop'] for row in m.snapshot()['handlers']} == {True, False}


def test_native_data_engine_dispatch_hits_instance_hook(monkeypatch):
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import LiveClock, MessageBus
    from nautilus_trader.live.data_engine import LiveDataEngine
    from nautilus_trader.model.identifiers import InstrumentId, TraderId
    from nautilus_trader.model.data import QuoteTick
    from nautilus_trader.model.objects import Price, Quantity
    m, clock, events = monitor(monkeypatch)
    loop = asyncio.new_event_loop()
    try:
        live_clock = LiveClock()
        bus = MessageBus(trader_id=TraderId('TEST-001'), clock=live_clock)
        engine = LiveDataEngine(loop=loop, msgbus=bus, cache=Cache(), clock=live_clock)
        assert m.wrap(engine, '_handle_quote_tick', 'quote_cache_bus_dispatch', 'QuoteTick')
        quote = QuoteTick(InstrumentId.from_str('TEST.POLYMARKET'), Price.from_str('0.50'), Price.from_str('0.51'), Quantity.from_str('1'), Quantity.from_str('1'), 1, 1)
        engine._handle_data(quote)
        assert m.snapshot()['handlers'][0]['handler'] == 'quote_cache_bus_dispatch'
    finally:
        loop.close()


def fake_node(fail=False):
    points = []
    async def start():
        return 11
    async def stop():
        if fail:
            raise ValueError('stop failed')
        return 22
    engine = SimpleNamespace(_data_queue=asyncio.Queue(), _clients={})
    kernel = SimpleNamespace(loop=Loop(), data_engine=engine, start_async=start,
        stop_async=stop, is_running=False, _stop_engines=lambda: 33)
    node = SimpleNamespace(kernel=kernel, _collection_shutdown_point=lambda stage, **kw: points.append(stage))
    host = SimpleNamespace(trade_db=SimpleNamespace(), run_id='test')
    return node, host, points


@pytest.mark.parametrize('fail', [False, True])
def test_kernel_stop_preserves_outcome_and_closes_monitor(monkeypatch, fail):
    monkeypatch.setattr(timing, 'diagnostic', lambda *a, **kw: None)
    node, host, points = fake_node(fail)
    timing.bind_consumer_timing(node, host)
    if fail:
        with pytest.raises(ValueError, match='stop failed'):
            asyncio.run(node.kernel.stop_async())
        assert points == ['kernel_stop_failed']
    else:
        assert asyncio.run(node.kernel.stop_async()) == 22
        assert points == ['kernel_stop_complete']
    assert host._consumer_timing.closed


def test_worker_end_observations_follow_stop_request(monkeypatch):
    monkeypatch.setattr(timing, 'diagnostic', lambda *a, **kw: None)
    async def run():
        node, host, points = fake_node()
        task = asyncio.create_task(asyncio.sleep(0))
        node.kernel.data_engine.get_data_queue_task = lambda: task
        timing.bind_consumer_timing(node, host)
        assert node.kernel.data_engine.get_data_queue_task() is task
        assert node.kernel.data_engine.get_data_queue_task() is task
        assert node.kernel._stop_engines() == 33
        await task
        await asyncio.sleep(0)
        assert points == ['data_engine.get_data_queue_task_stop_start', 'data_engine.get_data_queue_task_worker_end']
    asyncio.run(run())


def test_optional_journal_lock_never_waits():
    journal = object.__new__(TradeJournalDB)
    journal._backup_lock = threading.Lock()
    journal._backup_lock.acquire()
    try:
        assert journal.enqueue_strategy_event('test', 'diagnostic', {}) is False
    finally:
        journal._backup_lock.release()


def test_async_helpers_measure_await_not_coroutine_creation(monkeypatch):
    m, clock, events = monitor(monkeypatch)
    async def work(fail=False):
        await asyncio.sleep(0)
        clock.value += 2
        if fail:
            raise ValueError('async original')
        return 7
    owner = SimpleNamespace(work=work)
    m.wrap(owner, 'work', 'fee_lookup', 'Decision')
    assert asyncio.run(owner.work()) == 7
    with pytest.raises(ValueError, match='async original'):
        asyncio.run(owner.work(True))
    row = m.snapshot()['handlers'][0]
    assert row['handler'] == 'fee_lookup_await'
    assert row['timing_scope'] == 'await_inclusive'
    assert row['count'] == 2
    assert row['max'] == 2000
    slow = [fields for kind, fields in events if kind == 'SLOW_CONSUMER_CALLBACK']
    assert slow[0]['callback_exec_ms'] is None


@pytest.mark.parametrize('duration,threshold', [(101,100),(501,500),(1001,1000)])
def test_slow_trace_persists_using_real_diagnostic_and_journal(tmp_path, duration, threshold):
    import json
    import sqlite3
    journal = TradeJournalDB(str(tmp_path / 'diagnostics.db'))
    host = SimpleNamespace(run_id='real-signature', trade_db=journal,
        collection_identity={'process_instance_id':'test-process', 'cycle_idx':1})
    m = timing.ConsumerTiming(host, Loop(), monotonic=lambda:0)
    try:
        m.observe('strategy_quote_callback','QuoteTick',duration,instrument='TEST.POLYMARKET')
    finally:
        journal.stop()
    with sqlite3.connect(journal.db_path) as connection:
        rows = connection.execute('select event_type,payload_json from strategy_events').fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 'SLOW_CONSUMER_CALLBACK'
    payload = json.loads(rows[0][1])
    assert payload['consumer_event_type'] == 'QuoteTick'
    assert payload['threshold_ms'] == threshold
    assert payload['callback_exec_ms'] == duration
    assert payload['run_id'] == 'real-signature'
    assert payload['process_instance_id'] == 'test-process'
    assert payload['cycle_idx'] == 1


def test_cumulative_short_callback_load_visible_without_event_rows(monkeypatch):
    m, clock, events = monitor(monkeypatch)
    before = threading.active_count()
    for _ in range(30000):
        m.observe('adapter_raw_decoder','RawWebSocket',1.5)
    assert events == []
    clock.value = 60
    row = m.snapshot()['handlers'][0]
    assert row['count'] == row['count_per_min'] == 30000
    assert row['total_exec_ms'] == row['total_exec_ms_per_min'] == 45000
    assert row['mean_exec_ms'] == row['median_exec_ms'] == 1.5
    assert row['share_of_window_pct'] == 75
    assert row['sample_count'] == 512
    assert row['slow_count'] == 0
    assert threading.active_count() == before


def test_inclusive_shares_are_independent_unclamped_and_zero_window_unknown(monkeypatch):
    m, clock, events = monitor(monkeypatch)
    m.observe('adapter_raw_decoder','RawWebSocket',800)
    m.observe('adapter_price_change','PriceChangeMessage',600)
    m.observe('fee_lookup_await','Decision',2500)
    clock.value = 1
    report = m.snapshot()
    rows = {row['handler']:row for row in report['handlers']}
    assert rows['adapter_raw_decoder']['share_of_window_pct'] == 80
    assert rows['adapter_price_change']['share_of_window_pct'] == 60
    assert rows['fee_lookup_await']['share_of_window_pct'] == 250
    assert all(row['inclusive'] for row in rows.values())
    assert report['cumulative_scope'] == 'per_handler_inclusive_elapsed_not_cpu'
    assert 'must not be summed' in report['share_policy']
    assert 'total_exec_ms' not in report  # No sum of nested handlers at report level.
    m.observe('quote','QuoteTick',1)
    assert m.snapshot()['handlers'][0]['share_of_window_pct'] is None


def test_adapter_boundary_breakdown_preserves_book_and_publish_order(monkeypatch):
    events = []
    monkeypatch.setattr(timing, 'diagnostic', lambda *a, **k: None)
    node, host, points = fake_node()
    clock = Clock()
    class QuoteTick:
        pass
    class OrderBookDeltas:
        pass
    class PolymarketQuotes:
        pass
    class Client:
        def __init__(self):
            self._ws_client = SimpleNamespace(_handler=None)
        def _handle_raw_ws_message(self, raw):
            events.append(('raw',raw))
            if raw != b'PONG':
                self._handle_ws_message(PolymarketQuotes())
        def _handle_ws_message(self, message):
            self._handle_quotes(message)
        def _handle_quotes(self, message):
            for value in [1,2,3]:
                self._apply_quote_change(value)
            self._publish_quote(message)
            self._publish_l2_snapshot_if_due(message)
        def _apply_quote_change(self, value):
            events.append(('delta',value))
            clock.value += .001
        def _publish_quote(self, message):
            events.append(('generate',None))
            self._queue_latest_quote(QuoteTick())
        def _queue_latest_quote(self, quote):
            events.append(('coalesce',None))
            self._handle_data(quote)
        def _publish_l2_snapshot_if_due(self, message):
            self._handle_data(OrderBookDeltas())
        def _handle_data(self, data):
            events.append(('publish',type(data).__name__))
    client = Client()
    node.kernel.data_engine._clients = {'client':client}
    timing.bind_consumer_timing(node,host)
    host._consumer_timing.now = clock
    host._consumer_timing.started = 0
    client._ws_client._handler(b'update')
    client._ws_client._handler(b'PONG')
    assert events == [('raw',b'update'),('delta',1),('delta',2),('delta',3),
        ('generate',None),('coalesce',None),('publish','QuoteTick'),
        ('publish','OrderBookDeltas'),('raw',b'PONG')]
    clock.value = 60
    rows = host._consumer_timing.snapshot()['handlers']
    by_key = {(row['handler'],row['event_type']):row for row in rows}
    assert by_key[('adapter_local_book_delta','LocalBookDelta')]['count'] == 3
    assert by_key[('adapter_raw_decoder','RawWebSocket')]['count'] == 1
    assert by_key[('adapter_raw_decoder','HeartbeatPong')]['count'] == 1
    assert by_key[('adapter_data_publish','QuoteTick')]['count'] == 1
    assert by_key[('adapter_data_publish','OrderBookDeltas')]['count'] == 1
    assert by_key[('adapter_message_dispatch','PolymarketQuotes')]['count'] == 1
    assert by_key[('adapter_price_change','PriceChangeMessage')]['total_exec_ms'] == pytest.approx(3)
