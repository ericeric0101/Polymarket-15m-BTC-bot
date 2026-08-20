from alert_watcher import (
    ALERT_CONSECUTIVE_LOSSES,
    ALERT_HEARTBEAT_STALE_SEC,
    ALERT_LARGE_LOSS_USD,
    ALERT_LOW_BALANCE_USD,
    AlertWatcher,
)


def test_alert_watcher_uses_fixed_operational_defaults() -> None:
    watcher = AlertWatcher()

    assert watcher.consecutive_losses == ALERT_CONSECUTIVE_LOSSES == 3
    assert watcher.large_loss_usd == ALERT_LARGE_LOSS_USD == 7.0
    assert watcher.low_balance_usd == ALERT_LOW_BALANCE_USD == 20.0
    assert watcher.heartbeat_stale_sec == ALERT_HEARTBEAT_STALE_SEC == 300
