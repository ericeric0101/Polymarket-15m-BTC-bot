from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.db_runtime import StrategyDBRuntimeMixin
from monitoring.trade_journal_db import TradeJournalDB


class _GuardHost(StrategyDBRuntimeMixin):
    """Small integration host: the same runtime persistence boundary as live."""
    session_pnl_guard_enabled = True
    session_profit_arm_usdc = 8
    session_profit_drawdown_usdc = 4
    session_hard_profit_lock_enabled = False
    session_hard_profit_lock_usdc = 10
    session_max_loss_enabled = True
    session_max_loss_usdc = 6

    def __init__(self, db):
        self.trade_db = db
        self.events, self.block_reasons = [], []

    def _db_strategy_event(self, event_type, payload):
        self.events.append((event_type, payload))

    def _block_new_buys_for_trade_db(self, reason):
        self.block_reasons.append(reason)


def _taipei_ts(day: int) -> float:
    return datetime(2026, 9, day, 20, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()


def _taipei_at(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 9, day, hour, minute, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()


def test_session_key_uses_taipei_overnight_window():
    assert StrategyDBRuntimeMixin._taipei_session_date(_taipei_at(30, 19, 29)) == "2026-09-30-day"
    assert StrategyDBRuntimeMixin._taipei_session_date(_taipei_at(30, 19, 30)) == "2026-09-30"
    next_day_midnight = datetime(2026, 10, 1, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    next_day_before_end = datetime(2026, 10, 1, 7, 29, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    next_day_end = datetime(2026, 10, 1, 7, 30, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    assert StrategyDBRuntimeMixin._taipei_session_date(next_day_midnight) == "2026-09-30"
    assert StrategyDBRuntimeMixin._taipei_session_date(next_day_before_end) == "2026-09-30"
    assert StrategyDBRuntimeMixin._taipei_session_date(next_day_end) == "2026-10-01-day"


def test_overnight_reconstruction_includes_only_1930_to_next_day_0730(tmp_path):
    db = TradeJournalDB(str(tmp_path / "journal.db"), backup_interval_sec=3600)
    try:
        for timestamp, pnl in [
            (_taipei_at(30, 19, 29), 100),
            (_taipei_at(30, 19, 30), 5),
            (datetime(2026, 10, 1, 7, 29, tzinfo=ZoneInfo("Asia/Taipei")).timestamp(), 4),
            (datetime(2026, 10, 1, 7, 30, tzinfo=ZoneInfo("Asia/Taipei")).timestamp(), 200),
        ]:
            assert db.log_strategy_event(
                "run", "MARKET_CYCLE_PNL", {"cycle_combined_pnl_usdc": pnl, "test_ts": timestamp}
            )
            # Use an explicit ISO timestamp to exercise the same timestamp source
            # the journal reconstruction consumes.
            with db._connect() as conn:
                conn.execute(
                    "UPDATE strategy_events SET ts=? WHERE id=(SELECT MAX(id) FROM strategy_events)",
                    (datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),),
                )
                conn.commit()
        state = db.reconstruct_session_pnl_state("2026-09-30")
        assert state["realized_pnl_usdc"] == 9
        assert state["realized_high_water_usdc"] == 9
    finally:
        db.stop()


def test_session_pnl_state_is_durable_and_does_not_require_strategy_event(tmp_path):
    path = tmp_path / "journal.db"
    db = TradeJournalDB(str(path), backup_interval_sec=3600)
    try:
        assert db.save_session_pnl_state({
            "session_date_taipei": "2026-09-28", "realized_pnl_usdc": "8.25",
            "realized_high_water_usdc": "9.0", "profit_guard_armed": True,
            "buy_lock_active": True, "buy_lock_reason": "session_profit_drawdown_lock",
        })
    finally:
        db.stop()
    reopened = TradeJournalDB(str(path), backup_interval_sec=3600)
    try:
        state = reopened.load_session_pnl_state("2026-09-28")
        assert state is not None
        assert state["buy_lock_active"] == 1
        assert state["buy_lock_reason"] == "session_profit_drawdown_lock"
    finally:
        reopened.stop()


def test_session_pnl_reconstruction_uses_completed_cycle_events(tmp_path):
    db = TradeJournalDB(str(tmp_path / "journal.db"), backup_interval_sec=3600)
    try:
        assert db.log_strategy_event("run", "MARKET_CYCLE_PNL", {"cycle_combined_pnl_usdc": 9.0})
        state = db.reconstruct_session_pnl_state("2099-01-01")
        # The current UTC day will not match an arbitrary future day.
        assert state["realized_pnl_usdc"] == 0
    finally:
        db.stop()


def test_session_guard_end_to_end_persists_lock_and_resets_next_taipei_day(tmp_path):
    """BUY boundaries see the durable lock; SELL/redeem never consult it."""
    db = TradeJournalDB(str(tmp_path / "journal.db"), backup_interval_sec=3600)
    try:
        host = _GuardHost(db)
        host._initialize_session_pnl_guard(_taipei_ts(28))
        assert host._record_session_realized_pnl(5, source="sell_fill")
        assert host._record_session_realized_pnl(4, source="sell_fill")
        armed = host.session_buy_guard_decision(_taipei_ts(28))
        assert armed.allowed and armed.armed and armed.realized_high_water_usdc == 9
        assert host._record_session_realized_pnl(-4, source="sell_fill")
        locked = host.session_buy_guard_decision(_taipei_ts(28))
        assert not locked.allowed and locked.reason == "session_profit_drawdown_lock"
        # Maker, fast-follow, and re-entry all call this final new-BUY decision;
        # sell/redeem/cancel do not call it and retain their authority.
        assert host.session_buy_guard_decision(_taipei_ts(28)).allowed is False

        restarted = _GuardHost(db)
        restarted._initialize_session_pnl_guard(_taipei_ts(28))
        after_restart = restarted.session_buy_guard_decision(_taipei_ts(28))
        assert not after_restart.allowed
        assert after_restart.reason == "session_profit_drawdown_lock"
        assert after_restart.realized_pnl_usdc == 5 and after_restart.realized_high_water_usdc == 9

        next_day = restarted.session_buy_guard_decision(_taipei_ts(29))
        assert next_day.allowed and not next_day.armed
        assert next_day.realized_pnl_usdc == 0 and next_day.realized_high_water_usdc == 0
    finally:
        db.stop()


def test_startup_reconciliation_persists_lock_derived_from_loaded_high_water(tmp_path):
    """A stale durable row must not disagree with the in-memory BUY authority."""
    db = TradeJournalDB(str(tmp_path / "journal.db"), backup_interval_sec=3600)
    try:
        # This models a prior process that persisted PnL/high-water but exited
        # before serialising the derived sticky arm/lock flags.
        assert db.save_session_pnl_state({
            "session_date_taipei": "2026-09-28",
            "realized_pnl_usdc": "1",
            "realized_high_water_usdc": "14",
            "profit_guard_armed": False,
            "buy_lock_active": False,
            "buy_lock_reason": "",
        })

        host = _GuardHost(db)
        host._initialize_session_pnl_guard(_taipei_ts(28))

        decision = host.session_buy_guard_decision(_taipei_ts(28))
        assert not decision.allowed
        assert decision.reason == "session_profit_drawdown_lock"

        stored = db.load_session_pnl_state("2026-09-28")
        assert stored is not None
        assert stored["profit_guard_armed"] == 1
        assert stored["buy_lock_active"] == 1
        assert stored["buy_lock_reason"] == "session_profit_drawdown_lock"
        assert any(event == "SESSION_GUARD_STATE_RECONCILED" for event, _ in host.events)
    finally:
        db.stop()
