"""Current Phase A runtime contract; no override of the retired execution gate."""
import ast
import asyncio
import inspect
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from bot.outcome_lead_lag_exit_handoff import OutcomeFastFollowLive, FastFollowLiveConfig, observational_outcome_mode
from bot.market_runtime import handle_order_book_deltas
from test_pricing_runtime_volatility import _DepthHost


def owner_and_candidate():
    events=[]
    def execution_forbidden(*args, **kwargs):
        raise AssertionError('retired execution path touched')
    strategy=SimpleNamespace(
        _db_strategy_event=lambda name,payload: events.append((name,payload)),
        _db_order_event=execution_forbidden, submit_order=execution_forbidden,
        order_factory=SimpleNamespace(limit=execution_forbidden),
        trade_db=SimpleNamespace(save_fast_follow_night_risk=execution_forbidden),
    )
    owner=OutcomeFastFollowLive(strategy,FastFollowLiveConfig())
    candidate=SimpleNamespace(created_epoch_ns=time.time_ns(),slug='market',
                              decision=SimpleNamespace(state='follower_confirmed',direction=1))
    return owner,candidate,events


def test_signal_cannot_reserve_own_block_or_submit_even_with_legacy_live_config(monkeypatch):
    monkeypatch.setenv('OUTCOME_LEAD_LAG_MODE','live_entry_only')
    monkeypatch.setenv('FAST_FOLLOW_EXECUTION_ENABLED','1')
    owner,candidate,events=owner_and_candidate()
    owner.record_candidate(candidate)
    assert owner.execution_enabled is False
    assert owner._pending is None and not owner._attempted_slugs
    assert not owner._pending_order_ids and not owner._night_pending_entry_ids
    assert not owner.blocks_normal_buy('market')
    assert owner.on_quote(instrument_id='UP',best_bid=Decimal('.7'),best_ask=Decimal('.71'),ask_size=Decimal('50'),now_ts=time.time()) is False
    assert events[0][0]=='FAST_FOLLOW_ENTRY_BLOCKED'
    assert events[0][1]['reason']=='execution_disabled'


def test_retired_owner_does_not_consume_existing_risk_or_fill_callbacks():
    owner,candidate,_=owner_and_candidate()
    owner._attempted_slugs.add('market')
    owner._pending=candidate
    assert owner.blocks_normal_buy('market') is False
    assert owner._ensure_night_loaded('2026-10-07') is False
    assert owner._persist_night('2026-10-07') is False
    owner.on_fill(client_order_id='old',side='buy',instrument_id='UP')
    owner.on_order_terminal('old')
    assert not owner._night_filled_entries and not owner._night_pending_entry_ids
    assert owner.order_metadata('old') is None
    owner.on_quote(instrument_id='UP',best_bid=Decimal('.7'),best_ask=Decimal('.71'),ask_size=None,now_ts=time.time())
    assert owner._pending is None


def test_economics_reason_collision_preserves_block_reason_and_detail_once():
    owner,candidate,events=owner_and_candidate()
    for _ in range(2):
        owner._record_blocked(candidate,'fast_follow_economics_rejected',**{'reason':'insufficient_net_ev','economics_reason':'net_ev'})
    assert len(events)==1
    assert events[0][1]['reason']=='fast_follow_economics_rejected'
    assert events[0][1]['reason_detail']=='insufficient_net_ev'
    assert owner._pending is None


@pytest.mark.parametrize('requested,effective',[('live_entry_only','shadow'),('shadow','shadow'),('off','off')])
def test_legacy_live_request_only_enables_observations(requested,effective):
    assert observational_outcome_mode(requested)==effective


def test_production_wiring_has_no_fast_follow_owner_dispatch_or_maker_gate():
    from bot import settings,market_runtime
    from run_bot import IntegratedBTCStrategy
    setup=ast.parse(inspect.getsource(settings))
    assert not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='OutcomeFastFollowLive' for n in ast.walk(setup))
    assert 'blocks_normal_buy' not in inspect.getsource(IntegratedBTCStrategy._evaluate_quote_targets)
    assert 'fast_follow.on_quote' not in inspect.getsource(market_runtime.handle_quote_tick)
    # Phase A keeps both Outcome observation and shared persistence wired.
    calls={n.func.id for n in ast.walk(setup) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}
    assert {'HyperliquidOutcomeObserver','OutcomeLeadLagRuntime','OutcomeLeadLagShadow','LeadLagDB'} <= calls


@pytest.mark.parametrize('age,accepted',[(1.99,True),(2.01,False)])
def test_neutral_l2_stamp_preserves_exact_maker_delivery_threshold(monkeypatch,age,accepted):
    monkeypatch.setattr(time,'time',lambda:1000.0)
    host=_DepthHost()
    # Market-data callback owns the only timestamp map, with no FF object.
    handle_order_book_deltas(host,SimpleNamespace(instrument_id='UP'))
    assert host.l2_update_ts_by_inst=={'UP':1000.0}
    assert not hasattr(host,'fast_follow_l2_update_ts_by_inst')
    host.l2_update_ts_by_inst['UP']-=age
    bids,asks=asyncio.run(host._get_orderbook_levels_for_instrument('UP'))
    assert (bids is not None and asks is not None) is accepted
    assert host.quote_max_delivery_delay_sec==2.0
