from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace

from bot.adapter_overrides import install_runtime_compatibility_overrides
from bot.app_config import AppConfig
from bot.enums import ActiveSide, MarketPhase
from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.fill_ledger import FillLedgerMixin, classify_fill_liquidity
from bot.market_cycle_state import MarketCycleState, bind_market_cycle_state
from bot.pricing_runtime import PricingRuntimeMixin
from bot.models import MarketSnapshot, PositionState, SignalDecision, ExitDecisionType
from bot.position_manager import PositionManager, PositionManagerConfig
from bot.quoting import apply_quote_plan_guards
from bot.quote_service import (
    apply_shadow_entry_veto,
    apply_time_based_profitable_sell_cap,
    build_desired_quote_entry,
    build_directional_snapshot,
    maybe_apply_continuation_entry,
    maybe_apply_trapped_inventory_recovery,
    preserve_recent_loss_sell_order,
    retreat_crossing_buy_quote,
)
from bot.order_submission import submit_maker_quote
from bot.shadow_signal import ShadowSignalConfig, build_live_signal_compare_payload
from bot.spot_pricer import SpotPricerMixin
from bot.side_decision import SideDecisionMixin
from bot.taker_exit import TakerExitMixin
from execution.exit_policy import ExitStage
from run_bot import IntegratedBTCStrategy


class DummyOrder:
    def __init__(self, client_order_id: str) -> None:
        self.client_order_id = client_order_id


class DummyProfitHoldStrategy:
    def __init__(self) -> None:
        now_ts = time.time()
        self.maker_profit_run_enabled = True
        self.maker_early_profit_hold_enabled = True
        self.maker_early_profit_hold_min_hold_sec = 60
        self.maker_early_profit_hold_max_profit_ps = Decimal("0.08")
        self.maker_early_profit_hold_min_score_abs = Decimal("0.18")
        self.active_side_locked = True
        self.active_side = ActiveSide.DOWN
        self.side_decision_score = Decimal("-0.47")
        self.maker_profit_run_min_score_abs = Decimal("0.12")
        self.maker_profit_run_min_profit_ps = Decimal("0.04")
        self.maker_profit_run_unlock_profit_ps = Decimal("0.18")
        self.maker_profit_run_trailing_drawdown_ps = Decimal("0.05")
        self.maker_profit_run_unlock_trailing_drawdown_ps = Decimal("0.02")
        self.maker_profit_run_min_hold_sec = 20
        self.exit_policy = SimpleNamespace(stage=lambda _time_left_sec: SimpleNamespace(value="PASSIVE"))
        self.live_inventory_cost = {
            "inst-down": {
                "qty": Decimal("5.30"),
                "opened_ts": now_ts - 10.0,
            }
        }
        self.maker_profit_run_peak_bid_by_inst = {}
        self.maker_profit_run_peak_fair_by_inst = {}
        self.position_manager = PositionManager(
            PositionManagerConfig(
                early_profit_hold_enabled=True,
                early_profit_hold_min_hold_sec=60,
                early_profit_hold_max_profit_ps=Decimal("0.08"),
                early_profit_hold_min_score_abs=Decimal("0.18"),
                profit_run_enabled=True,
                profit_run_min_hold_sec=20,
                profit_run_min_profit_ps=Decimal("0.04"),
                profit_run_min_score_abs=Decimal("0.12"),
                profit_run_trailing_drawdown_ps=Decimal("0.05"),
                profit_run_unlock_profit_ps=Decimal("0.18"),
                profit_run_unlock_trailing_drawdown_ps=Decimal("0.02"),
                stop_loss_entry_protection_sec=45,
                continuation_entry_protection_sec=60,
                stop_loss_regime_min_sec=8,
                stop_loss_regime_confirmations=4,
                stop_loss_min_opposite_score_abs=Decimal("0.18"),
            )
        )

    def _instrument_key(self, instrument_id):
        return str(instrument_id) if instrument_id is not None else ""

    def _instrument_for_side(self, side):
        if side == ActiveSide.DOWN:
            return "inst-down"
        if side == ActiveSide.UP:
            return "inst-up"
        return None


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

    def _classify_fill_liquidity(self, liquidity_side, raw_commission_dec, maker_matched):
        return IntegratedBTCStrategy._classify_fill_liquidity(
            liquidity_side,
            raw_commission_dec,
            maker_matched,
        )

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


class DummyOrderFactory:
    def limit(self, **kwargs):
        return SimpleNamespace(**kwargs)


class DummyTrendSubmitStrategy:
    def __init__(self) -> None:
        self.instrument = SimpleNamespace(size_precision=6, price_precision=3, price_increment=Decimal("0.01"))
        self.order_factory = DummyOrderFactory()
        self.maker_use_post_only = False
        self.maker_post_only_strict = False
        self.maker_min_shares = Decimal("5.4")
        self.maker_exchange_min_shares = Decimal("5.0")
        self.continuation_entry_size_multiplier = Decimal("1.0")
        self.trend_buy_size_multiplier = Decimal("1.5")
        self.stop_loss_reentry_pause_until_by_inst = {}
        self.inventory_delta_shares = Decimal("0")
        self.maker_max_inventory_shares = Decimal("20")
        self._sell_recovery_required_by_inst = {}
        self._sell_recovery_reason_by_inst = {}
        self._sell_recovery_venue_cap_by_inst = {}
        self.active_maker_orders = {}
        self.rebate_reporter = SimpleNamespace(record_quote=lambda **kwargs: None)
        self.submitted_orders = []
        self.order_events = []
        self.consecutive_denied_orders = 0

    @property
    def cache(self):
        return SimpleNamespace(instrument=lambda _instrument_id: self.instrument)

    def _normalize_instrument_id(self, instrument_id):
        return instrument_id

    def _align_price_to_tick(self, limit_price, _side, _instrument):
        return limit_price

    def _get_quote_for_instrument(self, _instrument_id):
        return None

    def _should_skip_buy_submit_for_quote_drift(self, **_kwargs):
        return False

    def _compute_maker_order_qty(self, _limit_price, _precision):
        return Decimal("5.4")

    def _instrument_key(self, instrument_id):
        return str(instrument_id)

    def _project_inventory_after_fill(self, side, qty_dec, instrument_id=None):
        if side == "buy":
            return self.inventory_delta_shares + qty_dec
        return self.inventory_delta_shares - qty_dec

    def _is_dry_run_mode(self):
        return False

    def _extract_token_id_from_instrument(self, _instrument_id):
        return "token-1"

    def _order_key_for(self, side, instrument_id):
        return f"{side}:{instrument_id}"

    def submit_order(self, order):
        self.submitted_orders.append(order)

    def _db_order_event(self, **kwargs):
        self.order_events.append(kwargs)


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


class DummyBuyDriftStrategy:
    def _instrument_key(self, instrument_id):
        return str(instrument_id) if instrument_id is not None else ""


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
        self.sell_recovery_qty_buffer_shares = Decimal("0.01")
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
        self.maker_profit_run_peak_bid_by_inst = {}
        self.maker_profit_run_peak_fair_by_inst = {}
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
        self.maker_urgent_exit_winner_peak_profit_ps = Decimal("0.08")
        self.maker_urgent_exit_winner_extra_confirmations = 2
        self.maker_urgent_exit_min_loss_usdc = Decimal("0.10")
        self.side_decision_score = Decimal("-2")
        self.maker_exchange_min_shares = Decimal("5")
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


class DummyUrgentExitMatchedStrategy(TakerExitMixin):
    def __init__(self) -> None:
        self.maker_urgent_exit_enabled = True
        self._maker_urgent_exit_last_ts = 0.0
        self.maker_urgent_exit_cooldown_sec = 0
        self.current_market_end_timestamp = 1_000_000.0
        self.live_inventory_cost = {
            "inst-up": {
                "qty": Decimal("5.2"),
                "avg_entry_price": Decimal("0.47"),
            }
        }
        self.active_side = ActiveSide.UP
        self.active_side_locked = True
        self.maker_profit_run_peak_bid_by_inst = {}
        self.maker_profit_run_peak_fair_by_inst = {}
        self.side_decision_engine_new = True
        self.pending_taker_exit_by_inst = {}
        self.active_maker_orders = {}
        self.cache = SimpleNamespace(instrument=lambda _inst: None)
        self.order_factory = SimpleNamespace(limit=lambda **kwargs: kwargs)
        self._urgent_exit_confirm_hits = {}
        self.maker_urgent_exit_min_confirmations = 1
        self.maker_urgent_exit_winner_peak_profit_ps = Decimal("0.08")
        self.maker_urgent_exit_winner_extra_confirmations = 2
        self.maker_urgent_exit_min_loss_usdc = Decimal("0.10")
        self.side_decision_score = Decimal("0.18")
        self._signal_engine = SimpleNamespace(is_mid_reversal=lambda holding_up: True)
        self.cancel_calls = []
        self.submit_calls = []
        self.db_events = []
        self.position_manager = PositionManager(
            PositionManagerConfig(
                early_profit_hold_enabled=True,
                early_profit_hold_min_hold_sec=60,
                early_profit_hold_max_profit_ps=Decimal("0.08"),
                early_profit_hold_min_score_abs=Decimal("0.18"),
                profit_run_enabled=True,
                profit_run_min_hold_sec=20,
                profit_run_min_profit_ps=Decimal("0.04"),
                profit_run_min_score_abs=Decimal("0.12"),
                profit_run_trailing_drawdown_ps=Decimal("0.05"),
                profit_run_unlock_profit_ps=Decimal("0.18"),
                profit_run_unlock_trailing_drawdown_ps=Decimal("0.02"),
                stop_loss_entry_protection_sec=45,
                continuation_entry_protection_sec=60,
                stop_loss_regime_min_sec=8,
                stop_loss_regime_confirmations=4,
                stop_loss_min_opposite_score_abs=Decimal("0.18"),
            )
        )
        self.position_manager.on_fill(
            inst_key="inst-up",
            side="buy",
            remaining_qty=Decimal("5.2"),
            thesis_side=ActiveSide.UP.value,
            now_ts=time.time(),
        )

    def _maker_quote_instruments(self):
        return []

    def _instrument_key(self, instrument_id):
        return str(instrument_id) if instrument_id is not None else ""

    def _normalize_instrument_id(self, instrument_id):
        return instrument_id

    def _instrument_for_side(self, side):
        return "inst-up" if side == ActiveSide.UP else "inst-down"

    def _side_for_instrument_id(self, instrument_id):
        return ActiveSide.UP if instrument_id == "inst-up" else ActiveSide.DOWN

    def _get_quote_for_instrument(self, instrument_id):
        assert instrument_id == "inst-up"
        return Decimal("0.24"), Decimal("0.25")

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
        self.side_decision_engine_new = True
        self.active_side = ActiveSide.UP
        self.active_side_locked = True
        self.bi_side_allow_intramarket_flip = True
        self.side_flip_count = 1
        self.bi_side_flip_max_per_market = 1
        self.bi_side_flip_confirmations = 1
        self.bi_side_flip_min_score_up = Decimal("2")
        self.bi_side_flip_max_score_down = Decimal("-2")
        self.bi_side_flip_min_score_up_new = Decimal("0.12")
        self.bi_side_flip_max_score_down_new = Decimal("-0.12")
        self.bi_side_flip_confirmations_held_new = 4
        self.bi_side_flip_min_persist_sec_held_new = 8.0
        self.bi_side_flip_min_score_up_held_new = Decimal("0.18")
        self.bi_side_flip_max_score_down_held_new = Decimal("-0.18")
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
        self.side_pending_flip_since_ts = 0.0
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
        self.active_maker_orders = {
            "buy:inst-up": {
                "side": "buy",
                "instrument_id": "inst-up",
            }
        }
        self.cancel_calls = []

    def _instrument_key(self, instrument_id):
        return str(instrument_id) if instrument_id is not None else ""

    def _normalize_instrument_id(self, instrument_id):
        return instrument_id

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

    def _cancel_maker_order_side(self, order_key, reason=""):
        self.cancel_calls.append((order_key, reason))

    async def _get_market_strike_for_instrument(self, _instrument_id):
        return Decimal("66867")

    def _compute_side_decision(self, now_ts):
        return (
            ActiveSide.DOWN,
            Decimal("-0.20"),
            "cs=-0.2000",
            {
                "fair_up": 0.01,
                "fair_down": 0.99,
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


class DummySellableQtyStrategy(PricingRuntimeMixin):
    def __init__(self) -> None:
        self.live_inventory_cost = {"inst-down": {"qty": Decimal("5.312753")}}
        self.recent_buy_fill_ts_by_inst = {"inst-down": time.time()}
        self.sellable_fallback_after_buy_sec = 10
        self.sellable_after_buy_buffer_shares = Decimal("0.05")
        self.conditional_balance_safety_buffer_pct = Decimal("0.001")
        self._sell_recovery_venue_cap_by_inst = {}

    def _get_confirmed_inventory_qty_for_instrument(self, instrument_id=None):
        return Decimal("5.312753")

    def _get_sellable_qty_for_current_instrument(self, instrument_id=None):
        return Decimal("5.312753")

    def _extract_token_id_from_instrument(self, inst_txt):
        return "token-down"

    def _instrument_key(self, instrument_id):
        return str(instrument_id) if instrument_id is not None else ""

    def _get_conditional_balance_for_token(self, token_id=None, force_refresh=False):
        return Decimal("0")


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
    assert strategy._sell_recovery_venue_cap_by_inst["cond-123-456.POLYMARKET"] == Decimal("4.98783")
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


def test_urgent_exit_does_not_fire_when_signal_still_matches_position():
    strategy = DummyUrgentExitMatchedStrategy()

    asyncio.run(strategy._maybe_maker_urgent_exit(now_ts=100.0))

    assert strategy.cancel_calls == []
    assert strategy.submit_calls == []
    assert strategy.db_events == []


def test_urgent_exit_requires_more_confirmations_for_former_winner():
    strategy = DummyUrgentExitStrategy()
    strategy.active_maker_orders = {}
    strategy.maker_profit_run_peak_bid_by_inst["inst-up"] = Decimal("0.49")
    strategy._urgent_exit_confirm_hits["inst-up"] = 1

    asyncio.run(strategy._maybe_maker_urgent_exit(now_ts=100.0))

    assert strategy.submit_calls == []
    assert strategy.db_events == []


def test_held_inventory_allows_one_extra_flip_after_quota_exhausted():
    strategy = DummySideFlipStrategy(held_qty=Decimal("5.4"))
    strategy.bi_side_flip_confirmations = 1

    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=100.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=101.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=102.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=108.0, phase=MarketPhase.ACTIVE))

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


def test_new_signal_flip_uses_new_scale_thresholds():
    strategy = DummySideFlipStrategy(held_qty=Decimal("2.0"))
    strategy.side_decision_engine_new = True
    strategy.active_side = ActiveSide.DOWN
    strategy.active_side_locked = True
    strategy.side_flip_count = 0
    strategy.bi_side_flip_confirmations = 1

    def _compute_side_decision(_now_ts):
        return (
            ActiveSide.UP,
            Decimal("0.16"),
            "cs=+0.1600",
            {
                "fair_up": 0.72,
                "fair_down": 0.28,
            },
        )

    strategy._compute_side_decision = _compute_side_decision

    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=100.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=101.0, phase=MarketPhase.ACTIVE))

    assert strategy.active_side == ActiveSide.UP
    assert strategy.side_flip_count == 1
    assert strategy.strategy_events[-1][0] == "SIDE_MODE_FLIPPED"


def test_side_change_cancels_stale_buy_orders_for_old_instrument():
    strategy = DummySideFlipStrategy(held_qty=Decimal("2.0"))
    strategy.active_side = ActiveSide.UP
    strategy.side_flip_count = 0
    strategy.bi_side_flip_confirmations = 1

    def _compute_side_decision(_now_ts):
        return (
            ActiveSide.DOWN,
            Decimal("-0.20"),
            "cs=-0.2000",
            {
                "fair_up": 0.20,
                "fair_down": 0.80,
            },
        )

    strategy._compute_side_decision = _compute_side_decision

    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=100.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=101.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=102.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=108.0, phase=MarketPhase.ACTIVE))

    assert strategy.active_side == ActiveSide.DOWN
    assert ("buy:inst-up", "side_change_stale_buy") in strategy.cancel_calls
    assert any(evt == "SIDE_CHANGE_CANCELED_STALE_BUYS" for evt, _ in strategy.strategy_events)


def test_new_signal_held_inventory_flip_requires_more_time_and_confirms():
    strategy = DummySideFlipStrategy(held_qty=Decimal("5.4"))
    strategy.side_decision_engine_new = True
    strategy.active_side = ActiveSide.UP
    strategy.side_flip_count = 0
    strategy.bi_side_flip_confirmations = 1

    def _compute_side_decision(_now_ts):
        return (
            ActiveSide.DOWN,
            Decimal("-0.20"),
            "cs=-0.2000",
            {
                "fair_up": 0.20,
                "fair_down": 0.80,
            },
        )

    strategy._compute_side_decision = _compute_side_decision

    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=100.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=101.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=102.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=103.0, phase=MarketPhase.ACTIVE))

    assert strategy.active_side == ActiveSide.UP

    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=108.0, phase=MarketPhase.ACTIVE))

    assert strategy.active_side == ActiveSide.DOWN


def test_strike_prefers_polymarket_chainlink_history_anchor():
    strategy = DummySpotPricerStrategy()

    strike = asyncio.run(strategy._get_market_strike_for_instrument("inst-up"))

    assert strike == Decimal("66625.19")
    assert strategy.market_strike_source_by_slug["btc-updown-15m-test"] == "polymarket_chainlink_open"


def test_quote_plan_guards_uses_separate_buy_sell_momentum_thresholds():
    side_plan = {
        "buy": (
            Decimal("0.44"),
            None,
            True,
            Decimal("0.10"),
            Decimal("0"),
            Decimal("0.05"),
            Decimal("0.20"),
            Decimal("0.50"),
            Decimal("0"),
            Decimal("0"),
        ),
        "sell": (
            Decimal("0.50"),
            None,
            True,
            Decimal("0.10"),
            Decimal("0"),
            Decimal("0.01"),
            Decimal("0.05"),
            Decimal("0.50"),
            Decimal("0"),
            Decimal("0"),
        ),
    }
    outcome = apply_quote_plan_guards(
        side_plan=side_plan,
        quote_mode="both",
        phase_value=MarketPhase.ACTIVE.value,
        inventory_delta_shares=Decimal("0"),
        early_sell_only_sec=0.0,
        time_left_sec_global=600.0,
        directional_edge_gate_enabled=False,
        regime_guard_active=False,
        min_directional_edge_ps=Decimal("0.01"),
        min_directional_edge_ps_conservative=Decimal("0.02"),
        now_ts=100.0,
        buy_cooldown_until_ts=0.0,
        momentum_buy_filter_pct=Decimal("0.04"),
        momentum_sell_filter_pct=Decimal("0.20"),
        momentum_window_ticks=4,
        momentum_history=[Decimal("0.50"), Decimal("0.49"), Decimal("0.46"), Decimal("0.435")],
        fair=Decimal("0.50"),
        min_fair_price=Decimal("0.05"),
        max_fair_price=Decimal("0.95"),
        end_ts=1000.0,
        min_minutes_to_close=3.0,
        reduce_only_no_new_sell_last_sec=45,
        forced_sell_only=False,
        active_side=ActiveSide.UP.value,
        min_directional_edge_ps_down=None,
    )

    assert outcome.momentum_buy_blocked is False
    assert "buy" not in outcome.side_disable_reason_by_side


def test_quote_plan_guards_still_blocks_buy_momentum_without_active_side():
    side_plan = {
        "buy": (
            Decimal("0.43"),
            None,
            True,
            Decimal("0.10"),
            Decimal("0"),
            Decimal("0.05"),
            Decimal("0.20"),
            Decimal("0.50"),
            Decimal("0"),
            Decimal("0"),
        ),
    }
    outcome = apply_quote_plan_guards(
        side_plan=side_plan,
        quote_mode="both",
        phase_value=MarketPhase.ACTIVE.value,
        inventory_delta_shares=Decimal("0"),
        early_sell_only_sec=0.0,
        time_left_sec_global=600.0,
        directional_edge_gate_enabled=False,
        regime_guard_active=False,
        min_directional_edge_ps=Decimal("0.01"),
        min_directional_edge_ps_conservative=Decimal("0.02"),
        now_ts=100.0,
        buy_cooldown_until_ts=0.0,
        momentum_buy_filter_pct=Decimal("0.04"),
        momentum_sell_filter_pct=Decimal("0.20"),
        momentum_window_ticks=4,
        momentum_history=[Decimal("0.50"), Decimal("0.49"), Decimal("0.46"), Decimal("0.435")],
        fair=Decimal("0.50"),
        min_fair_price=Decimal("0.05"),
        max_fair_price=Decimal("0.95"),
        end_ts=1000.0,
        min_minutes_to_close=3.0,
        reduce_only_no_new_sell_last_sec=45,
        forced_sell_only=False,
        active_side=ActiveSide.NONE.value,
        min_directional_edge_ps_down=None,
    )

    assert outcome.momentum_buy_blocked is True
    assert outcome.momentum_sell_blocked is False
    assert outcome.side_disable_reason_by_side["buy"] == "momentum_buy_block"


def test_quote_plan_guards_never_blocks_inventory_exit_sell_on_momentum():
    side_plan = {
        "sell": (
            Decimal("0.60"),
            None,
            True,
            Decimal("0.10"),
            Decimal("0"),
            Decimal("0.03"),
            Decimal("0.15"),
            Decimal("0.55"),
            Decimal("0"),
            Decimal("0"),
        ),
    }
    outcome = apply_quote_plan_guards(
        side_plan=side_plan,
        quote_mode="both",
        phase_value=MarketPhase.ACTIVE.value,
        inventory_delta_shares=Decimal("5.4"),
        early_sell_only_sec=0.0,
        time_left_sec_global=600.0,
        directional_edge_gate_enabled=False,
        regime_guard_active=False,
        min_directional_edge_ps=Decimal("0.01"),
        min_directional_edge_ps_conservative=Decimal("0.02"),
        now_ts=100.0,
        buy_cooldown_until_ts=0.0,
        momentum_buy_filter_pct=Decimal("0.04"),
        momentum_sell_filter_pct=Decimal("0.02"),
        momentum_window_ticks=4,
        momentum_history=[Decimal("0.50"), Decimal("0.55"), Decimal("0.58"), Decimal("0.62")],
        fair=Decimal("0.55"),
        min_fair_price=Decimal("0.05"),
        max_fair_price=Decimal("0.95"),
        end_ts=1000.0,
        min_minutes_to_close=3.0,
        reduce_only_no_new_sell_last_sec=45,
        forced_sell_only=False,
        active_side=ActiveSide.UP.value,
        min_directional_edge_ps_down=None,
    )

    assert outcome.momentum_sell_blocked is False
    assert "sell" not in outcome.side_disable_reason_by_side


def test_effective_sellable_qty_prefers_local_after_buy_with_buffer():
    strategy = DummySellableQtyStrategy()

    qty = strategy._get_effective_sellable_qty("inst-down")

    assert qty == Decimal("5.262753")


def test_effective_sellable_qty_uses_buffered_venue_cap_after_reject():
    strategy = DummySellableQtyStrategy()
    strategy._sell_recovery_venue_cap_by_inst["inst-down"] = Decimal("5.25781")

    qty = strategy._get_effective_sellable_qty("inst-down")

    assert qty == Decimal("5.25781")


def test_exit_policy_does_not_mark_supported_new_signal_as_stop_loss():
    engine = ExitPolicyEngine(
        ExitEngineConfig(
            min_hold_sec=0,
            stop_loss_usdc=Decimal("0.50"),
            stop_loss_confirmations=2,
            stop_loss_requires_thesis_weakening=True,
            stop_loss_thesis_min_score_abs=Decimal("0.05"),
            stop_loss_hold_on_none_signal=True,
            conviction_band_min_price=Decimal("0.60"),
            hold_band_min_price=Decimal("0.68"),
            conviction_band_min_score_abs=Decimal("0.15"),
            hold_band_min_score_abs=Decimal("0.15"),
            hold_band_release_min_roi=Decimal("0.15"),
            conviction_stop_loss_multiplier=Decimal("1.75"),
            conviction_extra_confirmations=1,
            hold_band_requires_locked=True,
        )
    )
    snapshot = MarketSnapshot(
        instrument_id="inst-up",
        phase=MarketPhase.ACTIVE.value,
        time_left_sec=592.0,
        best_bid=Decimal("0.23"),
        best_ask=Decimal("0.25"),
        fee_rate=Decimal("0"),
        spread=Decimal("0.02"),
        spread_pct=Decimal("0.08"),
        slippage_buffer_pct=Decimal("0"),
        exit_stage=ExitStage.PASSIVE,
        in_reduce_only_tail=False,
        stop_loss_disabled_in_tail=False,
    )
    position = PositionState(
        instrument_id="inst-up",
        qty=Decimal("5.41"),
        sellable_qty=Decimal("5.41"),
        avg_entry_price=Decimal("0.470184842883549"),
        entry_fee_remaining=Decimal("0"),
        hold_sec=20.0,
        stop_loss_confirm_hits=0,
    )
    signal = SignalDecision(
        active_side=ActiveSide.UP.value,
        score=Decimal("0.183103"),
        locked=True,
        reason="cs=+0.1831",
        matches_position=True,
    )

    decision = engine.evaluate(snapshot, position, signal)

    assert decision.decision_type == ExitDecisionType.NONE
    assert decision.reason == "thesis_still_supported"


def test_exit_policy_holds_position_when_signal_is_none():
    engine = ExitPolicyEngine(
        ExitEngineConfig(
            min_hold_sec=0,
            stop_loss_usdc=Decimal("0.50"),
            stop_loss_confirmations=2,
            stop_loss_requires_thesis_weakening=True,
            stop_loss_thesis_min_score_abs=Decimal("0.05"),
            stop_loss_hold_on_none_signal=True,
            conviction_band_min_price=Decimal("0.60"),
            hold_band_min_price=Decimal("0.68"),
            conviction_band_min_score_abs=Decimal("0.15"),
            hold_band_min_score_abs=Decimal("0.15"),
            hold_band_release_min_roi=Decimal("0.15"),
            conviction_stop_loss_multiplier=Decimal("1.75"),
            conviction_extra_confirmations=1,
            hold_band_requires_locked=True,
        )
    )
    snapshot = MarketSnapshot(
        instrument_id="inst-down",
        phase=MarketPhase.ACTIVE.value,
        time_left_sec=554.0,
        best_bid=Decimal("0.68"),
        best_ask=Decimal("0.69"),
        fee_rate=Decimal("0"),
        spread=Decimal("0.01"),
        spread_pct=Decimal("0.02"),
        slippage_buffer_pct=Decimal("0"),
        exit_stage=ExitStage.PASSIVE,
        in_reduce_only_tail=False,
        stop_loss_disabled_in_tail=False,
    )
    position = PositionState(
        instrument_id="inst-down",
        qty=Decimal("5.40"),
        sellable_qty=Decimal("5.3946"),
        avg_entry_price=Decimal("0.66"),
        entry_fee_remaining=Decimal("0"),
        hold_sec=60.0,
        stop_loss_confirm_hits=1,
    )
    signal = SignalDecision(
        active_side=ActiveSide.NONE.value,
        score=Decimal("0.025633"),
        locked=False,
        reason="low_confidence",
        matches_position=False,
    )

    decision = engine.evaluate(snapshot, position, signal)

    assert decision.decision_type == ExitDecisionType.NONE
    assert decision.reason == "signal_none_hold"
    assert decision.metadata["signal_is_none"] == "1"


def test_exit_policy_treats_none_signal_as_thesis_weakening_when_losing():
    engine = ExitPolicyEngine(
        ExitEngineConfig(
            min_hold_sec=0,
            stop_loss_usdc=Decimal("0.10"),
            stop_loss_confirmations=2,
            stop_loss_requires_thesis_weakening=True,
            stop_loss_thesis_min_score_abs=Decimal("0.05"),
            stop_loss_hold_on_none_signal=True,
            conviction_band_min_price=Decimal("0.60"),
            hold_band_min_price=Decimal("0.68"),
            conviction_band_min_score_abs=Decimal("0.15"),
            hold_band_min_score_abs=Decimal("0.15"),
            hold_band_release_min_roi=Decimal("0.15"),
            conviction_stop_loss_multiplier=Decimal("1.75"),
            conviction_extra_confirmations=1,
            hold_band_requires_locked=True,
        )
    )
    snapshot = MarketSnapshot(
        instrument_id="inst-down",
        phase=MarketPhase.ACTIVE.value,
        time_left_sec=554.0,
        best_bid=Decimal("0.50"),
        best_ask=Decimal("0.51"),
        fee_rate=Decimal("0"),
        spread=Decimal("0.01"),
        spread_pct=Decimal("0.02"),
        slippage_buffer_pct=Decimal("0"),
        exit_stage=ExitStage.PASSIVE,
        in_reduce_only_tail=False,
        stop_loss_disabled_in_tail=False,
    )
    position = PositionState(
        instrument_id="inst-down",
        qty=Decimal("5.40"),
        sellable_qty=Decimal("5.3946"),
        avg_entry_price=Decimal("0.66"),
        entry_fee_remaining=Decimal("0"),
        hold_sec=60.0,
        stop_loss_confirm_hits=0,
    )
    signal = SignalDecision(
        active_side=ActiveSide.NONE.value,
        score=Decimal("0.00"),
        locked=False,
        reason="low_confidence",
        matches_position=False,
    )

    decision = engine.evaluate(snapshot, position, signal)

    assert decision.decision_type == ExitDecisionType.STOP_LOSS_PENDING_CONFIRMATION
    assert decision.metadata["signal_is_none"] == "1"
    assert decision.metadata["thesis_weakened"] == "1"


def test_exit_policy_holds_in_band_when_roi_is_below_release_threshold():
    engine = ExitPolicyEngine(
        ExitEngineConfig(
            min_hold_sec=0,
            stop_loss_usdc=Decimal("0.50"),
            stop_loss_confirmations=2,
            stop_loss_requires_thesis_weakening=True,
            stop_loss_thesis_min_score_abs=Decimal("0.05"),
            stop_loss_hold_on_none_signal=True,
            conviction_band_min_price=Decimal("0.60"),
            hold_band_min_price=Decimal("0.68"),
            conviction_band_min_score_abs=Decimal("0.15"),
            hold_band_min_score_abs=Decimal("0.15"),
            hold_band_release_min_roi=Decimal("0.15"),
            conviction_stop_loss_multiplier=Decimal("1.75"),
            conviction_extra_confirmations=1,
            hold_band_requires_locked=True,
        )
    )
    snapshot = MarketSnapshot(
        instrument_id="inst-up",
        phase=MarketPhase.ACTIVE.value,
        time_left_sec=540.0,
        best_bid=Decimal("0.69"),
        best_ask=Decimal("0.70"),
        fee_rate=Decimal("0"),
        spread=Decimal("0.01"),
        spread_pct=Decimal("0.014"),
        slippage_buffer_pct=Decimal("0"),
        exit_stage=ExitStage.PASSIVE,
        in_reduce_only_tail=False,
        stop_loss_disabled_in_tail=False,
    )
    position = PositionState(
        instrument_id="inst-up",
        qty=Decimal("5.40"),
        sellable_qty=Decimal("5.40"),
        avg_entry_price=Decimal("0.66"),
        entry_fee_remaining=Decimal("0"),
        hold_sec=80.0,
        stop_loss_confirm_hits=0,
    )
    signal = SignalDecision(
        active_side=ActiveSide.UP.value,
        score=Decimal("0.22"),
        locked=True,
        reason="cs=+0.22",
        matches_position=True,
    )

    decision = engine.evaluate(snapshot, position, signal)

    assert decision.decision_type == ExitDecisionType.HOLD_IN_BAND
    assert decision.reason == "hold_band_in_thesis"
    assert decision.metadata["hold_band_released"] == "0"


def test_exit_policy_releases_hold_band_when_roi_crosses_threshold():
    engine = ExitPolicyEngine(
        ExitEngineConfig(
            min_hold_sec=0,
            stop_loss_usdc=Decimal("0.50"),
            stop_loss_confirmations=2,
            stop_loss_requires_thesis_weakening=True,
            stop_loss_thesis_min_score_abs=Decimal("0.05"),
            stop_loss_hold_on_none_signal=True,
            conviction_band_min_price=Decimal("0.60"),
            hold_band_min_price=Decimal("0.68"),
            conviction_band_min_score_abs=Decimal("0.15"),
            hold_band_min_score_abs=Decimal("0.15"),
            hold_band_release_min_roi=Decimal("0.15"),
            conviction_stop_loss_multiplier=Decimal("1.75"),
            conviction_extra_confirmations=1,
            hold_band_requires_locked=True,
        )
    )
    snapshot = MarketSnapshot(
        instrument_id="inst-up",
        phase=MarketPhase.ACTIVE.value,
        time_left_sec=540.0,
        best_bid=Decimal("0.79"),
        best_ask=Decimal("0.80"),
        fee_rate=Decimal("0"),
        spread=Decimal("0.01"),
        spread_pct=Decimal("0.0125"),
        slippage_buffer_pct=Decimal("0"),
        exit_stage=ExitStage.PASSIVE,
        in_reduce_only_tail=False,
        stop_loss_disabled_in_tail=False,
    )
    position = PositionState(
        instrument_id="inst-up",
        qty=Decimal("5.40"),
        sellable_qty=Decimal("5.40"),
        avg_entry_price=Decimal("0.66"),
        entry_fee_remaining=Decimal("0"),
        hold_sec=80.0,
        stop_loss_confirm_hits=0,
    )
    signal = SignalDecision(
        active_side=ActiveSide.UP.value,
        score=Decimal("0.22"),
        locked=True,
        reason="cs=+0.22",
        matches_position=True,
    )

    decision = engine.evaluate(snapshot, position, signal)

    assert decision.decision_type == ExitDecisionType.NONE
    assert decision.metadata["band"] == "hold"
    assert decision.metadata["hold_band_released"] == "1"


def test_same_side_early_profit_hold_blocks_small_quick_profit_sells():
    strategy = DummyProfitHoldStrategy()

    hold, reason = IntegratedBTCStrategy._should_hold_profitable_position(
        strategy,
        instrument_id="inst-down",
        best_bid=Decimal("0.64"),
        fair=Decimal("0.65"),
        avg_entry=Decimal("0.6206"),
        time_left_sec=700.0,
        thesis_weakened=False,
        offside_confirmed=False,
    )

    assert hold is True
    assert reason.startswith("early_profit_hold")


def test_same_side_early_profit_hold_ignores_short_term_score_wobble():
    strategy = DummyProfitHoldStrategy()
    strategy.side_decision_score = Decimal("0.04")

    hold, reason = IntegratedBTCStrategy._should_hold_profitable_position(
        strategy,
        instrument_id="inst-down",
        best_bid=Decimal("0.64"),
        fair=Decimal("0.65"),
        avg_entry=Decimal("0.6206"),
        time_left_sec=700.0,
        thesis_weakened=False,
        offside_confirmed=False,
    )

    assert hold is True
    assert reason.startswith("early_profit_hold")


def test_winner_continuation_holds_when_fair_edge_remains_large():
    strategy = DummyProfitHoldStrategy()
    strategy.side_decision_score = Decimal("0.04")
    strategy.live_inventory_cost["inst-down"]["opened_ts"] = time.time() - 120.0
    strategy.position_manager = PositionManager(
        PositionManagerConfig(
            early_profit_hold_enabled=True,
            early_profit_hold_min_hold_sec=60,
            early_profit_hold_max_profit_ps=Decimal("0.08"),
            early_profit_hold_min_score_abs=Decimal("0.18"),
            profit_run_enabled=True,
            profit_run_min_hold_sec=20,
            profit_run_min_profit_ps=Decimal("0.04"),
            profit_run_min_score_abs=Decimal("0.12"),
            profit_run_trailing_drawdown_ps=Decimal("0.05"),
            profit_run_unlock_profit_ps=Decimal("0.18"),
            profit_run_unlock_trailing_drawdown_ps=Decimal("0.02"),
            stop_loss_entry_protection_sec=45,
            continuation_entry_protection_sec=60,
            stop_loss_regime_min_sec=8,
            stop_loss_regime_confirmations=4,
            stop_loss_min_opposite_score_abs=Decimal("0.18"),
            winner_continuation_min_fair_edge_ps=Decimal("0.04"),
        )
    )

    hold, reason = IntegratedBTCStrategy._should_hold_profitable_position(
        strategy,
        instrument_id="inst-down",
        best_bid=Decimal("0.74"),
        fair=Decimal("0.80"),
        avg_entry=Decimal("0.6206"),
        time_left_sec=420.0,
        thesis_weakened=False,
        offside_confirmed=False,
    )

    assert hold is True
    assert reason.startswith("winner_continuation")


def test_retreat_crossing_buy_quote_clamps_to_best_bid_for_synthetic_maker():
    info_messages = []
    warning_messages = []

    price = retreat_crossing_buy_quote(
        limit_price=Decimal("0.44"),
        instrument=None,
        quote_now=(Decimal("0.31"), Decimal("0.33")),
        align_price_fn=lambda px, _side, _instrument: px,
        logger_warning_fn=warning_messages.append,
        logger_info_fn=info_messages.append,
    )

    assert price == Decimal("0.31")
    assert info_messages
    assert not warning_messages


def test_continuation_entry_reenables_buy_when_locked_signal_is_strong_and_price_is_still_passive():
    desired = {
        "should_quote": False,
        "diag_reason": "econ_gate robust_net=-0.019712 (expected_net=0.022528, exec_penalty=0.033916) < min=0.001020",
        "robust_net": Decimal("-0.019712"),
        "price": Decimal("0.60"),
    }

    updated = maybe_apply_continuation_entry(
        desired_entry=desired,
        side="buy",
        active_side_locked=True,
        active_side_value=ActiveSide.UP.value,
        inst_id="inst-up",
        active_instrument_id="inst-up",
        side_score=Decimal("0.35"),
        locked_for_sec=30.0,
        time_left_sec=600.0,
        current_inventory_qty=Decimal("0"),
        best_bid=Decimal("0.58"),
        fair=Decimal("0.6034"),
        continuation_enabled=True,
        continuation_size_multiplier=Decimal("1.0"),
    )

    assert updated["should_quote"] is True
    assert updated["entry_mode"] == "continuation"
    assert updated["price"] == Decimal("0.58")


def test_continuation_entry_stays_blocked_when_market_is_too_expensive():
    desired = {
        "should_quote": False,
        "diag_reason": "econ_gate robust_net=-0.066255 (expected_net=-0.023000, exec_penalty=0.034818) < min=0.001020",
        "robust_net": Decimal("-0.066255"),
        "price": Decimal("0.70"),
    }

    updated = maybe_apply_continuation_entry(
        desired_entry=desired,
        side="buy",
        active_side_locked=True,
        active_side_value=ActiveSide.UP.value,
        inst_id="inst-up",
        active_instrument_id="inst-up",
        side_score=Decimal("0.42"),
        locked_for_sec=30.0,
        time_left_sec=580.0,
        current_inventory_qty=Decimal("0"),
        best_bid=Decimal("0.69"),
        fair=Decimal("0.6108"),
        continuation_enabled=True,
        continuation_size_multiplier=Decimal("1.0"),
    )

    assert updated["should_quote"] is False


def test_continuation_entry_stays_blocked_when_latest_observation_flips_against_locked_side():
    desired = {
        "should_quote": False,
        "diag_reason": "econ_gate robust_net=-0.010000 < min=0.001020",
        "robust_net": Decimal("-0.010000"),
        "price": Decimal("0.42"),
    }

    updated = maybe_apply_continuation_entry(
        desired_entry=desired,
        side="buy",
        active_side_locked=True,
        active_side_value=ActiveSide.DOWN.value,
        inst_id="inst-down",
        active_instrument_id="inst-down",
        side_score=Decimal("0.43"),
        locked_for_sec=30.0,
        time_left_sec=400.0,
        current_inventory_qty=Decimal("0"),
        best_bid=Decimal("0.42"),
        fair=Decimal("0.50"),
        continuation_enabled=True,
        continuation_size_multiplier=Decimal("1.0"),
    )

    assert updated["should_quote"] is False


def test_buy_submit_quote_guard_skips_when_book_drifted_too_far():
    strategy = DummyBuyDriftStrategy()

    should_skip = IntegratedBTCStrategy._should_skip_buy_submit_for_quote_drift(
        strategy,
        instrument_id="inst-down",
        quote_now=(Decimal("0.33"), Decimal("0.35")),
        directional_snapshot={
            "planned_best_bid": Decimal("0.42"),
            "planned_best_ask": Decimal("0.43"),
            "planned_quote_ts": time.time(),
        },
        instrument=None,
    )

    assert should_skip is True


def test_fill_liquidity_classification_prefers_matched_maker_when_raw_side_unknown_and_no_commission():
    classification = classify_fill_liquidity(
        liquidity_side="",
        raw_commission_dec=Decimal("0"),
        maker_matched=True,
    )

    assert classification == "maker"


def test_urgent_exit_respects_position_manager_gate_during_entry_protection():
    strategy = DummyUrgentExitMatchedStrategy()
    strategy.active_side = ActiveSide.DOWN
    strategy.active_side_locked = True
    strategy.side_decision_score = Decimal("-0.35")

    asyncio.run(strategy._maybe_maker_urgent_exit(time.time()))

    assert strategy.submit_calls == []


def test_position_manager_requires_persistent_opposite_regime_before_stop_loss_arms():
    manager = PositionManager(
        PositionManagerConfig(
            early_profit_hold_enabled=True,
            early_profit_hold_min_hold_sec=60,
            early_profit_hold_max_profit_ps=Decimal("0.08"),
            early_profit_hold_min_score_abs=Decimal("0.18"),
            profit_run_enabled=True,
            profit_run_min_hold_sec=20,
            profit_run_min_profit_ps=Decimal("0.04"),
            profit_run_min_score_abs=Decimal("0.12"),
            profit_run_trailing_drawdown_ps=Decimal("0.05"),
            profit_run_unlock_profit_ps=Decimal("0.18"),
            profit_run_unlock_trailing_drawdown_ps=Decimal("0.02"),
            stop_loss_entry_protection_sec=45,
            continuation_entry_protection_sec=60,
            stop_loss_regime_min_sec=8,
            stop_loss_regime_confirmations=4,
            stop_loss_min_opposite_score_abs=Decimal("0.18"),
        )
    )

    manager.on_fill(
        inst_key="inst-up",
        side="buy",
        remaining_qty=Decimal("5.4"),
        thesis_side=ActiveSide.UP.value,
        now_ts=100.0,
    )

    early = manager.assess_stop_loss_regime(
        inst_key="inst-up",
        now_ts=120.0,
        qty=Decimal("5.4"),
        opened_ts=100.0,
        held_side=ActiveSide.UP.value,
        signal_active_side=ActiveSide.DOWN.value,
        signal_score=Decimal("-0.40"),
        signal_matches_position=False,
        force_exit=False,
    )
    assert early.status == "hold"
    assert early.reason.startswith("entry_protection")

    pending = manager.assess_stop_loss_regime(
        inst_key="inst-up",
        now_ts=146.0,
        qty=Decimal("5.4"),
        opened_ts=100.0,
        held_side=ActiveSide.UP.value,
        signal_active_side=ActiveSide.DOWN.value,
        signal_score=Decimal("-0.40"),
        signal_matches_position=False,
        force_exit=False,
    )
    assert pending.status == "pending"

    manager.assess_stop_loss_regime(
        inst_key="inst-up",
        now_ts=150.0,
        qty=Decimal("5.4"),
        opened_ts=100.0,
        held_side=ActiveSide.UP.value,
        signal_active_side=ActiveSide.DOWN.value,
        signal_score=Decimal("-0.41"),
        signal_matches_position=False,
        force_exit=False,
    )
    manager.assess_stop_loss_regime(
        inst_key="inst-up",
        now_ts=154.0,
        qty=Decimal("5.4"),
        opened_ts=100.0,
        held_side=ActiveSide.UP.value,
        signal_active_side=ActiveSide.DOWN.value,
        signal_score=Decimal("-0.42"),
        signal_matches_position=False,
        force_exit=False,
    )

    armed = manager.assess_stop_loss_regime(
        inst_key="inst-up",
        now_ts=158.0,
        qty=Decimal("5.4"),
        opened_ts=100.0,
        held_side=ActiveSide.UP.value,
        signal_active_side=ActiveSide.DOWN.value,
        signal_score=Decimal("-0.42"),
        signal_matches_position=False,
        force_exit=False,
    )
    assert armed.status == "armed"

    quick_reset = manager.assess_stop_loss_regime(
        inst_key="inst-up",
        now_ts=154.0,
        qty=Decimal("5.4"),
        opened_ts=100.0,
        held_side=ActiveSide.UP.value,
        signal_active_side=ActiveSide.UP.value,
        signal_score=Decimal("0.22"),
        signal_matches_position=True,
        force_exit=False,
    )
    assert quick_reset.status == "hold"


def test_position_manager_resets_stop_loss_regime_when_signal_returns_supported():
    manager = PositionManager(
        PositionManagerConfig(
            early_profit_hold_enabled=True,
            early_profit_hold_min_hold_sec=60,
            early_profit_hold_max_profit_ps=Decimal("0.08"),
            early_profit_hold_min_score_abs=Decimal("0.18"),
            profit_run_enabled=True,
            profit_run_min_hold_sec=20,
            profit_run_min_profit_ps=Decimal("0.04"),
            profit_run_min_score_abs=Decimal("0.12"),
            profit_run_trailing_drawdown_ps=Decimal("0.05"),
            profit_run_unlock_profit_ps=Decimal("0.18"),
            profit_run_unlock_trailing_drawdown_ps=Decimal("0.02"),
            stop_loss_entry_protection_sec=20,
            continuation_entry_protection_sec=60,
            stop_loss_regime_min_sec=8,
            stop_loss_regime_confirmations=2,
            stop_loss_min_opposite_score_abs=Decimal("0.18"),
        )
    )

    manager.on_fill(
        inst_key="inst-up",
        side="buy",
        remaining_qty=Decimal("5.4"),
        thesis_side=ActiveSide.UP.value,
        now_ts=100.0,
    )
    manager.assess_stop_loss_regime(
        inst_key="inst-up",
        now_ts=130.0,
        qty=Decimal("5.4"),
        opened_ts=100.0,
        held_side=ActiveSide.UP.value,
        signal_active_side=ActiveSide.DOWN.value,
        signal_score=Decimal("-0.35"),
        signal_matches_position=False,
        force_exit=False,
    )
    decision = manager.assess_stop_loss_regime(
        inst_key="inst-up",
        now_ts=131.0,
        qty=Decimal("5.4"),
        opened_ts=100.0,
        held_side=ActiveSide.UP.value,
        signal_active_side=ActiveSide.UP.value,
        signal_score=Decimal("0.22"),
        signal_matches_position=True,
        force_exit=False,
    )

    assert decision.status == "hold"
    assert decision.reason.startswith("held_thesis_not_opposite")


def test_continuation_entry_gets_longer_entry_protection():
    manager = PositionManager(
        PositionManagerConfig(
            early_profit_hold_enabled=True,
            early_profit_hold_min_hold_sec=60,
            early_profit_hold_max_profit_ps=Decimal("0.08"),
            early_profit_hold_min_score_abs=Decimal("0.18"),
            profit_run_enabled=True,
            profit_run_min_hold_sec=20,
            profit_run_min_profit_ps=Decimal("0.04"),
            profit_run_min_score_abs=Decimal("0.12"),
            profit_run_trailing_drawdown_ps=Decimal("0.05"),
            profit_run_unlock_profit_ps=Decimal("0.18"),
            profit_run_unlock_trailing_drawdown_ps=Decimal("0.02"),
            stop_loss_entry_protection_sec=45,
            continuation_entry_protection_sec=60,
            stop_loss_regime_min_sec=8,
            stop_loss_regime_confirmations=2,
            stop_loss_min_opposite_score_abs=Decimal("0.18"),
        )
    )

    manager.on_fill(
        inst_key="inst-up",
        side="buy",
        remaining_qty=Decimal("5.4"),
        thesis_side=ActiveSide.UP.value,
        entry_mode="continuation",
        now_ts=100.0,
    )

    decision = manager.assess_stop_loss_regime(
        inst_key="inst-up",
        now_ts=150.0,
        qty=Decimal("5.4"),
        opened_ts=100.0,
        held_side=ActiveSide.UP.value,
        signal_active_side=ActiveSide.DOWN.value,
        signal_score=Decimal("-0.40"),
        signal_matches_position=False,
        force_exit=False,
    )

    assert decision.status == "hold"
    assert "mode=continuation" in decision.reason


def test_app_config_reads_extended_env(monkeypatch):
    monkeypatch.setenv("MAKER_REQUOTE_MIN_AGE_SEC", "9")
    monkeypatch.setenv("MAKER_DIGITAL_SIGMA_DEFAULT", "0.77")
    monkeypatch.setenv("AUTO_REDEEM_ENABLED", "1")
    monkeypatch.setenv("TRADE_DB_PATH", "./logs/custom.db")
    monkeypatch.setenv("REGIME_GUARD_N_MARKETS", "6")

    cfg = AppConfig.from_env(enable_terminal_dashboard=False)

    assert cfg.maker.requote_min_age_sec == 9.0
    assert cfg.maker.digital_sigma_default == Decimal("0.77")
    assert cfg.operations.auto_redeem_enabled is True
    assert cfg.operations.trade_db_path == "./logs/custom.db"
    assert cfg.risk.regime_guard_n_markets == 6


def test_runtime_compatibility_overrides_install():
    install_runtime_compatibility_overrides()

    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
    from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
    import py_clob_client.http_helpers.helpers as pyclob_helpers

    assert getattr(PolymarketDataClient, "_btc15m_runtime_compat_patched", False) is True
    assert getattr(PolymarketExecutionClient, "_btc15m_runtime_compat_patched", False) is True
    assert getattr(pyclob_helpers, "_btc15m_runtime_compat_patched", False) is True


def test_app_config_rejects_invalid_patch_mode(monkeypatch):
    monkeypatch.setenv("NAUTILUS_COMPAT_PATCH_MODE", "bad-mode")

    try:
        AppConfig.from_env(enable_terminal_dashboard=False)
        assert False, "expected invalid patch mode to raise"
    except ValueError as exc:
        assert "patch mode" in str(exc)


def test_market_cycle_state_binding_replaces_per_market_containers():
    strategy = SimpleNamespace()
    first = MarketCycleState()
    bind_market_cycle_state(strategy, first)
    strategy.pending_taker_exit_by_inst["inst-up"] = "order-1"

    second = MarketCycleState()
    bind_market_cycle_state(strategy, second)

    assert strategy.market_cycle_state is second
    assert strategy.pending_taker_exit_by_inst == {}
    assert strategy.pending_taker_exit_by_inst is second.pending_taker_exit_by_inst


def test_trapped_inventory_recovery_overrides_buy_cooldown():
    desired = {
        "should_quote": False,
        "diag_reason": "side_disabled:post_fill_buy_cooldown_12s",
        "robust_net": Decimal("0.03"),
        "entry_mode": "value",
        "size_multiplier": Decimal("1"),
    }

    out = maybe_apply_trapped_inventory_recovery(
        desired_entry=desired,
        side="buy",
        trapped_inventory_recovery_enabled=True,
        current_inst_inventory_qty=Decimal("3.0"),
        trapped_inventory_recovery_min_qty=Decimal("1.0"),
        maker_exchange_min_shares=Decimal("5.0"),
        active_side_locked=True,
        inst_id="inst-up",
        active_instrument_id="inst-up",
        latest_observation_supports_locked_side=True,
        robust_net=Decimal("0.03"),
        max_robust_net_deficit_usdc=Decimal("0.05"),
        time_left_sec=240.0,
    )

    assert out["should_quote"] is True
    assert out["entry_mode"] == "topup"
    assert "trapped_inventory_recovery" in out["diag_reason"]


def test_trapped_inventory_recovery_skips_dust_inventory():
    desired = {
        "should_quote": False,
        "diag_reason": "econ_gate robust_net=-0.010000 < min=0.001020",
        "robust_net": Decimal("0.01"),
        "entry_mode": "value",
        "size_multiplier": Decimal("1"),
    }

    out = maybe_apply_trapped_inventory_recovery(
        desired_entry=desired,
        side="buy",
        trapped_inventory_recovery_enabled=True,
        current_inst_inventory_qty=Decimal("0.0331"),
        trapped_inventory_recovery_min_qty=Decimal("1.0"),
        maker_exchange_min_shares=Decimal("5.0"),
        active_side_locked=True,
        inst_id="inst-up",
        active_instrument_id="inst-up",
        latest_observation_supports_locked_side=True,
        robust_net=Decimal("0.01"),
        max_robust_net_deficit_usdc=Decimal("0.05"),
        time_left_sec=240.0,
    )

    assert out["should_quote"] is False
    assert out["entry_mode"] == "value"


def test_trapped_inventory_recovery_blocks_tail_topup():
    desired = {
        "should_quote": False,
        "diag_reason": "econ_gate robust_net=-0.010000 < min=0.001020",
        "robust_net": Decimal("0.01"),
        "entry_mode": "value",
        "size_multiplier": Decimal("1"),
    }

    out = maybe_apply_trapped_inventory_recovery(
        desired_entry=desired,
        side="buy",
        trapped_inventory_recovery_enabled=True,
        current_inst_inventory_qty=Decimal("3.0"),
        trapped_inventory_recovery_min_qty=Decimal("1.0"),
        maker_exchange_min_shares=Decimal("5.0"),
        active_side_locked=True,
        inst_id="inst-up",
        active_instrument_id="inst-up",
        latest_observation_supports_locked_side=True,
        robust_net=Decimal("0.01"),
        max_robust_net_deficit_usdc=Decimal("0.05"),
        time_left_sec=120.0,
    )

    assert out["should_quote"] is False
    assert out["entry_mode"] == "value"


def test_shadow_entry_veto_blocks_opposite_candidate():
    desired = {
        "should_quote": True,
        "diag_reason": "ok",
        "entry_mode": "value",
    }

    out = apply_shadow_entry_veto(
        desired_entry=desired,
        side="buy",
        entry_mode="value",
        inst_id="inst-up",
        up_instrument_id="inst-up",
        down_instrument_id="inst-down",
        shadow_payload={
            "shadow_candidate_side": "BUY_DOWN",
            "shadow_bias_side": "DOWN",
            "shadow_score": -0.31,
            "shadow_min_score_abs": 0.15,
        },
    )

    assert out["should_quote"] is False
    assert out["diag_reason"] == "shadow_veto_opposite_candidate:BUY_DOWN"


def test_trend_buy_size_multiplier_flows_into_submit_qty():
    desired_entry = build_desired_quote_entry(
        order_key="buy:inst-up",
        side="buy",
        inst_id="inst-up",
        quote_data=(
            Decimal("0.64"),
            SimpleNamespace(
                expected_net_usdc=Decimal("0.007"),
                expected_rebate_usdc=Decimal("0"),
                expected_spread_capture_usdc=Decimal("0"),
                fee_equivalent_usdc=Decimal("0"),
            ),
            False,
            Decimal("-0.031"),
            Decimal("0.030"),
            Decimal("0.010"),
            Decimal("0.054"),
            Decimal("0.6244"),
            Decimal("0"),
            Decimal("0"),
        ),
        side_disable_reason_by_side={"buy": "econ_gate"},
        reduce_only_reason=None,
        reduce_only_tail_sell_block=False,
        reduce_only_no_new_sell_last_sec=30,
        forced_sell_only=False,
        min_expected_net_usdc=Decimal("-0.005"),
        now_ts=0.0,
        sell_pause_until=0.0,
        is_dry_run_mode=False,
        sellable_qty=None,
        maker_exchange_min_shares=Decimal("5.0"),
        avg_entry=Decimal("0"),
        emergency_window=False,
        high_cost_exit_cooldown_enabled=False,
        high_cost_exit_cooldown_sec=0.0,
        high_cost_exit_cooldown_until=0.0,
        maker_sell_cost_protect_enabled=False,
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0"),
        entry_mode="trend",
        trend_buy_penalty_discount=Decimal("0.50"),
        trend_buy_score=Decimal("0.20"),
        trend_buy_size_multiplier=Decimal("1.5"),
    )
    snapshot = build_directional_snapshot(desired_entry)
    strategy = DummyTrendSubmitStrategy()

    submit_maker_quote(
        strategy,
        instrument_id="inst-up",
        side="buy",
        limit_price=Decimal("0.64"),
        econ=desired_entry["econ"],
        directional_snapshot=snapshot,
    )

    assert strategy.submitted_orders, "expected submit_order to be called"
    submitted_qty = strategy.submitted_orders[0].quantity.as_decimal()
    assert submitted_qty == Decimal("8.100000")
    assert strategy.order_events[-1]["payload"]["entry_mode"] == "trend"


def test_reduce_only_overrides_trend_buy_quote():
    desired_entry = build_desired_quote_entry(
        order_key="buy:inst-up",
        side="buy",
        inst_id="inst-up",
        quote_data=(
            Decimal("0.81"),
            SimpleNamespace(
                expected_net_usdc=Decimal("0.135774"),
                expected_rebate_usdc=Decimal("0"),
                expected_spread_capture_usdc=Decimal("0"),
                fee_equivalent_usdc=Decimal("0"),
            ),
            False,
            Decimal("-0.010"),
            Decimal("0.020"),
            Decimal("0.010"),
            Decimal("0.040"),
            Decimal("0.8481"),
            Decimal("0"),
            Decimal("0"),
        ),
        side_disable_reason_by_side={"buy": "reduce_only_buy_block"},
        reduce_only_reason="fair 0.8481 > max 0.8000",
        reduce_only_tail_sell_block=False,
        reduce_only_no_new_sell_last_sec=30,
        forced_sell_only=False,
        min_expected_net_usdc=Decimal("-0.005"),
        now_ts=0.0,
        sell_pause_until=0.0,
        is_dry_run_mode=False,
        sellable_qty=None,
        maker_exchange_min_shares=Decimal("5.0"),
        avg_entry=Decimal("0"),
        emergency_window=False,
        high_cost_exit_cooldown_enabled=False,
        high_cost_exit_cooldown_sec=0.0,
        high_cost_exit_cooldown_until=0.0,
        maker_sell_cost_protect_enabled=False,
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0"),
        entry_mode="trend",
        trend_buy_penalty_discount=Decimal("0.50"),
        trend_buy_score=Decimal("0.51"),
        trend_buy_size_multiplier=Decimal("1.0"),
    )

    assert desired_entry["should_quote"] is False
    assert desired_entry["diag_reason"] == "reduce_only: fair 0.8481 > max 0.8000"


def test_loss_sell_requires_stop_loss_regime_and_min_hold():
    desired_entry = build_desired_quote_entry(
        order_key="sell:inst-down",
        side="sell",
        inst_id="inst-down",
        quote_data=(
            Decimal("0.50"),
            SimpleNamespace(
                expected_net_usdc=Decimal("0.020"),
                expected_rebate_usdc=Decimal("0"),
                expected_spread_capture_usdc=Decimal("0"),
                fee_equivalent_usdc=Decimal("0"),
            ),
            True,
            Decimal("0.020"),
            Decimal("0.008"),
            Decimal("0.010"),
            Decimal("0.050"),
            Decimal("0.5200"),
            Decimal("0"),
            Decimal("0"),
        ),
        side_disable_reason_by_side={},
        reduce_only_reason=None,
        reduce_only_tail_sell_block=False,
        reduce_only_no_new_sell_last_sec=30,
        forced_sell_only=False,
        min_expected_net_usdc=Decimal("0.001"),
        now_ts=120.0,
        sell_pause_until=0.0,
        is_dry_run_mode=False,
        sellable_qty=Decimal("5.4"),
        maker_exchange_min_shares=Decimal("5.0"),
        avg_entry=Decimal("0.58"),
        emergency_window=False,
        high_cost_exit_cooldown_enabled=False,
        high_cost_exit_cooldown_sec=0.0,
        high_cost_exit_cooldown_until=0.0,
        maker_sell_cost_protect_enabled=True,
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0.005"),
        maker_sell_min_profit_floor_ps=Decimal("0.010"),
        thesis_weakened=True,
        offside_confirmed=False,
        stop_loss_regime_armed=False,
        hold_sec=5.0,
        loss_sell_min_hold_sec=10.0,
        time_left_sec=600.0,
    )

    assert desired_entry["should_quote"] is False
    assert desired_entry["diag_reason"] == "sell_cost_protect sell=0.5000 < min=0.5850"
    assert desired_entry["loss_sell_reason"] == ""


def test_loss_sell_allows_cost_break_only_when_regime_armed_and_hold_elapsed():
    desired_entry = build_desired_quote_entry(
        order_key="sell:inst-down",
        side="sell",
        inst_id="inst-down",
        quote_data=(
            Decimal("0.50"),
            SimpleNamespace(
                expected_net_usdc=Decimal("0.020"),
                expected_rebate_usdc=Decimal("0"),
                expected_spread_capture_usdc=Decimal("0"),
                fee_equivalent_usdc=Decimal("0"),
            ),
            True,
            Decimal("0.020"),
            Decimal("0.008"),
            Decimal("0.010"),
            Decimal("0.050"),
            Decimal("0.5200"),
            Decimal("0"),
            Decimal("0"),
        ),
        side_disable_reason_by_side={},
        reduce_only_reason=None,
        reduce_only_tail_sell_block=False,
        reduce_only_no_new_sell_last_sec=30,
        forced_sell_only=False,
        min_expected_net_usdc=Decimal("0.001"),
        now_ts=120.0,
        sell_pause_until=0.0,
        is_dry_run_mode=False,
        sellable_qty=Decimal("5.4"),
        maker_exchange_min_shares=Decimal("5.0"),
        avg_entry=Decimal("0.58"),
        emergency_window=False,
        high_cost_exit_cooldown_enabled=False,
        high_cost_exit_cooldown_sec=0.0,
        high_cost_exit_cooldown_until=0.0,
        maker_sell_cost_protect_enabled=True,
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0.005"),
        maker_sell_min_profit_floor_ps=Decimal("0.010"),
        thesis_weakened=True,
        offside_confirmed=False,
        stop_loss_regime_armed=True,
        hold_sec=15.0,
        loss_sell_min_hold_sec=10.0,
        time_left_sec=600.0,
    )

    assert desired_entry["should_quote"] is True
    assert desired_entry["loss_sell_reason"] == "armed_thesis_bad"


def test_preserve_recent_loss_sell_order_holds_higher_existing_price():
    desired_entry = {
        "should_quote": True,
        "price": Decimal("0.40"),
        "loss_sell_reason": "armed_thesis_bad",
        "diag_reason": "armed_thesis_bad",
    }
    existing_state = {
        "price": Decimal("0.50"),
        "created_ts": 100.0,
        "loss_sell_reason": "armed_thesis_bad",
    }

    out = preserve_recent_loss_sell_order(
        desired_entry=desired_entry,
        side="sell",
        existing_state=existing_state,
        now_ts=120.0,
        loss_sell_reprice_min_interval_sec=45.0,
    )

    assert out["price"] == Decimal("0.50")
    assert "loss_sell_reprice_hold" in out["diag_reason"]


def test_profit_cap_clamps_profitable_sell_to_realistic_midgame_price():
    desired_entry = {
        "should_quote": True,
        "price": Decimal("0.8000"),
        "planned_best_bid": Decimal("0.7000"),
        "planned_best_ask": Decimal("0.7100"),
        "diag_reason": "sell_signal",
        "loss_sell_reason": "",
    }

    out = apply_time_based_profitable_sell_cap(
        desired_entry=desired_entry,
        side="sell",
        avg_entry=Decimal("0.5800"),
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0.0050"),
        maker_sell_min_profit_floor_ps=Decimal("0.0100"),
        profitable_sell_cap_enabled=True,
        profitable_sell_cap_passive_offset_ps=Decimal("0.0200"),
        profitable_sell_cap_aggressive_offset_ps=Decimal("0.0100"),
        profitable_sell_cap_taker_offset_ps=Decimal("0.0050"),
        exit_stage_value="PASSIVE",
        tick=Decimal("0.01"),
    )

    assert out["price"] == Decimal("0.7300")
    assert "profit_cap" in out["diag_reason"]
    assert "stage=PASSIVE" in out["diag_reason"]


def test_profit_cap_gets_more_aggressive_in_tail_stage():
    desired_entry = {
        "should_quote": True,
        "price": Decimal("0.8000"),
        "planned_best_bid": Decimal("0.7000"),
        "planned_best_ask": Decimal("0.7100"),
        "diag_reason": "sell_signal",
        "loss_sell_reason": "",
    }

    out = apply_time_based_profitable_sell_cap(
        desired_entry=desired_entry,
        side="sell",
        avg_entry=Decimal("0.5800"),
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0.0050"),
        maker_sell_min_profit_floor_ps=Decimal("0.0100"),
        profitable_sell_cap_enabled=True,
        profitable_sell_cap_passive_offset_ps=Decimal("0.0200"),
        profitable_sell_cap_aggressive_offset_ps=Decimal("0.0100"),
        profitable_sell_cap_taker_offset_ps=Decimal("0.0050"),
        exit_stage_value="AGGRESSIVE",
        tick=Decimal("0.01"),
    )

    assert out["price"] == Decimal("0.7200")
    assert "stage=AGGRESSIVE" in out["diag_reason"]


def test_profit_cap_gets_tightest_in_taker_stage():
    desired_entry = {
        "should_quote": True,
        "price": Decimal("0.8000"),
        "planned_best_bid": Decimal("0.7000"),
        "planned_best_ask": Decimal("0.7100"),
        "diag_reason": "sell_signal",
        "loss_sell_reason": "",
    }

    out = apply_time_based_profitable_sell_cap(
        desired_entry=desired_entry,
        side="sell",
        avg_entry=Decimal("0.5800"),
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0.0050"),
        maker_sell_min_profit_floor_ps=Decimal("0.0100"),
        profitable_sell_cap_enabled=True,
        profitable_sell_cap_passive_offset_ps=Decimal("0.0200"),
        profitable_sell_cap_aggressive_offset_ps=Decimal("0.0100"),
        profitable_sell_cap_taker_offset_ps=Decimal("0.0050"),
        exit_stage_value="TAKER",
        tick=Decimal("0.01"),
    )

    assert out["price"] == Decimal("0.7200")
    assert "stage=TAKER" in out["diag_reason"]


def test_profit_cap_does_not_modify_loss_sell_orders():
    desired_entry = {
        "should_quote": True,
        "price": Decimal("0.8000"),
        "planned_best_bid": Decimal("0.7000"),
        "planned_best_ask": Decimal("0.7100"),
        "diag_reason": "armed_thesis_bad",
        "loss_sell_reason": "armed_thesis_bad",
    }

    out = apply_time_based_profitable_sell_cap(
        desired_entry=desired_entry,
        side="sell",
        avg_entry=Decimal("0.5800"),
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0.0050"),
        maker_sell_min_profit_floor_ps=Decimal("0.0100"),
        profitable_sell_cap_enabled=True,
        profitable_sell_cap_passive_offset_ps=Decimal("0.0200"),
        profitable_sell_cap_aggressive_offset_ps=Decimal("0.0100"),
        profitable_sell_cap_taker_offset_ps=Decimal("0.0050"),
        exit_stage_value="TAKER",
        tick=Decimal("0.01"),
    )

    assert out["price"] == Decimal("0.8000")
    assert out["loss_sell_reason"] == "armed_thesis_bad"


def test_live_shadow_payload_keeps_bias_side_separate_from_candidate_side():
    payload = build_live_signal_compare_payload(
        slug="btc-updown-15m-test",
        spot=Decimal("100.2"),
        strike=Decimal("100.0"),
        sigma=Decimal("0.2"),
        time_left_sec=769.9,
        history=[
            (700.0, Decimal("100.0")),
            (730.0, Decimal("100.02")),
            (760.0, Decimal("100.04")),
        ],
        now_ts=770.0,
        active_side_value="UP",
        active_side_locked=True,
        side_score=Decimal("0.28"),
        side_reason="cs=+0.28",
        ask_up=Decimal("0.97"),
        ask_down=Decimal("0.20"),
        bid_up=Decimal("0.96"),
        bid_down=Decimal("0.19"),
        cfg=ShadowSignalConfig(
            min_edge=Decimal("0.04"),
            min_prob_band=Decimal("0.08"),
            max_prob_band=Decimal("0.99"),
            shadow_score_min_abs=0.05,
            strike_z_scale=10.0,
            ret_10_bps_scale=500.0,
            ret_30_bps_scale=800.0,
        ),
    )

    assert payload["shadow_bias_side"] == "UP"
    assert payload["shadow_candidate_side"] == "BUY_DOWN"
