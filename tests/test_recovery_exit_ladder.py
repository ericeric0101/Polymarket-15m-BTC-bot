from types import SimpleNamespace
from decimal import Decimal

from bot.order_events import handle_order_canceled
from bot.recovery_exit_ladder import recovery_exit_owns_sell_reservation, select_recovery_exit_action
from bot.taker_exit import TakerExitMixin


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


def test_recovery_exit_owns_sell_reservation_through_handoff_and_ladder() -> None:
    assert recovery_exit_owns_sell_reservation("awaiting_existing_sell_cancel")
    assert recovery_exit_owns_sell_reservation("passive")
    assert recovery_exit_owns_sell_reservation("awaiting_passive_cancel")
    assert recovery_exit_owns_sell_reservation("aggressive")
    assert not recovery_exit_owns_sell_reservation(None)


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


def test_recovery_ladder_cancels_existing_sell_before_submitting_replacement() -> None:
    instrument_id = "up-token"
    existing_order = SimpleNamespace(client_order_id="BTC-15M-MAKER-SELL-existing")

    class Strategy(TakerExitMixin):
        current_market_slug = "btc-updown-15m-test"
        recovery_exit_ladder_enabled = True
        recovery_exit_passive_min_time_left_sec = 120
        recovery_exit_passive_ttl_sec = 15

        def __init__(self) -> None:
            self.recovery_exit_stage_by_inst = {}
            self.active_maker_orders = {
                "sell:up-token": {
                    "order": existing_order,
                    "side": "sell",
                    "instrument_id": instrument_id,
                    "pending_cancel": False,
                },
            }
            self.cancel_calls = []
            self.events = []

        def _normalize_instrument_id(self, value):
            return value

        def _instrument_key(self, value):
            return str(value)

        def _order_key_for(self, side, value):
            return f"{side}:{value}"

        def _cancel_maker_order_side(self, side, reason="", instrument_id=None):
            self.cancel_calls.append((side, reason, instrument_id))
            self.active_maker_orders[f"{side}:{instrument_id}"]["pending_cancel"] = True

        def _db_strategy_event(self, event_type, payload):
            self.events.append((event_type, payload))

    strategy = Strategy()
    submitted = strategy._submit_invalidation_recovery_ladder(
        instrument_id=instrument_id,
        quantity=Decimal("5.5"),
        est_net_if_exit=Decimal("1"),
        best_bid=Decimal("0.42"),
        best_ask=Decimal("0.43"),
        fee_rate=Decimal("0"),
        time_left_sec=300,
        decision_payload={},
    )

    assert submitted is True
    assert strategy.cancel_calls == [
        ("sell", "recovery_exit_replace_existing_sell", instrument_id),
    ]
    assert strategy.active_maker_orders["sell:up-token"]["order"] is existing_order
    assert strategy.events == [
        (
            "EXIT_AUDIT_OUTCOME",
            {
                "slug": "btc-updown-15m-test",
                "instrument_id": instrument_id,
                "exit_reason": "invalidation_recovery",
                "outcome": "awaiting_existing_sell_cancel",
                "existing_client_order_id": "BTC-15M-MAKER-SELL-existing",
            },
        ),
    ]
