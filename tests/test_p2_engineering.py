"""P2 contracts use synthetic evidence, temporary files, and no production clients."""
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from collections import deque

import pytest

from bot.journal_replay import replay_evidence
from bot.execution_events import audit_reconciliation
from bot.research.clocks import available_at, latest_evidence, compare_clocks, epoch
from bot.research.indexing import migrate_closed_copy, benchmark_index
from bot.research.storage import StorageSummary, archival_readiness
from bot.research.store import ResearchStore
from bot.session_pnl_guard import SessionPnlGuardConfig
from bot.db_runtime import StrategyDBRuntimeMixin
from scripts.research_analysis import canonical_replay, engineering_integrity


def fill(qty=2, side='BUY', fill_id='f1', client='c1', ts=100, **updates):
    return {'event_type':'ORDER_FILLED','event_ts':ts,'persist_ts':ts,'run_id':'r','side':side,
            'client_order_id':client,'instrument_id':'up','price':.6,'qty':qty,'commission_usdc':0,
            'payload':{'fill_event_id':fill_id,'effective_fee_shares':0,'market_slug':'m', **updates}}


@pytest.mark.parametrize('field', ['btc_source_ts','p_ex_source_ts','market_up_source_ts',
                                 'settlement_evidence_ts','sigma_evidence_ts','fill_ts'])
@pytest.mark.parametrize('nested', [False, True])
def test_no_future_inputs_in_any_replay(field, nested):
    row = {'event_ts':100, field:101} if not nested else {'event_ts':100,'payload':{field:101}}
    result = replay_evidence([row], kind='MARKET', decision_ts=100)
    assert not result['rows'] and result['excluded']
    assert result['classification'] == 'APPROXIMATE_REPLAY'


def test_replay_repeatability_ordering_and_pure_guard_reuse():
    events = [{'event_type':'MARKET_CYCLE_PNL','event_ts':ts,'payload':{'cycle_combined_pnl_usdc':pnl,'market_slug':str(ts)}}
              for ts,pnl in [(110,-5),(100,10),(120,4)]]
    config = SessionPnlGuardConfig()
    args = dict(kind='DECISION', decision_ts=115, guard_config=config, complete_history=True)
    a,b = replay_evidence(events, **args), replay_evidence(events, **args)
    assert a == b
    assert a['classification'] == 'EXACT_REPLAY'
    assert a['rows'][-1]['reason_code'] == 'session_profit_drawdown_lock'
    assert a['rows'][-1]['realized_pnl_usdc'] == '5'
    assert not a['rows'][-1]['buy_allowed_after']
    assert len(a['excluded']) == 1
    assert replay_evidence(events, kind='DECISION', decision_ts=115)['classification'] == 'NOT_REPLAYABLE'


def test_accounting_replay_reuses_ledger_dedupes_and_rejects_future_fill():
    rows = [fill(), fill(), fill(qty=1,side='SELL',fill_id='f2',ts=101),fill(fill_id='future',ts=105)]
    result = replay_evidence(rows, kind='ACCOUNTING', decision_ts=101, complete_history=True)
    assert result['classification'] == 'EXACT_REPLAY'
    assert [row['qty'] for row in result['rows']] == ['2','1']
    assert result['rows'][-1]['realized_delta_usdc'] == '0.0'
    assert len(result['excluded']) == 2
    assert replay_evidence([fill(side='SELL')],kind='ACCOUNTING',decision_ts=101)['classification'] == 'NOT_REPLAYABLE'


def test_replay_missing_fees_or_duplicate_cycle_is_honest():
    row=fill();row['commission_usdc']=None
    assert replay_evidence([row],kind='ACCOUNTING',decision_ts=100,complete_history=True)['classification']=='APPROXIMATE_REPLAY'
    cycle={'event_type':'MARKET_CYCLE_PNL','event_ts':100,'payload':{'market_slug':'m','cycle_combined_pnl_usdc':3}}
    result=replay_evidence([cycle,cycle],kind='DECISION',decision_ts=100,guard_config=SessionPnlGuardConfig(),complete_history=True)
    assert result['classification']=='NOT_REPLAYABLE'
    assert len(result['rows'])==1


@pytest.mark.parametrize('left,right,status', [
    ('SOURCE_TS','SOURCE_TS','COMPARABLE'),('RECEIVE_TS','RECEIVE_TS','COMPARABLE'),
    ('SOURCE_TS','PERSIST_TS','CLOCKS_NOT_COMPARABLE'),('DECISION_TS','PERSIST_TS','CLOCKS_NOT_COMPARABLE')])
def test_clock_domains_require_explicit_cross_domain_justification(left,right,status):
    a,b={'ts':100,'kind':left,'clock':'binance'},{'ts':101,'kind':right,'clock':'binance'}
    assert compare_clocks(a,b)['status']==status
    assert compare_clocks(a,b,justification='explicit receive-to-persist latency')['delta_sec']==1


def test_external_clock_skew_not_faked_and_snapshot_selector_shared():
    assert epoch('2026-10-04T00:00:00') is None
    assert compare_clocks({'ts':100,'kind':'SOURCE_TS','clock':'binance'},
                          {'ts':99,'kind':'RECEIVE_TS','clock':'host'})['delta_sec'] is None
    rows=[{'snapshot_ts':95,'joint_fresh':True},{'snapshot_ts':101,'joint_fresh':True}]
    assert latest_evidence(rows,100,max_age_sec=8,require_joint_fresh=True)['snapshot_ts']==95
    assert available_at({'snapshot_ts':100,'btc_source_ts':101},100) is False


def test_reconciliation_partial_cancel_duplicate_and_restart():
    submit={'event_type':'ORDER_BUY_SUBMIT','client_order_id':'c1','instrument_id':'up','qty':5,'payload':{}}
    rows=[submit,fill(),fill(qty=1,fill_id='f2'),{'event_type':'ORDER_CANCELED','client_order_id':'c1'}]
    report=audit_reconciliation(rows,local_inventory={'up':3},venue_inventory={'up':3},open_orders=[{'client_order_id':'c1','filled_qty':3}])
    assert report['status']=='RECOVERABLE_MISMATCH'
    assert report['journal_inventory']=={'up':'3'}
    assert report['execution_action'] is None
    assert report['issues'][0]['reason_code']=='CANCELLED_WITH_PARTIAL_FILL'
    dup=audit_reconciliation([fill(),fill()])
    assert dup['status']=='RECOVERABLE_MISMATCH'
    assert dup['journal_inventory']['up']=='2'
    conflict=audit_reconciliation([fill(),fill(qty=3)])
    assert conflict['status']=='CRITICAL_INCONSISTENCY'


@pytest.mark.parametrize('case,code', [
    ('venue','VENUE_LOCAL_QTY_MISMATCH'),('journal','JOURNAL_LOCAL_QTY_MISMATCH'),
    ('restart','RESTART_OPEN_ORDER_WITHOUT_INTENT'),('duplicate','DUPLICATE_CLIENT_ORDER_ID'),
    ('settlement','REPEATED_FINALIZATION'),('redeem','REPEATED_FINALIZATION')])
def test_reconciliation_mismatch_classes(case,code):
    rows=[fill()];args={}
    if case=='venue':args={'local_inventory':{'up':2},'venue_inventory':{'up':3}}
    if case=='journal':args={'local_inventory':{'up':1}}
    if case=='restart':args={'open_orders':[{'client_order_id':'unknown','filled_qty':1}]}
    if case=='duplicate':rows=[{'event_type':'ORDER_BUY_SUBMIT','client_order_id':'c','qty':2}]*2
    if case in {'settlement','redeem'}:
        rows=[{'event_type':'MARKET_SETTLEMENT' if case=='settlement' else 'REDEEM_RECONCILIATION','payload':{'slug':'m'}}]*2
    result=audit_reconciliation(rows,**args)
    assert result['status']=='UNRESOLVED_MISMATCH'
    assert code in [item['reason_code'] for item in result['issues']]


def test_reconciliation_reopen_identity_and_open_inventory():
    assert audit_reconciliation([fill()],local_inventory={'up':2})['status']=='CONSISTENT'
    rows=[fill(position_lifecycle_id='m|up|c1'),fill(side='SELL',fill_id='f2'),
          fill(fill_id='f3',client='c2',position_lifecycle_id='m|up|c1')]
    assert audit_reconciliation(rows)['status']=='CRITICAL_INCONSISTENCY'
    rows[-1]['payload']['position_lifecycle_id']='m|up|c2'
    assert audit_reconciliation(rows)['status']=='CONSISTENT'


def dbs(tmp_path):
    research,journal=tmp_path/'research?#.db',tmp_path/'journal?#.db'
    with sqlite3.connect(research) as conn:
        conn.execute('CREATE TABLE lead_lag_decisions(run_id TEXT,slug TEXT,decision_epoch_ns INTEGER,payload_json TEXT)')
        for run,ts in [('r',100),('r',100),('other',95),('r',105)]:
            payload={'event_type':'PREDICTION_RESEARCH_SNAPSHOT','snapshot_ts':ts,'joint_fresh':True,'market_slug':'m'}
            conn.execute('INSERT INTO lead_lag_decisions VALUES(?,?,?,?)',(run,'m',int(ts*1e9),json.dumps(payload)))
    with sqlite3.connect(journal) as conn:
        conn.execute('CREATE TABLE order_events(id INTEGER PRIMARY KEY,ts TEXT,run_id TEXT,event_type TEXT,side TEXT,instrument_id TEXT,price REAL,qty REAL,commission_usdc REAL,payload_json TEXT)')
        conn.execute('CREATE TABLE strategy_events(id INTEGER PRIMARY KEY,ts TEXT,run_id TEXT,event_type TEXT,payload_json TEXT)')
        conn.execute("INSERT INTO order_events VALUES(1,'1970-01-01T00:01:40+00:00','r','ORDER_FILLED','BUY','up',.6,2,0,?)",
                     (json.dumps(fill()['payload']),))
    return research,journal


def test_store_time_range_journal_filters_readonly_and_canonical_replay(tmp_path):
    research,journal=dbs(tmp_path);store=ResearchStore(research)
    assert len(list(store.rows(run_id='r',slug='m',start_ts=100,end_ts=100)))==2
    assert len(store.get_prediction_snapshots(run_id='r'))==2
    rows=store.journal_events(journal,run_id='r',slug='m')
    assert len(rows)==1 and rows[0]['persist_ts']==100
    assert not store.journal_events(journal,run_id='other')
    assert canonical_replay(research,journal,kind='ACCOUNTING',decision_ts=100,complete_history=True)['classification']=='APPROXIMATE_REPLAY'
    report=engineering_integrity(store,journal)
    assert report['reconciliation']['status']=='CONSISTENT'
    with store._connect() as conn:
        with pytest.raises(sqlite3.OperationalError):conn.execute('CREATE TABLE bad(x)')


def test_migration_closed_copy_only_idempotent_and_plans(tmp_path):
    research,_=dbs(tmp_path)
    with pytest.raises(ValueError):migrate_closed_copy(research)
    first=migrate_closed_copy(research,closed_copy_confirmed=True)
    second=migrate_closed_copy(research,closed_copy_confirmed=True)
    assert first['created'] and not second['created']
    assert first['quick_check']=='ok' and not first['destructive']
    benchmark=benchmark_index(rows=1000)
    after=[r for r in benchmark['measurements'] if r['phase']=='after']
    assert len(after)==5 and all(r['index_used'] for r in after)
    assert all(r['elapsed_ms_median']>=0 for r in after)


def test_storage_throttle_failure_isolation_and_archive_no_actions(tmp_path,monkeypatch):
    import bot.research.storage as storage
    research,journal=dbs(tmp_path)
    btc=tmp_path/'history';btc.mkdir();(btc/'x.parquet').write_bytes(b'abc')
    summary=StorageSummary(interval_sec=100)
    a=summary.measure(journal=journal,research=research,btc_dir=btc,now_monotonic=0)
    assert a['btc_parquet_size']==3 and a['free_disk']>0
    monkeypatch.setattr(storage.shutil,'disk_usage',lambda *_: (_ for _ in ()).throw(OSError('unavailable')))
    b=summary.measure(journal=journal,research=research,btc_dir=btc,now_monotonic=50)
    assert b['cached'] and b['research_db_size']==a['research_db_size']
    c=summary.measure(journal=journal,research=research,btc_dir=btc,now_monotonic=101)
    assert c['state']=='UNKNOWN'
    for closed,stopped,verified,state in [(False,False,False,'ACTIVE'),(True,True,False,'CLOSED'),(True,True,True,'ARCHIVABLE')]:
        result=archival_readiness(collection_closed=closed,writers_stopped=stopped,integrity_verified=verified,backup_verified=verified)
        assert result['state']==state and result['automatic_action'] is None


@pytest.mark.parametrize('qty', ['NaN','bad','Infinity'])
def test_invalid_reconciliation_snapshot_quantity_is_reported(qty):
    result=audit_reconciliation([fill()],local_inventory={'up':qty})
    assert result['status']=='CRITICAL_INCONSISTENCY'
    assert result['issues'][-1]['reason_code']=='INVALID_SNAPSHOT_QUANTITY'


def test_replay_failure_cannot_mutate_inputs_or_policy():
    row=fill();row['price']='invalid'
    before=json.dumps(row,sort_keys=True)
    with pytest.raises(Exception):replay_evidence([row],kind='ACCOUNTING',decision_ts=100)
    assert json.dumps(row,sort_keys=True)==before


def test_canonical_replay_cli_and_index_benchmark_without_source_database(tmp_path,monkeypatch,capsys):
    import sys
    from scripts import research_analysis as analysis
    research,journal=dbs(tmp_path)
    monkeypatch.setattr(sys,'argv',['research_analysis','replay','--db',str(research),'--journal',str(journal),
                                   '--replay-kind','MARKET','--decision-ts','100','--run-id','r'])
    analysis.main()
    output=json.loads(capsys.readouterr().out)
    assert output['kind']=='MARKET' and len(output['rows'])==1
    # Canonical replay uses Store snapshot dedupe.
    monkeypatch.setattr(sys,'argv',['research_analysis','replay','--decision-ts','100'])
    with pytest.raises(SystemExit):analysis.main()


def test_clock_requires_known_named_origin_and_receive_source_skew():
    a,b={'ts':2,'kind':'SOURCE_TS'},{'ts':3,'kind':'SOURCE_TS'}
    assert compare_clocks(a,b)['status']=='CLOCKS_NOT_COMPARABLE'
    result=compare_clocks({'ts':2,'kind':'SOURCE_TS','clock':'exchange'},
                          {'ts':1,'kind':'RECEIVE_TS','clock':'host'},justification='observe clock skew only')
    assert result['delta_sec']==-1  # Diagnostic skew; not an impossible failure assumption.


def test_real_redeem_tag_repeats_same_tx_but_allows_distinct_partial_redemptions():
    events=[{'event_type':'REDEEM_EXECUTED','payload':{'slug':'m','tx_hash':'tx1'}}]*2
    assert audit_reconciliation(events)['status']=='UNRESOLVED_MISMATCH'
    events[-1]={'event_type':'REDEEM_EXECUTED','payload':{'slug':'m','tx_hash':'tx2'}}
    assert audit_reconciliation(events)['status']=='CONSISTENT'


def test_snapshot_replay_revision_after_decision_is_never_backfilled(tmp_path):
    research,journal=dbs(tmp_path)
    with sqlite3.connect(research) as conn:
        payload={'event_type':'PREDICTION_RESEARCH_SNAPSHOT','snapshot_ts':100,'p_up_ex_market':.99,'market_slug':'m'}
        conn.execute('INSERT INTO lead_lag_decisions VALUES(?,?,?,?)',('r','m',int(105e9),json.dumps(payload)))
    result=canonical_replay(research,journal,kind='MARKET',decision_ts=100,run_id='r')
    assert len(result['rows'])==1
    assert 'p_up_ex_market' not in result['rows'][0]


def test_integrity_cli_reads_synthetic_schema_and_reports_missing_trace(tmp_path,monkeypatch,capsys):
    import sys
    from scripts.research_analysis import main
    research,journal=dbs(tmp_path)
    monkeypatch.setattr(sys,'argv',['research_analysis','integrity','--db',str(research),'--journal',str(journal)])
    main()
    report=json.loads(capsys.readouterr().out)
    assert report['quick_check']=='ok'
    assert report['run_provenance']['unclassified_market_count']==1
    assert report['engineering']['journal_trace_unavailable_rows']==1
    assert report['engineering']['reconciliation']['execution_action'] is None


def test_incomplete_history_sell_is_unresolved_not_proven_critical():
    assert audit_reconciliation([fill(side='SELL')])['status']=='UNRESOLVED_MISMATCH'
    assert audit_reconciliation([fill(side='SELL')],complete_history=True)['status']=='CRITICAL_INCONSISTENCY'


def test_same_fill_identity_with_conflicting_fee_is_critical():
    a,b=fill(),fill();b['commission_usdc']=1
    assert audit_reconciliation([a,b])['status']=='CRITICAL_INCONSISTENCY'
