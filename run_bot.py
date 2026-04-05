"""
Complete BTC 15-Min Trading Bot - FIXED VERSION
- Uses time-based filtering (proven to work from test)
- $1 per trade maximum
- Reloads instruments every 12 minutes
- Pre-loads price history on startup
- Full P&L tracking in simulation
"""

import asyncio
import json
import os
import sys
from collections import deque
from pathlib import Path
from datetime import datetime, timezone, timedelta
import math
from decimal import Decimal
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
import random
import re
import threading
import uuid
import subprocess

# Add project to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Now import Nautilus
from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.identifiers import InstrumentId, ClientOrderId
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.objects import Price, Quantity
from dotenv import load_dotenv
from loguru import logger

# Import our phases
from bot.inventory import InventoryLedger
from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.position_manager import PositionManager, PositionManagerConfig
from bot.enums import ActiveSide, MarketPhase
from bot.side_decision import SideDecisionMixin
from bot.spot_pricer import SpotPricerMixin
from bot.taker_exit import TakerExitMixin
from bot.fill_ledger import FillLedgerMixin
from bot.order_runtime import OrderRuntimeMixin
from bot.order_events import (
    handle_order_canceled,
    handle_order_cancel_rejected,
    handle_order_filled,
    handle_order_rejection_like_event,
)
from bot.order_submission import submit_maker_quote
from bot.market_runtime import (
    align_price_to_tick,
    find_btc_instrument,
    handle_generic_event,
    handle_quote_tick,
    handle_stop,
    maker_quote_sync,
    start_maker_worker,
    wait_for_btc_instrument,
)
from bot.pricing_runtime import PricingRuntimeMixin
from bot.recovery import StrategyRecoveryMixin
from bot.lifecycle_runtime import StrategyLifecycleMixin
from bot.lifecycle import (
    evaluate_market_phase,
)
from bot.ops import (
    adjust_inventory_after_merge,
    dedupe_price_history,
    extend_synthetic_history,
    handle_quote_watchdog_recovery,
    log_strategy_run_start,
    run_auto_redeem_script,
    start_background_thread,
    should_run_quote_watchdog,
    should_skip_auto_redeem_run,
)
from bot.market_data import (
    estimate_external_spot_sigma_annualized,
    extract_market_start_ts_from_slug,
    extract_price_to_beat_from_market_payload,
    extract_strike_from_question,
    fetch_binance_open_price_sync,
    fetch_coinbase_spot_sync,
    record_external_spot_observation,
    resolve_opening_strike_from_history,
)
from bot.models import ExitDecisionType, MarketSnapshot, PositionState, SignalDecision
from bot.quoting import (
    apply_quote_plan_guards,
)
from bot.quote_service import (
    apply_shadow_entry_veto,
    apply_time_based_profitable_sell_cap,
    attach_desired_entry_runtime_metadata,
    apply_confirmed_inventory_sell_guard,
    apply_reload_edge_guard,
    build_active_maker_order_state,
    build_desired_quote_entry,
    build_directional_snapshot,
    build_quote_instrument_context,
    compute_requote_target_version,
    evaluate_buy_entry_controls,
    extract_instrument_tick,
    log_no_quote_diagnostics,
    maybe_apply_continuation_entry,
    maybe_apply_trapped_inventory_recovery,
    preserve_profitable_existing_sell_order,
    preserve_recent_loss_sell_order,
    reconcile_unwanted_quotes,
    should_requote_existing_order,
)
from bot.shadow_signal import build_live_signal_compare_payload
from bot.settings import initialize_strategy_settings
from bot.market_cycle_state import MarketCycleState, bind_market_cycle_state
from bot.market_discovery import (
    resolve_best_btc_15m_market,
    resolve_btc_15m_market_slugs,
    resolve_primary_btc_15m_instrument_ids,
)
from bot.merge_ops import try_merge_yes_no_positions

load_dotenv()


def detect_runtime_git_revision(repo_root: Path) -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if head.returncode != 0:
            return "unknown"
        commit = head.stdout.strip() or "unknown"
        dirty = subprocess.run(
            ["git", "diff", "--quiet", "--exit-code"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if dirty.returncode == 1:
            return f"{commit}-dirty"
        return commit
    except Exception:
        return "unknown"
class IntegratedBTCStrategy(
    SideDecisionMixin,
    SpotPricerMixin,
    TakerExitMixin,
    FillLedgerMixin,
    OrderRuntimeMixin,
    PricingRuntimeMixin,
    StrategyRecoveryMixin,
    StrategyLifecycleMixin,
    Strategy,
):
    """
    Integrated BTC Strategy combining:
    - Nautilus trading framework
    - Our 7-phase system
    - Redis simulation control
    - Paper trading tracking
    - Auto-reload instruments every 12 minutes
    - Pre-loaded price history for immediate trading
    """
    
    def __init__(
        self,
        redis_client=None,
        enable_grafana=True,
        test_mode=False,
        selected_slug: Optional[str] = None,
        enable_terminal_dashboard: bool = False,
    ):
        super().__init__()
        
        # Nautilus
        self.instrument_id = None
        self.redis_client = redis_client
        self.selected_slug = selected_slug
        initialize_strategy_settings(
            self,
            enable_grafana=enable_grafana,
            test_mode=test_mode,
            enable_terminal_dashboard=enable_terminal_dashboard,
            project_root=project_root,
            detect_runtime_git_revision_fn=detect_runtime_git_revision,
        )

        if test_mode:
            logger.info("⚠️  TEST MODE ACTIVE - Trading every minute!")
        logger.info("Integrated BTC strategy initialized.")

    @property
    def inventory_delta_shares(self) -> Decimal:
        return self._inventory_delta_shares

    @inventory_delta_shares.setter
    def inventory_delta_shares(self, value: Decimal):
        old_val = getattr(self, "_inventory_delta_shares", Decimal("0"))
        if value != old_val:
            self.inventory_last_update_ts = time.time()
            self._inventory_delta_shares = value

    def _log_strategy_config_summary(self) -> None:
        logger.info(
            "Config summary: "
            f"mode={'maker' if self.maker_mode else 'signal'} "
            f"quote_sides={self.maker_quote_sides} "
            f"pricer={self.maker_fair_pricer_mode} "
            f"bi_side={'on' if self.bi_side_enabled else 'off'} "
            f"post_only={'on' if self.maker_use_post_only else 'off'} "
            f"auto_tune={'on' if self.auto_tune_enabled else 'off'} "
            f"auto_redeem={'on' if self.auto_redeem_enabled else 'off'} "
            f"trade_db={'on' if self.trade_db_enabled else 'off'}"
        )
        logger.info(
            "Risk/ops: "
            f"max_order_usdc={float(self.maker_max_order_usdc):.2f} "
            f"reduce_only_cutoff_min={self.maker_min_minutes_to_close:.1f} "
            f"watchdog_stale={self.quote_stale_sec}s "
            f"requote_min_age={self.maker_requote_min_age_sec:.1f}s"
        )
        if self.startup_verbose:
            logger.info(
                "Verbose config: "
                f"fee_interval={self.fee_rate_fetch_interval_sec}s "
                f"balance_guard={self.conditional_balance_check_interval_sec}s/"
                f"{float(self.conditional_balance_safety_buffer_pct)*100:.2f}% "
                f"taker_exit={'on' if self.taker_exit_enabled else 'off'} "
                f"taker_max_spread={float(self.taker_exit_max_spread_pct):.3f} "
                f"early_sell_only={self.maker_early_sell_only_sec}s "
                f"dir_edge_gate={'on' if self.maker_directional_edge_gate_enabled else 'off'} "
                f"dir_edge_min={float(self.maker_min_directional_edge_ps):.4f} "
                f"regime_guard={'on' if self.regime_guard_enabled else 'off'}"
            )

    def _max_inventory_avg_entry(self) -> Decimal:
        return InventoryLedger.max_avg_entry(self.live_inventory_cost)

    def _clear_profit_run_state(self, instrument_id: Any) -> None:
        inst_key = self._instrument_key(instrument_id)
        if not inst_key:
            return
        self.maker_profit_run_peak_bid_by_inst.pop(inst_key, None)
        self.maker_profit_run_peak_fair_by_inst.pop(inst_key, None)

    def _update_profit_run_peaks(
        self,
        instrument_id: Any,
        *,
        best_bid: Optional[Decimal],
        fair: Optional[Decimal],
    ) -> None:
        inst_key = self._instrument_key(instrument_id)
        if not inst_key:
            return
        state = self.live_inventory_cost.get(inst_key)
        if not state:
            self._clear_profit_run_state(instrument_id)
            return
        try:
            qty = Decimal(str(state.get("qty", "0")))
        except Exception:
            qty = Decimal("0")
        if qty <= 0:
            self._clear_profit_run_state(instrument_id)
            return
        if best_bid is not None and best_bid > 0:
            prev_peak_bid = self.maker_profit_run_peak_bid_by_inst.get(inst_key)
            if prev_peak_bid is None or best_bid > prev_peak_bid:
                self.maker_profit_run_peak_bid_by_inst[inst_key] = best_bid
        if fair is not None and fair > 0:
            prev_peak_fair = self.maker_profit_run_peak_fair_by_inst.get(inst_key)
            if prev_peak_fair is None or fair > prev_peak_fair:
                self.maker_profit_run_peak_fair_by_inst[inst_key] = fair

    def _should_hold_profitable_position(
        self,
        *,
        instrument_id: Any,
        best_bid: Decimal,
        fair: Optional[Decimal],
        avg_entry: Decimal,
        time_left_sec: Optional[float],
        thesis_weakened: bool,
        offside_confirmed: bool,
    ) -> tuple[bool, str]:
        inst_key = self._instrument_key(instrument_id)
        state = self.live_inventory_cost.get(inst_key)
        if not inst_key or not state:
            return False, ""
        try:
            qty = Decimal(str(state.get("qty", "0")))
        except Exception:
            qty = Decimal("0")
        peak_bid = self.maker_profit_run_peak_bid_by_inst.get(inst_key, best_bid)
        peak_fair = self.maker_profit_run_peak_fair_by_inst.get(inst_key, fair or best_bid)
        try:
            opened_ts = float(state.get("opened_ts", 0.0))
        except Exception:
            opened_ts = 0.0
        return self.position_manager.should_hold_profitable_position(
            inst_key=inst_key,
            qty=qty,
            best_bid=best_bid,
            fair=fair,
            avg_entry=avg_entry,
            active_side_locked=self.active_side_locked,
            active_side=self.active_side.value,
            instrument_matches_active_side=(self._instrument_for_side(self.active_side) == instrument_id),
            side_decision_score=self.side_decision_score,
            exit_stage_value=self.exit_policy.stage(time_left_sec).value,
            thesis_weakened=thesis_weakened,
            offside_confirmed=offside_confirmed,
            opened_ts=opened_ts,
            peak_bid=peak_bid,
            peak_fair=peak_fair,
        )



    def _is_emergency_exit_window(self, time_left_sec: Optional[float]) -> bool:
        if time_left_sec is None:
            return False
        if self.maker_sell_cost_protect_emergency_last_sec <= 0:
            return False
        return time_left_sec <= float(self.maker_sell_cost_protect_emergency_last_sec)

    def _assess_thesis_weakened(
        self,
        *,
        inst_id: Any,
        now_ts: float,
        side_score: Decimal,
    ) -> bool:
        inst_key = self._instrument_key(inst_id)
        if not inst_key:
            return False

        raw_thesis_weakened = False
        score = float(side_score)
        opposite_score_abs = float(self.side_thesis_weak_opposite_score_abs_new)
        requires_opposite_side = bool(self.side_thesis_weak_requires_opposite_side_new)
        if self.active_side == ActiveSide.UP and score <= -opposite_score_abs:
            raw_thesis_weakened = True
        elif self.active_side == ActiveSide.DOWN and score >= opposite_score_abs:
            raw_thesis_weakened = True
        elif (
            not requires_opposite_side
            and self.active_side != ActiveSide.NONE
            and abs(score) < float(self.side_thesis_weak_score_abs)
        ):
            raw_thesis_weakened = True

        recent_buy_ts = float(self.recent_buy_fill_ts_by_inst.get(inst_key, 0.0))
        if (
            raw_thesis_weakened
            and recent_buy_ts > 0
            and self.side_thesis_weak_min_hold_sec_new > 0
            and (now_ts - recent_buy_ts) < float(self.side_thesis_weak_min_hold_sec_new)
        ):
            self.side_thesis_weak_hits_by_inst[inst_key] = 0
            return False

        hits = int(self.side_thesis_weak_hits_by_inst.get(inst_key, 0))
        hits = hits + 1 if raw_thesis_weakened else 0
        self.side_thesis_weak_hits_by_inst[inst_key] = hits
        return raw_thesis_weakened and hits >= int(self.side_thesis_weak_confirmations_new)

    def _normalize_active_side(self, side: Any) -> ActiveSide:
        txt = str(side or "").strip().upper()
        if txt == ActiveSide.UP.value:
            return ActiveSide.UP
        if txt == ActiveSide.DOWN.value:
            return ActiveSide.DOWN
        return ActiveSide.NONE

    def _primary_instrument_for_market(self) -> Optional[InstrumentId]:
        return self.current_up_instrument_id or self.current_down_instrument_id or self.instrument_id

    def _instrument_for_side(self, side: ActiveSide) -> Optional[InstrumentId]:
        if side == ActiveSide.UP:
            return self.current_up_instrument_id or self._primary_instrument_for_market()
        if side == ActiveSide.DOWN:
            return self.current_down_instrument_id
        return None

    def _side_for_instrument_id(self, instrument_id: Optional[Any]) -> ActiveSide:
        inst = self._normalize_instrument_id(instrument_id)
        if inst is None:
            return ActiveSide.NONE
        if self.current_up_instrument_id is not None and inst == self.current_up_instrument_id:
            return ActiveSide.UP
        if self.current_down_instrument_id is not None and inst == self.current_down_instrument_id:
            return ActiveSide.DOWN
        return ActiveSide.NONE

    def _sync_active_instrument(self) -> None:
        target = self._instrument_for_side(self.active_side)
        if target is None:
            target = self._primary_instrument_for_market()
        self.instrument_id = target
        self.current_token_id = self._extract_token_id_from_instrument(str(target)) if target is not None else None

    def _capture_market_open_spot(self) -> Optional[Decimal]:
        if self.latest_external_spot is not None and self.latest_external_spot > 0:
            src_age = time.time() - float(self.latest_external_spot_source_ts or 0.0)
            if src_age < 10.0:
                return self.latest_external_spot
        spot = self.latest_external_spot or self.last_external_spot
        if spot is not None and spot > 0:
            return spot
        if self._binance_ws_price is not None and self._binance_ws_price > 0:
            return self._binance_ws_price
        if self._polymarket_chainlink_price is not None and self._polymarket_chainlink_price > 0:
            return self._polymarket_chainlink_price
        if self.external_spot_history:
            _, hist_px = self.external_spot_history[-1]
            if hist_px > 0:
                return hist_px
        return None

    def _emit_live_signal_compare_snapshot(self, now_ts: float) -> None:
        if not self.trade_db or not getattr(self, "shadow_signal_enabled", False):
            return
        payload = self._build_live_signal_compare_payload(now_ts)
        if payload is None:
            return
        self._db_strategy_event("LIVE_SIGNAL_COMPARE", payload)

        main_sig = json.dumps(
            {
                "slug": payload["slug"],
                "main_candidate_side": payload.get("main_candidate_side"),
                "main_score": round(float(payload.get("main_score") or 0.0), 4),
                "main_locked": bool(payload.get("main_side_locked")),
            },
            sort_keys=True,
        )
        if main_sig != getattr(self, "_last_main_live_candidate_signature", None):
            self._last_main_live_candidate_signature = main_sig
            self._db_strategy_event("MAIN_SIGNAL_CANDIDATE_LIVE", payload)

        shadow_sig = json.dumps(
            {
                "slug": payload["slug"],
                "shadow_candidate_side": payload.get("shadow_candidate_side"),
                "shadow_candidate_edge": round(float(payload.get("shadow_candidate_edge") or 0.0), 4),
                "shadow_score": round(float(payload.get("shadow_score") or 0.0), 4),
            },
            sort_keys=True,
        )
        if shadow_sig != getattr(self, "_last_shadow_live_candidate_signature", None):
            self._last_shadow_live_candidate_signature = shadow_sig
            self._db_strategy_event("SHADOW_SIGNAL_CANDIDATE_LIVE", payload)

    def _build_live_signal_compare_payload(self, now_ts: float) -> Optional[Dict[str, Any]]:
        if not getattr(self, "shadow_signal_enabled", False):
            return None
        slug = str(self.current_market_slug or "")
        if not slug:
            return None
        spot = self._capture_market_open_spot()
        if spot is None or spot <= 0:
            return None
        strike = self.market_strike_cache_by_slug.get(slug)
        up_quote = (
            self._get_quote_for_instrument(self.current_up_instrument_id)
            if self.current_up_instrument_id is not None
            else None
        )
        down_quote = (
            self._get_quote_for_instrument(self.current_down_instrument_id)
            if self.current_down_instrument_id is not None
            else None
        )
        sigma = self._estimate_external_spot_sigma_annualized() or self.maker_digital_sigma_default
        sigma = min(self.maker_digital_sigma_ceiling, max(self.maker_digital_sigma_floor, sigma))
        end_ts = getattr(self, "current_market_end_timestamp", None)
        time_left_sec = max(0.0, float(end_ts - now_ts)) if end_ts is not None else 0.0
        return build_live_signal_compare_payload(
            slug=slug,
            spot=spot,
            strike=strike,
            sigma=sigma,
            time_left_sec=time_left_sec,
            history=self.external_spot_history,
            now_ts=now_ts,
            active_side_value=self.active_side.value,
            active_side_locked=bool(self.active_side_locked),
            side_score=self.side_decision_score,
            side_reason=self.side_decision_reason,
            ask_up=up_quote[1] if up_quote is not None else None,
            ask_down=down_quote[1] if down_quote is not None else None,
            bid_up=up_quote[0] if up_quote is not None else None,
            bid_down=down_quote[0] if down_quote is not None else None,
            cfg=self.shadow_signal_config,
        )

    # Side decision methods extracted to bot/side_decision.py (SideDecisionMixin)

    def _db_strategy_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        if not self.trade_db:
            return
        payload_out: Dict[str, Any] = dict(payload or {})
        if self.current_market_slug and "slug" not in payload_out:
            payload_out["slug"] = self.current_market_slug
        if self.current_market_slug and "market_slug" not in payload_out:
            payload_out["market_slug"] = self.current_market_slug
        if self.instrument_id and "instrument_id" not in payload_out:
            payload_out["instrument_id"] = str(self.instrument_id)
        self.trade_db.log_strategy_event(
            run_id=self.run_id,
            event_type=event_type,
            payload=payload_out,
        )

    def _db_order_event(
        self,
        event_type: str,
        client_order_id: Optional[str] = None,
        venue_order_id: Optional[str] = None,
        side: Optional[str] = None,
        price: Optional[float] = None,
        qty: Optional[float] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        commission_usdc: Optional[float] = None,
        expected_net_usdc: Optional[float] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.trade_db:
            return
        payload_out: Dict[str, Any] = dict(payload or {})
        if self.current_market_slug and "slug" not in payload_out:
            payload_out["slug"] = self.current_market_slug
        if self.current_market_slug and "market_slug" not in payload_out:
            payload_out["market_slug"] = self.current_market_slug
        if self.instrument_id and "instrument_id" not in payload_out:
            payload_out["instrument_id"] = str(self.instrument_id)

        side_out = side
        if side_out:
            side_norm = self._normalize_side_text(side_out)
            if side_norm:
                side_out = side_norm.upper()
        self.trade_db.log_order_event(
            run_id=self.run_id,
            event_type=event_type,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            side=side_out,
            price=price,
            qty=qty,
            status=status,
            reason=reason,
            instrument_id=str(self.instrument_id) if self.instrument_id else None,
            token_id=self.current_token_id,
            fee_rate_bps=self.last_observed_fee_rate_bps,
            expected_net_usdc=expected_net_usdc,
            commission_usdc=commission_usdc,
            payload=payload_out,
        )

    def _increment_order_metric(self, status: str) -> None:
        """
        Increment order counters on Grafana exporter when available.
        """
        if self.grafana_exporter and hasattr(self.grafana_exporter, "increment_order_counter"):
            try:
                self.grafana_exporter.increment_order_counter(status)
            except Exception as e:
                logger.debug(f"Failed to increment order metric [{status}]: {e}")

    def _init_live_prom_metrics(self) -> None:
        """Initialize Prometheus gauges/counters for live trading metrics."""
        try:
            from prometheus_client import Gauge, Counter
            self._prom_live_pnl = Gauge('trading_live_realized_pnl', 'Cumulative realized PnL from live trades (USDC)')
            self._prom_live_trades = Counter('trading_live_trades_total', 'Total live trades (position round-trips)')
            self._prom_live_wins = Counter('trading_live_winning_trades', 'Live winning trades')
            self._prom_live_losses = Counter('trading_live_losing_trades', 'Live losing trades')
            self._prom_live_win_rate = Gauge('trading_live_win_rate', 'Live win rate percentage')
            self._prom_live_open_pos = Gauge('trading_live_open_positions', 'Number of open positions')
            self._prom_live_inventory = Gauge('trading_live_inventory_shares', 'Current inventory in shares')
            self._live_cumulative_pnl = 0.0
            self._live_total_trades = 0
            self._live_total_wins = 0
            self._prom_live_metrics_ok = True
            logger.info("✓ Live Prometheus trading metrics initialized")
        except Exception as e:
            logger.debug(f"Failed to init live prom metrics: {e}")
            self._prom_live_metrics_ok = False

    def _push_position_closed_to_prometheus(self, realized_pnl: float, duration_ns: int) -> None:
        """Push a completed round-trip trade to Prometheus metrics."""
        if not getattr(self, '_prom_live_metrics_ok', False):
            return
        try:
            self._live_cumulative_pnl += realized_pnl
            self._live_total_trades += 1
            won = realized_pnl > 0
            if won:
                self._live_total_wins += 1
                self._prom_live_wins.inc()
            else:
                self._prom_live_losses.inc()
            self._prom_live_trades.inc()
            self._prom_live_pnl.set(self._live_cumulative_pnl)
            win_rate = (self._live_total_wins / self._live_total_trades * 100) if self._live_total_trades > 0 else 0
            self._prom_live_win_rate.set(win_rate)

            # Also push to grafana_exporter if available
            if self.grafana_exporter:
                try:
                    self.grafana_exporter.increment_trade_counter(won=won)
                    dur_sec = duration_ns / 1e9 if duration_ns else 0
                    if dur_sec > 0:
                        self.grafana_exporter.record_trade_duration(dur_sec)
                    # Update PnL gauge exposed by exporter
                    self.grafana_exporter.total_pnl.set(self._live_cumulative_pnl)
                    self.grafana_exporter.win_rate.set(win_rate)
                except Exception:
                    pass

            logger.info(
                f"📊 Prometheus: trade #{self._live_total_trades} pnl={realized_pnl:+.4f} "
                f"cum_pnl={self._live_cumulative_pnl:+.4f} win_rate={win_rate:.0f}%"
            )
            if self.terminal_dashboard:
                self.terminal_dashboard.record_position_closed(
                    realized_pnl=realized_pnl,
                    total_trades=self._live_total_trades,
                    win_rate=win_rate,
                )
        except Exception as e:
            logger.debug(f"Failed to push position metrics: {e}")

    def _update_inventory_metric(self) -> None:
        """Update the inventory gauge in Prometheus."""
        if not getattr(self, '_prom_live_metrics_ok', False):
            return
        try:
            self._prom_live_inventory.set(float(self.inventory_delta_shares))
        except Exception:
            pass

    def _update_terminal_dashboard_snapshot(self) -> None:
        if not self.terminal_dashboard:
            return
        try:
            self.terminal_dashboard.update(
                phase=self.market_phase.value,
                slug=self.current_market_slug or self.selected_slug or "-",
                active_side=self.active_side.value,
                inventory_shares=float(self.inventory_delta_shares),
                wallet_balance_usdc=(
                    float(self._cached_usdc_balance) if self._cached_usdc_balance is not None else None
                ),
                active_orders=len(self.active_maker_orders),
            )
        except Exception as e:
            logger.debug(f"Failed to update terminal dashboard snapshot: {e}")

    def _start_terminal_dashboard_sync(self) -> None:
        if not self.terminal_dashboard:
            return
        try:
            self.terminal_dashboard.start()
            while not self._terminal_dashboard_stop_event.wait(self.terminal_dashboard_refresh_sec):
                self._refresh_balance_cache()
                self._update_terminal_dashboard_snapshot()
        except Exception as e:
            logger.error(f"Failed to start terminal dashboard: {e}")
    
    def _is_dry_run_mode(self) -> bool:
        """
        Test mode remains a safety rail, but simulation execution paths are removed.
        """
        return bool(self.test_mode)

    # ------------------------------------------------------------------
    # Binance WebSocket for real-time BTC price
    # ------------------------------------------------------------------

    # Spot pricer methods extracted to bot/spot_pricer.py (SpotPricerMixin)

    @staticmethod
    def _extract_market_start_ts_from_slug(slug: str) -> Optional[int]:
        return extract_market_start_ts_from_slug(slug)

    @staticmethod
    def _extract_price_to_beat_from_market_payload(market: Dict[str, Any]) -> Optional[Decimal]:
        return extract_price_to_beat_from_market_payload(market)

    @staticmethod
    def _resolve_btc_15m_market_slugs() -> List[str]:
        return resolve_btc_15m_market_slugs()

    @staticmethod
    def _instrument_key(instrument_id: Any) -> str:
        return str(instrument_id) if instrument_id is not None else ""

    @staticmethod
    def _normalize_side_text(side_val: Any) -> str:
        txt = str(side_val or "").strip().lower()
        if txt in {"buy", "bid"}:
            return "buy"
        if txt in {"sell", "ask"}:
            return "sell"
        if "buy" in txt:
            return "buy"
        if "sell" in txt:
            return "sell"
        if txt == "1":
            return "buy"
        if txt == "2":
            return "sell"
        return ""

    @staticmethod
    def _reason_family(reason: str) -> str:
        """
        Collapse verbose diagnostic strings into stable reason families.
        This avoids state-machine jitter when numeric diagnostics change every tick.
        """
        r = str(reason or "")
        if r.startswith("econ_gate"):
            return "econ_gate"
        if r.startswith("reduce_only_tail_guard"):
            return "reduce_only_tail_guard"
        if r.startswith("reduce_only"):
            return "reduce_only"
        if r.startswith("side_disabled:momentum_buy_block") or r.startswith("side_disabled:momentum_sell_block"):
            return "trend_protection"
        if r.startswith("balance_forced_sell_only"):
            return "balance_forced_sell_only"
        if r.startswith("regime_guard_sell_only"):
            return "balance_forced_sell_only"
        if r.startswith("sell_pause"):
            return "sell_pause"
        if r.startswith("sellable_below_min"):
            return "sellable_below_min"
        if r.startswith("side_disabled"):
            return "side_disabled"
        if r.startswith("risk:no_desired_quote"):
            return "no_desired_quote"
        return "risk"

    @staticmethod
    def _latest_observation_supports_locked_side(active_side: Any, side_score: Decimal) -> bool:
        if active_side == ActiveSide.UP:
            return side_score > 0
        if active_side == ActiveSide.DOWN:
            return side_score < 0
        return False

    def _should_skip_buy_submit_for_quote_drift(
        self,
        *,
        instrument_id: Any,
        quote_now: tuple[Decimal, Decimal] | None,
        directional_snapshot: Optional[Dict[str, Any]],
        instrument: Any,
    ) -> bool:
        if quote_now is None or not directional_snapshot:
            return False
        try:
            planned_bid = Decimal(str(directional_snapshot.get("planned_best_bid")))
            planned_ask = Decimal(str(directional_snapshot.get("planned_best_ask")))
            planned_quote_ts = float(directional_snapshot.get("planned_quote_ts") or 0.0)
        except Exception:
            return False
        if planned_bid <= 0 or planned_ask <= 0 or planned_quote_ts <= 0:
            return False
        quote_age_sec = max(0.0, time.time() - planned_quote_ts)
        if quote_age_sec > 1.5:
            logger.warning(
                "Skip BUY quote: planned quote snapshot is stale "
                f"(inst={self._instrument_key(instrument_id)}, age={quote_age_sec:.2f}s)"
            )
            return True
        current_bid, current_ask = quote_now
        tick = extract_instrument_tick(instrument, default_tick="0.01")
        max_drift = tick * 2
        if (
            abs(current_bid - planned_bid) > max_drift
            or abs(current_ask - planned_ask) > max_drift
        ):
            logger.warning(
                "Skip BUY quote: top-of-book drifted before submit "
                f"(inst={self._instrument_key(instrument_id)} "
                f"planned={float(planned_bid):.4f}/{float(planned_ask):.4f} "
                f"current={float(current_bid):.4f}/{float(current_ask):.4f} "
                f"max_drift={float(max_drift):.4f})"
            )
            return True
        return False

    @staticmethod
    def _extract_token_id_from_instrument(instrument_id: str) -> Optional[str]:
        """
        Extract token_id from Nautilus Polymarket instrument ID:
        {condition_id}-{token_id}.POLYMARKET
        """
        m = re.search(r"-([0-9]+)\.POLYMARKET$", instrument_id)
        if not m:
            return None
        return m.group(1)

    @staticmethod
    def _extract_venue_balance_shares_from_reject(reason: str) -> Optional[Decimal]:
        txt = str(reason or "")
        m = re.search(r"balance:\s*([0-9]+)", txt)
        if not m:
            return None
        try:
            return Decimal(m.group(1)) / Decimal("1000000")
        except Exception:
            return None

    @staticmethod
    def _extract_market_slug_from_instrument(instrument: Any) -> str:
        info = getattr(instrument, "info", None) or {}
        if isinstance(info, dict):
            for key in ("market_slug", "slug", "event_slug", "marketSlug", "eventSlug"):
                s = str(info.get(key, "") or "")
                if s:
                    return s
        return ""

    def _extract_outcome_from_instrument(self, instrument: Any) -> str:
        """
        Best-effort outcome mapping (up/down) from instrument metadata.
        """
        try:
            inst_id = str(getattr(instrument, "id", "") or "")
            token_id = self._extract_token_id_from_instrument(inst_id)
            info = getattr(instrument, "info", None) or {}
            if not isinstance(info, dict):
                return ""
            tokens = info.get("tokens")
            if isinstance(tokens, list):
                for t in tokens:
                    if not isinstance(t, dict):
                        continue
                    t_id = str(t.get("token_id", "") or "")
                    if token_id and t_id == token_id:
                        return str(t.get("outcome", "") or "").strip().lower()
        except Exception:
            return ""
        return ""

    @staticmethod
    def _normalize_instrument_id(instrument_id: Any) -> Optional[InstrumentId]:
        if instrument_id is None:
            return None
        if isinstance(instrument_id, InstrumentId):
            return instrument_id
        try:
            return InstrumentId.from_str(str(instrument_id))
        except Exception:
            return None

    def _append_real_mid_price(self, instrument_id: Any, mid_price: Decimal) -> None:
        inst_key = str(instrument_id) if instrument_id is not None else ""
        self.real_price_history.append(mid_price)
        if len(self.real_price_history) > self.max_real_history:
            self.real_price_history.pop(0)
        if not inst_key:
            return
        history = self.real_price_history_by_inst.setdefault(inst_key, [])
        history.append(mid_price)
        if len(history) > self.max_real_history:
            history.pop(0)
        # Feed UP token mid into SignalEngine for market consensus signal
        up_inst = getattr(self, 'current_up_instrument_id', None)
        if (
            up_inst is not None
            and self._normalize_instrument_id(instrument_id) == up_inst
            and hasattr(self, '_signal_engine')
        ):
            import time as _time
            self._signal_engine.update_market_mid(mid_price, _time.time())

    def _reset_maker_state_for_new_market(
        self,
        prev_instrument_id: Optional[str],
        new_instrument_id: Optional[str],
        *,
        previous_slug: str = "",
        current_slug: str = "",
    ) -> None:
        """
        Per-market maker state reset.
        Inventory and kill-switch are strategy-local controls and should not carry across 15m markets.
        When the slug is unchanged (same-market rollover / side flip), preserve inventory
        tracking so SELL quotes are not incorrectly blocked.
        """
        if prev_instrument_id == new_instrument_id:
            return
        same_slug = bool(previous_slug and current_slug and previous_slug == current_slug)
        self._cancel_active_maker_orders()
        if same_slug:
            logger.info(
                f"Same-slug rollover: preserving inventory_delta_shares="
                f"{float(self.inventory_delta_shares):.6f} and live_inventory_cost "
                f"(slug={current_slug}, inst {prev_instrument_id} -> {new_instrument_id})"
            )
        else:
            self.inventory_delta_shares = Decimal("0")
            self.live_inventory_cost.clear()
            self._startup_rehydrated_inventory_force_sell_only = False
            self.position_manager.clear_all()
        self.market_cycle_realized_net_usdc = Decimal("0")
        bind_market_cycle_state(self, MarketCycleState())
        if self.maker_kill_switch and self.maker_kill_switch_reset_on_rollover:
            self.maker_kill_switch = False
            logger.warning("Maker kill switch auto-reset on market rollover.")
        self.last_quote_update_ts = 0.0
        logger.info(f"Reset maker per-market state: {prev_instrument_id} -> {new_instrument_id} (same_slug={same_slug})")

    def _project_inventory_after_fill(self, side: str, qty: Decimal, instrument_id: Optional[Any] = None) -> Decimal:
        inst_id = instrument_id if instrument_id is not None else self.instrument_id
        projected = self.inventory_delta_shares
        if side.lower() == "sell":
            projected = self._get_confirmed_inventory_qty_for_instrument(inst_id)
        
        # Calculate in-flight volume for the SAME side
        target_side_str = "BUY" if side.lower() == "buy" else "SELL"
        in_flight_qty = Decimal("0")
        inst_target = str(inst_id) if inst_id else None
        
        for _, state in self.active_maker_orders.items():
            o_side = str(state.get("side", "")).upper()
            if o_side != target_side_str:
                continue
                
            o_inst = str(state.get("instrument_id", ""))
            if inst_target and o_inst != inst_target:
                continue
                
            # If the order is in our active dict, assume its REMAINING quantity is occupying inventory capacity
            # regardless of whether it lacks a VenueOrderId yet or is pending cancel.
            o_qty = Decimal(str(state.get("quantity", "0")))
            o_filled = Decimal(str(state.get("filled_qty", "0")))
            in_flight_qty += max(Decimal("0"), o_qty - o_filled)
            
        if side.lower() == "buy":
            return projected + in_flight_qty + qty
        return projected - in_flight_qty - qty

    def _maybe_auto_tune(self, now_ts: float) -> None:
        if not self.auto_tune_enabled:
            return
        if now_ts - self.last_auto_tune_ts < self.auto_tune_interval_sec:
            return
        metrics = self.rebate_reporter.get_current_metrics()
        suggestion = self.parameter_tuner.suggest(
            current_half_spread=self.maker_half_spread,
            current_min_expected_net=self.maker_min_expected_net_usdc,
            metrics=metrics,
        )
        new_half = suggestion["maker_half_spread"]
        new_min = suggestion["maker_min_expected_net_usdc"]
        if new_half != self.maker_half_spread or new_min != self.maker_min_expected_net_usdc:
            logger.info(
                "Auto-tune update: "
                f"half_spread {float(self.maker_half_spread):.6f}->{float(new_half):.6f}, "
                f"min_net {float(self.maker_min_expected_net_usdc):.6f}->{float(new_min):.6f}"
            )
        self.maker_half_spread = new_half
        self.maker_min_expected_net_usdc = new_min
        # Keep decoupled maker engine config in sync with auto-tuned values.
        self.maker_engine.config.maker_half_spread = new_half
        self.maker_engine.config.maker_min_expected_net_usdc = new_min
        self.last_auto_tune_ts = now_ts

    def _maker_quote_instruments(self) -> List[InstrumentId]:
        instruments: List[InstrumentId] = []
        seen: Set[str] = set()

        def _append(inst: Optional[InstrumentId]) -> None:
            if inst is None:
                return
            key = self._instrument_key(inst)
            if not key or key in seen:
                return
            instruments.append(inst)
            seen.add(key)

        if self.bi_side_enabled:
            active_inst = self._instrument_for_side(self.active_side)
            if self.active_side != ActiveSide.NONE and active_inst is not None:
                _append(active_inst)
        elif self.instrument_id is not None:
            _append(self.instrument_id)

        # Always include legs with confirmed inventory so they remain in the
        # normal maker requote loop even after active-side flips.
        for inst_key, state in list(self.live_inventory_cost.items()):
            try:
                qty = Decimal(str(state.get("qty", "0")))
            except Exception:
                qty = Decimal("0")
            if qty <= 0:
                continue
            inst = self._normalize_instrument_id(inst_key)
            if inst is not None:
                _append(inst)

        # Preserve instruments that recently failed SELL due to venue balance lag.
        for inst_key in list(self._sell_recovery_required_by_inst.keys()):
            inst = self._normalize_instrument_id(inst_key)
            if inst is not None:
                _append(inst)

        return instruments

    async def _prepare_quote_cycle(self) -> Optional[Dict[str, Any]]:
        if self.maker_kill_switch:
            return None

        if time.time() < self.quote_pause_until_ts:
            return None

        phase = self._update_market_phase()
        if phase in (MarketPhase.WAITING, MarketPhase.SETTLING):
            self._cancel_active_maker_orders()
            return None
        await self._maybe_finalize_side_decision(time.time(), phase)
        if self.bi_side_enabled and self.active_side == ActiveSide.NONE:
            if self.inventory_delta_shares <= 0:
                self._cancel_active_maker_orders()
                return None

        _balance_forced_sell_only = False
        _regime_guard_active = False
        if not self._is_dry_run_mode():
            balance = self._refresh_balance_cache()
            if balance is not None:
                required = self.maker_quote_size_usdc * Decimal("1.1")  # 10% buffer
                if balance < required:
                    gross_sellable = self._get_total_sellable_qty(self._maker_quote_instruments())
                    if gross_sellable > 0:
                        # Have inventory — switch to SELL-only to free up capital
                        _balance_forced_sell_only = True
                        if time.time() - getattr(self, '_last_balance_warn_ts', 0) >= 60:
                            logger.warning(
                                f"Balance pre-check: low USDC "
                                f"(available={float(balance):.2f}, needed≈{float(required):.2f}). "
                                f"Switching to SELL-only until balance recovers. "
                                f"net_inventory={float(self.inventory_delta_shares):.4f} "
                                f"gross_sellable={float(gross_sellable):.4f}"
                            )
                            self._last_balance_warn_ts = time.time()
                    else:
                        # No inventory and no balance — skip quoting entirely
                        if time.time() - getattr(self, '_last_balance_warn_ts', 0) >= 60:
                            logger.warning(
                                f"Balance pre-check: insufficient USDC and no inventory "
                                f"(available={float(balance):.2f}, needed≈{float(required):.2f}). "
                                f"Skipping maker quotes."
                            )
                            self._last_balance_warn_ts = time.time()
                        return None
        if self.regime_guard_enabled:
            now_guard_ts = time.time()
            if self.regime_guard_conservative_until_ts > 0 and now_guard_ts >= self.regime_guard_conservative_until_ts:
                self.regime_guard_conservative_until_ts = 0.0
                self._db_strategy_event("REGIME_GUARD_RECOVERED", {"ts": now_guard_ts})
            elif now_guard_ts < self.regime_guard_conservative_until_ts:
                _regime_guard_active = True
        if self.inventory_delta_shares <= 0 and self._startup_rehydrated_inventory_force_sell_only:
            self._startup_rehydrated_inventory_force_sell_only = False
        _forced_sell_only = _balance_forced_sell_only or self._startup_rehydrated_inventory_force_sell_only

        # Check whether current inventory should be force-closed via taker orders.
        await self._maybe_taker_exit_positions(time.time(), is_simulation=self._is_dry_run_mode())
        # Maker-style urgent exit: thesis weakened → place SELL at best_bid (zero taker fee)
        await self._maybe_maker_urgent_exit(time.time())

        now_ts = time.time()
        force_quote_refresh_once = bool(getattr(self, "_force_quote_refresh_once", False))
        if not force_quote_refresh_once and now_ts - self.last_quote_update_ts < self.quote_refresh_sec:
            return None
        if force_quote_refresh_once:
            logger.info(
                "Fast requote triggered after locked side change: "
                f"reason={getattr(self, '_force_quote_refresh_reason', 'locked_side_change')}"
            )
            self._force_quote_refresh_once = False
            self._force_quote_refresh_reason = ""
        self.last_quote_update_ts = now_ts
        self._maybe_auto_tune(now_ts)
        self._cleanup_stale_pending_cancels(now_ts)

        # Cancel stale quotes by TTL before computing new target quotes.
        for order_key, state in list(self.active_maker_orders.items()):
            created_ts = float(state.get("created_ts", 0.0))
            # Urgent exit orders use their own (shorter) TTL
            if state.get("is_urgent_exit"):
                ttl = float(state.get("urgent_exit_ttl", self.maker_order_ttl_sec))
            else:
                ttl = self.maker_order_ttl_sec
                if str(state.get("side", "") or "") == "sell" and state.get("loss_sell_reason"):
                    ttl = max(ttl, float(getattr(self, "maker_loss_sell_reprice_min_interval_sec", ttl)))
            if created_ts <= 0 or (now_ts - created_ts) >= ttl:
                side = str(state.get("side", "") or "")
                is_urgent = " (urgent_exit)" if state.get("is_urgent_exit") else ""
                logger.info(f"Maker order [{side}]{is_urgent} exceeded TTL={ttl}s, cancel and requote.")
                self._cancel_maker_order_side(order_key, reason="ttl")

        if abs(self.inventory_delta_shares) > self.maker_max_inventory_shares:
            self._activate_maker_kill_switch(
                f"Inventory {self.inventory_delta_shares} exceeds max {self.maker_max_inventory_shares}"
            )
            return None

        recent_vol = self._compute_recent_volatility()
        if recent_vol is None:
            logger.debug(
                f"Volatility gate warmup: real_quotes={len(self.real_price_history)}/{self.maker_vol_warmup_quotes}"
            )
        else:
            # We no longer pause the engine on high volatility.
            # Volatility Regimes inside MakerEngine handle STRESSED and EXTREME states.
            pass

        target_instruments = self._maker_quote_instruments()
        if not target_instruments:
            if self.bi_side_enabled:
                self._cancel_active_maker_orders()
            return None

        time_passed = max(0.0, now_ts - self.requote_bucket_last_refill)
        self.requote_bucket_tokens = min(
            self.maker_requote_max_per_sec,
            self.requote_bucket_tokens + (time_passed * self.maker_requote_max_per_sec)
        )
        self.requote_bucket_last_refill = now_ts

        target_inst_set = {str(inst) for inst in target_instruments}
        end_ts = getattr(self, "current_market_end_timestamp", None)
        time_left_sec_global = (end_ts - now_ts) if end_ts is not None else None

        return {
            "phase": phase,
            "forced_sell_only": _forced_sell_only,
            "regime_guard_active": _regime_guard_active,
            "now_ts": now_ts,
            "recent_vol": recent_vol,
            "target_instruments": target_instruments,
            "target_inst_set": target_inst_set,
            "end_ts": end_ts,
            "time_left_sec_global": time_left_sec_global,
        }

    async def _evaluate_quote_targets(
        self,
        *,
        phase: MarketPhase,
        forced_sell_only: bool,
        regime_guard_active: bool,
        now_ts: float,
        recent_vol: Optional[Decimal],
        target_instruments: List[Any],
        end_ts: Optional[float],
        time_left_sec_global: Optional[float],
    ) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        desired_quotes: Dict[str, Dict[str, Any]] = {}
        diag_context_by_inst: Dict[str, Dict[str, Any]] = {}

        for inst_id in target_instruments:
            quote_ctx = await build_quote_instrument_context(
                inst_id=inst_id,
                normalize_instrument_id_fn=self._normalize_instrument_id,
                instrument_key_fn=self._instrument_key,
                get_quote_for_instrument_fn=self._get_quote_for_instrument,
                compute_fair_probability_fn=self._compute_fair_probability,
                cache_instrument_fn=self.cache.instrument,
                extract_token_id_fn=self._extract_token_id_from_instrument,
                get_dynamic_fee_rate_fn=self._get_dynamic_fee_rate,
                get_orderbook_levels_fn=self._get_orderbook_levels_for_token,
                latest_quote_depth_by_inst=self.latest_quote_depth_by_inst,
                maker_econ_fee_rate_decimal=self.maker_econ_fee_rate_decimal,
            )
            inst_key = quote_ctx.inst_key
            diag_context_by_inst[inst_key] = quote_ctx.diag_context
            if quote_ctx.quote is None or quote_ctx.fair is None:
                continue
            inst_bid, inst_ask = quote_ctx.quote
            fair = quote_ctx.fair
            self.rebate_reporter.record_api_health(self.fee_rate_client.get_health_snapshot())
            if quote_ctx.dynamic_fee_rate is not None:
                pass

            side_plan = self.maker_engine.generate_quote_plan(
                inst_bid=inst_bid,
                inst_ask=inst_ask,
                fair_price=fair,
                fee_rate=quote_ctx.fee_rate_val,
                inventory_delta_shares=self.inventory_delta_shares,
                inventory_last_update_ts=self.inventory_last_update_ts,
                current_time_ts=now_ts,
                tick_size=quote_ctx.tick,
                recent_vol=recent_vol,
                balance_forced_sell_only=forced_sell_only,
                bid_depth=quote_ctx.bid_depth,
                ask_depth=quote_ctx.ask_depth,
                bid_levels=quote_ctx.bid_levels,
                ask_levels=quote_ctx.ask_levels,
            )
            if not side_plan:
                diag_context_by_inst[inst_key]["reason"] = "invalid_quote_plan"
                continue

            momentum_history = self._momentum_history_for_instrument(inst_id)
            guard_outcome = apply_quote_plan_guards(
                side_plan=side_plan,
                quote_mode=self.maker_quote_sides,
                phase_value=phase.value,
                inventory_delta_shares=self.inventory_delta_shares,
                early_sell_only_sec=float(self.maker_early_sell_only_sec),
                time_left_sec_global=time_left_sec_global,
                directional_edge_gate_enabled=self.maker_directional_edge_gate_enabled,
                regime_guard_active=regime_guard_active,
                min_directional_edge_ps=self.maker_min_directional_edge_ps,
                min_directional_edge_ps_conservative=self.maker_min_directional_edge_ps_conservative,
                now_ts=now_ts,
                buy_cooldown_until_ts=float(self.buy_cooldown_until_ts),
                momentum_buy_filter_pct=self.maker_momentum_buy_filter_pct,
                momentum_sell_filter_pct=self.maker_momentum_sell_filter_pct,
                momentum_window_ticks=self.maker_momentum_window_ticks,
                momentum_history=momentum_history,
                fair=fair,
                min_fair_price=self.maker_min_fair_price,
                max_fair_price=self.maker_max_fair_price,
                end_ts=end_ts,
                min_minutes_to_close=self.maker_min_minutes_to_close,
                reduce_only_no_new_sell_last_sec=self.maker_reduce_only_no_new_sell_last_sec,
                forced_sell_only=forced_sell_only,
                active_side=self.active_side.value,
                min_directional_edge_ps_down=self.maker_min_directional_edge_ps_down,
            )
            side_disable_reason_by_side = guard_outcome.side_disable_reason_by_side
            reduce_only_reason = guard_outcome.reduce_only.reason
            reduce_only_tail_sell_block = guard_outcome.reduce_only.tail_sell_block
            reduce_only_tail_sec_left = guard_outcome.reduce_only.tail_sec_left

            if guard_outcome.buy_cooldown_remaining is not None:
                remaining = guard_outcome.buy_cooldown_remaining
                if not getattr(self, "_logged_buy_cd", False) or time.time() - getattr(self, "_last_buy_cd_log_ts", 0) > 30:
                    logger.info(f"Post-fill buy cooldown active: {remaining:.1f}s remaining")
                    self._logged_buy_cd = True
                    self._last_buy_cd_log_ts = time.time()
            elif getattr(self, "_logged_buy_cd", False):
                self._logged_buy_cd = False
                logger.info("Post-fill buy cooldown cleared.")



            if guard_outcome.momentum_buy_blocked and guard_outcome.momentum_trend_pct is not None:
                if not getattr(self, "_logged_mom_buy", False) or time.time() - getattr(self, "_last_mom_ts", 0) > 30:
                    threshold_pct = (
                        float((guard_outcome.momentum_buy_threshold_pct or Decimal("0")) * 100)
                        if guard_outcome.momentum_buy_threshold_pct is not None
                        else 0.0
                    )
                    logger.warning(
                        "Trend Protection: momentum filter "
                        f"(dropped {float(guard_outcome.momentum_trend_pct * 100):.1f}% <= -{threshold_pct:.1f}%). "
                        "Blocking BUY orders."
                    )
                    self._logged_mom_buy = True
                    self._last_mom_ts = time.time()
            elif "buy" in side_plan and getattr(self, "_logged_mom_buy", False):
                self._logged_mom_buy = False

            if guard_outcome.momentum_sell_blocked and guard_outcome.momentum_trend_pct is not None:
                if not getattr(self, "_logged_mom_sell", False) or time.time() - getattr(self, "_last_mom_ts_s", 0) > 30:
                    threshold_pct = (
                        float((guard_outcome.momentum_sell_threshold_pct or Decimal("0")) * 100)
                        if guard_outcome.momentum_sell_threshold_pct is not None
                        else 0.0
                    )
                    logger.warning(
                        "Trend Protection: momentum filter "
                        f"(pumped {float(guard_outcome.momentum_trend_pct * 100):.1f}% >= +{threshold_pct:.1f}%). "
                        "Blocking SELL orders."
                    )
                    self._logged_mom_sell = True
                    self._last_mom_ts_s = time.time()
            elif "sell" in side_plan and getattr(self, "_logged_mom_sell", False):
                self._logged_mom_sell = False

            if reduce_only_reason:
                if "buy" in side_plan:
                    if not getattr(self, "_logged_reduce_only", False) or time.time() - getattr(self, "_last_ro_log_ts", 0) > 60:
                        logger.warning(f"Maker Reduce-Only active ({reduce_only_reason}). Blocking BUY orders.")
                        self._logged_reduce_only = True
                        self._last_ro_log_ts = time.time()
                if "sell" in side_plan and reduce_only_tail_sell_block:
                    if (
                        not getattr(self, "_logged_reduce_only_tail_sell_block", False)
                        or time.time() - getattr(self, "_last_ro_tail_sell_log_ts", 0) > 60
                    ):
                        logger.warning(
                            "Maker Reduce-Only tail guard active "
                            f"({reduce_only_tail_sec_left:.1f}s left <= {self.maker_reduce_only_no_new_sell_last_sec}s). "
                            "Blocking new SELL quotes."
                        )
                        self._logged_reduce_only_tail_sell_block = True
                        self._last_ro_tail_sell_log_ts = time.time()
            elif "buy" in side_plan and getattr(self, "_logged_reduce_only", False):
                self._logged_reduce_only = False
                self._logged_extreme_sell_block = False
                self._logged_reduce_only_tail_sell_block = False

            live_shadow_payload = None
            for side, quote_data in side_plan.items():
                order_key = self._order_key_for(side, inst_id)
                inst_key = self._instrument_key(inst_id)
                current_slug = str(self.current_market_slug or "")
                market_stop_loss_count = int(self.market_stop_loss_count_by_slug.get(current_slug, 0))
                market_buy_count = int(self.market_buy_count_by_slug.get(current_slug, 0))
                if (
                    side == "buy"
                    and current_slug
                    and self.market_stop_loss_max_per_market > 0
                    and market_stop_loss_count >= self.market_stop_loss_max_per_market
                ):
                    self._db_order_event(
                        event_type="ORDER_SKIP_MARKET_STOP_LOSS_LIMIT",
                        side=side.upper(),
                        status="SKIPPED",
                        reason="market_stop_loss_limit",
                        payload={
                            "slug": current_slug,
                            "instrument_id": str(inst_id),
                            "market_stop_loss_count": market_stop_loss_count,
                            "market_stop_loss_max_per_market": self.market_stop_loss_max_per_market,
                        },
                    )
                    continue
                if (
                    side == "buy"
                    and current_slug
                    and self.market_max_buy_events_per_market > 0
                    and market_buy_count >= self.market_max_buy_events_per_market
                ):
                    self._db_order_event(
                        event_type="ORDER_SKIP_MARKET_BUY_LIMIT",
                        side=side.upper(),
                        status="SKIPPED",
                        reason="market_buy_limit",
                        payload={
                            "slug": current_slug,
                            "instrument_id": str(inst_id),
                            "market_buy_count": market_buy_count,
                            "market_max_buy_events_per_market": self.market_max_buy_events_per_market,
                        },
                    )
                    continue
                if inst_key in self.pending_taker_exit_by_inst:
                    self._db_order_event(
                        event_type="ORDER_SKIP_PENDING_TAKER_EXIT",
                        side=side.upper(),
                        status="SKIPPED",
                        reason="pending_taker_exit",
                        payload={
                            "instrument_id": str(inst_id),
                            "pending_taker_exit_client_order_id": self.pending_taker_exit_by_inst.get(inst_key),
                        },
                    )
                    continue
                sell_pause_until = float(self._sell_reject_pause_until_by_inst.get(inst_key, 0.0))
                sellable_qty = None
                confirmed_inventory_qty = Decimal("0")
                other_held_inventory_qty = Decimal("0")
                if side == "sell":
                    confirmed_inventory_qty = self._get_confirmed_inventory_qty_for_instrument(inst_id)
                    for held_key, held_state in list(self.live_inventory_cost.items()):
                        if held_key == inst_key:
                            continue
                        try:
                            held_qty = Decimal(str(held_state.get("qty", "0")))
                        except Exception:
                            held_qty = Decimal("0")
                        if held_qty > 0:
                            other_held_inventory_qty += held_qty
                    recent_buy_ts = float(self.recent_buy_fill_ts_by_inst.get(inst_key, 0.0))
                    if recent_buy_ts > 0 and self.sell_delay_after_buy_sec > 0:
                        sell_pause_until = max(
                            sell_pause_until,
                            recent_buy_ts + float(self.sell_delay_after_buy_sec),
                        )
                if side == "sell" and not self._is_dry_run_mode():
                    sellable_qty = self._get_effective_sellable_qty(instrument_id=inst_id)
                inv_state = self.live_inventory_cost.get(inst_key) if inst_key else None
                avg_entry = (
                    Decimal(str(inv_state.get("avg_entry_price", "0")))
                    if inv_state is not None
                    else Decimal("0")
                )
                current_inst_inventory_qty = (
                    Decimal(str(inv_state.get("qty", "0")))
                    if inv_state is not None
                    else Decimal("0")
                )
                buy_entry_eval = evaluate_buy_entry_controls(
                    side=side,
                    bi_side_enabled=self.bi_side_enabled,
                    active_side_locked=self.active_side_locked,
                    active_side_value=self.active_side.value,
                    latest_observation_supports_locked_side=self._latest_observation_supports_locked_side(
                        self.active_side,
                        self.side_decision_score,
                    ),
                    side_score=self.side_decision_score,
                    directional_entry_min_score_abs_new=self.directional_entry_min_score_abs_new,
                    maker_min_expected_net_usdc=self.maker_min_expected_net_usdc,
                    maker_reload_min_expected_net_multiplier=self.maker_reload_min_expected_net_multiplier,
                    current_inst_inventory_qty=current_inst_inventory_qty,
                    maker_reload_inventory_threshold_shares=self.maker_reload_inventory_threshold_shares,
                    current_slug=current_slug,
                    inst_id=inst_id,
                    # Trend-buy params
                    trend_buy_enabled=self.trend_buy_enabled,
                    trend_buy_min_score=self.trend_buy_min_score,
                    trend_buy_min_net_usdc=self.trend_buy_min_net_usdc,
                    active_instrument_id=self._instrument_for_side(self.active_side),
                    time_left_sec=time_left_sec_global,
                    trend_buy_min_time_left_sec=self.trend_buy_min_time_left_sec,
                    best_bid=quote_ctx.quote[0] if quote_ctx.quote is not None else None,
                    fair=quote_ctx.fair,
                    trend_buy_max_price_premium_ps=self.trend_buy_max_price_premium_ps,
                )
                min_expected_net_usdc = buy_entry_eval.min_expected_net_usdc
                if buy_entry_eval.skip:
                    self._db_order_event(
                        event_type=buy_entry_eval.event_type,
                        side=side.upper(),
                        status="SKIPPED",
                        reason=buy_entry_eval.reason,
                        payload=buy_entry_eval.payload or {},
                    )
                    continue
                # Determine if the directional thesis has weakened against our position.
                # Loss-selling should be allowed more aggressively when we are confirmed
                # offside against a locked side decision, even if cost-protect would
                # normally block the new SELL price.
                _thesis_weakened = False
                _offside_confirmed = False
                _stop_loss_regime_armed = False
                hold_sec = 0.0
                if (
                    side == "sell"
                    and self.inventory_delta_shares > 0
                    and self.bi_side_enabled
                ):
                    target_active_inst = self._instrument_for_side(self.active_side)
                    if (
                        self.active_side_locked
                        and self.active_side != ActiveSide.NONE
                        and target_active_inst is not None
                        and target_active_inst != inst_id
                    ):
                        _offside_confirmed = True
                    _thesis_weakened = self._assess_thesis_weakened(
                        inst_id=inst_id,
                        now_ts=now_ts,
                        side_score=self.side_decision_score,
                    )
                    if (
                        hasattr(self, "position_manager")
                        and inv_state is not None
                    ):
                        opened_ts = float(inv_state.get("opened_ts", 0.0))
                        hold_sec = max(0.0, now_ts - opened_ts) if opened_ts > 0 else 0.0
                        held_side = (
                            self._side_for_instrument_id(inst_id).value
                            if hasattr(self, "_side_for_instrument_id")
                            else "NONE"
                        )
                        matches_position = self._instrument_for_side(self.active_side) == inst_id
                        regime = self.position_manager.assess_stop_loss_regime(
                            inst_key=inst_key,
                            now_ts=now_ts,
                            qty=current_inst_inventory_qty,
                            opened_ts=opened_ts,
                            held_side=held_side,
                            signal_active_side=self.active_side.value,
                            signal_score=self.side_decision_score,
                            signal_matches_position=matches_position,
                            force_exit=False,
                        )
                        _stop_loss_regime_armed = regime.status == "armed"
                    if quote_ctx.quote is not None:
                        self._update_profit_run_peaks(
                            inst_id,
                            best_bid=quote_ctx.quote[0],
                            fair=quote_ctx.fair,
                        )

                desired_entry = build_desired_quote_entry(
                    order_key=order_key,
                    side=side,
                    inst_id=inst_id,
                    quote_data=quote_data,
                    side_disable_reason_by_side=side_disable_reason_by_side,
                    reduce_only_reason=reduce_only_reason,
                    reduce_only_tail_sell_block=reduce_only_tail_sell_block,
                    reduce_only_no_new_sell_last_sec=self.maker_reduce_only_no_new_sell_last_sec,
                    forced_sell_only=forced_sell_only,
                    min_expected_net_usdc=min_expected_net_usdc,
                    now_ts=now_ts,
                    sell_pause_until=sell_pause_until,
                    is_dry_run_mode=self._is_dry_run_mode(),
                    sellable_qty=sellable_qty,
                    maker_exchange_min_shares=self.maker_exchange_min_shares,
                    avg_entry=avg_entry,
                    emergency_window=self._is_emergency_exit_window(time_left_sec_global),
                    high_cost_exit_cooldown_enabled=self.maker_high_cost_exit_cooldown_enabled,
                    high_cost_exit_cooldown_sec=float(self.maker_high_cost_exit_cooldown_sec),
                    high_cost_exit_cooldown_until=float(self.high_cost_exit_cooldown_until_by_inst.get(inst_key, 0.0)),
                    maker_sell_cost_protect_enabled=self.maker_sell_cost_protect_enabled,
                    maker_sell_cost_protect_fee_buffer_ps=self.maker_sell_cost_protect_fee_buffer_ps,
                    maker_sell_min_profit_floor_ps=self.maker_sell_min_profit_floor_ps,
                    thesis_weakened=_thesis_weakened,
                    offside_confirmed=_offside_confirmed,
                    stop_loss_regime_armed=_stop_loss_regime_armed,
                    hold_sec=hold_sec,
                    loss_sell_min_hold_sec=self.maker_loss_sell_min_hold_sec,
                    time_left_sec=time_left_sec_global,
                    # Trend-buy params
                    entry_mode=buy_entry_eval.entry_mode,
                    trend_buy_penalty_discount=self.trend_buy_penalty_discount,
                    trend_buy_score=self.side_decision_score,
                    trend_buy_size_multiplier=self.trend_buy_size_multiplier,
                )
                desired_entry = attach_desired_entry_runtime_metadata(
                    desired_entry=desired_entry,
                    dynamic_fee_rate=quote_ctx.dynamic_fee_rate,
                    min_expected_net_usdc=min_expected_net_usdc,
                    quote=quote_ctx.quote,
                    now_ts=now_ts,
                )
                desired_entry = apply_confirmed_inventory_sell_guard(
                    desired_entry=desired_entry,
                    side=side,
                    confirmed_inventory_qty=confirmed_inventory_qty,
                    other_held_inventory_qty=other_held_inventory_qty,
                )
                desired_entry = preserve_profitable_existing_sell_order(
                    desired_entry=desired_entry,
                    side=side,
                    existing_state=self.active_maker_orders.get(order_key),
                    avg_entry=avg_entry,
                    maker_sell_cost_protect_fee_buffer_ps=self.maker_sell_cost_protect_fee_buffer_ps,
                    maker_sell_min_profit_floor_ps=self.maker_sell_min_profit_floor_ps,
                )
                desired_entry = preserve_recent_loss_sell_order(
                    desired_entry=desired_entry,
                    side=side,
                    existing_state=self.active_maker_orders.get(order_key),
                    now_ts=now_ts,
                    loss_sell_reprice_min_interval_sec=self.maker_loss_sell_reprice_min_interval_sec,
                )
                if (
                    side == "sell"
                    and desired_entry.get("should_quote", False)
                    and quote_ctx.quote is not None
                ):
                    hold_profit_run, hold_reason = self._should_hold_profitable_position(
                        instrument_id=inst_id,
                        best_bid=quote_ctx.quote[0],
                        fair=quote_ctx.fair,
                        avg_entry=avg_entry,
                        time_left_sec=time_left_sec_global,
                        thesis_weakened=_thesis_weakened,
                        offside_confirmed=_offside_confirmed,
                    )
                    if hold_profit_run:
                        desired_entry["should_quote"] = False
                        desired_entry["diag_reason"] = hold_reason

                desired_entry = apply_reload_edge_guard(
                    desired_entry=desired_entry,
                    side=side,
                    current_inst_inventory_qty=current_inst_inventory_qty,
                    maker_reload_inventory_threshold_shares=self.maker_reload_inventory_threshold_shares,
                    maker_reload_min_directional_edge_ps=self.maker_reload_min_directional_edge_ps,
                )
                desired_entry = maybe_apply_trapped_inventory_recovery(
                    desired_entry=desired_entry,
                    side=side,
                    trapped_inventory_recovery_enabled=self.trapped_inventory_recovery_enabled,
                    current_inst_inventory_qty=current_inst_inventory_qty,
                    trapped_inventory_recovery_min_qty=self.trapped_inventory_recovery_min_qty,
                    maker_exchange_min_shares=self.maker_exchange_min_shares,
                    active_side_locked=bool(self.active_side_locked),
                    inst_id=inst_id,
                    active_instrument_id=self._instrument_for_side(self.active_side),
                    latest_observation_supports_locked_side=self._latest_observation_supports_locked_side(
                        self.active_side,
                        self.side_decision_score,
                    ),
                    robust_net=desired_entry.get("robust_net"),
                    max_robust_net_deficit_usdc=self.trapped_inventory_recovery_max_robust_net_deficit_usdc,
                    time_left_sec=time_left_sec_global,
                )
                profit_cap_before_price = desired_entry.get("price")
                profit_cap_before_reason = desired_entry.get("diag_reason")
                desired_entry = apply_time_based_profitable_sell_cap(
                    desired_entry=desired_entry,
                    side=side,
                    avg_entry=avg_entry,
                    maker_sell_cost_protect_fee_buffer_ps=self.maker_sell_cost_protect_fee_buffer_ps,
                    maker_sell_min_profit_floor_ps=self.maker_sell_min_profit_floor_ps,
                    profitable_sell_cap_enabled=self.maker_profitable_sell_cap_enabled,
                    profitable_sell_cap_passive_offset_ps=self.maker_profitable_sell_cap_passive_offset_ps,
                    profitable_sell_cap_aggressive_offset_ps=self.maker_profitable_sell_cap_aggressive_offset_ps,
                    profitable_sell_cap_taker_offset_ps=self.maker_profitable_sell_cap_taker_offset_ps,
                    exit_stage_value=self.exit_policy.stage(time_left_sec_global).value,
                    tick=quote_ctx.tick,
                )
                if (
                    side == "sell"
                    and desired_entry.get("should_quote", False)
                    and desired_entry.get("diag_reason") != profit_cap_before_reason
                    and str(desired_entry.get("diag_reason", "")).startswith("profit_cap ")
                    and desired_entry.get("price") != profit_cap_before_price
                ):
                    self._db_strategy_event(
                        "PROFIT_CAP_APPLIED",
                        {
                            "side": "SELL",
                            "old_price": float(Decimal(str(profit_cap_before_price))),
                            "new_price": float(Decimal(str(desired_entry.get("price")))),
                            "avg_entry": float(avg_entry),
                            "best_bid": float(quote_ctx.quote[0]) if quote_ctx.quote is not None else None,
                            "best_ask": float(quote_ctx.quote[1]) if quote_ctx.quote is not None else None,
                            "exit_stage": self.exit_policy.stage(time_left_sec_global).value,
                            "diag_reason": desired_entry.get("diag_reason"),
                        },
                    )
                if side == "buy" and quote_ctx.quote is not None:
                    locked_for_sec = (
                        max(0.0, now_ts - float(getattr(self, "active_side_locked_since_ts", 0.0)))
                        if getattr(self, "active_side_locked_since_ts", 0.0) > 0
                        else 0.0
                    )
                    desired_entry = maybe_apply_continuation_entry(
                        desired_entry=desired_entry,
                        side=side,
                        active_side_locked=bool(self.active_side_locked),
                        active_side_value=self.active_side.value,
                        inst_id=inst_id,
                        active_instrument_id=self._instrument_for_side(self.active_side),
                        side_score=self.side_decision_score,
                        locked_for_sec=locked_for_sec,
                        time_left_sec=time_left_sec_global,
                        current_inventory_qty=current_inst_inventory_qty,
                        best_bid=quote_ctx.quote[0],
                        fair=quote_ctx.fair,
                        continuation_enabled=self.continuation_entry_enabled,
                        continuation_size_multiplier=self.continuation_entry_size_multiplier,
                    )
                    if live_shadow_payload is None:
                        live_shadow_payload = self._build_live_signal_compare_payload(now_ts)
                    desired_entry = apply_shadow_entry_veto(
                        desired_entry=desired_entry,
                        side=side,
                        entry_mode=str(desired_entry.get("entry_mode", buy_entry_eval.entry_mode or "value")).lower(),
                        inst_id=inst_id,
                        up_instrument_id=self.current_up_instrument_id,
                        down_instrument_id=self.current_down_instrument_id,
                        shadow_payload=live_shadow_payload,
                    )
                desired_quotes[order_key] = desired_entry

        return desired_quotes, diag_context_by_inst

    async def _submit_quote_cycle(
        self,
        *,
        phase: MarketPhase,
        now_ts: float,
        target_instruments: List[Any],
        target_inst_set: Set[str],
        desired_quotes: Dict[str, Dict[str, Any]],
        diag_context_by_inst: Dict[str, Dict[str, Any]],
    ) -> None:
        submitted_attempts = 0

        reconcile_unwanted_quotes(
            active_maker_orders=self.active_maker_orders,
            desired_quotes=desired_quotes,
            target_inst_set=target_inst_set,
            now_ts=now_ts,
            cancel_cooldown_sec=float(self.maker_cancel_cooldown_sec),
            gate_block_grace_sec=float(self.maker_gate_block_grace_sec),
            reason_family_fn=self._reason_family,
            cancel_order_fn=self._cancel_maker_order_side,
            gate_block_since_by_order_key=self._gate_block_since_by_order_key,
            gate_block_reason_by_order_key=self._gate_block_reason_by_order_key,
            gate_last_cancel_ts_by_order_key=self._gate_last_cancel_ts_by_order_key,
        )

        # Quote desired sides with selective requote.
        for order_key, desired in desired_quotes.items():
            if not bool(desired.get("should_quote", False)):
                continue
            submitted_attempts += 1
            side = str(desired["side"])
            inst_id = desired["instrument_id"]
            limit_price = Decimal(str(desired["price"]))
            econ = desired["econ"]
            dynamic_fee_rate = desired.get("dynamic_fee_rate")
            directional_snapshot = build_directional_snapshot(desired)

            # Use per-instrument tick size to build stable target versions.
            inst_for_tick = self._normalize_instrument_id(inst_id)
            inst_obj = self.cache.instrument(inst_for_tick) if inst_for_tick else None
            tick = extract_instrument_tick(inst_obj, default_tick="0.01")

            target_version = compute_requote_target_version(
                order_key=order_key,
                limit_price=limit_price,
                tick=tick,
                maker_requote_hysteresis_ticks=self.maker_requote_hysteresis_ticks,
                target_anchor_price_by_order_key=self._target_anchor_price_by_order_key,
            target_version_by_order_key=self._target_version_by_order_key,
            )

            current = self.active_maker_orders.get(order_key)
            if should_requote_existing_order(
                current=current,
                target_version=target_version,
                now_ts=now_ts,
                maker_requote_min_age_sec=float(self.maker_requote_min_age_sec),
                side=side,
                maker_requote_min_age_sec_sell=float(self.maker_requote_min_age_sec_sell),
            ):
                # Token Bucket guard against cancel storms
                if self.requote_bucket_tokens < 1.0:
                    now_ts = time.time()
                    if now_ts - getattr(self, "_last_rl_log_ts", 0) > 10:
                        logger.warning(
                            f"Rate Limiter: out of requote tokens ({float(self.requote_bucket_tokens):.2f}/{self.maker_requote_max_per_sec}). "
                            "Skipping requote."
                        )
                        self._last_rl_log_ts = now_ts
                    continue
                
                self.requote_bucket_tokens -= 1.0

                # Safety-first requote: cancel existing order and wait for cancel ack/reconcile
                # before submitting a replacement. This prevents multiple live orders on the same
                # side/instrument when cancel acknowledgements are delayed or duplicated.
                self._cancel_maker_order_side(order_key, reason="requote")
                continue
            if current:
                continue

            await self._submit_maker_quote(
                inst_id,
                side,
                limit_price,
                econ,
                dynamic_fee_rate,
                directional_snapshot=directional_snapshot,
                target_version=target_version,
                loss_sell_reason=desired.get("loss_sell_reason", ""),
            )

        log_no_quote_diagnostics(
            submitted_attempts=submitted_attempts,
            target_instruments=target_instruments,
            desired_quotes=desired_quotes,
            diag_context_by_inst=diag_context_by_inst,
            now_ts=now_ts,
            no_quote_diag_interval_sec=float(self.no_quote_diag_interval_sec),
            phase_value=phase.value,
            instrument_key_fn=self._instrument_key,
            active_order_keys_fn=self._active_order_keys,
            last_no_quote_diag_ts_by_inst=self._last_no_quote_diag_ts_by_inst,
            logger_info_fn=logger.info,
            reason_family_fn=self._reason_family,
            strategy_event_fn=self._db_strategy_event,
        )

    async def _quote_maker_orders(self, bid_price: Decimal, ask_price: Decimal) -> None:
        """
        Place symmetric maker quotes if expected net economics is positive.
        """
        cycle = await self._prepare_quote_cycle()
        if cycle is None:
            return
        desired_quotes, diag_context_by_inst = await self._evaluate_quote_targets(
            phase=cycle["phase"],
            forced_sell_only=cycle["forced_sell_only"],
            regime_guard_active=cycle["regime_guard_active"],
            now_ts=cycle["now_ts"],
            recent_vol=cycle["recent_vol"],
            target_instruments=cycle["target_instruments"],
            end_ts=cycle["end_ts"],
            time_left_sec_global=cycle["time_left_sec_global"],
        )
        await self._submit_quote_cycle(
            phase=cycle["phase"],
            now_ts=cycle["now_ts"],
            target_instruments=cycle["target_instruments"],
            target_inst_set=cycle["target_inst_set"],
            desired_quotes=desired_quotes,
            diag_context_by_inst=diag_context_by_inst,
        )

    async def _submit_maker_quote(
        self,
        instrument_id: Any,
        side: str,
        limit_price: Decimal,
        econ,
        dynamic_fee_rate: Optional[Decimal] = None,
        directional_snapshot: Optional[Dict[str, Any]] = None,
        target_version: Optional[int] = None,
        loss_sell_reason: str = "",
    ) -> None:
        submit_maker_quote(
            self,
            instrument_id=instrument_id,
            side=side,
            limit_price=limit_price,
            econ=econ,
            dynamic_fee_rate=dynamic_fee_rate,
            directional_snapshot=directional_snapshot,
            target_version=target_version,
            loss_sell_reason=loss_sell_reason,
        )
    
    def on_start(self):
        """Called when strategy starts."""
        logger.info("Strategy start sequence initiated.")
        self._log_strategy_config_summary()
        self.last_valid_quote_ts = time.time()
        self.consecutive_invalid_quote_ticks = 0
        
        # Find BTC instrument FIRST and wait for it
        if not self._wait_for_btc_instrument(timeout_sec=60, poll_interval_sec=2):
            raise RuntimeError("Startup check failed: no BTC 15-min instrument loaded")

        # Recover local inventory tracking if the strategy restarted mid-market.
        self._rehydrate_inventory_state_on_startup()

        log_strategy_run_start(
            trade_db=self.trade_db,
            run_id=self.run_id,
            is_dry_run_mode=self._is_dry_run_mode(),
            test_mode=self.test_mode,
            maker_mode=self.maker_mode,
            instrument_id=self.instrument_id,
            selected_slug=self.selected_slug,
            maker_quote_sides=self.maker_quote_sides,
            maker_quote_size_usdc=self.maker_quote_size_usdc,
        )
        self._db_strategy_event(
            "STRATEGY_START",
            {
                "instrument_id": str(self.instrument_id) if self.instrument_id else None,
                "selected_slug": self.selected_slug,
                "test_mode": self.test_mode,
                "bi_side_enabled": self.bi_side_enabled,
                "active_side": self.active_side.value,
                "git_revision": self.runtime_git_revision,
                "maker_fixed_shares": float(self.maker_fixed_shares),
                "maker_max_order_usdc": float(self.maker_max_order_usdc),
                "directional_entry_min_score_abs": float(self.directional_entry_min_score_abs),
                "maker_urgent_exit_enabled": self.maker_urgent_exit_enabled,
                "maker_min_directional_edge_ps_down": float(self.maker_min_directional_edge_ps_down),
                "maker_reload_inventory_threshold_shares": float(self.maker_reload_inventory_threshold_shares),
                "maker_reload_min_expected_net_multiplier": float(self.maker_reload_min_expected_net_multiplier),
                "maker_reload_min_directional_edge_ps": float(self.maker_reload_min_directional_edge_ps),
                "taker_exit_eval_interval_sec": float(self.taker_exit_eval_interval_sec),
                "taker_exit_stop_loss_confirmations": int(self.taker_exit_stop_loss_confirmations),
                "taker_exit_stop_loss_usdc": float(self.taker_exit_stop_loss_usdc),
                "taker_exit_wait_for_sell_quote_sec": float(self.taker_exit_wait_for_sell_quote_sec),
                "market_stop_loss_max_per_market": int(self.market_stop_loss_max_per_market),
                "market_max_buy_events_per_market": int(self.market_max_buy_events_per_market),
                "stop_loss_reentry_cooldown_sec": int(self.stop_loss_reentry_cooldown_sec),
                "exit_stop_loss_requires_thesis_weakening": self.exit_stop_loss_requires_thesis_weakening,
                "exit_stop_loss_hold_on_none_signal": self.exit_stop_loss_hold_on_none_signal,
                "exit_stop_loss_thesis_min_score_abs": float(self.exit_stop_loss_thesis_min_score_abs),
                "exit_conviction_band_min_price": float(self.exit_conviction_band_min_price),
                "exit_hold_band_min_price": float(self.exit_hold_band_min_price),
                "exit_conviction_band_min_score_abs": float(self.exit_conviction_band_min_score_abs),
                "exit_hold_band_min_score_abs": float(self.exit_hold_band_min_score_abs),
                "exit_conviction_stop_loss_multiplier": float(self.exit_conviction_stop_loss_multiplier),
                "exit_conviction_extra_confirmations": int(self.exit_conviction_extra_confirmations),
                "exit_hold_band_requires_locked": self.exit_hold_band_requires_locked,
            },
        )
        self._bootstrap_regime_guard_window_from_db()

        # Log which side-decision engine is active
        if self.bi_side_enabled:
            logger.info(
                "Side decision engine: SignalEngine (probabilistic) | "
                f"min_confidence={self.side_signal_min_confidence} "
                f"threshold_up={self.side_signal_threshold_up} "
                f"threshold_down={self.side_signal_threshold_down} | "
                f"BTC EMA {self.side_signal_btc_ema_fast_sec}s/{self.side_signal_btc_ema_slow_sec}s | "
                f"Mid EMA {self.side_signal_mid_ema_fast_sec}s/{self.side_signal_mid_ema_slow_sec}s"
            )
        
        # Ensure we have sufficient history.
        if len(self.price_history) < 20:
            self._generate_synthetic_history(target_count=20, existing_count=len(self.price_history))
        
        # Try to get real price if instrument exists and we have quotes
        if self.instrument_id:
            try:
                # Get the most recent quote from cache
                quote = self.cache.quote_tick(self.instrument_id)
                if quote and quote.bid_price and quote.ask_price:
                    current_price = (quote.bid_price + quote.ask_price) / 2
                    # Replace last synthetic price with real one
                    if self.price_history:
                        self.price_history[-1] = current_price
                    else:
                        self.price_history.append(current_price)
                    self.real_price_history.append(current_price)
                    if len(self.real_price_history) > self.max_real_history:
                        self.real_price_history.pop(0)
            except Exception as e:
                logger.debug(f"Could not get real price: {e}")
                logger.debug("Using synthetic prices until real quotes arrive")
        
        # Start market lifecycle timer (replaces fixed 12-min reload)
        self._lifecycle_stop_event.clear()
        self._lifecycle_thread = start_background_thread(self._start_market_lifecycle_timer, "market-lifecycle")
        # Initialize live Prometheus trading metrics
        self._init_live_prom_metrics()
        # Start real-time BTC price streams
        self._start_polymarket_chainlink_ws()
        self._start_binance_ws()
        # Also start the legacy reload timer as a fallback
        self._reload_stop_event.clear()
        self._reload_thread = start_background_thread(self._start_reload_timer, "reload-timer")
        self._quote_watchdog_stop_event.clear()
        self._quote_watchdog_thread = start_background_thread(self._start_quote_watchdog_timer, "quote-watchdog")
        # Initialize phase based on current market
        self._update_market_phase()
        if self.auto_redeem_enabled:
            self._redeem_stop_event.clear()
            self._redeem_thread = start_background_thread(self._start_auto_redeem_timer, "auto-redeem")
            self._schedule_auto_redeem(reason="startup")
        self._balance_stop_event.clear()
        self._balance_thread = start_background_thread(self._start_balance_refresh_timer, "balance-refresh")
        try:
            self._refresh_balance_cache_sync()
        except Exception as e:
            logger.debug(f"Initial balance refresh failed: {e}")
        
        # Start Grafana if enabled
        if self.grafana_exporter:
            start_background_thread(self._start_grafana_sync, "grafana-sync")
        if self.terminal_dashboard:
            self._terminal_dashboard_stop_event.clear()
            self._terminal_dashboard_thread = start_background_thread(
                self._start_terminal_dashboard_sync,
                "terminal-dashboard",
            )
            self._update_terminal_dashboard_snapshot()

        logger.info(f"Strategy active. price_history_points={len(self.price_history)}")
        if len(self.price_history) >= 20:
            logger.info("Ready to trade at next interval.")
        else:
            logger.warning(f"Need more history ({len(self.price_history)}/20)")
                
    def _preload_history_sync(self):
        """Synchronous wrapper for history preload."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._preload_price_history())
        finally:
            loop.close()
    
    async def _preload_price_history(self):
        """Pre-load price history from cache or generate synthetic data for testing."""
        logger.info("Preloading price history...")
        
        # Get current instrument
        if not self.instrument_id:
            logger.warning("No instrument ID, skipping preload")
            return
        
        # Try to get current price from cache first
        quote = self.cache.quote_tick(self.instrument_id)
        if quote:
            current_price = (quote.bid_price + quote.ask_price) / 2
            self.price_history.append(current_price)
        
        self.price_history = dedupe_price_history(self.price_history)
        
        # If still not enough, generate synthetic data
        if len(self.price_history) < 20:
            logger.warning(f"Only {len(self.price_history)} historical quotes found, generating synthetic data to fill")
            self._generate_synthetic_history(existing_count=len(self.price_history))
        
        logger.info(f"Price history preload complete: points={len(self.price_history)}")
        if len(self.price_history) >= 20:
            logger.info("Price history is sufficient.")
        else:
            logger.warning("Still need more history - will collect from live data")
    
    def _generate_synthetic_history(self, target_count: int = 20, existing_count: int = 0):
        """Generate synthetic price history for testing/initialization."""
        needed = extend_synthetic_history(
            price_history=self.price_history,
            target_count=target_count,
            existing_count=existing_count,
        )
        if needed > 0:
            logger.info(f"Synthetic history added: +{needed} (total={len(self.price_history)})")
    
    def _start_reload_timer(self):
        """Start timer to reload instruments every 12 minutes."""
        while not self._reload_stop_event.wait(720):  # 12 minutes
            logger.info("Reloading instruments (timer)...")
            
            try:
                # Request instrument reload from data client
                instruments = self.cache.instruments()
                
                # Re-find BTC instrument (this will select the active one)
                previous_slug = self.current_market_slug
                if not self._find_btc_instrument():
                    logger.warning("Reload completed but no BTC 15-min instrument found")
                elif self.auto_redeem_enabled and self.auto_redeem_on_rollover and previous_slug and self.current_market_slug and previous_slug != self.current_market_slug:
                    self._schedule_auto_redeem(reason=f"market_rollover:{previous_slug}->{self.current_market_slug}")
                
                logger.info(f"Instruments reload complete. cached={len(instruments)}")
            except Exception as e:
                logger.error(f"Failed to reload instruments: {e}")

    def _schedule_auto_redeem(self, reason: str) -> None:
        """
        Run redeem checker script in a detached worker so trading flow is never blocked.
        """
        if not self.auto_redeem_enabled:
            return

        def _runner() -> None:
            if not self._redeem_job_lock.acquire(blocking=False):
                logger.debug(f"Auto redeem skipped (already running): reason={reason}")
                return
            try:
                now_ts = time.time()
                skip_run, elapsed_since_last = should_skip_auto_redeem_run(
                    now_ts=now_ts,
                    auto_redeem_min_gap_sec=float(self.auto_redeem_min_gap_sec),
                    last_redeem_run_ts=float(self._last_redeem_run_ts),
                )
                if skip_run:
                    logger.info(
                        "Auto redeem skipped by min gap: "
                        f"reason={reason} elapsed={elapsed_since_last:.1f}s "
                        f"required={self.auto_redeem_min_gap_sec}s"
                    )
                    return
                self._last_redeem_run_ts = now_ts
                run_auto_redeem_script(
                    repo_root=Path(__file__).parent,
                    reason=reason,
                    auto_redeem_slug_filter=self.auto_redeem_slug_filter,
                    auto_redeem_apply=self.auto_redeem_apply,
                    auto_redeem_timeout_sec=int(self.auto_redeem_timeout_sec),
                    logger_info_fn=logger.info,
                    logger_warning_fn=logger.warning,
                    db_strategy_event_fn=self._db_strategy_event,
                )
            except Exception as e:
                logger.warning(f"Auto redeem failed (reason={reason}): {e}")
            finally:
                self._redeem_job_lock.release()

        threading.Thread(target=_runner, daemon=True).start()

    def _start_auto_redeem_timer(self) -> None:
        """
        Periodic auto redeem timer (default every 15 minutes).
        Also checks for YES/NO merge opportunities.
        """
        while not self._redeem_stop_event.wait(self.auto_redeem_interval_sec):
            if self._stopping:
                return
            self._schedule_auto_redeem(reason="interval")
            # Check for merge opportunities periodically
            self._try_merge_yes_no_positions()

    def _try_merge_yes_no_positions(self) -> None:
        try_merge_yes_no_positions(
            strategy=self,
            logger_info_fn=logger.info,
            logger_debug_fn=logger.debug,
            logger_warning_fn=logger.warning,
            adjust_inventory_after_merge_fn=adjust_inventory_after_merge,
        )

    def _execute_merge_on_chain(
        self, pk: str, condition_id: str, amount: int, rpc_url: str, chain_id: int
    ) -> bool:
        from bot.merge_ops import execute_merge_on_chain
        return execute_merge_on_chain(
            pk=pk,
            condition_id=condition_id,
            amount=amount,
            rpc_url=rpc_url,
            chain_id=chain_id,
            logger_info_fn=logger.info,
            logger_warning_fn=logger.warning,
        )

    def _trigger_quote_watchdog_reload(self, trigger: str, now_ts: float) -> None:
        """
        Recover quote stream when valid bid/ask updates disappear for too long.
        """
        selected_ok, reload_ts, _stale_for, prev_instrument = handle_quote_watchdog_recovery(
            trigger=trigger,
            now_ts=now_ts,
            last_quote_watchdog_reload_ts=float(self.last_quote_watchdog_reload_ts),
            quote_reload_cooldown_sec=float(self.quote_reload_cooldown_sec),
            instrument_id=self.instrument_id,
            last_valid_quote_ts=float(self.last_valid_quote_ts),
            consecutive_invalid_quote_ticks=int(self.consecutive_invalid_quote_ticks),
            db_strategy_event_fn=self._db_strategy_event,
            cancel_active_maker_orders_fn=self._cancel_active_maker_orders,
            find_btc_instrument_fn=self._find_btc_instrument,
            logger_warning_fn=logger.warning,
            logger_error_fn=logger.error,
        )
        if reload_ts == self.last_quote_watchdog_reload_ts:
            return
        self.last_quote_watchdog_reload_ts = reload_ts
        new_instrument = str(self.instrument_id) if self.instrument_id else None
        if selected_ok and self.instrument_id is not None:
            logger.warning(f"Quote watchdog recovery complete: {prev_instrument} -> {new_instrument}")
            self._db_strategy_event(
                "QUOTE_WATCHDOG_RECOVERED",
                {
                    "instrument_before": prev_instrument,
                    "instrument_after": new_instrument,
                },
            )
            self.consecutive_invalid_quote_ticks = 0
            self.last_valid_quote_ts = now_ts
            return

        logger.error("Quote watchdog recovery failed: no BTC 15-min instrument selected")
        self._db_strategy_event(
            "QUOTE_WATCHDOG_FAILED",
            {
                "instrument_before": prev_instrument,
                "instrument_after": new_instrument,
            },
        )

    def _maybe_run_quote_watchdog(self, trigger: str) -> None:
        now_ts = time.time()
        should_run, stale_for = should_run_quote_watchdog(
            now_ts=now_ts,
            last_quote_watchdog_check_ts=float(self.last_quote_watchdog_check_ts),
            quote_healthcheck_interval_sec=float(self.quote_healthcheck_interval_sec),
            last_valid_quote_ts=float(self.last_valid_quote_ts),
            quote_stale_sec=float(self.quote_stale_sec),
            consecutive_invalid_quote_ticks=int(self.consecutive_invalid_quote_ticks),
            quote_invalid_tick_reload_threshold=int(self.quote_invalid_tick_reload_threshold),
        )
        if not should_run:
            return
        self.last_quote_watchdog_check_ts = now_ts

        reason = trigger
        stale_hit = self.last_valid_quote_ts > 0 and stale_for >= self.quote_stale_sec
        invalid_hit = self.consecutive_invalid_quote_ticks >= self.quote_invalid_tick_reload_threshold
        if stale_hit:
            reason = f"{reason}|stale_quotes"
        if invalid_hit:
            reason = f"{reason}|invalid_ticks"
        self._trigger_quote_watchdog_reload(reason, now_ts)

    def _start_quote_watchdog_timer(self) -> None:
        """
        Background heartbeat for quote health.
        Needed because DataClient can drop incomplete ticks before strategy receives them.
        """
        while not self._quote_watchdog_stop_event.wait(self.quote_healthcheck_interval_sec):
            if self._stopping:
                return
            now_ts = time.time()
            self._emit_strategy_status(now_ts)
            stale_for = (now_ts - self.last_valid_quote_ts) if self.last_valid_quote_ts > 0 else None
            if stale_for is None or stale_for < self.quote_stale_sec:
                continue
            self._trigger_quote_watchdog_reload("timer_stale_quotes", now_ts)

    def _emit_strategy_status(self, now_ts: float) -> None:
        """
        Periodic concise status line to explain why bot is (not) quoting.
        """
        if now_ts - self.last_status_log_ts < self.strategy_status_interval_sec:
            return
        self.last_status_log_ts = now_ts
        self._emit_live_signal_compare_snapshot(now_ts)

        reasons: List[str] = []
        if self._stopping:
            reasons.append("stopping")
        if self.market_phase == MarketPhase.WAITING:
            reasons.append("phase_waiting")
        elif self.market_phase == MarketPhase.SETTLING:
            reasons.append("phase_settling")
        if not self.maker_mode:
            reasons.append("maker_mode_off")
        if self.maker_kill_switch:
            reasons.append("kill_switch_on")
        if now_ts < self.quote_pause_until_ts:
            reasons.append(f"paused_{int(self.quote_pause_until_ts - now_ts)}s")
        if now_ts < self.orderbook_unavailable_until_ts:
            reasons.append(f"orderbook_unavailable_{int(self.orderbook_unavailable_until_ts - now_ts)}s")
        if self.latest_market_bid is None or self.latest_market_ask is None:
            reasons.append("no_valid_quote")
        if self.instrument_id is None:
            reasons.append("no_instrument")
        if now_ts < float(self.regime_guard_conservative_until_ts):
            reasons.append(f"regime_guard_{int(self.regime_guard_conservative_until_ts - now_ts)}s")
        current_slug = str(self.current_market_slug or "")
        market_stop_loss_count = int(self.market_stop_loss_count_by_slug.get(current_slug, 0))
        if (
            current_slug
            and self.market_stop_loss_max_per_market > 0
            and market_stop_loss_count >= self.market_stop_loss_max_per_market
        ):
            reasons.append(
                f"market_stop_loss_limit_{market_stop_loss_count}/{self.market_stop_loss_max_per_market}"
            )
        if self.bi_side_enabled:
            if self.active_side == ActiveSide.NONE:
                reasons.append("active_side_none")
            elif self.active_side == ActiveSide.DOWN and self.current_down_instrument_id is None:
                reasons.append("down_instrument_missing")

        bid_txt = f"{float(self.latest_market_bid):.4f}" if self.latest_market_bid is not None else "None"
        ask_txt = f"{float(self.latest_market_ask):.4f}" if self.latest_market_ask is not None else "None"
        stale_for = (now_ts - self.last_valid_quote_ts) if self.last_valid_quote_ts > 0 else -1.0
        active_orders = list(self.active_maker_orders.keys())
        tradable = "YES" if len(reasons) == 0 else "NO"
        reason_txt = "ok" if len(reasons) == 0 else ",".join(reasons)
        side_score_txt = f"{float(self.side_decision_score):+.2f}" if self.bi_side_enabled else "n/a"
        side_reason_txt = self.side_decision_reason if self.bi_side_enabled else "disabled"
        side_locked_txt = "1" if self.active_side_locked else "0"
        side_due_in = max(0.0, self.side_decision_due_ts - now_ts) if self.bi_side_enabled and self.side_decision_due_ts > 0 else 0.0
        ref_spot_txt = f"{float(self.latest_external_spot):.2f}" if self.latest_external_spot is not None else "None"
        ref_src_txt = str(self.latest_external_spot_source or "-")
        ref_age = max(0.0, now_ts - float(self.latest_external_spot_source_ts or 0.0)) if self.latest_external_spot_source_ts > 0 else -1.0
        binance_spot_txt = f"{float(self._binance_ws_price):.2f}" if self._binance_ws_price is not None else "None"
        binance_age = max(0.0, now_ts - float(self._binance_ws_price_ts or 0.0)) if self._binance_ws_price_ts > 0 else -1.0

        logger.info(
            "STATUS "
            f"tradable={tradable} reason={reason_txt} "
            f"phase={self.market_phase.value} "
            f"slug={self.current_market_slug or '-'} "
            f"instrument={self.instrument_id or '-'} "
            f"active_side={self.active_side.value} "
            f"side_score={side_score_txt} "
            f"side_locked={side_locked_txt} "
            f"side_reason={side_reason_txt} "
            f"side_due_in={side_due_in:.1f}s "
            f"ref_spot={ref_spot_txt} ref_src={ref_src_txt} ref_age={ref_age:.1f}s "
            f"binance_spot={binance_spot_txt} binance_age={binance_age:.1f}s "
            f"bid={bid_txt} ask={ask_txt} "
            f"stale_for={stale_for:.1f}s invalid_ticks={self.consecutive_invalid_quote_ticks} "
            f"inventory={float(self.inventory_delta_shares):.4f}/{float(self.maker_max_inventory_shares):.4f} "
            f"active_orders={active_orders}"
            f"{self._format_time_left()}"
        )
        if "active_side_none" in reasons and self.bi_side_enabled:
            throttle_key = f"{self.current_market_slug or '-'}:active_side_none"
            last_ts = float(getattr(self, "_last_no_trade_reason_event_ts_by_key", {}).get(throttle_key, 0.0))
            if now_ts - last_ts >= max(30.0, float(self.strategy_status_interval_sec)):
                if not hasattr(self, "_last_no_trade_reason_event_ts_by_key"):
                    self._last_no_trade_reason_event_ts_by_key = {}
                self._last_no_trade_reason_event_ts_by_key[throttle_key] = now_ts
                self._db_strategy_event(
                    "NO_TRADE_ACTIVE_SIDE_NONE",
                    {
                        "phase": self.market_phase.value,
                        "side_score": float(self.side_decision_score),
                        "side_reason": self.side_decision_reason,
                        "side_locked": bool(self.active_side_locked),
                        "time_left_sec": (
                            float(self.current_market_end_timestamp - now_ts)
                            if self.current_market_end_timestamp is not None
                            else None
                        ),
                    },
                )

    def _format_time_left(self) -> str:
        """Format remaining time and next-market info for status line."""
        parts: List[str] = []
        end_ts = getattr(self, "current_market_end_timestamp", None)
        if end_ts is not None:
            remaining = end_ts - time.time()
            if remaining > 0:
                parts.append(f" time_left={remaining / 60:.1f}m")
            else:
                parts.append(f" time_left=ENDED({abs(remaining):.0f}s ago)")
        if self.next_market_slug:
            if self.next_market_start_ts:
                until = self.next_market_start_ts - time.time()
                parts.append(f" next={self.next_market_slug}(in {until / 60:.1f}m)")
            else:
                parts.append(f" next={self.next_market_slug}")
        return "".join(parts)

    # ------------------------------------------------------------------
    # Market Lifecycle State Machine
    # ------------------------------------------------------------------

    def _update_market_phase(self) -> MarketPhase:
        """
        Evaluate current time vs market end timestamp and transition
        between lifecycle phases.

        Returns the current phase after evaluation.
        """
        now_ts = time.time()
        end_ts = getattr(self, "current_market_end_timestamp", None)
        decision = evaluate_market_phase(
            current_phase_value=self.market_phase.value,
            end_ts=end_ts,
            now_ts=now_ts,
            min_minutes_to_close=self.maker_min_minutes_to_close,
            settling_since_ts=self._market_settling_since_ts,
            settling_grace_sec=self.market_settling_grace_sec,
        )
        if decision is not None:
            if decision.set_settling_since:
                self._market_settling_since_ts = now_ts
            self._transition_market_phase(MarketPhase(decision.next_phase_value), now_ts)

        return self.market_phase

    def _align_price_to_tick(self, price: Decimal, side: str, instrument: Optional[Any]) -> Decimal:
        return align_price_to_tick(self, price, side, instrument)

    def _start_maker_worker(self, bid_decimal: Decimal, ask_decimal: Decimal) -> None:
        start_maker_worker(self, bid_decimal, ask_decimal)

    def _start_grafana_sync(self):
        """Start Grafana in separate thread."""
        try:
            self.grafana_exporter.start()
            logger.info("Grafana metrics started on port 8000")
        except Exception as e:
            logger.error(f"Failed to start Grafana: {e}")
    
    def _find_btc_instrument(self):
        return find_btc_instrument(self)

    def _wait_for_btc_instrument(self, timeout_sec: int = 60, poll_interval_sec: int = 2) -> bool:
        return wait_for_btc_instrument(self, timeout_sec=timeout_sec, poll_interval_sec=poll_interval_sec)
                        
    def on_quote_tick(self, tick: QuoteTick):
        handle_quote_tick(self, tick)

    def _maker_quote_sync(self, bid_price: float, ask_price: float) -> None:
        maker_quote_sync(self, bid_price, ask_price)
	                                            
    def on_order_filled(self, event):
        handle_order_filled(self, event)

    def on_event(self, event):
        handle_generic_event(self, event)

    def on_order_canceled(self, event):
        handle_order_canceled(self, event)
    
    def on_order_cancel_rejected(self, event):
        handle_order_cancel_rejected(self, event)
    
    def on_order_denied(self, event):
        self._handle_order_rejection_like_event(event, title="ORDER DENIED")

    def on_order_rejected(self, event):
        self._handle_order_rejection_like_event(event, title="ORDER REJECTED")

    def _handle_order_rejection_like_event(self, event, title: str = "ORDER REJECTED") -> None:
        handle_order_rejection_like_event(self, event, title=title)
    
    def on_stop(self):
        handle_stop(self)


if __name__ == "__main__":
    from bot.launcher import main

    main()
