import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from bot.db_runtime import StrategyDBRuntimeMixin
from bot.market_runtime import replace_market_subscriptions
from bot.quote_recovery_observability import (
    bind_collection_observers, confirm_fresh_quote, request_refresh, subscription_counts,
)
from monitoring.trade_journal_db import TradeJournalDB


def host(journal):
    return SimpleNamespace(trade_db=journal, run_id='r', current_market_slug='m',
        instrument_id=None, collection_identity={'process_instance_id':'p', 'cycle_idx':2})


def test_quote_telemetry_is_only_async_event_and_failure_nonfatal():
    calls=[]
    journal=SimpleNamespace(enqueue_strategy_event=lambda *a: calls.append(('async',a)) or True,
        log_strategy_event=lambda **k: calls.append(('sync',k)) or True)
    strategy=host(journal)
    payload={'quote_received_ts':123, 'quote_source':'ws_snapshot'}
    assert StrategyDBRuntimeMixin._db_strategy_event(strategy, 'QUOTE_TRANSPORT_TELEMETRY',payload)
    assert calls[0][0]=='async'
    assert calls[0][1][2]['quote_received_ts']==123
    assert calls[0][1][2]['process_instance_id']=='p'
    assert StrategyDBRuntimeMixin._db_strategy_event(strategy,'MARKET_CYCLE_PNL',{})
    assert calls[-1][0]=='sync'
    journal.enqueue_strategy_event=lambda *a: (_ for _ in ()).throw(OSError())
    assert StrategyDBRuntimeMixin._db_strategy_event(strategy,'QUOTE_TRANSPORT_TELEMETRY',payload) is False
    assert payload=={'quote_received_ts':123,'quote_source':'ws_snapshot'}


def test_quote_telemetry_persists_async_preserving_occurrence_timestamp(tmp_path):
    journal=TradeJournalDB(str(tmp_path/'journal.db'))
    strategy=host(journal)
    assert StrategyDBRuntimeMixin._db_strategy_event(strategy,'QUOTE_TRANSPORT_TELEMETRY',{'quote_received_ts':123})
    journal.stop()
    with sqlite3.connect(journal.db_path) as conn:
        event, payload, ts=conn.execute('select event_type,payload_json,ts from strategy_events').fetchone()
    assert event=='QUOTE_TRANSPORT_TELEMETRY'
    assert json.loads(payload)['quote_received_ts']==123
    assert ts is not None


def instrument(token):
    return InstrumentId.from_str(f'condition-{token}.POLYMARKET')


def observer_node(strategy, refs=2):
    up, down=getattr(strategy,'current_market_instruments',None) or [instrument('1'),instrument('2')]
    strategy.current_market_instruments=[up,down]
    strategy.quote_prewarm_instruments=set()
    operations=[]
    tokens=[i.symbol.value.split('-')[1] for i in (up,down)]
    ws=SimpleNamespace(_subscription_counts={tokens[0]:refs,tokens[1]:2},_subscriptions=tokens)
    def operation(token, delta):
        async def run(command):
            await asyncio.sleep(0)
            key=command.instrument_id.symbol.value.split('-')[1]
            before=ws._subscription_counts.get(key,0)
            ws._subscription_counts[key]=before+delta
            if delta==-1 and before==1: operations.append(('unsubscribe',key))
            if delta==1 and before==0: operations.append(('subscribe',key))
        return run
    async def done(*a,**k): return True
    client=SimpleNamespace(_ws_client=ws, subscribed_quote_ticks=lambda:{up,down},
        subscribed_order_book_deltas=lambda:{up,down},_disconnect=done,cancel_pending_tasks=done,
        _unsubscribe_quote_ticks=operation('',-1),_unsubscribe_order_book_deltas=operation('',-1),
        _subscribe_quote_ticks=operation('',1),_subscribe_order_book_deltas=operation('',1))
    execution=SimpleNamespace(_disconnect=done,cancel_pending_tasks=done)
    kernel=SimpleNamespace(_await_trader_residuals=done,_await_engines_disconnected=done,
        _stop_engines=lambda:None, data_engine=SimpleNamespace(_clients={'p':client}),
        exec_engine=SimpleNamespace(_clients={'p':execution}))
    node=SimpleNamespace(kernel=kernel)
    bind_collection_observers(node,strategy)
    return node,client,up,operations


@pytest.mark.parametrize('refs,blocked',[(2,False),(3,True)])
def test_refresh_request_completion_freshness_and_refcounts_are_distinct(refs,blocked):
    events=[]
    journal=SimpleNamespace(enqueue_strategy_event=lambda r,e,p:events.append((e,p)) or True)
    strategy=host(journal)
    node,client,up,operations=observer_node(strategy,refs)
    request_refresh(strategy,[up])
    assert [e for e,p in events].count('QUOTE_REFRESH_REFCOUNT_BLOCKED')==int(blocked)
    assert 'QUOTE_REFRESH_TASKS_COMPLETED' not in [e for e,p in events]
    async def refresh():
        command=SimpleNamespace(instrument_id=up)
        for name in ('_unsubscribe_quote_ticks','_unsubscribe_order_book_deltas',
                     '_subscribe_quote_ticks','_subscribe_order_book_deltas'):
            await getattr(client,name)(command)
    asyncio.run(refresh())
    assert [e for e,p in events].count('QUOTE_REFRESH_TASKS_COMPLETED')==1
    assert 'QUOTE_REFRESH_FRESH_QUOTE_CONFIRMED' not in [e for e,p in events]
    confirm_fresh_quote(strategy,up,time.time())
    confirm_fresh_quote(strategy,up,time.time())
    assert [e for e,p in events].count('QUOTE_REFRESH_FRESH_QUOTE_CONFIRMED')==1
    assert operations==([] if blocked else [('unsubscribe','1'),('subscribe','1')])
    assert events[0][1]['completed_operations']==[]  # enqueue payload is immutable snapshot
    assert client._ws_client._subscription_counts['1']==refs
    count=len(events)
    asyncio.run(client._subscribe_quote_ticks(SimpleNamespace(instrument_id=up)))
    assert len(events)==count  # Later unrelated ownership work is not this refresh.
    assert len(strategy._collection_quote_refresh[str(up)]['completed_operations'])==4


def test_repeated_handoffs_remain_bounded_and_refresh_keeps_ownership():
    events=[]
    strategy=host(SimpleNamespace(enqueue_strategy_event=lambda r,e,p:events.append((e,p)) or True))
    quotes,l2=set(),set()
    strategy.subscribe_quote_ticks=lambda i:quotes.add(i)
    strategy.unsubscribe_quote_ticks=lambda i:quotes.remove(i)
    strategy.subscribe_order_book_deltas=lambda i:l2.add(i)
    strategy.unsubscribe_order_book_deltas=lambda i:l2.remove(i)
    previous=[]
    for cycle in range(4):
        current=[instrument(str(cycle*2+1)),instrument(str(cycle*2+2))]
        next_pair=[instrument(str(cycle*2+3)),instrument(str(cycle*2+4))]
        strategy.current_market_instruments=current
        assert replace_market_subscriptions(strategy,previous,current,prewarm_instrument_ids=next_pair)
        assert len(quotes)==4 and len(l2)==2
        assert l2==set(current)
        assert quotes==set(current+next_pair)
        assert subscription_counts(strategy)['prewarm_quote_tokens']==2
        previous=current
    assert len(events)==4
    _,client,up,operations=observer_node(strategy)
    request_refresh(strategy,[up])
    async def refresh():
        command=SimpleNamespace(instrument_id=up)
        for name in ('_unsubscribe_quote_ticks','_unsubscribe_order_book_deltas',
                     '_subscribe_quote_ticks','_subscribe_order_book_deltas'):
            await getattr(client,name)(command)
    asyncio.run(refresh())
    assert operations==[('unsubscribe','7'),('subscribe','7')]
    assert quotes==set(current+next_pair) and l2==set(current)



def test_shutdown_observer_retains_monotonic_stages_after_journal_stops():
    journal=SimpleNamespace(enqueue_strategy_event=lambda *a:False)
    strategy=host(journal)
    node,client,up,ops=observer_node(strategy)
    async def shutdown():
        node._collection_shutdown_point('stop_dispatch')
        node._collection_shutdown_point('owner_loop_stop_callback_start')
        node._collection_shutdown_point('strategy_teardown_start')
        node._collection_shutdown_point('strategy_teardown_end')
        await node.kernel._await_trader_residuals()
        await client._disconnect()
        await client.cancel_pending_tasks()
        execution=node.kernel.exec_engine._clients['p']
        await execution._disconnect()
        await execution.cancel_pending_tasks()
        await node.kernel._await_engines_disconnected()
        node.kernel._stop_engines()
    asyncio.run(shutdown())
    stages=node._collection_shutdown_timestamps
    times=[v['monotonic_ts'] for v in stages.values()]
    assert times==sorted(times)
    assert 'data_disconnect_end' in stages and 'execution_disconnect_end' in stages
    count=len(stages)
    node.kernel._stop_engines()
    assert len(stages)==count


def test_refresh_failure_is_distinct_and_original_exception_is_preserved():
    events=[]
    strategy=host(SimpleNamespace(enqueue_strategy_event=lambda r,e,p:events.append(e) or True))
    _,client,up,_=observer_node(strategy)
    request_refresh(strategy,[up])
    from bot.quote_recovery_observability import observe_subscription
    async def fail(command): raise ValueError('adapter failure')
    with pytest.raises(ValueError,match='adapter failure'):
        asyncio.run(observe_subscription(client,up,'subscribe_quote',fail,SimpleNamespace(instrument_id=up)))
    assert 'QUOTE_REFRESH_FAILED' in events
    assert 'QUOTE_REFRESH_TASKS_COMPLETED' not in events


def test_refresh_observation_failure_cannot_change_adapter_result():
    strategy=host(SimpleNamespace(enqueue_strategy_event=lambda *a:(_ for _ in ()).throw(OSError())))
    _,client,up,_=observer_node(strategy)
    request_refresh(strategy,[up])
    async def refresh():
        return await client._subscribe_quote_ticks(SimpleNamespace(instrument_id=up))
    assert asyncio.run(refresh()) is None
    confirm_fresh_quote(strategy,up,time.time())


def test_refresh_completion_is_bound_at_request_creation_not_later_execution():
    events=[]
    strategy=host(SimpleNamespace(enqueue_strategy_event=lambda r,e,p:events.append((e,p)) or True))
    _,client,up,_=observer_node(strategy)
    command=SimpleNamespace(instrument_id=up)
    old_operation=client._subscribe_quote_ticks(command)
    request_refresh(strategy,[up])
    asyncio.run(old_operation)
    assert 'QUOTE_REFRESH_TASK_COMPLETED' not in [e for e,p in events]
    record=strategy._collection_quote_refresh[str(up)]
    operation=client._unsubscribe_quote_ticks(command)
    request_refresh(strategy,[up])
    new_record=strategy._collection_quote_refresh[str(up)]
    asyncio.run(operation)
    assert record['completed_operations']==['unsubscribe_quote']
    assert new_record['completed_operations']==[]


def test_pre_refresh_dataengine_command_cannot_confirm_new_refresh():
    events=[]
    strategy=host(SimpleNamespace(enqueue_strategy_event=lambda r,e,p:events.append(e) or True))
    _,client,up,_=observer_node(strategy)
    command=SimpleNamespace(instrument_id=up,ts_init=time.time_ns()-1000000)
    request_refresh(strategy,[up])
    asyncio.run(client._subscribe_quote_ticks(command))
    assert 'QUOTE_REFRESH_TASK_COMPLETED' not in events


def test_delayed_pre_refresh_quote_cannot_confirm_new_refresh():
    events=[]
    strategy=host(SimpleNamespace(enqueue_strategy_event=lambda r,e,p:events.append(e) or True))
    _,client,up,_=observer_node(strategy)
    request_refresh(strategy,[up])
    now=time.time()
    confirm_fresh_quote(strategy,up,now,adapter_emitted_ts=now-1)
    assert 'QUOTE_REFRESH_FRESH_QUOTE_CONFIRMED' not in events
    confirm_fresh_quote(strategy,up,now,adapter_emitted_ts=now)
    assert 'QUOTE_REFRESH_FRESH_QUOTE_CONFIRMED' in events
