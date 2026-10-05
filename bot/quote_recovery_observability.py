"""Bounded observations over existing subscription and lifecycle authorities."""
from __future__ import annotations

import time
from copy import deepcopy
from functools import wraps

from bot.ops import collection_lifecycle


def diagnostic(strategy, event_type, **fields):
    try:
        payload = {**getattr(strategy, 'collection_identity', {}),
                   'run_id': strategy.run_id, 'timestamp': time.time(),
                   'market_slug': getattr(strategy, 'current_market_slug', None), **deepcopy(fields)}
        return bool(strategy.trade_db.enqueue_strategy_event(strategy.run_id, event_type, payload))
    except Exception:
        return False


def subscription_counts(strategy):
    current = {str(x) for x in getattr(strategy, 'current_market_instruments', ())}
    quotes = set(getattr(strategy, '_managed_market_quote_subscription_ids', ()))
    l2 = set(getattr(strategy, '_managed_market_l2_subscription_ids', ()))
    client = getattr(strategy, '_collection_data_client', None)
    ws = getattr(client, '_ws_client', None)
    return dict(current_quote_tokens=len(current & quotes), current_l2_tokens=len(current & l2),
                prewarm_quote_tokens=len(quotes - current),
                unique_venue_assets=len(ws._subscriptions) if ws is not None else None,
                logical_quote_tokens=len(quotes), logical_l2_tokens=len(l2))


def request_refresh(strategy, instruments):
    """Observe requests without changing refcounts or subscription ownership."""
    client = getattr(strategy, '_collection_data_client', None)
    if client is None:
        diagnostic(strategy, 'QUOTE_REFRESH_REQUESTED', completion_tracking='unavailable')
        return
    from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
    pending = {}
    for instrument in instruments:
        token = get_polymarket_token_id(instrument)
        key = str(instrument)
        quote_owned = instrument in client.subscribed_quote_ticks()
        l2_owned = instrument in client.subscribed_order_book_deltas()
        expected = int(quote_owned) + int(l2_owned)
        before = client._ws_client._subscription_counts.get(token, 0)
        record = dict(token=token, instrument=key, ref_count_before=before,
                      expected_ref_count=expected, current_ownership=True,
                      prewarm_ownership=key in getattr(strategy, 'quote_prewarm_instruments', ()),
                      requested_at=time.time(), requested_epoch_ns=time.time_ns(), completed_operations=[], failed=False,
                      fresh_quote_confirmed_at=None, unsubscribe_requested=True, subscribe_requested=True,
                      unsubscribe_tasks_completed=False, subscribe_tasks_completed=False)
        pending[key] = record
        diagnostic(strategy, 'QUOTE_REFRESH_REQUESTED', **record)
        if before > expected:
            diagnostic(strategy, 'QUOTE_REFRESH_REFCOUNT_BLOCKED', **record)
    client._collection_quote_refresh = pending  # current pair only, replaced per request
    strategy._collection_quote_refresh = pending


def observe_subscription(client, instrument, operation, original, command):
    """Use the existing adapter coroutine completion, with no new tasks."""
    # Bind at coroutine creation, not its later event-loop execution.
    records = getattr(client, '_collection_quote_refresh', {})
    record = records.get(str(instrument))
    strategy = getattr(client, '_collection_strategy', None)
    if record is not None and record.get('completion_emitted'):
        record = None
    command_ts = getattr(command, 'ts_init', None)
    if record is not None and command_ts is not None and command_ts < record['requested_epoch_ns']:
        record = None  # A pre-refresh command delayed in the DataEngine queue.
    async def run():
        if record is None or strategy is None:
            return await original(command)
        try:
            record.setdefault('operation_started_at', {})[operation] = time.time()
            record.setdefault('operation_started_monotonic', {})[operation] = time.monotonic()
            diagnostic(strategy, 'QUOTE_REFRESH_TASK_STARTED', **record, operation=operation,
                       task_queue_delay_ms=max(0.0,(time.time_ns()-command_ts)/1e6) if command_ts is not None else None)
        except Exception:
            pass
        try:
            result = await original(command)
        except BaseException as exc:
            record['failed'] = True
            diagnostic(strategy, 'QUOTE_REFRESH_FAILED', **record,
                       operation=operation, exception_type=type(exc).__name__)
            raise
        try:
            record.setdefault('operation_completed_at', {})[operation] = time.time()
            record.setdefault('operation_completed_monotonic', {})[operation] = time.monotonic()
            if operation not in record['completed_operations']:
                record['completed_operations'].append(operation)
            completed = set(record['completed_operations'])
            record['unsubscribe_tasks_completed'] = {'unsubscribe_quote', 'unsubscribe_l2'} <= completed
            record['subscribe_tasks_completed'] = {'subscribe_quote', 'subscribe_l2'} <= completed
            diagnostic(strategy, 'QUOTE_REFRESH_TASK_COMPLETED', **record, operation=operation,
                       ref_count_after=client._ws_client._subscription_counts.get(record['token'], 0))
            required = {'unsubscribe_quote', 'unsubscribe_l2', 'subscribe_quote', 'subscribe_l2'}
            if required <= completed and not record['failed'] and not record.get('completion_emitted'):
                record['completion_emitted'] = True
                diagnostic(strategy, 'QUOTE_REFRESH_TASKS_COMPLETED', **record,
                           ref_count_after=client._ws_client._subscription_counts.get(record['token'], 0),
                           venue_acknowledged=False, **subscription_counts(strategy))
        except Exception:
            pass  # Diagnostics must preserve the original adapter result.
        return result
    return run()


def confirm_fresh_quote(strategy, instrument, received_ts, adapter_emitted_ts=None):
    try:
        record = getattr(strategy, '_collection_quote_refresh', {}).get(str(instrument))
        emitted = received_ts if adapter_emitted_ts is None else adapter_emitted_ts
        if record is not None and record['fresh_quote_confirmed_at'] is None and emitted >= record['requested_at']:
            record['fresh_quote_adapter_emitted_at'] = emitted
            record['fresh_quote_confirmed_at'] = received_ts
            diagnostic(strategy, 'QUOTE_REFRESH_FRESH_QUOTE_CONFIRMED', **record)
    except Exception:
        pass


def bind_collection_observers(node, strategy):
    """Attach observations to this node only; preserve original awaits and results."""
    kernel = node.kernel
    node._collection_shutdown_timestamps = {}
    def point(name, **fields):
        try:
            stages = node._collection_shutdown_timestamps
            if name not in stages:
                stages[name] = {'timestamp': time.time(), 'monotonic_ts': time.monotonic(), **fields}
                collection_lifecycle(strategy, name, **stages[name])
        except Exception:
            pass
    node._collection_shutdown_point = point
    for name in ('_await_trader_residuals', '_await_engines_disconnected'):
        original = getattr(kernel, name)
        label = {'_await_trader_residuals': 'trader_residual_wait',
                 '_await_engines_disconnected': 'engine_disconnect_wait'}[name]
        def wrap_async(original, label):
            @wraps(original)
            async def observed(*args, **kwargs):
                point(label + '_start')
                try:
                    return await original(*args, **kwargs)
                finally:
                    point(label + '_end')
            return observed
        setattr(kernel, name, wrap_async(original, label))
    original = kernel._stop_engines
    @wraps(original)
    def stop_engines(*args, **kwargs):
        point('engine_stop_start')
        try:
            return original(*args, **kwargs)
        finally:
            point('engine_stop_end')
    kernel._stop_engines = stop_engines
    for domain in ('data', 'exec'):
        for client in getattr(getattr(kernel, domain + '_engine'), '_clients', {}).values():
            client._collection_strategy = strategy
            if domain == 'data' and hasattr(client, '_ws_client'):
                strategy._collection_data_client = client
                for method, operation in (
                    ('_unsubscribe_quote_ticks', 'unsubscribe_quote'),
                    ('_unsubscribe_order_book_deltas', 'unsubscribe_l2'),
                    ('_subscribe_quote_ticks', 'subscribe_quote'),
                    ('_subscribe_order_book_deltas', 'subscribe_l2')):
                    original_method = getattr(client, method)
                    def wrap_subscription(original_method, operation, client):
                        @wraps(original_method)
                        def observed(command):
                            return observe_subscription(client, command.instrument_id,
                                                              operation, original_method, command)
                        return observed
                    setattr(client, method, wrap_subscription(original_method, operation, client))
            original_disconnect = client._disconnect
            label = 'data_disconnect' if domain == 'data' else 'execution_disconnect'
            def wrap_disconnect(original_disconnect, label):
                @wraps(original_disconnect)
                async def observed(*args, **kwargs):
                    point(label + '_start')
                    try:
                        return await original_disconnect(*args, **kwargs)
                    finally:
                        point(label + '_adapter_end')
                return observed
            setattr(client, '_disconnect', wrap_disconnect(original_disconnect, label))
            original_cancel = client.cancel_pending_tasks
            def wrap_cleanup(original_cancel, label):
                @wraps(original_cancel)
                async def observed(*args, **kwargs):
                    try:
                        return await original_cancel(*args, **kwargs)
                    finally:
                        if label + '_start' in node._collection_shutdown_timestamps:
                            point(label + '_end', boundary='cleanup_complete_before_connected_flag_update')
                return observed
            client.cancel_pending_tasks = wrap_cleanup(original_cancel, label)
