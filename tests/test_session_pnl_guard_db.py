from monitoring.trade_journal_db import TradeJournalDB


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
