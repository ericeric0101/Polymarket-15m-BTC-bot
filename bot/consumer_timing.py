"""Bounded best-effort callback observations, persisted through the existing journal."""
from __future__ import annotations

import asyncio
import inspect
import random
import threading
import time
from functools import wraps

from bot.quote_recovery_observability import diagnostic


class ConsumerTiming:
    WINDOW_SEC = 60.0
    SAMPLE_LIMIT = 512
    HANDLER_LIMIT = 40

    def __init__(self, strategy, loop, *, monotonic=time.monotonic):
        self.strategy, self.loop, self.now = strategy, loop, monotonic
        self.lock = threading.Lock()
        self.states = {}
        self.started = self.now()
        self.last_slow = float('-inf')
        self.dropped = 0
        self.random = random.Random(0)
        self.timer = None
        self.closed = False
        self.emitting = False
        self.last_lag = None
        self.enqueued = {}
        self.dequeued = {}

    def mark_enqueued(self, event):
        if not self.lock.acquire(blocking=False):
            return
        try:
            if len(self.enqueued) < 32:
                self.enqueued[id(event)] = self.now()
        finally:
            self.lock.release()

    def observe(self, handler, event_type, duration_ms, *, instrument=None, queue_wait_ms=None):
        if self.emitting or not self.lock.acquire(blocking=False):
            self.dropped += 1
            return
        report = slow = None
        try:
            now = self.now()
            try:
                owner_loop = asyncio.get_running_loop() is self.loop
            except RuntimeError:
                owner_loop = False
            key = (handler, event_type, owner_loop)
            if key not in self.states and len(self.states) >= self.HANDLER_LIMIT:
                self.dropped += 1
                return
            state = self.states.setdefault(key, {'count':0, 'samples':[], 'max':0.0, 'slow_count':0})
            value = max(0.0, float(duration_ms))
            state['count'] += 1
            state['max'] = max(state['max'], value)
            state['slow_count'] += int(value > 100.0)
            samples = state['samples']
            if len(samples) < self.SAMPLE_LIMIT:
                samples.append(value)
            else:
                index = self.random.randrange(state['count'])
                if index < self.SAMPLE_LIMIT:
                    samples[index] = value
            if value > 100.0 and now - self.last_slow >= 10.0:
                self.last_slow = now
                slow = dict(handler=handler,event_type=event_type,instrument=instrument,
                    duration_ms=value,callback_exec_ms=None if handler in {'loop_lag','data_engine_queue_wait','dequeue_to_handler'} or handler.endswith('_await') else value,
                    queue_wait_ms=queue_wait_ms,owner_loop=owner_loop,
                    threshold_ms=1000 if value>1000 else 500 if value>500 else 100)
            if now - self.started >= self.WINDOW_SEC:
                report = self._snapshot(now)
        except Exception:
            self.dropped += 1
        finally:
            self.lock.release()
        self._emit(report, slow)

    def _snapshot(self, now):
        handlers = []
        for (handler,event_type,owner_loop), state in self.states.items():
            samples = sorted(state['samples'])
            def quantile(p):
                return samples[int((len(samples)-1)*p)] if samples else None
            handlers.append(dict(handler=handler,event_type=event_type,owner_loop=owner_loop,
                timing_scope='await_inclusive' if handler.endswith('_await') else 'boundary_delay' if handler in {'loop_lag','data_engine_queue_wait','dequeue_to_handler'} else 'synchronous_inclusive',
                count=state['count'],sample_count=len(samples),median=quantile(.5),
                P95=quantile(.95),P99=quantile(.99),max=state['max'],slow_count=state['slow_count']))
        report = dict(window_sec=max(0.0,now-self.started),handlers=handlers,
            dropped_observations=self.dropped,units='ms',quantile_scope='bounded_uniform_reservoir',
            opaque_stages=['cache_update','message_bus_publish'],last_loop_lag=self.last_lag)
        self.states = {}
        self.started = now
        self.dropped = 0
        return report

    def snapshot(self):
        if not self.lock.acquire(blocking=False):
            return {'unavailable':'collector_busy'}
        try:
            return self._snapshot(self.now())
        finally:
            self.lock.release()

    def _emit(self, report, slow):
        if not report and not slow:
            return
        self.emitting = True
        try:
            if report:
                diagnostic(self.strategy,'EVENT_LOOP_CONSUMER_TIMING',**report)
            if slow:
                diagnostic(self.strategy,'SLOW_CONSUMER_CALLBACK',**slow)
        except Exception:
            pass  # Diagnostics must never replace a callback result or exception.
        finally:
            self.emitting = False

    def start(self):
        if self.timer is not None or self.closed:
            return
        self._schedule()

    def _schedule(self):
        expected = self.loop.time()+1.0
        self.timer = self.loop.call_later(1.0,self.beat,expected)

    def beat(self, scheduled_at):
        self.timer = None
        if self.closed:
            return
        executed_at = self.loop.time()
        lag = max(0.0,(executed_at-scheduled_at)*1000.0)
        self.last_lag = dict(scheduled_at=scheduled_at,executed_at=executed_at,lag_ms=lag,
                             clock='event_loop_monotonic')
        self.observe('loop_lag','Timer',lag)
        self._schedule()

    def close(self):
        self.closed = True
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def wrap(self, owner, name, handler, event_type):
        original = getattr(owner,name,None)
        if not callable(original):
            return False
        @wraps(original)
        def measured(*args,**kwargs):
            started = self.now()
            event = args[0] if args else None
            wait = None
            if handler=='data_engine_handler' and event is not None:
                if self.lock.acquire(blocking=False):
                    try:
                        enqueued = self.enqueued.pop(id(event),None)
                        dequeued = self.dequeued.pop(id(event),None)
                    finally:
                        self.lock.release()
                    if enqueued is not None and dequeued is not None:
                        wait = max(0.0,(dequeued-enqueued)*1000.0)
                        self.observe('data_engine_queue_wait',type(event).__name__,wait)
                    if dequeued is not None:
                        self.observe('dequeue_to_handler',type(event).__name__,max(0.0,(started-dequeued)*1000.0))
            try:
                return original(*args,**kwargs)
            finally:
                self.observe(handler,type(event).__name__ if event_type=='dynamic' and event is not None else event_type,
                             (self.now()-started)*1000.0,
                             instrument=str(getattr(event,'instrument_id','')) or None,queue_wait_ms=wait)
        if inspect.iscoroutinefunction(original):
            @wraps(original)
            async def measured_async(*args, **kwargs):
                started = self.now()
                try:
                    return await original(*args, **kwargs)
                finally:
                    self.observe(handler + '_await', event_type, (self.now()-started)*1000.0)
            measured = measured_async
        try:
            setattr(owner,name,measured)
            return True
        except (AttributeError,TypeError):
            return False


def bind_consumer_timing(node,strategy):
    kernel = node.kernel
    monitor = ConsumerTiming(strategy,kernel.loop)
    strategy._consumer_timing = monitor
    node._consumer_timing = monitor
    hooked = {}
    kernel.data_engine._consumer_timing = monitor
    queue = getattr(kernel.data_engine,'_data_queue',None)
    if queue is not None:
        original_get = queue.get
        async def timed_get():
            event = await original_get()
            if monitor.lock.acquire(blocking=False):
                try:
                    if len(monitor.dequeued)<32:
                        monitor.dequeued[id(event)] = monitor.now()
                finally:
                    monitor.lock.release()
            return event
        queue.get = timed_get
    for owner,name,label,kind in (
        (kernel.data_engine,'_handle_data','data_engine_handler','dynamic'),
        (kernel.data_engine,'_handle_quote_tick','quote_cache_bus_dispatch','QuoteTick'),
        (kernel.data_engine,'_handle_order_book_deltas','l2_cache_bus_dispatch','OrderBookDeltas'),
        (strategy,'on_quote_tick','strategy_quote_callback','QuoteTick'),
        (strategy,'on_order_book_deltas','strategy_l2_callback','OrderBookDeltas'),
        (strategy,'_compute_fair_probability','quote_decision_compute','Decision'),
        (strategy,'_find_btc_instrument','lifecycle_market_selection','Lifecycle'),
        (strategy,'_update_market_phase','lifecycle_phase','Lifecycle'),
        (strategy,'_maybe_run_quote_watchdog','watchdog_check','Timer'),
        (strategy.trade_db,'enqueue_strategy_event','journal_enqueue','Journal'),
        (strategy,'_db_strategy_event','journal_event','Journal'),
        (strategy,'_db_order_event','journal_order','Journal'),
        (strategy,'_lead_lag_observation_on_quote','lead_lag_quote','QuoteTick'),
        (strategy,'_shadow_simulation_on_quote','shadow_simulation_quote','QuoteTick'),
        (strategy,'_depth_risk_shadow_on_quote','depth_shadow_quote','QuoteTick'),
        (strategy,'_emit_strategy_status','status_timer','Timer'),
        (strategy,'_get_dynamic_fee_rate','fee_lookup','Decision'),
        (strategy,'_get_orderbook_levels_for_instrument','decision_cache_read','Decision')):
        hooked[label] = monitor.wrap(owner,name,label,kind)
    for client in getattr(kernel.data_engine,'_clients',{}).values():
        for name,label in (('_handle_raw_ws_message','adapter_raw_decoder'),
                           ('_handle_quotes','adapter_price_change'),
                           ('_handle_book_snapshot','adapter_book_snapshot')):
            hooked[label] = monitor.wrap(client,name,label,'Adapter')
        # The websocket captured its bound raw handler at client construction.
        ws = getattr(client,'_ws_client',None)
        if ws is not None and hooked.get('adapter_raw_decoder'):
            ws._handler = client._handle_raw_ws_message
    original_start = kernel.start_async
    @wraps(original_start)
    async def start(*args,**kwargs):
        monitor.start()
        return await original_start(*args,**kwargs)
    kernel.start_async = start
    def shutdown_point(stage, **context):
        try:
            node._collection_shutdown_point(stage, **context)
        except Exception:
            pass

    original_stop = kernel.stop_async
    @wraps(original_stop)
    async def stop(*args,**kwargs):
        try:
            result = await original_stop(*args,**kwargs)
        except BaseException:
            shutdown_point('kernel_stop_failed', is_running=kernel.is_running)
            raise
        else:
            shutdown_point('kernel_stop_complete', is_running=kernel.is_running)
            return result
        finally:
            monitor.close()
    kernel.stop_async = stop
    workers = {}
    for engine_name in ('data_engine','risk_engine','exec_engine'):
        engine = getattr(kernel,engine_name,None)
        for getter in ('get_cmd_queue_task','get_req_queue_task','get_res_queue_task',
                       'get_data_queue_task','get_evt_queue_task'):
            method = getattr(engine,getter,None)
            if not callable(method):
                continue
            original_getter = method
            def wrap_getter(original_getter,label):
                @wraps(original_getter)
                def observed():
                    task = original_getter()
                    if task is not None and not getattr(task,'_consumer_timing_bound',False):
                        task._consumer_timing_bound = True
                        workers[label] = task
                        task.add_done_callback(lambda done:shutdown_point(label+'_worker_end',cancelled=done.cancelled()))
                    return task
                return observed
            setattr(engine,getter,wrap_getter(original_getter,engine_name+'.'+getter))
    original_engine_stop = kernel._stop_engines
    @wraps(original_engine_stop)
    def request_engine_stop(*args,**kwargs):
        for label,task in workers.items():
            if not task.done():
                shutdown_point(label+'_stop_start')
        return original_engine_stop(*args,**kwargs)
    kernel._stop_engines = request_engine_stop
    diagnostic(strategy,'CONSUMER_TIMING_HOOKS' ,hooks=hooked,
               opaque_stages=['cache_update','message_bus_publish'])


def timed_call(strategy, handler, event_type, function, *args, **kwargs):
    monitor = getattr(strategy,'_consumer_timing',None)
    if monitor is None:
        return function(*args,**kwargs)
    started = monitor.now()
    try:
        return function(*args,**kwargs)
    finally:
        tick = kwargs.get('tick')
        instrument = kwargs.get('instrument_id') or getattr(tick, 'instrument_id', None)
        monitor.observe(handler,event_type,(monitor.now()-started)*1000.0,
                        instrument=str(instrument) if instrument is not None else None)
