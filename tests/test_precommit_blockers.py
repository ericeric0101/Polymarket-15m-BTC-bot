"""Regression coverage for the pre-commit audit, using synthetic/temp data only."""
from __future__ import annotations

import csv
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest

from bot.db_runtime import StrategyDBRuntimeMixin
from bot.inventory import InventoryLedger
from bot.research.evidence import MarketEvidence
from bot.research.lifecycle import fill_lifecycle_metadata, first_crossings, position_lifecycle_id
from bot.research.provenance import _git_metadata, build_run_manifest
from bot.research.store import ResearchStore
from bot.session_pnl_guard import SessionPnlGuardConfig
from monitoring.lead_lag_db import LeadLagDB
from monitoring.trade_journal_db import TradeJournalDB
from scripts.research_analysis import (
    _brier, _checkpoint_summary, _journal_fill_entries, _read_shadow_settlements,
    _snapshot_near, entry_stop_status_analysis, position_lifecycle_analysis,
    preliminary_regime_comparison,
)

SLUG = "btc-updown-15m-1791072000"
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc).timestamp()


class GuardHost(StrategyDBRuntimeMixin):
    def __init__(self, db, mode="legacy"):
        self.trade_db = db
        self.session_pnl_guard_mode = mode
        self.monthly_net_target_usdc = Decimal("500") if mode != "legacy" else None
        self.session_guard_per_trade_risk_usdc = Decimal("10")
        self.events = []
        self.block_reasons = []

    def _db_strategy_event(self, event_type, payload):
        self.events.append((event_type, payload))

    def _block_new_buys_for_trade_db(self, reason):
        self.block_reasons.append(reason)


@pytest.fixture
def journal(tmp_path):
    db = TradeJournalDB(str(tmp_path / "journal.db"), backup_interval_sec=3600)
    yield db
    db.stop()


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, "background operation did not finish"
        time.sleep(.005)


def research_db(tmp_path, snapshots=(), stops=()):
    path = tmp_path / "research?# space.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE lead_lag_decisions (id INTEGER PRIMARY KEY, run_id TEXT, slug TEXT, market_id INTEGER, decision_epoch_ns INTEGER, payload_json TEXT)")
        payloads = [*snapshots, *stops, {"event_type": "MARKET_TWAP_SUMMARY", "market_slug": SLUG,
                                      "canonical_settlement_side": "DOWN", "snapshot_ts": 200}]
        for payload in payloads:
            epoch = int(payload.get("snapshot_ts", payload.get("actual_stop_ts", 100)) * 1e9)
            conn.execute("INSERT INTO lead_lag_decisions VALUES(NULL,'r',?,NULL,?,?)",
                         (SLUG, epoch, json.dumps(payload)))
    return path


def snapshot(ts=100, **kwargs):
    return {"event_type": "PREDICTION_RESEARCH_SNAPSHOT", "freshness_clock_semantics_version": 2, "market_slug": SLUG,
            "snapshot_ts": ts, "joint_fresh": True, "p_ex_fresh": True,
            "market_mid_up_fresh": True, "market_mid_up": .7,
            "p_up_ex_market": .8, "p_down_ex_market": .2, "settlement_state_side": "UP",
            "time_left_sec": 300, "up_instrument_id": "up", **kwargs}


def add_fill(db, *, client="buy-1", qty=2, price=.6, ts=100, identity=None, fill_id=None):
    payload = {"market_slug": SLUG, "slug": SLUG, "research_candidate_id": f"{SLUG}|up|UP|99"}
    if identity:
        payload.update(position_lifecycle_id=identity, entry_client_order_id=identity.rsplit("|", 1)[-1])
    if fill_id:
        payload["fill_event_id"] = fill_id
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO order_events(ts,run_id,event_type,client_order_id,side,price,qty,instrument_id,payload_json) VALUES(?,'r','ORDER_FILLED',?,'BUY',?,?,'up',?)",
                     (datetime.fromtimestamp(ts, timezone.utc).isoformat(), client, price, qty, json.dumps(payload)))


def test_brier_checkpoint_contract_empty_and_nonempty():
    assert _brier([]) is None
    assert _brier([(.3, False), (.8, True)]) == pytest.approx(.065)
    assert all(row["market_brier"] is None for row in _checkpoint_summary([], "WEEKEND"))
    rows = [{"checkpoint_sec": 300, "observed_flip": True,
             "market_implied_flip_probability": .7, "analytic_flip_probability": .8}]
    output = _checkpoint_summary(rows, "WEEKEND")[0]
    assert output["market_brier"] == pytest.approx(.09)
    assert output["analytic_brier"] == pytest.approx(.04)


def test_preliminary_regimes_and_checkpoint_calibration_execute(tmp_path, journal):
    path = research_db(tmp_path, [snapshot(required_move_sigma=0, required_move_bps=0)])
    preliminary_regime_comparison(path, Path(journal.db_path), tmp_path / "prelim")
    entry_stop_status_analysis(path, Path(journal.db_path), tmp_path / "status")
    with (tmp_path / "status" / "calibration_by_checkpoint.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert any(row["usable_N"] == "1" and row["brier"] for row in rows)


def test_reporting_failure_does_not_change_journal_or_buy_health(journal, monkeypatch):
    host = GuardHost(journal, "target_scaled_v2")
    host._initialize_session_pnl_guard(NOW)
    journal.request_monthly_pnl_refresh("2026-10")
    wait_for(lambda: journal.cached_monthly_pnl("2026-10")["updated_ts"] > 0)
    prior_health = journal.runtime_health()
    with journal._connect() as conn:
        conn.execute("INSERT INTO strategy_events(ts,run_id,event_type,payload_json) VALUES('2026-10-04T12:00:00+00:00','r','MARKET_CYCLE_PNL','not-json')")
    assert journal.reconstruct_monthly_realized_pnl("2026-10") is None
    assert journal.runtime_health() == prior_health
    before = journal.cached_monthly_pnl("2026-10")["updated_ts"]
    journal.request_monthly_pnl_refresh("2026-10")
    wait_for(lambda: journal.cached_monthly_pnl("2026-10")["updated_ts"] > before)
    assert host.session_buy_guard_decision(NOW).allowed
    assert host._monthly_pnl_status["realized_pnl_usdc"] is None
    assert not host.block_reasons


@pytest.mark.parametrize("mode", ["legacy", "target_scaled_v2", "shadow_target_scaled_v2"])
def test_realized_update_never_calls_monthly_query(journal, monkeypatch, mode):
    host = GuardHost(journal, mode)
    host._initialize_session_pnl_guard(NOW)
    if mode != "legacy":
        wait_for(lambda: journal.cached_monthly_pnl("2026-10")["updated_ts"] > 0)
    def forbidden(*args):
        pytest.fail("monthly reconstruction on realized hot path")
    monkeypatch.setattr(journal, "reconstruct_monthly_realized_pnl", forbidden)
    assert host._record_session_realized_pnl(1, source="sell_fill")
    assert host._session_pnl_guard.state.realized_pnl_usdc == 1


def test_completed_cycle_refresh_is_background_and_cache_correct(journal, monkeypatch):
    calls = []
    original = journal.reconstruct_monthly_realized_pnl
    def observed(month):
        calls.append(threading.current_thread().name)
        return original(month)
    monkeypatch.setattr(journal, "reconstruct_monthly_realized_pnl", observed)
    month = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Taipei')).strftime('%Y-%m')
    journal.request_monthly_pnl_refresh(month)
    wait_for(lambda: journal.cached_monthly_pnl(month)["updated_ts"] > 0)
    assert journal.log_strategy_event("r", "MARKET_CYCLE_PNL", {"cycle_combined_pnl_usdc": 3})
    wait_for(lambda: journal.cached_monthly_pnl(month)["realized_pnl_usdc"] == 3)
    assert calls and set(calls) == {"monthly-pnl-report"}


@pytest.mark.parametrize("old,new", [("legacy", "target_scaled_v2"), ("target_scaled_v2", "legacy"),
                                    ("legacy", "legacy"), ("target_scaled_v2", "target_scaled_v2")])
@pytest.mark.parametrize("completed", [False, True])
def test_guard_restart_preserves_completed_plus_interim_accounting(journal, old, new, completed):
    if completed:
        journal.log_strategy_event("r", "MARKET_CYCLE_PNL", {"cycle_combined_pnl_usdc": 5})
    journal.save_session_pnl_state({"session_date_taipei": "2026-10-04", "realized_pnl_usdc": 14,
                                   "realized_high_water_usdc": 18, "profit_guard_armed": True,
                                   "buy_lock_active": False, "buy_lock_reason": "", "guard_mode": old})
    host = GuardHost(journal, new)
    host._initialize_session_pnl_guard(NOW)
    assert host._session_pnl_guard.state.realized_pnl_usdc == 14
    assert host._session_pnl_guard.state.realized_high_water_usdc == 18
    assert journal.load_session_pnl_state("2026-10-04")["guard_mode"] == new


def test_same_mode_restart_keeps_sticky_lock(journal):
    journal.save_session_pnl_state({"session_date_taipei": "2026-10-04", "realized_pnl_usdc": 1,
        "realized_high_water_usdc": 18, "profit_guard_armed": True, "buy_lock_active": True,
        "buy_lock_reason": "session_profit_drawdown_lock", "guard_mode": "legacy"})
    host = GuardHost(journal)
    host._initialize_session_pnl_guard(NOW)
    assert not host.session_buy_guard_decision(NOW).allowed


def test_lifecycle_partial_scalein_close_reopen_uses_one_identity():
    ledger = {}
    identities = []
    for side, qty, client in [("buy", 2, "buy-1"), ("buy", 3, "buy-1"),
                              ("buy", 1, "buy-2"), ("sell", 6, "sell-1"), ("buy", 2, "buy-3")]:
        before = dict(ledger.get("up", {}))
        InventoryLedger.update_from_fill(ledger, "up", side, Decimal('.6'), Decimal(qty), Decimal(0), Decimal(0), 100)
        metadata = fill_lifecycle_metadata(market_slug=SLUG, instrument_id="up", client_order_id=client,
                                           side=side, before=before, after=ledger["up"])
        identities.append(metadata["position_lifecycle_id"])
        if side == 'sell':
            assert 'position_lifecycle_id' not in ledger['up']
    assert len(set(identities[:4])) == 1
    assert identities[-1] != identities[0]
    assert identities[-1].endswith('|buy-3')


def test_offline_partial_fills_scalein_and_duplicate_event(journal):
    identity = position_lifecycle_id(market_slug=SLUG, instrument_id="up", entry_client_order_id="buy-1")
    add_fill(journal.db_path, identity=identity, fill_id="f1", qty=2)
    add_fill(journal.db_path, identity=identity, fill_id="f2", qty=3, price=.7, ts=101)
    add_fill(journal.db_path, identity=identity, fill_id="f2", qty=3, price=.7, ts=101)
    add_fill(journal.db_path, identity=identity, fill_id="f3", client="buy-2", qty=1, ts=102)
    entries, audit = _journal_fill_entries(Path(journal.db_path), {SLUG: [snapshot(run_id='r')]})
    assert len(entries) == 1
    assert entries[0]['entry_qty'] == 5
    assert entries[0]['entry_price'] == pytest.approx(.66)
    assert entries[0]['total_buy_qty'] == 6
    assert entries[0]['scale_in_qty'] == 1
    assert entries[0]['buy_client_order_count'] == 2
    assert audit[0]['reason'] == 'DUPLICATE_FILL_EVENT'


@pytest.mark.parametrize('ts,fresh,expected', [(95, True, 95), (100, True, 100),
                                            (105, True, None), (91, True, None), (95, False, None)])
def test_entry_anchor_never_uses_future_or_stale_snapshot(ts, fresh, expected):
    row = _snapshot_near([snapshot(ts, joint_fresh=fresh)], 100)
    assert (row['snapshot_ts'] if row else None) == expected


def test_future_closer_snapshot_cannot_beat_prior():
    assert _snapshot_near([snapshot(95), snapshot(101)], 100)['snapshot_ts'] == 95


def test_legacy_stop_fallback_is_honestly_labeled(tmp_path, journal):
    add_fill(journal.db_path)
    path = research_db(tmp_path, [snapshot()], [{"event_type": "STOP_SHADOW_ACTUAL_STOP", "slug": SLUG,
        "instrument_id": "up", "actual_stop_ts": 130, "actual_stop_price": .5}])
    result = position_lifecycle_analysis(path, Path(journal.db_path), tmp_path / 'out')
    with (tmp_path / 'out' / 'position_lifecycle.csv').open() as handle:
        row = next(csv.DictReader(handle))
    assert row['stop_join_status'] == 'LEGACY_SLUG_INSTRUMENT_FALLBACK'
    assert row['stop_trigger_ts'] == ''
    assert row['stop_execution_ts'] == '130.0'
    assert result['exact_stop_joins'] == 0


def test_zero_sigma_crosses_all_thresholds():
    values = first_crossings([snapshot(required_move_sigma=0)], side='UP')
    assert values['sigma_cross_0_25_ts'] == 100
    assert values['sigma_cross_2_ts'] == 100


@pytest.mark.parametrize('bps,usable', [(None, '0'), (0, '1')])
def test_missing_bps_is_not_imputed_zero(tmp_path, journal, bps, usable):
    path = research_db(tmp_path, [snapshot(required_move_bps=bps)])
    entry_stop_status_analysis(path, Path(journal.db_path), tmp_path / 'out')
    with (tmp_path / 'out' / 'flip_rate_by_bps.csv').open() as handle:
        rows = list(csv.DictReader(handle))
    assert next(r for r in rows if r['session_regime'] == 'WEEKEND' and r['bin'] == '<2bps')['usable_N'] == usable


@pytest.mark.parametrize('pnl,status', [(0, 'USABLE'), (None, 'EXCLUDED')])
def test_shadow_missing_pnl_is_not_usable_zero(journal, pnl, status):
    with journal._connect() as conn:
        payload = {'slug': SLUG, 'simulation_id': 's', 'side': 'UP', 'filled_ts': 100,
                   'entry_price': .6, 'qty': 2, 'simulated_gross_pnl_usdc': pnl}
        conn.execute("INSERT INTO order_events(ts,run_id,event_type,payload_json) VALUES('1970-01-01T00:02:00+00:00','r','SHADOW_SIM_SETTLED',?)", (json.dumps(payload),))
    rows, _ = _read_shadow_settlements(Path(journal.db_path), regime='WEEKEND')
    assert rows[0]['status'] == status
    assert rows[0]['gross_pnl'] == pnl
    if pnl is None:
        assert rows[0]['exclusion_reason'] == 'PNL_UNKNOWN'


def test_leadlag_write_failure_is_not_successful_drain(tmp_path, monkeypatch):
    db = LeadLagDB(str(tmp_path / 'db.sqlite'))
    def fail():
        raise sqlite3.OperationalError('synthetic write failure')
    monkeypatch.setattr(db, '_connect', fail)
    assert db.enqueue_decision(run_id='r', slug='s', market_id=None, decision_epoch_ns=1, payload={})
    assert db.stop() is False
    assert db.research_health()['write_errors'] == 1


def test_leadlag_rejects_all_enqueue_paths_after_stop(tmp_path):
    db = LeadLagDB(str(tmp_path / 'db.sqlite'))
    assert db.stop()
    assert not db.enqueue_decision(run_id='r', slug='s', market_id=None, decision_epoch_ns=1, payload={})
    db.enqueue_snapshot(run_id='r', polymarket_slug='s', hyperliquid_market_id=None, observed_ts=1, payload={})
    db.enqueue_reference_1s(run_id='r', slug='s', market_id=None, bucket_epoch_ms=1, source='s', price_cents=1, received_epoch_ns=1)
    assert db._queue.empty()
    assert db.research_health()['late_enqueue_rejections'] == 3


def test_btc_no_worker_unflushed_rows_is_failure_and_late_observer_rejected(tmp_path):
    from bot.btc_1s_history import BTC1sHistoryCollector
    collector = BTC1sHistoryCollector(data_dir=tmp_path)
    collector.observe_aggtrade(price=100, source_ts_ms=1000, received_ts=1)
    assert not collector.stop()
    depth = collector._queue.qsize()
    collector.observe_aggtrade(price=100, source_ts_ms=4000, received_ts=4)
    assert collector._queue.qsize() == depth
    assert not collector._bars
    assert not collector.stop()


def test_btc_write_failure_invalidates_stop(tmp_path, monkeypatch):
    import bot.btc_1s_history as history
    pytest.importorskip('pyarrow')
    monkeypatch.setattr(history, 'atomic_write_parquet_part', lambda *_args: (_ for _ in ()).throw(OSError('synthetic disk full')))
    collector = history.BTC1sHistoryCollector(data_dir=tmp_path, min_free_disk_gb=0, batch_rows=1)
    collector.start()
    collector.observe_aggtrade(price=100, source_ts_ms=1000, received_ts=1)
    assert not collector.stop()
    assert collector._failure_reason


def test_store_json_formats_stable_ties_and_malformed_integrity(tmp_path):
    path = research_db(tmp_path)
    with sqlite3.connect(path) as conn:
        for i, separators in enumerate([None, (',', ':')]):
            conn.execute('INSERT INTO lead_lag_decisions VALUES(NULL,\'r\',?,NULL,100000000000,?)',
                         (SLUG, json.dumps(snapshot(value=i), separators=separators)))
        for raw in ['{bad', '[]']:
            conn.execute('INSERT INTO lead_lag_decisions VALUES(NULL,\'r\',?,NULL,100000000000,?)', (SLUG, raw))
    store = ResearchStore(path)
    assert store.get_prediction_snapshots()[0]['value'] == 1
    assert store.get_prediction_snapshots()[0]['value'] == 1
    report = store.integrity()
    assert report['malformed_json_rows'] == 1
    assert report['non_object_payload_rows'] == 1
    assert report['excluded_payload_rows'] == 2
    with store._connect() as conn:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            conn.execute('CREATE TABLE forbidden(x)')


def test_provenance_schema_probe_checks_ended_at(tmp_path):
    path = research_db(tmp_path)
    journal_path = tmp_path / 'legacy.db'
    with sqlite3.connect(journal_path) as conn:
        conn.execute('CREATE TABLE strategy_runs(run_id TEXT, started_at TEXT, mode TEXT, notes_json TEXT)')
    result = ResearchStore(path).get_run_provenance(journal_path)
    assert 'journal_strategy_runs_schema_unavailable' in result['warnings']
    assert 'journal_read_failed' not in result['warnings']


def test_evidence_deeply_immutable_and_display_preserves_provenance():
    payload = snapshot(config_hash='c', nested={'items': [1]})
    evidence = MarketEvidence.from_snapshot(payload, run_id='r', market_start_taipei=None, session_regime='WEEKEND')
    payload['nested']['items'].append(2)
    assert evidence.get('nested')['items'] == (1,)
    with pytest.raises(TypeError):
        evidence.payload['joint_fresh'] = False
    with pytest.raises(TypeError):
        evidence.get('nested')['items'] = ()
    display = evidence.display_fields()
    assert display['run_id'] == 'r'
    assert display['config_hash'] == 'c'
    display['joint_fresh'] = False
    assert evidence.get('joint_fresh') is True


def test_git_unavailable_is_unknown_and_profile_scope_explicit(monkeypatch, tmp_path):
    import bot.research.provenance as provenance
    monkeypatch.setattr(provenance.subprocess, 'run', lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('git unavailable')))
    assert _git_metadata(tmp_path)['git_dirty'] is None
    config = {'strategy_profile': 'known-profile', 'maker': {'api_secret': 'NEVER_STORE', 'passphrase': 'NEVER_STORE_PHRASE', 'apiKey': 'NEVER_STORE_CAMEL', 'size': 2}}
    manifest = build_run_manifest(run_id='r', config=config, mode='TEST_DRY_RUN', test_mode=True, maker_mode=True, repo_root=tmp_path)
    assert manifest['strategy_profile'] == 'known-profile'
    assert manifest['config_hash_scope'] == 'explicit_safe_section_allowlist_only'
    assert 'NEVER_STORE' not in json.dumps(manifest)


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity'])
@pytest.mark.parametrize('field', ['monthly_net_target_usdc', 'per_trade_risk_usdc'])
def test_v2_rejects_nonfinite_target_and_risk(value, field):
    args = {'mode': 'target_scaled_v2', 'monthly_net_target_usdc': Decimal(500), 'per_trade_risk_usdc': Decimal(10)}
    args[field] = Decimal(value)
    with pytest.raises(ValueError):
        SessionPnlGuardConfig(**args).target_scaled_thresholds()


def test_prometheus_registration_reinit_and_same_run_multi_instance(monkeypatch):
    import prometheus_client
    from run_bot import IntegratedBTCStrategy
    registry = prometheus_client.CollectorRegistry()
    monkeypatch.setattr(prometheus_client, 'REGISTRY', registry)
    first = SimpleNamespace(run_id='r', dashboard_state=None, terminal_dashboard=None)
    second = SimpleNamespace(run_id='r', dashboard_state=None, terminal_dashboard=None)
    IntegratedBTCStrategy._init_live_prom_metrics(first)
    IntegratedBTCStrategy._push_position_closed_to_prometheus(first, 3, 0)
    IntegratedBTCStrategy._init_live_prom_metrics(first)
    IntegratedBTCStrategy._init_live_prom_metrics(second)
    assert first._prom_live_metrics_ok and second._prom_live_metrics_ok
    assert first._live_cumulative_pnl == 3
    labels = {'strategy_run_id': 'r', 'strategy_instance': str(id(first))}
    other = {'strategy_run_id': 'r', 'strategy_instance': str(id(second))}
    assert registry.get_sample_value('trading_position_close_realized_pnl_usdc', labels) == 3
    assert registry.get_sample_value('trading_position_close_realized_pnl_usdc', other) == 0
    assert registry.get_sample_value('trading_position_close_trades_total', labels) == 1
    assert registry.get_sample_value('trading_live_realized_pnl') is None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_environment_rejects_nonfinite_monthly_target(monkeypatch, value):
    from bot.app_config import AppConfig
    monkeypatch.setenv("SESSION_PNL_GUARD_MODE", "target_scaled_v2")
    monkeypatch.setenv("MONTHLY_NET_TARGET_USDC", value)
    with pytest.raises(ValueError, match="finite"):
        AppConfig.from_env(enable_terminal_dashboard=False)


def test_restart_restores_persisted_lifecycle_without_changing_cost_basis(journal):
    from bot.recovery import StrategyRecoveryMixin
    now = time.time()
    identity = position_lifecycle_id(market_slug=SLUG, instrument_id="up", entry_client_order_id="buy-1")
    add_fill(journal.db_path, identity=identity, fill_id="a", ts=now, qty=2)
    add_fill(journal.db_path, identity=identity, fill_id="b", ts=now+1, client="buy-2", qty=1)
    host = SimpleNamespace(trade_db=journal, _normalize_instrument_id=lambda value: value,
                           _instrument_key=str, _normalize_side_text=lambda value: value.lower())
    state = StrategyRecoveryMixin._rebuild_inventory_state_from_db(host, "up", Decimal(3))
    assert state["position_lifecycle_id"] == identity
    assert state["entry_client_order_id"] == "buy-1"
    assert state["qty"] == 3
    assert state["avg_entry_price"] == Decimal("0.6")


def test_shutdown_stops_producers_before_shared_research_writer_and_reports_failure(monkeypatch):
    from bot import market_runtime
    from bot.enums import ActiveSide
    calls, warnings = [], []
    monkeypatch.setattr(market_runtime, "stop_event_threads", lambda **kwargs: calls.append("producers"))
    monkeypatch.setattr(market_runtime, "log_strategy_run_stop", lambda **kwargs: calls.append("run-stop"))
    monkeypatch.setattr(market_runtime.logger, "warning", lambda message, **kwargs: warnings.append(message))
    writer = SimpleNamespace(stop=lambda: calls.append("writer") or False)
    strategy = SimpleNamespace(
        _stopping=False, hyperliquid_outcome_observer=SimpleNamespace(stop=lambda: calls.append("observer")),
        outcome_lead_lag_runtime=SimpleNamespace(stop=lambda: calls.append("runtime")),
        lead_lag_db=writer, twap_research_db=writer, smart_money_tracker=None,
        _cancel_active_maker_orders=lambda: calls.append("cancel"),
        rebate_reporter=SimpleNamespace(flush_daily_report=lambda: None),
        _db_strategy_event=lambda *args: calls.append("final-event"), _is_dry_run_mode=lambda: True,
        inventory_delta_shares=Decimal(0), active_side=ActiveSide.NONE, market_cycle_realized_net_usdc=Decimal(0),
        trade_db=None, test_mode=True, maker_mode=True, run_id="r", instrument_id=None, selected_slug=None,
        terminal_dashboard=None,
    )
    for stem in ("lifecycle", "reload", "quote_watchdog", "redeem", "balance", "binance_ws", "polymarket_chainlink_ws", "terminal_dashboard"):
        setattr(strategy, f"_{stem}_stop_event", None)
        setattr(strategy, f"_{stem}_thread", None)
    market_runtime.handle_stop(strategy)
    assert "observer" not in calls and "runtime" not in calls
    assert calls.index("producers") < calls.index("writer")
    assert calls.index("final-event") < calls.index("writer")
    assert calls.count("writer") == 1
    assert any("incomplete or data lost" in message for message in warnings)


def test_leadlag_timeout_and_known_queue_drop_report_failure(tmp_path, monkeypatch):
    db = LeadLagDB(str(tmp_path / "writer.db"))
    original = db._connect
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(2)
        return original()
    monkeypatch.setattr(db, "_connect", blocked)
    db.enqueue_decision(run_id="r", slug="s", market_id=None, decision_epoch_ns=1, payload={})
    assert entered.wait(1)
    try:
        assert db.stop(timeout_sec=.1) is False
    finally:
        release.set()
        assert db.stop() is True
    # Known dropped rows invalidate a later clean drain.
    with db._health_lock:
        db._queue_drops += 1
    assert db.stop() is False


def test_btc_successful_flush_writes_final_bar_and_stop_is_idempotent(tmp_path):
    from bot.btc_1s_history import BTC1sHistoryCollector, load_btc_1s_history
    pytest.importorskip("pyarrow")
    collector = BTC1sHistoryCollector(data_dir=tmp_path, min_free_disk_gb=0)
    collector.start()
    collector.observe_aggtrade(price=100, source_ts_ms=1000, received_ts=1)
    assert collector.stop()
    assert collector.stop()
    assert len(load_btc_1s_history(1, 2, tmp_path)) == 1


def test_canonical_analysis_uses_store_without_legacy_loader(tmp_path, journal, monkeypatch):
    from scripts import four_market_prediction_forensics as forensic
    monkeypatch.setattr(forensic, "_load", lambda *args: pytest.fail("legacy loader used"))
    path = research_db(tmp_path, [snapshot()])
    entry_stop_status_analysis(path, Path(journal.db_path), tmp_path / "entry")
    position_lifecycle_analysis(path, Path(journal.db_path), tmp_path / "lifecycle")


def test_sparse_entry_confirms_identity_and_uses_prior_evidence(tmp_path, journal):
    identity = position_lifecycle_id(market_slug=SLUG, instrument_id="up", entry_client_order_id="buy-1")
    add_fill(journal.db_path, identity=identity, ts=101)
    entries, _ = _journal_fill_entries(Path(journal.db_path), {SLUG: [snapshot(95), snapshot(105)]}, [
        {"position_lifecycle_id": identity, "entry_client_order_id": "buy-1", "entry_ts": 100, "side": "UP"}])
    assert entries[0]["entry_ts"] == 100
    assert entries[0]["identity_source"] == "SPARSE_ENTRY_CONFIRMED"
    assert entries[0]["entry_anchor_status"] == "JOINED"


def test_reporting_worker_start_failure_does_not_change_buy_or_persistence(journal, monkeypatch):
    host = GuardHost(journal, 'target_scaled_v2')
    original_start = threading.Thread.start
    def start(thread):
        if thread.name == 'monthly-pnl-report':
            raise RuntimeError('synthetic thread unavailable')
        return original_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    host._initialize_session_pnl_guard(NOW)
    assert host.session_buy_guard_decision(NOW).allowed
    assert host._monthly_pnl_status['realized_pnl_usdc'] is None
    assert journal.runtime_health()['ready']
    assert not host.block_reasons
    assert journal.log_strategy_event('r', 'MARKET_CYCLE_PNL', {'cycle_combined_pnl_usdc': 2})
    assert journal.runtime_health()['ready']


def test_store_filters_run_slug_and_false_event_mentions(tmp_path):
    path = research_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO lead_lag_decisions VALUES(NULL,'other',?,NULL,100000000000,?)",
                     (SLUG, json.dumps(snapshot())))
        conn.execute("INSERT INTO lead_lag_decisions VALUES(NULL,'r',?,NULL,101000000000,?)",
                     (SLUG, json.dumps({'event_type': 'OTHER', 'note': 'PREDICTION_RESEARCH_SNAPSHOT'})))
    assert ResearchStore(path).get_prediction_snapshots(run_id='r') == []
    assert len(ResearchStore(path).get_prediction_snapshots(run_id='other', slug=SLUG)) == 1
    assert ResearchStore(path).get_prediction_snapshots(slug='missing') == []



def test_offline_reopen_is_a_new_position_not_scalein(journal):
    first = position_lifecycle_id(market_slug=SLUG, instrument_id="up", entry_client_order_id="buy-1")
    second = position_lifecycle_id(market_slug=SLUG, instrument_id="up", entry_client_order_id="buy-3")
    add_fill(journal.db_path, identity=first, fill_id="f1")
    add_fill(journal.db_path, identity=second, client="buy-3", fill_id="f2", ts=110)
    rows, _ = _journal_fill_entries(Path(journal.db_path), {SLUG: [snapshot(100), snapshot(110)]})
    assert len(rows) == 2
    assert {row["position_lifecycle_id"] for row in rows} == {first, second}
    assert all(row["scale_in_qty"] == 0 for row in rows)


def test_multiple_legacy_entries_make_stop_fallback_ambiguous(tmp_path, journal):
    add_fill(journal.db_path)
    add_fill(journal.db_path, client="buy-2", ts=110)
    path = research_db(tmp_path, [snapshot(100), snapshot(110)], [{
        "event_type": "STOP_SHADOW_ACTUAL_STOP", "slug": SLUG, "instrument_id": "up",
        "actual_stop_ts": 130, "actual_stop_price": .5}])
    position_lifecycle_analysis(path, Path(journal.db_path), tmp_path / "out")
    with (tmp_path / "out" / "position_lifecycle.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert all(row["stop_join_status"] == "AMBIGUOUS" for row in rows)
    assert all(row["stop_execution_ts"] == "" for row in rows)


def test_leadlag_connection_close_failure_invalidates_success(tmp_path, monkeypatch):
    db = LeadLagDB(str(tmp_path / "db.sqlite"))
    original = db._connect
    class Connection:
        def __init__(self):
            self.conn = original()
        def execute(self, *args):
            return self.conn.execute(*args)
        def commit(self):
            self.conn.commit()
        def close(self):
            self.conn.close()
            raise OSError("synthetic close failure")
    monkeypatch.setattr(db, "_connect", Connection)
    db.enqueue_decision(run_id="r", slug="s", market_id=None, decision_epoch_ns=1, payload={})
    assert not db.stop()
    assert db.research_health()["write_errors"] == 1
