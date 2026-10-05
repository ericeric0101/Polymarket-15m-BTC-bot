"""In-memory quote generation preserves real local-book and QuoteTick semantics."""
from types import SimpleNamespace
import threading

import pytest
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import BookAction, BookType, OrderSide, RecordFlag
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from bot.adapter_overrides import (install_runtime_compatibility_overrides,
    request_quote_generation, cancel_quote_generation, cancel_quote_delivery_tasks,
    quote_provenance_for_tick)
from bot.consumer_timing import ConsumerTiming


class Loop:
    def __init__(self):
        self.ready=[]
        self.delays=[]
    def call_soon(self, function, *args):
        handle=SimpleNamespace(cancelled=False)
        handle.cancel=lambda:setattr(handle,'cancelled',True)
        self.ready.append((handle,function,args))
        return handle
    def call_later(self, delay, function, *args):
        self.delays.append(delay)
        return self.call_soon(function,*args)
    def turn(self):
        current,self.ready=self.ready,[]
        for handle,function,args in current:
            if not handle.cancelled:
                function(*args)


def setup_client():
    install_runtime_compatibility_overrides()
    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
    import nautilus_trader.adapters.polymarket.data as data
    ids=[InstrumentId.from_str('condition-1.POLYMARKET'),InstrumentId.from_str('condition-2.POLYMARKET')]
    instruments=[SimpleNamespace(id=i,make_price=lambda x:Price(float(x),2),
        make_qty=lambda x:Quantity.from_str(str(int(x)))) for i in ids]
    subscribed=set(ids)
    quotes=[]
    client=SimpleNamespace(_loop=Loop(),_btc15m_disconnecting=False,
        _btc15m_raw_ws_received_ts=0,_quote_delivery_coalesce_sec=.25,
        _clock=SimpleNamespace(timestamp_ns=lambda:10**12),
        _local_books={i:OrderBook(i,BookType.L2_MBP) for i in ids},
        subscribed_quote_ticks=lambda:set(subscribed), subscribed_order_book_deltas=lambda:set(),
        _last_quotes={},_quote_heartbeat_emit_ns={},_quote_heartbeat_sec=5,
        _config=SimpleNamespace(drop_quotes_missing_side=True),
        _queue_latest_quote=lambda quote:quotes.append(quote))
    for inst in instruments:
        order=BookOrder(OrderSide.SELL,Price.from_str('0.60'),Quantity.from_str('1'),0)
        delta=OrderBookDelta(inst.id,BookAction.UPDATE,order,RecordFlag.F_LAST,0,1,1)
        client._local_books[inst.id].apply(OrderBookDeltas(inst.id,[delta]))
    client._publish_quote=lambda inst,msg,**kw:PolymarketDataClient._publish_quote(client,inst,msg,**kw)
    client._apply_quote_change=lambda inst,msg,change,**kw:PolymarketDataClient._apply_quote_change(client,inst,msg,change,**kw)
    def update(inst, size, timestamp=None):
        message=SimpleNamespace(timestamp=size if timestamp is None else timestamp)
        change=SimpleNamespace(side=data.PolymarketOrderSide.BUY,price='0.40',size=str(size))
        assert client._apply_quote_change(inst,message,change,publish_delta=False)
        client._btc15m_raw_ws_received_ts=size/1000
        request_quote_generation(client,inst,message)
    return client,instruments,subscribed,quotes,update


def test_ten_thousand_real_book_updates_generate_one_latest_quote_without_thread():
    client,insts,subscribed,quotes,update=setup_client()
    before=threading.active_count()
    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
    import nautilus_trader.adapters.polymarket.data as data
    applications=[]
    l2_attempts=[]
    original_apply=client._apply_quote_change
    def apply(*args,**kwargs):
        applications.append(1)
        return original_apply(*args,**kwargs)
    client._apply_quote_change=apply
    client._cache=SimpleNamespace(instrument=lambda id:next(inst for inst in insts if inst.id==id))
    client._publish_l2_snapshot_if_due=lambda *args:l2_attempts.append(1)
    for n in range(1,10002):
        client._btc15m_raw_ws_received_ts=n/1000
        change=SimpleNamespace(asset_id='1',side=data.PolymarketOrderSide.BUY,price='0.40',size=str(n))
        PolymarketDataClient._handle_quotes(client,SimpleNamespace(market='condition',timestamp=n,price_changes=[change]))
    assert len(applications)==len(l2_attempts)==10001
    assert int(client._local_books[insts[0].id].best_bid_size()) == 10001
    assert quotes == []
    assert len(client._quote_generation_pending)==1
    assert len(client._loop.ready)==1
    client._loop.turn()
    assert len(quotes)==1
    assert int(quotes[0].bid_size)==10001
    assert str(quotes[0].bid_price)=='0.40'
    assert str(quotes[0].ask_price)=='0.60'
    assert quotes[0].ts_event == 10001*1000000
    assert quotes[0].ts_init==10**12
    assert quote_provenance_for_tick(quotes[0])['raw_ws_received_ts']==10.001
    assert client._quote_generation_pending == {}
    assert client._quote_generation_flush_handle is None
    assert threading.active_count()==before


def test_current_and_prewarm_have_independent_latest_state_and_provenance():
    client,insts,subscribed,quotes,update=setup_client()
    update(insts[0],11)
    update(insts[1],22)
    update(insts[0],33)
    client._loop.turn()
    by_id={q.instrument_id:q for q in quotes}
    assert int(by_id[insts[0].id].bid_size)==33
    assert int(by_id[insts[1].id].bid_size)==22
    assert quote_provenance_for_tick(by_id[insts[0].id])['raw_ws_received_ts']==.033
    assert quote_provenance_for_tick(by_id[insts[1].id])['raw_ws_received_ts']==.022


def test_reentrant_update_schedules_later_turn_without_recursion():
    client,insts,subscribed,quotes,update=setup_client()
    original=client._publish_quote
    def publish(inst,msg,**kw):
        original(inst,msg,**kw)
        if len(quotes)==1:
            update(inst,99)
    client._publish_quote=publish
    update(insts[0],1)
    client._loop.turn()
    assert len(quotes)==1
    assert len(client._loop.ready)==1
    client._loop.turn()
    assert len(quotes)==2
    assert int(quotes[-1].bid_size)==99
    assert client._loop.ready==[]


def test_transient_failure_keeps_dirty_latest_metadata_and_does_not_wedge():
    client,insts,subscribed,quotes,update=setup_client()
    original=client._publish_quote
    def fail(inst,msg,**kw):
        raise ValueError('synthetic failure')
    client._publish_quote=fail
    update(insts[0],5)
    client._loop.turn()
    assert len(client._quote_generation_pending)==1
    assert client._loop.delays==[.25]  # Failure-only retry, no normal debounce.
    update(insts[0],7)
    assert len(client._loop.ready)==1
    client._publish_quote=original
    client._loop.turn()
    assert int(quotes[0].bid_size)==7
    assert client._quote_generation_pending=={}


def test_handoff_and_dispose_cancel_obsolete_generation():
    client,insts,subscribed,quotes,update=setup_client()
    update(insts[0],1)
    update(insts[1],2)
    subscribed.remove(insts[0].id)
    cancel_quote_generation(client,insts[0].id)
    client._loop.turn()
    assert [q.instrument_id for q in quotes]==[insts[1].id]
    update(insts[1],3)
    cancel_quote_delivery_tasks(client)
    client._loop.turn()
    assert len(quotes)==1
    assert client._quote_generation_pending=={}
    assert client._quote_generation_flush_handle is None
    update(insts[1],4)
    assert client._loop.ready==[]


def test_cancel_then_resubscribe_cannot_reuse_obsolete_metadata():
    client,insts,subscribed,quotes,update=setup_client()
    update(insts[0],1)
    cancel_quote_generation(client,insts[0].id)
    update(insts[0],2)
    client._loop.turn()
    assert len(quotes)==1 and quotes[0].ts_event==2000000


def test_pending_storage_bound_to_owned_quote_instruments():
    client,insts,subscribed,quotes,update=setup_client()
    update(insts[0],1)
    subscribed.remove(insts[0].id)
    for n in range(10000):
        request_quote_generation(client,SimpleNamespace(id=f'obsolete-{n}'),SimpleNamespace(timestamp=n))
    assert client._quote_generation_pending=={}
    client._loop.turn()
    assert quotes==[]


def test_generation_window_counters_reuse_existing_summary():
    client,insts,subscribed,quotes,update=setup_client()
    clock=SimpleNamespace(value=0)
    monitor=ConsumerTiming(SimpleNamespace(),client._loop,monotonic=lambda:clock.value)
    client._consumer_timing=monitor
    monitor.wrap(client,'_publish_quote','adapter_quote_generation','QuoteGeneration')
    for n in range(1,101):
        update(insts[0],n)
    client._loop.turn()
    clock.value=60
    report=monitor.snapshot()['quote_generation']
    assert report['quote_generation_requested']==100
    assert report['quote_generation_executed']==1
    assert report['quote_generation_coalesced']==99
    assert report['requested_per_min']==100
    assert report['executed_per_min']==1
    assert report['coalescing_ratio']==.99
    assert report['share_of_window_pct']==0


def test_public_unsubscribe_cleans_only_obsolete_generation_and_delegates_ownership():
    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
    client,insts,subscribed,quotes,update=setup_client()
    update(insts[0],1)
    update(insts[1],2)
    removed=[]
    client._remove_subscription_quote_ticks=lambda id:(removed.append(id),subscribed.remove(id))
    async def unsubscribe(command):
        pass
    client._unsubscribe_quote_ticks=unsubscribe
    created=[]
    def create(coro,**kwargs):
        created.append(1)
        coro.close()
    client.create_task=create
    PolymarketDataClient.unsubscribe_quote_ticks(client,SimpleNamespace(instrument_id=insts[0].id))
    assert removed==[insts[0].id] and created==[1]
    client._loop.turn()
    assert [q.instrument_id for q in quotes]==[insts[1].id]


def test_real_owner_loop_generation_cost_and_counter_share():
    import asyncio
    async def exercise():
        client,insts,subscribed,quotes,update=setup_client()
        client._loop=asyncio.get_running_loop()
        clock=SimpleNamespace(value=0)
        monitor=ConsumerTiming(SimpleNamespace(),client._loop,monotonic=lambda:clock.value)
        client._consumer_timing=monitor
        original=client._publish_quote
        def publish(*args,**kwargs):
            original(*args,**kwargs)
            clock.value+=.01
        client._publish_quote=publish
        monitor.wrap(client,'_publish_quote','adapter_quote_generation','QuoteGeneration')
        for n in range(1,101):
            update(insts[0],n)
        await asyncio.sleep(0)
        clock.value=60
        row=monitor.snapshot()['quote_generation']
        assert row['quote_generation_executed']==1
        assert row['quote_generation_total_exec_ms']==pytest.approx(10)
        assert row['share_of_window_pct']==pytest.approx(10/60000*100)
    asyncio.run(exercise())


def test_delayed_flush_preserves_latest_update_clock_and_staleness_contract():
    from bot.market_runtime import quote_delivery_is_fresh
    client,insts,subscribed,quotes,update=setup_client()
    clock=SimpleNamespace(value=1800000000000000000)
    client._clock.timestamp_ns=lambda:clock.value
    update(insts[0],1)
    clock.value+=1000000000
    update(insts[0],2)
    expected=clock.value
    clock.value+=10000000000
    client._loop.turn()
    assert quotes[0].ts_init==expected
    assert not quote_delivery_is_fresh(received_ts=clock.value/1e9,
        adapter_emitted_ts=quotes[0].ts_init/1e9,max_delivery_delay_sec=2,
        clock_skew_tolerance_sec=.25)
