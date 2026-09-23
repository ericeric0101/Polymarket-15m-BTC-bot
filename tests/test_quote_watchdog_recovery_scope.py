from decimal import Decimal
from types import SimpleNamespace

from run_bot import IntegratedBTCStrategy


def test_quote_watchdog_skips_reduce_only_market_without_risk():
    strategy = SimpleNamespace(
        market_phase=SimpleNamespace(value="REDUCE_ONLY"),
        inventory_delta_shares=Decimal("0"),
        active_maker_orders={},
    )

    assert IntegratedBTCStrategy._quote_watchdog_recovery_is_needed(strategy) is False


def test_quote_watchdog_keeps_recovery_for_inventory_or_active_market():
    active = SimpleNamespace(
        market_phase=SimpleNamespace(value="ACTIVE"),
        inventory_delta_shares=Decimal("0"),
        active_maker_orders={},
    )
    held = SimpleNamespace(
        market_phase=SimpleNamespace(value="REDUCE_ONLY"),
        inventory_delta_shares=Decimal("1"),
        active_maker_orders={},
    )

    assert IntegratedBTCStrategy._quote_watchdog_recovery_is_needed(active) is True
    assert IntegratedBTCStrategy._quote_watchdog_recovery_is_needed(held) is True


def test_quote_watchdog_rollover_uses_launcher_node_stop_callback():
    stopped = []
    events = []
    strategy = SimpleNamespace(
        _stopping=False,
        _rollover_requested_flag=False,
        _quote_stream_rollover_requested=False,
        instrument_id="token-up",
        _request_node_stop_callback=lambda: stopped.append(True),
        _db_strategy_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    IntegratedBTCStrategy._request_quote_stream_node_rollover(
        strategy,
        trigger="quote_resubscribe_timeout",
        now_ts=123.0,
    )

    assert stopped == [True]
    assert strategy._stopping is True
    assert strategy._rollover_requested_flag is True
    assert events[0][0] == "QUOTE_WATCHDOG_NODE_ROLLOVER"


def test_quote_watchdog_stop_failure_does_not_leave_strategy_stuck_stopping():
    def fail_to_stop():
        raise RuntimeError("node loop unavailable")

    strategy = SimpleNamespace(
        _stopping=False,
        _rollover_requested_flag=False,
        _quote_stream_rollover_requested=False,
        instrument_id="token-up",
        _request_node_stop_callback=fail_to_stop,
        _db_strategy_event=lambda *_args: None,
    )

    IntegratedBTCStrategy._request_quote_stream_node_rollover(
        strategy,
        trigger="quote_resubscribe_timeout",
        now_ts=123.0,
    )

    assert strategy._stopping is False
    assert strategy._rollover_requested_flag is False
    assert strategy._quote_stream_rollover_requested is False
