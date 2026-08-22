from decimal import Decimal

from bot import wallet_ops


class _FailingBalanceClient:
    def __init__(self) -> None:
        self.calls = 0

    def get_balance_allowance(self, _params):
        self.calls += 1
        raise RuntimeError("PolyApiException[status_code=500, error_message=OOM]")


def test_conditional_balance_failure_uses_per_token_backoff_even_when_forced(monkeypatch) -> None:
    now = [1_000.0]
    monkeypatch.setattr(wallet_ops.time, "time", lambda: now[0])
    client = _FailingBalanceClient()
    logs: list[str] = []

    _, balance, cached = wallet_ops.fetch_conditional_balance(
        token="token-1",
        current_client=client,
        cached_entry={"ts": 0.0, "balance": "5.5"},
        conditional_balance_check_interval_sec=8.0,
        force_refresh=True,
        logger_debug_fn=logs.append,
    )

    assert balance == Decimal("5.5")
    assert client.calls == 1
    assert cached is not None
    assert cached["failure_count"] == 1
    assert cached["retry_after_ts"] == 1_008.0
    assert "retry suppressed for 8s" in logs[-1]

    now[0] = 1_001.0
    _, balance, cached_after_backoff = wallet_ops.fetch_conditional_balance(
        token="token-1",
        current_client=client,
        cached_entry=cached,
        conditional_balance_check_interval_sec=8.0,
        force_refresh=True,
        logger_debug_fn=logs.append,
    )

    assert balance == Decimal("5.5")
    assert cached_after_backoff == cached
    assert client.calls == 1
    assert len(logs) == 1


def test_conditional_balance_failure_without_cached_balance_stays_safe(monkeypatch) -> None:
    monkeypatch.setattr(wallet_ops.time, "time", lambda: 1_000.0)
    client = _FailingBalanceClient()

    _, balance, cached = wallet_ops.fetch_conditional_balance(
        token="token-2",
        current_client=client,
        cached_entry=None,
        conditional_balance_check_interval_sec=8.0,
        force_refresh=False,
        logger_debug_fn=lambda _message: None,
    )

    assert balance is None
    assert cached is not None
    assert cached["failure_count"] == 1
