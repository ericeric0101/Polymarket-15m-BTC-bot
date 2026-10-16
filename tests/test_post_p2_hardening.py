"""Post-P2 semantic regressions; synthetic cache/evidence and temp databases only."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.db_runtime import StrategyDBRuntimeMixin
from bot.execution_events import audit_reconciliation
from bot.prediction_research_snapshot import PredictionResearchSnapshotter
from bot.research.evidence import annotate_decision, STRATEGY_TRACE_EVENTS
from bot.research.health import component_health, strategy_health
from bot.research.storage import StorageSummary, archival_readiness
from bot.research.store import ResearchStore
from scripts.research_analysis import engineering_integrity


def initial_cycle():
    # Both lifecycle_runtime producers persist these fields; no source/tx yet.
    return {'event_type': 'MARKET_CYCLE_PNL', 'payload': {'slug': 'm', 'active_side': 'UP',
        'cycle_fill_realized_usdc': 2., 'cycle_settlement_pnl_usdc': -5.,
        'cycle_combined_pnl_usdc': -3., 'recent_window_size': 1}}


def reconciliation_payload():
    # Actual TradeJournalDB.reconcile_redeem_cycle return shape.
    return {'buy_cost_usdc': 5., 'sell_proceeds_usdc': 2., 'redeem_value_usdc': 4.,
        'cycle_fill_realized_usdc': 2., 'cycle_settlement_pnl_usdc': -1.,
        'cycle_combined_pnl_usdc': 1., 'cycle_pnl_reconciled_source': 'onchain_redeem',
        'cycle_pnl_reconciled_at': '2026-10-04T00:00:00+00:00',
        'redeem_tx_hash': 'synthetic-tx', 'redeem_condition_id': 'synthetic-condition'}


@pytest.mark.parametrize('wrote_cycle', [False, True])
def test_actual_redeem_runtime_emission_is_update_not_duplicate(wrote_cycle):
    events = [initial_cycle()]
    host = SimpleNamespace(trade_db=SimpleNamespace(reconcile_redeem_cycle=lambda *a, **k:
        {**reconciliation_payload(), 'wrote_cycle_pnl': wrote_cycle}),
        _db_strategy_event=lambda event_type, payload: events.append({'event_type': event_type, 'payload': payload}))
    StrategyDBRuntimeMixin._reconcile_redeem_cycle_pnl(host, {'slug': 'm', 'redeem_cash_usdc': 4., 'tx_hash': 'synthetic-tx'})
    report = audit_reconciliation(events)
    assert report['status'] == 'CONSISTENT'
    assert [e['classification'] for e in report['finalization_events']] == [
        'INITIAL_FINALIZATION', 'LEGITIMATE_RECONCILIATION_UPDATE']
    events.append(events[-1])
    assert audit_reconciliation(events)['finalization_events'][-1]['classification'] == 'TRUE_DUPLICATE'


def test_true_duplicate_and_insufficient_correction_identity():
    assert audit_reconciliation([initial_cycle(), initial_cycle()])['finalization_events'][-1]['classification'] == 'TRUE_DUPLICATE'
    correction = {'event_type': 'MARKET_PNL_RECONCILED', 'payload': {'slug': 'm', **reconciliation_payload()}}
    correction['payload']['redeem_tx_hash'] = ''
    report = audit_reconciliation([initial_cycle(), correction])
    assert report['finalization_events'][-1]['classification'] == 'IDENTITY_INSUFFICIENT'
    assert all(i['reason_code'] != 'REPEATED_FINALIZATION' for i in report['issues'])


def test_real_journal_redeem_updates_cycle_in_place(tmp_path):
    from monitoring.trade_journal_db import TradeJournalDB
    db = TradeJournalDB(str(tmp_path/'journal.db'), backup_interval_sec=3600)
    try:
        db.log_strategy_event(run_id='r', event_type='MARKET_CYCLE_PNL', payload=initial_cycle()['payload'])
        for side, price, qty in [('BUY', .5, 10), ('SELL', .5, 4)]:
            db.log_order_event(run_id='r', event_type='ORDER_FILLED', side=side, price=price, qty=qty,
                               payload={'slug': 'm', 'effective_fee_usdc': 0})
        events = []
        host = SimpleNamespace(trade_db=db, _db_strategy_event=lambda tag, payload:
            (events.append({'event_type': tag, 'payload': payload}),
             db.log_strategy_event(run_id='r', event_type=tag, payload=payload)))
        StrategyDBRuntimeMixin._reconcile_redeem_cycle_pnl(host, {'slug': 'm', 'redeem_cash_usdc': 4, 'tx_hash': 'synthetic-tx'})
        rows = ResearchStore(tmp_path/'unused.db').journal_events(Path(db.db_path), table='strategy_events')
        assert sum(r['event_type'] == 'MARKET_CYCLE_PNL' for r in rows) == 1
        assert events[0]['event_type'] == 'MARKET_PNL_RECONCILED'
        report = audit_reconciliation(rows)
        assert report['status'] == 'CONSISTENT'
        assert all(e['classification'] == 'LEGITIMATE_RECONCILIATION_UPDATE' for e in report['finalization_events'])
    finally:
        db.stop()


def test_realized_fill_pnl_never_becomes_position_mtm():
    host = SimpleNamespace(run_id='r', current_market_slug='m', instrument_id=None)
    trace = annotate_decision(host, {'realized_net_usdc': 3.}, kind='EXECUTION_DECISION', now_ts=100)['decision_trace']
    assert trace['position_pnl'] is None
    assert trace['realized_net_usdc'] == 3.
    assert trace['trace_schema_version'] == 2


def test_current_slug_silence_not_masked_by_recent_other_market():
    capture = PredictionResearchSnapshotter(db=SimpleNamespace(), run_id='r')
    capture._last_snapshot_ts.update(A=100, B=499)
    capture._recent_quality.extend([(100, True, True, 1, 'A'), (499, True, True, 1, 'B')])
    assert capture.recent_health(500, slug='A')['recent_largest_gap_sec'] == 400
    assert capture.recent_health(500)['recent_largest_gap_sec'] == 1
    host = SimpleNamespace(current_market_slug='A', prediction_research_snapshotter=capture)
    view = strategy_health(host, 500)
    assert 'prediction:RECENT_SNAPSHOT_GAP' in view['domains']['Research']['reason_codes']
    assert view['domains']['Research']['components']['prediction']['source_metrics']['slug'] == 'A'


def test_attempted_vs_accepted_freshness_and_drops():
    capture = PredictionResearchSnapshotter(db=None, run_id='r')
    capture._recent_quality.extend([(100, True, True, 1, 'A'), (101, False, False, 1, 'A')])
    metrics = capture.recent_health(102, slug='A')
    assert metrics['capture_joint_fresh_pct'] == metrics['joint_fresh_pct'] == 50
    assert metrics['persisted_joint_fresh_pct'] == 100
    assert metrics['drop_count'] == 1 and metrics['drop_pct'] == 50
    reasons = component_health(metrics, domain='Research')['reason_codes']
    assert 'DATA_DROPPED' in reasons and 'RECENT_STALE_EVIDENCE' not in reasons


def actual_cache_host():
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.model.book import OrderBook
    from nautilus_trader.model.data import BookOrder
    from nautilus_trader.model.enums import BookType, OrderSide
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.objects import Price, Quantity
    from run_bot import IntegratedBTCStrategy
    inst = InstrumentId.from_str('up.POLYMARKET')
    cache = Cache()
    book = OrderBook(inst, BookType.L2_MBP)
    book.add(BookOrder(OrderSide.BUY, Price.from_str('0.60'), Quantity.from_str('2'), 1), ts_event=1)
    book.add(BookOrder(OrderSide.SELL, Price.from_str('0.70'), Quantity.from_str('3'), 2), ts_event=2)
    cache.add_order_book(book)
    rows, journal = [], []
    host = SimpleNamespace(run_id='r', current_market_slug='m', instrument_id=inst, cache=cache,
        _normalize_instrument_id=IntegratedBTCStrategy._normalize_instrument_id,
        _normalize_side_text=lambda side: side.lower(), current_token_id='token', last_observed_fee_rate_bps=0,
        twap_research_db=SimpleNamespace(enqueue_decision=lambda **kw: rows.append(kw) or True),
        trade_db=SimpleNamespace(log_order_event=lambda **kw: journal.append(kw) or True,
                                 log_strategy_event=lambda **kw: journal.append(kw) or True))
    return host, rows, journal


@pytest.mark.parametrize('use_string', [True, False])
def test_nautilus_cache_runtime_resolver_and_persisted_identity(use_string):
    host, rows, journal = actual_cache_host()
    inst = str(host.instrument_id) if use_string else host.instrument_id
    assert StrategyDBRuntimeMixin._db_order_event(host, event_type='ORDER_BUY_SUBMIT', instrument_id=inst, status='SUBMITTED')
    assert rows[0]['payload']['l2']['bids'] == [[.6, 2.]]
    assert rows[0]['payload']['instrument_id'] == str(host.instrument_id)
    assert journal[0]['payload']['decision_trace']['l2']['status'] == 'L2_ENQUEUED'
    assert StrategyDBRuntimeMixin._db_strategy_event(host, 'ENTRY_DECISION_TRACE', {'should_quote': True})
    assert len(rows) == 2


def test_invalid_cache_id_is_unavailable_not_current_book_substitution():
    host, rows, _ = actual_cache_host()
    trace = annotate_decision(host, {}, kind='EXECUTION_DECISION', instrument_id='invalid', now_ts=100)['decision_trace']
    assert trace['l2']['status'] == 'L2_NOT_AVAILABLE' and rows == []


@pytest.mark.parametrize('availability,state', [('NOT_CONFIGURED', 'HEALTHY'), ('DISABLED_BY_POLICY', 'HEALTHY'), ('FAILED', 'CRITICAL')])
def test_optional_observer_policy_is_not_required_writer_failure(availability, state):
    result = component_health({'enabled': False, 'availability': availability}, domain='Research')
    assert result['state'] == state
    host = SimpleNamespace(btc_1s_history_collector=None, _btc_history_observer_status=availability)
    component = strategy_health(host, 100)['domains']['Research']['components']['btc']
    assert component['state'] == state
    assert component_health({'ready': False, 'availability': 'DISABLED_BY_POLICY'}, domain='Execution')['state'] == 'CRITICAL'


def test_failed_writer_without_policy_still_critical():
    assert component_health({'enabled': False, 'failure_reason': 'disk_error'}, domain='Research')['state'] == 'CRITICAL'


def test_storage_distinct_filesystems_and_same_device_dedup(tmp_path, monkeypatch):
    import bot.research.storage as storage
    research_dir, journal_dir, btc_dir = (tmp_path/n for n in ('research', 'journal', 'btc'))
    for d in (research_dir, journal_dir, btc_dir): d.mkdir()
    research, journal = research_dir/'r.db', journal_dir/'j.db'
    research.write_bytes(b'r'); journal.write_bytes(b'j')
    original_stat = Path.stat
    devices = {research_dir: 1, journal_dir: 2, btc_dir: 3}
    def stat(path, *args, **kwargs):
        if path in devices: return SimpleNamespace(st_dev=devices[path])
        return original_stat(path, *args, **kwargs)
    calls = []
    monkeypatch.setattr(Path, 'stat', stat)
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda p: calls.append(p) or SimpleNamespace(free=devices[p]*100))
    result = StorageSummary().measure(research=research, journal=journal, btc_dir=None)
    assert result['research_free_disk'] == 100 and result['journal_free_disk'] == 200
    # No files need scanning to test the disk-only branch; retain real directory mode.
    monkeypatch.setattr(Path, 'is_dir', lambda p: False if p == btc_dir else True)
    result = StorageSummary().measure(research=research, journal=journal, btc_dir=btc_dir)
    assert result['btc_free_disk'] == 300
    calls.clear(); devices[journal_dir] = devices[btc_dir] = 1
    result = StorageSummary().measure(research=research, journal=journal, btc_dir=btc_dir)
    assert len(calls) == 1 and result['research_free_disk'] == result['btc_free_disk'] == result['journal_free_disk']


@pytest.mark.parametrize('field,code', [('collection_closed', 'COLLECTION_ACTIVE'), ('writers_stopped', 'WRITERS_RUNNING'),
    ('integrity_verified', 'INTEGRITY_NOT_VERIFIED'), ('backup_verified', 'BACKUP_NOT_VERIFIED')])
def test_archive_exact_missing_precondition(field, code):
    args = dict(collection_closed=True, writers_stopped=True, integrity_verified=True, backup_verified=True)
    args[field] = False
    result = archival_readiness(**args)
    assert result['reason_codes'] == [code] and result['automatic_action'] is None


def test_integrity_l2_persistence_reference_joins_and_complete_event_set(tmp_path):
    research, journal = tmp_path/'r.db', tmp_path/'j.db'
    with sqlite3.connect(research) as conn:
        conn.execute('CREATE TABLE lead_lag_decisions(run_id TEXT,slug TEXT,decision_epoch_ns INTEGER,payload_json TEXT)')
        p = {'event_type': 'DECISION_POINT_L2', 'market_slug': 'm', 'l2_evidence_id': 'joined', 'l2': {'status': 'L2_AVAILABLE'}}
        conn.execute('INSERT INTO lead_lag_decisions VALUES(?,?,?,?)', ('r','m',100,json.dumps(p)))
    with sqlite3.connect(journal) as conn:
        for table in ('order_events', 'strategy_events'):
            conn.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY,ts TEXT,run_id TEXT,event_type TEXT,payload_json TEXT)')
        events = sorted(STRATEGY_TRACE_EVENTS)
        for i, tag in enumerate(events):
            conn.execute('INSERT INTO strategy_events VALUES(?,?,?,?,?)', (i, '1970-01-01T00:01:40+00:00', 'r', tag, '{}'))
        for i, (status, ref) in enumerate([('L2_ENQUEUED','joined'), ('L2_ENQUEUED','missing'), ('L2_NOT_PERSISTED','failed')]):
            p = {'decision_trace': {'run_id': 'r', 'market_slug': 'm', 'l2': {'status': status, 'l2_evidence_id': ref}}}
            conn.execute('INSERT INTO order_events VALUES(?,?,?,?,?)', (i,'1970-01-01T00:01:40+00:00','r','ORDER_BUY_SUBMIT',json.dumps(p)))
    report = engineering_integrity(ResearchStore(research), journal)
    assert report['expected_sparse_journal_boundaries'] == len(STRATEGY_TRACE_EVENTS) + 3
    assert report['journal_trace_unavailable_rows'] == len(STRATEGY_TRACE_EVENTS)
    assert report['journal_l2_enqueued_rows'] == 2 and report['journal_l2_not_persisted_rows'] == 1
    assert report['persisted_decision_point_l2_rows'] == 1
    assert report['l2_reference_joined_rows'] == 1 and report['l2_reference_missing_rows'] == 2


def test_submit_payload_instrument_resolves_actual_book_not_current_side():
    from nautilus_trader.model.identifiers import InstrumentId
    host, rows, journal = actual_cache_host()
    expected_id = str(host.instrument_id)
    host.instrument_id = InstrumentId.from_str('down.POLYMARKET')
    # Real maker submission shape: only submitted_instrument_id is provided.
    assert StrategyDBRuntimeMixin._db_order_event(host, event_type='ORDER_SUBMIT',
        status='SUBMITTED', payload={'submitted_instrument_id': expected_id})
    assert rows[0]['payload']['instrument_id'] == expected_id
    assert journal[0]['payload']['decision_trace']['l2']['status'] == 'L2_ENQUEUED'


def test_reference_join_never_crosses_run_or_market(tmp_path):
    # Same reference text in another run is not proof of persistence for this journal.
    research, journal = tmp_path/'r.db', tmp_path/'j.db'
    with sqlite3.connect(research) as conn:
        conn.execute('CREATE TABLE lead_lag_decisions(run_id TEXT,slug TEXT,decision_epoch_ns INTEGER,payload_json TEXT)')
    with sqlite3.connect(journal) as conn:
        for table in ('order_events', 'strategy_events'):
            conn.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY,ts TEXT,run_id TEXT,event_type TEXT,payload_json TEXT)')
        conn.execute("INSERT INTO order_events VALUES(1,'1970-01-01T00:01:40+00:00','r','ORDER_SUBMIT','{}')")
    with sqlite3.connect(research) as conn:
        p = {'event_type': 'DECISION_POINT_L2', 'l2_evidence_id': 'same', 'l2': {'status': 'L2_AVAILABLE'}}
        conn.execute('INSERT INTO lead_lag_decisions VALUES(?,?,?,?)', ('other','m',100,json.dumps(p)))
    with sqlite3.connect(journal) as conn:
        payload = {'slug': 'm', 'decision_trace': {'run_id': 'r', 'market_slug': 'm',
                    'l2': {'status': 'L2_ENQUEUED', 'l2_evidence_id': 'same'}}}
        conn.execute('UPDATE order_events SET payload_json=?', (json.dumps(payload),))
    report = engineering_integrity(ResearchStore(research), journal)
    assert report['l2_reference_joined_rows'] == 0 and report['l2_reference_missing_rows'] == 1


@pytest.mark.parametrize('tag', sorted(STRATEGY_TRACE_EVENTS))
def test_all_claimed_strategy_boundaries_actually_annotate(tag):
    host, _, journal = actual_cache_host()
    assert StrategyDBRuntimeMixin._db_strategy_event(host, tag, {'should_quote': False})
    assert journal[0]['payload']['decision_trace']['trace_schema_version'] == 2


def test_resolver_exception_cannot_change_authoritative_journal():
    host, rows, journal = actual_cache_host()
    host._normalize_instrument_id = lambda value: (_ for _ in ()).throw(RuntimeError('resolver failure'))
    assert StrategyDBRuntimeMixin._db_order_event(host, event_type='ORDER_SUBMIT', status='SUBMITTED')
    assert not rows and journal[0]['status'] == 'SUBMITTED'
    assert journal[0]['payload']['decision_trace']['l2']['status'] == 'L2_NOT_AVAILABLE'


def test_book_read_failure_retains_explicit_unavailable_trace():
    host, rows, journal = actual_cache_host()
    broken = SimpleNamespace(bids=lambda: (_ for _ in ()).throw(RuntimeError('depth unavailable')))
    host.cache = SimpleNamespace(order_book=lambda instrument: broken)
    assert StrategyDBRuntimeMixin._db_order_event(host, event_type='ORDER_SUBMIT', status='SUBMITTED')
    assert not rows
    assert journal[0]['payload']['decision_trace']['l2']['status'] == 'L2_NOT_AVAILABLE'
