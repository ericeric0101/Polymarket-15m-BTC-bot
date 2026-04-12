from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from bot.enums import ActiveSide
from bot.quoting import normalize_quote_mode
from execution.rebate_model import CRYPTO_FEE_CURVE


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_bool_inverted(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None else default


def _env_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name)
    return Decimal(raw if raw is not None else default)


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw is not None else default


@dataclass(frozen=True)
class ObservabilityConfig:
    startup_verbose: bool
    terminal_dashboard_enabled: bool
    terminal_dashboard_refresh_sec: float
    strategy_status_interval_sec: int
    no_quote_diag_interval_sec: int
    fee_log_interval_sec: int
    strike_fallback_log_interval_sec: int
    rebate_report_dir: str


@dataclass(frozen=True)
class CompatibilityConfig:
    patch_mode: str
    auto_apply_patches: bool

    def __post_init__(self) -> None:
        if self.patch_mode not in {"off", "verify", "runtime"}:
            raise ValueError(f"Unsupported compatibility patch mode: {self.patch_mode}")


@dataclass(frozen=True)
class MakerConfig:
    maker_mode: bool
    quote_refresh_sec: int
    half_spread: Decimal
    quote_size_usdc: Decimal
    min_shares: Decimal
    exchange_min_shares: Decimal
    fixed_shares: Decimal
    quote_sides: str
    directional_edge_gate_enabled: bool
    min_directional_edge_ps: Decimal
    min_directional_edge_ps_down: Decimal
    min_directional_edge_ps_conservative: Decimal
    min_expected_net_usdc: Decimal
    reload_inventory_threshold_shares: Decimal
    reload_min_expected_net_multiplier: Decimal
    reload_min_directional_edge_ps: Decimal
    adverse_selection_buffer: Decimal
    use_post_only: bool
    post_only_strict: bool
    max_inventory_shares: Decimal
    inventory_skew_max: Decimal
    stale_inventory_sec: int
    stale_inventory_multiplier: Decimal
    kill_switch_reset_on_rollover: bool
    vol_real_history_max: int
    vol_stressed_threshold: Decimal
    vol_extreme_threshold: Decimal
    vol_stressed_spread_mult: Decimal
    vol_stressed_size_mult: Decimal
    vol_extreme_spread_mult: Decimal
    pennying_enabled: bool
    pennying_min_edge: Decimal
    requote_max_per_sec: float
    requote_hysteresis_ticks: Decimal
    execution_penalty_enable: bool
    execution_penalty_floor_usdc: Decimal
    execution_slippage_spread_mult: Decimal
    execution_non_atomic_vol_mult: Decimal
    execution_depth_impact_mult: Decimal
    execution_vwap_mult: Decimal
    buy_taker_leakage_prob: Decimal
    orderbook_fetch_interval_sec: int
    orderbook_levels_limit: int
    vol_warmup_quotes: int
    vol_return_clip: Decimal
    vol_rolling_window: int
    vol_ewma_alpha: float
    max_consecutive_denied: int
    order_ttl_sec: int
    balance_pause_sec: int
    error_pause_sec: int
    min_minutes_to_close: float
    min_fair_price: Decimal
    max_fair_price: Decimal
    reduce_only_no_new_sell_last_sec: int
    fee_rate_default_decimal: Decimal
    fee_rate_legacy_bps_default: int
    econ_fee_rate_decimal: Decimal
    max_order_usdc: Decimal
    auto_tune_enabled: bool
    auto_tune_interval_sec: int
    momentum_filter_pct: Decimal
    momentum_buy_filter_pct: Decimal
    momentum_sell_filter_pct: Decimal
    momentum_window_ticks: int
    fair_pricer_mode: str
    digital_vol_window: int
    digital_vol_min_points: int
    digital_sigma_default: Decimal
    digital_sigma_floor: Decimal
    digital_sigma_ceiling: Decimal
    digital_vol_scale: Decimal
    digital_sigma_time_decay_enabled: bool
    digital_sigma_time_decay_ref_sec: float
    digital_sigma_time_decay_min: float
    implied_sigma_enabled: bool
    implied_sigma_weight: Decimal
    continuation_entry_enabled: bool
    continuation_entry_size_multiplier: Decimal
    trend_buy_enabled: bool
    trend_buy_min_score: Decimal
    trend_buy_min_net_usdc: Decimal
    trend_buy_penalty_discount: Decimal
    trend_buy_min_time_left_sec: float
    trend_buy_max_price_premium_ps: Decimal
    trend_buy_size_multiplier: Decimal
    trapped_inventory_recovery_enabled: bool
    trapped_inventory_recovery_min_qty: Decimal
    trapped_inventory_recovery_max_robust_net_deficit_usdc: Decimal
    sell_cost_protect_enabled: bool
    sell_cost_protect_fee_buffer_ps: Decimal
    sell_min_profit_floor_ps: Decimal
    sell_cost_protect_emergency_last_sec: int
    profitable_sell_cap_enabled: bool
    profitable_sell_cap_passive_offset_ps: Decimal
    profitable_sell_cap_aggressive_offset_ps: Decimal
    profitable_sell_cap_taker_offset_ps: Decimal
    loss_sell_min_hold_sec: float
    loss_sell_reprice_min_interval_sec: float
    high_cost_exit_cooldown_enabled: bool
    high_cost_fill_threshold: Decimal
    high_cost_exit_cooldown_sec: int
    cancel_max_retries: int
    cancel_cooldown_sec: int
    cancel_ack_timeout_sec: int
    requote_min_age_sec: float
    requote_min_age_sec_sell: float
    early_sell_only_sec: int
    post_fill_buy_cooldown_sec: float
    max_consecutive_losses: int
    loss_pause_sec: float
    gate_block_grace_sec: int
    cancel_ack_dedupe_window_sec: int

    def __post_init__(self) -> None:
        if self.half_spread < 0:
            raise ValueError("MAKER_HALF_SPREAD must be >= 0")
        if self.quote_size_usdc <= 0:
            raise ValueError("MAKER_QUOTE_SIZE_USDC must be > 0")
        if self.min_shares <= 0 or self.exchange_min_shares <= 0:
            raise ValueError("Maker min share settings must be > 0")
        if self.reload_inventory_threshold_shares < 0:
            raise ValueError("MAKER_RELOAD_INVENTORY_THRESHOLD_SHARES must be >= 0")
        if self.continuation_entry_size_multiplier <= 0:
            raise ValueError("CONTINUATION_ENTRY_SIZE_MULTIPLIER must be > 0")
        if self.trapped_inventory_recovery_min_qty < 0:
            raise ValueError("TRAPPED_INVENTORY_RECOVERY_MIN_QTY must be >= 0")
        if self.loss_sell_min_hold_sec < 0:
            raise ValueError("MAKER_LOSS_SELL_MIN_HOLD_SEC must be >= 0")
        if self.loss_sell_reprice_min_interval_sec < 0:
            raise ValueError("MAKER_LOSS_SELL_REPRICE_MIN_INTERVAL_SEC must be >= 0")


@dataclass(frozen=True)
class SideDecisionConfig:
    bi_side_enabled: bool
    decision_mode: str
    default_mode: str
    decision_grace_sec: int
    lock_until_reduce_only: bool
    allow_intramarket_flip: bool
    min_score_up: Decimal
    max_score_down: Decimal
    mixed_low: Decimal
    mixed_high: Decimal
    regime_n_markets: int
    regime_sum_pnl_usdc: Decimal
    regime_min_neg: int
    mixed_policy: str
    mixed_small_size_mult: Decimal
    down_size_mult: Decimal
    min_time_left_sec: int
    reeval_interval_sec: float
    decision_log_interval_sec: float
    flip_confirmations: int
    flip_confirmations_held: int
    flip_min_persist_sec_held: float
    min_confidence: float
    threshold_up: float
    threshold_down: float
    entry_score_abs_default: Decimal
    confident_score_abs_default: Decimal
    flip_min_score_up_new: Decimal
    flip_max_score_down_new: Decimal
    flip_min_score_up_held_new: Decimal
    flip_max_score_down_held_new: Decimal
    directional_entry_min_score_abs_new: Decimal
    directional_first_entry_min_score_abs_new: Decimal
    side_thesis_weak_score_abs: Decimal
    side_thesis_weak_requires_opposite_side_new: bool
    side_thesis_weak_opposite_score_abs_new: Decimal
    side_thesis_weak_confirmations_new: int
    side_thesis_weak_min_hold_sec_new: int
    btc_ema_fast_sec: float
    btc_ema_slow_sec: float
    mid_ema_fast_sec: float
    mid_ema_slow_sec: float
    btc_trend_norm_pct: float
    mid_velocity_reversal: float
    skip_log_interval_sec: float

    def __post_init__(self) -> None:
        if self.flip_confirmations < 1 or self.flip_confirmations_held < 1:
            raise ValueError("Side flip confirmations must be >= 1")
        if self.side_thesis_weak_confirmations_new < 1:
            raise ValueError("SIDE_THESIS_WEAK_CONFIRMATIONS_NEW must be >= 1")
        if self.directional_entry_min_score_abs_new < 0:
            raise ValueError("DIRECTIONAL_ENTRY_MIN_SCORE_ABS_NEW must be >= 0")
        if self.directional_first_entry_min_score_abs_new < 0:
            raise ValueError("DIRECTIONAL_FIRST_ENTRY_MIN_SCORE_ABS_NEW must be >= 0")
        if self.side_thesis_weak_opposite_score_abs_new < 0:
            raise ValueError("SIDE_THESIS_WEAK_OPPOSITE_SCORE_ABS_NEW must be >= 0")


@dataclass(frozen=True)
class ExitConfig:
    taker_exit_enabled: bool
    taker_exit_min_net_usdc: Decimal
    taker_exit_stop_loss_usdc: Decimal
    catastrophic_stop_loss_enabled: bool
    catastrophic_stop_loss_usdc: Decimal
    catastrophic_stop_loss_min_score_abs: Decimal
    catastrophic_stop_loss_confirmations: int
    taker_exit_max_hold_sec: int
    taker_exit_min_hold_sec: int
    taker_exit_cooldown_sec: int
    taker_exit_eval_interval_sec: float
    taker_exit_slippage_buffer_pct: Decimal
    taker_exit_only_on_profit: bool
    taker_exit_max_spread_pct: Decimal
    taker_exit_stop_loss_max_spread_pct: Decimal
    taker_exit_wait_for_sell_quote_sec: int
    market_stop_loss_max_per_market: int
    market_max_buy_events_per_market: int
    taker_exit_max_hold_near_close_sec: int
    taker_exit_reject_cooldown_sec: int
    taker_exit_skip_log_interval_sec: int
    taker_exit_disable_stop_loss_last_sec: int
    taker_exit_stop_loss_confirmations: int
    stop_loss_reentry_cooldown_sec: int
    exit_conviction_band_min_price: Decimal
    exit_hold_band_min_price: Decimal
    exit_conviction_stop_loss_multiplier: Decimal
    exit_conviction_extra_confirmations: int
    exit_stop_loss_requires_thesis_weakening: bool
    exit_stop_loss_hold_on_none_signal: bool
    exit_hold_band_requires_locked: bool
    maker_profit_run_enabled: bool
    maker_profit_run_min_hold_sec: int
    maker_profit_run_min_profit_ps: Decimal
    maker_early_profit_hold_enabled: bool
    maker_early_profit_hold_min_hold_sec: int
    maker_early_profit_hold_max_profit_ps: Decimal
    maker_early_profit_hold_min_score_abs: Decimal
    maker_profit_run_trailing_drawdown_ps: Decimal
    maker_profit_run_fair_veto_min: Decimal
    maker_profit_run_unlock_profit_ps: Decimal
    maker_profit_run_unlock_trailing_drawdown_ps: Decimal
    maker_urgent_exit_enabled: bool
    maker_urgent_exit_min_loss_usdc: Decimal
    maker_urgent_exit_ttl_sec: int
    maker_urgent_exit_cooldown_sec: int
    maker_urgent_exit_min_confirmations: int
    maker_urgent_exit_winner_peak_profit_ps: Decimal
    maker_urgent_exit_winner_extra_confirmations: int
    exit_conviction_band_min_score_abs: Decimal
    exit_hold_band_min_score_abs: Decimal
    exit_hold_band_release_min_roi: Decimal
    exit_stop_loss_thesis_min_score_abs: Decimal
    maker_profit_run_min_score_abs: Decimal
    maker_winner_continuation_min_fair_edge_ps: Decimal
    exit_policy_aggressive_stage_sec: int
    exit_policy_taker_stage_sec: int
    maker_winner_sell_max_offset_ps: Decimal
    maker_signal_flip_cooldown_cycles: int

    def __post_init__(self) -> None:
        if self.taker_exit_stop_loss_confirmations < 1:
            raise ValueError("TAKER_EXIT_STOP_LOSS_CONFIRMATIONS must be >= 1")
        if self.catastrophic_stop_loss_confirmations < 1:
            raise ValueError("CATASTROPHIC_STOP_LOSS_CONFIRMATIONS must be >= 1")
        if self.maker_urgent_exit_min_confirmations < 1:
            raise ValueError("MAKER_URGENT_EXIT_MIN_CONFIRMATIONS must be >= 1")


@dataclass(frozen=True)
class RiskConfig:
    regime_guard_enabled: bool
    regime_guard_n_markets: int
    regime_guard_trigger_sum_pnl_usdc: Decimal
    regime_guard_cooldown_sec: int
    regime_guard_min_negative_markets: int
    regime_guard_bootstrap_lookback_markets: int


@dataclass(frozen=True)
class MarketDataConfig:
    external_spot_max_failures: int
    external_spot_history_max: int
    polymarket_chainlink_history_max: int
    market_strike_anchor_max_lag_sec: int
    market_strike_anchor_near_sec: int
    market_strike_rest_retry_sec: int
    market_strike_gamma_validate_interval_sec: int
    market_strike_gamma_warn_abs_usd: Decimal
    market_strike_gamma_warn_interval_sec: int
    quote_healthcheck_interval_sec: int
    quote_stale_sec: int
    quote_invalid_tick_reload_threshold: int
    quote_reload_cooldown_sec: int
    stale_quote_synth_max_age_sec: float
    fee_rate_fetch_interval_sec: int
    fee_rate_cache_ttl_sec: int
    clob_base_url: str


@dataclass(frozen=True)
class OperationsConfig:
    sellable_fallback_after_buy_sec: int
    sellable_after_buy_buffer_shares: Decimal
    auto_redeem_enabled: bool
    auto_redeem_apply: bool
    auto_redeem_interval_sec: int
    auto_redeem_on_rollover: bool
    auto_redeem_timeout_sec: int
    auto_redeem_min_gap_sec: int
    auto_redeem_slug_filter: str
    market_settling_grace_sec: int
    market_next_poll_sec: int
    balance_check_interval_sec: int
    conditional_balance_check_interval_sec: int
    conditional_balance_safety_buffer_pct: Decimal
    sell_recovery_qty_buffer_shares: Decimal
    sell_delay_after_buy_sec: float
    sell_balance_retry_pause_sec: float
    trade_db_enabled: bool
    trade_db_path: str


@dataclass(frozen=True)
class AppConfig:
    observability: ObservabilityConfig
    compatibility: CompatibilityConfig
    maker: MakerConfig
    side: SideDecisionConfig
    exit: ExitConfig
    risk: RiskConfig
    market_data: MarketDataConfig
    operations: OperationsConfig

    @classmethod
    def from_env(cls, *, enable_terminal_dashboard: bool) -> "AppConfig":
        startup_verbose = _env_bool("STARTUP_VERBOSE", False)
        terminal_dashboard_enabled = enable_terminal_dashboard or _env_bool("TERMINAL_DASHBOARD", False)
        terminal_dashboard_refresh_sec = max(0.5, _env_float("TERMINAL_DASHBOARD_REFRESH_SEC", 1.0))
        quote_sides_raw = _env_str("MAKER_QUOTE_SIDES", "both").strip().lower()
        default_mode = _env_str("BI_SIDE_DEFAULT_MODE", "NONE").strip().upper()
        if default_mode not in {ActiveSide.UP.value, ActiveSide.DOWN.value, ActiveSide.NONE.value}:
            default_mode = ActiveSide.NONE.value
        threshold_up = _env_float("SIDE_SIGNAL_THRESHOLD_UP", 0.05)
        threshold_down = _env_float("SIDE_SIGNAL_THRESHOLD_DOWN", 0.05)
        min_confidence = _env_float("SIDE_SIGNAL_MIN_CONFIDENCE", 0.15)
        entry_score_abs_default = Decimal(str(max(threshold_up, threshold_down)))
        confident_score_abs_default = Decimal(str(max(min_confidence, float(entry_score_abs_default))))

        maker_min_shares = _env_decimal("MAKER_MIN_SHARES", "5")
        maker_exchange_min_shares = _env_decimal("MAKER_EXCHANGE_MIN_SHARES", "5")
        maker_fixed_shares = _env_decimal("MAKER_FIXED_SHARES", "0")
        maker_reload_inventory_threshold_shares = _env_decimal(
            "MAKER_RELOAD_INVENTORY_THRESHOLD_SHARES",
            str(maker_fixed_shares if maker_fixed_shares > 0 else maker_min_shares),
        )
        maker_min_directional_edge_ps_conservative = _env_decimal("MAKER_MIN_DIRECTIONAL_EDGE_PS_CONSERVATIVE", "0.03")
        fee_rate_default_decimal = _env_decimal("MAKER_FEE_RATE_DEFAULT_DECIMAL", str(CRYPTO_FEE_CURVE.fee_rate))
        if fee_rate_default_decimal <= 0:
            fee_rate_default_decimal = CRYPTO_FEE_CURVE.fee_rate
        econ_fee_rate_decimal = _env_decimal("MAKER_ECON_FEE_RATE_DECIMAL", "0")
        if econ_fee_rate_decimal < 0:
            econ_fee_rate_decimal = Decimal("0")
        buy_taker_leakage_prob = max(
            Decimal("0"),
            min(Decimal("1"), _env_decimal("MAKER_BUY_TAKER_LEAKAGE_PROB", "0.15")),
        )

        bi_side_flip_confirmations = max(1, _env_int("BI_SIDE_FLIP_CONFIRMATIONS", 2))
        bi_side_flip_confirmations_held = max(
            bi_side_flip_confirmations,
            _env_int("BI_SIDE_FLIP_CONFIRMATIONS_HELD_NEW", max(bi_side_flip_confirmations, 4)),
        )
        held_flip_default = max(confident_score_abs_default, Decimal("0.18"))

        regime_guard_n_markets = max(2, _env_int("REGIME_GUARD_N_MARKETS", 4))
        regime_guard_min_negative_markets = max(
            1,
            min(
                regime_guard_n_markets,
                _env_int(
                    "REGIME_GUARD_MIN_NEGATIVE_MARKETS",
                    max(1, regime_guard_n_markets - 1),
                ),
            ),
        )
        regime_guard_bootstrap_lookback_markets = max(
            regime_guard_n_markets,
            _env_int(
                "REGIME_GUARD_BOOTSTRAP_LOOKBACK_MARKETS",
                regime_guard_n_markets * 3,
            ),
        )

        external_spot_history_max = max(60, _env_int("EXTERNAL_SPOT_HISTORY_MAX", 1200))
        fee_rate_cache_ttl_sec = _env_int("FEE_RATE_CACHE_TTL_SEC", 300)

        return cls(
            observability=ObservabilityConfig(
                startup_verbose=startup_verbose,
                terminal_dashboard_enabled=terminal_dashboard_enabled,
                terminal_dashboard_refresh_sec=terminal_dashboard_refresh_sec,
                strategy_status_interval_sec=max(15, _env_int("STRATEGY_STATUS_INTERVAL_SEC", 60)),
                no_quote_diag_interval_sec=max(15, _env_int("NO_QUOTE_DIAG_INTERVAL_SEC", 60)),
                fee_log_interval_sec=max(5, _env_int("FEE_LOG_INTERVAL_SEC", 60)),
                strike_fallback_log_interval_sec=max(10, _env_int("STRIKE_FALLBACK_LOG_INTERVAL_SEC", 60)),
                rebate_report_dir=_env_str("REBATE_REPORT_DIR", "./logs/rebate"),
            ),
            compatibility=CompatibilityConfig(
                patch_mode=_env_str("NAUTILUS_COMPAT_PATCH_MODE", "runtime").strip().lower(),
                auto_apply_patches=_env_bool_inverted("AUTO_APPLY_NAUTILUS_PATCH", True),
            ),
            maker=MakerConfig(
                maker_mode=_env_bool_inverted("MAKER_MODE", True),
                quote_refresh_sec=_env_int("MAKER_QUOTE_REFRESH_SEC", 5),
                half_spread=_env_decimal("MAKER_HALF_SPREAD", "0.01"),
                quote_size_usdc=_env_decimal("MAKER_QUOTE_SIZE_USDC", "1.0"),
                min_shares=maker_min_shares,
                exchange_min_shares=maker_exchange_min_shares,
                fixed_shares=maker_fixed_shares,
                quote_sides=normalize_quote_mode(quote_sides_raw),
                directional_edge_gate_enabled=_env_bool("MAKER_DIRECTIONAL_EDGE_GATE_ENABLED", False),
                min_directional_edge_ps=_env_decimal("MAKER_MIN_DIRECTIONAL_EDGE_PS", "0.02"),
                min_directional_edge_ps_down=_env_decimal(
                    "MAKER_MIN_DIRECTIONAL_EDGE_PS_DOWN",
                    _env_str("MAKER_MIN_DIRECTIONAL_EDGE_PS", "0.02"),
                ),
                min_directional_edge_ps_conservative=maker_min_directional_edge_ps_conservative,
                min_expected_net_usdc=_env_decimal("MAKER_MIN_EXPECTED_NET_USDC", "0.0001"),
                reload_inventory_threshold_shares=maker_reload_inventory_threshold_shares,
                reload_min_expected_net_multiplier=_env_decimal("MAKER_RELOAD_MIN_EXPECTED_NET_MULTIPLIER", "2.0"),
                reload_min_directional_edge_ps=_env_decimal(
                    "MAKER_RELOAD_MIN_DIRECTIONAL_EDGE_PS",
                    str(maker_min_directional_edge_ps_conservative),
                ),
                adverse_selection_buffer=_env_decimal("MAKER_ADVERSE_SELECTION_BUFFER", "0.0005"),
                use_post_only=_env_bool("MAKER_POST_ONLY", False),
                post_only_strict=_env_bool_inverted("MAKER_POST_ONLY_STRICT", True),
                max_inventory_shares=_env_decimal("MAKER_MAX_INVENTORY_SHARES", "25"),
                inventory_skew_max=_env_decimal("MAKER_INVENTORY_SKEW_MAX", "0.03"),
                stale_inventory_sec=_env_int("MAKER_STALE_INVENTORY_SEC", 30),
                stale_inventory_multiplier=_env_decimal("MAKER_STALE_INVENTORY_MULTIPLIER", "2.0"),
                kill_switch_reset_on_rollover=_env_bool_inverted("MAKER_KILL_SWITCH_RESET_ON_ROLLOVER", True),
                vol_real_history_max=_env_int("MAKER_VOL_REAL_HISTORY_MAX", 300),
                vol_stressed_threshold=_env_decimal("MAKER_VOL_STRESSED_THRESHOLD", "0.015"),
                vol_extreme_threshold=_env_decimal("MAKER_VOL_EXTREME_THRESHOLD", "0.08"),
                vol_stressed_spread_mult=_env_decimal("MAKER_VOL_STRESSED_SPREAD_MULT", "2.0"),
                vol_stressed_size_mult=_env_decimal("MAKER_VOL_STRESSED_SIZE_MULT", "0.5"),
                vol_extreme_spread_mult=_env_decimal("MAKER_VOL_EXTREME_SPREAD_MULT", "3.0"),
                pennying_enabled=_env_bool_inverted("MAKER_PENNYING_ENABLED", True),
                pennying_min_edge=_env_decimal("MAKER_PENNYING_MIN_EDGE", "0.005"),
                requote_max_per_sec=_env_float("MAX_REQUOTE_PER_SEC", 1.0),
                requote_hysteresis_ticks=_env_decimal("REQUOTE_HYSTERESIS_TICKS", "1"),
                execution_penalty_enable=_env_bool_inverted("MAKER_EXECUTION_PENALTY_ENABLE", True),
                execution_penalty_floor_usdc=_env_decimal("MAKER_EXECUTION_PENALTY_FLOOR_USDC", "0.001"),
                execution_slippage_spread_mult=_env_decimal("MAKER_EXECUTION_SLIPPAGE_SPREAD_MULT", "0.15"),
                execution_non_atomic_vol_mult=_env_decimal("MAKER_EXECUTION_NON_ATOMIC_VOL_MULT", "0.2"),
                execution_depth_impact_mult=_env_decimal("MAKER_EXECUTION_DEPTH_IMPACT_MULT", "1.0"),
                execution_vwap_mult=_env_decimal("MAKER_EXECUTION_VWAP_MULT", "0.5"),
                buy_taker_leakage_prob=buy_taker_leakage_prob,
                orderbook_fetch_interval_sec=max(1, _env_int("ORDERBOOK_FETCH_INTERVAL_SEC", 5)),
                orderbook_levels_limit=max(1, _env_int("ORDERBOOK_LEVELS_LIMIT", 10)),
                vol_warmup_quotes=_env_int("MAKER_VOL_WARMUP_QUOTES", 30),
                vol_return_clip=_env_decimal("MAKER_VOL_RETURN_CLIP", "0.20"),
                vol_rolling_window=_env_int("MAKER_VOL_ROLLING_WINDOW", 30),
                vol_ewma_alpha=_env_float("MAKER_VOL_EWMA_ALPHA", 0.35),
                max_consecutive_denied=max(1, _env_int("MAKER_MAX_CONSECUTIVE_DENIED", 5)),
                order_ttl_sec=_env_int("MAKER_ORDER_TTL_SEC", 20),
                balance_pause_sec=_env_int("MAKER_BALANCE_PAUSE_SEC", 60),
                error_pause_sec=_env_int("MAKER_ERROR_PAUSE_SEC", 30),
                min_minutes_to_close=_env_float("MAKER_MIN_MINUTES_TO_CLOSE", 3.0),
                min_fair_price=_env_decimal("MAKER_MIN_FAIR_PRICE", "0.05"),
                max_fair_price=_env_decimal("MAKER_MAX_FAIR_PRICE", "0.95"),
                reduce_only_no_new_sell_last_sec=max(0, _env_int("MAKER_REDUCE_ONLY_NO_NEW_SELL_LAST_SEC", 45)),
                fee_rate_default_decimal=fee_rate_default_decimal,
                fee_rate_legacy_bps_default=_env_int("MAKER_FEE_RATE_BPS_DEFAULT", 0),
                econ_fee_rate_decimal=econ_fee_rate_decimal,
                max_order_usdc=_env_decimal("MAKER_MAX_ORDER_USDC", "1.0"),
                auto_tune_enabled=_env_bool_inverted("MAKER_AUTO_TUNE", True),
                auto_tune_interval_sec=_env_int("MAKER_AUTO_TUNE_INTERVAL_SEC", 300),
                momentum_filter_pct=_env_decimal("MAKER_MOMENTUM_FILTER_PCT", "0.06"),
                momentum_buy_filter_pct=_env_decimal(
                    "MAKER_MOMENTUM_BUY_FILTER_PCT",
                    _env_str("MAKER_MOMENTUM_FILTER_PCT", "0.04"),
                ),
                momentum_sell_filter_pct=_env_decimal(
                    "MAKER_MOMENTUM_SELL_FILTER_PCT",
                    _env_str("MAKER_MOMENTUM_FILTER_PCT", "0.02"),
                ),
                momentum_window_ticks=_env_int("MAKER_MOMENTUM_WINDOW_TICKS", 20),
                fair_pricer_mode=(
                    _env_str("MAKER_FAIR_PRICER_MODE", "drift").strip().lower()
                    if _env_str("MAKER_FAIR_PRICER_MODE", "drift").strip().lower() in {"drift", "digital"}
                    else "drift"
                ),
                digital_vol_window=max(10, _env_int("MAKER_DIGITAL_VOL_WINDOW", 120)),
                digital_vol_min_points=max(5, _env_int("MAKER_DIGITAL_VOL_MIN_POINTS", 20)),
                digital_sigma_default=_env_decimal("MAKER_DIGITAL_SIGMA_DEFAULT", "0.60"),
                digital_sigma_floor=_env_decimal("MAKER_DIGITAL_SIGMA_FLOOR", "0.20"),
                digital_sigma_ceiling=_env_decimal("MAKER_DIGITAL_SIGMA_CEILING", "2.00"),
                digital_vol_scale=_env_decimal("MAKER_DIGITAL_VOL_SCALE", "1.00"),
                digital_sigma_time_decay_enabled=_env_bool("MAKER_DIGITAL_SIGMA_TIME_DECAY", True),
                digital_sigma_time_decay_ref_sec=_env_float("MAKER_DIGITAL_SIGMA_TIME_DECAY_REF_SEC", 600.0),
                digital_sigma_time_decay_min=_env_float("MAKER_DIGITAL_SIGMA_TIME_DECAY_MIN", 0.30),
                implied_sigma_enabled=_env_bool("MAKER_DIGITAL_IMPLIED_SIGMA_ENABLED", True),
                implied_sigma_weight=_env_decimal("MAKER_DIGITAL_IMPLIED_SIGMA_WEIGHT", "0.50"),
                continuation_entry_enabled=_env_bool_inverted("CONTINUATION_ENTRY_ENABLED", True),
                continuation_entry_size_multiplier=_env_decimal("CONTINUATION_ENTRY_SIZE_MULTIPLIER", "1.0"),
                trend_buy_enabled=_env_bool("TREND_BUY_ENABLED", False),
                trend_buy_min_score=_env_decimal("TREND_BUY_MIN_SCORE", "0.20"),
                trend_buy_min_net_usdc=_env_decimal("TREND_BUY_MIN_NET_USDC", "-0.005"),
                trend_buy_penalty_discount=max(
                    Decimal("0"),
                    min(Decimal("1"), _env_decimal("TREND_BUY_PENALTY_DISCOUNT", "0.50")),
                ),
                trend_buy_min_time_left_sec=_env_float("TREND_BUY_MIN_TIME_LEFT_SEC", 300.0),
                trend_buy_max_price_premium_ps=_env_decimal("TREND_BUY_MAX_PRICE_PREMIUM_PS", "0.02"),
                trend_buy_size_multiplier=_env_decimal("TREND_BUY_SIZE_MULTIPLIER", "1.0"),
                trapped_inventory_recovery_enabled=_env_bool_inverted("TRAPPED_INVENTORY_RECOVERY_ENABLED", True),
                trapped_inventory_recovery_min_qty=_env_decimal("TRAPPED_INVENTORY_RECOVERY_MIN_QTY", "1.0"),
                trapped_inventory_recovery_max_robust_net_deficit_usdc=_env_decimal(
                    "TRAPPED_INVENTORY_RECOVERY_MAX_ROBUST_NET_DEFICIT_USDC",
                    "0.05",
                ),
                sell_cost_protect_enabled=_env_bool_inverted("MAKER_SELL_COST_PROTECT_ENABLED", True),
                sell_cost_protect_fee_buffer_ps=_env_decimal("MAKER_SELL_COST_PROTECT_FEE_BUFFER_PS", "0.005"),
                sell_min_profit_floor_ps=_env_decimal("MAKER_SELL_MIN_PROFIT_FLOOR_PS", "0"),
                sell_cost_protect_emergency_last_sec=max(0, _env_int("MAKER_SELL_COST_PROTECT_EMERGENCY_LAST_SEC", 60)),
                profitable_sell_cap_enabled=_env_bool("MAKER_PROFITABLE_SELL_CAP_ENABLED", True),
                profitable_sell_cap_passive_offset_ps=_env_decimal("MAKER_PROFITABLE_SELL_CAP_PASSIVE_OFFSET_PS", "0.02"),
                profitable_sell_cap_aggressive_offset_ps=_env_decimal("MAKER_PROFITABLE_SELL_CAP_AGGRESSIVE_OFFSET_PS", "0.01"),
                profitable_sell_cap_taker_offset_ps=_env_decimal("MAKER_PROFITABLE_SELL_CAP_TAKER_OFFSET_PS", "0.005"),
                loss_sell_min_hold_sec=max(0.0, _env_float("MAKER_LOSS_SELL_MIN_HOLD_SEC", 60.0)),
                loss_sell_reprice_min_interval_sec=max(
                    0.0,
                    _env_float("MAKER_LOSS_SELL_REPRICE_MIN_INTERVAL_SEC", 45.0),
                ),
                high_cost_exit_cooldown_enabled=_env_bool_inverted("MAKER_HIGH_COST_EXIT_COOLDOWN_ENABLED", True),
                high_cost_fill_threshold=_env_decimal("MAKER_HIGH_COST_FILL_THRESHOLD", "0.75"),
                high_cost_exit_cooldown_sec=max(0, _env_int("MAKER_HIGH_COST_EXIT_COOLDOWN_SEC", 180)),
                cancel_max_retries=_env_int("MAKER_CANCEL_MAX_RETRIES", 3),
                cancel_cooldown_sec=_env_int("MAKER_CANCEL_COOLDOWN_SEC", 2),
                cancel_ack_timeout_sec=_env_int("MAKER_CANCEL_ACK_TIMEOUT_SEC", 8),
                requote_min_age_sec=max(0.0, _env_float("MAKER_REQUOTE_MIN_AGE_SEC", 6.0)),
                requote_min_age_sec_sell=max(0.0, _env_float("MAKER_REQUOTE_MIN_AGE_SEC_SELL", 0.0)),
                early_sell_only_sec=max(0, _env_int("MAKER_EARLY_SELL_ONLY_SEC", 120)),
                post_fill_buy_cooldown_sec=max(0.0, _env_float("MAKER_POST_FILL_BUY_COOLDOWN_SEC", 15.0)),
                max_consecutive_losses=_env_int("MAKER_MAX_CONSECUTIVE_LOSSES", 3),
                loss_pause_sec=_env_float("MAKER_LOSS_PAUSE_SEC", 60.0),
                gate_block_grace_sec=max(0, _env_int("MAKER_GATE_BLOCK_GRACE_SEC", 4)),
                cancel_ack_dedupe_window_sec=max(1, _env_int("MAKER_CANCEL_ACK_DEDUPE_WINDOW_SEC", 3)),
            ),
            side=SideDecisionConfig(
                bi_side_enabled=_env_bool("BI_SIDE_ENABLED", False),
                decision_mode=_env_str("BI_SIDE_DECISION_MODE", "boundary_only").strip().lower(),
                default_mode=default_mode,
                decision_grace_sec=max(0, _env_int("BI_SIDE_DECISION_GRACE_SEC", 30)),
                lock_until_reduce_only=_env_bool_inverted("BI_SIDE_LOCK_UNTIL_REDUCE_ONLY", True),
                allow_intramarket_flip=_env_bool("BI_SIDE_ALLOW_INTRAMARKET_FLIP", False),
                min_score_up=_env_decimal("BI_SIDE_MIN_SCORE_UP", "1"),
                max_score_down=_env_decimal("BI_SIDE_MAX_SCORE_DOWN", "-1"),
                mixed_low=_env_decimal("BI_SIDE_MIXED_LOW", "-1"),
                mixed_high=_env_decimal("BI_SIDE_MIXED_HIGH", "1"),
                regime_n_markets=max(2, _env_int("BI_SIDE_REGIME_N_MARKETS", 4)),
                regime_sum_pnl_usdc=_env_decimal("BI_SIDE_REGIME_SUM_PNL_USDC", "-2.0"),
                regime_min_neg=max(1, _env_int("BI_SIDE_REGIME_MIN_NEG", 3)),
                mixed_policy=_env_str("BI_SIDE_MIXED_POLICY", "none").strip().lower(),
                mixed_small_size_mult=_env_decimal("BI_SIDE_MIXED_SMALL_SIZE_MULT", "0.0"),
                down_size_mult=_env_decimal("BI_SIDE_DOWN_SIZE_MULT", "1.0"),
                min_time_left_sec=max(0, _env_int("BI_SIDE_MIN_TIME_LEFT_SEC", 180)),
                reeval_interval_sec=max(0.2, _env_float("BI_SIDE_REEVAL_INTERVAL_SEC", 1.0)),
                decision_log_interval_sec=max(1.0, _env_float("BI_SIDE_LOG_INTERVAL_SEC", 15.0)),
                flip_confirmations=bi_side_flip_confirmations,
                flip_confirmations_held=bi_side_flip_confirmations_held,
                flip_min_persist_sec_held=max(0.0, _env_float("BI_SIDE_FLIP_MIN_PERSIST_SEC_HELD_NEW", 8.0)),
                min_confidence=min_confidence,
                threshold_up=threshold_up,
                threshold_down=threshold_down,
                entry_score_abs_default=entry_score_abs_default,
                confident_score_abs_default=confident_score_abs_default,
                flip_min_score_up_new=_env_decimal("BI_SIDE_FLIP_MIN_SCORE_UP_NEW", str(confident_score_abs_default)),
                flip_max_score_down_new=_env_decimal("BI_SIDE_FLIP_MAX_SCORE_DOWN_NEW", str(-confident_score_abs_default)),
                flip_min_score_up_held_new=_env_decimal("BI_SIDE_FLIP_MIN_SCORE_UP_HELD_NEW", str(held_flip_default)),
                flip_max_score_down_held_new=_env_decimal("BI_SIDE_FLIP_MAX_SCORE_DOWN_HELD_NEW", str(-held_flip_default)),
                directional_entry_min_score_abs_new=_env_decimal("DIRECTIONAL_ENTRY_MIN_SCORE_ABS_NEW", str(entry_score_abs_default)),
                directional_first_entry_min_score_abs_new=min(
                    _env_decimal(
                        "DIRECTIONAL_FIRST_ENTRY_MIN_SCORE_ABS_NEW",
                        str(entry_score_abs_default),
                    ),
                    max(entry_score_abs_default, Decimal("0.18")),
                ),
                side_thesis_weak_score_abs=_env_decimal("SIDE_THESIS_WEAK_SCORE_ABS_NEW", str(entry_score_abs_default)),
                side_thesis_weak_requires_opposite_side_new=_env_bool_inverted("SIDE_THESIS_WEAK_REQUIRES_OPPOSITE_SIDE_NEW", True),
                side_thesis_weak_opposite_score_abs_new=_env_decimal("SIDE_THESIS_WEAK_OPPOSITE_SCORE_ABS_NEW", str(held_flip_default)),
                side_thesis_weak_confirmations_new=max(1, _env_int("SIDE_THESIS_WEAK_CONFIRMATIONS_NEW", 3)),
                side_thesis_weak_min_hold_sec_new=max(0, _env_int("SIDE_THESIS_WEAK_MIN_HOLD_SEC_NEW", 45)),
                btc_ema_fast_sec=_env_float("SIDE_SIGNAL_BTC_EMA_FAST_SEC", 3.0),
                btc_ema_slow_sec=_env_float("SIDE_SIGNAL_BTC_EMA_SLOW_SEC", 10.0),
                mid_ema_fast_sec=_env_float("SIDE_SIGNAL_MID_EMA_FAST_SEC", 5.0),
                mid_ema_slow_sec=_env_float("SIDE_SIGNAL_MID_EMA_SLOW_SEC", 20.0),
                btc_trend_norm_pct=_env_float("SIDE_SIGNAL_BTC_TREND_NORM_PCT", 0.0005),
                mid_velocity_reversal=_env_float("SIDE_SIGNAL_MID_VELOCITY_REVERSAL", 0.010),
                skip_log_interval_sec=max(2.0, _env_float("BI_SIDE_SKIP_LOG_INTERVAL_SEC", 10.0)),
            ),
            exit=ExitConfig(
                taker_exit_enabled=_env_bool_inverted("TAKER_EXIT_ENABLED", True),
                taker_exit_min_net_usdc=_env_decimal("TAKER_EXIT_MIN_NET_USDC", "0.02"),
                taker_exit_stop_loss_usdc=_env_decimal("TAKER_EXIT_STOP_LOSS_USDC", "0.15"),
                catastrophic_stop_loss_enabled=_env_bool_inverted("CATASTROPHIC_STOP_LOSS_ENABLED", True),
                catastrophic_stop_loss_usdc=_env_decimal("CATASTROPHIC_STOP_LOSS_USDC", "0.40"),
                catastrophic_stop_loss_min_score_abs=_env_decimal(
                    "CATASTROPHIC_STOP_LOSS_MIN_SCORE_ABS_NEW",
                    "0.50",
                ),
                catastrophic_stop_loss_confirmations=max(
                    1,
                    _env_int("CATASTROPHIC_STOP_LOSS_CONFIRMATIONS", 2),
                ),
                taker_exit_max_hold_sec=_env_int("TAKER_EXIT_MAX_HOLD_SEC", 120),
                taker_exit_min_hold_sec=_env_int("TAKER_EXIT_MIN_HOLD_SEC", 20),
                taker_exit_cooldown_sec=_env_int("TAKER_EXIT_COOLDOWN_SEC", 8),
                taker_exit_eval_interval_sec=max(0.0, _env_float("TAKER_EXIT_EVAL_INTERVAL_SEC", 1.0)),
                taker_exit_slippage_buffer_pct=_env_decimal("TAKER_EXIT_SLIPPAGE_BUFFER_PCT", "0.002"),
                taker_exit_only_on_profit=_env_bool("TAKER_EXIT_ONLY_ON_PROFIT", False),
                taker_exit_max_spread_pct=_env_decimal("TAKER_EXIT_MAX_SPREAD_PCT", "0.02"),
                taker_exit_stop_loss_max_spread_pct=_env_decimal("TAKER_EXIT_STOP_LOSS_MAX_SPREAD_PCT", "0.03"),
                taker_exit_wait_for_sell_quote_sec=max(0, _env_int("TAKER_EXIT_WAIT_FOR_SELL_QUOTE_SEC", 20)),
                market_stop_loss_max_per_market=max(0, _env_int("MARKET_STOP_LOSS_MAX_PER_MARKET", 2)),
                market_max_buy_events_per_market=max(0, _env_int("MARKET_MAX_BUY_EVENTS_PER_MARKET", 2)),
                taker_exit_max_hold_near_close_sec=max(0, _env_int("TAKER_EXIT_MAX_HOLD_NEAR_CLOSE_SEC", 90)),
                taker_exit_reject_cooldown_sec=max(0, _env_int("TAKER_EXIT_REJECT_COOLDOWN_SEC", 20)),
                taker_exit_skip_log_interval_sec=max(1, _env_int("TAKER_EXIT_SKIP_LOG_INTERVAL_SEC", 20)),
                taker_exit_disable_stop_loss_last_sec=max(0, _env_int("TAKER_EXIT_DISABLE_STOP_LOSS_LAST_SEC", 45)),
                taker_exit_stop_loss_confirmations=max(1, _env_int("TAKER_EXIT_STOP_LOSS_CONFIRMATIONS", 2)),
                stop_loss_reentry_cooldown_sec=max(0, _env_int("STOP_LOSS_REENTRY_COOLDOWN_SEC", 180)),
                exit_conviction_band_min_price=_env_decimal("EXIT_CONVICTION_BAND_MIN_PRICE", "0.60"),
                exit_hold_band_min_price=_env_decimal("EXIT_HOLD_BAND_MIN_PRICE", "0.68"),
                exit_conviction_stop_loss_multiplier=_env_decimal("EXIT_CONVICTION_STOP_LOSS_MULTIPLIER", "1.75"),
                exit_conviction_extra_confirmations=max(0, _env_int("EXIT_CONVICTION_EXTRA_CONFIRMATIONS", 1)),
                exit_stop_loss_requires_thesis_weakening=_env_bool_inverted("EXIT_STOP_LOSS_REQUIRES_THESIS_WEAKENING", True),
                exit_stop_loss_hold_on_none_signal=_env_bool_inverted("EXIT_STOP_LOSS_HOLD_ON_NONE_SIGNAL", True),
                exit_hold_band_requires_locked=_env_bool_inverted("EXIT_HOLD_BAND_REQUIRES_LOCKED", True),
                maker_profit_run_enabled=_env_bool_inverted("MAKER_PROFIT_RUN_ENABLED", True),
                maker_profit_run_min_hold_sec=max(0, _env_int("MAKER_PROFIT_RUN_MIN_HOLD_SEC", 20)),
                maker_profit_run_min_profit_ps=_env_decimal("MAKER_PROFIT_RUN_MIN_PROFIT_PS", "0.04"),
                maker_early_profit_hold_enabled=_env_bool_inverted("MAKER_EARLY_PROFIT_HOLD_ENABLED", True),
                maker_early_profit_hold_min_hold_sec=max(0, _env_int("MAKER_EARLY_PROFIT_HOLD_MIN_HOLD_SEC", 60)),
                maker_early_profit_hold_max_profit_ps=_env_decimal("MAKER_EARLY_PROFIT_HOLD_MAX_PROFIT_PS", "0.08"),
                maker_early_profit_hold_min_score_abs=_env_decimal("MAKER_EARLY_PROFIT_HOLD_MIN_SCORE_ABS_NEW", str(confident_score_abs_default)),
                maker_profit_run_trailing_drawdown_ps=_env_decimal("MAKER_PROFIT_RUN_TRAILING_DRAWDOWN_PS", "0.05"),
                maker_profit_run_fair_veto_min=_env_decimal("MAKER_PROFIT_RUN_FAIR_VETO_MIN", "0.65"),
                maker_profit_run_unlock_profit_ps=_env_decimal("MAKER_PROFIT_RUN_UNLOCK_PROFIT_PS", "0.18"),
                maker_profit_run_unlock_trailing_drawdown_ps=_env_decimal("MAKER_PROFIT_RUN_UNLOCK_TRAILING_DRAWDOWN_PS", "0.02"),
                maker_urgent_exit_enabled=_env_bool("MAKER_URGENT_EXIT_ENABLED", True),
                maker_urgent_exit_min_loss_usdc=_env_decimal("MAKER_URGENT_EXIT_MIN_LOSS_USDC", "0.10"),
                maker_urgent_exit_ttl_sec=max(5, _env_int("MAKER_URGENT_EXIT_TTL_SEC", 15)),
                maker_urgent_exit_cooldown_sec=max(1, _env_int("MAKER_URGENT_EXIT_COOLDOWN_SEC", 5)),
                maker_urgent_exit_min_confirmations=max(1, _env_int("MAKER_URGENT_EXIT_MIN_CONFIRMATIONS", 3)),
                maker_urgent_exit_winner_peak_profit_ps=_env_decimal("MAKER_URGENT_EXIT_WINNER_PEAK_PROFIT_PS", "0.08"),
                maker_urgent_exit_winner_extra_confirmations=max(0, _env_int("MAKER_URGENT_EXIT_WINNER_EXTRA_CONFIRMATIONS", 2)),
                exit_conviction_band_min_score_abs=_env_decimal("EXIT_CONVICTION_BAND_MIN_SCORE_ABS_NEW", str(confident_score_abs_default)),
                exit_hold_band_min_score_abs=_env_decimal("EXIT_HOLD_BAND_MIN_SCORE_ABS_NEW", str(confident_score_abs_default)),
                exit_hold_band_release_min_roi=_env_decimal("EXIT_HOLD_BAND_RELEASE_MIN_ROI", "0.15"),
                exit_stop_loss_thesis_min_score_abs=_env_decimal("EXIT_STOP_LOSS_THESIS_MIN_SCORE_ABS_NEW", str(entry_score_abs_default)),
                maker_profit_run_min_score_abs=_env_decimal("MAKER_PROFIT_RUN_MIN_SCORE_ABS_NEW", str(confident_score_abs_default)),
                maker_winner_continuation_min_fair_edge_ps=_env_decimal("MAKER_WINNER_CONTINUATION_MIN_FAIR_EDGE_PS", "0.04"),
                exit_policy_aggressive_stage_sec=max(30, _env_int("EXIT_POLICY_AGGRESSIVE_STAGE_SEC", 180)),
                exit_policy_taker_stage_sec=max(15, _env_int("EXIT_POLICY_TAKER_STAGE_SEC", 75)),
                maker_winner_sell_max_offset_ps=_env_decimal("MAKER_WINNER_SELL_MAX_OFFSET_PS", "0.05"),
                maker_signal_flip_cooldown_cycles=max(1, _env_int("MAKER_SIGNAL_FLIP_COOLDOWN_CYCLES", 2)),
            ),
            risk=RiskConfig(
                regime_guard_enabled=_env_bool_inverted("REGIME_GUARD_ENABLED", True),
                regime_guard_n_markets=regime_guard_n_markets,
                regime_guard_trigger_sum_pnl_usdc=_env_decimal("REGIME_GUARD_TRIGGER_SUM_PNL_USDC", "-3.5"),
                regime_guard_cooldown_sec=max(60, _env_int("REGIME_GUARD_COOLDOWN_SEC", 3600)),
                regime_guard_min_negative_markets=regime_guard_min_negative_markets,
                regime_guard_bootstrap_lookback_markets=regime_guard_bootstrap_lookback_markets,
            ),
            market_data=MarketDataConfig(
                external_spot_max_failures=_env_int("EXTERNAL_SPOT_MAX_FAILURES", 10),
                external_spot_history_max=external_spot_history_max,
                polymarket_chainlink_history_max=max(
                    60,
                    _env_int("POLYMARKET_CHAINLINK_HISTORY_MAX", external_spot_history_max),
                ),
                market_strike_anchor_max_lag_sec=max(10, _env_int("MARKET_STRIKE_ANCHOR_MAX_LAG_SEC", 180)),
                market_strike_anchor_near_sec=max(5, _env_int("MARKET_STRIKE_ANCHOR_NEAR_SEC", 30)),
                market_strike_rest_retry_sec=max(10, _env_int("MARKET_STRIKE_REST_RETRY_SEC", 60)),
                market_strike_gamma_validate_interval_sec=max(30, _env_int("MARKET_STRIKE_GAMMA_VALIDATE_INTERVAL_SEC", 180)),
                market_strike_gamma_warn_abs_usd=max(Decimal("1"), _env_decimal("MARKET_STRIKE_GAMMA_WARN_ABS_USD", "5")),
                market_strike_gamma_warn_interval_sec=max(30, _env_int("MARKET_STRIKE_GAMMA_WARN_INTERVAL_SEC", 120)),
                quote_healthcheck_interval_sec=_env_int("QUOTE_HEALTHCHECK_INTERVAL_SEC", 10),
                quote_stale_sec=_env_int("QUOTE_STALE_SEC", 30),
                quote_invalid_tick_reload_threshold=_env_int("QUOTE_INVALID_TICK_RELOAD_THRESHOLD", 80),
                quote_reload_cooldown_sec=_env_int("QUOTE_RELOAD_COOLDOWN_SEC", 60),
                stale_quote_synth_max_age_sec=_env_float("STALE_QUOTE_SYNTH_MAX_AGE_SEC", 10.0),
                fee_rate_fetch_interval_sec=max(5, _env_int("FEE_RATE_FETCH_INTERVAL_SEC", fee_rate_cache_ttl_sec)),
                fee_rate_cache_ttl_sec=fee_rate_cache_ttl_sec,
                clob_base_url=_env_str("POLYMARKET_CLOB_BASE_URL", "https://clob.polymarket.com"),
            ),
            operations=OperationsConfig(
                sellable_fallback_after_buy_sec=max(0, _env_int("SELLABLE_FALLBACK_AFTER_BUY_SEC", 10)),
                sellable_after_buy_buffer_shares=max(Decimal("0"), _env_decimal("SELLABLE_AFTER_BUY_BUFFER_SHARES", "0.05")),
                auto_redeem_enabled=_env_bool_inverted("AUTO_REDEEM_ENABLED", False),
                auto_redeem_apply=_env_bool_inverted("AUTO_REDEEM_APPLY", False),
                auto_redeem_interval_sec=max(120, _env_int("AUTO_REDEEM_INTERVAL_SEC", 900)),
                auto_redeem_on_rollover=_env_bool_inverted("AUTO_REDEEM_ON_ROLLOVER", True),
                auto_redeem_timeout_sec=max(30, _env_int("AUTO_REDEEM_TIMEOUT_SEC", 180)),
                auto_redeem_min_gap_sec=max(0, _env_int("AUTO_REDEEM_MIN_GAP_SEC", 300)),
                auto_redeem_slug_filter=_env_str("AUTO_REDEEM_SLUG_FILTER", "btc-updown-15m").strip(),
                market_settling_grace_sec=max(1, _env_int("MARKET_SETTLING_GRACE_SEC", 15)),
                market_next_poll_sec=max(5, _env_int("MARKET_NEXT_POLL_SEC", 15)),
                balance_check_interval_sec=max(10, _env_int("MAKER_BALANCE_CHECK_INTERVAL_SEC", 30)),
                conditional_balance_check_interval_sec=max(2, _env_int("CONDITIONAL_BALANCE_CHECK_INTERVAL_SEC", 8)),
                conditional_balance_safety_buffer_pct=max(
                    Decimal("0"),
                    min(Decimal("0.05"), _env_decimal("CONDITIONAL_BALANCE_SAFETY_BUFFER_PCT", "0.001")),
                ),
                sell_recovery_qty_buffer_shares=max(Decimal("0"), _env_decimal("SELL_RECOVERY_QTY_BUFFER_SHARES", "0.01")),
                sell_delay_after_buy_sec=max(0.0, _env_float("SELL_DELAY_AFTER_BUY_SEC", 3.0)),
                sell_balance_retry_pause_sec=max(1.0, _env_float("SELL_BALANCE_RETRY_PAUSE_SEC", 3.0)),
                trade_db_enabled=_env_bool_inverted("TRADE_DB_ENABLED", True),
                trade_db_path=_env_str("TRADE_DB_PATH", "./logs/trade_journal.db"),
            ),
        )
