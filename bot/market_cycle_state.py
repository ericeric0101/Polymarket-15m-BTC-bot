from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MarketCycleState:
    pending_taker_exit_by_inst: dict[str, Any] = field(default_factory=dict)
    taker_exit_tail_attempted_by_inst: dict[str, Any] = field(default_factory=dict)
    taker_exit_last_eval_ts_by_inst: dict[str, float] = field(default_factory=dict)
    taker_exit_reject_cooldown_until_by_inst: dict[str, float] = field(default_factory=dict)
    taker_exit_stop_loss_hits_by_inst: dict[str, Any] = field(default_factory=dict)
    stop_loss_execution_priority_by_inst: dict[str, bool] = field(default_factory=dict)
    stop_loss_reentry_pause_until_by_inst: dict[str, float] = field(default_factory=dict)
    side_stop_loss_penalty_until_by_market_side: dict[str, float] = field(default_factory=dict)
    market_stop_loss_count_by_slug: dict[str, int] = field(default_factory=dict)
    market_buy_count_by_slug: dict[str, int] = field(default_factory=dict)
    thesis_epoch_by_slug: dict[str, int] = field(default_factory=dict)
    market_buy_counted_order_ids_by_slug: dict[str, set[str]] = field(default_factory=dict)
    taker_exit_reason_by_client_order_id: dict[str, str] = field(default_factory=dict)
    taker_exit_skip_log_ts_by_key: dict[str, float] = field(default_factory=dict)
    high_cost_exit_cooldown_until_by_inst: dict[str, float] = field(default_factory=dict)
    high_cost_last_fill_price_by_inst: dict[str, float] = field(default_factory=dict)
    sell_reject_pause_until_by_inst: dict[str, float] = field(default_factory=dict)
    conditional_balance_cache_by_token: dict[str, Any] = field(default_factory=dict)
    latest_quote_depth_by_inst: dict[str, Any] = field(default_factory=dict)
    latest_quote_by_inst: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    last_quote_update_ts_by_inst: dict[str, float] = field(default_factory=dict)
    last_edge_observation_signature_by_inst: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    last_edge_observation_ts_by_inst: dict[str, float] = field(default_factory=dict)
    maker_profit_run_peak_bid_by_inst: dict[str, Any] = field(default_factory=dict)
    maker_profit_run_peak_fair_by_inst: dict[str, Any] = field(default_factory=dict)
    recent_buy_fill_ts_by_inst: dict[str, float] = field(default_factory=dict)
    recent_sell_fill_ts_by_inst: dict[str, float] = field(default_factory=dict)
    side_thesis_weak_hits_by_inst: dict[str, int] = field(default_factory=dict)
    orderbook_levels_cache_by_token: dict[str, Any] = field(default_factory=dict)
    maker_signal_flip_hits_by_inst: dict[str, int] = field(default_factory=dict)
    side_invalidation_hits_by_slug: dict[str, int] = field(default_factory=dict)
    side_invalidation_confirmed_by_slug: dict[str, bool] = field(default_factory=dict)
    strike_pending_log_state_by_slug: dict[str, str] = field(default_factory=dict)
    baseline_metrics: dict[str, Any] = field(default_factory=dict)


def bind_market_cycle_state(strategy: Any, state: MarketCycleState) -> None:
    strategy.market_cycle_state = state
    strategy.pending_taker_exit_by_inst = state.pending_taker_exit_by_inst
    strategy.taker_exit_tail_attempted_by_inst = state.taker_exit_tail_attempted_by_inst
    strategy.taker_exit_last_eval_ts_by_inst = state.taker_exit_last_eval_ts_by_inst
    strategy.taker_exit_reject_cooldown_until_by_inst = state.taker_exit_reject_cooldown_until_by_inst
    strategy.taker_exit_stop_loss_hits_by_inst = state.taker_exit_stop_loss_hits_by_inst
    strategy._stop_loss_execution_priority_by_inst = state.stop_loss_execution_priority_by_inst
    strategy.stop_loss_reentry_pause_until_by_inst = state.stop_loss_reentry_pause_until_by_inst
    strategy.side_stop_loss_penalty_until_by_market_side = state.side_stop_loss_penalty_until_by_market_side
    strategy.market_stop_loss_count_by_slug = state.market_stop_loss_count_by_slug
    strategy.market_buy_count_by_slug = state.market_buy_count_by_slug
    strategy._thesis_epoch_by_slug = state.thesis_epoch_by_slug
    strategy.market_buy_counted_order_ids_by_slug = state.market_buy_counted_order_ids_by_slug
    strategy.taker_exit_reason_by_client_order_id = state.taker_exit_reason_by_client_order_id
    strategy._taker_exit_skip_log_ts_by_key = state.taker_exit_skip_log_ts_by_key
    strategy.high_cost_exit_cooldown_until_by_inst = state.high_cost_exit_cooldown_until_by_inst
    strategy.high_cost_last_fill_price_by_inst = state.high_cost_last_fill_price_by_inst
    strategy._sell_reject_pause_until_by_inst = state.sell_reject_pause_until_by_inst
    strategy._conditional_balance_cache_by_token = state.conditional_balance_cache_by_token
    strategy.latest_quote_depth_by_inst = state.latest_quote_depth_by_inst
    strategy.latest_quote_by_inst = state.latest_quote_by_inst
    strategy.last_quote_update_ts_by_inst = state.last_quote_update_ts_by_inst
    strategy._last_edge_observation_signature_by_inst = state.last_edge_observation_signature_by_inst
    strategy._last_edge_observation_ts_by_inst = state.last_edge_observation_ts_by_inst
    strategy.maker_profit_run_peak_bid_by_inst = state.maker_profit_run_peak_bid_by_inst
    strategy.maker_profit_run_peak_fair_by_inst = state.maker_profit_run_peak_fair_by_inst
    strategy.recent_buy_fill_ts_by_inst = state.recent_buy_fill_ts_by_inst
    strategy.recent_sell_fill_ts_by_inst = state.recent_sell_fill_ts_by_inst
    strategy.side_thesis_weak_hits_by_inst = state.side_thesis_weak_hits_by_inst
    strategy.orderbook_levels_cache_by_token = state.orderbook_levels_cache_by_token
    strategy._maker_signal_flip_hits = state.maker_signal_flip_hits_by_inst
    strategy._side_invalidation_hits_by_slug = state.side_invalidation_hits_by_slug
    strategy._side_invalidation_confirmed_by_slug = state.side_invalidation_confirmed_by_slug
    strategy._strike_pending_log_state_by_slug = state.strike_pending_log_state_by_slug
    strategy._baseline_metrics = state.baseline_metrics
