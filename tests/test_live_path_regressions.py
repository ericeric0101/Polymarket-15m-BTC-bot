from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from bot.enums import ActiveSide
from bot.fill_ledger import FillLedgerMixin
from bot.taker_exit import TakerExitMixin
from run_bot import IntegratedBTCStrategy


class DummyOrder:
    def __init__(self, client_order_id: str) -> None:
        self.client_order_id = client_order_id


class DummyStrategyForFill(FillLedgerMixin):
    def __init__(self) -> None:
        self.active_maker_orders = {
            "buy:inst-1": {
                "side": "buy",
                "order": DummyOrder("BUY-1"),
                "instrument_id": "inst-1",
                "quantity": Decimal("5.2"),
                "filled_qty": Decimal("0"),
                "econ": None,
                "directional_snapshot": {},
            }
        }
        self._inventory_delta_shares = Decimal("0")
        self.inventory_last_update_ts = 0.0
        self.instrument_id = "inst-1"
        self.taker_exit_reason_by_client_order_id = {}
        self.maker_high_cost_exit_cooldown_enabled = False
        self.maker_high_cost_exit_cooldown_sec = 0
        self.maker_high_cost_fill_threshold = Decimal("0.75")
        self.high_cost_exit_cooldown_until_by_inst = {}
        self.high_cost_last_fill_price_by_inst = {}
        self.stop_loss_reentry_cooldown_sec = 0
        self.market_stop_loss_count_by_slug = {}
        self.current_market_slug = "btc-updown-15m-test"
        self.side_stop_loss_penalty_until_by_market_side = {}
        self.active_side = ActiveSide.NONE
        self.active_side_locked = False
        self.side_pending_flip_side = ActiveSide.NONE
        self.side_pending_flip_count = 0
        self.side_decision_due_ts = 0.0
        self.side_decision_reason = ""
        self.market_buy_count_by_slug = {}
        self.market_buy_counted_order_ids_by_slug = {}
        self.market_max_buy_events_per_market = 2
        self.consecutive_denied_orders = 0
        self.last_quote_update_ts = 123.0
        self.last_observed_fee_rate_bps = None
        self.rebate_reporter = SimpleNamespace(
            record_fill=lambda **kwargs: None,
            flush_daily_report=lambda: None,
        )
        self.terminal_dashboard = None
        self.market_cycle_realized_net_usdc = Decimal("0")
        self.recent_fill_pnl_results = []
        self.quote_pause_until_ts = 0.0
        self.post_fill_buy_cooldown_sec = 0.0
        self.buy_cooldown_until_ts = 0.0
        self.max_consecutive_losses = 3
        self.loss_pause_sec = 60.0
        self.fill_cooldown_policy = SimpleNamespace(
            next_buy_cooldown_until=lambda now_ts: now_ts,
            register_realized_pnl=lambda **kwargs: (
                kwargs["recent_fill_pnl_results"],
                kwargs["current_quote_pause_until_ts"],
                False,
                0.0,
            ),
        )
        self._stopping = True
        self.maker_mode = True
        self.maker_kill_switch = False
        self.latest_market_bid = None
        self.latest_market_ask = None
        self.live_inventory_fill_calls = []
        self.strategy_events = []
        self.order_events = []
        self.order_metric_count = 0
        self.inventory_metric_count = 0

    @property
    def inventory_delta_shares(self) -> Decimal:
        return self._inventory_delta_shares

    @inventory_delta_shares.setter
    def inventory_delta_shares(self, value: Decimal) -> None:
        self._inventory_delta_shares = value

    def _normalize_side_text(self, side_val):
        return IntegratedBTCStrategy._normalize_side_text(side_val)

    def _is_maker_fill_liquidity(self, liquidity_side):
        return IntegratedBTCStrategy._is_maker_fill_liquidity(liquidity_side)

    def _instrument_key(self, instrument_id):
        return str(instrument_id) if instrument_id is not None else ""

    def _update_live_inventory_cost_from_fill(self, **kwargs):
        self.live_inventory_fill_calls.append(kwargs)
        return Decimal("0")

    def _clear_pending_taker_exit_for_order(self, *_args, **_kwargs):
        return None

    def _side_for_instrument_id(self, _instrument_id):
        return ActiveSide.NONE

    def _sync_active_instrument(self):
        return None

    def _cancel_maker_order_side(self, **_kwargs):
        return None

    def _db_strategy_event(self, event_type, payload=None):
        self.strategy_events.append((event_type, payload or {}))

    def _db_order_event(self, **kwargs):
        self.order_events.append(kwargs)

    def _update_terminal_dashboard_snapshot(self):
        return None

    def _increment_order_metric(self, *_args, **_kwargs):
        self.order_metric_count += 1

    def _update_inventory_metric(self):
        self.inventory_metric_count += 1

    def _start_maker_worker(self, *_args, **_kwargs):
        return None


class DummyStrategyForRehydrate:
    def __init__(self) -> None:
        self.live_inventory_cost = {}
        self._inventory_delta_shares = Decimal("0")
        self.inventory_last_update_ts = 0.0
        self.current_market_instruments = ["inst-up", "inst-down"]
        self.instrument_id = "inst-up"
        self.current_market_slug = "btc-updown-15m-test"
        self._startup_rehydrated_inventory_force_sell_only = False
        self.strategy_events = []
        self.sellable_qty_by_inst = {
            "inst-up": Decimal("5.2"),
            "inst-down": Decimal("0"),
        }

    @property
    def inventory_delta_shares(self) -> Decimal:
        return self._inventory_delta_shares

    @inventory_delta_shares.setter
    def inventory_delta_shares(self, value: Decimal) -> None:
        self._inventory_delta_shares = value

    def _normalize_instrument_id(self, instrument_id):
        return instrument_id

    def _instrument_key(self, instrument_id):
        return str(instrument_id) if instrument_id is not None else ""

    def _get_sellable_qty_for_current_instrument(self, instrument_id=None):
        return self.sellable_qty_by_inst.get(str(instrument_id), Decimal("0"))

    def _rebuild_inventory_state_from_db(self, instrument_id, target_qty, lookback_hours=72):
        if str(instrument_id) != "inst-up":
            return None
        return {
            "qty": target_qty,
            "avg_entry_price": Decimal("0.37"),
            "entry_fee_remaining": Decimal("0"),
            "opened_ts": 1000.0,
        }

    def _db_strategy_event(self, event_type, payload=None):
        self.strategy_events.append((event_type, payload or {}))


class DummyUrgentExitStrategy(TakerExitMixin):
    def __init__(self) -> None:
        self.maker_urgent_exit_enabled = True
        self._maker_urgent_exit_last_ts = 0.0
        self.maker_urgent_exit_cooldown_sec = 0
        self.current_market_end_timestamp = 1_000_000.0
        self.live_inventory_cost = {
            "inst-up": {
                "qty": Decimal("5.2"),
                "avg_entry_price": Decimal("0.37"),
            }
        }
        self.active_side = ActiveSide.DOWN
        self.active_side_locked = True
        self.pending_taker_exit_by_inst = {}
        self.active_maker_orders = {
            "sell:inst-up": {
                "price": Decimal("0.50"),
                "created_ts": 96.0,
                "is_urgent_exit": True,
                "urgent_exit_ttl": 15,
            }
        }
        self.cache = SimpleNamespace(instrument=lambda _inst: None)
        self.order_factory = SimpleNamespace(limit=lambda **kwargs: kwargs)
        self._urgent_exit_confirm_hits = {}
        self.maker_urgent_exit_min_confirmations = 1
        self.maker_urgent_exit_min_loss_usdc = Decimal("0.10")
        self.side_decision_score = Decimal("-2")
        self.cancel_calls = []
        self.submit_calls = []
        self.db_events = []

    def _maker_quote_instruments(self):
        return []

    def _instrument_key(self, instrument_id):
        return str(instrument_id) if instrument_id is not None else ""

    def _normalize_instrument_id(self, instrument_id):
        return instrument_id

    def _instrument_for_side(self, side):
        return "inst-down" if side == ActiveSide.DOWN else "inst-up"

    def _get_quote_for_instrument(self, instrument_id):
        assert instrument_id == "inst-up"
        return Decimal("0.20"), Decimal("0.21")

    def _order_key_for(self, side, instrument_id):
        return f"{side}:{instrument_id}"

    def _cancel_maker_order_side(self, side, reason, instrument_id=None):
        self.cancel_calls.append((side, reason, instrument_id))

    def _get_effective_sellable_qty(self, instrument_id=None):
        return Decimal("5.2")

    def submit_order(self, order):
        self.submit_calls.append(order)

    def _db_order_event(self, **kwargs):
        self.db_events.append(kwargs)


def test_partial_fills_only_increment_market_buy_count_once():
    strategy = DummyStrategyForFill()

    first_fill = SimpleNamespace(
        client_order_id="BUY-1",
        last_px=0.37,
        last_qty=5.0,
        commission=0.0,
        liquidity_side="MAKER",
        order_side="BUY",
        instrument_id="inst-1",
    )
    second_fill = SimpleNamespace(
        client_order_id="BUY-1",
        last_px=0.37,
        last_qty=0.2,
        commission=0.0,
        liquidity_side="MAKER",
        order_side="BUY",
        instrument_id="inst-1",
    )

    IntegratedBTCStrategy.on_order_filled(strategy, first_fill)
    IntegratedBTCStrategy.on_order_filled(strategy, second_fill)

    assert strategy.market_buy_count_by_slug["btc-updown-15m-test"] == 1
    assert strategy.market_buy_counted_order_ids_by_slug["btc-updown-15m-test"] == {"BUY-1"}
    count_events = [evt for evt, _ in strategy.strategy_events if evt == "MARKET_BUY_COUNT_UPDATED"]
    assert count_events == ["MARKET_BUY_COUNT_UPDATED"]


def test_startup_rehydrate_restores_inventory_and_forces_sell_only():
    strategy = DummyStrategyForRehydrate()

    IntegratedBTCStrategy._rehydrate_inventory_state_on_startup(strategy)

    assert strategy.inventory_delta_shares == Decimal("5.2")
    assert strategy._startup_rehydrated_inventory_force_sell_only is True
    assert strategy.live_inventory_cost["inst-up"]["avg_entry_price"] == Decimal("0.37")
    assert strategy.strategy_events[0][0] == "STARTUP_INVENTORY_REHYDRATED"


def test_urgent_exit_does_not_replace_recent_urgent_sell_too_quickly():
    strategy = DummyUrgentExitStrategy()

    asyncio.run(strategy._maybe_maker_urgent_exit(now_ts=100.0))

    assert strategy.cancel_calls == []
    assert strategy.submit_calls == []
    assert strategy.db_events == []
