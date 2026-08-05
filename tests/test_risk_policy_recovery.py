from bot.risk_policy import FillCooldownConfig, FillCooldownPolicy


def test_loss_pause_preserves_recent_history_for_regime_awareness():
    policy = FillCooldownPolicy(
        FillCooldownConfig(
            post_fill_buy_cooldown_sec=0,
            max_consecutive_losses=3,
            loss_pause_sec=30,
        )
    )

    updated, pause_until, triggered, total_loss = policy.register_realized_pnl(
        recent_fill_pnl_results=[-0.10, -0.08],
        realized_net_usdc=-0.06,
        now_ts=100.0,
        current_quote_pause_until_ts=0.0,
    )

    assert triggered is True
    assert pause_until == 130.0
    assert total_loss == -0.24
    assert updated == [-0.10, -0.08, -0.06]


def test_loss_recovery_controls_are_conservative():
    policy = FillCooldownPolicy(
        FillCooldownConfig(
            post_fill_buy_cooldown_sec=0,
            max_consecutive_losses=3,
            loss_pause_sec=30,
        )
    )

    assert policy.recovery_size_multiplier([]) == 1.0
    assert policy.recovery_size_multiplier([-0.1]) == 0.75
    assert policy.recovery_size_multiplier([-0.1, -0.1]) == 0.50
    assert policy.recovery_min_edge_addition([-0.1]) == 0.01
    assert policy.recovery_min_edge_addition([-0.1, -0.1]) == 0.02
