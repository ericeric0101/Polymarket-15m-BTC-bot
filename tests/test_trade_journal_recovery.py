from decimal import Decimal
from types import SimpleNamespace
from datetime import datetime, timezone
import sqlite3
import time

import bot.db_runtime as db_runtime
import bot.market_runtime as market_runtime
from bot.execution_penalty_snapshot import load_execution_penalty_snapshot

from bot.db_runtime import StrategyDBRuntimeMixin
from monitoring.trade_journal_db import TradeJournalDB


def _fill(db, *, slug, order_id, side, price, qty, fee=0.0):
    db.log_order_event(
        run_id="run",
        event_type="ORDER_FILLED",
        client_order_id=order_id,
        side=side,
        price=price,
        qty=qty,
        payload={"slug": slug, "effective_fee_usdc": fee},
    )


def test_fresh_journal_created_at_startup_is_buy_ready(tmp_path):
    db = TradeJournalDB(tmp_path / "missing-journal.db")

    health = db.startup_health()

    assert health["ready"] is True
    assert health["reason"] == "fresh_initialized"
    db.stop()


def test_existing_empty_journal_without_recovery_evidence_is_buy_ready(tmp_path):
    path = tmp_path / "empty-journal.db"
    first = TradeJournalDB(path)
    first.stop()

    second = TradeJournalDB(path)
    health = second.startup_health()

    assert health["ready"] is True
    assert health["reason"] == "empty_journal_no_recovery_evidence"
    second.stop()


def test_missing_new_path_migrates_healthy_legacy_journal_atomically(tmp_path):
    legacy = tmp_path / "logs" / "trade_journal.db"
    target = tmp_path / "data" / "trading" / "trade_journal.db"
    source = TradeJournalDB(legacy)
    _fill(source, slug="btc-updown-test", order_id="buy-1", side="BUY", price=0.6, qty=5)
    source.stop()

    db = TradeJournalDB(target, legacy_db_path=legacy)

    assert db.startup_health()["ready"] is True
    assert db.startup_health()["reason"] == "migrated_legacy_journal"
    assert target.is_file()
    assert db.load_market_guard_counts("btc-updown-test")["buy_count"] == 1
    db.stop()


def test_legacy_journal_schema_is_not_buy_ready(tmp_path):
    path = tmp_path / "legacy-journal.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE strategy_runs (run_id TEXT PRIMARY KEY)")

    health = TradeJournalDB(path).startup_health()

    assert health["ready"] is False
    assert health["reason"] == "schema_invalid"


def test_order_event_write_creates_atomic_journal_backup(tmp_path):
    path = tmp_path / "trading" / "trade_journal.db"
    backup_path = tmp_path / "backups" / "trade_journal.db"
    db = TradeJournalDB(path, backup_path=backup_path)

    _fill(db, slug="btc-updown-test", order_id="buy-1", side="BUY", price=0.6, qty=5)
    db.flush_backup()

    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM order_events").fetchone()[0] == 1


def test_stop_flushes_last_dirty_journal_backup(tmp_path):
    path = tmp_path / "trading" / "trade_journal.db"
    backup_path = tmp_path / "backups" / "trade_journal.db"
    db = TradeJournalDB(path, backup_path=backup_path, backup_interval_sec=3600)

    _fill(db, slug="btc-updown-test", order_id="buy-1", side="BUY", price=0.6, qty=5)

    assert db._backup_dirty is True
    db.stop()

    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as conn:
        assert conn.execute("SELECT client_order_id FROM order_events").fetchone()[0] == "buy-1"


def test_production_shutdown_flushes_final_trade_journal_backup(monkeypatch, tmp_path):
    path = tmp_path / "trading" / "trade_journal.db"
    backup_path = tmp_path / "backups" / "trade_journal.db"
    db = TradeJournalDB(path, backup_path=backup_path, backup_interval_sec=3600)
    db._last_backup_monotonic = time.monotonic()
    db.log_run_start("run", "TEST", True, True)
    _fill(db, slug="btc-updown-test", order_id="buy-1", side="BUY", price=0.6, qty=5)
    assert db._backup_dirty is True

    monkeypatch.setattr(market_runtime, "stop_event_threads", lambda **_kwargs: None)
    strategy = SimpleNamespace(
        _stopping=False,
        hyperliquid_outcome_observer=None,
        outcome_lead_lag_runtime=None,
        lead_lag_db=None,
        _lifecycle_stop_event=None, _reload_stop_event=None, _quote_watchdog_stop_event=None,
        _redeem_stop_event=None, _balance_stop_event=None, _binance_ws_stop_event=None,
        _polymarket_chainlink_ws_stop_event=None, _terminal_dashboard_stop_event=None,
        _lifecycle_thread=None, _reload_thread=None, _quote_watchdog_thread=None,
        _redeem_thread=None, _balance_thread=None, _binance_ws_thread=None,
        _polymarket_chainlink_ws_thread=None, _terminal_dashboard_thread=None,
        smart_money_tracker=None,
        _cancel_active_maker_orders=lambda: None,
        rebate_reporter=SimpleNamespace(flush_daily_report=lambda: None),
        _db_strategy_event=lambda event, payload: db.log_strategy_event("run", event, payload),
        _is_dry_run_mode=lambda: True,
        inventory_delta_shares=Decimal("0"), active_side=SimpleNamespace(value="NONE"),
        trade_db=db, run_id="run", test_mode=True, maker_mode=True, instrument_id=None,
        selected_slug=None, market_cycle_realized_net_usdc=Decimal("0"), terminal_dashboard=None,
    )

    market_runtime.handle_stop(strategy)

    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as conn:
        assert conn.execute("SELECT client_order_id FROM order_events").fetchone()[0] == "buy-1"
        assert conn.execute(
            "SELECT COUNT(*) FROM strategy_events WHERE event_type='STRATEGY_STOP'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT ended_at FROM strategy_runs WHERE run_id='run'").fetchone()[0] is not None


def test_failed_backup_stays_dirty_and_retries_without_marking_journal_unhealthy(monkeypatch, tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db", backup_interval_sec=3600)
    db._last_backup_monotonic = time.monotonic()
    _fill(db, slug="btc-updown-test", order_id="buy-1", side="BUY", price=0.6, qty=5)
    attempts = []

    def backup_once():
        attempts.append(True)
        return len(attempts) > 1

    monkeypatch.setattr(db, "_backup_after_write", backup_once)
    db.flush_backup()
    assert db._backup_dirty is True
    assert db.runtime_health()["ready"] is True

    db.flush_backup()
    assert db._backup_dirty is False
    assert len(attempts) == 2
    db.stop()


def test_night_risk_query_error_returns_none_instead_of_zero_risk(monkeypatch, tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    monkeypatch.setattr(db, "_connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))

    assert db.load_fast_follow_night_risk("2026-09-08") is None


def test_critical_runtime_write_failure_marks_journal_not_buy_ready(monkeypatch, tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    monkeypatch.setattr(db, "_connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))

    assert db.log_order_event(run_id="r", event_type="ORDER_FILLED") is False

    assert db.runtime_health()["ready"] is False
    assert db.runtime_health()["reason"] == "write_failed"
    assert db.runtime_health()["event_type"] == "ORDER_FILLED"
    assert "disk full" in db.runtime_health()["error"]


def test_transient_critical_write_failure_recovers_after_a_successful_probe(monkeypatch, tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    original_connect = db._connect
    attempts = []

    def fail_once():
        attempts.append(True)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_connect()

    monkeypatch.setattr(db, "_connect", fail_once)
    assert db.log_order_event(run_id="r", event_type="ORDER_FILLED") is False
    assert db.runtime_health()["state"] == "HEALTHY"
    assert db.runtime_health()["ready"] is True
    db.stop()


def test_noncritical_runtime_write_failure_keeps_buy_health_ready(monkeypatch, tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    monkeypatch.setattr(db, "_connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))

    assert db.log_order_event(run_id="r", event_type="ENTRY_EDGE_OBSERVATION") is False

    assert db.runtime_health()["ready"] is True


def test_strategy_order_event_accepts_explicit_instrument_id(tmp_path):
    class Strategy(StrategyDBRuntimeMixin):
        def _normalize_side_text(self, side):
            return str(side).lower()

    captured = []
    strategy = Strategy()
    strategy.trade_db = SimpleNamespace(
        log_order_event=lambda **kwargs: captured.append(kwargs) or True,
    )
    strategy.run_id = "run"
    strategy.current_market_slug = "slug"
    strategy.instrument_id = "DEFAULT.INST"
    strategy.current_token_id = None
    strategy.last_observed_fee_rate_bps = None

    assert strategy._db_order_event(
        event_type="ORDER_FAST_FOLLOW_INTENT",
        side="BUY",
        instrument_id="FAST_FOLLOW.INST",
    ) is True
    assert captured[0]["instrument_id"] == "FAST_FOLLOW.INST"


def test_schema_missing_critical_recovery_column_is_not_buy_ready(tmp_path):
    path = tmp_path / "partial-journal.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE order_events (id INTEGER, ts TEXT, run_id TEXT, event_type TEXT, side TEXT, price REAL, qty REAL, payload_json TEXT)")

    assert TradeJournalDB(path).startup_health()["reason"] in {"schema_invalid", "schema_init_failed"}


class _RecoveryStrategy(StrategyDBRuntimeMixin):
    def __init__(self, db, slug="btc-updown-15m-test"):
        self.trade_db = db
        self.current_market_slug = slug
        self.market_strike_cache_by_slug = {}
        self.market_strike_source_by_slug = {}
        self.market_strike_status_by_slug = {}
        self.market_strike_provisional_by_slug = {}
        self.market_strike_provisional_source_by_slug = {}
        self.current_market_open_spot = None
        self.events = []

    def _is_authoritative_strike_source(self, source):
        return source == "polymarket_crypto_price_twap_open"

    def _db_strategy_event(self, event_type, payload):
        self.events.append((event_type, payload))


class _FallbackCalibrationStrategy(StrategyDBRuntimeMixin):
    def __init__(self):
        self.trade_db = None
        self.maker_engine = SimpleNamespace(config=SimpleNamespace())
        self.maker_execution_empirical_markout_lookback_hours = 168.0
        self.maker_execution_empirical_markout_min_samples = 30
        self.maker_fixed_shares = Decimal("10")
        self.maker_buy_markout_calibrations = {}
        self.strong_directional_regime_calibration = None
        self.events = []

    def _db_strategy_event(self, event_type, payload):
        self.events.append((event_type, payload))


def test_market_guard_counts_survive_restart_and_ignore_partial_fill_rows(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    slug = "btc-updown-15m-test"
    _fill(db, slug=slug, order_id="buy-1", side="BUY", price=0.7, qty=2)
    _fill(db, slug=slug, order_id="buy-1", side="BUY", price=0.7, qty=3)
    db.log_order_event(
        run_id="run",
        event_type="ORDER_TAKER_EXIT_SUBMIT",
        client_order_id="exit-1",
        side="SELL",
        reason="invalidation_recovery",
        payload={"slug": slug},
    )
    _fill(db, slug=slug, order_id="exit-1", side="SELL", price=0.4, qty=5)

    assert db.load_market_guard_counts(slug) == {
        "buy_count": 1,
        "protective_exit_count": 1,
    }


def test_recovery_preserves_explicitly_verified_authoritative_strike(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    slug = "btc-updown-15m-test"
    db.log_strategy_event(
        "run",
        "MARKET_STRIKE_LOCKED",
        {
            "slug": slug,
            "strike": 66611.11,
            "strike_source": "polymarket_crypto_price_twap_open",
            "authoritative": True,
            "strike_status": "verified",
        },
    )

    strategy = _RecoveryStrategy(db, slug)
    strategy._recover_market_strike_from_trade_db_on_startup()

    assert strategy.market_strike_cache_by_slug[slug] == Decimal("66611.11")
    assert strategy.market_strike_status_by_slug[slug] == "verified"
    recovered = strategy.events[-1][1]
    assert recovered["strike_status"] == "verified"


def test_recovery_keeps_legacy_strike_unverified_without_recorded_status(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    slug = "btc-updown-15m-test"
    db.log_strategy_event(
        "run",
        "MARKET_STRIKE_LOCKED",
        {
            "slug": slug,
            "strike": 66611.11,
            "strike_source": "polymarket_crypto_price_twap_open",
            "authoritative": True,
        },
    )

    strategy = _RecoveryStrategy(db, slug)
    strategy._recover_market_strike_from_trade_db_on_startup()

    assert strategy.market_strike_status_by_slug[slug] == "recovered_unverified"


def test_insufficient_journal_uses_portable_d4_168h_penalty_fallback(monkeypatch):
    strategy = _FallbackCalibrationStrategy()
    strategy.maker_execution_empirical_markout_lookback_hours = 48.0
    strategy.maker_execution_empirical_markout_min_samples = 5
    monkeypatch.setattr(
        db_runtime,
        "load_execution_penalty_snapshot",
        lambda: {
            "snapshot_id": "test-d4-snapshot",
            "expires_at": "2026-10-08T00:00:00+00:00",
            "source": "d4_portable_168h_snapshot",
            "sample_count": 84,
            "horizon_sec": 10,
            "lookback_hours": 168.0,
            "adverse_markout_per_share": Decimal("0.02515"),
            "raw_mean_adverse_markout_per_share": Decimal("0.02454"),
            "winsor_cap_per_share": None,
            "method": "d4_frozen_168h_estimator",
            "fallback_reason": "insufficient_current_journal_samples",
            "minimum_independent_samples": 30,
            "evidence": {"settled_training_samples": 84},
        },
    )

    strategy._apply_empirical_execution_penalty_calibration()

    assert strategy.maker_engine.config.maker_execution_empirical_adverse_markout_per_share == Decimal("0.02515")
    event_type, payload = strategy.events[0]
    assert event_type == "EXECUTION_PENALTY_FALLBACK_APPLIED"
    assert payload["fallback_applied"] is True
    assert payload["source"] == "d4_portable_168h_snapshot"
    assert payload["lookback_hours"] == 168.0
    assert payload["configured_lookback_hours"] == 48.0
    assert payload["minimum_independent_samples"] == 30
    assert payload["snapshot_id"] == "test-d4-snapshot"


def test_execution_penalty_snapshot_is_valid_before_its_expiry():
    snapshot = load_execution_penalty_snapshot(
        now=datetime(2026, 9, 8, tzinfo=timezone.utc),
    )

    assert snapshot is not None
    assert snapshot["source"] == "d4_portable_168h_snapshot"
    assert snapshot["adverse_markout_per_share"] == Decimal("0.02515")


def test_execution_penalty_snapshot_fails_closed_after_expiry():
    snapshot = load_execution_penalty_snapshot(
        now=datetime(2026, 10, 8, tzinfo=timezone.utc),
    )

    assert snapshot is None


def test_strong_directional_regime_calibration_uses_one_first_observation_per_market(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    for index, (candidate, outcome) in enumerate((("UP", "UP"), ("DOWN", "UP"), ("UP", "UP"))):
        slug = f"btc-updown-{index}"
        db.log_strategy_event(
            "run", "LIVE_SIGNAL_COMPARE",
            {
                "slug": slug,
                "main_candidate_side": f"BUY_{candidate}",
                "main_score": 0.40,
                "main_side_locked": True,
                "spot_minus_strike": 20 if candidate == "UP" else -20,
                "time_left_sec": 480,
            },
        )
        # A later flip must not create another sample for this market.
        db.log_strategy_event(
            "run", "LIVE_SIGNAL_COMPARE",
            {
                "slug": slug,
                "main_candidate_side": f"BUY_{outcome}",
                "main_score": 0.50,
                "main_side_locked": True,
                "spot_minus_strike": 20 if outcome == "UP" else -20,
                "time_left_sec": 480,
            },
        )
        db.log_strategy_event("run", "MARKET_SETTLEMENT", {"slug": slug, "outcome": outcome})

    calibrations = db.load_strong_directional_regime_calibrations(
        lookback_hours=168,
        min_score_abs=0.35,
        min_samples=3,
    )

    calibration = calibrations["10_30"]
    assert calibration["sample_count"] == 3
    assert calibration["wins"] == 2
    assert calibration["win_probability"] == 2 / 3


def test_strong_directional_regime_calibration_selects_first_eligible_60_plus_observation(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    for index in range(3):
        slug = f"btc-updown-60-plus-{index}"
        db.log_strategy_event(
            "run", "LIVE_SIGNAL_COMPARE",
            {
                "slug": slug,
                "main_candidate_side": "BUY_UP",
                "main_score": 0.50,
                "main_side_locked": True,
                "spot_minus_strike": 80,
                "time_left_sec": 700,
            },
        )
        db.log_strategy_event(
            "run", "LIVE_SIGNAL_COMPARE",
            {
                "slug": slug,
                "main_candidate_side": "BUY_UP",
                "main_score": 0.50,
                "main_side_locked": True,
                "spot_minus_strike": 80,
                "time_left_sec": 480,
            },
        )
        db.log_strategy_event("run", "MARKET_SETTLEMENT", {"slug": slug, "outcome": "UP"})

    calibrations = db.load_strong_directional_regime_calibrations(
        lookback_hours=168,
        min_score_abs=0.35,
        min_samples=3,
    )

    calibration = calibrations["60_plus"]
    assert calibration["sample_count"] == 3
    assert calibration["wins"] == 3


def test_reconcile_redeem_cycle_rebuilds_missing_pnl_with_buy_fees(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    slug = "btc-updown-15m-test"
    _fill(db, slug=slug, order_id="buy-down", side="BUY", price=0.72, qty=5.4, fee=0.02)
    _fill(db, slug=slug, order_id="sell-down", side="SELL", price=0.38, qty=5.3, fee=0.03)
    _fill(db, slug=slug, order_id="buy-up", side="BUY", price=0.73, qty=5.4)

    reconciled = db.reconcile_redeem_cycle(slug, 5.4)

    assert reconciled is not None
    assert round(reconciled["buy_cost_usdc"], 3) == 7.85
    assert round(reconciled["sell_proceeds_usdc"], 3) == 1.984
    assert round(reconciled["cycle_combined_pnl_usdc"], 3) == -0.466
    db.log_strategy_event(
        "run",
        "MARKET_CYCLE_PNL",
        {"slug": slug, "cycle_combined_pnl_usdc": reconciled["cycle_combined_pnl_usdc"]},
    )
    assert db.reconcile_redeem_cycle(slug, 5.4)["wrote_cycle_pnl"] is False


def test_reconcile_redeem_cycle_updates_existing_cycle_without_duplicate(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    slug = "btc-updown-15m-existing"
    _fill(db, slug=slug, order_id="buy", side="BUY", price=0.70, qty=10, fee=0.10)
    db.log_strategy_event(
        "run",
        "MARKET_SETTLEMENT",
        {"slug": slug, "inventory_shares": 9.9, "inventory_cost_usdc": 7.1, "redeem_value_usdc": 0.0},
    )
    db.log_strategy_event(
        "run",
        "MARKET_CYCLE_PNL",
        {"slug": slug, "cycle_combined_pnl_usdc": -7.0},
    )

    reconciled = db.reconcile_redeem_cycle(slug, 9.9, tx_hash="0xtx", condition_id="0xcondition")

    assert reconciled is not None
    assert reconciled["wrote_cycle_pnl"] is False
    assert round(reconciled["cycle_combined_pnl_usdc"], 6) == 2.8
    with db._connect() as conn:
        pnl_rows = conn.execute(
            "SELECT payload_json FROM strategy_events WHERE event_type='MARKET_CYCLE_PNL'"
        ).fetchall()
        settlement = conn.execute(
            "SELECT payload_json FROM strategy_events WHERE event_type='MARKET_SETTLEMENT'"
        ).fetchone()
    assert len(pnl_rows) == 1
    assert '"cycle_pnl_reconciled_source": "onchain_redeem"' in pnl_rows[0][0]
    assert '"redeem_value_usdc": 9.9' in settlement[0]
