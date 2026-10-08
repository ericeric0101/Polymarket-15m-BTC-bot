"""Hermetic reproductions of the independent audit; no venue/production DB I/O."""
import asyncio
import json
import sqlite3
from collections import deque
from decimal import Decimal
from types import SimpleNamespace

from bot.enums import ActiveSide
from bot.exit_engine import ExitPolicyEngine
from bot.prediction_research_snapshot import PredictionResearchSnapshotter
from bot.research.store import ResearchStore
from run_bot import IntegratedBTCStrategy
from scripts.prediction_snapshot_analysis import read_snapshots
from test_absolute_max_loss_breaker import _make_config, _snapshot, _position, _signal, _BreakerExitHost
from test_live_path_regressions import DummyUrgentExitStrategy
from test_prediction_research_snapshot import _clock_capture_strategy


def test_urgent_exit_never_reaches_authenticated_submit_in_test_mode():
    host = DummyUrgentExitStrategy()
    host.test_mode = True
    host.stop_loss_enabled = True
    host.active_maker_orders = {}
    host.cache.instrument = lambda _: SimpleNamespace(size_precision=6, price_precision=2)
    asyncio.run(host._maybe_maker_urgent_exit(100))
    assert host.submit_calls == []
    assert host.cancel_calls == []


def test_absolute_loss_breaker_remains_active_with_adaptive_stop_disabled():
    engine = ExitPolicyEngine(_make_config(stop_loss_enabled=False))
    decision = engine.evaluate(_snapshot('0.20'), _position('0.69'),
                               _signal(score=Decimal('-.30'), matches=False, active_side='DOWN'))
    assert decision.reason == 'absolute_max_loss_breaker'


def test_stop_disabled_runtime_still_dispatches_absolute_breaker():
    host = _BreakerExitHost()
    host.stop_loss_enabled = False
    host.active_side = ActiveSide.DOWN
    host.side_decision_score = Decimal('-.30')
    host.exit_policy_engine = ExitPolicyEngine(_make_config(stop_loss_enabled=False))
    asyncio.run(host._maybe_taker_exit_positions(10_000, is_simulation=False))
    assert len(host.submissions) == 1
    assert host.submissions[0]['decision_payload']['decision_reason'] == 'absolute_max_loss_breaker'


def invalidation_host():
    host = SimpleNamespace(_side_invalidation_hits_by_slug={}, _side_invalidation_confirmed_by_slug={},
        active_side=ActiveSide.UP, maker_side_invalidation_confirm_cycles=3,
        _spot_still_supports_side=lambda side, **kw: kw['spot'] is not None and kw['spot'] >= kw['strike'],
        _side_for_instrument_id=lambda _: ActiveSide.UP, _bump_thesis_epoch=lambda _: None,
        _db_strategy_event=lambda *args: None)
    return host


def invalidation(host, ts, **overrides):
    args=dict(now_ts=ts, slug='m', instrument_id='up', side=ActiveSide.UP,
              spot=Decimal('99'), strike=Decimal('100'), fair=Decimal('.3'),
              inventory_qty=Decimal('0'), time_left_sec=300)
    args.update(overrides)
    return IntegratedBTCStrategy._update_side_invalidation_state(host, **args)


def test_one_logical_cycle_with_sibling_evaluations_adds_one_invalidation_confirmation():
    host = invalidation_host()
    invalidation(host, 100)
    invalidation(host, 100, instrument_id='down')
    assert host._side_invalidation_hits_by_slug['m'] == 1
    assert not host._side_invalidation_confirmed_by_slug['m']


def prediction_db(tmp_path):
    path = tmp_path / 'research.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE lead_lag_decisions (run_id TEXT,slug TEXT,decision_epoch_ns INTEGER,payload_json TEXT)')
        for index, version in enumerate((1, 2, None)):
            payload={'event_type':'PREDICTION_RESEARCH_SNAPSHOT','snapshot_ts':100+index,'market_slug':'m','joint_fresh':True}
            if version is not None: payload['freshness_clock_semantics_version']=version
            db.execute('INSERT INTO lead_lag_decisions VALUES(?,?,?,?)', ('r','m',(100+index)*10**9,json.dumps(payload)))
    return path


def test_native_prediction_reader_excludes_legacy_and_missing_versions(tmp_path):
    rows = ResearchStore(prediction_db(tmp_path)).get_prediction_snapshots()
    assert len(rows) == 1
    assert rows[0]['freshness_clock_semantics_version'] == 2


def test_legacy_prediction_cli_reader_excludes_non_v2(tmp_path):
    rows, _ = read_snapshots(prediction_db(tmp_path))
    assert len(rows) == 1
    assert rows[0]['freshness_clock_semantics_version'] == 2


def test_research_captures_open_market_without_waiting_for_trading_handoff():
    # Trading deliberately remains SETTLING on prior market. New pair/strike
    # and locally received sources are usable; research must not promote trading.
    old, new = 'btc-updown-15m-900', 'btc-updown-15m-1800'
    host = _clock_capture_strategy([(1795,100,1795),(1801,100,1801)])
    host.current_market_slug=old;host.current_market_end_timestamp=1800
    host.market_strike_cache_by_slug={new:100}
    host.research_market_instruments_by_slug={new:('up','down')}
    host._polymarket_chainlink_twap_observation_ts=1801
    host._polymarket_chainlink_twap_price_ts=1801
    host._binance_ws_price_source_ts=1801;host._binance_ws_price_ts=1801
    host.last_quote_source_ts_by_inst={'up':1801,'down':1801}
    host.last_quote_received_ts_by_inst={'up':1801,'down':1801}
    row = PredictionResearchSnapshotter(db=SimpleNamespace(enqueue_decision=lambda **_: True),run_id='r').capture(host,now_ts=1801.2)
    assert row is not None
    assert row['market_slug'] == new
    assert row['snapshot_ts'] - 1800 <= 2
    assert row['freshness_clock_semantics_version'] == 2
    assert host.current_market_slug == old

import pytest
from bot.execution_safety import ExecutionSafetyMixin
from bot.research.freshness import native_v2_exclusion_reason
from bot.research.evidence import MarketEvidence


class FakeVenue:
    def __init__(self):
        self.calls = []
    def submit_order(self, order, *args, **kwargs):
        self.calls.append((order, args, kwargs))
        return 'accepted'


class GuardedHost(ExecutionSafetyMixin, FakeVenue):
    pass


@pytest.mark.parametrize('path', ['maker_buy', 'maker_sell', 'taker_ioc', 'taker_fok',
                                  'urgent_gtc', 'passive_recovery', 'tp_recycle', 'replace_buy'])
@pytest.mark.parametrize('mode', [True, None, 'false'])
def test_public_submit_choke_point_blocks_every_order_shape_without_explicit_live_mode(path, mode):
    host = GuardedHost()
    host.test_mode = mode
    host._is_dry_run_mode = lambda: False
    assert host.submit_order({'path': path}) is False
    assert host.calls == []


def test_guard_is_production_mro_authority_and_preserves_live_arguments():
    assert IntegratedBTCStrategy.submit_order is ExecutionSafetyMixin.submit_order
    host = GuardedHost(); host.test_mode = False
    host._is_dry_run_mode = lambda: False
    assert host.submit_order('order', 'position', client_id='venue') == 'accepted'
    assert host.calls == [('order', ('position',), {'client_id': 'venue'})]
    host._is_dry_run_mode = lambda: True
    assert host.submit_order('contradictory-mode') is False
    def broken(): raise RuntimeError('mode unavailable')
    host._is_dry_run_mode = broken
    assert host.submit_order('unknown-mode') is False
    assert len(host.calls) == 1


def test_three_confirmations_require_three_distinct_cycles_and_reset_on_missing_evidence():
    host = invalidation_host()
    for ts in (100, 101):
        invalidation(host, ts); invalidation(host, ts, instrument_id='down')
        assert not host._side_invalidation_confirmed_by_slug['m']
    invalidation(host, 102)
    assert host._side_invalidation_hits_by_slug['m'] == 3
    assert host._side_invalidation_confirmed_by_slug['m']
    invalidation(host, 103, spot=None)
    assert host._side_invalidation_hits_by_slug['m'] == 0
    assert not host._side_invalidation_confirmed_by_slug['m']
    invalidation(host, 104, fair=Decimal('.7'), spot=Decimal('101'))
    assert host._side_invalidation_hits_by_slug['m'] == 0


def test_market_rollover_and_side_change_start_new_confirmation_series():
    host = invalidation_host()
    invalidation(host, 100); invalidation(host, 101)
    invalidation(host, 102, slug='next')
    assert host._side_invalidation_hits_by_slug['next'] == 1
    host.active_side = ActiveSide.DOWN
    invalidation(host, 103, slug='next', side=ActiveSide.DOWN)
    assert host._side_invalidation_hits_by_slug['next'] == 1
    # A return to a previously visited market cannot resurrect its old count.
    invalidation(host, 104, side=ActiveSide.DOWN)
    assert host._side_invalidation_hits_by_slug['m'] == 1


def test_native_readers_count_exclusions_and_historical_opt_in_preserves_original_version(tmp_path):
    path = prediction_db(tmp_path)
    store = ResearchStore(path)
    store.get_prediction_snapshots()
    expected = {'LEGACY_FRESHNESS_CLOCK_V1': 1, 'MISSING_FRESHNESS_CLOCK_VERSION': 1}
    assert store.prediction_exclusions == expected
    exclusions = {}; read_snapshots(path, exclusions=exclusions)
    assert exclusions == expected
    historical = store.get_prediction_snapshots(provenance='HISTORICAL_RECOMPUTATION_INPUT')
    assert len(historical) == 3
    assert [r.get('freshness_clock_semantics_version') for r in historical] == [1, 2, None]
    assert all(native_v2_exclusion_reason(r) == 'HISTORICAL_RECOMPUTATION_NOT_NATIVE' for r in historical)


@pytest.mark.parametrize('version', [1, None, '2', True, 3])
def test_typed_prediction_projection_cannot_bypass_native_provenance_gate(version):
    with pytest.raises(ValueError, match='excluded'):
        MarketEvidence.from_snapshot({'freshness_clock_semantics_version': version},
                                     run_id='r', market_start_taipei=None, session_regime='WEEKDAY')


def opening_host():
    host = _clock_capture_strategy([(1795,100,1795),(1801,100,1801)])
    host.current_market_slug = 'btc-updown-15m-900'
    host.current_market_end_timestamp = 1800
    host.market_strike_cache_by_slug = {'btc-updown-15m-1800':100}
    host.research_market_instruments_by_slug = {'btc-updown-15m-1800': {'UP':'up','DOWN':'down'}}
    from bot.spot_pricer import SpotPricerMixin
    host._research_market_quote_instruments = lambda **kw: SpotPricerMixin._research_market_quote_instruments(host, **kw)
    host._polymarket_chainlink_twap_observation_ts = host._polymarket_chainlink_twap_price_ts = 1801
    host._binance_ws_price_source_ts = host._binance_ws_price_ts = 1801
    host.last_quote_source_ts_by_inst = {}; host.last_quote_received_ts_by_inst = {}
    host.latest_quote_by_inst = {}
    host.quote_prewarm_latest_by_inst = {
        'up':dict(bid=Decimal('.6'),ask=Decimal('.62'),received_ts=1801,source_ts=1801),
        'down':dict(bid=Decimal('.38'),ask=Decimal('.4'),received_ts=1801,source_ts=1801)}
    return host


def capture_open(host):
    return PredictionResearchSnapshotter(db=SimpleNamespace(enqueue_decision=lambda **_: True),run_id='r').capture(host,now_ts=1801.2)


def test_opening_uses_real_prewarm_receipts_without_old_side_or_quote_state():
    host = opening_host()
    row = capture_open(host)
    assert row['market_slug'] == 'btc-updown-15m-1800'
    assert row['market_mid_fresh'] is True
    assert row['active_side'] == 'NONE'
    assert row['side_score'] is None
    assert host.latest_quote_by_inst == {}
    assert host.current_market_slug == 'btc-updown-15m-900'


@pytest.mark.parametrize('quote_ts', [1700, 1810])
def test_opening_stale_or_future_prewarm_never_becomes_fresh(quote_ts):
    host = opening_host()
    for quote in host.quote_prewarm_latest_by_inst.values():
        quote['received_ts'] = quote_ts; quote['source_ts'] = quote_ts
    row = capture_open(host)
    assert not row['market_mid_fresh']
    assert not row['joint_fresh']


def test_opening_without_admitted_pair_does_not_fabricate_new_market_row():
    host = opening_host(); host.research_market_instruments_by_slug = {}
    assert capture_open(host) is None


def test_required_twap_evidence_is_actually_persisted_above_cap(tmp_path):
    from bot.twap_forward_shadow import TwapForwardShadow, REQUIRED_EVENT_TYPES
    from monitoring.lead_lag_db import LeadLagDB
    path = tmp_path / 'research.db'
    db = LeadLagDB(db_path=str(path))
    model = TwapForwardShadow(db=db, max_db_mb=0, min_free_disk_gb=0)
    try:
        model._storage_health(100)
        for i, event in enumerate(sorted(REQUIRED_EVENT_TYPES)):
            model._persist('m', 101+i, event, {'marker': event})
        model._persist('m', 120, 'OPTIONAL_DIAGNOSTIC', {})
    finally:
        db.stop()
    with sqlite3.connect(path) as conn:
        events = {json.loads(row[0]).get('event_type') for row in conn.execute('SELECT payload_json FROM lead_lag_decisions')}
    assert REQUIRED_EVENT_TYPES <= events
    assert 'OPTIONAL_DIAGNOSTIC' not in events


def test_crash_persisted_intent_and_unfilled_gtc_is_not_adopted_by_inventory_rehydration(tmp_path, monkeypatch):
    """Strategy-layer reproduction; framework startup reconciliation is NOT modeled."""
    from bot.recovery import StrategyRecoveryMixin
    from bot.order_submission import submit_maker_quote
    from test_live_path_regressions import DummyTrendSubmitStrategy
    path = tmp_path / 'intents.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE intents(client_order_id TEXT,event_type TEXT)')
    venue = []
    def host():
        h = DummyTrendSubmitStrategy()
        h.live_inventory_cost = {}; h.current_market_instruments = ['inst-up']; h.instrument_id = 'inst-up'
        h._get_sellable_qty_for_current_instrument = lambda **_: Decimal('0')
        h.test_mode = False
        def persist(**event):
            with sqlite3.connect(path) as db:
                db.execute('INSERT INTO intents VALUES(?,?)', (event.get('client_order_id'), event['event_type']))
            return True
        h._db_order_event = persist
        h.submit_order = venue.append
        return h
    econ = SimpleNamespace(expected_net_usdc=Decimal('.02'), expected_rebate_usdc=Decimal('0'),
        expected_spread_capture_usdc=Decimal('0'), fee_equivalent_usdc=Decimal('0'))
    before = host()
    monkeypatch.setattr('bot.order_submission.time.time', lambda: 1000.0)
    submit_maker_quote(before, instrument_id='inst-up', side='buy', limit_price=Decimal('.6'), econ=econ)
    assert len(venue) == 1
    # Crash loses local active_maker_orders; the venue still owns unfilled GTC.
    after = host()
    StrategyRecoveryMixin._rehydrate_inventory_state_on_startup(after)
    assert after.active_maker_orders == {}
    monkeypatch.setattr('bot.order_submission.time.time', lambda: 1001.0)
    submit_maker_quote(after, instrument_id='inst-up', side='buy', limit_price=Decimal('.6'), econ=econ)
    assert len(venue) == 2
    assert venue[0].client_order_id != venue[1].client_order_id
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM intents WHERE event_type='ORDER_MAKER_INTENT'").fetchone()[0] == 2


def test_opening_prewarm_wakes_existing_worker_only_for_research(monkeypatch):
    import threading
    from bot import market_runtime
    host = opening_host()
    host._maker_worker_lock = threading.Lock(); host._maker_worker_running = False; host._stopping = False
    captured = []
    host.prediction_research_snapshotter = SimpleNamespace(interval_sec=1.0,
        capture=lambda *args, **kw: captured.append(kw))
    class InlineThread:
        def __init__(self, *, target, **_): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(market_runtime.threading, 'Thread', InlineThread)
    monkeypatch.setattr(market_runtime.time, 'time', lambda:1801.2)
    monkeypatch.setattr(market_runtime, 'maker_quote_sync', lambda *_: pytest.fail('prewarm must not execute maker'))
    market_runtime.start_maker_worker(host, Decimal('.6'), Decimal('.62'), research_only=True)
    market_runtime.start_maker_worker(host, Decimal('.6'), Decimal('.62'), research_only=True)
    assert len(captured) == 1
    assert captured[0]['now_ts'] == 1801.2
    assert not host._maker_worker_running
    host._stopping = True
    market_runtime.start_maker_worker(host, Decimal('.6'), Decimal('.62'), research_only=True)
    assert len(captured) == 1


def test_generic_research_event_reader_cannot_bypass_prediction_gate(tmp_path):
    store = ResearchStore(prediction_db(tmp_path))
    assert len(store.get_research_events(event_type='PREDICTION_RESEARCH_SNAPSHOT')) == 1
    assert store.prediction_exclusions['LEGACY_FRESHNESS_CLOCK_V1'] == 1


def test_configured_catastrophic_breaker_is_also_independent_with_confirmations_preserved():
    engine = ExitPolicyEngine(_make_config(stop_loss_enabled=False, absolute_max_loss_enabled=False))
    args = (_snapshot('.55'), _position('.69'), _signal(score=Decimal('-.60'), matches=False, active_side='DOWN'))
    pending = engine.evaluate(*args)
    assert pending.reason == 'catastrophic_stop_loss_confirming'
    assert pending.confirm_hits == 1
    confirmed = engine.evaluate(args[0], _position('.69', confirm_hits=1), args[2])
    assert confirmed.reason == 'catastrophic_stop_loss_confirmed'


def test_runtime_dispatches_catastrophic_when_absolute_and_adaptive_stop_are_disabled():
    host = _BreakerExitHost()
    host.stop_loss_enabled = False
    host.absolute_max_loss_enabled = False
    host.catastrophic_stop_loss_enabled = True
    host.active_side = ActiveSide.DOWN
    host.side_decision_score = Decimal('-.60')
    host.exit_policy_engine = ExitPolicyEngine(_make_config(stop_loss_enabled=False, absolute_max_loss_enabled=False))
    host.taker_exit_stop_loss_hits_by_inst = {'up': 1}
    host.active_maker_orders = {}
    host._get_quote_for_instrument = lambda _: (Decimal('.20'), Decimal('.201'))
    asyncio.run(host._maybe_taker_exit_positions(10_000, is_simulation=False))
    assert len(host.submissions) == 1
    assert host.submissions[0]['decision_payload']['decision_reason'] == 'catastrophic_stop_loss_confirmed'


@pytest.mark.parametrize('side', ['buy','sell'])
def test_real_maker_submit_path_uses_choke_even_if_local_simulation_check_is_wrong(side):
    from bot.order_submission import submit_maker_quote
    from test_live_path_regressions import DummyTrendSubmitStrategy
    class GuardedMaker(ExecutionSafetyMixin, DummyTrendSubmitStrategy):
        pass
    host = GuardedMaker(); host.test_mode = True
    if side == 'sell': host.inventory_delta_shares = Decimal('6')
    econ = SimpleNamespace(expected_net_usdc=Decimal('.02'), expected_rebate_usdc=Decimal('0'),
        expected_spread_capture_usdc=Decimal('0'), fee_equivalent_usdc=Decimal('0'))
    submit_maker_quote(host, instrument_id='inst-up', side=side, limit_price=Decimal('.6'), econ=econ)
    assert host.submitted_orders == []


def test_all_repo_order_submission_sites_resolve_public_strategy_choke():
    import ast
    from pathlib import Path
    paths = [*Path('bot').rglob('*.py'), *Path('execution').rglob('*.py'), Path('run_bot.py')]
    sites = []
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'submit_order':
                if path.name == 'execution_safety.py': continue  # the sole framework forwarding boundary
                assert isinstance(node.func.value, ast.Name)
                assert node.func.value.id in {'self','strategy'}
                sites.append(str(path))
    assert sorted(sites) == ['bot/order_submission.py', 'bot/taker_exit.py', 'bot/taker_exit.py', 'bot/taker_exit.py']
    assert IntegratedBTCStrategy.submit_order is ExecutionSafetyMixin.submit_order


def test_opening_waits_for_new_strike_and_complete_pair_instead_of_borrowing_prior_state():
    host = opening_host()
    host.market_strike_cache_by_slug = {'btc-updown-15m-900': 100}
    assert capture_open(host) is None
    host.market_strike_cache_by_slug = {'btc-updown-15m-1800':100}
    host.research_market_instruments_by_slug['btc-updown-15m-1800'] = {'UP':'up'}
    assert capture_open(host) is None
