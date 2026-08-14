from types import SimpleNamespace

from bot.order_events import handle_order_canceled
from bot.recovery_exit_ladder import select_recovery_exit_action


def test_recovery_exit_prefers_passive_when_time_allows() -> None:
    assert select_recovery_exit_action(
        enabled=True,
        stage=None,
        time_left_sec=300,
        passive_min_time_left_sec=120,
        passive_order_active=False,
        passive_age_sec=0,
        passive_ttl_sec=15,
    ) == "passive_limit"


def test_recovery_exit_escalates_only_after_passive_ttl() -> None:
    common = dict(
        enabled=True,
        stage="passive",
        time_left_sec=300,
        passive_min_time_left_sec=120,
        passive_order_active=True,
        passive_ttl_sec=15,
    )
    assert select_recovery_exit_action(**common, passive_age_sec=14.9) == "wait_passive"
    assert select_recovery_exit_action(**common, passive_age_sec=15) == "cancel_passive"
    assert select_recovery_exit_action(
        **{**common, "passive_order_active": False},
        passive_age_sec=16,
    ) == "limit_fak"


def test_recovery_exit_uses_price_bound_fak_in_tail() -> None:
    assert select_recovery_exit_action(
        enabled=True,
        stage=None,
        time_left_sec=90,
        passive_min_time_left_sec=120,
        passive_order_active=False,
        passive_age_sec=0,
        passive_ttl_sec=15,
    ) == "limit_fak"


def test_disabled_ladder_keeps_legacy_behavior() -> None:
    assert select_recovery_exit_action(
        enabled=False,
        stage=None,
        time_left_sec=300,
        passive_min_time_left_sec=120,
        passive_order_active=False,
        passive_age_sec=0,
        passive_ttl_sec=15,
    ) == "legacy_market"


def test_cancelled_recovery_order_has_terminal_audit_outcome() -> None:
    client_order_id = "BTC-15M-RECOVERY-PASSIVE-1"

    class Strategy:
        current_market_slug = "btc-updown-15m-test"
        terminal_dashboard = None
        active_maker_orders = {}
        _last_cancel_ack_ts_by_client_order_id = {}
        _cancel_ack_dedupe_window_sec = 0.0
        taker_exit_reason_by_client_order_id = {client_order_id: "invalidation_recovery"}
        taker_exit_execution_by_client_order_id = {
            client_order_id: {
                "requested_tif": "GTC",
                "requested_order_kind": "limit",
                "venue_order_type": "GTC",
            },
        }

        def __init__(self):
            self.events = []

        def _clear_pending_taker_exit_for_order(self, order_id):
            self.taker_exit_reason_by_client_order_id.pop(order_id, None)
            self.taker_exit_execution_by_client_order_id.pop(order_id, None)

        def _update_terminal_dashboard_snapshot(self):
            pass

        def _db_strategy_event(self, event_type, payload):
            self.events.append((event_type, payload))

        def _db_order_event(self, **_kwargs):
            pass

    strategy = Strategy()
    handle_order_canceled(
        strategy,
        SimpleNamespace(
            client_order_id=client_order_id,
            venue_order_id=None,
            order_side="SELL",
            instrument_id="token",
        ),
    )

    assert strategy.events == [
        (
            "EXIT_AUDIT_OUTCOME",
            {
                "slug": "btc-updown-15m-test",
                "instrument_id": "token",
                "client_order_id": client_order_id,
                "exit_reason": "invalidation_recovery",
                "outcome": "cancelled",
                "requested_tif": "GTC",
                "requested_order_kind": "limit",
                "venue_order_type": "GTC",
                "cancel_reason": "",
            },
        ),
    ]
