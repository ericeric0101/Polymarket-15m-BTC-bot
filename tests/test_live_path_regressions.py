from __future__ import annotations

import asyncio
import math
import time
from decimal import Decimal
from types import SimpleNamespace

from bot.adapter_overrides import (
    build_transport_heartbeat_quote,
    coalesce_price_changes_by_asset,
    install_runtime_compatibility_overrides,
    position_fetch_retry_delay_sec,
    record_quote_data_engine_queue_depth,
    quote_provenance_for_tick,
    record_quote_provenance,
    retain_latest_quote,
    should_emit_quote_heartbeat,
    should_publish_order_book_deltas,
    should_emit_transport_heartbeat,
)
from bot.app_config import AppConfig
from bot.entry_quality import evaluate_entry_quality_adjustment
from bot.execution_events import is_benign_cancel_reject_reason, reconcile_benign_cancel_reject
from bot.edge_observation import build_quote_age_telemetry
from bot.enums import ActiveSide, MarketPhase
from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.fill_ledger import FillLedgerMixin, classify_fill_liquidity
from bot.lifecycle_runtime import StrategyLifecycleMixin
from bot.lifecycle import resolve_bi_side_market_selection
from bot.market_runtime import (
    handle_quote_tick,
    quote_event_is_fresh,
    quote_tick_adapter_timestamp,
    quote_transport_is_fresh,
    refresh_quote_tick_subscriptions,
)
from bot.market_cycle_state import MarketCycleState, bind_market_cycle_state
from bot.process_lock import ProcessLock
from bot.ops import should_run_quote_watchdog
from bot.launcher import _strategy_requested_rollover
from bot.pricing_runtime import PricingRuntimeMixin
from bot.models import DecisionPhase, DecisionRegime, ExitDecisionType, MarketSnapshot, PositionState, QuoteMode, SignalDecision
from bot.position_manager import PositionManager, PositionManagerConfig
from bot.price_streams import (
    build_polymarket_chainlink_subscribe_payload,
    extract_polymarket_chainlink_tick,
)
from bot.quoting import apply_quote_plan_guards
from bot.quote_service import (
    apply_forced_exit_sell_pricing,
    apply_entry_quality_quote_placement,
    apply_shadow_entry_veto,
    apply_high_entry_price_size_adjustment,
    apply_weak_pfair_size_adjustment,
    apply_reload_edge_guard,
    build_desired_quote_entry,
    compute_loss_sell_policy,
    build_directional_snapshot,
    evaluate_buy_entry_controls,
    maybe_apply_continuation_entry,
    maybe_apply_trapped_inventory_recovery,
    preserve_recent_loss_sell_order,
    reconcile_unwanted_quotes,
    resolve_quote_intent_state,
    retreat_crossing_buy_quote,
    should_requote_existing_order,
)
from bot.recovery import StrategyRecoveryMixin
from bot.order_submission import submit_maker_quote
from bot.shadow_signal import (
    ShadowSignalConfig,
    attach_forecast_snapshot_telemetry,
    build_entry_regime_observation_payload,
    build_live_signal_compare_payload,
    select_shadow_candidate,
)
from bot.spot_pricer import SpotPricerMixin
from bot.side_decision import SideDecisionMixin
from bot.taker_exit import TakerExitMixin
from execution.exit_policy import ExitStage
from monitoring.trade_journal_db import TradeJournalDB
from run_bot import IntegratedBTCStrategy


class DummyOrder:
    def __init__(self, client_order_id: str) -> None:
        self.client_order_id = client_order_id


def test_coalesce_price_changes_keeps_asset_order_and_all_book_updates():
    changes = [
        SimpleNamespace(asset_id="up", price="0.50"),
        SimpleNamespace(asset_id="down", price="0.49"),
        SimpleNamespace(asset_id="up", price="0.51"),
        SimpleNamespace(asset_id="down", price="0.48"),
    ]

    groups = coalesce_price_changes_by_asset(changes)

    assert [[change.price for change in group] for group in groups] == [
        ["0.50", "0.51"],
        ["0.49", "0.48"],
    ]


def test_retain_latest_quote_replaces_undelivered_tick_per_instrument():
    pending: dict[str, object] = {}
    first = SimpleNamespace(instrument_id="up-token", bid_price="0.50")
    second = SimpleNamespace(instrument_id="up-token", bid_price="0.51")
    other = SimpleNamespace(instrument_id="down-token", bid_price="0.49")

    retain_latest_quote(pending, first)
    retain_latest_quote(pending, other)
    retain_latest_quote(pending, second)

    assert list(pending) == ["up-token", "down-token"]
    assert pending["up-token"] is second
    assert pending["down-token"] is other


def test_position_fetch_retries_only_http_429_with_bounded_backoff():
    assert position_fetch_retry_delay_sec(RuntimeError("HTTP 429: Failed to fetch positions"), 0) == 1.0
    assert position_fetch_retry_delay_sec(RuntimeError("HTTP 429: Failed to fetch positions"), 3) == 8.0
    assert position_fetch_retry_delay_sec(RuntimeError("HTTP 500: Failed to fetch positions"), 0) is None


def test_order_book_deltas_publish_only_when_explicitly_subscribed():
    assert should_publish_order_book_deltas(has_delta_subscription=True)
    assert not should_publish_order_book_deltas(has_delta_subscription=False)


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
        self.live_inventory_cost = {}
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
        self.market_buy_count_total_by_slug = {}
        self.market_buy_counted_order_ids_by_slug = {}
        self._thesis_epoch_by_slug = {}
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

    def _clear_pending_taker_exit_for_order(self, client_order_id):
        return None

    def _db_strategy_event(self, event_type, payload):
        self.strategy_events.append((event_type, payload))

    def _db_order_event(self, **payload):
        self.order_events.append(payload)

    def _update_terminal_dashboard_snapshot(self):
        return None

    def _apply_post_fill_followup(self, **kwargs):
        return None

    def _start_maker_worker(self, *args, **kwargs):
        return None

    def _increment_order_metric(self, *_args, **_kwargs):
        self.order_metric_count += 1

    def _update_inventory_metric(self):
        self.inventory_metric_count += 1


def test_position_manager_decision_state_holds_trend_position():
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
    now_ts = time.time()
    state = manager.compute_decision_state(
        inst_key="inst-up",
        now_ts=now_ts,
        qty=Decimal("5.4"),
        opened_ts=now_ts - 90.0,
        held_side="UP",
        active_side="UP",
        signal_score=Decimal("0.62"),
        signal_matches_position=True,
        current_price=Decimal("69260"),
        price_to_beat=Decimal("69213"),
        best_bid=Decimal("0.74"),
        best_ask=Decimal("0.75"),
        fair=Decimal("0.81"),
        time_left_sec=420.0,
        avg_entry=Decimal("0.69"),
        peak_bid=Decimal("0.76"),
        peak_fair=Decimal("0.84"),
    )
    assert state.regime == DecisionRegime.TREND
    assert state.phase in {DecisionPhase.HOLD, DecisionPhase.PROBE}
    assert state.pressure > Decimal("0")


def test_position_manager_decision_state_exits_broken_wrong_side_position():
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
    now_ts = time.time()
    state = manager.compute_decision_state(
        inst_key="inst-up",
        now_ts=now_ts,
        qty=Decimal("5.4"),
        opened_ts=now_ts - 120.0,
        held_side="UP",
        active_side="DOWN",
        signal_score=Decimal("-0.56"),
        signal_matches_position=False,
        current_price=Decimal("69100"),
        price_to_beat=Decimal("69213"),
        best_bid=Decimal("0.44"),
        best_ask=Decimal("0.45"),
        fair=Decimal("0.41"),
        time_left_sec=480.0,
        avg_entry=Decimal("0.72"),
        peak_bid=Decimal("0.78"),
        peak_fair=Decimal("0.80"),
    )
    assert state.regime == DecisionRegime.BROKEN
    assert state.phase == DecisionPhase.EXIT
    assert state.pressure < Decimal("0")


def test_position_manager_decision_state_does_not_exit_winner_pullback_when_thesis_matches():
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
    now_ts = time.time()
    state = manager.compute_decision_state(
        inst_key="inst-up",
        now_ts=now_ts,
        qty=Decimal("5.4"),
        opened_ts=now_ts - 105.0,
        held_side="UP",
        active_side="UP",
        signal_score=Decimal("-0.04"),
        signal_matches_position=True,
        current_price=Decimal("71966.74"),
        price_to_beat=Decimal("71906.80"),
        best_bid=Decimal("0.62"),
        best_ask=Decimal("0.63"),
        fair=Decimal("0.6643"),
        time_left_sec=299.0,
        avg_entry=Decimal("0.66"),
        peak_bid=Decimal("0.80"),
        peak_fair=Decimal("0.9059"),
    )

    assert state.regime == DecisionRegime.CHOP
    assert state.phase != DecisionPhase.EXIT
    assert state.spot_minus_strike_bps is not None
    assert state.spot_minus_strike_bps > Decimal("0")


def test_position_manager_chop_regime_does_not_de_risk_held_position_by_itself():
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
    now_ts = time.time()
    state = manager.compute_decision_state(
        inst_key="inst-up",
        now_ts=now_ts,
        qty=Decimal("5.4"),
        opened_ts=now_ts - 180.0,
        held_side="UP",
        active_side="UP",
        signal_score=Decimal("-0.03"),
        signal_matches_position=True,
        current_price=Decimal("71478.40"),
        price_to_beat=Decimal("71473.53"),
        best_bid=Decimal("0.57"),
        best_ask=Decimal("0.58"),
        fair=Decimal("0.54"),
        time_left_sec=520.0,
        avg_entry=Decimal("0.58"),
        peak_bid=Decimal("0.77"),
        peak_fair=Decimal("0.69"),
    )

    assert state.regime == DecisionRegime.CHOP
    assert state.phase != DecisionPhase.DE_RISK


def test_position_manager_marks_near_strike_entry_as_chop_even_with_positive_edge():
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

    state = manager.compute_decision_state(
        inst_key="inst-down",
        now_ts=time.time(),
        qty=Decimal("0"),
        opened_ts=0.0,
        held_side="DOWN",
        active_side="DOWN",
        signal_score=Decimal("-0.24"),
        signal_matches_position=True,
        current_price=Decimal("71137.04"),
        price_to_beat=Decimal("71168.86"),
        best_bid=Decimal("0.58"),
        best_ask=Decimal("0.59"),
        fair=Decimal("0.6393"),
        time_left_sec=780.0,
        avg_entry=Decimal("0"),
    )

    assert state.regime == DecisionRegime.CHOP
    assert state.edge > Decimal("0")


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
        self.recovery_exit_stage_by_inst = {}
        self.tail_exit_calls = []

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

    def _get_effective_sellable_qty(self, instrument_id=None):
        return self.inventory_delta_shares

    def _get_confirmed_inventory_qty_for_instrument(self, instrument_id=None):
        return self.inventory_delta_shares

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

    def _submit_taker_exit_order(self, **kwargs):
        self.tail_exit_calls.append(kwargs)
        return True


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
        self.maker_kill_switch = False
        self.kill_switch_reason = ""

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

    def _activate_maker_kill_switch(self, reason):
        self.maker_kill_switch = True
        self.kill_switch_reason = reason


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
        self.bi_side_flip_fair_inversion_min_ps = Decimal("0.03")
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
        self.market_strike_provisional_by_slug = {}
        self.market_strike_provisional_source_by_slug = {}
        self._strike_pending_log_state_by_slug = {}
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
        self.polymarket_chainlink_twap_enabled = True
        self.external_spot_history = [(1000.1, Decimal("66602.20"))]
        self.external_spot_history_max = 1200
        self.latest_external_spot = Decimal("66630.00")
        self.latest_external_spot_source = "polymarket_chainlink_ws"
        self.latest_external_spot_source_ts = 1000.2
        self.current_market_slug = "btc-updown-15m-test"
        self._logged_first_spot = False
        self.strategy_events = []
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

    def _db_strategy_event(self, event_type, payload=None):
        self.strategy_events.append((event_type, payload or {}))


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

    def _current_thesis_epoch(self, slug):
        return int(self._thesis_epoch_by_slug.get(str(slug or ""), 0))

    def _market_buy_budget_key(self, slug):
        slug_key = str(slug or "")
        return f"{slug_key}:{self._current_thesis_epoch(slug_key)}" if slug_key else ""

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

    assert strategy.market_buy_count_by_slug["btc-updown-15m-test:0"] == 1
    assert strategy.market_buy_count_total_by_slug["btc-updown-15m-test"] == 1
    assert strategy.market_buy_counted_order_ids_by_slug["btc-updown-15m-test:0"] == {"BUY-1"}
    count_events = [evt for evt, _ in strategy.strategy_events if evt == "MARKET_BUY_COUNT_UPDATED"]
    assert count_events == ["MARKET_BUY_COUNT_UPDATED"]


def test_startup_rehydrate_restores_inventory_and_forces_sell_only():
    strategy = DummyStrategyForRehydrate()

    IntegratedBTCStrategy._rehydrate_inventory_state_on_startup(strategy)

    assert strategy.inventory_delta_shares == Decimal("5.2")
    assert strategy._startup_rehydrated_inventory_force_sell_only is True
    assert strategy.live_inventory_cost["inst-up"]["avg_entry_price"] == Decimal("0.37")
    assert strategy.strategy_events[0][0] == "STARTUP_INVENTORY_REHYDRATED"


def test_startup_rehydrate_recovers_cost_basis_from_recent_buy_submit(tmp_path):
    class SubmitFallbackStrategy(StrategyRecoveryMixin):
        def __init__(self) -> None:
            self.trade_db = TradeJournalDB(str(tmp_path / "journal.db"))

    strategy = SubmitFallbackStrategy()
    inst = "condition-token.POLYMARKET"
    strategy.trade_db.log_order_event(
        run_id="test",
        event_type="ORDER_SUBMIT",
        side="BUY",
        price=0.64,
        qty=10.8,
        status="SUBMITTED",
        instrument_id="current-primary-inst",
        payload={"submitted_instrument_id": inst},
    )

    state = strategy._rebuild_inventory_state_from_recent_buy_submit(
        inst_key=inst,
        target_qty=Decimal("10.8"),
        cutoff="2026-01-01T00:00:00+00:00",
    )

    assert state is not None
    assert state["qty"] == Decimal("10.8")
    assert state["avg_entry_price"] == Decimal("0.64")


def test_market_selection_honors_preferred_current_slug_even_if_cache_closed_flag_is_stale():
    now_ts = int(time.time())
    current_start = now_ts - 20
    future_start = current_start + 900
    preferred_slug = f"btc-updown-15m-{current_start}"
    future_slug = f"btc-updown-15m-{future_start}"

    def make_item(slug: str, outcome: str, start_ts: int, closed: bool) -> dict:
        return {
            "instrument": SimpleNamespace(
                id=f"{slug}-{outcome}",
                info={
                    "question": "Bitcoin Up or Down",
                    "market_slug": slug,
                    "active": True,
                    "closed": closed,
                },
            ),
            "slug": slug,
            "market_timestamp": start_ts,
            "end_timestamp": start_ts + 900,
            "question": "bitcoin",
            "active": True,
            "closed": closed,
            "time_diff_minutes": (start_ts - now_ts) / 60,
        }

    selection, _, _, _ = resolve_bi_side_market_selection(
        btc_instruments=[
            make_item(preferred_slug, "up", current_start, True),
            make_item(preferred_slug, "down", current_start, True),
            make_item(future_slug, "up", future_start, False),
        ],
        current_timestamp=now_ts,
        extract_outcome=lambda instrument: "down" if str(instrument.id).endswith("-down") else "up",
        preferred_slug=preferred_slug,
    )

    assert selection is not None
    assert selection.current_market_slug == preferred_slug


def test_ghost_inventory_reconcile_recovers_cost_basis_from_recent_buy_order():
    strategy = DummySellableQtyStrategy()
    strategy.live_inventory_cost = {}
    strategy.inventory_delta_shares = Decimal("0")
    strategy.active_maker_orders = {
        "buy": {
            "side": "buy",
            "instrument_id": "inst-down",
            "price": Decimal("0.64"),
            "quantity": Decimal("10.8"),
            "created_ts": time.time(),
            "order": SimpleNamespace(client_order_id="BUY-1"),
        }
    }
    strategy.strategy_events = []
    strategy._db_strategy_event = lambda event_type, payload=None: strategy.strategy_events.append((event_type, payload or {}))

    restored = IntegratedBTCStrategy._reconcile_ghost_inventory(
        strategy,
        instrument_id="inst-down",
        confirmed_qty=Decimal("0"),
        onchain_qty=Decimal("10.8"),
    )

    assert restored == Decimal("10.8")
    assert strategy.live_inventory_cost["inst-down"]["avg_entry_price"] == Decimal("0.64")
    assert strategy.strategy_events[-1][1]["avg_entry_recovered"] is True


def test_ghost_inventory_reconcile_clears_stale_sell_state_before_requoting():
    strategy = DummySellableQtyStrategy()
    strategy.live_inventory_cost = {}
    strategy.inventory_delta_shares = Decimal("0")
    strategy.active_maker_orders = {
        "sell": {
            "side": "sell",
            "instrument_id": "inst-down",
            "order": SimpleNamespace(client_order_id="SELL-1"),
        },
        "other-market-sell": {
            "side": "sell",
            "instrument_id": "inst-up",
            "order": SimpleNamespace(client_order_id="SELL-2"),
        },
    }
    strategy.strategy_events = []
    strategy._db_strategy_event = lambda event_type, payload=None: strategy.strategy_events.append((event_type, payload or {}))

    restored = IntegratedBTCStrategy._reconcile_ghost_inventory(
        strategy,
        instrument_id="inst-down",
        confirmed_qty=Decimal("0"),
        onchain_qty=Decimal("5.4"),
    )

    assert restored == Decimal("5.4")
    assert strategy.inventory_delta_shares == Decimal("5.4")
    assert "sell" not in strategy.active_maker_orders
    assert "other-market-sell" in strategy.active_maker_orders
    assert strategy.strategy_events[-1][1]["cleared_sell_orders"] == ["SELL-1"]


def test_polymarket_user_trade_decimal_fallback_handles_empty_maker_fields():
    install_runtime_compatibility_overrides()
    from nautilus_trader.adapters.polymarket.common.enums import PolymarketEventType
    from nautilus_trader.adapters.polymarket.common.enums import PolymarketLiquiditySide
    from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderSide
    from nautilus_trader.adapters.polymarket.common.enums import PolymarketTradeStatus
    from nautilus_trader.adapters.polymarket.schemas.order import PolymarketMakerOrder
    from nautilus_trader.adapters.polymarket.schemas.user import PolymarketUserTrade

    trade = PolymarketUserTrade(
        asset_id="taker-token",
        bucket_index=0,
        fee_rate_bps="",
        id="trade-1",
        last_update="0",
        maker_address="maker",
        maker_orders=[
            PolymarketMakerOrder(
                asset_id="maker-token",
                fee_rate_bps="",
                maker_address="maker",
                matched_amount="",
                order_id="maker-order-1",
                outcome="Up",
                owner="api-key",
                price="",
            )
        ],
        market="condition-1",
        match_time="0",
        outcome="Up",
        owner="api-key",
        price="0.64",
        side=PolymarketOrderSide.BUY,
        size="10.8",
        status=PolymarketTradeStatus.CONFIRMED,
        taker_order_id="taker-order-1",
        timestamp="0",
        trade_owner="api-key",
        trader_side=PolymarketLiquiditySide.MAKER,
        type=PolymarketEventType.TRADE,
    )

    assert trade.last_px("maker-order-1") == Decimal("0.64")
    assert trade.last_qty("maker-order-1") == Decimal("10.8")
    assert trade.get_fee_rate_bps("maker-order-1") == Decimal("0")


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


def test_geoblock_rejection_stops_maker_quoting_without_retrying():
    strategy = DummyRejectRecoveryStrategy()
    event = SimpleNamespace(
        client_order_id="BTC-15M-MAKER-BUY-1",
        reason=(
            "PolyApiException[status_code=403, error_message={'error': "
            "'Trading restricted in your region, please refer to available regions - "
            "https://docs.polymarket.com/developers/CLOB/geoblock'}]"
        ),
        instrument_id="cond-123-456.POLYMARKET",
        order_side="BUY",
        venue_order_id=None,
    )

    IntegratedBTCStrategy._handle_order_rejection_like_event(strategy, event, title="ORDER REJECTED")

    assert strategy.maker_kill_switch is True
    assert "trading restricted" in strategy.kill_switch_reason.lower()
    assert strategy.order_events[-1]["event_type"] == "ORDER_REJECTED"


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


def test_pre_entry_flip_is_allowed_but_inventory_disables_it():
    flat = DummySideFlipStrategy(held_qty=Decimal("0"))
    flat.bi_side_allow_pre_entry_flip = True
    assert flat._pre_entry_flip_allowed(proposed_side=ActiveSide.DOWN) is True

    held = DummySideFlipStrategy(held_qty=Decimal("0.01"))
    held.bi_side_allow_pre_entry_flip = True
    assert held._pre_entry_flip_allowed(proposed_side=ActiveSide.DOWN) is False


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


def test_new_signal_flip_requires_fair_inversion_margin():
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
                "fair_up": 0.61,
                "fair_down": 0.60,
            },
        )

    strategy._compute_side_decision = _compute_side_decision

    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=100.0, phase=MarketPhase.ACTIVE))
    asyncio.run(strategy._maybe_finalize_side_decision(now_ts=101.0, phase=MarketPhase.ACTIVE))

    assert strategy.active_side == ActiveSide.DOWN
    assert strategy.side_flip_count == 0


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
    strategy.market_strike_crypto_price_last_try_ts_by_slug = {
        "btc-updown-15m-test": time.time(),
    }

    strike = asyncio.run(strategy._get_market_strike_for_instrument("inst-up"))

    assert strike is None
    assert strategy.market_strike_provisional_by_slug["btc-updown-15m-test"] == Decimal("66625.19")
    assert "btc-updown-15m-test" not in strategy.market_strike_cache_by_slug


def test_strike_uses_published_polymarket_price_to_beat(monkeypatch):
    strategy = DummySpotPricerStrategy()

    async def _published_ptb(**_kwargs):
        return Decimal("66611.11")

    monkeypatch.setattr("bot.spot_pricer.fetch_crypto_price_to_beat", _published_ptb)

    strike = asyncio.run(strategy._get_market_strike_for_instrument("inst-up"))

    assert strike == Decimal("66611.11")
    assert strategy.market_strike_source_by_slug["btc-updown-15m-test"] == "polymarket_crypto_price_open"


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
        quote_sides_mode="both",
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
        quote_sides_mode="both",
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
        quote_sides_mode="both",
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


def test_exit_policy_unified_continue_holds_strong_winner_when_fair_edge_is_large():
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
            early_profit_hold_enabled=True,
            early_profit_hold_min_hold_sec=60,
            early_profit_hold_max_profit_ps=Decimal("0.08"),
            profit_run_enabled=True,
            profit_run_min_hold_sec=20,
            profit_run_min_profit_ps=Decimal("0.04"),
            profit_run_min_score_abs=Decimal("0.12"),
            profit_run_trailing_drawdown_ps=Decimal("0.05"),
            profit_run_unlock_profit_ps=Decimal("0.18"),
            profit_run_unlock_trailing_drawdown_ps=Decimal("0.02"),
            recycle_locked_side_min_fair_edge_ps=Decimal("0.04"),
        )
    )
    snapshot = MarketSnapshot(
        instrument_id="inst-up",
        phase=MarketPhase.ACTIVE.value,
        time_left_sec=260.0,
        best_bid=Decimal("0.62"),
        best_ask=Decimal("0.63"),
        fee_rate=Decimal("0"),
        spread=Decimal("0.01"),
        spread_pct=Decimal("0.016"),
        slippage_buffer_pct=Decimal("0"),
        exit_stage=ExitStage.PASSIVE,
        in_reduce_only_tail=False,
        stop_loss_disabled_in_tail=False,
        fair=Decimal("0.93"),
        fair_edge_ps=Decimal("0.31"),
        spot_minus_strike_bps=Decimal("12"),
    )
    position = PositionState(
        instrument_id="inst-up",
        qty=Decimal("5.40"),
        sellable_qty=Decimal("5.40"),
        avg_entry_price=Decimal("0.60"),
        entry_fee_remaining=Decimal("0"),
        hold_sec=120.0,
        stop_loss_confirm_hits=0,
        held_side=ActiveSide.UP.value,
        peak_bid=Decimal("0.62"),
        peak_fair=Decimal("0.93"),
    )
    signal = SignalDecision(
        active_side=ActiveSide.UP.value,
        score=Decimal("0.38"),
        locked=True,
        reason="cs=+0.38",
        matches_position=True,
    )

    decision = engine.evaluate(snapshot, position, signal)

    assert decision.decision_type == ExitDecisionType.HOLD_IN_BAND
    assert decision.reason == "recycle_locked_side_hold"
    assert decision.metadata["exit_intent"] == "continue"


def test_exit_policy_unified_de_risk_allows_profitable_soft_winner_to_sell():
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
            early_profit_hold_enabled=True,
            early_profit_hold_min_hold_sec=60,
            early_profit_hold_max_profit_ps=Decimal("0.08"),
            profit_run_enabled=True,
            profit_run_min_hold_sec=20,
            profit_run_min_profit_ps=Decimal("0.04"),
            profit_run_min_score_abs=Decimal("0.12"),
            profit_run_trailing_drawdown_ps=Decimal("0.05"),
            profit_run_unlock_profit_ps=Decimal("0.18"),
            profit_run_unlock_trailing_drawdown_ps=Decimal("0.02"),
            recycle_locked_side_min_fair_edge_ps=Decimal("0.04"),
        )
    )
    snapshot = MarketSnapshot(
        instrument_id="inst-up",
        phase=MarketPhase.ACTIVE.value,
        time_left_sec=260.0,
        best_bid=Decimal("0.62"),
        best_ask=Decimal("0.63"),
        fee_rate=Decimal("0"),
        spread=Decimal("0.01"),
        spread_pct=Decimal("0.016"),
        slippage_buffer_pct=Decimal("0"),
        exit_stage=ExitStage.PASSIVE,
        in_reduce_only_tail=False,
        stop_loss_disabled_in_tail=False,
        fair=Decimal("0.64"),
        fair_edge_ps=Decimal("0.02"),
        spot_minus_strike_bps=Decimal("4"),
    )
    position = PositionState(
        instrument_id="inst-up",
        qty=Decimal("5.40"),
        sellable_qty=Decimal("5.40"),
        avg_entry_price=Decimal("0.60"),
        entry_fee_remaining=Decimal("0"),
        hold_sec=120.0,
        stop_loss_confirm_hits=0,
        held_side=ActiveSide.UP.value,
        peak_bid=Decimal("0.62"),
        peak_fair=Decimal("0.64"),
    )
    signal = SignalDecision(
        active_side=ActiveSide.UP.value,
        score=Decimal("0.08"),
        locked=True,
        reason="cs=+0.08",
        matches_position=True,
    )

    decision = engine.evaluate(snapshot, position, signal)

    assert decision.decision_type == ExitDecisionType.DE_RISK
    assert decision.metadata["exit_intent"] == "de_risk"


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


def test_recycle_locked_side_hold_holds_when_fair_edge_remains_large():
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
            recycle_locked_side_min_fair_edge_ps=Decimal("0.04"),
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
    assert reason.startswith("recycle_locked_side_hold")


def test_recycle_hold_latch_holds_even_after_short_term_signal_softens():
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
            recycle_locked_side_min_fair_edge_ps=Decimal("0.04"),
        )
    )

    first_hold, first_reason = IntegratedBTCStrategy._should_hold_profitable_position(
        strategy,
        instrument_id="inst-down",
        best_bid=Decimal("0.74"),
        fair=Decimal("0.80"),
        avg_entry=Decimal("0.6206"),
        time_left_sec=420.0,
        thesis_weakened=False,
        offside_confirmed=False,
    )

    second_hold, second_reason = IntegratedBTCStrategy._should_hold_profitable_position(
        strategy,
        instrument_id="inst-down",
        best_bid=Decimal("0.65"),
        fair=Decimal("0.66"),
        avg_entry=Decimal("0.6206"),
        time_left_sec=405.0,
        thesis_weakened=False,
        offside_confirmed=False,
    )

    assert first_hold is True
    assert first_reason.startswith("recycle_locked_side_hold")
    assert second_hold is True
    assert second_reason.startswith("recycle_hold_latch")


def test_resolve_quote_intent_state_emits_hard_exit_and_acquire_modes():
    hard_exit = resolve_quote_intent_state(
        side="sell",
        desired_should_quote=True,
        tail_inventory_exit_context=False,
        adverse_exit_context=True,
        stop_loss_pending_active=False,
        recycle_sell_ready=False,
        recycle_profit_candidate=False,
        active_side_locked=True,
        active_side_value="UP",
        inst_id="inst-up",
        active_instrument_id="inst-up",
        locked_side_entry_blocked=False,
    )
    acquire = resolve_quote_intent_state(
        side="buy",
        desired_should_quote=True,
        tail_inventory_exit_context=False,
        adverse_exit_context=False,
        stop_loss_pending_active=False,
        recycle_sell_ready=False,
        recycle_profit_candidate=False,
        active_side_locked=True,
        active_side_value="UP",
        inst_id="inst-up",
        active_instrument_id="inst-up",
        locked_side_entry_blocked=False,
    )

    assert hard_exit.quote_mode == QuoteMode.HARD_EXIT
    assert hard_exit.sell_intent == "FORCED_EXIT"
    assert hard_exit.hard_exit_allowed is True
    assert acquire.quote_mode == QuoteMode.ACQUIRE_LOCKED_SIDE


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
        market_buy_count=1,
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
        market_buy_count=1,
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
        market_buy_count=1,
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


def test_buy_submit_quote_guard_allows_recent_planned_snapshot_with_relaxed_limit():
    strategy = DummyBuyDriftStrategy()
    strategy.maker_buy_planned_quote_max_age_sec = 10.0

    should_skip = IntegratedBTCStrategy._should_skip_buy_submit_for_quote_drift(
        strategy,
        instrument_id="inst-down",
        quote_now=(Decimal("0.42"), Decimal("0.43")),
        directional_snapshot={
            "planned_best_bid": Decimal("0.42"),
            "planned_best_ask": Decimal("0.43"),
            "planned_quote_ts": time.time() - 2.0,
        },
        instrument=None,
    )

    assert should_skip is False


def test_buy_submit_quote_guard_rejects_old_cached_top_of_book():
    strategy = DummyBuyDriftStrategy()
    strategy.maker_buy_planned_quote_max_age_sec = 10.0
    strategy.last_quote_received_ts_by_inst = {"inst-down": time.time() - 10.1}

    should_skip = IntegratedBTCStrategy._should_skip_buy_submit_for_quote_drift(
        strategy,
        instrument_id="inst-down",
        quote_now=(Decimal("0.42"), Decimal("0.43")),
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
    assert early.reason.startswith("state_machine_hold")
    assert "legacy(entry=1,thesis=0,pending=1)" in early.reason

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
    assert armed.status == "hold"
    assert armed.reason.startswith("state_machine_hold")
    assert "legacy(entry=0,thesis=0,pending=0)" in armed.reason

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
    assert decision.reason.startswith("state_machine_hold")
    assert "legacy(entry=0,thesis=1,pending=0)" in decision.reason


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
    assert decision.reason.startswith("state_machine_hold")
    assert "pending=1" in decision.reason


def test_app_config_reads_extended_env(monkeypatch):
    monkeypatch.setenv("MAKER_REQUOTE_MIN_AGE_SEC", "9")
    monkeypatch.setenv("MAKER_DIGITAL_SIGMA_DEFAULT", "0.77")
    monkeypatch.setenv("AUTO_REDEEM_ENABLED", "1")
    monkeypatch.setenv("TRADE_DB_PATH", "./logs/custom.db")
    monkeypatch.setenv("REGIME_GUARD_N_MARKETS", "6")
    monkeypatch.setenv("POLYMARKET_CHAINLINK_TWAP_WINDOW_SEC", "60")
    monkeypatch.setenv("TAKER_EXIT_ONLY_AFTER_INVALIDATION", "1")
    monkeypatch.setenv("TAKER_EXIT_MAX_TIME_LEFT_SEC", "720")
    monkeypatch.setenv("TAKER_EXIT_MIN_HOLD_SEC", "120")
    monkeypatch.setenv("TAKER_EXIT_MIN_RECOVERY_RATIO", "0.50")
    monkeypatch.setenv("QUOTE_EVENT_CLOCK_SKEW_TOLERANCE_SEC", "0.25")
    monkeypatch.setenv("SHADOW_SIMULATION_FILL_TIMEOUT_SEC", "75")
    monkeypatch.setenv("SHADOW_SIMULATION_MAX_QUOTE_AGE_SEC", "1.5")
    monkeypatch.setenv("SHADOW_SIMULATION_AGED_QUOTE_MAX_AGE_SEC", "20")
    monkeypatch.setenv("MAKER_BUY_PLANNED_QUOTE_MAX_AGE_SEC", "10")

    cfg = AppConfig.from_env(enable_terminal_dashboard=False)

    assert cfg.maker.requote_min_age_sec == 9.0
    assert cfg.maker.digital_sigma_default == Decimal("0.77")
    assert cfg.operations.auto_redeem_enabled is True
    assert cfg.operations.trade_db_path == "./logs/custom.db"
    assert cfg.risk.regime_guard_n_markets == 6
    assert cfg.market_data.polymarket_chainlink_twap_enabled is True
    assert cfg.market_data.polymarket_chainlink_twap_window_sec == 60
    assert cfg.market_data.require_twap_reference_spot is True
    assert cfg.exit.taker_exit_only_after_invalidation is True
    assert cfg.exit.taker_exit_max_time_left_sec == 720
    assert cfg.exit.taker_exit_min_hold_sec == 120
    assert cfg.exit.taker_exit_min_recovery_ratio == Decimal("0.50")
    assert cfg.market_data.quote_event_clock_skew_tolerance_sec == Decimal("0.25")
    assert cfg.operations.shadow_simulation_enabled is True
    assert cfg.operations.shadow_simulation_fill_timeout_sec == 75.0
    assert cfg.operations.shadow_simulation_max_quote_age_sec == 1.5
    assert cfg.operations.shadow_simulation_aged_quote_max_age_sec == 20.0
    assert cfg.maker.buy_planned_quote_max_age_sec == 10.0


def test_edge_observation_quote_age_uses_tolerance_only_for_small_future_event_time():
    normal = build_quote_age_telemetry(observation_ts=100.2, quote_ts=100.0)
    tolerated = build_quote_age_telemetry(
        observation_ts=100.0,
        quote_ts=100.2,
        clock_skew_tolerance_sec=Decimal("0.25"),
    )
    invalid = build_quote_age_telemetry(
        observation_ts=100.0,
        quote_ts=100.3,
        clock_skew_tolerance_sec=Decimal("0.25"),
    )

    assert normal.raw_age_sec == Decimal("0.2")
    assert normal.effective_age_sec == Decimal("0.2")
    assert normal.tolerance_applied is False
    assert tolerated.raw_age_sec == Decimal("-0.2")
    assert tolerated.effective_age_sec == Decimal("0")
    assert tolerated.clock_skew_sec == Decimal("0.2")
    assert tolerated.tolerance_applied is True
    assert invalid.effective_age_sec is None
    assert invalid.tolerance_applied is False


def test_process_lock_rejects_second_local_owner(tmp_path):
    lock_path = tmp_path / "bot.lock"
    first = ProcessLock(lock_path)
    second = ProcessLock(lock_path)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_benign_cancel_reject_recognizes_already_canceled_exchange_reply():
    assert is_benign_cancel_reject_reason("{'order': 'the order is already canceled'}")
    assert is_benign_cancel_reject_reason("matched orders can't be canceled")
    assert not is_benign_cancel_reject_reason("insufficient balance")


def test_benign_cancel_reject_clears_order_without_order_object():
    active_orders = {
        "sell:inst-up": {
            "client_order_id": "BTC-15M-MAKER-SELL-1",
            "order": None,
        }
    }

    assert reconcile_benign_cancel_reject("BTC-15M-MAKER-SELL-1", active_orders)
    assert active_orders == {}


def test_polymarket_chainlink_twap_subscribe_payload_uses_60s_topic():
    payload = build_polymarket_chainlink_subscribe_payload(
        use_twap=True,
        window_seconds=60,
        symbol="BTC/USD",
    )

    sub = payload["subscriptions"][0]
    assert sub["topic"] == "crypto_prices_twap_sixty"
    assert sub["type"] == "update"
    assert sub["filters"] == '{"symbol":"btc/usd"}'


def test_extract_polymarket_chainlink_twap_tick_prefers_full_accuracy_e18():
    tick = extract_polymarket_chainlink_tick(
        {
            "topic": "crypto_prices_twap_sixty",
            "type": "update",
            "timestamp": 1785178800123,
            "payload": {
                "symbol": "btc/usd",
                "value": 65000.0,
                "full_accuracy_value": "65000500000000000000000",
                "timestamp": 1785178800000,
                "window_s": 60,
            },
        }
    )

    assert tick is not None
    assert tick.price == Decimal("65000.5")
    assert tick.updated_at_ms == 1785178800000
    assert tick.window_seconds == 60
    assert tick.source == "polymarket_chainlink_twap_60s_ws"


def test_runtime_compatibility_overrides_install():
    install_runtime_compatibility_overrides()

    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
    from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
    import py_clob_client_v2.http_helpers.helpers as pyclob_helpers

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


def test_app_config_disables_short_window_auto_tune_by_default(monkeypatch):
    monkeypatch.delenv("MAKER_AUTO_TUNE", raising=False)
    monkeypatch.delenv("MAKER_AUTO_TUNE_INTERVAL_SEC", raising=False)

    config = AppConfig.from_env(enable_terminal_dashboard=False)

    assert config.maker.auto_tune_enabled is False
    assert config.maker.auto_tune_interval_sec == 3600


def test_no_inventory_settlement_records_outcome_for_journal_replay():
    class Host(StrategyLifecycleMixin):
        def __init__(self):
            self.inventory_delta_shares = Decimal("0")
            self.live_inventory_cost = {}
            self.latest_external_spot = Decimal("65100")
            self.last_external_spot = None
            self._binance_ws_price = None
            self._binance_ws_price_ts = 0.0
            self.external_spot_history = []
            self.current_market_slug = "btc-updown-15m-test"
            self.market_strike_cache_by_slug = {self.current_market_slug: Decimal("65000")}
            self.market_cycle_realized_net_usdc = Decimal("0")
            self.recent_market_combined_pnls = []
            self.active_side = ActiveSide.UP
            self.latest_external_spot_source = "polymarket_chainlink_twap_60s_ws"
            self._cycle_total_trades = 0
            self._cycle_total_wins = 0
            self.terminal_dashboard = None
            self.events = []

        def _settle_shadow_simulation(self, **_kwargs):
            return None

        def _db_strategy_event(self, event_type, payload):
            self.events.append((event_type, payload))

        def _append_cycle_and_maybe_trigger_regime_guard(self, **_kwargs):
            return None

        def _update_terminal_dashboard_snapshot(self):
            return None

    host = Host()
    host._record_market_settlement()

    settlement = next(payload for event_type, payload in host.events if event_type == "MARKET_SETTLEMENT")
    assert settlement["outcome"] == "UP"
    assert settlement["outcome_only"] is True
    assert settlement["reference_source"] == "polymarket_chainlink_twap_60s_ws"


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


def test_shadow_candidate_respects_positive_bias_only():
    side, edge = select_shadow_candidate(
        shadow_prob_up=0.56,
        shadow_prob_down=0.44,
        ask_up=Decimal("0.60"),
        ask_down=Decimal("0.30"),
        time_left_sec=300.0,
        shadow_score=0.20,
        cfg=ShadowSignalConfig(min_edge=Decimal("0.04")),
    )

    assert side is None
    assert edge is None


def test_shadow_candidate_respects_negative_bias_only():
    side, edge = select_shadow_candidate(
        shadow_prob_up=0.44,
        shadow_prob_down=0.56,
        ask_up=Decimal("0.20"),
        ask_down=Decimal("0.60"),
        time_left_sec=300.0,
        shadow_score=-0.20,
        cfg=ShadowSignalConfig(min_edge=Decimal("0.04")),
    )

    assert side is None
    assert edge is None


def test_reconcile_unwanted_quotes_cancels_existing_sell_immediately_for_hold_reason():
    cancels = []
    reconcile_unwanted_quotes(
        active_maker_orders={
            "sell:inst-up": {
                "instrument_id": "inst-up",
            }
        },
        desired_quotes={
            "sell:inst-up": {
                "should_quote": False,
                "diag_reason": "recycle_locked_side_hold fair_edge=0.0600>=0.0400 best_bid=0.7400 fair=0.8000",
                "force_cancel_existing": True,
            }
        },
        target_inst_set={"inst-up"},
        now_ts=100.0,
        cancel_cooldown_sec=30.0,
        gate_block_grace_sec=60.0,
        reason_family_fn=lambda reason: "risk",
        cancel_order_fn=lambda order_key, reason: cancels.append((order_key, reason)),
        gate_block_since_by_order_key={},
        gate_block_reason_by_order_key={},
        gate_last_cancel_ts_by_order_key={"sell:inst-up": 95.0},
    )

    assert cancels == [
        (
            "sell:inst-up",
            "risk:recycle_locked_side_hold fair_edge=0.0600>=0.0400 best_bid=0.7400 fair=0.8000",
        )
    ]


def test_trend_buy_size_multiplier_flows_into_submit_qty_without_economics_exemption():
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
            True,
            Decimal("0.007"),
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
        min_expected_net_usdc=Decimal("0.001"),
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


def test_crossing_tail_protect_tp_uses_bounded_taker_exit_not_post_only_maker_order():
    strategy = DummyTrendSubmitStrategy()
    strategy.inventory_delta_shares = Decimal("5.5")
    strategy._get_quote_for_instrument = lambda _instrument_id: (Decimal("0.99"), Decimal("1.00"))
    econ = SimpleNamespace(expected_net_usdc=Decimal("0.38"))

    submit_maker_quote(
        strategy,
        instrument_id="inst-up",
        side="sell",
        limit_price=Decimal("0.97"),
        econ=econ,
        dynamic_fee_rate=Decimal("0.01"),
        directional_snapshot={"tail_protect_tp": True, "tail_protect_tp_price": Decimal("0.97")},
        target_qty_override=Decimal("5.5"),
    )

    assert strategy.submitted_orders == []
    assert len(strategy.tail_exit_calls) == 1
    exit_call = strategy.tail_exit_calls[0]
    assert exit_call["reason"] == "tail_protect_tp_crossing"
    assert exit_call["execution_mode"] == "limit_fak"
    assert exit_call["best_bid"] == Decimal("0.99")
    assert exit_call["quantity"] == Decimal("5.5")


def test_entry_quality_adjustment_soft_sizes_high_price_chase_risk():
    adjustment = evaluate_entry_quality_adjustment(
        candidate_entry_price=Decimal("0.85"),
        side_score=Decimal("0.72"),
        fair=Decimal("0.87"),
        robust_net_usdc=Decimal("0.03"),
        spot_minus_strike_avg=Decimal("12"),
        active_side_value="UP",
        shadow_payload={
            "spot_minus_strike": 8.0,
            "ret_30_bps": 6.2,
            "breakout_persistence_60s": 0.15,
        },
    )

    assert adjustment.size_multiplier < Decimal("1")
    assert adjustment.min_expected_net_uplift_usdc > Decimal("0")
    assert adjustment.label in {"moderate_chase_risk", "high_chase_risk"}
    assert "high_price" in adjustment.reasons


def test_value_entry_size_multiplier_flows_into_submit_qty():
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
            True,
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
        maker_exchange_min_shares=Decimal("1.0"),
        avg_entry=Decimal("0"),
        emergency_window=False,
        high_cost_exit_cooldown_enabled=False,
        high_cost_exit_cooldown_sec=0.0,
        high_cost_exit_cooldown_until=0.0,
        maker_sell_cost_protect_enabled=False,
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0"),
        entry_mode="value",
        entry_size_multiplier=Decimal("0.60"),
        entry_quality={"entry_quality_label": "moderate_chase_risk"},
    )
    snapshot = build_directional_snapshot(desired_entry)
    strategy = DummyTrendSubmitStrategy()
    strategy.maker_min_shares = Decimal("1.0")
    strategy.maker_exchange_min_shares = Decimal("1.0")

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
    assert submitted_qty == Decimal("3.240000")
    assert strategy.order_events[-1]["payload"]["size_multiplier"] == 0.6


def test_high_entry_price_size_adjustment_applies_when_reduced_qty_meets_exchange_minimum():
    desired_entry = {
        "should_quote": True,
        "price": Decimal("0.71"),
        "size_multiplier": Decimal("1"),
        "diag_reason": "trend_buy_entry",
    }

    adjusted = apply_high_entry_price_size_adjustment(
        desired_entry=desired_entry,
        side="buy",
        enabled=True,
        threshold=Decimal("0.70"),
        multiplier=Decimal("0.5"),
    )
    snapshot = build_directional_snapshot(adjusted)
    strategy = DummyTrendSubmitStrategy()

    submit_maker_quote(
        strategy,
        instrument_id="inst-up",
        side="buy",
        limit_price=Decimal("0.71"),
        econ=SimpleNamespace(
            expected_net_usdc=Decimal("0.01"),
            expected_rebate_usdc=Decimal("0"),
            expected_spread_capture_usdc=Decimal("0"),
            fee_equivalent_usdc=Decimal("0"),
        ),
        directional_snapshot=snapshot,
        target_qty_override=Decimal("6.0"),
    )

    assert not strategy.submitted_orders
    assert strategy.order_events[-1]["event_type"] == "ORDER_SKIP_SIZE_BELOW_EXCHANGE_MIN"
    assert strategy.order_events[-1]["payload"]["size_multiplier"] == 0.5
    assert adjusted["high_entry_price_size_adjustment"]["threshold"] == Decimal("0.70")


def test_high_entry_price_size_adjustment_does_not_round_up_to_exchange_minimum():
    strategy = DummyTrendSubmitStrategy()
    strategy.maker_min_shares = Decimal("5.4")
    strategy.maker_exchange_min_shares = Decimal("5.0")

    submit_maker_quote(
        strategy,
        instrument_id="inst-up",
        side="buy",
        limit_price=Decimal("0.71"),
        econ=SimpleNamespace(
            expected_net_usdc=Decimal("0.01"),
            expected_rebate_usdc=Decimal("0"),
            expected_spread_capture_usdc=Decimal("0"),
            fee_equivalent_usdc=Decimal("0"),
        ),
        directional_snapshot={"size_multiplier": Decimal("0.5")},
        target_qty_override=Decimal("6.0"),
    )

    assert not strategy.submitted_orders
    assert strategy.order_events[-1]["event_type"] == "ORDER_SKIP_SIZE_BELOW_EXCHANGE_MIN"


def test_buy_is_not_submitted_above_the_evaluated_economics_quantity():
    strategy = DummyTrendSubmitStrategy()

    submit_maker_quote(
        strategy,
        instrument_id="inst-up",
        side="buy",
        limit_price=Decimal("0.60"),
        econ=SimpleNamespace(
            expected_net_usdc=Decimal("0.02"),
            expected_rebate_usdc=Decimal("0"),
            expected_spread_capture_usdc=Decimal("0"),
            fee_equivalent_usdc=Decimal("0"),
        ),
        directional_snapshot={
            "size_multiplier": Decimal("1"),
            "planned_quantity": Decimal("5"),
        },
    )

    assert not strategy.submitted_orders
    assert strategy.order_events[-1]["event_type"] == "ORDER_SKIP_ECONOMICS_QTY_MISMATCH"
    assert strategy.order_events[-1]["payload"]["planned_quantity"] == 5.0


def test_partial_fill_reload_uses_robust_net_not_legacy_forced_exit_edge():
    desired = apply_reload_edge_guard(
        desired_entry={
            "should_quote": True,
            "diag_reason": "eligible",
            "directional_edge_ps": Decimal("-0.12"),
            "robust_net": Decimal("0.03"),
        },
        side="buy",
        current_inst_inventory_qty=Decimal("5"),
        maker_reload_inventory_threshold_shares=Decimal("5"),
        maker_reload_min_directional_edge_ps=Decimal("0.03"),
    )

    assert desired["should_quote"] is True
    assert desired["diag_reason"] == "eligible"
    assert desired["reload_directional_edge_ps_telemetry"] == Decimal("-0.12")


def test_partial_fill_buy_is_capped_to_remaining_market_inventory_capacity():
    strategy = DummyTrendSubmitStrategy()
    strategy.maker_max_inventory_shares = Decimal("10")
    strategy.inventory_delta_shares = Decimal("4")
    strategy._compute_maker_order_qty = lambda _price, _precision: Decimal("10")

    submit_maker_quote(
        strategy,
        instrument_id="inst-up",
        side="buy",
        limit_price=Decimal("0.60"),
        econ=SimpleNamespace(
            expected_net_usdc=Decimal("0.01"),
            expected_rebate_usdc=Decimal("0"),
            expected_spread_capture_usdc=Decimal("0"),
            fee_equivalent_usdc=Decimal("0"),
        ),
    )

    assert strategy.submitted_orders[0].quantity.as_decimal() == Decimal("6.000000")
    cap_event = next(
        event for event in strategy.order_events
        if event["event_type"] == "ORDER_BUY_QTY_CAPPED_INVENTORY"
    )
    assert cap_event["payload"]["requested_qty"] == 10.0
    assert cap_event["payload"]["submitted_qty"] == 6.0


def test_buy_is_skipped_when_remaining_market_capacity_is_below_exchange_minimum():
    strategy = DummyTrendSubmitStrategy()
    strategy.maker_max_inventory_shares = Decimal("10")
    strategy.inventory_delta_shares = Decimal("5.4")
    strategy._compute_maker_order_qty = lambda _price, _precision: Decimal("10")

    submit_maker_quote(
        strategy,
        instrument_id="inst-up",
        side="buy",
        limit_price=Decimal("0.60"),
        econ=SimpleNamespace(
            expected_net_usdc=Decimal("0.01"),
            expected_rebate_usdc=Decimal("0"),
            expected_spread_capture_usdc=Decimal("0"),
            fee_equivalent_usdc=Decimal("0"),
        ),
    )

    assert not strategy.submitted_orders
    assert strategy.order_events[-1]["event_type"] == "ORDER_SKIP_INVENTORY_CAP"
    assert strategy.order_events[-1]["reason"] == "remaining_inventory_capacity_below_exchange_min"


def test_projected_buy_inventory_counts_pending_opposite_outcome_order():
    strategy = SimpleNamespace(
        instrument_id="inst-down",
        inventory_delta_shares=Decimal("0"),
        active_maker_orders={
            "buy:inst-up": {
                "side": "buy",
                "instrument_id": "inst-up",
                "quantity": Decimal("10"),
                "filled_qty": Decimal("0"),
            }
        },
        _get_confirmed_inventory_qty_for_instrument=lambda _inst: Decimal("0"),
    )

    projected = IntegratedBTCStrategy._project_inventory_after_fill(
        strategy,
        "buy",
        Decimal("10"),
        instrument_id="inst-down",
    )

    assert projected == Decimal("20")


def test_entry_quality_quote_placement_caps_high_decay_risk_to_best_bid():
    desired_entry = {
        "should_quote": True,
        "price": Decimal("0.74"),
        "entry_quality": {
            "entry_quality_quote_placement_mode": "join_bid",
        },
    }

    out = apply_entry_quality_quote_placement(
        desired_entry=desired_entry,
        side="buy",
        quote=(Decimal("0.70"), Decimal("0.75")),
        tick=Decimal("0.01"),
    )

    assert out["price"] == Decimal("0.70")
    assert out["entry_quality_quote_price_cap"] == Decimal("0.70")
    assert "entry_quality_quote_placement join_bid 0.7400->0.7000" in out["diag_reason"]


def test_weak_and_high_price_risk_caps_produce_half_size_not_quarter_size():
    desired = {
        "should_quote": True,
        "p_fair": Decimal("0.50"),
        "price": Decimal("0.75"),
        "size_multiplier": Decimal("1"),
    }
    desired = apply_weak_pfair_size_adjustment(
        desired_entry=desired,
        side="buy",
        enabled=True,
        lower=Decimal("0.47"),
        upper=Decimal("0.53"),
        multiplier=Decimal("0.5"),
    )
    desired = apply_high_entry_price_size_adjustment(
        desired_entry=desired,
        side="buy",
        enabled=True,
        threshold=Decimal("0.70"),
        multiplier=Decimal("0.5"),
    )

    assert desired["size_multiplier"] == Decimal("0.5")
    assert desired["weak_pfair_size_adjustment"]["adjusted_size_multiplier"] == Decimal("0.5")
    assert desired["high_entry_price_size_adjustment"]["adjusted_size_multiplier"] == Decimal("0.5")


def test_trend_mode_cannot_recover_a_negative_robust_net_with_discounted_cost():
    desired = build_desired_quote_entry(
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
        min_expected_net_usdc=Decimal("0.001"),
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
        trend_buy_size_multiplier=Decimal("1"),
    )

    assert desired["should_quote"] is False
    assert desired["robust_net"] == Decimal("-0.031")
    assert desired["diag_reason"].startswith("econ_gate")


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
    assert desired_entry["diag_reason"].startswith("sell_cost_protect sell=0.5000 < min=0.5850")
    assert "phase=- regime=-" in desired_entry["diag_reason"]
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


def test_de_risk_loss_sell_respects_min_hold_timer():
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
        decision_phase="DE_RISK",
        decision_regime="CHOP",
        hold_sec=5.0,
        loss_sell_min_hold_sec=10.0,
        time_left_sec=600.0,
    )

    assert desired_entry["should_quote"] is False
    assert desired_entry["loss_sell_reason"] == ""
    assert desired_entry["diag_reason"].startswith("sell_cost_protect sell=0.5000 < min=0.5850")
    assert "phase=DE_RISK regime=CHOP" in desired_entry["diag_reason"]


def test_exit_loss_sell_bypasses_min_hold_timer():
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
        offside_confirmed=True,
        stop_loss_regime_armed=False,
        decision_phase="EXIT",
        decision_regime="BROKEN",
        hold_sec=5.0,
        loss_sell_min_hold_sec=10.0,
        time_left_sec=600.0,
    )

    assert desired_entry["should_quote"] is True
    assert desired_entry["loss_sell_reason"] == "state_machine_exit:BROKEN"


def test_de_risk_without_thesis_bad_stays_cost_protected():
    desired_entry = build_desired_quote_entry(
        order_key="sell:inst-up",
        side="sell",
        inst_id="inst-up",
        quote_data=(
            Decimal("0.6300"),
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
            Decimal("0.6500"),
            Decimal("0"),
            Decimal("0"),
        ),
        side_disable_reason_by_side={},
        reduce_only_reason=None,
        reduce_only_tail_sell_block=False,
        reduce_only_no_new_sell_last_sec=30,
        forced_sell_only=False,
        min_expected_net_usdc=Decimal("0.001"),
        now_ts=200.0,
        sell_pause_until=0.0,
        is_dry_run_mode=False,
        sellable_qty=Decimal("5.4"),
        maker_exchange_min_shares=Decimal("5.0"),
        avg_entry=Decimal("0.69"),
        emergency_window=False,
        high_cost_exit_cooldown_enabled=False,
        high_cost_exit_cooldown_sec=0.0,
        high_cost_exit_cooldown_until=0.0,
        maker_sell_cost_protect_enabled=True,
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0.005"),
        maker_sell_min_profit_floor_ps=Decimal("0.010"),
        thesis_weakened=False,
        offside_confirmed=False,
        spot_still_supports_position=False,
        stop_loss_pending_active=False,
        stop_loss_regime_armed=False,
        decision_phase="DE_RISK",
        decision_regime="CHOP",
        hold_sec=120.0,
        loss_sell_min_hold_sec=90.0,
        time_left_sec=400.0,
    )

    assert desired_entry["should_quote"] is False
    assert desired_entry["loss_sell_reason"] == ""
    assert desired_entry["diag_reason"].startswith("sell_cost_protect")


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


def test_should_requote_existing_order_when_loss_exit_recovers():
    now_ts = time.time()
    current = {
        "pending_cancel": False,
        "target_version": 10,
        "created_ts": now_ts - 1.0,
        "loss_sell_reason": "forced_exit_thesis_bad",
    }

    assert should_requote_existing_order(
        current=current,
        target_version=10,
        now_ts=now_ts,
        maker_requote_min_age_sec=30.0,
        side="sell",
        maker_requote_min_age_sec_sell=30.0,
        desired_loss_sell_reason="",
    )




def test_forced_exit_sell_pricing_uses_fair_edge_not_plain_cost_floor():
    desired_entry = {
        "should_quote": True,
        "price": Decimal("0.6200"),
        "diag_reason": "unified_de_risk",
    }

    out = apply_forced_exit_sell_pricing(
        desired_entry=desired_entry,
        side="sell",
        avg_entry=Decimal("0.6000"),
        fair=Decimal("0.7354"),
        best_bid=Decimal("0.6700"),
        best_ask=Decimal("0.6800"),
        tick=Decimal("0.01"),
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0.0050"),
        maker_sell_min_profit_floor_ps=Decimal("0.0150"),
        exit_decision_reason="unified_de_risk",
    )

    assert out["price"] == Decimal("0.7300")
    assert "forced_exit_price" in out["diag_reason"]


def test_forced_exit_sell_pricing_can_exit_below_cost_floor_when_invalidation_confirmed():
    desired_entry = {
        "should_quote": True,
        "price": Decimal("0.3900"),
        "diag_reason": "confirmed_locked_side_invalidation",
    }

    out = apply_forced_exit_sell_pricing(
        desired_entry=desired_entry,
        side="sell",
        avg_entry=Decimal("0.7700"),
        fair=Decimal("0.0610"),
        best_bid=Decimal("0.0500"),
        best_ask=Decimal("0.0600"),
        tick=Decimal("0.01"),
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0.0050"),
        maker_sell_min_profit_floor_ps=Decimal("0.0400"),
        exit_decision_reason="confirmed_locked_side_invalidation",
        allow_loss_exit_below_cost_floor=True,
    )

    assert out["price"] == Decimal("0.0600")
    assert "below_cost=1" in out["diag_reason"]


def test_confirmed_adverse_exit_allows_loss_sell_without_exit_phase():
    allow_loss_sell, reason = compute_loss_sell_policy(
        thesis_weakened=False,
        offside_confirmed=True,
        confirmed_adverse_exit_active=True,
        spot_still_supports_position=False,
        stop_loss_pending_active=False,
        stop_loss_regime_armed=False,
        decision_phase="HOLD",
        decision_regime="TREND",
        hold_sec=5.0,
        loss_sell_min_hold_sec=60.0,
        emergency_window=False,
        time_left_sec=420.0,
        absolute_last_resort_sec=60.0,
        true_last_resort_sec=15.0,
    )

    assert allow_loss_sell is True
    assert reason == "confirmed_adverse_exit"


def test_extreme_winner_lock_profit_prices_high_near_market_top():
    desired_entry = {
        "should_quote": True,
        "price": Decimal("0.7100"),
        "diag_reason": "extreme_winner_lock_profit",
    }

    out = apply_forced_exit_sell_pricing(
        desired_entry=desired_entry,
        side="sell",
        avg_entry=Decimal("0.6900"),
        fair=Decimal("0.9900"),
        best_bid=Decimal("0.9800"),
        best_ask=Decimal("0.9900"),
        tick=Decimal("0.01"),
        maker_sell_cost_protect_fee_buffer_ps=Decimal("0.0050"),
        maker_sell_min_profit_floor_ps=Decimal("0.0150"),
        exit_decision_reason="extreme_winner_lock_profit",
    )

    assert out["price"] == Decimal("0.9900")
    assert "extreme_winner_lock_profit" in out["diag_reason"]


def test_exit_engine_does_not_hold_profitable_position_after_confirmed_invalidation():
    engine = ExitPolicyEngine(
        ExitEngineConfig(
            min_hold_sec=8,
            stop_loss_usdc=Decimal("0.20"),
            stop_loss_confirmations=2,
            stop_loss_requires_thesis_weakening=True,
            stop_loss_thesis_min_score_abs=Decimal("0.18"),
            stop_loss_hold_on_none_signal=True,
            conviction_band_min_price=Decimal("0.75"),
            hold_band_min_price=Decimal("0.60"),
            conviction_band_min_score_abs=Decimal("0.30"),
            hold_band_min_score_abs=Decimal("0.15"),
            hold_band_release_min_roi=Decimal("0"),
            conviction_stop_loss_multiplier=Decimal("1.5"),
            conviction_extra_confirmations=1,
            hold_band_requires_locked=True,
        )
    )
    snapshot = MarketSnapshot(
        instrument_id="inst-up",
        phase="ACTIVE",
        time_left_sec=420.0,
        best_bid=Decimal("0.82"),
        best_ask=Decimal("0.83"),
        fee_rate=Decimal("0.00"),
        spread=Decimal("0.01"),
        spread_pct=Decimal("0.0122"),
        fair=Decimal("0.84"),
        slippage_buffer_pct=Decimal("0.02"),
        exit_stage=ExitStage.PASSIVE,
        in_reduce_only_tail=False,
        fair_edge_ps=Decimal("0.02"),
        spot_minus_strike_bps=Decimal("-15"),
        stop_loss_disabled_in_tail=False,
    )
    position = PositionState(
        instrument_id="inst-up",
        qty=Decimal("5.4"),
        sellable_qty=Decimal("5.4"),
        avg_entry_price=Decimal("0.68"),
        entry_fee_remaining=Decimal("0.03"),
        held_side="UP",
        hold_sec=180.0,
        peak_bid=Decimal("0.90"),
        peak_fair=Decimal("0.92"),
        stop_loss_confirm_hits=0,
    )
    signal = SignalDecision(
        active_side=ActiveSide.UP,
        score=Decimal("-0.28"),
        locked=True,
        reason="confirmed_offside",
        matches_position=False,
    )

    decision = engine.evaluate(
        snapshot,
        position,
        signal,
        external_thesis_weakened=True,
        external_offside_confirmed=True,
        locked_side_invalidated=True,
        confirmed_adverse_exit_active=True,
    )

    assert decision.decision_type != ExitDecisionType.HOLD_IN_BAND


def test_live_shadow_payload_does_not_emit_opposite_candidate_side():
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
    assert payload["shadow_candidate_side"] is None


def test_forecast_snapshot_telemetry_preserves_shadow_payload_and_serializes_decimals():
    original = {"slug": "btc-updown-15m-test", "shadow_score": 0.2}
    payload = attach_forecast_snapshot_telemetry(
        original,
        diagnostics={
            "sigma_default": Decimal("0.60"),
            "sigma_raw_realized": Decimal("0.42"),
            "sigma_input_source": "realized_external_spot",
            "sigma_after_scale": Decimal("0.42"),
            "sigma_after_bounds": Decimal("0.42"),
            "sigma_time_decay_enabled": True,
            "sigma_time_decay_factor": Decimal("0.50"),
            "sigma_after_time_decay": Decimal("0.21"),
            "sigma": Decimal("0.21"),
            "implied_sigma": Decimal("0.35"),
            "implied_sigma_floor": Decimal("0.21"),
            "implied_sigma_floor_applied": True,
            "standard_up_probability": Decimal("0.61"),
            "twap_average_up_probability": Decimal("0.58"),
            "settlement_model": "twap_average_approx",
        },
        reference_source="polymarket_chainlink_twap_60s_ws",
        reference_source_age_sec=0.25,
    )

    assert original == {"slug": "btc-updown-15m-test", "shadow_score": 0.2}
    assert payload["forecast_schema_version"] == 1
    assert payload["forecast_sigma_final"] == 0.21
    assert payload["forecast_twap_average_up_probability"] == 0.58
    assert payload["forecast_reference_source"] == "polymarket_chainlink_twap_60s_ws"


def test_entry_regime_observation_payload_tags_mid_late_signed_spot_intersection():
    payload = {
        "slug": "btc-updown-15m-test",
        "main_candidate_side": "BUY_UP",
        "main_active_side": "UP",
        "main_score": 0.41,
        "time_left_sec": 420.0,
        "spot_minus_strike": 18.5,
        "ask_up": 0.67,
        "ask_down": 0.35,
    }

    observation = build_entry_regime_observation_payload(payload)

    assert observation is not None
    assert observation["regime_tag"] == "mid_late_signed_spot_10_30"
    assert observation["main_candidate_outcome"] == "UP"
    assert math.isclose(observation["signed_spot_minus_strike"], 18.5, rel_tol=0.0, abs_tol=1e-9)
    assert math.isclose(observation["token_price"], 0.67, rel_tol=0.0, abs_tol=1e-9)


def test_entry_regime_observation_payload_inverts_down_side_and_skips_outside_regime():
    in_regime = {
        "slug": "btc-updown-15m-test",
        "main_candidate_side": "BUY_DOWN",
        "main_active_side": "DOWN",
        "main_score": -0.33,
        "time_left_sec": 480.0,
        "spot_minus_strike": -14.0,
        "ask_up": 0.42,
        "ask_down": 0.58,
    }
    observation = build_entry_regime_observation_payload(in_regime)
    assert observation is not None
    assert observation["main_candidate_outcome"] == "DOWN"
    assert math.isclose(observation["signed_spot_minus_strike"], 14.0, rel_tol=0.0, abs_tol=1e-9)
    assert math.isclose(observation["token_price"], 0.58, rel_tol=0.0, abs_tol=1e-9)

    outside_regime = dict(in_regime)
    outside_regime["time_left_sec"] = 610.0
    assert build_entry_regime_observation_payload(outside_regime) is None


def test_capture_market_open_spot_prefers_fresh_chainlink_over_stale_latest_external():
    now_ts = 1_000.0
    dummy = SimpleNamespace(
        _polymarket_chainlink_price=Decimal("101.25"),
        _polymarket_chainlink_price_ts=999.4,
        _binance_ws_price=Decimal("100.80"),
        _binance_ws_price_ts=999.7,
        latest_external_spot=Decimal("99.50"),
        latest_external_spot_source="binance_ws",
        latest_external_spot_source_ts=810.0,
        last_external_spot=Decimal("99.40"),
        external_spot_history=[],
    )

    price, source, age = IntegratedBTCStrategy._capture_market_open_spot_detail(dummy, now_ts=now_ts)

    assert price == Decimal("101.25")
    assert source == "polymarket_chainlink_ws"
    assert math.isclose(age, 0.6, rel_tol=0.0, abs_tol=1e-9)


def test_capture_market_open_spot_prefers_fresh_twap_over_snapshot_chainlink():
    now_ts = 1_000.0
    dummy = SimpleNamespace(
        _polymarket_chainlink_twap_price=Decimal("101.50"),
        _polymarket_chainlink_twap_price_ts=999.6,
        _polymarket_chainlink_twap_window_sec=60,
        polymarket_chainlink_twap_window_sec=60,
        _polymarket_chainlink_price=Decimal("101.25"),
        _polymarket_chainlink_price_ts=999.7,
        _binance_ws_price=Decimal("100.80"),
        _binance_ws_price_ts=999.8,
        latest_external_spot=Decimal("99.50"),
        latest_external_spot_source="binance_ws",
        latest_external_spot_source_ts=810.0,
        last_external_spot=Decimal("99.40"),
        external_spot_history=[],
    )

    price, source, age = IntegratedBTCStrategy._capture_market_open_spot_detail(dummy, now_ts=now_ts)

    assert price == Decimal("101.50")
    assert source == "polymarket_chainlink_twap_60s_ws"
    assert math.isclose(age, 0.4, rel_tol=0.0, abs_tol=1e-9)


def test_first_entry_gate_is_stricter_than_general_directional_entry_gate():
    out = evaluate_buy_entry_controls(
        side="buy",
        bi_side_enabled=True,
        active_side_locked=True,
        active_side_value="DOWN",
        latest_observation_supports_locked_side=True,
        side_score=Decimal("-0.21"),
        directional_entry_min_score_abs_new=Decimal("0.18"),
        directional_first_entry_min_score_abs_new=Decimal("0.25"),
        maker_min_expected_net_usdc=Decimal("0.001"),
        maker_reload_min_expected_net_multiplier=Decimal("1.5"),
        current_inst_inventory_qty=Decimal("0"),
        maker_reload_inventory_threshold_shares=Decimal("5"),
        current_slug="btc-updown-15m-test",
        inst_id="inst-down",
        market_buy_count=0,
        trend_buy_enabled=True,
        trend_buy_min_score=Decimal("0.16"),
        active_instrument_id="inst-down",
        time_left_sec=600.0,
        trend_buy_min_time_left_sec=360.0,
        best_bid=Decimal("0.55"),
        fair=Decimal("0.62"),
        trend_buy_max_price_premium_ps=Decimal("0.018"),
    )

    assert out.skip is True
    assert out.event_type == "ORDER_SKIP_DIRECTIONAL_FIRST_ENTRY_GATE"
    assert out.reason == "directional_first_entry_gate"


def test_entry_spot_strike_average_gate_blocks_unstable_up_entry():
    out = evaluate_buy_entry_controls(
        side="buy",
        bi_side_enabled=True,
        active_side_locked=True,
        active_side_value="UP",
        locked_side_entry_blocked=False,
        locked_side_entry_block_reason="",
        side_score=Decimal("0.28"),
        directional_entry_min_score_abs_new=Decimal("0.18"),
        directional_first_entry_min_score_abs_new=Decimal("0.25"),
        maker_min_expected_net_usdc=Decimal("0.001"),
        maker_reload_min_expected_net_multiplier=Decimal("1.5"),
        current_inst_inventory_qty=Decimal("0"),
        maker_reload_inventory_threshold_shares=Decimal("5"),
        max_locked_side_position=Decimal("8"),
        inventory_full_behavior="STOP_BUY",
        current_slug="btc-updown-15m-test",
        inst_id="inst-up",
        market_buy_count=1,
        candidate_entry_price=Decimal("0.55"),
        fair=Decimal("0.62"),
        spot_minus_strike_avg=Decimal("-3"),
        entry_spot_strike_avg_min_abs=Decimal("0"),
    )

    assert out.skip is True
    assert out.event_type == "ORDER_SKIP_ENTRY_SPOT_STRIKE_AVG_GATE"
    assert out.reason == "entry_spot_strike_avg_gate"


def test_entry_fair_edge_gate_blocks_thin_entry_buffer():
    out = evaluate_buy_entry_controls(
        side="buy",
        bi_side_enabled=True,
        active_side_locked=True,
        active_side_value="UP",
        locked_side_entry_blocked=False,
        locked_side_entry_block_reason="",
        side_score=Decimal("0.28"),
        directional_entry_min_score_abs_new=Decimal("0.18"),
        directional_first_entry_min_score_abs_new=Decimal("0.25"),
        maker_min_expected_net_usdc=Decimal("0.001"),
        maker_reload_min_expected_net_multiplier=Decimal("1.5"),
        current_inst_inventory_qty=Decimal("1"),
        maker_reload_inventory_threshold_shares=Decimal("5"),
        max_locked_side_position=Decimal("8"),
        inventory_full_behavior="STOP_BUY",
        current_slug="btc-updown-15m-test",
        inst_id="inst-up",
        market_buy_count=1,
        candidate_entry_price=Decimal("0.60"),
        fair=Decimal("0.63"),
        entry_fair_edge_min_ps=Decimal("0.05"),
    )

    assert out.skip is True
    assert out.event_type == "ORDER_SKIP_ENTRY_FAIR_EDGE_GATE"
    assert out.reason == "entry_fair_edge_gate"


def test_entry_fair_edge_shadow_keeps_other_entry_controls_and_marks_bucket():
    out = evaluate_buy_entry_controls(
        side="buy",
        bi_side_enabled=True,
        active_side_locked=True,
        active_side_value="UP",
        locked_side_entry_blocked=False,
        locked_side_entry_block_reason="",
        side_score=Decimal("0.28"),
        directional_entry_min_score_abs_new=Decimal("0.18"),
        directional_first_entry_min_score_abs_new=Decimal("0.25"),
        maker_min_expected_net_usdc=Decimal("0.001"),
        maker_reload_min_expected_net_multiplier=Decimal("1.5"),
        current_inst_inventory_qty=Decimal("1"),
        maker_reload_inventory_threshold_shares=Decimal("5"),
        max_locked_side_position=Decimal("8"),
        inventory_full_behavior="STOP_BUY",
        current_slug="btc-updown-15m-test",
        inst_id="inst-up",
        market_buy_count=1,
        candidate_entry_price=Decimal("0.68"),
        fair=Decimal("0.64"),
        entry_fair_edge_min_ps=Decimal("0.002"),
        allow_fair_edge_shadow=True,
    )

    assert out.skip is False
    assert out.shadow_only is True
    assert out.fair_edge_bucket == "neg_0_05_to_neg_0_02"


def test_down_high_price_gate_requires_stronger_context():
    out = evaluate_buy_entry_controls(
        side="buy",
        bi_side_enabled=True,
        active_side_locked=True,
        active_side_value="DOWN",
        locked_side_entry_blocked=False,
        locked_side_entry_block_reason="",
        side_score=Decimal("-0.18"),
        directional_entry_min_score_abs_new=Decimal("0.18"),
        directional_first_entry_min_score_abs_new=Decimal("0.25"),
        maker_min_expected_net_usdc=Decimal("0.001"),
        maker_reload_min_expected_net_multiplier=Decimal("1.5"),
        current_inst_inventory_qty=Decimal("1"),
        maker_reload_inventory_threshold_shares=Decimal("5"),
        max_locked_side_position=Decimal("8"),
        inventory_full_behavior="STOP_BUY",
        current_slug="btc-updown-15m-test",
        inst_id="inst-down",
        market_buy_count=1,
        candidate_entry_price=Decimal("0.70"),
        fair=Decimal("0.76"),
        robust_net_usdc=Decimal("0.08"),
        spot_minus_strike_avg=Decimal("-4"),
        down_high_price_threshold=Decimal("0.65"),
        down_high_price_min_score_abs=Decimal("0.25"),
        down_high_price_min_robust_net_usdc=Decimal("0.15"),
        down_high_price_spot_strike_avg_max=Decimal("-10"),
    )

    assert out.skip is True
    assert out.event_type == "ORDER_SKIP_DOWN_HIGH_PRICE_GATE"
    assert out.reason == "down_high_price_gate"


def test_continuation_entry_cannot_open_first_position_in_market():
    desired_entry = {
        "should_quote": False,
        "diag_reason": "econ_gate robust_net=-0.01",
        "robust_net": Decimal("0.12"),
    }

    out = maybe_apply_continuation_entry(
        desired_entry=desired_entry,
        side="buy",
        active_side_locked=True,
        active_side_value="UP",
        inst_id="inst-up",
        active_instrument_id="inst-up",
        side_score=Decimal("0.42"),
        locked_for_sec=40.0,
        time_left_sec=600.0,
        current_inventory_qty=Decimal("0"),
        market_buy_count=0,
        best_bid=Decimal("0.60"),
        fair=Decimal("0.78"),
        continuation_enabled=True,
        continuation_size_multiplier=Decimal("1.0"),
    )

    assert out["should_quote"] is False
    assert out["diag_reason"].startswith("econ_gate")


def test_continuation_entry_still_works_after_market_has_prior_buy():
    desired_entry = {
        "should_quote": False,
        "diag_reason": "econ_gate robust_net=-0.01",
        "robust_net": Decimal("0.12"),
    }

    out = maybe_apply_continuation_entry(
        desired_entry=desired_entry,
        side="buy",
        active_side_locked=True,
        active_side_value="UP",
        inst_id="inst-up",
        active_instrument_id="inst-up",
        side_score=Decimal("0.42"),
        locked_for_sec=40.0,
        time_left_sec=600.0,
        current_inventory_qty=Decimal("0"),
        market_buy_count=1,
        best_bid=Decimal("0.60"),
        fair=Decimal("0.78"),
        continuation_enabled=True,
        continuation_size_multiplier=Decimal("1.0"),
    )

    assert out["should_quote"] is True
    assert out["entry_mode"] == "continuation"


def test_quote_watchdog_refresh_clears_stale_cache_and_replaces_subscriptions():
    class DummyStrategy:
        def __init__(self):
            self.current_market_instruments = ["up-token", "down-token"]
            self.latest_quote_by_inst = {"up-token": (Decimal("0.60"), Decimal("0.61"))}
            self.latest_quote_depth_by_inst = {"up-token": (Decimal("1"), Decimal("1"))}
            self.last_quote_update_ts_by_inst = {"up-token": 1.0}
            self.last_quote_received_ts_by_inst = {"up-token": 1.0}
            self.unsubscribed = []
            self.subscribed = []

        def unsubscribe_quote_ticks(self, instrument_id):
            self.unsubscribed.append(instrument_id)

        def subscribe_quote_ticks(self, instrument_id):
            self.subscribed.append(instrument_id)

    strategy = DummyStrategy()
    refresh_quote_tick_subscriptions(strategy)

    assert strategy.unsubscribed == ["up-token", "down-token"]
    assert strategy.subscribed == ["up-token", "down-token"]
    assert strategy.latest_quote_by_inst == {}
    assert strategy.last_quote_update_ts_by_inst == {}
    assert strategy.quote_recovery_pending_instruments == {"up-token", "down-token"}
    assert strategy.quote_recovery_attempts == 0


def test_quote_event_freshness_uses_source_event_time_not_receive_time():
    assert quote_event_is_fresh(
        received_ts=100.0,
        event_ts=99.9,
        max_age_sec=30.0,
        clock_skew_tolerance_sec=Decimal("0.25"),
    )
    assert quote_event_is_fresh(
        received_ts=100.0,
        event_ts=100.15,
        max_age_sec=30.0,
        clock_skew_tolerance_sec=Decimal("0.25"),
    )
    assert not quote_event_is_fresh(
        received_ts=100.0,
        event_ts=69.9,
        max_age_sec=30.0,
        clock_skew_tolerance_sec=Decimal("0.25"),
    )


def test_transport_heartbeat_keeps_quiet_connected_book_out_of_watchdog():
    now_ts = 100.0
    assert should_emit_transport_heartbeat(is_connected=True, has_quote=True)
    should_run, stale_for = should_run_quote_watchdog(
        now_ts=now_ts,
        last_quote_watchdog_check_ts=0.0,
        quote_healthcheck_interval_sec=1.0,
        last_valid_quote_ts=now_ts,
        quote_stale_sec=30.0,
        consecutive_invalid_quote_ticks=0,
        quote_invalid_tick_reload_threshold=80,
    )
    assert not should_run
    assert stale_for == 0.0


def test_dead_transport_still_triggers_quote_watchdog():
    should_run, stale_for = should_run_quote_watchdog(
        now_ts=135.0,
        last_quote_watchdog_check_ts=0.0,
        quote_healthcheck_interval_sec=1.0,
        last_valid_quote_ts=100.0,
        quote_stale_sec=30.0,
        consecutive_invalid_quote_ticks=0,
        quote_invalid_tick_reload_threshold=80,
    )
    assert should_run
    assert stale_for == 35.0


def test_stale_exchange_event_stays_rejected_when_transport_is_alive():
    assert should_emit_transport_heartbeat(is_connected=True, has_quote=True)
    assert not quote_event_is_fresh(
        received_ts=100.0,
        event_ts=69.9,
        max_age_sec=30.0,
        clock_skew_tolerance_sec=Decimal("0.25"),
    )


def test_stale_transport_heartbeat_updates_liveness_without_updating_quote_state():
    class Price:
        def __init__(self, value):
            self.value = Decimal(value)

        def as_decimal(self):
            return self.value

    class Strategy:
        _stopping = False
        instrument_id = "up-token"
        current_market_instruments = ["up-token"]
        stale_quote_synth_max_age_sec = 10.0
        latest_market_bid_ts = 0.0
        latest_market_ask_ts = 0.0
        latest_market_bid = None
        latest_market_ask = None
        active_side = ActiveSide.UP
        quote_stale_sec = 30.0
        quote_event_clock_skew_tolerance_sec = Decimal("0.25")
        last_valid_quote_ts = 0.0
        consecutive_invalid_quote_ticks = 0

        def __init__(self):
            self.last_quote_received_ts_by_inst = {}
            self.latest_quote_by_inst = {}
            self.latest_quote_depth_by_inst = {}
            self.watchdog_triggers = []

        def _instrument_for_side(self, _side):
            return "up-token"

        def _primary_instrument_for_market(self):
            return "up-token"

        def _maybe_run_quote_watchdog(self, trigger):
            self.watchdog_triggers.append(trigger)

    strategy = Strategy()
    tick = SimpleNamespace(
        instrument_id="up-token",
        bid_price=Price("0.60"),
        ask_price=Price("0.61"),
        bid_size=Price("10"),
        ask_size=Price("10"),
        ts_event=1,
    )

    handle_quote_tick(strategy, tick)

    assert strategy.last_quote_received_ts_by_inst["up-token"] > 0
    assert strategy.last_valid_quote_ts > 0
    assert strategy.latest_quote_by_inst == {}
    assert strategy.watchdog_triggers == []


def test_quote_transport_telemetry_records_adapter_source_and_stale_age():
    class Price:
        def __init__(self, value):
            self.value = Decimal(value)

        def as_decimal(self):
            return self.value

    class Strategy:
        _stopping = False
        instrument_id = "up-token"
        current_market_instruments = ["up-token"]
        stale_quote_synth_max_age_sec = 10.0
        latest_market_bid_ts = 0.0
        latest_market_ask_ts = 0.0
        latest_market_bid = None
        latest_market_ask = None
        active_side = ActiveSide.UP
        quote_stale_sec = 30.0
        quote_event_clock_skew_tolerance_sec = Decimal("0.25")
        last_valid_quote_ts = 0.0
        consecutive_invalid_quote_ticks = 0

        def __init__(self):
            self.last_quote_received_ts_by_inst = {}
            self.latest_quote_by_inst = {}
            self.latest_quote_depth_by_inst = {}
            self.events = []

        def _instrument_for_side(self, _side):
            return "up-token"

        def _primary_instrument_for_market(self):
            return "up-token"

        def _db_strategy_event(self, event_type, payload):
            self.events.append((event_type, payload))

    strategy = Strategy()
    tick = SimpleNamespace(
        instrument_id="up-token",
        bid_price=Price("0.60"),
        ask_price=Price("0.61"),
        bid_size=Price("10"),
        ask_size=Price("10"),
        ts_event=1,
    )
    record_quote_provenance(tick, source="transport_heartbeat")

    assert quote_provenance_for_tick(tick)["source"] == "transport_heartbeat"
    handle_quote_tick(strategy, tick)

    assert strategy.events
    event_type, payload = strategy.events[-1]
    assert event_type == "QUOTE_TRANSPORT_TELEMETRY"
    assert payload["quote_source"] == "transport_heartbeat"
    assert payload["quote_is_fresh"] is False
    assert payload["quote_age_raw_sec"] > 30
    assert payload["raw_ws_received_ts"] is None


def test_quote_provenance_retains_raw_websocket_ingress_timestamp():
    tick = SimpleNamespace(
        instrument_id="up-token",
        ts_event=1,
        ts_init=2,
    )

    record_quote_provenance(
        tick,
        source="ws_price_change",
        raw_ws_received_ts=1234.5,
    )

    provenance = quote_provenance_for_tick(tick)
    assert provenance["source"] == "ws_price_change"
    assert provenance["raw_ws_received_ts"] == 1234.5


def test_quote_provenance_retains_data_engine_queue_depth():
    tick = SimpleNamespace(
        instrument_id="up-token",
        ts_event=1,
        ts_init=2,
    )
    record_quote_provenance(tick, source="ws_price_change")

    record_quote_data_engine_queue_depth(tick, 37)

    assert quote_provenance_for_tick(tick)["data_engine_queue_depth"] == 37


def test_transport_heartbeat_uses_distinct_tick_provenance():
    class QuoteTick:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    quote = SimpleNamespace(
        instrument_id="up-token",
        bid_price="0.60",
        ask_price="0.61",
        bid_size="10",
        ask_size="11",
        ts_event=1,
        ts_init=2,
    )
    data_mod = SimpleNamespace(QuoteTick=QuoteTick)
    record_quote_provenance(quote, source="ws_price_change")

    heartbeat = build_transport_heartbeat_quote(data_mod, quote, 3)
    record_quote_provenance(heartbeat, source="transport_heartbeat")

    assert quote_provenance_for_tick(quote)["source"] == "ws_price_change"
    assert quote_provenance_for_tick(heartbeat)["source"] == "transport_heartbeat"


def test_native_clob_snapshot_uses_adapter_emit_time_not_old_book_timestamp():
    now_ts = time.time()

    assert quote_transport_is_fresh(
        received_ts=now_ts,
        adapter_emitted_ts=now_ts,
        max_age_sec=30.0,
        clock_skew_tolerance_sec=Decimal("0.25"),
    )
    assert not quote_transport_is_fresh(
        received_ts=now_ts,
        adapter_emitted_ts=now_ts - 31.0,
        max_age_sec=30.0,
        clock_skew_tolerance_sec=Decimal("0.25"),
    )


def test_quote_adapter_timestamp_uses_nautilus_ts_init_not_exchange_event_time():
    now_ts = time.time()
    tick = SimpleNamespace(
        ts_event=int((now_ts - 90.0) * 1_000_000_000),
        ts_init=int(now_ts * 1_000_000_000),
    )

    assert abs(quote_tick_adapter_timestamp(tick, 0.0) - now_ts) < 0.01


def test_native_quote_with_old_exchange_timestamp_is_executable_when_ts_init_is_fresh():
    class Price:
        def __init__(self, value):
            self.value = Decimal(value)

        def as_decimal(self):
            return self.value

    class Strategy:
        _stopping = False
        instrument_id = "up-token"
        current_market_instruments = ["up-token"]
        stale_quote_synth_max_age_sec = 10.0
        latest_market_bid_ts = 0.0
        latest_market_ask_ts = 0.0
        latest_market_bid = None
        latest_market_ask = None
        active_side = ActiveSide.UP
        quote_stale_sec = 30.0
        quote_event_clock_skew_tolerance_sec = Decimal("0.25")
        last_valid_quote_ts = 0.0
        consecutive_invalid_quote_ticks = 0
        maker_mode = False
        max_history = 100

        def __init__(self):
            self.last_quote_received_ts_by_inst = {}
            self.last_quote_update_ts_by_inst = {}
            self.latest_quote_by_inst = {}
            self.latest_quote_depth_by_inst = {}
            self.price_history = []
            self.events = []

        def _instrument_for_side(self, _side):
            return "up-token"

        def _primary_instrument_for_market(self):
            return "up-token"

        def _append_real_mid_price(self, *_args):
            pass

        def _db_strategy_event(self, event_type, payload):
            self.events.append((event_type, payload))

    now_ts = time.time()
    tick = SimpleNamespace(
        instrument_id="up-token",
        bid_price=Price("0.60"),
        ask_price=Price("0.61"),
        bid_size=Price("10"),
        ask_size=Price("10"),
        ts_event=int((now_ts - 90.0) * 1_000_000_000),
        ts_init=int(now_ts * 1_000_000_000),
    )
    record_quote_provenance(tick, source="ws_snapshot")

    strategy = Strategy()
    handle_quote_tick(strategy, tick)

    assert strategy.latest_quote_by_inst["up-token"] == (Decimal("0.60"), Decimal("0.61"))
    assert abs(strategy.last_quote_update_ts_by_inst["up-token"] - now_ts) < 0.01
    assert strategy.events[-1][1]["quote_is_fresh"] is True


def test_quote_recovery_waits_for_both_binary_outcomes_before_clearing_pending_set():
    class Price:
        def __init__(self, value):
            self.value = Decimal(value)

        def as_decimal(self):
            return self.value

    class Strategy:
        _stopping = False
        instrument_id = "up-token"
        current_market_instruments = ["up-token", "down-token"]
        stale_quote_synth_max_age_sec = 10.0
        latest_market_bid_ts = 0.0
        latest_market_ask_ts = 0.0
        latest_market_bid = None
        latest_market_ask = None
        active_side = ActiveSide.UP
        quote_stale_sec = 30.0
        quote_event_clock_skew_tolerance_sec = Decimal("0.25")
        last_valid_quote_ts = 0.0
        consecutive_invalid_quote_ticks = 0
        quote_recovery_started_ts = 123.0
        quote_recovery_attempts = 1

        def __init__(self):
            self.last_quote_received_ts_by_inst = {}
            self.latest_quote_by_inst = {}
            self.latest_quote_depth_by_inst = {}
            self.last_quote_update_ts_by_inst = {}
            self.quote_recovery_pending_instruments = {"up-token", "down-token"}

        def _instrument_for_side(self, _side):
            return "up-token"

        def _primary_instrument_for_market(self):
            return "up-token"

        def _append_real_mid_price(self, *_args):
            pass

        def _maybe_run_quote_watchdog(self, _trigger):
            raise AssertionError("fresh quote must not trigger the watchdog")

    strategy = Strategy()
    fresh_tick = SimpleNamespace(
        instrument_id="down-token",
        bid_price=Price("0.40"),
        ask_price=Price("0.41"),
        bid_size=Price("10"),
        ask_size=Price("10"),
        ts_event=int(time.time() * 1_000_000_000),
    )

    handle_quote_tick(strategy, fresh_tick)

    assert strategy.quote_recovery_pending_instruments == {"up-token"}
    assert strategy.quote_recovery_started_ts == 123.0
    assert strategy.quote_recovery_attempts == 1

    fresh_tick.instrument_id = "up-token"
    handle_quote_tick(strategy, fresh_tick)

    assert strategy.quote_recovery_pending_instruments == set()
    assert strategy.quote_recovery_started_ts == 0.0
    assert strategy.quote_recovery_attempts == 0


def test_rollover_flag_is_captured_before_node_dispose_clears_strategies():
    class Strategy:
        _rollover_requested_flag = True

    class Trader:
        def __init__(self):
            self._strategies = [Strategy()]

        def strategies(self):
            return self._strategies

    class Node:
        def __init__(self):
            self.trader = Trader()

        def dispose(self):
            self.trader = None

    node = Node()
    requested_before_dispose = _strategy_requested_rollover(node)
    node.dispose()
    assert requested_before_dispose
    assert _strategy_requested_rollover(node) is False


def test_unchanged_top_of_book_emits_bounded_heartbeat():
    assert should_emit_quote_heartbeat(
        quote_unchanged=False,
        now_ns=1_000_000_000,
        last_emit_ns=999_000_000,
        heartbeat_sec=5.0,
    )


def test_stale_twap_degrades_to_fresh_binance_instead_of_stopping_pipeline():
    strategy = DummySpotPricerStrategy()
    now_ts = time.time()
    strategy.require_twap_reference_spot = True
    strategy.external_spot_source_delta_abs_max_usd = Decimal("0")
    strategy._polymarket_chainlink_twap_price = Decimal("65000")
    strategy._polymarket_chainlink_twap_price_ts = now_ts - 11.0
    strategy._polymarket_chainlink_twap_window_sec = 60
    strategy._polymarket_chainlink_price = Decimal("65001")
    strategy._polymarket_chainlink_price_ts = now_ts
    strategy._binance_ws_price = Decimal("64999")
    strategy._binance_ws_price_ts = now_ts
    strategy.strategy_events = []

    price = asyncio.run(strategy._fetch_external_spot_price())

    assert price == Decimal("64999")
    assert strategy.latest_external_spot_source == "binance_ws"
    assert strategy.strategy_events[-1][0] == "TWAP_REFERENCE_DEGRADED"
    assert not should_emit_quote_heartbeat(
        quote_unchanged=True,
        now_ns=5_999_000_000,
        last_emit_ns=1_000_000_000,
        heartbeat_sec=5.0,
    )
    assert should_emit_quote_heartbeat(
        quote_unchanged=True,
        now_ns=6_000_000_000,
        last_emit_ns=1_000_000_000,
        heartbeat_sec=5.0,
    )
