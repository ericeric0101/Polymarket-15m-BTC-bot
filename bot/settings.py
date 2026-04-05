import os
import threading
import time
import uuid
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger

from bot.app_config import AppConfig
from bot.market_cycle_state import MarketCycleState, bind_market_cycle_state
from bot.enums import ActiveSide, MarketPhase
from bot.position_manager import PositionManager, PositionManagerConfig
from bot.quoting import normalize_quote_mode
from bot.risk_policy import (
    FillCooldownConfig,
    FillCooldownPolicy,
    RegimeGuardConfig,
    RegimeGuardPolicy,
)
from execution.exit_policy import ExitPolicy, ExitPolicyConfig
from execution.fee_rate_client import FeeRateClient
from execution.maker_engine import MakerEngine, MakerEngineConfig
from execution.parameter_tuner import ParameterTuner
from execution.rebate_model import CRYPTO_FEE_CURVE
from execution.rebate_reporter import RebateReporter
from monitoring.grafana_exporter import get_grafana_exporter
from monitoring.performance_tracker import get_performance_tracker
from monitoring.terminal_dashboard import TerminalDashboard
from monitoring.trade_journal_db import TradeJournalDB
from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.shadow_signal import DEFAULT_SHADOW_SIGNAL_CONFIG


def initialize_strategy_settings(
    strategy: Any,
    *,
    enable_grafana: bool,
    test_mode: bool,
    enable_terminal_dashboard: bool,
    project_root: Path,
    detect_runtime_git_revision_fn,
) -> None:
    config = AppConfig.from_env(enable_terminal_dashboard=enable_terminal_dashboard)
    strategy.app_config = config
    strategy.startup_verbose = config.observability.startup_verbose

    strategy.performance_tracker = get_performance_tracker()
    strategy.grafana_exporter = get_grafana_exporter() if enable_grafana else None
    strategy.terminal_dashboard_enabled = config.observability.terminal_dashboard_enabled
    strategy.terminal_dashboard_refresh_sec = config.observability.terminal_dashboard_refresh_sec
    strategy.terminal_dashboard = (
        TerminalDashboard(
            title="BTC 15M Terminal Dashboard",
            refresh_interval_sec=strategy.terminal_dashboard_refresh_sec,
        )
        if strategy.terminal_dashboard_enabled
        else None
    )

    strategy.price_history = []
    strategy.max_history = 100
    strategy.real_price_history: List[Decimal] = []
    strategy.real_price_history_by_inst: Dict[str, List[Decimal]] = {}
    strategy.max_real_history = config.maker.vol_real_history_max
    strategy.last_reload_time = 0
    strategy._rollover_requested_flag = False
    strategy._waiting_miss_count = 0
    strategy.shadow_signal_enabled = True
    strategy.shadow_signal_config = DEFAULT_SHADOW_SIGNAL_CONFIG
    strategy._last_shadow_live_candidate_signature = None
    strategy._last_main_live_candidate_signature = None

    strategy.test_mode = test_mode
    strategy.maker_mode = config.maker.maker_mode
    strategy.quote_refresh_sec = config.maker.quote_refresh_sec
    strategy.maker_half_spread = config.maker.half_spread
    strategy.maker_quote_size_usdc = config.maker.quote_size_usdc
    strategy.maker_min_shares = config.maker.min_shares
    strategy.maker_exchange_min_shares = config.maker.exchange_min_shares
    strategy.maker_fixed_shares = config.maker.fixed_shares
    raw_quote_mode = config.maker.quote_sides
    strategy.maker_quote_sides = config.maker.quote_sides
    if raw_quote_mode in {"sell", "both_buy"}:
        logger.warning(
            f"Deprecated maker quote mode '{raw_quote_mode}' detected; coercing to UP-only 'both'."
        )
    strategy.maker_directional_edge_gate_enabled = config.maker.directional_edge_gate_enabled
    strategy.maker_min_directional_edge_ps = config.maker.min_directional_edge_ps
    strategy.maker_min_directional_edge_ps_down = config.maker.min_directional_edge_ps_down
    strategy.maker_min_directional_edge_ps_conservative = config.maker.min_directional_edge_ps_conservative
    strategy.maker_min_expected_net_usdc = config.maker.min_expected_net_usdc
    strategy.maker_reload_inventory_threshold_shares = config.maker.reload_inventory_threshold_shares
    strategy.maker_reload_min_expected_net_multiplier = config.maker.reload_min_expected_net_multiplier
    strategy.maker_reload_min_directional_edge_ps = config.maker.reload_min_directional_edge_ps
    strategy.maker_adverse_selection_buffer = config.maker.adverse_selection_buffer
    strategy.maker_use_post_only = config.maker.use_post_only
    strategy.maker_post_only_strict = config.maker.post_only_strict
    strategy.maker_max_inventory_shares = config.maker.max_inventory_shares
    strategy.maker_kill_switch_reset_on_rollover = config.maker.kill_switch_reset_on_rollover
    strategy.maker_inventory_skew_max = config.maker.inventory_skew_max
    strategy.maker_stale_inventory_sec = config.maker.stale_inventory_sec
    strategy.maker_stale_inventory_multiplier = config.maker.stale_inventory_multiplier
    strategy.maker_vol_stressed_threshold = config.maker.vol_stressed_threshold
    strategy.maker_vol_extreme_threshold = config.maker.vol_extreme_threshold
    strategy.maker_vol_stressed_spread_mult = config.maker.vol_stressed_spread_mult
    strategy.maker_vol_stressed_size_mult = config.maker.vol_stressed_size_mult
    strategy.maker_vol_extreme_spread_mult = config.maker.vol_extreme_spread_mult
    strategy.maker_pennying_enabled = config.maker.pennying_enabled
    strategy.maker_pennying_min_edge = config.maker.pennying_min_edge
    strategy.maker_requote_max_per_sec = config.maker.requote_max_per_sec
    strategy.maker_requote_hysteresis_ticks = config.maker.requote_hysteresis_ticks
    strategy.maker_execution_penalty_enable = config.maker.execution_penalty_enable
    strategy.maker_execution_penalty_floor_usdc = config.maker.execution_penalty_floor_usdc
    strategy.maker_execution_slippage_spread_mult = config.maker.execution_slippage_spread_mult
    strategy.maker_execution_non_atomic_vol_mult = config.maker.execution_non_atomic_vol_mult
    strategy.maker_execution_depth_impact_mult = config.maker.execution_depth_impact_mult
    strategy.maker_execution_vwap_mult = config.maker.execution_vwap_mult
    strategy.maker_buy_taker_leakage_prob = config.maker.buy_taker_leakage_prob
    strategy.orderbook_fetch_interval_sec = config.maker.orderbook_fetch_interval_sec
    strategy.orderbook_levels_limit = config.maker.orderbook_levels_limit
    strategy.requote_bucket_tokens = strategy.maker_requote_max_per_sec
    strategy.requote_bucket_last_refill = time.time()
    strategy.maker_vol_warmup_quotes = config.maker.vol_warmup_quotes
    strategy.maker_vol_return_clip = config.maker.vol_return_clip
    strategy.maker_vol_rolling_window = config.maker.vol_rolling_window
    strategy.maker_vol_ewma_alpha = config.maker.vol_ewma_alpha
    strategy.maker_max_consecutive_denied = config.maker.max_consecutive_denied
    strategy.maker_order_ttl_sec = config.maker.order_ttl_sec
    strategy.maker_balance_pause_sec = config.maker.balance_pause_sec
    strategy.maker_error_pause_sec = config.maker.error_pause_sec
    strategy.maker_min_minutes_to_close = config.maker.min_minutes_to_close
    strategy.maker_min_fair_price = config.maker.min_fair_price
    strategy.maker_max_fair_price = config.maker.max_fair_price
    strategy.maker_reduce_only_no_new_sell_last_sec = config.maker.reduce_only_no_new_sell_last_sec
    strategy.maker_fee_rate_default_decimal = config.maker.fee_rate_default_decimal
    strategy.maker_fee_rate_legacy_bps_default = config.maker.fee_rate_legacy_bps_default
    strategy.maker_fee_rate_bps_default = int(
        (strategy.maker_fee_rate_default_decimal * Decimal("10000")).quantize(Decimal("1"))
    )
    strategy.maker_econ_fee_rate_decimal = config.maker.econ_fee_rate_decimal
    strategy.maker_max_order_usdc = config.maker.max_order_usdc
    strategy.maker_auto_tune = config.maker.auto_tune_enabled
    strategy.maker_auto_tune_interval_sec = config.maker.auto_tune_interval_sec
    strategy.maker_momentum_filter_pct = config.maker.momentum_filter_pct
    strategy.maker_momentum_buy_filter_pct = config.maker.momentum_buy_filter_pct
    strategy.maker_momentum_sell_filter_pct = config.maker.momentum_sell_filter_pct
    strategy.maker_momentum_window_ticks = config.maker.momentum_window_ticks
    strategy.bi_side_enabled = config.side.bi_side_enabled
    strategy.bi_side_decision_mode = config.side.decision_mode
    strategy.bi_side_default_mode = config.side.default_mode
    strategy.bi_side_decision_grace_sec = config.side.decision_grace_sec
    strategy.bi_side_lock_until_reduce_only = config.side.lock_until_reduce_only
    strategy.bi_side_allow_intramarket_flip = config.side.allow_intramarket_flip
    strategy.bi_side_min_score_up = config.side.min_score_up
    strategy.bi_side_max_score_down = config.side.max_score_down
    strategy.bi_side_mixed_low = config.side.mixed_low
    strategy.bi_side_mixed_high = config.side.mixed_high
    strategy.bi_side_regime_n_markets = config.side.regime_n_markets
    strategy.bi_side_regime_sum_pnl_usdc = config.side.regime_sum_pnl_usdc
    strategy.bi_side_regime_min_neg = config.side.regime_min_neg
    strategy.bi_side_mixed_policy = config.side.mixed_policy
    strategy.bi_side_mixed_small_size_mult = config.side.mixed_small_size_mult
    strategy.bi_side_down_size_mult = config.side.down_size_mult
    strategy.bi_side_min_time_left_sec = config.side.min_time_left_sec
    strategy.bi_side_reeval_interval_sec = config.side.reeval_interval_sec
    strategy.bi_side_decision_log_interval_sec = config.side.decision_log_interval_sec
    strategy.bi_side_flip_confirmations = config.side.flip_confirmations
    strategy.side_decision_engine_new = True
    strategy.side_signal_min_confidence = config.side.min_confidence
    strategy.side_signal_threshold_up = config.side.threshold_up
    strategy.side_signal_threshold_down = config.side.threshold_down
    strategy._new_signal_entry_score_abs_default = config.side.entry_score_abs_default
    strategy._new_signal_confident_score_abs_default = config.side.confident_score_abs_default
    strategy.bi_side_flip_min_score_up_new = config.side.flip_min_score_up_new
    strategy.bi_side_flip_max_score_down_new = config.side.flip_max_score_down_new
    strategy.bi_side_flip_confirmations_held_new = config.side.flip_confirmations_held
    strategy.bi_side_flip_min_persist_sec_held_new = config.side.flip_min_persist_sec_held
    held_flip_default = max(strategy._new_signal_confident_score_abs_default, Decimal("0.18"))
    strategy.bi_side_flip_min_score_up_held_new = config.side.flip_min_score_up_held_new
    strategy.bi_side_flip_max_score_down_held_new = config.side.flip_max_score_down_held_new
    strategy.directional_entry_min_score_abs_new = config.side.directional_entry_min_score_abs_new
    strategy.directional_entry_min_score_abs = strategy.directional_entry_min_score_abs_new
    strategy.continuation_entry_enabled = config.maker.continuation_entry_enabled
    strategy.continuation_entry_size_multiplier = config.maker.continuation_entry_size_multiplier
    strategy.trend_buy_enabled = config.maker.trend_buy_enabled
    strategy.trend_buy_min_score = config.maker.trend_buy_min_score
    strategy.trend_buy_min_net_usdc = config.maker.trend_buy_min_net_usdc
    strategy.trend_buy_penalty_discount = config.maker.trend_buy_penalty_discount
    strategy.trend_buy_min_time_left_sec = config.maker.trend_buy_min_time_left_sec
    strategy.trend_buy_max_price_premium_ps = config.maker.trend_buy_max_price_premium_ps
    strategy.trend_buy_size_multiplier = config.maker.trend_buy_size_multiplier
    strategy.trapped_inventory_recovery_enabled = config.maker.trapped_inventory_recovery_enabled
    strategy.trapped_inventory_recovery_min_qty = config.maker.trapped_inventory_recovery_min_qty
    strategy.trapped_inventory_recovery_max_robust_net_deficit_usdc = (
        config.maker.trapped_inventory_recovery_max_robust_net_deficit_usdc
    )
    strategy.maker_loss_sell_min_hold_sec = config.maker.loss_sell_min_hold_sec
    strategy.maker_loss_sell_reprice_min_interval_sec = config.maker.loss_sell_reprice_min_interval_sec
    strategy.side_signal_btc_ema_fast_sec = config.side.btc_ema_fast_sec
    strategy.side_signal_btc_ema_slow_sec = config.side.btc_ema_slow_sec
    strategy.side_signal_mid_ema_fast_sec = config.side.mid_ema_fast_sec
    strategy.side_signal_mid_ema_slow_sec = config.side.mid_ema_slow_sec
    strategy.side_signal_btc_trend_norm_pct = config.side.btc_trend_norm_pct
    strategy.side_signal_mid_velocity_reversal = config.side.mid_velocity_reversal
    strategy.maker_fair_pricer_mode = config.maker.fair_pricer_mode
    strategy.maker_digital_vol_window = config.maker.digital_vol_window
    strategy.maker_digital_vol_min_points = config.maker.digital_vol_min_points
    strategy.maker_digital_sigma_default = config.maker.digital_sigma_default
    strategy.maker_digital_sigma_floor = config.maker.digital_sigma_floor
    strategy.maker_digital_sigma_ceiling = config.maker.digital_sigma_ceiling
    strategy.maker_digital_vol_scale = config.maker.digital_vol_scale
    strategy.maker_digital_sigma_time_decay_enabled = config.maker.digital_sigma_time_decay_enabled
    strategy.maker_digital_sigma_time_decay_ref_sec = config.maker.digital_sigma_time_decay_ref_sec
    strategy.maker_digital_sigma_time_decay_min = config.maker.digital_sigma_time_decay_min
    strategy.taker_exit_enabled = config.exit.taker_exit_enabled
    strategy.taker_exit_min_net_usdc = config.exit.taker_exit_min_net_usdc
    strategy.taker_exit_stop_loss_usdc = config.exit.taker_exit_stop_loss_usdc
    strategy.taker_exit_max_hold_sec = config.exit.taker_exit_max_hold_sec
    strategy.taker_exit_min_hold_sec = config.exit.taker_exit_min_hold_sec
    strategy.taker_exit_cooldown_sec = config.exit.taker_exit_cooldown_sec
    strategy.taker_exit_eval_interval_sec = config.exit.taker_exit_eval_interval_sec
    strategy.taker_exit_slippage_buffer_pct = config.exit.taker_exit_slippage_buffer_pct
    strategy.taker_exit_only_on_profit = config.exit.taker_exit_only_on_profit
    strategy.taker_exit_max_spread_pct = config.exit.taker_exit_max_spread_pct
    strategy.taker_exit_stop_loss_max_spread_pct = config.exit.taker_exit_stop_loss_max_spread_pct
    strategy.taker_exit_wait_for_sell_quote_sec = config.exit.taker_exit_wait_for_sell_quote_sec
    strategy.market_stop_loss_max_per_market = config.exit.market_stop_loss_max_per_market
    strategy.market_max_buy_events_per_market = config.exit.market_max_buy_events_per_market
    strategy.taker_exit_max_hold_near_close_sec = config.exit.taker_exit_max_hold_near_close_sec
    strategy.taker_exit_reject_cooldown_sec = config.exit.taker_exit_reject_cooldown_sec
    strategy.taker_exit_skip_log_interval_sec = config.exit.taker_exit_skip_log_interval_sec
    strategy.taker_exit_disable_stop_loss_last_sec = config.exit.taker_exit_disable_stop_loss_last_sec
    strategy.taker_exit_stop_loss_confirmations = config.exit.taker_exit_stop_loss_confirmations
    strategy.stop_loss_reentry_cooldown_sec = config.exit.stop_loss_reentry_cooldown_sec
    strategy.exit_conviction_band_min_price = config.exit.exit_conviction_band_min_price
    strategy.exit_hold_band_min_price = config.exit.exit_hold_band_min_price
    strategy.exit_conviction_stop_loss_multiplier = config.exit.exit_conviction_stop_loss_multiplier
    strategy.exit_conviction_extra_confirmations = config.exit.exit_conviction_extra_confirmations
    strategy.exit_stop_loss_requires_thesis_weakening = config.exit.exit_stop_loss_requires_thesis_weakening
    strategy.exit_stop_loss_hold_on_none_signal = config.exit.exit_stop_loss_hold_on_none_signal
    strategy.exit_hold_band_requires_locked = config.exit.exit_hold_band_requires_locked
    strategy.maker_profit_run_enabled = config.exit.maker_profit_run_enabled
    strategy.maker_profit_run_min_hold_sec = config.exit.maker_profit_run_min_hold_sec
    strategy.maker_profit_run_min_profit_ps = config.exit.maker_profit_run_min_profit_ps
    strategy.maker_early_profit_hold_enabled = config.exit.maker_early_profit_hold_enabled
    strategy.maker_early_profit_hold_min_hold_sec = config.exit.maker_early_profit_hold_min_hold_sec
    strategy.maker_early_profit_hold_max_profit_ps = config.exit.maker_early_profit_hold_max_profit_ps
    strategy.maker_early_profit_hold_min_score_abs = config.exit.maker_early_profit_hold_min_score_abs
    strategy.maker_profit_run_trailing_drawdown_ps = config.exit.maker_profit_run_trailing_drawdown_ps
    strategy.maker_profit_run_unlock_profit_ps = config.exit.maker_profit_run_unlock_profit_ps
    strategy.maker_profit_run_unlock_trailing_drawdown_ps = config.exit.maker_profit_run_unlock_trailing_drawdown_ps
    strategy.maker_urgent_exit_enabled = config.exit.maker_urgent_exit_enabled
    strategy.maker_urgent_exit_min_loss_usdc = config.exit.maker_urgent_exit_min_loss_usdc
    strategy.maker_urgent_exit_ttl_sec = config.exit.maker_urgent_exit_ttl_sec
    strategy.maker_urgent_exit_cooldown_sec = config.exit.maker_urgent_exit_cooldown_sec
    strategy.maker_urgent_exit_min_confirmations = config.exit.maker_urgent_exit_min_confirmations
    strategy.maker_urgent_exit_winner_peak_profit_ps = config.exit.maker_urgent_exit_winner_peak_profit_ps
    strategy.maker_urgent_exit_winner_extra_confirmations = config.exit.maker_urgent_exit_winner_extra_confirmations
    strategy.side_thesis_weak_score_abs = config.side.side_thesis_weak_score_abs
    strategy.side_thesis_weak_requires_opposite_side_new = config.side.side_thesis_weak_requires_opposite_side_new
    strategy.side_thesis_weak_opposite_score_abs_new = config.side.side_thesis_weak_opposite_score_abs_new
    strategy.side_thesis_weak_confirmations_new = config.side.side_thesis_weak_confirmations_new
    strategy.side_thesis_weak_min_hold_sec_new = config.side.side_thesis_weak_min_hold_sec_new
    strategy.exit_conviction_band_min_score_abs = config.exit.exit_conviction_band_min_score_abs
    strategy.exit_hold_band_min_score_abs = config.exit.exit_hold_band_min_score_abs
    strategy.exit_hold_band_release_min_roi = config.exit.exit_hold_band_release_min_roi
    strategy.exit_stop_loss_thesis_min_score_abs = config.exit.exit_stop_loss_thesis_min_score_abs
    strategy.maker_profit_run_min_score_abs = config.exit.maker_profit_run_min_score_abs
    strategy.maker_winner_continuation_min_fair_edge_ps = config.exit.maker_winner_continuation_min_fair_edge_ps
    strategy.maker_implied_sigma_enabled = config.maker.implied_sigma_enabled
    strategy.maker_implied_sigma_weight = config.maker.implied_sigma_weight
    strategy.exit_policy = ExitPolicy(
        ExitPolicyConfig(
            aggressive_stage_sec=config.exit.exit_policy_aggressive_stage_sec,
            taker_stage_sec=config.exit.exit_policy_taker_stage_sec,
        )
    )
    strategy.exit_policy_engine = ExitPolicyEngine(
        ExitEngineConfig(
            min_hold_sec=strategy.taker_exit_min_hold_sec,
            stop_loss_usdc=strategy.taker_exit_stop_loss_usdc,
            stop_loss_confirmations=strategy.taker_exit_stop_loss_confirmations,
            stop_loss_requires_thesis_weakening=strategy.exit_stop_loss_requires_thesis_weakening,
            stop_loss_thesis_min_score_abs=strategy.exit_stop_loss_thesis_min_score_abs,
            stop_loss_hold_on_none_signal=strategy.exit_stop_loss_hold_on_none_signal,
            conviction_band_min_price=strategy.exit_conviction_band_min_price,
            hold_band_min_price=strategy.exit_hold_band_min_price,
            conviction_band_min_score_abs=strategy.exit_conviction_band_min_score_abs,
            hold_band_min_score_abs=strategy.exit_hold_band_min_score_abs,
            hold_band_release_min_roi=strategy.exit_hold_band_release_min_roi,
            conviction_stop_loss_multiplier=strategy.exit_conviction_stop_loss_multiplier,
            conviction_extra_confirmations=strategy.exit_conviction_extra_confirmations,
            hold_band_requires_locked=strategy.exit_hold_band_requires_locked,
        ),
    )
    strategy.position_manager = PositionManager(
        PositionManagerConfig(
            early_profit_hold_enabled=strategy.maker_early_profit_hold_enabled,
            early_profit_hold_min_hold_sec=strategy.maker_early_profit_hold_min_hold_sec,
            early_profit_hold_max_profit_ps=strategy.maker_early_profit_hold_max_profit_ps,
            early_profit_hold_min_score_abs=strategy.maker_early_profit_hold_min_score_abs,
            profit_run_enabled=strategy.maker_profit_run_enabled,
            profit_run_min_hold_sec=strategy.maker_profit_run_min_hold_sec,
            profit_run_min_profit_ps=strategy.maker_profit_run_min_profit_ps,
            profit_run_min_score_abs=strategy.maker_profit_run_min_score_abs,
            profit_run_trailing_drawdown_ps=strategy.maker_profit_run_trailing_drawdown_ps,
            profit_run_unlock_profit_ps=strategy.maker_profit_run_unlock_profit_ps,
            profit_run_unlock_trailing_drawdown_ps=strategy.maker_profit_run_unlock_trailing_drawdown_ps,
            stop_loss_entry_protection_sec=max(
                strategy.taker_exit_min_hold_sec,
                min(strategy.maker_early_profit_hold_min_hold_sec, 45),
            ),
            continuation_entry_protection_sec=max(
                max(
                    strategy.taker_exit_min_hold_sec,
                    min(strategy.maker_early_profit_hold_min_hold_sec, 45),
                ),
                60,
            ),
            stop_loss_regime_min_sec=max(
                8,
                int(getattr(strategy, "bi_side_flip_min_persist_sec_held_new", 8.0)),
            ),
            stop_loss_regime_confirmations=max(
                2,
                int(getattr(strategy, "bi_side_flip_confirmations_held_new", 4)),
            ),
            stop_loss_min_opposite_score_abs=strategy.side_thesis_weak_opposite_score_abs_new,
            winner_continuation_min_fair_edge_ps=strategy.maker_winner_continuation_min_fair_edge_ps,
        )
    )
    strategy.runtime_git_revision = detect_runtime_git_revision_fn(project_root)
    strategy.regime_guard_enabled = config.risk.regime_guard_enabled
    strategy.regime_guard_n_markets = config.risk.regime_guard_n_markets
    strategy.regime_guard_trigger_sum_pnl_usdc = config.risk.regime_guard_trigger_sum_pnl_usdc
    strategy.regime_guard_cooldown_sec = config.risk.regime_guard_cooldown_sec
    strategy.regime_guard_min_negative_markets = config.risk.regime_guard_min_negative_markets
    strategy.regime_guard_bootstrap_lookback_markets = config.risk.regime_guard_bootstrap_lookback_markets
    strategy.maker_sell_cost_protect_enabled = config.maker.sell_cost_protect_enabled
    strategy.maker_sell_cost_protect_fee_buffer_ps = config.maker.sell_cost_protect_fee_buffer_ps
    strategy.maker_sell_min_profit_floor_ps = config.maker.sell_min_profit_floor_ps
    strategy.maker_sell_cost_protect_emergency_last_sec = config.maker.sell_cost_protect_emergency_last_sec
    strategy.maker_profitable_sell_cap_enabled = config.maker.profitable_sell_cap_enabled
    strategy.maker_profitable_sell_cap_passive_offset_ps = config.maker.profitable_sell_cap_passive_offset_ps
    strategy.maker_profitable_sell_cap_aggressive_offset_ps = config.maker.profitable_sell_cap_aggressive_offset_ps
    strategy.maker_profitable_sell_cap_taker_offset_ps = config.maker.profitable_sell_cap_taker_offset_ps
    strategy.maker_high_cost_exit_cooldown_enabled = config.maker.high_cost_exit_cooldown_enabled
    strategy.maker_high_cost_fill_threshold = config.maker.high_cost_fill_threshold
    strategy.maker_high_cost_exit_cooldown_sec = config.maker.high_cost_exit_cooldown_sec
    strategy.regime_guard_policy = RegimeGuardPolicy(
        RegimeGuardConfig(
            n_markets=strategy.regime_guard_n_markets,
            trigger_sum_pnl_usdc=strategy.regime_guard_trigger_sum_pnl_usdc,
            min_negative_markets=strategy.regime_guard_min_negative_markets,
        )
    )
    strategy.maker_cancel_max_retries = config.maker.cancel_max_retries
    strategy.maker_cancel_cooldown_sec = config.maker.cancel_cooldown_sec
    strategy.maker_cancel_ack_timeout_sec = config.maker.cancel_ack_timeout_sec
    strategy.maker_requote_min_age_sec = config.maker.requote_min_age_sec
    strategy.maker_requote_min_age_sec_sell = config.maker.requote_min_age_sec_sell
    strategy.maker_early_sell_only_sec = config.maker.early_sell_only_sec
    strategy.quote_healthcheck_interval_sec = config.market_data.quote_healthcheck_interval_sec
    strategy.strategy_status_interval_sec = config.observability.strategy_status_interval_sec
    strategy.quote_stale_sec = config.market_data.quote_stale_sec
    strategy.quote_invalid_tick_reload_threshold = config.market_data.quote_invalid_tick_reload_threshold
    strategy.quote_reload_cooldown_sec = config.market_data.quote_reload_cooldown_sec
    strategy.last_quote_update_ts = 0.0
    strategy.quote_pause_until_ts = 0.0
    strategy.post_fill_buy_cooldown_sec = config.maker.post_fill_buy_cooldown_sec
    strategy.buy_cooldown_until_ts = 0.0
    strategy.max_consecutive_losses = config.maker.max_consecutive_losses
    strategy.loss_pause_sec = config.maker.loss_pause_sec
    strategy.recent_fill_pnl_results = []
    strategy.fill_cooldown_policy = FillCooldownPolicy(
        FillCooldownConfig(
            post_fill_buy_cooldown_sec=strategy.post_fill_buy_cooldown_sec,
            max_consecutive_losses=strategy.max_consecutive_losses,
            loss_pause_sec=strategy.loss_pause_sec,
        )
    )
    strategy.last_valid_quote_ts = 0.0
    strategy.consecutive_invalid_quote_ticks = 0
    strategy.last_quote_watchdog_check_ts = 0.0
    strategy.last_quote_watchdog_reload_ts = 0.0
    maker_config = MakerEngineConfig(
        maker_half_spread=strategy.maker_half_spread,
        maker_quote_size_usdc=strategy.maker_quote_size_usdc,
        maker_min_shares=strategy.maker_min_shares,
        maker_fixed_shares=strategy.maker_fixed_shares,
        maker_max_order_usdc=strategy.maker_max_order_usdc,
        maker_adverse_selection_buffer=strategy.maker_adverse_selection_buffer,
        maker_min_expected_net_usdc=strategy.maker_min_expected_net_usdc,
        maker_quote_sides=strategy.maker_quote_sides,
        maker_inventory_skew_max=strategy.maker_inventory_skew_max,
        maker_max_inventory_shares=strategy.maker_max_inventory_shares,
        maker_stale_inventory_sec=strategy.maker_stale_inventory_sec,
        maker_stale_inventory_multiplier=strategy.maker_stale_inventory_multiplier,
        maker_vol_stressed_threshold=strategy.maker_vol_stressed_threshold,
        maker_vol_extreme_threshold=strategy.maker_vol_extreme_threshold,
        maker_vol_stressed_spread_mult=strategy.maker_vol_stressed_spread_mult,
        maker_vol_stressed_size_mult=strategy.maker_vol_stressed_size_mult,
        maker_vol_extreme_spread_mult=strategy.maker_vol_extreme_spread_mult,
        maker_pennying_enabled=strategy.maker_pennying_enabled,
        maker_pennying_min_edge=strategy.maker_pennying_min_edge,
        maker_execution_penalty_enable=strategy.maker_execution_penalty_enable,
        maker_execution_penalty_floor_usdc=strategy.maker_execution_penalty_floor_usdc,
        maker_execution_slippage_spread_mult=strategy.maker_execution_slippage_spread_mult,
        maker_execution_non_atomic_vol_mult=strategy.maker_execution_non_atomic_vol_mult,
        maker_execution_depth_impact_mult=strategy.maker_execution_depth_impact_mult,
        maker_execution_vwap_mult=strategy.maker_execution_vwap_mult,
        maker_buy_taker_leakage_prob=strategy.maker_buy_taker_leakage_prob,
    )
    strategy.maker_engine = MakerEngine(maker_config)
    strategy.last_status_log_ts = 0.0
    strategy.orderbook_unavailable_until_ts = 0.0
    strategy.orderbook_unavailable_token = None
    strategy.last_external_spot = None
    strategy.latest_external_spot = None
    strategy.latest_external_spot_source = ""
    strategy.latest_external_spot_source_ts = 0.0
    strategy.external_spot_consecutive_failures = 0
    strategy.external_spot_max_failures = config.market_data.external_spot_max_failures
    strategy.external_spot_history = []
    strategy.external_spot_history_max = config.market_data.external_spot_history_max
    strategy.polymarket_chainlink_history = []
    strategy.polymarket_chainlink_history_max = config.market_data.polymarket_chainlink_history_max
    strategy.market_strike_cache_by_slug = {}
    strategy.market_strike_source_by_slug = {}
    strategy.market_start_ts_by_slug = {}
    strategy.market_strike_anchor_max_lag_sec = config.market_data.market_strike_anchor_max_lag_sec
    strategy.market_strike_anchor_near_sec = config.market_data.market_strike_anchor_near_sec
    strategy.market_strike_rest_retry_sec = config.market_data.market_strike_rest_retry_sec
    strategy.market_strike_rest_last_try_ts_by_slug = {}
    strategy.market_strike_gamma_validate_interval_sec = config.market_data.market_strike_gamma_validate_interval_sec
    strategy.market_strike_gamma_warn_abs_usd = config.market_data.market_strike_gamma_warn_abs_usd
    strategy.market_strike_gamma_mismatch_warn_interval_sec = config.market_data.market_strike_gamma_warn_interval_sec
    strategy.market_strike_last_gamma_validate_ts_by_slug = {}
    strategy.market_strike_last_gamma_warn_ts_by_slug = {}
    strategy._last_strike_slug_log_ts = 0.0
    strategy.no_quote_diag_interval_sec = config.observability.no_quote_diag_interval_sec
    strategy._last_no_quote_diag_ts_by_inst = {}
    strategy._last_sellable_skip_log_ts_by_inst = {}
    strategy.sellable_fallback_after_buy_sec = config.operations.sellable_fallback_after_buy_sec
    strategy.sellable_after_buy_buffer_shares = config.operations.sellable_after_buy_buffer_shares
    strategy.maker_gate_block_grace_sec = config.maker.gate_block_grace_sec
    strategy._gate_block_since_by_order_key = {}
    strategy._gate_block_reason_by_order_key = {}
    strategy._gate_last_cancel_ts_by_order_key = {}
    strategy._cancel_ack_dedupe_window_sec = config.maker.cancel_ack_dedupe_window_sec
    strategy._last_cancel_ack_ts_by_client_order_id = {}
    strategy._target_anchor_price_by_order_key = {}
    strategy._target_version_by_order_key = {}
    strategy.strike_fallback_log_interval_sec = config.observability.strike_fallback_log_interval_sec
    strategy._last_strike_fallback_log_ts = 0.0
    strategy._last_digital_pricer_log_ts = 0.0
    strategy.live_inventory_cost = {}
    strategy.last_taker_exit_ts_by_inst = {}
    bind_market_cycle_state(strategy, MarketCycleState())
    strategy.market_cycle_realized_net_usdc = Decimal("0")
    strategy.recent_market_combined_pnls = deque(maxlen=strategy.regime_guard_n_markets)
    strategy.regime_guard_conservative_until_ts = 0.0
    strategy.fee_log_interval_sec = config.observability.fee_log_interval_sec
    strategy._last_fee_log_state_by_token = {}
    strategy.fee_rate_fetch_interval_sec = config.market_data.fee_rate_fetch_interval_sec
    strategy._fee_rate_local_cache_by_token = {}
    strategy.latest_market_bid = None
    strategy.latest_market_ask = None
    strategy.latest_market_bid_ts = 0.0
    strategy.latest_market_ask_ts = 0.0
    strategy.stale_quote_synth_max_age_sec = config.market_data.stale_quote_synth_max_age_sec
    strategy._inventory_delta_shares = Decimal("0")
    strategy._startup_rehydrated_inventory_force_sell_only = False
    strategy.inventory_last_update_ts = 0.0
    strategy.consecutive_denied_orders = 0
    strategy.maker_kill_switch = False
    strategy.active_maker_orders = {}
    strategy.current_token_id = None
    strategy.current_market_instruments = []
    strategy.current_up_instrument_id = None
    strategy.current_down_instrument_id = None
    strategy.current_market_open_spot = None
    strategy.active_side = ActiveSide.UP if not strategy.bi_side_enabled else ActiveSide.NONE
    strategy.active_side_locked = False
    strategy.side_decision_ts = 0.0
    strategy.side_decision_score = Decimal("0")
    strategy.side_decision_reason = "startup"
    strategy.side_decision_due_ts = 0.0
    strategy.side_decision_done_for_market = False
    strategy.side_decision_inputs = {}
    strategy._force_quote_refresh_once = False
    strategy._force_quote_refresh_reason = ""
    strategy.side_flip_count = 0
    strategy.side_pending_flip_side = ActiveSide.NONE
    strategy.side_pending_flip_count = 0
    strategy._last_side_observation_signature = None
    strategy._side_decision_skip_log_ts_by_reason = {}
    strategy.side_decision_skip_log_interval_sec = config.side.skip_log_interval_sec
    strategy._last_side_decision_log_ts = 0.0
    strategy._last_side_decision_log_signature = None
    strategy.last_observed_fee_rate_bps = None
    clob_base = config.market_data.clob_base_url
    fee_ttl = config.market_data.fee_rate_cache_ttl_sec
    strategy.fee_rate_client = FeeRateClient(base_url=clob_base, ttl_sec=fee_ttl)
    strategy.rebate_reporter = RebateReporter(output_dir=config.observability.rebate_report_dir)
    strategy.auto_tune_enabled = config.maker.auto_tune_enabled
    strategy.auto_tune_interval_sec = config.maker.auto_tune_interval_sec
    strategy.last_auto_tune_ts = 0.0
    strategy.parameter_tuner = ParameterTuner()
    strategy._stopping = False
    strategy._reload_stop_event = threading.Event()
    strategy._reload_thread = None
    strategy._quote_watchdog_stop_event = threading.Event()
    strategy._quote_watchdog_thread = None
    strategy.auto_redeem_enabled = config.operations.auto_redeem_enabled
    strategy.auto_redeem_apply = config.operations.auto_redeem_apply
    strategy.auto_redeem_interval_sec = config.operations.auto_redeem_interval_sec
    strategy.auto_redeem_on_rollover = config.operations.auto_redeem_on_rollover
    strategy.auto_redeem_timeout_sec = config.operations.auto_redeem_timeout_sec
    strategy.auto_redeem_min_gap_sec = config.operations.auto_redeem_min_gap_sec
    strategy.auto_redeem_slug_filter = config.operations.auto_redeem_slug_filter
    strategy._redeem_stop_event = threading.Event()
    strategy._redeem_thread = None
    strategy._redeem_job_lock = threading.Lock()
    strategy._last_redeem_run_ts = 0.0
    strategy._balance_stop_event = threading.Event()
    strategy._balance_thread = None
    strategy._balance_refresh_lock = threading.Lock()
    strategy._balance_refresh_inflight = False
    strategy.current_market_slug = None
    strategy.market_phase = MarketPhase.WAITING
    strategy.market_settling_grace_sec = config.operations.market_settling_grace_sec
    strategy.market_next_poll_sec = config.operations.market_next_poll_sec
    strategy._market_settling_since_ts = 0.0
    strategy.next_market_slug = None
    strategy.next_market_start_ts = None
    strategy._lifecycle_stop_event = threading.Event()
    strategy._lifecycle_thread = None
    strategy._terminal_dashboard_stop_event = threading.Event()
    strategy._terminal_dashboard_thread = None
    strategy._cached_usdc_balance = None
    strategy._balance_last_check_ts = 0.0
    strategy._last_balance_log_ts = 0.0
    strategy._last_logged_balance_value = None
    strategy.balance_check_interval_sec = config.operations.balance_check_interval_sec
    strategy.conditional_balance_check_interval_sec = config.operations.conditional_balance_check_interval_sec
    strategy.conditional_balance_safety_buffer_pct = config.operations.conditional_balance_safety_buffer_pct
    strategy._conditional_balance_cache_by_token = {}
    strategy._sell_reject_pause_until_by_inst = {}
    strategy._sell_recovery_required_by_inst = {}
    strategy._sell_recovery_reason_by_inst = {}
    strategy._sell_recovery_venue_cap_by_inst = {}
    strategy.sell_recovery_qty_buffer_shares = config.operations.sell_recovery_qty_buffer_shares
    strategy.sell_delay_after_buy_sec = config.operations.sell_delay_after_buy_sec
    strategy.sell_balance_retry_pause_sec = config.operations.sell_balance_retry_pause_sec
    strategy._binance_ws_price = None
    strategy._binance_ws_price_ts = 0.0
    strategy._binance_ws_stop_event = threading.Event()
    strategy._binance_ws_thread = None
    strategy._polymarket_chainlink_price = None
    strategy._polymarket_chainlink_price_ts = 0.0
    strategy._polymarket_chainlink_event_ts_ms = None
    strategy._polymarket_chainlink_ws_stop_event = threading.Event()
    strategy._polymarket_chainlink_ws_thread = None
    from bot.signal_engine import SignalEngine, SignalEngineConfig
    strategy._signal_engine = SignalEngine(SignalEngineConfig(
        btc_ema_fast_sec=strategy.side_signal_btc_ema_fast_sec,
        btc_ema_slow_sec=strategy.side_signal_btc_ema_slow_sec,
        mid_ema_fast_sec=strategy.side_signal_mid_ema_fast_sec,
        mid_ema_slow_sec=strategy.side_signal_mid_ema_slow_sec,
        min_confidence=strategy.side_signal_min_confidence,
        btc_trend_norm_pct=strategy.side_signal_btc_trend_norm_pct,
        mid_velocity_reversal_threshold=strategy.side_signal_mid_velocity_reversal,
    ))
    strategy._maker_worker_lock = threading.Lock()
    strategy._maker_worker_running = False
    strategy.run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    strategy.trade_db_enabled = config.operations.trade_db_enabled
    strategy.trade_db = TradeJournalDB(
        db_path=config.operations.trade_db_path,
    ) if strategy.trade_db_enabled else None
    strategy._cycle_total_trades = 0
    strategy._cycle_total_wins = 0
