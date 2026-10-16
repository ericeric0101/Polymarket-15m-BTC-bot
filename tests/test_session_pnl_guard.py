from decimal import Decimal

from bot.session_pnl_guard import SessionPnlGuard, SessionPnlGuardConfig, SessionPnlState


def test_profit_drawdown_lock_is_sticky_until_next_taipei_day():
    guard = SessionPnlGuard(
        SessionPnlGuardConfig(enabled=True, profit_arm_usdc=Decimal("8"), profit_drawdown_usdc=Decimal("4")),
        session_date="2026-09-28",
    )
    guard.apply_realized_delta(Decimal("8"))
    assert guard.decision().allowed is True
    assert guard.state.profit_guard_armed is True

    guard.apply_realized_delta(Decimal("-4"))
    decision = guard.decision()
    assert decision.allowed is False
    assert decision.reason == "session_profit_drawdown_lock"

    guard.apply_realized_delta(Decimal("10"))
    assert guard.decision().allowed is False


def test_max_loss_and_optional_hard_profit_lock():
    config = SessionPnlGuardConfig(
        enabled=True,
        max_loss_enabled=True,
        max_loss_usdc=Decimal("6"),
        hard_profit_lock_enabled=True,
        hard_profit_lock_usdc=Decimal("10"),
    )
    loss_guard = SessionPnlGuard(config, session_date="2026-09-28")
    loss_guard.apply_realized_delta(Decimal("-6"))
    assert loss_guard.decision().reason == "session_max_loss_lock"

    profit_guard = SessionPnlGuard(config, session_date="2026-09-28")
    profit_guard.apply_realized_delta(Decimal("10"))
    assert profit_guard.decision().reason == "session_hard_profit_lock"


def test_disabled_guard_never_blocks():
    guard = SessionPnlGuard(
        SessionPnlGuardConfig(enabled=False, max_loss_enabled=True, max_loss_usdc=Decimal("1")),
        session_date="2026-09-28",
    )
    guard.apply_realized_delta(Decimal("-99"))
    assert guard.decision().allowed is True


def test_restart_reconstruction_arms_from_high_water_not_only_current_pnl():
    state = {
        "session_date_taipei": "2026-09-28", "realized_pnl_usdc": "1",
        "realized_high_water_usdc": "14", "profit_guard_armed": False,
        "buy_lock_active": False, "buy_lock_reason": "",
    }
    guard = SessionPnlGuard(
        SessionPnlGuardConfig(profit_arm_usdc=Decimal("8"), profit_drawdown_usdc=Decimal("4")),
        session_date="2026-09-28", state=SessionPnlState.from_mapping(state),
    )
    assert guard.state.profit_guard_armed is True
    assert guard.decision().reason == "session_profit_drawdown_lock"
