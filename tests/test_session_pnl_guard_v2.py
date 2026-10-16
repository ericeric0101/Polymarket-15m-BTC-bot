from decimal import Decimal

import pytest

from bot.session_pnl_guard import SessionPnlGuard, SessionPnlGuardConfig
from bot.app_config import AppConfig


def _config(monthly="500", risk="10", *, mode="target_scaled_v2"):
    return SessionPnlGuardConfig(
        mode=mode,
        monthly_net_target_usdc=Decimal(monthly),
        per_trade_risk_usdc=Decimal(risk),
    )


def test_v2_decimal_thresholds_for_500_and_1000_monthly_target():
    thresholds = _config().target_scaled_thresholds()
    assert thresholds["normalized_target_usdc"] == Decimal("500") / Decimal("30")
    assert thresholds["profit_arm_usdc"] == Decimal("10")
    assert thresholds["target_profit_drawdown_usdc"] == Decimal("7.50")
    assert thresholds["hard_profit_lock_usdc"] == Decimal("22.500")
    assert thresholds["max_loss_usdc"] == Decimal("15.0")
    larger = _config("1000").target_scaled_thresholds()
    assert larger["profit_arm_usdc"] == Decimal("35") / Decimal("3")
    assert larger["max_loss_usdc"] == Decimal("15.0")


def test_v2_target_protection_uses_tighter_drawdown_and_is_sticky():
    guard = SessionPnlGuard(_config(), session_date="2026-10-03")
    guard.apply_realized_delta("18")
    assert guard.decision().state == "TARGET_PROTECTION"
    assert guard.decision().current_allowed_drawdown_usdc == Decimal("7.50")
    guard.apply_realized_delta("-7.5")
    assert guard.decision().reason == "session_profit_drawdown_lock"
    assert not guard.decision().allowed


def test_v2_hard_profit_and_loss_locks_and_shadow_never_vetoes():
    profit = SessionPnlGuard(_config(), session_date="2026-10-03")
    profit.apply_realized_delta("22.5")
    assert profit.decision().reason == "session_hard_profit_lock"
    loss = SessionPnlGuard(_config(), session_date="2026-10-03")
    loss.apply_realized_delta("-15")
    assert loss.decision().reason == "session_max_loss_lock"
    shadow = SessionPnlGuard(_config(mode="shadow_target_scaled_v2"), session_date="2026-10-03")
    shadow.apply_realized_delta("-15")
    assert shadow.decision().allowed
    assert shadow.decision().shadow_would_block


@pytest.mark.parametrize("monthly,risk", [("0", "10"), ("500", "0"), ("-1", "10")])
def test_v2_rejects_non_positive_inputs(monthly, risk):
    with pytest.raises(ValueError):
        _config(monthly, risk).target_scaled_thresholds()


def test_v2_environment_requires_positive_monthly_target(monkeypatch):
    monkeypatch.setenv("SESSION_PNL_GUARD_MODE", "target_scaled_v2")
    monkeypatch.delenv("MONTHLY_NET_TARGET_USDC", raising=False)
    with pytest.raises(ValueError, match="MONTHLY_NET_TARGET_USDC"):
        AppConfig.from_env(enable_terminal_dashboard=False)
    monkeypatch.setenv("MONTHLY_NET_TARGET_USDC", "500")
    config = AppConfig.from_env(enable_terminal_dashboard=False)
    assert config.operations.session_pnl_guard_mode == "target_scaled_v2"
    assert config.operations.monthly_net_target_usdc == Decimal("500")
