from decimal import Decimal
from types import SimpleNamespace

from run_bot import IntegratedBTCStrategy
from bot.launcher import idempotent_stop_callback
from concurrent.futures import ThreadPoolExecutor


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


def test_quote_watchdog_recovery_cancels_buys_but_preserves_protective_sells():
    canceled = []
    strategy = SimpleNamespace(
        active_maker_orders={
            "buy:up": {"side": "buy", "instrument_id": "up"},
            "sell:up": {"side": "sell", "instrument_id": "up"},
        },
        _cancel_maker_order_side=lambda key, reason: canceled.append((key, reason)),
    )

    IntegratedBTCStrategy._cancel_maker_buys_for_quote_recovery(strategy)

    assert canceled == [("buy:up", "quote_watchdog_recovery")]


def test_quote_watchdog_defers_node_rollover_while_protective_sell_is_live():
    stopped = []
    events = []

    class Order:
        status = "ACCEPTED"

    strategy = SimpleNamespace(
        _stopping=False,
        _rollover_requested_flag=False,
        _quote_stream_rollover_requested=False,
        instrument_id="up",
        inventory_delta_shares=Decimal("10"),
        active_maker_orders={
            "sell:up": {
                "side": "sell",
                "instrument_id": "up",
                "order": Order(),
                "directional_snapshot": {"tail_protect_tp": True},
            },
        },
        _request_node_stop_callback=lambda: stopped.append(True),
        _db_strategy_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    IntegratedBTCStrategy._request_quote_stream_node_rollover(
        strategy,
        trigger="quote_resubscribe_timeout",
        now_ts=123.0,
    )

    assert stopped == []
    assert strategy._stopping is False
    assert strategy._rollover_requested_flag is False
    assert events[-1][0] == "QUOTE_WATCHDOG_ROLLOVER_DEFERRED_PROTECTIVE_SELL"


def test_quote_watchdog_allows_node_rollover_after_protective_sell_is_terminal():
    stopped = []

    class Order:
        status = "FILLED"

    strategy = SimpleNamespace(
        _stopping=False,
        _rollover_requested_flag=False,
        _quote_stream_rollover_requested=False,
        instrument_id="up",
        active_maker_orders={
            "sell:up": {"side": "sell", "instrument_id": "up", "order": Order()},
        },
        _request_node_stop_callback=lambda: stopped.append(True),
        _db_strategy_event=lambda *_args: None,
    )

    requested = IntegratedBTCStrategy._request_quote_stream_node_rollover(
        strategy,
        trigger="quote_resubscribe_timeout",
        now_ts=123.0,
    )

    assert requested is True
    assert stopped == [True]


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


def test_watchdog_recovery_does_not_resubscribe_during_shutdown():
    strategy = SimpleNamespace(_stopping=True)
    IntegratedBTCStrategy._trigger_quote_watchdog_reload(strategy, "timer_stale_quotes", 10.0)


def test_node_stop_callback_is_idempotent_across_concurrent_rollover_requests():
    stops = []
    request_stop = idempotent_stop_callback(lambda: stops.append("stop"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: request_stop(), range(32)))
    assert sum(results) == 1
    assert stops == ["stop"]
