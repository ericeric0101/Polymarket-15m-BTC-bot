from decimal import Decimal
from types import SimpleNamespace
import time

from run_bot import IntegratedBTCStrategy
from bot.launcher import idempotent_stop_callback
from bot.order_runtime import OrderRuntimeMixin
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
        active_maker_orders={},
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
        current_market_instruments=["up", "down"],
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


def test_old_market_pending_sell_does_not_block_watchdog_rollover_after_market_switch():
    stopped, events = [], []

    class Order:
        status = "ACCEPTED"

    strategy = SimpleNamespace(
        _stopping=False, _rollover_requested_flag=False, _quote_stream_rollover_requested=False,
        instrument_id="new-up", current_market_slug="new-market",
        current_market_instruments=["new-up", "new-down"],
        active_maker_orders={
            "sell:old-up": {"side": "sell", "instrument_id": "old-up", "pending_cancel": True,
                            "order": Order()},
        },
        _request_node_stop_callback=lambda: stopped.append(True),
        _db_strategy_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    requested = IntegratedBTCStrategy._request_quote_stream_node_rollover(
        strategy, trigger="quote_resubscribe_timeout", now_ts=123.0)

    assert requested is True
    assert stopped == [True]
    assert any(event == "QUOTE_WATCHDOG_PRIOR_MARKET_SELL_IGNORED" for event, _ in events)
    assert not any(event == "QUOTE_WATCHDOG_ROLLOVER_DEFERRED_PROTECTIVE_SELL" for event, _ in events)


def test_watchdog_timer_reconciles_stale_prior_market_cancel_without_quote_callback():
    events = []

    class Order:
        client_order_id = "old-sell-coid"
        status = "ACCEPTED"

    class StopEvent:
        calls = 0
        def wait(self, _seconds):
            self.calls += 1
            return self.calls > 1

    state = {"side": "sell", "instrument_id": "old-up", "pending_cancel": True,
             "last_cancel_ts": 1.0, "order": Order()}
    active_orders = {"sell:old-up": state}
    class CleanupHost(OrderRuntimeMixin):
        pass
    cleanup_host = CleanupHost()
    cleanup_host.active_maker_orders = active_orders
    cleanup_host.current_market_instruments = ["new-up", "new-down"]
    cleanup_host.maker_cancel_ack_timeout_sec = 5
    cleanup_host.maker_cancel_max_retries = 2
    cleanup_host._db_order_event = lambda **kwargs: events.append(kwargs)
    strategy = SimpleNamespace(
        _stopping=False, _quote_watchdog_stop_event=StopEvent(), quote_healthcheck_interval_sec=1.0,
        quote_recovery_pending_instruments=set(), quote_recovery_started_ts=0.0,
        last_valid_quote_ts=0.0, quote_stale_sec=1.0,
        _emit_strategy_status=lambda _now: None,
        _cleanup_stale_pending_cancels=lambda now: OrderRuntimeMixin._cleanup_stale_pending_cancels(cleanup_host, now),
    )

    IntegratedBTCStrategy._start_quote_watchdog_timer(strategy)

    assert strategy._quote_watchdog_stop_event.calls == 2
    assert active_orders == {}
    assert not any(event.get("event_type") == "ORDER_CANCEL_RECONCILE_UNKNOWN_KILL" for event in events)
    retired = next(event for event in events if event.get("event_type") == "ORDER_CANCEL_PRIOR_MARKET_RETIRED")
    assert retired["payload"]["local_tracker_retired"] is True
    assert retired["payload"]["venue_cancel_confirmed"] is False


def test_watchdog_cleanup_does_not_retry_cancel_before_ack_timeout():
    class StopEvent:
        calls = 0
        def wait(self, _seconds):
            self.calls += 1
            return self.calls > 1

    cancel_calls, reconcile_calls = [], []
    order = SimpleNamespace(client_order_id="current-sell", status="ACCEPTED")
    state = {"side": "sell", "instrument_id": "new-up", "pending_cancel": True,
             "last_cancel_ts": time.time(), "cancel_retries": 0, "order": order}
    class CleanupHost(OrderRuntimeMixin):
        def _db_order_event(self, **_kwargs): pass
        def _is_order_still_open_in_cache(self, coid): reconcile_calls.append(coid); return True
        def cancel_order(self, active_order): cancel_calls.append(active_order)

    host = CleanupHost()
    host.active_maker_orders = {"sell:new-up": state}
    host.current_market_instruments = ["new-up", "new-down"]
    host.maker_cancel_ack_timeout_sec = 30
    host.maker_cancel_max_retries = 2
    host.maker_error_pause_sec = 5
    host.quote_pause_until_ts = 0.0
    strategy = SimpleNamespace(
        _stopping=False, _quote_watchdog_stop_event=StopEvent(), quote_healthcheck_interval_sec=1.0,
        quote_recovery_pending_instruments=set(), quote_recovery_started_ts=0.0,
        last_valid_quote_ts=0.0, quote_stale_sec=1.0,
        _emit_strategy_status=lambda _now: None,
        _cleanup_stale_pending_cancels=lambda now: OrderRuntimeMixin._cleanup_stale_pending_cancels(host, now),
    )

    IntegratedBTCStrategy._start_quote_watchdog_timer(strategy)

    assert cancel_calls == []
    assert reconcile_calls == []
    assert state["pending_cancel"] is True


def test_cross_market_reset_clears_global_quote_display_but_same_slug_preserves_it():
    class PositionManager:
        def clear_all(self): pass

    def strategy(slug):
        return SimpleNamespace(
            inventory_delta_shares=Decimal("0"), live_inventory_cost={},
            _startup_rehydrated_inventory_force_sell_only=False, _inventory_overage_sell_only=False,
            position_manager=PositionManager(), market_cycle_realized_net_usdc=Decimal("0"),
            maker_kill_switch=False, maker_kill_switch_reset_on_rollover=False,
            last_quote_update_ts=123.0, latest_market_bid=Decimal("0.43"),
            latest_market_ask=Decimal("0.44"), latest_market_bid_ts=123.0,
            latest_market_ask_ts=123.0, last_valid_quote_ts=123.0,
            _cancel_active_maker_orders=lambda: None,
        )

    cross = strategy("new-market")
    IntegratedBTCStrategy._reset_maker_state_for_new_market(
        cross, "old-token", "new-token", previous_slug="old-market", current_slug="new-market")
    assert cross.latest_market_bid is None and cross.latest_market_ask is None
    assert cross.latest_market_bid_ts == 0 and cross.latest_market_ask_ts == 0
    assert cross.last_valid_quote_ts == 0

    same = strategy("same-market")
    IntegratedBTCStrategy._reset_maker_state_for_new_market(
        same, "old-token", "new-token", previous_slug="same-market", current_slug="same-market")
    assert same.latest_market_bid == Decimal("0.43")
    assert same.latest_market_ask == Decimal("0.44")
    assert same.last_valid_quote_ts == 123.0


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
