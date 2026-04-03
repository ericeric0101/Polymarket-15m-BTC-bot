from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from bot.enums import ActiveSide, MarketPhase
from bot.fill_ledger import FillLedgerMixin
from bot.spot_pricer import SpotPricerMixin
from bot.side_decision import SideDecisionMixin
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
        self._sell_recovery_required_by_inst = {}
        self._sell_recovery_reason_by_inst = {}
        self._sell_recovery_venue_cap_by_inst = {}
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
        self.maker_profit_run_peak_bid_by_inst = {}
        self.maker_profit_run_peak_fair_by_inst = {}
        self.recent_buy_fill_ts_by_inst = {}
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


class DummyMakerInstrumentStrategy:
    def __init__(self) -> None:
        self.bi_side_enabled = True
        self.active_side = ActiveSide.DOWN
        self.instrument_id = "inst-primary"
        self.current_up_instrument_id = "inst-up"
        self.current_down_instrument_id = "inst-down"
        self.live_inventory_cost = {
            "inst-up": {"qty": Decimal("5.1")},
        }
        self._sell_recovery_required_by_inst = {"inst-up": 1.0, "inst-recovery": 2.0}

    def _instrument_key(self, instrument_id):
        return str(instrument_id) if instrument_id is not None else ""

    def _normalize_instrument_id(self, instrument_id):
        return instrument_id

    def _primary_instrument_for_market(self):
        return self.current_up_instrument_id or self.current_down_instrument_id or self.instrument_id

    def _instrument_for_side(self, side):
        if side == ActiveSide.UP:
            return self.current_up_instrument_id or self._primary_instrument_for_market()
        if side == ActiveSide.DOWN:
            return self.current_down_instrument_id
        return None


class DummyRejectRecoveryStrategy:
    def __init__(self) -> None:
        self.active_maker_orders = {}
        self.pending_taker_exit_by_inst = {}
        self.taker_exit_reject_cooldown_sec = 0
        self.taker_exit_reject_cooldown_until_by_inst = {}
        self._sell_reject_pause_until_by_inst = {}
        self._sell_recovery_required_by_inst = {}
        self._sell_recovery_reason_by_inst = {}
        self._sell_recovery_venue_cap_by_inst = {}
        self.consecutive_denied_orders = 0
        self.sell_balance_retry_pause_sec = 3.0
        self._force_quote_refresh_once = False
        self._force_quote_refresh_reason = ""
        self.rebate_reporter = SimpleNamespace(record_denied=lambda: None)
        self.order_metric_count = 0
        self.current_market_slug = "btc-updown-15m-test"
        self.order_events = []

    def _clear_pending_taker_exit_for_order(self, *_args, **_kwargs):
        return None

    def _normalize_side_text(self, side_val):
        return IntegratedBTCStrategy._normalize_side_text(side_val)

    def _instrument_key(self, instrument_id):
        return str(instrument_id) if instrument_id is not None else ""

    def _extract_token_id_from_instrument(self, instrument_id):
        return IntegratedBTCStrategy._extract_token_id_from_instrument(str(instrument_id))

    def _extract_venue_balance_shares_from_reject(self, reason):
        return IntegratedBTCStrategy._extract_venue_balance_shares_from_reject(reason)

    def _cancel_maker_order_side(self, *_args, **_kwargs):
        return None

    def _get_conditional_balance_for_token(self, token_id=None, force_refresh=False):
        self.last_conditional_refresh = (token_id, force_refresh)
        return Decimal("0")

    def _increment_order_metric(self, *_args, **_kwargs):
        self.order_metric_count += 1

    def _db_order_event(self, **kwargs):
        self.order_events.append(kwargs)


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


class DummySideFlipStrategy(SideDecisionMixin):
    def __init__(self, *, held_qty: Decimal) -> None:
        self.bi_side_enabled = True
        self.active_side = ActiveSide.UP
        self.active_side_locked = True
        self.bi_side_allow_intramarket_flip = True
        self.side_flip_count = 1
        self.bi_side_flip_max_per_market = 1
        self.bi_side_flip_confirmations = 1
        self.bi_side_flip_min_score_up = Decimal("2")
        self.bi_side_flip_max_score_down = Decimal("-2")
        self.bi_side_flip_min_fair = Decimal("0.60")
        self.bi_side_min_time_left_sec = 180
        self.current_market_end_timestamp = 10_000.0
        self.active_side_locked = True
        self.side_decision_due_ts = 0.0
        self.active_side = ActiveSide.UP
        self.current_market_slug = "btc-updown-15m-test"
        self.market_strike_cache_by_slug = {"btc-updown-15m-test": Decimal("66867")}
        self.market_strike_source_by_slug = {"btc-updown-15m-test": "binance_rest_open"}
        self.current_up_instrument_id = "inst-up"
        self.current_down_instrument_id = "inst-down"
        self.instrument_id = "inst-up"
        self.live_inventory_cost = {
            "inst-up": {"qty": held_qty, "avg_entry_price": Decimal("0.59")},
        }
        self.side_pending_flip_side = ActiveSide.NONE
        self.side_pending_flip_count = 0
        self.side_decision_score = Decimal("2")
        self.side_decision_reason = "strike=1 momentum=1 open_drift=0 regime=0"
        self.side_decision_ts = 0.0
        self.side_decision_done_for_market = True
        self.side_decision_inputs = {}
        self._force_quote_refresh_once = False
        self._force_quote_refresh_reason = ""
        self._last_side_observation_signature = None
        self._last_side_decision_log_ts = 0.0
        self._last_side_decision_log_signature = None
        self._side_decision_skip_log_ts_by_reason = {}
        self.side_decision_skip_log_interval_sec = 1.0
        self.bi_side_decision_log_interval_sec = 1.0
        self.bi_side_reeval_interval_sec = 1.0
        self.strategy_events = []

    def _instrument_key(self, instrument_id):
        return str(instrument_id) if instrument_id is not None else ""

    def _instrument_for_side(self, side):
        if side == ActiveSide.UP:
            return "inst-up"
        if side == ActiveSide.DOWN:
            return "inst-down"
        return None

    def _primary_instrument_for_market(self):
        return "inst-up"

    def _sync_active_instrument(self):
        return None

    def _db_strategy_event(self, event_type, payload=None):
        self.strategy_events.append((event_type, payload or {}))

    async def _get_market_strike_for_instrument(self, _instrument_id):
        return Decimal("66867")

    def _compute_side_decision(self, now_ts):
        return (
            ActiveSide.DOWN,
            Decimal("-2"),
            "strike=-1 momentum=0 open_drift=-1 regime=0",
            {
                "fair_up": 0.01,
                "fair_down": 0.99,
                "strike_signal": -1,
                "momentum_signal": 0,
                "open_drift_signal": -1,
                "regime_signal": 0,
            },
        )


class DummySpotPricerStrategy(SpotPricerMixin):
    def __init__(self) -> None:
        self.market_strike_cache_by_slug = {}
        self.market_strike_source_by_slug = {}
        self.market_start_ts_by_slug = {"btc-updown-15m-test": 1_000}
        self.market_strike_anchor_max_lag_sec = 180
        self.market_strike_anchor_near_sec = 30
        self.market_strike_rest_retry_sec = 60
        self.market_strike_rest_last_try_ts_by_slug = {}
        self.market_strike_last_gamma_validate_ts_by_slug = {}
        self.market_strike_last_gamma_warn_ts_by_slug = {}
        self.market_strike_gamma_validate_interval_sec = 180
        self.market_strike_gamma_warn_abs_usd = Decimal("5")
        self.market_strike_gamma_mismatch_warn_interval_sec = 180
        self.polymarket_chainlink_history = [(1000.1, Decimal("66625.19"))]
        self.polymarket_chainlink_history_max = 1200
        self.external_spot_history = [(1000.1, Decimal("66602.20"))]
        self.external_spot_history_max = 1200
        self.latest_external_spot = Decimal("66630.00")
        self.latest_external_spot_source = "polymarket_chainlink_ws"
        self.latest_external_spot_source_ts = 1000.2
        self.current_market_slug = "btc-updown-15m-test"
        self._logged_first_spot = False
        self.cache = SimpleNamespace(instrument=lambda _inst: SimpleNamespace(info={"question": ""}))

    def _normalize_instrument_id(self, instrument_id):
        return instrument_id

    def _extract_market_slug_from_instrument(self, _instrument):
        return "btc-updown-15m-test"

    async def _maybe_validate_strike_with_gamma(self, slug, local_strike):
        return None

    def _extract_strike_from_question(self, question_text):
        return None

    def _fetch_binance_open_price_sync(self, start_ts):
        return Decimal("66602.20")


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


def test_maker_quote_instruments_include_held_and_recovery_legs():
    strategy = DummyMakerInstrumentStrategy()

    instruments = IntegratedBTCStrategy._maker_quote_instruments(strategy)

    assert instruments == ["inst-down", "inst-up", "inst-recovery"]


def test_balance_reject_marks_sell_recovery_and_caps_qty():
    strategy = DummyRejectRecoveryStrategy()
    event = SimpleNamespace(
        client_order_id="BTC-15M-MAKER-SELL-1",
        reason="PolyApiException[status_code=400, error_message={'error': 'not enough balance / allowance: the balance is not enough -> balance: 4997830, order amount: 5100000'}]",
        instrument_id="cond-123-456.POLYMARKET",
        order_side="SELL",
        venue_order_id=None,
    )

    IntegratedBTCStrategy._handle_order_rejection_like_event(strategy, event, title="ORDER REJECTED")

    assert strategy._sell_recovery_required_by_inst["cond-123-456.POLYMARKET"] > 0
    assert strategy._sell_recovery_venue_cap_by_inst["cond-123-456.POLYMARKET"] == Decimal("4.99783")
    assert strategy._force_quote_refresh_once is True
    assert strategy._force_quote_refresh_reason == "sell_recovery_balance_reject"


def test_taker_buy_fill_updates_inventory_delta_with_net_shares():
    strategy = DummyStrategyForFill()

    fill = SimpleNamespace(
        client_order_id="BUY-1",
        last_px=0.46,
        last_qty=5.2,
        commission=0.2392,
        liquidity_side="TAKER",
        order_side="BUY",
        instrument_id="inst-1",
        venue_order_id=None,
    )

    IntegratedBTCStrategy.on_order_filled(strategy, fill)

    assert strategy.inventory_delta_shares == Decimal("5.10699904")


def test_urgent_exit_does_not_replace_recent_urgent_sell_too_quickly():
    strategy = DummyUrgentExitStrategy()

    asyncio.run(strategy._maybe_maker_urgent_exit(now_ts=100.0))

    assert strategy.cancel_calls == []
    assert strategy.submit_calls == []
    assert strategy.db_events == []


def test_held_inventory_allows_one_extra_flip_after_quota_exhausted():
    strategy = DummySideFlipStrategy(held_qty=Decimal("5.4"))

    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=100.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=101.0, phase=MarketPhase.ACTIVE))

    assert strategy.active_side == ActiveSide.DOWN
    assert strategy.side_flip_count == 2
    assert strategy.strategy_events[-1][0] == "SIDE_MODE_FLIPPED"
    assert strategy.strategy_events[-1][1]["extra_flip_for_held_inventory"] is True


def test_no_extra_flip_without_material_held_inventory():
    strategy = DummySideFlipStrategy(held_qty=Decimal("0.01"))

    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=100.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=101.0, phase=MarketPhase.ACTIVE))

    assert strategy.active_side == ActiveSide.UP
    assert strategy.side_flip_count == 1
    assert strategy.strategy_events == []


def test_strike_prefers_polymarket_chainlink_history_anchor():
    strategy = DummySpotPricerStrategy()

    strike = asyncio.run(strategy._get_market_strike_for_instrument("inst-up"))

    assert strike == Decimal("66625.19")
    assert strategy.market_strike_source_by_slug["btc-updown-15m-test"] == "polymarket_chainlink_open"
