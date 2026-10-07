"""Phase B startup proof using temporary stores and no production clients."""
import ast
import inspect
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import settings
from monitoring.lead_lag_db import LeadLagDB
from monitoring.trade_journal_db import TradeJournalDB


def test_normal_dry_run_initialization_has_one_writer_and_no_outcome_runtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('TRADE_DB_PATH', str(tmp_path / 'journal.db'))
    monkeypatch.setenv('TWAP_RESEARCH_DB_PATH', str(tmp_path / 'twap.db'))
    monkeypatch.setenv('BTC_1S_HISTORY_DIR', str(tmp_path / 'btc'))
    monkeypatch.setenv('REBATE_REPORT_DIR', str(tmp_path / 'rebate'))
    monkeypatch.setenv('OUTCOME_LEAD_LAG_MODE', 'live_entry_only')
    # Current BTC history collector stays enabled, but its files belong to tmp.
    h=SimpleNamespace(_initialize_session_pnl_guard=lambda: None)
    created=[]
    def writer(*args, **kwargs):
        db=LeadLagDB(*args, **kwargs);created.append(db);return db
    monkeypatch.setattr(settings,'LeadLagDB',writer)
    try:
        settings.initialize_strategy_settings(h,test_mode=True,enable_terminal_dashboard=False,
            project_root=tmp_path,detect_runtime_git_revision_fn=lambda _root: 'synthetic')
        assert len(created)==1 and created[0] is h.twap_research_db
        assert h.trend_entry_shadow.db is h.twap_research_db
        assert h.forward_shadow_experiment.db is h.twap_research_db
        assert h.stop_forensics_shadow.db is h.twap_research_db
        assert h.prediction_research_snapshotter.db is h.twap_research_db
        for name in ('lead_lag_db','hyperliquid_outcome_observer','outcome_fast_follow_live',
                     'outcome_lead_lag_runtime','outcome_lead_lag_shadow'):
            assert not hasattr(h,name)
        assert h.l2_update_ts_by_inst=={} and h.quote_max_delivery_delay_sec==2.0
        assert h.trade_db._backup_interval_sec==10800
        assert not list(tmp_path.rglob('hyperliquid_lead_lag.db'))
        assert not hasattr(h.app_config,'outcome_lead_lag')
        # Retained non-Outcome stream payloads survive the same writer.
        h.twap_research_db.enqueue_decision(run_id=h.run_id,slug='market',market_id=None,
            decision_epoch_ns=1,payload={'event_type':'TREND_ENTRY_SHADOW','value':42})
    finally:
        if getattr(h,'btc_1s_history_collector',None):h.btc_1s_history_collector.stop()
        for db in created:assert db.stop()
        if getattr(h,'trade_db',None):h.trade_db.stop()
    with sqlite3.connect(tmp_path/'twap.db') as c:
        rows=c.execute('select payload_json from lead_lag_decisions').fetchall()
        assert any(json.loads(r[0]).get('value')==42 for r in rows)


@pytest.mark.parametrize('alias', [False,True])
def test_runtime_store_cannot_point_to_historical_database(tmp_path,monkeypatch,alias):
    old=tmp_path/'hyperliquid_lead_lag.db';old.write_bytes(b'historical evidence')
    target=old
    if alias:
        target=tmp_path/'alias.db';target.symlink_to(old)
    monkeypatch.setenv('TWAP_RESEARCH_DB_PATH',str(target))
    with pytest.raises(ValueError,match='Historical Hyperliquid'):
        settings.build_twap_research_db()
    assert old.read_bytes()==b'historical evidence'


def test_legacy_db_requires_explicit_path():
    with pytest.raises(TypeError):LeadLagDB()


def test_no_runtime_outcome_import_network_or_execution_hooks():
    from run_bot import IntegratedBTCStrategy
    from bot import market_runtime,spot_pricer,db_runtime,order_events
    for module in (settings,market_runtime,spot_pricer,db_runtime):
        source=inspect.getsource(module)
        assert not any(name in source for name in ('OutcomeFastFollowLive','HyperliquidOutcomeObserver',
            'OutcomeLeadLagRuntime','publish_strategy_tick','record_hyperliquid_btc_probe'))
    assert not hasattr(IntegratedBTCStrategy,'fast_follow_execution_penalty_allows')
    assert not hasattr(IntegratedBTCStrategy,'_build_fast_follow_forecast_state')
    assert 'blocks_normal_buy' not in inspect.getsource(IntegratedBTCStrategy._evaluate_quote_targets)
    assert 'ORDER_FAST_FOLLOW' not in inspect.getsource(IntegratedBTCStrategy)
    assert 'outcome_fast_follow_live' not in inspect.getsource(order_events)
    assert 'outcome_subsystem_retired' in inspect.getsource(IntegratedBTCStrategy.on_start)


def test_missing_historical_report_db_is_not_created(tmp_path):
    from scripts.fast_follow_execution_report import build_report
    path=tmp_path/'missing.db'
    with pytest.raises(sqlite3.OperationalError):build_report(path)
    assert not path.exists()


def test_runtime_store_rejects_hardlink_to_old_database(tmp_path,monkeypatch):
    import os
    root=tmp_path/'repo';old=root/'data/research/hyperliquid_lead_lag.db'
    old.parent.mkdir(parents=True);old.write_bytes(b'keep history')
    alias=root/'alias.db';os.link(old,alias)
    monkeypatch.setattr(settings,'__file__',str(root/'bot/settings.py'))
    monkeypatch.setenv('TWAP_RESEARCH_DB_PATH',str(alias))
    with pytest.raises(ValueError,match='Historical Hyperliquid'):settings.build_twap_research_db()
    assert old.read_bytes()==b'keep history'
